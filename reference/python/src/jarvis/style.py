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

STYLE_VERSION = "2.7.0"

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
SEARCH_LABELS = {  # google: „Die Suche“
    "youtube": "YouTube", "amazon": "Amazon", "wikipedia": "Wikipedia", "ebay": "eBay", "spotify": "Spotify",
    "netflix": "Netflix", "twitch": "Twitch", "github": "GitHub", "reddit": "Reddit", "maps": "Google-Maps",
    "idealo": "Idealo", "chefkoch": "Chefkoch", "tiktok": "TikTok", "bing": "Bing", "soundcloud": "SoundCloud",
    "steam": "Steam", "zalando": "Zalando", "otto": "Otto", "mediamarkt": "MediaMarkt",
    "kleinanzeigen": "Kleinanzeigen", "pinterest": "Pinterest", "imdb": "IMDb", "duckduckgo": "DuckDuckGo",
}
KEY_REPLIES = {
    "play_pause": "Wiedergabe umgeschaltet.", "next_track": "Nächster Titel.", "previous_track": "Vorheriger Titel.",
    "stop_media": "Wiedergabe gestoppt.", "volume_up": "Etwas lauter.", "volume_down": "Etwas leiser.",
    "mute": "Ton umgeschaltet.", "copy": "Kopiert.", "paste": "Eingefügt.", "cut": "Ausgeschnitten.",
    "undo": "Rückgängig gemacht.", "redo": "Wiederhergestellt.", "select_all": "Alles markiert.",
    "save": "Speichern ist ausgelöst.", "new_tab": "Ein neuer Tab ist geöffnet.", "close_tab": "Der Tab ist geschlossen.",
    "reopen_tab": "Der letzte Tab ist wieder geöffnet.", "next_tab": "Nächster Tab.", "previous_tab": "Vorheriger Tab.",
    "back": "Eine Seite zurück.", "forward": "Eine Seite vor.", "refresh": "Die Seite wird neu geladen.",
    "fullscreen": "Vollbild umgeschaltet.", "zoom_in": "Vergrößert.", "zoom_out": "Verkleinert.",
    "page_down": "Nach unten geblättert.", "page_up": "Nach oben geblättert.", "switch_window": "Fenster gewechselt.",
    "close_window": "Das Fenster wird geschlossen.", "find": "Die Suche auf der Seite ist geöffnet.",
}
KEY_LABELS = {"enter": "Enter", "tab": "Tab", "escape": "Escape", "space": "Leertaste", "backspace": "Rücktaste",
              "delete": "Entfernen", "up": "Pfeil hoch", "down": "Pfeil runter", "left": "Pfeil links",
              "right": "Pfeil rechts", "home": "Pos1", "end": "Ende", "print": "Drucken"}
CLARIFY = {
    "search": "Wonach soll ich suchen{sir}?", "open": "Was soll ich öffnen{sir}?", "play": "Was soll ich abspielen{sir}?",
    "close": "Welches Programm soll ich schließen{sir}?", "type": "Was soll ich schreiben{sir}?",
    "when": "Wann soll ich Sie erinnern{sir}?", "when_event": "Wann ist der Termin{sir}?",
}
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
    # Erfundene Dauerüberwachung („Ich überwache stets die Kommunikation“) – JARVIS hört nur nach „Jarvis“ zu
    (re.compile(r"^ich (?:überwache|beobachte|belausche|höre)\s+(?:stets|ständig|permanent|kontinuierlich|laufend|immer|"
                r"jederzeit|rund um die uhr|(?:die |alle |ihre )?(?:kommunikation|gespräche|umgebung|geräusche))\b.*$",
                re.I), ""),
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
_ABBREV = re.compile(r"(?:(?:^|\s)(?:[A-Za-zÄÖÜäöü]|bzw|ca|usw|etc|nr|dr|ggf|inkl|evtl|vgl|bspw|min|std)\.|"
                     r"(?:[A-Za-zÄÖÜäöü]\.){2,})$", re.I)  # auch Initialen wie „A.R.T.E.“
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


