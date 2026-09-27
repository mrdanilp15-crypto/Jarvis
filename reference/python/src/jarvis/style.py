"""Jarvis-Stil-Engine (Version 2): Jede Antwort geht durch den Formatter, bevor sie Nutzer oder Stimme erreicht.

Pipeline im Orchestrator::

    Intent-Erkennung  ->  Kontext-Interpretation  ->  Ausführung / LLM  ->  Formatter  ->  Ausgabe
    (Fast Path,           (Raum, Ort, Name aus          (Policy-Gate)        (diese Datei)   (Text, Stream,
     Gesprächs-Intents)    der Situation)                                                     Stimme)

- Deterministische Antworten (Aktionen, Status, Tagesplan, Gesprächs-Intents) kommen aus der Phrasenbank und
  sind damit exakt reproduzierbar.
- Freie LLM-Antworten laufen satzweise durch den Filter – auch beim Streaming: Umgangssprache, Duzen, Emojis,
  Ausrufezeichen und Floskeln werden in den Jarvis-Ton überführt, die Anrede höchstens einmal pro Antwort gesetzt.
- Der Formatter ändert Ton und Form, nie den Inhalt: Er behauptet keine Aktion, die nicht stattgefunden hat,
  und entschärft keine Warnung.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from .persona import Persona
from .voice.pipeline import SentenceSegmenter

STYLE_VERSION = "2.1.0"

WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
          "November", "Dezember"]

# Pflicht-Phrasen (Bausteine). {sir} -> ", Sir" bzw. leer, je nach Anrede.
PHRASES = {
    "of_course": "Selbstverständlich{sir}.",
    "analysis_done": "Analyse abgeschlossen.",
    "on_it": "Ich kümmere mich darum.",
    "systems_stable": "Die Systeme laufen stabil.",
    "as_you_wish": "Wie Sie wünschen.",
    "very_well": "Sehr wohl.",
    "monitoring": "Ich überwache die Situation.",
    "ready": "Natürlich{sir}. Ich stehe bereit.",
    "apology": "Verzeihung{sir}.",
    "at_service": "Stets zu Diensten{sir}.",
}

APP_LABELS = {
    "explorer": "Der Datei-Explorer ist", "browser": "Der Browser ist", "editor": "Der Editor ist",
    "rechner": "Der Rechner ist", "paint": "Paint ist", "einstellungen": "Die Einstellungen sind",
    "taskmanager": "Der Task-Manager ist", "spotify": "Spotify ist", "word": "Word ist", "excel": "Excel ist",
    "powerpoint": "PowerPoint ist", "outlook": "Outlook ist", "systemsteuerung": "Die Systemsteuerung ist",
    "kamera": "Die Kamera ist", "uhr": "Die Uhr ist", "store": "Der Microsoft Store ist",
    "snipping": "Das Ausschneidewerkzeug ist",
}
FOLDER_LABELS = {
    "downloads": "Downloads", "documents": "Dokumente", "desktop": "Desktop", "pictures": "Bilder",
    "music": "Musik", "videos": "Videos", "home": "Benutzerordner", "pc": "Dieser PC",
}
SEARCH_LABELS = {"youtube": "YouTube", "amazon": "Amazon", "wikipedia": "Wikipedia", "ebay": "eBay"}  # google: „Die Suche“
SITE_LABELS = {"youtube.com": "YouTube", "google.de": "Google", "google.com": "Google", "netflix.com": "Netflix",
               "amazon.de": "Amazon", "de.wikipedia.org": "Wikipedia", "mail.google.com": "Gmail",
               "twitch.tv": "Twitch", "ebay.de": "eBay", "web.whatsapp.com": "WhatsApp",
               "instagram.com": "Instagram", "facebook.com": "Facebook"}

# Gesprächsbausteine für Satzanfänge: umgangssprachlicher Auftakt -> Jarvis-Auftakt
_OPENERS = [
    (re.compile(r"^(?:okay|ok|alles klar|klar doch|na klar|klaro|jo|jep|yep|yes|jawohl)\b(?:[\s,]+klar\b)?[\s,.!]*",
                re.I), "Sehr wohl. "),
    (re.compile(r"^klar\s*[.!]?$", re.I), "Selbstverständlich."),
    (re.compile(r"^klar[,!]?\s+(?=(?:kann|mache?|helfe|gern|gerne|doch|wird)\b)", re.I), "Selbstverständlich "),
    (re.compile(r"^(?:super|toll|prima|perfekt|cool|nice|geil|mega|wow)\b[\s,.!]*", re.I), "Ausgezeichnet. "),
    (re.compile(r"^(?:hey|hi|hallo|servus|moin|yo)\b(?:\s+(?:du|da|jarvis))?[\s,.!]*", re.I), ""),
    (re.compile(r"^(?:gute frage|interessante frage)[\s,.!]*", re.I), ""),
    # „Als KI-Sprachmodell kann ich …“ -> „Ich kann …“ (keine Floskeln über das eigene KI-Sein)
    (re.compile(r"^als (?:ki-sprachmodell|ki|sprachmodell|künstliche intelligenz)\s*,?\s*(\w+)\s+ich\b", re.I),
     lambda m: f"Ich {m[1].lower()}"),
    (re.compile(r"^als (?:ki-sprachmodell|ki|sprachmodell|künstliche intelligenz)\s*,?\s*", re.I), ""),
]
# Wörter und Wendungen innerhalb des Satzes (Grundbestand; die Persona kann per lexicon.replace ergänzen)
_REPLACE = {
    r"kein problem": "selbstverständlich",
    r"sorry|sry": "Verzeihung",
    r"ups|oops|hoppla": "Verzeihung",
    r"leider kann ich das nicht": "bedauerlicherweise ist mir das nicht möglich",
    r"krass": "bemerkenswert",
    r"mega": "äußerst",
    r"cool|geil|nice": "ausgezeichnet",
    r"ein bisschen": "etwas",
    r"geklappt": "funktioniert",
    r"(?:das )?mach(?:e)? ich(?: gerne?)?(?: für (?:dich|sie))?|wird gemacht|geht klar": "",
    r"na ja|naja": "nun",
    r"lol|haha\w*|hehe\w*|xd": "",
    r"ich hoffe,? (?:das|dies) hilft(?: (?:dir|ihnen|weiter))?": "",
}
# Duzen -> Siezen (häufige Verbformen zuerst, dann Pronomen)
_DU_VERBS = {
    "hast": "haben", "bist": "sind", "kannst": "können", "willst": "möchten", "möchtest": "möchten",
    "musst": "müssen", "solltest": "sollten", "brauchst": "brauchen", "weißt": "wissen", "siehst": "sehen",
    "darfst": "dürfen", "wirst": "werden", "hättest": "hätten", "wärst": "wären", "würdest": "würden",
    "findest": "finden", "meinst": "meinen", "suchst": "suchen", "planst": "planen", "magst": "mögen",
}
# Befehlsform am Satzanfang: „Öffne …“ -> „Öffnen Sie …“
_IMPERATIVES = {
    "nutze": "Nutzen", "nutz": "Nutzen", "schau": "Schauen", "schaue": "Schauen", "klick": "Klicken",
    "klicke": "Klicken", "öffne": "Öffnen", "gib": "Geben", "denk": "Denken", "denke": "Denken",
    "probier": "Probieren", "probiere": "Probieren", "versuch": "Versuchen", "versuche": "Versuchen",
    "drück": "Drücken", "drücke": "Drücken", "geh": "Gehen", "gehe": "Gehen", "starte": "Starten",
    "prüf": "Prüfen", "prüfe": "Prüfen", "achte": "Achten", "lies": "Lesen", "sieh": "Sehen", "mach": "Machen",
    "mache": "Machen", "tipp": "Tippen", "tippe": "Tippen", "wähl": "Wählen", "wähle": "Wählen",
    "stell": "Stellen", "stelle": "Stellen", "sag": "Sagen", "sage": "Sagen", "schreib": "Schreiben",
    "schreibe": "Schreiben", "installier": "Installieren", "installiere": "Installieren", "melde": "Melden",
}
_IMPERATIVE = re.compile(rf"^({'|'.join(sorted(_IMPERATIVES, key=len, reverse=True))})\b(?!\s+(?:Sie|ich|wir)\b)",
                         re.I)
_DU_PRONOUNS = {"dir": "Ihnen", "dich": "Sie", "dein": "Ihr", "deine": "Ihre", "deinen": "Ihren",
                "deinem": "Ihrem", "deiner": "Ihrer", "deines": "Ihres", "du": "Sie"}
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF⬀-⯿️‍]")
_BOUNDARY = re.compile(r"(?<=[.?…])\s+(?=\S)")
_ABBREV = re.compile(r"(?:^|\s)(?:[A-Za-zÄÖÜäöü]|bzw|ca|usw|etc|nr|dr|ggf|inkl|evtl|vgl|bspw|min|std)\.$", re.I)
_LIST_ITEM = re.compile(r"^(\s*(?:[-*•]|\d+[.)])\s+)(.*)$")


def split_sentences(text: str) -> list[str]:
    """Satzgrenzen ohne Abkürzungen („z. B.“, „bzw.“) und ohne Punkte in Adressen („youtube.com“)."""
    sentences, start = [], 0
    for match in _BOUNDARY.finditer(text):
        head = text[start:match.start()]
        if _ABBREV.search(head):
            continue
        sentences.append(head.strip())
        start = match.end()
    sentences.append(text[start:].strip())
    return [s for s in sentences if s]


def _cap(text: str) -> str:
    if not text or re.match(r"^[a-z][A-Z]", text):  # iPhone, eBay
        return text
    return text[:1].upper() + text[1:]


def format_duration(seconds: int) -> str:
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    for value, one, many in ((hours, "Stunde", "Stunden"), (minutes, "Minute", "Minuten"),
                             (secs, "Sekunde", "Sekunden")):
        if value:
            parts.append(f"{value} {one if value == 1 else many}")
    return " und ".join(parts) or "0 Sekunden"


def format_number(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.1f}".rstrip("0").rstrip(".").replace(".", ",")
    return str(value)


def entity_label(entity_id: str) -> str:
    name = entity_id.split(".", 1)[-1].replace("_", " ")
    for ascii_, umlaut in (("ae", "ä"), ("oe", "ö"), ("ue", "ü")):
        name = name.replace(ascii_, umlaut)
    return " ".join(_cap(word) for word in name.split())


class PlainStyle:
    """Neutraler Ton (Persona „neutral“ oder ausgeschaltet): kurze, sachliche Texte, gleicher Ablauf."""

    version = STYLE_VERSION
    name = "neutral"

    def __init__(self, address: str = "") -> None:
        self.address = address

    # -- Bausteine ---------------------------------------------------------------------------------------
    def phrase(self, key: str) -> str:
        sir = f", {self.address}" if self.address else ""
        return PHRASES[key].format(sir=sir)

    # -- Aktionen ----------------------------------------------------------------------------------------
    def action_reply(self, record: Any, slots: dict[str, Any] | None = None, *, via_confirmation: bool = False) -> str:
        status = record.status
        if status == "succeeded":
            return "Erledigt."
        if status == "pending_confirmation":
            return "Soll ich das wirklich tun?"
        if status == "denied":
            return f"Das darf ich nicht: {record.decision.reason}."
        if status == "rejected":
            return "In Ordnung, ich habe es abgebrochen."
        return (record.error or {}).get("user_message") or "Das hat leider nicht funktioniert."

    def confirmation_prompt(self, capability: Any, arguments: dict[str, Any], method: str) -> str:
        import json

        return f"{capability.description.split('.')[0]}: {json.dumps(arguments, ensure_ascii=False)} – ausführen?"

    def unavailable(self, capability: str) -> str:
        return "Diese Funktion ist derzeit nicht verfügbar."

    # -- Gesprächs-Intents ---------------------------------------------------------------------------------
    def conversation(self, intent: str, situation: Any, *, capabilities: Iterable[str] = ()) -> str:
        now: datetime = situation.now
        if intent == "greeting":
            return "Guten Morgen." if 5 <= now.hour < 11 else "Guten Tag." if now.hour < 18 else "Guten Abend."
        if intent == "time":
            return f"Es ist {now:%H:%M} Uhr."
        if intent == "date":
            return f"Heute ist {WEEKDAYS[now.weekday()]}, der {now.day}. {MONTHS[now.month - 1]} {now.year}."
        if intent == "name":
            name = getattr(situation, "user_display", None)
            return f"Sie sind {name}." if name else "Ihren Namen kenne ich noch nicht."
        return {
            "help": "Gern. Wobei kann ich helfen?", "thanks": "Gern geschehen.", "goodbye": "Bis später.",
            "goodnight": "Gute Nacht.", "identity": "Ich bin JARVIS, Ihr persönlicher Assistent.",
            "how_are_you": "Danke, alles läuft.", "capabilities": "Fragen Sie mich einfach, was Sie brauchen.",
        }.get(intent, "Wie kann ich helfen?")

    # -- Systemtexte -------------------------------------------------------------------------------------
    def system_text(self, key: str) -> str:
        return {
            "timeout": "Das dauert länger als erwartet; ich habe den Vorgang abgebrochen.",
            "not_processed": "Das konnte ich nicht vollständig verarbeiten. Bitte formulieren Sie es anders.",
            "max_iterations": "Ich habe die Aufgabe nach mehreren Schritten angehalten, um nichts Unbeabsichtigtes "
                              "zu tun.",
            "confirm_in_app": "Für diese Aktion benötige ich Ihre Bestätigung in der App.",
            "confirm_same_person": "Diese Bestätigung muss von der Person kommen, die den Auftrag gegeben hat.",
            "generic_error": "Da ist bei mir etwas schiefgegangen.",
        }[key]

    def error_message(self, message: str) -> str:
        return message

    # -- Freitext (LLM) ----------------------------------------------------------------------------------
    def polish(self, text: str, state: dict[str, Any] | None = None, *, list_item: bool = False) -> str:
        return re.sub(r"\s+", " ", text).strip()

    def finalize(self, text: str) -> str:
        return text.strip()

    def stream(self, on_text: Callable[[str], Awaitable[None]] | None) -> PassThrough:
        return PassThrough(on_text)


class JarvisStyle(PlainStyle):
    """Höflich, charmant, britisch-präzise; ruhig und technisch klar; Anrede sparsam; kein Slang."""

    name = "jarvis"

    def __init__(self, address: str = "Sir", replacements: dict[str, str] | None = None) -> None:
        super().__init__(address)
        rules = {**_REPLACE, **{re.escape(k.lower()): v for k, v in (replacements or {}).items()}}
        self._replace = [(re.compile(rf"\b(?:{pattern})\b", re.I), repl) for pattern, repl in rules.items()]
        verbs = "|".join(_DU_VERBS)
        self._du_before = re.compile(rf"\b(du)\s+({verbs})\b", re.I)   # „du hast“ -> „Sie haben“
        self._du_after = re.compile(rf"\b({verbs})\s+(du)\b", re.I)    # „hast du“ -> „haben Sie“
        self._du_pronouns = re.compile(rf"\b({'|'.join(_DU_PRONOUNS)})\b", re.I)

    @classmethod
    def from_persona(cls, persona: Persona | None) -> PlainStyle:
        if persona is None or not persona.enabled or persona.id != "jarvis":
            address = persona.address_for(None) if persona is not None and persona.enabled else ""
            return PlainStyle(address)
        lexicon = persona.config.get("lexicon", {})
        return cls(persona.address_for(None) or "Sir", lexicon.get("replace"))

    # -- Aktionen ----------------------------------------------------------------------------------------
    def action_reply(self, record: Any, slots: dict[str, Any] | None = None, *, via_confirmation: bool = False) -> str:
        slots = slots or {}
        status = record.status
        if status == "succeeded":
            opener, body = self._success(record.capability, record.arguments, record.result, slots)
            if via_confirmation:
                opener = self.phrase("as_you_wish")
            return f"{opener} {body}".strip()
        if status == "pending_confirmation":
            return slots.get("prompt") or "Das erfordert Ihre Bestätigung."
        if status == "denied":
            reason = (record.decision.reason or "").rstrip(".")
            return f"{self.phrase('apology')} Das ist mir nicht gestattet: {reason}."
        if status == "rejected":
            return f"{self.phrase('as_you_wish')} Ich habe den Vorgang abgebrochen."
        if status == "timed_out":
            return f"{self.phrase('apology')} Die Ausführung hat nicht rechtzeitig geantwortet."
        message = (record.error or {}).get("user_message")
        return self.error_message(message) if message else f"{self.phrase('apology')} Das ist nicht gelungen."

    def _success(self, capability: str, args: dict[str, Any], result: Any, slots: dict[str, Any]) -> tuple[str, str]:
        of_course, very_well = self.phrase("of_course"), self.phrase("very_well")
        if capability == "home.set_light":
            where = f" {slots['location']}" if slots.get("location") else ""
            if not args.get("on", True):
                return of_course, f"Das Licht{where} ist nun ausgeschaltet."
            pct = args.get("brightness_pct")
            if pct is not None and pct < 100:
                return of_course, f"Das Licht{where} ist nun auf {pct} Prozent gedimmt."
            return of_course, f"Das Licht{where} ist nun eingeschaltet."
        if capability == "home.lock":
            name = entity_label(args.get("entity_id", "lock.tuer"))
            return of_course, f"{name}: {'verriegelt' if args.get('state') == 'locked' else 'entriegelt'}."
        if capability == "home.set_climate" and args.get("temperature") is not None:
            return very_well, f"Die Heizung ist auf {format_number(args['temperature'])} Grad eingestellt."
        if capability == "home.activate_scene":
            return very_well, "Die Szene ist aktiviert."
        if capability == "timer.start":
            return very_well, (f"Der Timer läuft: {format_duration(args.get('duration_s', 0))}. "
                               f"{self.phrase('on_it')}")
        if capability == "pc.open_app":
            # Der Agent meldet, was er tatsächlich gestartet hat („Steam“, „Minecraft Launcher“)
            opened = result.get("opened") if isinstance(result, dict) else None
            app = str(opened or args.get("app", ""))
            label = APP_LABELS.get(app.lower(), f"{_cap(app)} ist")
            return very_well, f"{label} geöffnet."
        if capability == "pc.open_folder":
            return very_well, f"Der Ordner „{FOLDER_LABELS.get(args.get('folder'), args.get('folder'))}“ ist geöffnet."
        if capability == "pc.open_url":
            host = (urlsplit(str(args.get("url", ""))).hostname or "").removeprefix("www.")
            return very_well, f"{SITE_LABELS.get(host, host or 'Die Seite')} ist geöffnet."
        if capability == "pc.search_web":
            site = SEARCH_LABELS.get(args.get("site") or "google", "")
            return very_well, f"Die {site + '-' if site else ''}Suche nach „{args.get('query', '')}“ ist geöffnet."
        if capability == "pc.search_files":
            return very_well, f"Die Dateisuche nach „{args.get('query', '')}“ ist geöffnet."
        if capability == "memory.remember":
            return very_well, "Ich habe es mir notiert."
        if capability == "system.status":
            if slots.get("intro") == "how_are_you":
                sir = f", {self.address}" if self.address else ""
                return f"Danke der Nachfrage{sir}.", self.status_text(result or {}, casual=True)
            return self.phrase("analysis_done"), self.status_text(result or {})
        if capability == "assistant.day_plan":
            return very_well, self.day_plan_text(result or {})
        return very_well, "Erledigt."

    # -- Status und Tagesplan -------------------------------------------------------------------------
    STATUS_ISSUES = {
        ("sprachmodell", "loading"): "Das Sprachmodell wird noch geladen.",
        ("sprachmodell", "missing"): "Das Sprachmodell fehlt noch – bitte führen Sie ./deploy/start.sh aus.",
        ("sprachmodell", "unavailable"): "Das Sprachmodell ist derzeit nicht erreichbar.",
        ("pc_steuerung", "disconnected"): "Die PC-Steuerung ist nicht verbunden.",
    }
    ESSENTIAL = {"sprachmodell"}

    def status_text(self, result: dict[str, Any], *, casual: bool = False) -> str:
        components: dict[str, str] = result.get("components", {})
        issues = [(name, self.STATUS_ISSUES[(name, state)]) for name, state in components.items()
                  if (name, state) in self.STATUS_ISSUES]
        if not issues:
            return self.phrase("systems_stable") if casual else "Alle Systeme laufen stabil."
        essential = [text for name, text in issues if name in self.ESSENTIAL]
        optional = [text for name, text in issues if name not in self.ESSENTIAL]
        if essential:
            return " ".join(essential + optional + [self.phrase("monitoring")])
        return f"Die Kernsysteme laufen stabil. Hinweis: {' '.join(optional)}"

    def day_plan_text(self, result: dict[str, Any]) -> str:
        day = "morgen" if result.get("day") == "tomorrow" else "heute"
        parts = []
        events = result.get("events")
        if events is None:
            parts.append(f"Für {day} liegen mir keine Termine vor – ein Kalender ist noch nicht verbunden.")
        elif not events:
            parts.append(f"Für {day} sind keine Termine eingetragen.")
        else:
            parts.append(f"Für {day} stehen {len(events)} Termine an: " + "; ".join(events) + ".")
        forecast = result.get("forecast")
        if forecast:
            weather = (f"Das Wetter in {result.get('location', 'Ihrer Region')}: {forecast['conditions']}, "
                       f"{format_number(forecast['temp_min_c'])} bis {format_number(forecast['temp_max_c'])} Grad")
            probability = forecast.get("precipitation_probability_pct")
            if probability:
                weather += f", Regenwahrscheinlichkeit {probability} Prozent"
            parts.append(weather + ".")
            if (probability or 0) >= 60:
                parts.append("Ein Schirm wäre ratsam.")
        elif result.get("weather_note"):
            parts.append(result["weather_note"])
        return " ".join(parts)

    # -- Bestätigungen und Hinweise ---------------------------------------------------------------------
    def confirmation_prompt(self, capability: Any, arguments: dict[str, Any], method: str) -> str:
        name = capability.name
        if name == "home.lock":
            verb = "verriegeln" if arguments.get("state") == "locked" else "entriegeln"
            question = f"Soll ich {entity_label(arguments.get('entity_id', 'lock.tuer'))} {verb}?"
        elif name == "home.set_climate" and arguments.get("temperature") is not None:
            question = f"Soll ich die Heizung auf {format_number(arguments['temperature'])} Grad stellen?"
        elif name == "pc.open_url":
            host = (urlsplit(str(arguments.get("url", ""))).hostname or "die Seite").removeprefix("www.")
            question = f"Soll ich {host} öffnen?"
        elif name in ("home.set_switch", "home.set_light"):
            label = ", ".join(entity_label(e) for e in arguments.get("entity_ids", [])) or "das Gerät"
            question = f"Soll ich {label} {'einschalten' if arguments.get('on') else 'ausschalten'}?"
        else:
            question = f"Soll ich „{capability.description.split('.')[0]}“ ausführen?"
        if method in ("app", "app_biometric", "pin"):
            return f"{question} Dafür benötige ich Ihre Bestätigung in der App{', ' + self.address if self.address else ''}."
        return f"{question} Ein Ja genügt{', ' + self.address if self.address else ''}."

    def unavailable(self, capability: str) -> str:
        domain = capability.split(".", 1)[0]
        detail = {
            "home": "Für das Haus ist noch keine Steuerung verbunden – dafür wird Home Assistant benötigt.",
            "pc": "Die PC-Steuerung ist nicht eingerichtet.",
            "timer": "Timer stehen mir derzeit nicht zur Verfügung.",
        }.get(domain, "Diese Funktion steht mir derzeit nicht zur Verfügung.")
        return f"{self.phrase('apology')} {detail}"

    # -- Gesprächs-Intents ---------------------------------------------------------------------------------
    def conversation(self, intent: str, situation: Any, *, capabilities: Iterable[str] = ()) -> str:
        sir = f", {self.address}" if self.address else ""
        now: datetime = situation.now
        if intent == "help":
            return self.phrase("ready")
        if intent == "thanks":
            return self.phrase("at_service")
        if intent == "greeting":
            hour = now.hour
            greeting = "Guten Morgen" if 5 <= hour < 11 else "Guten Tag" if hour < 18 else "Guten Abend"
            return f"{greeting}{sir}. Womit kann ich dienen?"
        if intent == "identity":
            return f"Ich bin J.A.R.V.I.S., Ihr persönlicher Assistent. {self.phrase('at_service')}"
        if intent == "name":
            name = getattr(situation, "user_display", None)
            if name:
                return f"Sie sind {name}{sir}."
            return (f"Ihren Namen kenne ich noch nicht{sir}. Hinterlegen Sie ihn bitte als JARVIS_USER_NAME "
                    "in deploy/.env.")
        if intent == "time":
            return f"Es ist {now:%H:%M} Uhr{sir}."
        if intent == "date":
            return f"Heute ist {WEEKDAYS[now.weekday()]}, der {now.day}. {MONTHS[now.month - 1]} {now.year}."
        if intent == "goodbye":
            return f"{self.phrase('very_well')} Ich bleibe in Bereitschaft."
        if intent == "goodnight":
            return f"Gute Nacht{sir}."
        if intent == "capabilities":
            return self._capabilities(set(capabilities))
        return self.phrase("ready")

    def _capabilities(self, names: set[str]) -> str:
        skills = []
        domains = {name.split(".", 1)[0] for name in names}
        if "pc" in domains:
            skills.append("Programme und Spiele auf Ihrem PC starten, Ordner und Webseiten öffnen")
            skills.append("im Internet, auf YouTube oder in Ihren Dateien suchen")
        if "home" in domains:
            skills.append("Licht, Heizung und Geräte im Haus steuern")
        if domains & {"info"}:
            skills.append("Wetter, Nachrichten und Wissen nachschlagen")
        if "timer" in domains:
            skills.append("Timer stellen")
        if "memory" in domains:
            skills.append("mir Wichtiges merken")
        skills.append("Ihnen den Systemstatus melden")
        listing = ", ".join(skills[:-1]) + f" und {skills[-1]}" if len(skills) > 1 else skills[0]
        return f"Ich kann {listing}. Sagen Sie einfach, was Sie benötigen{', ' + self.address if self.address else ''}."

    # -- Systemtexte -------------------------------------------------------------------------------------
    def system_text(self, key: str) -> str:
        apology = self.phrase("apology")
        return {
            "timeout": f"{apology} Das dauert länger als vorgesehen – ich habe den Vorgang angehalten.",
            "not_processed": f"{apology} Das konnte ich nicht vollständig verarbeiten. Formulieren Sie es bitte "
                             "anders.",
            "max_iterations": "Ich habe die Aufgabe nach mehreren Schritten angehalten, um nichts Unbeabsichtigtes "
                              "zu veranlassen.",
            "confirm_in_app": f"Für diese Aktion benötige ich Ihre Bestätigung in der App{', ' + self.address if self.address else ''}.",
            "confirm_same_person": "Diese Bestätigung muss von der Person kommen, die den Auftrag erteilt hat.",
            "generic_error": f"{apology} Dabei ist ein Fehler aufgetreten.",
        }[key]

    def stream(self, on_text: Callable[[str], Awaitable[None]] | None) -> StyledStream:
        return StyledStream(self, on_text)

    def error_message(self, message: str) -> str:
        text = self.polish(message)
        if not text:
            return self.system_text("generic_error")
        return text if text.startswith(("Verzeihung", "Bedauerlicherweise")) else f"{self.phrase('apology')} {text}"

    # -- Freitext (LLM) ----------------------------------------------------------------------------------
    def polish(self, text: str, state: dict[str, Any] | None = None, *, list_item: bool = False) -> str:
        """Satzweiser Filter: Slang, Duzen, Emojis, Ausrufezeichen, Floskeln; Anrede höchstens einmal."""
        state = state if state is not None else {}
        code: list[str] = []
        text = re.sub(r"`[^`]+`", lambda m: code.append(m.group(0)) or f"\x00{len(code) - 1}\x00", text)
        text = re.sub(rf"(?:{_EMOJI.pattern})+(?=\s+[A-ZÄÖÜ])", ".", text)  # Emoji als Satzende
        text = _EMOJI.sub("", text)
        for pattern, repl in self._replace:
            text = pattern.sub(repl, text)
        text = self._du_before.sub(lambda m: f"Sie {_DU_VERBS[m[2].lower()]}", text)
        text = self._du_after.sub(lambda m: f"{_DU_VERBS[m[1].lower()]} Sie", text)
        text = self._du_pronouns.sub(lambda m: _DU_PRONOUNS[m[1].lower()], text)
        text = re.sub(r"[!?]*\?[!?]*", "?", text)          # „?!“ -> „?“
        text = re.sub(r"!+", ".", text)                    # ruhig: keine Ausrufezeichen
        text = re.sub(r"\.{3,}", "…", text)
        text = re.sub(r"\s+([,.;:?…])", r"\1", text)
        text = re.sub(r"([,;?])(?=[A-Za-zÄÖÜäöüß])", r"\1 ", text)
        text = re.sub(r"([,;])\s*(?=[.?])", "", text)      # Reste entfernter Wörter („Okay, .“)
        text = re.sub(r"\.(?:\s*\.)+", ".", text)
        sentences = []
        for sentence in split_sentences(re.sub(r"\s+", " ", text).strip()):
            for pattern, repl in _OPENERS:
                sentence = pattern.sub(repl, sentence, count=1)
            sentence = _IMPERATIVE.sub(lambda m: f"{_IMPERATIVES[m[1].lower()]} Sie", sentence, count=1)
            for part in split_sentences(sentence):
                part = part.strip(" ,;")
                if not re.search(r"[\wÄÖÜäöüß]", part.replace("\x00", "")):
                    continue
                if not list_item and not re.search(r"[.?…:;\"“»)\x00]$", part):
                    part += "."
                if self.address:
                    part = self._one_address(part, state)
                sentences.append(_cap(part))
        result = " ".join(s for s in sentences if s)
        return re.sub(r"\x00(\d+)\x00", lambda m: code[int(m[1])], result)

    def finalize(self, text: str) -> str:
        """Ganze Antwort: Code-Blöcke bleiben unberührt, Zeilen und Listen bleiben erhalten."""
        state: dict[str, Any] = {}
        blocks = []
        for block in re.split(r"(```[\s\S]*?```)", text):
            if block.startswith("```"):
                blocks.append(block)
                continue
            lines = []
            for line in block.split("\n"):
                item = _LIST_ITEM.match(line)
                prefix, body = (item[1], item[2]) if item else ("", line)
                lines.append(prefix + self.polish(body, state, list_item=bool(item)) if body.strip() else "")
            blocks.append("\n".join(lines))
        result = re.sub(r"\n{3,}", "\n\n", "".join(blocks)).strip()
        return result or self.phrase("very_well")  # nur Floskeln – nie ungefiltert zurückgeben

    def _one_address(self, sentence: str, state: dict[str, Any]) -> str:
        pattern = re.compile(rf",?\s*\b{re.escape(self.address)}\b(?=[\s,.!?]|$)")
        if state.get("addressed"):
            return re.sub(r"\s+([.,?])", r"\1", pattern.sub("", sentence)).strip()
        if pattern.search(sentence):
            state["addressed"] = True
            first = True

            def keep_first(match: re.Match[str]) -> str:
                nonlocal first
                if first:
                    first = False
                    return match.group(0)
                return ""

            return pattern.sub(keep_first, sentence)
        return sentence


class PassThrough:
    """Neutraler Stil: Deltas unverändert weiterreichen."""

    def __init__(self, on_text: Callable[[str], Awaitable[None]] | None) -> None:
        self.on_text = on_text

    async def __call__(self, delta: str) -> None:
        if self.on_text is not None:
            await self.on_text(delta)

    async def flush(self) -> None:
        return None


class StyledStream:
    """Streaming-Formatter: puffert Deltas zu Sätzen, schickt jeden Satz gefiltert weiter (Text und Stimme)."""

    def __init__(self, style: PlainStyle, on_text: Callable[[str], Awaitable[None]] | None) -> None:
        self.style, self.on_text = style, on_text
        self.segmenter = SentenceSegmenter()
        self.state: dict[str, Any] = {}
        self.raw = False  # sobald Code kommt: unverändert weiterreichen (die Endfassung formatiert korrekt)

    async def __call__(self, delta: str) -> None:
        if not self.raw and "`" in delta:
            self.raw = True
            for sentence in self.segmenter.flush():
                await self._emit(sentence)
        if self.raw:
            if self.on_text is not None:
                await self.on_text(delta)
            return
        for sentence in self.segmenter.feed(delta):
            await self._emit(sentence)

    async def flush(self) -> None:
        for sentence in self.segmenter.flush():
            await self._emit(sentence)

    async def _emit(self, sentence: str) -> None:
        styled = self.style.polish(sentence, self.state)
        if styled and self.on_text is not None:
            await self.on_text(styled + " ")


def examples() -> list[tuple[str, str]]:
    """Beispiel-Outputs des Freitext-Filters (für Doku und Tests)."""
    style = JarvisStyle()
    samples = [
        "Okay, mach ich! Kein Problem 😊",
        "Hey! Klar kann ich dir helfen. Was brauchst du?",
        "Sorry, das hat nicht geklappt. Hast du das Kabel geprüft?",
        "Super!!! Das Wetter wird mega gut, Sir. Genießen Sie es, Sir!",
        "Als KI-Sprachmodell kann ich das nicht fühlen. Ich hoffe, das hilft dir.",
    ]
    return [(sample, style.finalize(sample)) for sample in samples]


if __name__ == "__main__":  # python -m jarvis.style
    for before, after in examples():
        print(f"{before}\n  -> {after}\n")