FOLDER_SEGMENTS = {"Documents": "Dokumente", "Pictures": "Bilder", "Music": "Musik", "Videos": "Videos",
                   "Desktop": "Desktop", "Downloads": "Downloads"}


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


def _short(text: str | None, limit: int = 70) -> str:
    text = re.sub(r"[„“\"]+", "", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _quoted(text: str, limit: int = 120) -> str:
    """Gehörtes wörtlich in Anführungszeichen – ohne zweites Satzzeichen nach „…?“."""
    said = _short(text, limit)
    return f"„{verbatim(said)}“" + ("" if re.search(r"[.?!…]$", said) else ".")


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + f" und {parts[-1]}"


def folder_label(folder: str | None) -> str:
    """„Documents\\Bewerbungen“ -> „Bewerbungen“, „Documents“ -> „Dokumente“ (vorlesbar, ohne Pfadzeichen)."""
    parts = [p for p in re.split(r"[\\/]+", folder or "") if p]
    if not parts:
        return "Benutzerordner"
    return parts[-1] if len(parts) > 1 else FOLDER_SEGMENTS.get(parts[0], parts[0])


def listing_text(capability: str, args: dict[str, Any], result: Any) -> str | None:
    """Trefferlisten (Dateien, Links) zum Vorlesen – mit Frage, welcher Eintrag geöffnet werden soll."""
    if not isinstance(result, dict):
        return None
    query = args.get("query", "")
    items = [i for i in result.get("results") or [] if isinstance(i, dict)]
    if capability == "web.search":
        if not items:
            return f"Zu „{query}“ habe ich keine Treffer gefunden."
        names = [f"„{_short(i.get('title'), 60)}“ auf {_host(str(i.get('url', '')))}" for i in items[:3]]
        return f"Die besten Treffer zu „{query}“: {_join(names)}. {_which(len(names), 'Welchen')}"
    if capability == "mail.list_unread":
        total = int(result.get("total") or len(items))
        if not items:
            return "Es liegen keine ungelesenen E-Mails vor."
        names = [f"von {verbatim(i.get('from'))}: „{verbatim(_short(i.get('subject'), 80))}“" for i in items[:3]]
        if total == 1:
            return f"Eine ungelesene E-Mail {names[0]}. Soll ich sie vorlesen?"
        return (f"Sie haben {total} ungelesene E-Mails. Die neuesten: {_join(names)}. "
                f"{_which(len(names), 'Welche', 'vorlesen')}")
    if capability == "pc.find_files":
        if not items:
            return f"In Ihren Dateien finde ich nichts zu „{query}“."
        total = int(result.get("total") or len(items))
        if total == 1:
            item = items[0]
            pronoun = "ihn" if item.get("kind") == "folder" else "sie"
            return f"Gefunden: „{item.get('name')}“ im Ordner {folder_label(item.get('folder'))}. Soll ich {pronoun} öffnen?"
        count = f"{total}" if total < 20 else "mindestens 20"
        names = [f"„{i.get('name')}“ in {folder_label(i.get('folder'))}" for i in items[:3]]
        return f"Ich habe {count} Treffer gefunden, die neuesten: {_join(names)}. {_which(len(names), 'Welchen')}"
    return None


def _which(count: int, word: str, verb: str = "öffnen") -> str:
    article = ("die erste oder die zweite", "die erste, zweite oder dritte") if word == "Welche" else (
        "den ersten oder den zweiten", "den ersten, zweiten oder dritten")
    options = {2: article[0], 3: article[1]}.get(count)
    return f"{word} soll ich {verb} – {options}?" if options else f"{word} soll ich {verb}?"


VERBATIM = "\u2063"  # unsichtbares Trennzeichen: Fremdtext dazwischen (Betreff, Mailtext) formatiert JARVIS nicht um


def verbatim(text: str | None) -> str:
    return f"{VERBATIM}{(text or '').replace(VERBATIM, '')}{VERBATIM}"


def clock(moment: datetime) -> str:
    return f"{moment.hour} Uhr" if moment.minute == 0 else f"{moment.hour}:{moment.minute:02d} Uhr"


def spoken_when(moment: datetime, now: datetime | None = None, *, all_day: bool = False) -> str:
    """„heute um 15:30 Uhr“, „morgen um 8 Uhr“, „am Freitag um 10 Uhr“, „am 3. Oktober“, „in 10 Minuten“.
    Gerechnet wird in der Ortszeit des Zeitpunkts (Wanduhr)."""
    local = moment.replace(tzinfo=None)
    now = (now or datetime.now(moment.tzinfo)).replace(tzinfo=None)
    seconds = (local - now).total_seconds()
    if not all_day and 0 < seconds < 3600:
        return f"in {format_duration(round(seconds / 60) * 60 or int(seconds))}"
    days = (local.date() - now.date()).days
    at = "" if all_day else f" um {clock(local)}"
    if days == 0:
        return f"heute{at}"
    if days == 1:
        return f"morgen{at}"
    if days == 2:
        return f"übermorgen{at}"
    if 2 < days < 7:
        return f"am {WEEKDAYS[local.weekday()]}{at}"
    return f"am {local.day}. {MONTHS[local.month - 1]}{at}"


def _parse_moment(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _reference(result: Any) -> datetime:
    """Bezugszeit für „heute/morgen“: die Uhr des Dienstes (im Ergebnis), sonst jetzt."""
    now = _parse_moment(result.get("now")) if isinstance(result, dict) else None
    return (now or datetime.now().astimezone()).replace(tzinfo=None)


def calendar_text(args: dict[str, Any], result: Any) -> str:
    events = [e for e in (result or {}).get("events") or [] if isinstance(e, dict)]
    now = _reference(result)
    if args.get("upcoming"):
        if not events:
            return "Es stehen keine Termine an."
        event = events[0]
        moment = _parse_moment(f"{event['date']}T{event['time'] or '00:00'}")
        return f"Ihr nächster Termin: {verbatim(event['title'])}, {spoken_when(moment, now, all_day=not event['time'])}."
    first = _parse_moment((result or {}).get("from"))
    days = int((result or {}).get("days") or 1)
    label = "In diesem Zeitraum" if days > 1 else _cap(spoken_when(first, now, all_day=True)) if first else "Heute"
    if not events:
        return f"{label} sind keine Termine eingetragen."

    def entry(event: dict[str, Any]) -> str:
        when = f"um {clock(_parse_moment(event['date'] + 'T' + event['time']))}" if event.get("time") else "ganztägig"
        if days > 1:
            day = _parse_moment(event["date"])
            when = f"{WEEKDAYS[day.weekday()]} {when}" if day else when
        return f"{when} {verbatim(event['title'])}"

    count = "ein Termin" if len(events) == 1 else f"{len(events)} Termine"
    return f"{label} {'steht' if len(events) == 1 else 'stehen'} {count} an: {_join([entry(e) for e in events[:8]])}."


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
            return listing_text(record.capability, record.arguments, record.result) or "Erledigt."
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

    def clarify(self, kind: str, site: str | None = None) -> str:
        """Rückfrage bei unvollständigem Befehl („Such mal …“)."""
        sir = f", {self.address}" if self.address else ""
        if kind == "search" and site:
            return f"Wonach soll ich auf {SEARCH_LABELS.get(site, site.capitalize()).replace('-', ' ')} suchen{sir}?"
        return CLARIFY.get(kind, "Worum geht es{sir}?").format(sir=sir)

    # -- Nachgeschlagenes Wissen ----------------------------------------------------------------------------
    def knowledge_text(self, source: str | None, text: str) -> str:
        """Quelle wörtlich vorlesen: „Laut Wikipedia: …“ (Folgesätze bei „mehr“ ohne Quellenangabe)."""
        return f"Laut {source}: {verbatim(text)}" if source else verbatim(text)

    def not_found_text(self, name: str, *, offer: bool, searched_web: bool = True) -> str:
        sir = f", {self.address}" if self.address else ""
        text = (f"Zu „{verbatim(name)}“ finde ich nichts Verlässliches{sir} – weder in der Wikipedia noch in der "
                "Websuche." if searched_web else
                f"Zu „{verbatim(name)}“ steht nichts in der Wikipedia{sir}, und die Websuche ist gerade nicht "
                "erreichbar.")
        return f"{text} Soll ich im Browser danach suchen?" if offer else text

    def no_more_text(self, *, offer: str | None) -> str:
        """Kein weiterer Text zum Thema; ``offer``: „article“ (Artikel öffnen), „search“ (Browser-Suche) oder None."""
        sir = f", {self.address}" if self.address else ""
        text = f"Mehr steht in meinen Quellen nicht{sir}."
        question = {"article": " Soll ich den Wikipedia-Artikel öffnen?", "search": " Soll ich im Browser weitersuchen?"}
        return text + question.get(offer or "", "")

    # -- Gesprächs-Intents ---------------------------------------------------------------------------------
    def conversation(self, intent: str, situation: Any, *, capabilities: Iterable[str] = (), heard: str = "") -> str:
        now: datetime = situation.now
        if intent == "hear_check":
            return f"Ja, ich verstehe Sie. Angekommen ist: {_quoted(heard)}" if heard else "Ja, ich verstehe Sie."
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

    # -- E-Mail-Assistent -------------------------------------------------------------------------------
    def mail_text(self, kind: str, draft: Any, *, heard: str = "") -> str:
        """Rückfragen des E-Mail-Assistenten (maildialog.py). Adressen und Betreff bleiben wörtlich."""
        sir = f", {self.address}" if self.address else ""
        dictate = "„at“ für das @ und „Punkt“ für den Punkt"
        if kind == "ask_to":
            return f"An wen soll die E-Mail gehen{sir}? Nennen Sie einen Kontakt oder diktieren Sie die Adresse – {dictate}."
        if kind == "ask_to_again":
            return f"Verzeihung{sir}. Wie lautet die Adresse? Diktieren Sie sie bitte in Ruhe – {dictate}."
        if kind == "bad_to":
            return (f"Daraus wird keine gültige Adresse{sir}: „{verbatim(_short(heard, 80))}“. Bitte noch einmal – "
                    "etwa „max punkt mustermann at gmx punkt de“.")
        if kind == "unknown_contact":
            return (f"„{verbatim(heard)}“ steht nicht in meinen Kontakten{sir}. Diktieren Sie die Adresse bitte – oder "
                    "sagen Sie „weiter“, dann tragen Sie sie im Entwurf selbst ein.")
        if kind == "ask_subject":
            address, named = draft.address, draft.to
            if address and named and not re.search(r"@|\b(?:at|punkt)\b", named, re.I) and \
                    named.lower() != address.lower():
                target = f"An {verbatim(_cap(named))} ({verbatim(address)}){sir}."  # Kontakt: Name und Adresse
            elif address:
                target = f"An {verbatim(address)}{sir}."
            elif named:
                target = f"An „{verbatim(named)}“ – die Adresse tragen Sie im Entwurf ein{sir}."
            else:
                target = f"Ohne Empfänger – den tragen Sie im Entwurf ein{sir}."
            return f"{target} Wie lautet der Betreff?"
        if kind == "ask_subject_again":
            return f"Verzeihung{sir}. Wie soll der Betreff lauten?"
        if kind == "ask_body":
            subject = f"Betreff: „{verbatim(draft.subject)}“." if draft.subject else "Ohne Betreff."
            return f"{subject} Was soll in der E-Mail stehen{sir}?"
        if kind == "cancel":
            return f"{self.phrase('very_well')} Die E-Mail ist verworfen."
        return self.phrase("very_well")

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
            "no_passwords": "Passwörter tippe ich nicht ein – dafür ist der Passwortmanager Ihres Browsers sicherer.",
        }[key]

    def error_message(self, message: str) -> str:
        return message

    # -- Freitext (LLM) ----------------------------------------------------------------------------------
    def polish(self, text: str, state: dict[str, Any] | None = None, *, list_item: bool = False) -> str:
        return re.sub(r"\s+", " ", text).strip()

    def finalize(self, text: str) -> str:
        return text.replace(VERBATIM, "").strip()

    def alarm_text(self, kind: str, label: str, *, duration_s: int | None = None, due: datetime | None = None,
                   late: bool = False) -> str:
        """Meldung, wenn ein Timer, eine Erinnerung oder ein Termin fällig ist."""
        sir = f"{self.address}, " if self.address else ""
        prefix = "Während ich nicht erreichbar war, wurde fällig: " if late else ""
        if kind == "timer":
            name = f"der Timer „{label}“" if label else (f"Ihr Timer über {format_duration(duration_s)}" if duration_s
                                                         else "Ihr Timer")
            return f"{prefix}{sir}{name} ist abgelaufen." if not late else f"{prefix}{name}."
        if kind == "event":
            minutes, _, title = label.partition("|")
            return f"{prefix}{sir}in {minutes} Minuten: {title}." if minutes.isdigit() else f"{prefix}{sir}{label}."
        if label == "Aufstehen":
            greeting = "Guten Morgen" if due is None or due.hour < 11 else "Hallo"
            at = f"Es ist {clock(due)} – " if due else ""
            return f"{greeting}{', ' + self.address if self.address else ''}. {at}Zeit aufzustehen."
        return f"{prefix}{sir}Sie wollten erinnert werden: {label}."

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
        where = f" {slots['location']}" if slots.get("location") else ""
        name = verbatim(slots["name"]) if slots.get("name") else ""
        if capability == "home.set_light":
            on, pct = args.get("on", True), args.get("brightness_pct")
            subject = "Alle Lichter sind" if slots.get("all") else f"{name} ist" if name else f"Das Licht{where} ist"
            if not on:
                return of_course, f"{subject} nun ausgeschaltet."
            if pct is not None and pct < 100:
                return of_course, f"{subject} nun auf {pct} Prozent gedimmt."
            return of_course, f"{subject} nun eingeschaltet."
        if capability == "home.set_switch":
            subject = name or "Der Schalter"
            return of_course, f"{subject} ist nun {'eingeschaltet' if args.get('on') else 'ausgeschaltet'}."
        if capability == "home.set_cover":
            what = {"rollos": "Rollos", "rollo": "Das Rollo", "jalousien": "Jalousien", "jalousie": "Die Jalousie",
                    "markise": "Die Markise", "markisen": "Markisen", "vorhang": "Der Vorhang",
                    "vorhänge": "Vorhänge"}.get(slots.get("what", ""), "Rollläden")
            single = what.startswith(("Das ", "Die ", "Der "))
            subject = what if single else f"Die {what}"
            position = args.get("position", 0)
            motion = "fährt" if single else "fahren"
            target = "hoch" if position >= 100 else "herunter" if position <= 0 else f"auf {position} Prozent"
            return very_well, f"{subject}{where} {motion} {target}."
        if capability == "home.lock":
            label = name or entity_label(args.get("entity_id", "lock.tuer"))
            return of_course, f"{label}: {'verriegelt' if args.get('state') == 'locked' else 'entriegelt'}."
        if capability == "home.set_climate" and args.get("temperature") is not None:
            return very_well, f"Die Heizung{where} ist auf {format_number(args['temperature'])} Grad eingestellt."
        if capability == "home.set_climate" and args.get("hvac_mode"):
            state = "ausgeschaltet" if args["hvac_mode"] == "off" else "eingeschaltet"
            return very_well, f"Die Heizung{where} ist {state}."
        if capability == "home.activate_scene":
            return very_well, f"Die Szene „{name}“ ist aktiviert." if name else "Die Szene ist aktiviert."
        if capability == "timer.start":
            label = f" „{verbatim(args['label'])}“" if args.get("label") else ""
            due = _parse_moment((result or {}).get("due")) if isinstance(result, dict) else None
            tail = f" Ich melde mich um {clock(due)}." if due and args.get("duration_s", 0) >= 300 else ""
            return very_well, f"Der Timer{label} läuft: {format_duration(args.get('duration_s', 0))}.{tail}"
        if capability == "reminder.create":
            due = _parse_moment((result or {}).get("due")) if isinstance(result, dict) else None
            when = spoken_when(due, _reference(result)) if due else "zur gewünschten Zeit"
            if args.get("text") == "Aufstehen":
                return very_well, f"Ich wecke Sie {when}."
            return very_well, f"Ich erinnere Sie {when} an: {verbatim(args.get('text'))}."
        if capability == "timer.list":
            alarms = (result or {}).get("alarms") or [] if isinstance(result, dict) else []
            if not alarms:
                return "", "Derzeit laufen weder Timer noch Erinnerungen."
            parts = []
            for alarm in alarms[:6]:
                name = f" „{verbatim(alarm['label'])}“" if alarm.get("label") else ""
                if alarm.get("kind") == "timer":
                    parts.append(f"Timer{name}: noch {format_duration(alarm.get('remaining_s', 0))}")
                else:
                    due = _parse_moment(alarm.get("due"))
                    parts.append(f"Erinnerung{name}: {spoken_when(due, _reference(result)) if due else ''}".rstrip(": "))
            return "", f"Aktiv {'ist' if len(parts) == 1 else 'sind'}: {_join(parts)}."
        if capability == "timer.cancel":
            cancelled = (result or {}).get("cancelled") or [] if isinstance(result, dict) else []
            if not cancelled:
                return self.phrase("apology"), "Es läuft kein passender Timer."
            if len(cancelled) > 1:
                return very_well, f"{len(cancelled)} Timer und Erinnerungen sind gestoppt."
            item = cancelled[0]
            name = f" „{verbatim(item['label'])}“" if item.get("label") else ""
            if item.get("kind") == "reminder":
                return very_well, f"Die Erinnerung{name} ist gelöscht."
            return very_well, f"Der Timer{name} ist gestoppt."
        if capability == "calendar.add":
            event = (result or {}).get("event") or {} if isinstance(result, dict) else {}
            moment = _parse_moment(f"{event.get('date')}T{event.get('time') or '00:00'}")
            when = spoken_when(moment, _reference(result), all_day=not event.get("time")) if moment else ""
            text = f"Eingetragen: {verbatim(event.get('title') or args.get('title'))} {when}"
            if not event.get("time"):
                return very_well, f"{text}, ganztägig."
            minutes = (result or {}).get("reminder_minutes")
            return very_well, f"{text}. Ich erinnere Sie {minutes} Minuten vorher." if minutes else f"{text}."
        if capability == "calendar.list":
            return "", calendar_text(args, result)
        if capability == "calendar.delete":
            deleted = (result or {}).get("deleted") or [] if isinstance(result, dict) else []
            if not deleted:
                return self.phrase("apology"), "Einen solchen Termin finde ich in Ihrem JARVIS-Kalender nicht."
            names = [verbatim(e.get("title")) for e in deleted]
            return very_well, f"Gelöscht: {_join(names)}."
        if capability == "mail.read":
            mail = result if isinstance(result, dict) else {}
            text = _short(mail.get("text"), 1200) or "(kein Text)"
            return "", (f"E-Mail von {verbatim(mail.get('from'))}, Betreff „{verbatim(mail.get('subject'))}“: "
                        f"{verbatim(text)}")
        if capability == "mail.list_unread":
            return "", listing_text(capability, args, result) or "Erledigt."
        if capability == "pc.open_app":
            # Der Agent meldet, was er tatsächlich gestartet hat („Steam“, „Minecraft Launcher“)
            opened = result.get("opened") if isinstance(result, dict) else None
            app = str(opened or args.get("app", ""))
            label = APP_LABELS.get(app.lower(), f"{_cap(app)} ist")
            return very_well, f"{label} geöffnet."
        if capability == "pc.open_folder":
            return very_well, f"Der Ordner „{FOLDER_LABELS.get(args.get('folder'), args.get('folder'))}“ ist geöffnet."
        if capability == "pc.open_url":
            host = _host(str(args.get("url", "")))
            if slots.get("title"):  # aus einer Trefferliste gewählt
                return very_well, f"„{_short(slots['title'])}“ auf {host} ist geöffnet."
            return very_well, f"{SITE_LABELS.get(host, host or 'Die Seite')} ist geöffnet."
        if capability == "pc.open_link":
            result = result if isinstance(result, dict) else {}
            if slots.get("site") and slots.get("login") is not False and "anmelden" in str(args.get("query", "")):
                return very_well, (f"Die Anmeldeseite von {slots['site']} ist geöffnet. Passwörter gebe ich aus "
                                   "Sicherheitsgründen nicht ein – das übernimmt der Passwortmanager Ihres Browsers.")
            if result.get("url"):
                return very_well, f"„{_short(result.get('title'))}“ auf {_host(result['url'])} ist geöffnet."
            what = "Der erste YouTube-Treffer" if args.get("site") == "youtube" else "Der erste Treffer"
            return very_well, f"{what} für „{args.get('query', '')}“ ist geöffnet."
        if capability == "pc.open_file":
            result = result if isinstance(result, dict) else {}
            name, where = result.get("opened") or args.get("query", ""), folder_label(result.get("folder"))
            if result.get("blocked"):
                return very_well, (f"„{name}“ ist ein Programm – ich habe es im Explorer markiert, statt es zu "
                                   "starten.")
            if result.get("shown"):
                return very_well, f"„{name}“ ist im Explorer markiert, im Ordner {where}."
            if result.get("kind") == "folder":
                return very_well, f"Der Ordner „{name}“ ist geöffnet."
            return very_well, f"„{name}“ aus dem Ordner {where} ist geöffnet."
        if capability == "pc.close_app":
            result = result if isinstance(result, dict) else {}
            name = str(result.get("closed") or args.get("app", ""))
            if name.lower() == "explorer":
                return very_well, "Die Explorer-Fenster werden geschlossen."
            label = {"browser": "Der Browser", "editor": "Der Editor", "rechner": "Der Rechner",
                     "taskmanager": "Der Task-Manager"}.get(name.lower(), _cap(name))
            return very_well, f"{label} wird geschlossen."
        if capability == "pc.type_text":
            sent = " und abgeschickt" if args.get("enter") else ""
            return very_well, f"Der Text ist eingefügt{sent}."
        if capability == "pc.press_key":
            key = args.get("key", "")
            return very_well, KEY_REPLIES.get(key) or f"{KEY_LABELS.get(key, key)} gedrückt."
        if capability == "pc.click":
            clicked = (result or {}).get("clicked") if isinstance(result, dict) else None
            return very_well, f"Ich habe auf „{_short(clicked or args.get('label'), 60)}“ geklickt."
        if capability == "pc.compose_mail":
            result = result if isinstance(result, dict) else {}
            if result.get("unknown_recipient"):
                return very_well, (f"Der E-Mail-Entwurf ist geöffnet. Die Adresse von „{result['unknown_recipient']}“ "
                                   "kenne ich noch nicht – tragen Sie sie bitte ein.")
            if result.get("to"):
                return very_well, f"Der E-Mail-Entwurf an {result['to']} ist geöffnet – Sie müssen ihn nur noch absenden."
            return very_well, "Ein neuer E-Mail-Entwurf ist geöffnet."
        if capability in ("pc.find_files", "web.search"):
            return "", listing_text(capability, args, result) or "Erledigt."
        if capability == "pc.search_web":
            site = SEARCH_LABELS.get(args.get("site") or "google", "")
            return very_well, f"Die {site + '-' if site else ''}Suche nach „{args.get('query', '')}“ ist geöffnet."
        if capability == "pc.search_files":
            text = f"Die Dateisuche nach „{args.get('query', '')}“ ist geöffnet."
            items = (result or {}).get("results") if isinstance(result, dict) else None
            if items:
                text += f" Der neueste Treffer: „{items[0].get('name')}“ in {folder_label(items[0].get('folder'))}."
            return very_well, text
        if capability == "memory.remember":
            return very_well, "Ich habe es mir notiert."
        if capability == "memory.list":
            items = (result or {}).get("memories") if isinstance(result, dict) else None
            if not items:
                return "", "Bislang habe ich mir nichts über Sie gemerkt. Sagen Sie einfach „Merk dir, dass …“."
            lines = "\n".join(f"- {verbatim(i['content'])}" for i in items)
            return "", f"Folgendes habe ich mir gemerkt:\n{lines}"
        if capability == "memory.forget":
            gone = (result or {}).get("forgotten") if isinstance(result, dict) else None
            if gone:
                return very_well, f"Vergessen: „{verbatim(gone)}“."
            return "", "Dazu habe ich nichts gespeichert – „Was weißt du über mich?“ zeigt alles, was ich mir gemerkt habe."
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
        ("smart_home", "disconnected"): "Home Assistant ist gerade nicht erreichbar.",
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
            "home": "Das Smart Home ist noch nicht verbunden. Im Zahnrad-Menü unter „Smart Home“ suche ich Home "
                    "Assistant und verbinde mich mit einem Klick.",
            "pc": "Die PC-Steuerung ist nicht eingerichtet.",
            "timer": "Timer stehen mir derzeit nicht zur Verfügung.",
        }.get(domain, "Diese Funktion steht mir derzeit nicht zur Verfügung.")
        return f"{self.phrase('apology')} {detail}"

    # -- Gesprächs-Intents ---------------------------------------------------------------------------------
    def conversation(self, intent: str, situation: Any, *, capabilities: Iterable[str] = (), heard: str = "") -> str:
        sir = f", {self.address}" if self.address else ""
        now: datetime = situation.now
        if intent == "hear_check":
            # Ehrlich statt „Ich überwache die Kommunikation“: zeigen, was die Spracherkennung geliefert hat
            if not heard:
                return f"Laut und deutlich{sir}."
            return f"Laut und deutlich{sir}. Bei mir angekommen ist: {_quoted(heard)}"
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
            skills.append("Programme und Spiele auf Ihrem PC starten und schließen")
            skills.append("Links, Dateien und Ordner heraussuchen und öffnen")
            skills.append("tippen, klicken, Tasten und Musik steuern")
        if "home" in domains:
            skills.append("Licht, Heizung und Geräte im Haus steuern")
        if domains & {"info"}:
            skills.append("Wetter, Nachrichten und Wissen nachschlagen")
        if "timer" in domains:
            skills.append("Timer und Erinnerungen stellen")
        if "calendar" in domains:
            skills.append("Termine eintragen und nennen")
        if "mail" in domains:
            skills.append("E-Mails vorlesen")
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
            "no_passwords": (f"Passwörter tippe ich aus Sicherheitsgründen nicht ein{', ' + self.address if self.address else ''} "
                             "– sie liefen dabei durch Spracherkennung und Protokoll. Der Passwortmanager Ihres "
                             "Browsers erledigt das sicherer."),
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
        text = re.sub(r"\s+([,.;:?…])(?=\s|$)", r"\1", text)  # nicht „mit ./deploy/start.sh“
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
                if not list_item and not re.search(r"[.?…:;\"“»)\x00\x01]$", part):
                    part += "."
                if self.address:
                    part = self._one_address(part, state)
                sentences.append(_cap(part))
        result = " ".join(s for s in sentences if s)
        return re.sub(r"\x00(\d+)\x00", lambda m: code[int(m[1])], result)

    def finalize(self, text: str) -> str:
        """Ganze Antwort: Code-Blöcke bleiben unberührt, Zeilen und Listen bleiben erhalten."""
        state: dict[str, Any] = {}
        # Fremdtext (Mailtext, Betreff, Termintitel) bleibt wörtlich: Platzhalter statt Text, am Ende zurück
        kept: list[str] = []
        text = re.sub(f"{VERBATIM}([^{VERBATIM}]*){VERBATIM}",
                      lambda m: kept.append(m[1]) or f"\x01{len(kept) - 1}\x01", text)
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
        result = re.sub(r"\x01(\d+)\x01", lambda m: kept[int(m[1])], result).replace(VERBATIM, "")
        return result or self.phrase("very_well")  # nur Floskeln – nie ungefiltert zurückgeben

    def _one_address(self, sentence: str, state: dict[str, Any]) -> str:
        pattern = re.compile(rf",?\s*\b{re.escape(self.address)}\b(?=[\s,.!?]|$)")
        if state.get("addressed"):
            return re.sub(r"\s+([.,?])(?=\s|$)", r"\1", pattern.sub("", sentence)).strip()
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
