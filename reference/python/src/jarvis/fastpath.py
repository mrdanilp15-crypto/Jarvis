"""Fast-Path-NLU: deterministische Grammatiken für häufige Befehle – ohne LLM, < 300 ms.

Im Betrieb werden die Muster aus Home-Assistant-Intents (hassil-kompatibel) und dem Entitätsregister
generiert; dieses Modul zeigt das Prinzip an Licht-, Timer-, PC- und Bestätigungsbefehlen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

NUMBER_WORDS = {
    "null": 0, "eins": 1, "ein": 1, "eine": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "sechs": 6,
    "sieben": 7, "acht": 8, "neun": 9, "zehn": 10, "elf": 11, "zwölf": 12, "fünfzehn": 15, "zwanzig": 20,
    "dreißig": 30, "vierzig": 40, "fünfzig": 50, "sechzig": 60, "siebzig": 70, "achtzig": 80,
    "neunzig": 90, "hundert": 100,
}

# Bestätigungsantworten müssen die *gesamte* Äußerung sein – "Mach das Licht an" ist kein "Ja".
YES = re.compile(r"^(ja(\s+(bitte|gerne|mach das|genau))?|jawohl|bestätigt|bestätige|mach das|genau|korrekt|freigeben)"
                 r"(\s+(bitte|jarvis))?$", re.I)
NO = re.compile(r"^(nein(\s+(danke|abbrechen|lass es|lieber nicht))?|abbrechen|stopp|lass es|lieber nicht"
                r"|auf keinen fall|ablehnen)(\s+(bitte|jarvis))?$", re.I)

_AREA = r"(?P<location>(?:in der|im|in dem|in)\s+(?P<area>[a-zäöüß]+))"
LIGHT_ON_OFF = re.compile(rf"^(?:mach|schalte?)\s+(?:das\s+)?licht\s+{_AREA}\s+(?P<state>an|aus|ein)$", re.I)
LIGHT_HERE = re.compile(r"^(?:mach|schalte?)\s+(?:das\s+)?licht\s+(?P<state>an|aus|ein)$", re.I)  # Raum des Sprechers
LIGHT_PCT = re.compile(
    rf"^(?:mach|stell|dimm|setz)e?\s+(?:das\s+)?licht\s+{_AREA}\s+auf\s+(?P<num>\d{{1,3}}|[a-zäöüß]+)\s*(?:prozent|%)$",
    re.I,
)
TIMER = re.compile(
    r"^(?:stell|starte?)e?\s+(?:einen\s+)?timer\s+(?:auf|für)\s+(?P<num>\d{1,3}|[a-zäöüß]+)\s+(?P<unit>minuten?|sekunden?|stunden?)$",
    re.I,
)


# PC-Befehle. Natürliche Formen werden erst auf die Befehlsform zurückgeführt („Kannst du bitte Steam starten?“
# -> „öffne Steam“), dann gegen Programme, Ordner, Webseiten und Suchen geprüft. Groß-/Kleinschreibung bleibt
# erhalten (Suchbegriffe), verglichen wird ohne Rücksicht darauf.
_OPEN_VERBS = r"öffnen|starten|aufmachen|aufrufen|anmachen|hochfahren|laden"
_SHOW_VERBS = r"zeigen|anzeigen"
_PLAY_VERBS = r"spielen|abspielen|zocken"
_SEARCH_VERBS = r"suchen|googeln|finden|nachschauen|nachsehen|raussuchen|heraussuchen"
_ANY_VERB = rf"{_OPEN_VERBS}|{_SHOW_VERBS}|{_PLAY_VERBS}|{_SEARCH_VERBS}"
_FILLER = re.compile(r"\b(?:bitte|mal|doch|kurz|schnell|einmal|jetzt|gerade|eben|für mich|gleich|eventuell|"
                     r"vielleicht)\b", re.I)
_ASK = re.compile(rf"^(?:(?:kannst|könntest|würdest) du|(?:können|könnten|würden) sie|ich (?:möchte|will|würde gerne?|"
                  rf"hätte gerne?|muss)|lass uns|wir (?:müssen|sollten))\s+(?P<rest>.+?)\s+(?P<verb>{_ANY_VERB})$", re.I)
_VERB_LAST = re.compile(rf"^(?P<rest>.+?)\s+(?P<verb>{_ANY_VERB})$", re.I)
PC_OPEN = re.compile(r"^(?P<verb>öffne|starte|start|ruf|rufe|mach|zeig|zeige|spiel|spiele)\s+(?:mir\s+)?"
                     r"(?:(?P<article>den|die|das|einen|eine|ein|meine|meinen|mein)\s+)?(?P<target>[\wäöüß .+&'-]+?)"
                     r"(?P<auf>\s+auf)?$", re.I)
_SITES = r"youtube|google|amazon|wikipedia|ebay"
_ON_PC = r"(?:auf (?:dem|meinem) (?:pc|computer|rechner|laptop)|am (?:pc|computer|rechner)|in meinen dateien|" \
         r"auf der festplatte)"
PC_SEARCH_FILES = [
    re.compile(rf"^(?:such|suche|finde|find)\s+{_ON_PC}\s+(?:nach\s+)?(?P<query>.+)$", re.I),
    re.compile(rf"^(?:such|suche|finde|find)\s+(?:nach\s+)?(?:der|die|meine|meiner|meinen|eine|einer|den|dem|das)?\s*"
               rf"(?:datei|dateien|ordner|dokument|dokumente)\s+(?:namens\s+|mit dem namen\s+)?(?P<query>.+?)"
               rf"(?:\s+{_ON_PC})?$", re.I),
    re.compile(rf"^(?:such|suche|finde|find)\s+(?:nach\s+)?(?P<query>.+?)\s+{_ON_PC}$", re.I),
]
PC_SEARCH_SITE = [
    re.compile(rf"^(?:such|suche|google|googel)\s+(?:auf|bei|in)\s+(?P<site>{_SITES})\s+(?:nach\s+)?(?P<query>.+)$",
               re.I),
    re.compile(rf"^(?:such|suche)\s+(?:nach\s+)?(?P<query>.+?)\s+(?:auf|bei|in)\s+(?P<site>{_SITES})$", re.I),
    re.compile(r"^(?:zeig|zeige|öffne|starte)\s+(?:mir\s+)?(?P<query>.+?)\s+(?:auf|bei|in)\s+(?P<site>youtube)$", re.I),
]
# Link heraussuchen und direkt öffnen (erster Treffer) bzw. Trefferliste zum Auswählen
_LINK = r"(?:link|webseite|website|homepage|internetseite|seite|url)"
_ABOUT = r"(?:zu|zum|zur|für|von|vom|über|mit)"
PC_OPEN_LINK = [
    re.compile(rf"^(?:such|suche|finde|find|hol|hole|gib)\s+(?:mir\s+)?(?:nach\s+)?(?:(?:einen|den|ein|die|eine)\s+)?"
               rf"(?:passenden\s+|guten\s+)?{_LINK}\s+{_ABOUT}\s+(?P<query>.+?)(?:\s+(?:raus|heraus))?"
               rf"(?:\s+und\s+(?:öffne|mach|zeig)(?:\s+(?:ihn|sie|es|den|die|das))?(?:\s+auf)?)?$", re.I),
    re.compile(r"^(?:such|suche|google|googel)\s+(?:nach\s+)?(?P<query>.+?)\s+und\s+(?:öffne|mach|zeig)\s+(?:mir\s+)?"
               r"(?:ihn|sie|es|das|den|die)(?:\s+(?:erste|ersten|beste|besten))?"
               r"(?:\s+(?:ergebnis|treffer|link|seite|video))?(?:\s+auf)?$", re.I),
    re.compile(r"^(?:öffne|zeig|zeige)\s+(?:mir\s+)?(?:den|das|die)\s+(?:ersten?|besten?)\s+(?:treffer|link|ergebnis|"
               r"suchergebnis|seite)\s+(?:für|zu|von|zum|zur)\s+(?P<query>.+)$", re.I),
    re.compile(r"^(?:öffne|zeig|zeige|spiel|spiele)\s+(?:mir\s+)?(?:(?:das|ein)\s+)?(?:erste\s+|beste\s+)?video\s+"
               r"(?:von|zu|über|mit|vom|zum)\s+(?P<query>.+?)(?:\s+(?:auf|bei)\s+youtube)?(?P<yt>)$", re.I),
    re.compile(r"^(?:spiel|spiele)\s+(?:mir\s+)?(?P<query>.+?)\s+(?:auf|bei|in)\s+youtube(?:\s+ab)?(?P<yt>)$", re.I),
]
PC_OPEN_WEBSITE = re.compile(r"^(?:öffne|zeig|zeige)\s+(?:mir\s+)?(?:die\s+)?(?:offizielle\s+)?"
                             r"(?:webseite|website|homepage|internetseite|seite)\s+(?:von\s+(?:der\s+|dem\s+)?|"
                             r"vom\s+|der\s+|des\s+|zu\s+|zum\s+|zur\s+|für\s+)?(?P<query>.+)$", re.I)
WEB_LIST = re.compile(rf"^(?:such|suche|zeig|zeige|finde|gib|nenn|nenne)\s+(?:mir\s+)?(?:nach\s+)?"
                      rf"(?:ein paar|einige|mehrere|die besten)?\s*(?:links|webseiten|seiten|ergebnisse|treffer|"
                      rf"quellen)\s+{_ABOUT}\s+(?P<query>.+?)(?:\s+(?:raus|heraus))?$", re.I)
# Dateien im Benutzerordner: öffnen („Öffne die Datei Bewerbung“) und finden („Wo ist meine Steuererklärung?“)
_FILE_KINDS = r"datei|dokument|pdf|präsentation|tabelle|foto|bild|video|ordner"
_FILE_HINTS = {"pdf": ".pdf", "präsentation": ".ppt", "tabelle": ".xls"}  # „die PDF Bewerbung“ -> Bewerbung .pdf
PC_OPEN_FILE = re.compile(rf"^(?:öffne|zeig|zeige)\s+(?:mir\s+)?(?:(?:die|meine|das|den|mein|meinen|meiner)\s+)?"
                          rf"(?P<kind>{_FILE_KINDS})\s+(?:namens\s+|mit dem namen\s+|von\s+|zu\s+|über\s+)?"
                          rf"(?P<query>.+)$", re.I)
_FILE_EXT = re.compile(r"\.(?:pdf|docx?|xlsx?|pptx?|txt|odt|ods|csv|rtf|jpe?g|png|gif|heic|mp3|wav|mp4|mov|mkv|zip|rar|"
                       r"7z)$", re.I)
_WHERE = r"^wo\s+(?:ist|liegt|sind|liegen|finde ich|habe ich|hab ich)\s+"
_STORED = r"(?:\s+(?:gespeichert|abgelegt|hin|gespeichert hin|abgespeichert))?$"
PC_FIND_FILES = [
    re.compile(rf"{_WHERE}(?:meine|mein|meinen)\s+(?:(?P<kind>datei|dateien|ordner|dokument|dokumente)\s+)?"
               rf"(?P<query>.+?){_STORED}", re.I),
    re.compile(rf"{_WHERE}(?:die|der|das|den)\s+(?P<kind>datei|ordner|dokument|pdf)\s+(?P<query>.+?){_STORED}", re.I),
    re.compile(r"^(?:zeig|zeige|nenn|nenne|liste)\s+(?:mir\s+)?(?:alle\s+|meine\s+)?(?:dateien|dokumente)\s+"
               r"(?:mit|zu|namens|über|von)\s+(?P<query>.+)$", re.I),
]
# Auswahl aus der zuletzt genannten Liste: „die zweite“, „öffne den dritten Link“, „Nummer 2“, „ja“ (= den ersten)
_ORDINALS = {"erste": 1, "zweite": 2, "dritte": 3, "vierte": 4, "fünfte": 5, "eins": 1, "zwei": 2, "drei": 3,
             "vier": 4, "fünf": 5}
SELECTION = re.compile(r"^(?:(?:öffne|nimm|zeig|zeige|mach|spiel)\s+(?:mir\s+)?)?(?:(?:die|den|das|der)\s+)?"
                       r"(?:nummer\s+|nr\s+)?(?P<n>\d{1,2}|eins|zwei|drei|vier|fünf|(?:erst|zweit|dritt|viert|fünft|"
                       r"letzt)e[nrs]?)(?:\s+(?:datei|link|treffer|ergebnis|eintrag|seite|video|davon))?(?:\s+auf)?$",
                       re.I)
AFFIRM = re.compile(r"^(?:ja(?:\s+(?:bitte|gerne|gern|mach das|öffne sie|öffne ihn|öffne es))?|gerne|gern|"
                    r"(?:öffne|zeig|zeige)\s+(?:sie|ihn|es)|mach\s+(?:sie|ihn|es)\s+auf|mach das)$", re.I)
PC_SEARCH = [
    re.compile(r"^(?:such|suche|google|googel|googeln)\s+(?:im internet\s+|online\s+|im web\s+)?nach\s+(?P<query>.+?)"
               r"(?:\s+im internet|\s+online|\s+im web)?$", re.I),
    re.compile(r"^(?:such|suche)\s+(?:im internet|online|im web)\s+(?P<query>.+)$", re.I),
    re.compile(r"^google\s+(?P<query>.+)$", re.I),
]
_PREFIX = re.compile(r"^(?:(?:hey|hallo|ok|okay)\s+)?jarvis\s*[,:]?\s*|^bitte\s+", re.I)
_GO_TO = re.compile(r"^(?:geh|gehe|navigier|navigiere|bring mich|führ mich|leite mich)\s+(?:auf|zu|zur|zum|nach)\s+"
                    r"(?:(?:die\s+)?(?:seite|webseite|website)\s+)?", re.I)
_DOMAIN = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:de|com|org|net|io|eu|at|ch|tv|info)$")
# Diese Ziele gehören zum Haus, nicht zum PC – sie gehen an Home Assistant bzw. das LLM
_HOME_WORDS = re.compile(r"\b(?:licht|lampe|tür|haustür|fenster|rollladen|rollo|jalousie|garage|garagentor|tor|heizung|"
                         r"klima|steckdose|schloss|kühlschrank|waschmaschine|musik|radio|fernseher|tv)\b")

PC_APPS = {
    "explorer": "explorer", "datei explorer": "explorer", "dateiexplorer": "explorer", "datei-explorer": "explorer",
    "windows explorer": "explorer", "file explorer": "explorer", "dateimanager": "explorer",
    "browser": "browser", "webbrowser": "browser", "internetbrowser": "browser", "internet": "browser",
    "editor": "editor", "texteditor": "editor", "notepad": "editor", "notizblock": "editor",
    "rechner": "rechner", "taschenrechner": "rechner", "paint": "paint",
    "einstellungen": "einstellungen", "windows einstellungen": "einstellungen", "systemeinstellungen": "einstellungen",
    "taskmanager": "taskmanager", "task manager": "taskmanager", "task-manager": "taskmanager",
    "systemsteuerung": "systemsteuerung", "kamera": "kamera", "uhr": "uhr", "wecker": "uhr", "store": "store",
    "microsoft store": "store", "snipping tool": "snipping", "bildschirmfoto": "snipping", "screenshot": "snipping",
    "spotify": "spotify", "word": "word", "excel": "excel", "powerpoint": "powerpoint", "outlook": "outlook",
}
PC_FOLDERS = {
    "downloads": "downloads", "download": "downloads", "dokumente": "documents", "dokumentenordner": "documents",
    "desktop": "desktop", "schreibtisch": "desktop", "bilder": "pictures", "fotos": "pictures", "musik": "music",
    "videos": "videos", "benutzerordner": "home", "dieser pc": "pc", "arbeitsplatz": "pc", "computer": "pc",
}
PC_SITES = {
    "youtube": "https://www.youtube.com", "google": "https://www.google.de", "netflix": "https://www.netflix.com",
    "amazon": "https://www.amazon.de", "wikipedia": "https://de.wikipedia.org", "gmail": "https://mail.google.com",
    "twitch": "https://www.twitch.tv", "ebay": "https://www.ebay.de", "whatsapp web": "https://web.whatsapp.com",
    "instagram": "https://www.instagram.com", "facebook": "https://www.facebook.com",
}


def _pc_target(raw: str) -> str:
    target = re.sub(r"\s+", " ", raw.lower()).strip(" .-")
    target = re.sub(rf"\s+{_ON_PC}$", "", target)
    target = re.sub(r"^(?:ordner|programm|app|webseite|seite|anwendung|spiel)\s+", "", target)
    return re.sub(r"[\s-]*(?:ordner|programm|app|anwendung)$", "", target)


def canonical_command(text: str) -> str:
    """„Kannst du mir bitte mal den Explorer öffnen?“ -> „öffne den Explorer“; „Steam starten“ -> „öffne Steam“."""
    text = re.sub(r"[,!?;:„“\"]+", " ", text)
    text = re.sub(r"\.(?=\s|$)", " ", text)  # Satzpunkte weg, Punkte in Adressen („heise.de“) bleiben
    text = _PREFIX.sub("", re.sub(r"\s+", " ", text).strip())
    text = re.sub(r"\s+", " ", _FILLER.sub(" ", text)).strip()
    text = re.sub(r"\s+(?:jarvis|sir)$", "", text, flags=re.I)
    text = _GO_TO.sub("öffne die webseite ", text)
    text = re.sub(r"^(?:wechsel|wechsle|wechsele)\s+(?:zu|zum|zur|in)\s+(?:(?:den|die|das|dem|der)\s+)?", "öffne ", text,
                  flags=re.I)
    text = re.sub(r"\s+(?:suchen|raussuchen|heraussuchen|finden)\s+und\s+(?:öffnen|aufmachen|anzeigen|zeigen)$",
                  " raussuchen", text, flags=re.I)
    for pattern in (_ASK, _VERB_LAST):
        if m := pattern.match(text):
            rest, verb = re.sub(r"^mir\s+", "", m["rest"], flags=re.I), m["verb"].lower()
            if re.fullmatch(_SEARCH_VERBS, verb):
                keep = rest.lower().startswith(("nach ", "auf ", "bei ", "im ", "in ", "die ", "meine ", "der ",
                                                "den ", "das "))
                return f"such {rest}" if keep else f"such nach {rest}"
            if re.fullmatch(_SHOW_VERBS, verb):
                return f"zeig {rest}"
            if re.fullmatch(_PLAY_VERBS, verb):
                return f"spiel {rest}"
            return f"öffne {rest}"
    return text


# Status und Tagesplan (Skills mit Capability) sowie Gesprächs-Intents ohne Aktion. Verglichen wird die ganze,
# normalisierte Äußerung – „Status?“ ja, „Wie ist der Status der Waschmaschine?“ nein (geht an das LLM).
STATUS = re.compile(r"^(?:status|systemstatus|status ?bericht|wie ist der status|diagnose|systemdiagnose|"
                    r"systemcheck|system check|alle systeme)$")
DAY_PLAN = re.compile(r"^(?:(?:was ist (?:der|mein) )?(?:tages)?plan für (?P<a>morgen|heute)|tagesplan|"
                      r"was steht (?P<b>morgen|heute) an|wie sieht mein tag (?P<c>morgen|heute) aus|"
                      r"was habe ich (?P<d>morgen|heute) vor)$")
CONVERSATION = [
    ("help", re.compile(r"^(?:kannst du mir helfen|können sie mir helfen|hilf mir|hilfe|ich brauche (?:deine |ihre )?hilfe)$")),
    ("thanks", re.compile(r"^(?:danke(?: schön| sehr| dir| ihnen)?|dankeschön|vielen dank|herzlichen dank|merci)$")),
    ("greeting", re.compile(r"^(?:hallo|hi|hey|servus|moin|guten (?:morgen|tag|abend)|grüß dich|grüß gott)$")),
    ("how_are_you", re.compile(r"^(?:wie geht(?:s| es)(?: dir| ihnen)?|wie läuft(?:s| es)|alles (?:gut|klar) bei dir)$")),
    ("identity", re.compile(r"^(?:wer bist du|was bist du|stell dich vor)$")),
    ("capabilities", re.compile(r"^(?:was kannst du(?: alles)?(?: tun)?|was sind deine fähigkeiten|wobei kannst du helfen)$")),
    ("name", re.compile(r"^(?:wie heiße ich|wer bin ich|weißt du wie ich heiße|kennst du meinen namen)$")),
    ("time", re.compile(r"^(?:wie spät ist es|wie viel uhr ist es|wieviel uhr ist es|uhrzeit)$")),
    ("date", re.compile(r"^(?:welcher tag ist heute|welches datum (?:ist|haben wir)(?: heute)?|was ist heute für ein tag|datum)$")),
    ("goodnight", re.compile(r"^gute nacht$")),
    ("goodbye", re.compile(r"^(?:tschüss|tschau|ciao|bis später|bis dann|auf wiedersehen|das wars|danke das wars)$")),
]


def normalize_utterance(text: str) -> str:
    """Kleinbuchstaben, ohne Satzzeichen, ohne Anrede/Füllwörter am Rand („Jarvis, …“, „… bitte“)."""
    text = re.sub(r"[.,!?;:„“\"]+", " ", text.lower())
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(?:(?:hey|hallo|ok|okay)\s+)?jarvis\s+|^(?:sag mal|bitte)\s+", "", text)
    return re.sub(r"\s+(?:bitte|jarvis|sir)$", "", text).strip()


def conversation_intent(text: str) -> str | None:
    """Gesprächs-Intents ohne Aktion (Hilfe, Dank, Gruß, Uhrzeit …) – beantwortet die Stil-Engine direkt."""
    normalized = normalize_utterance(text)
    for name, pattern in CONVERSATION:
        if pattern.match(normalized):
            return name
    return None


@dataclass
class FastPathMatch:
    capability: str
    arguments: dict[str, Any]
    confidence: float
    grammar: str
    slots: dict[str, Any] = field(default_factory=dict)


def parse_number(token: str) -> int | None:
    token = token.lower()
    if token.isdigit():
        return int(token)
    return NUMBER_WORDS.get(token)


def confirmation_reply(text: str) -> bool | None:
    """True = Zustimmung, False = Ablehnung, None = keine Bestätigungsantwort."""
    normalized = re.sub(r"\s+", " ", re.sub(r"[,.!?]", " ", text)).strip()
    if YES.match(normalized):
        return True
    if NO.match(normalized):
        return False
    return None


class FastPath:
    def __init__(self, area_lights: dict[str, list[str]], area_aliases: dict[str, str] | None = None) -> None:
        self.area_lights = area_lights  # area_id -> [light.*]
        self.area_aliases = area_aliases or {}  # "küche" -> "kueche"

    def _area(self, spoken: str) -> str | None:
        spoken = spoken.lower()
        area = self.area_aliases.get(spoken, spoken)
        return area if area in self.area_lights else None

    def match(self, text: str, default_area: str | None = None) -> FastPathMatch | None:
        normalized = _PREFIX.sub("", re.sub(r"[.!?]+$", "", text.strip())).strip()
        if m := LIGHT_ON_OFF.match(normalized):
            area = self._area(m["area"])
            if area:
                location = re.sub(r"\S+$", lambda w: w.group(0).capitalize(), m["location"].lower())
                return FastPathMatch(
                    "home.set_light",
                    {"entity_ids": self.area_lights[area], "on": m["state"].lower() in ("an", "ein")},
                    0.97, "light_on_off", {"area": area, "location": location},
                )
        if m := LIGHT_HERE.match(normalized):
            on = m["state"].lower() in ("an", "ein")
            if default_area in self.area_lights:
                return FastPathMatch("home.set_light", {"entity_ids": self.area_lights[default_area], "on": on},
                                     0.95, "light_here", {"area": default_area})
            if not self.area_lights:  # kein Haus verbunden: ehrlich antworten statt raten
                return FastPathMatch("home.set_light", {"on": on}, 0.9, "light_unavailable")
        simple = normalize_utterance(text)
        if STATUS.match(simple):
            return FastPathMatch("system.status", {}, 0.97, "status")
        if m := DAY_PLAN.match(simple):
            day = next((v for v in m.groups() if v), "morgen")
            return FastPathMatch("assistant.day_plan", {"day": "tomorrow" if day == "morgen" else "today"}, 0.95,
                                 "day_plan")
        if m := LIGHT_PCT.match(normalized):
            area, pct = self._area(m["area"]), parse_number(m["num"])
            if area and pct is not None and 0 <= pct <= 100:
                return FastPathMatch(
                    "home.set_light",
                    {"entity_ids": self.area_lights[area], "on": pct > 0, "brightness_pct": pct},
                    0.96, "light_brightness", {"area": area, "brightness_pct": pct},
                )
        if m := TIMER.match(normalized):
            n = parse_number(m["num"])
            if n:
                factor = {"s": 1, "m": 60, "h": 3600}[
                    "s" if m["unit"].lower().startswith("sek") else "h" if m["unit"].lower().startswith("st") else "m"
                ]
                return FastPathMatch("timer.start", {"duration_s": n * factor}, 0.95, "timer", {"seconds": n * factor})
        return self._match_pc(canonical_command(text))

    def _match_pc(self, text: str) -> FastPathMatch | None:
        for pattern in PC_OPEN_LINK:
            if m := pattern.match(text):
                return _open_link(m["query"], "youtube" if "yt" in m.groupdict() else None)
        if m := WEB_LIST.match(text):
            query = _web_query(m["query"])
            return FastPathMatch("web.search", {"query": query}, 0.92, "web_list", {"query": query})
        if m := PC_OPEN_WEBSITE.match(text):
            return _open_site(m["query"])
        for pattern in PC_SEARCH_FILES:
            if m := pattern.match(text):
                query = m["query"].strip()
                return FastPathMatch("pc.search_files", {"query": query}, 0.92, "pc_search_files", {"query": query})
        for pattern in PC_FIND_FILES:
            if m := pattern.match(text):
                query, kind = m["query"].strip(), (m.groupdict().get("kind") or "").lower()
                arguments = {"query": query, "kind": "folder" if kind == "ordner" else "any"}
                return FastPathMatch("pc.find_files", arguments, 0.9, "pc_find_files", {"query": query})
        if m := PC_OPEN_FILE.match(text):
            kind, query = m["kind"].lower(), m["query"].strip()
            if kind == "ordner" and _pc_target(query) in PC_FOLDERS:
                folder = PC_FOLDERS[_pc_target(query)]
                return FastPathMatch("pc.open_folder", {"folder": folder}, 0.95, "pc_open_folder", {"target": query})
            if kind in _FILE_HINTS:
                query = f"{query} {_FILE_HINTS[kind]}"
            arguments = {"query": query, "kind": "folder" if kind == "ordner" else "file"}
            return FastPathMatch("pc.open_file", arguments, 0.92, "pc_open_file", {"query": query})
        for pattern in PC_SEARCH_SITE:
            if m := pattern.match(text):
                query, site = m["query"].strip(), m["site"].lower()
                return FastPathMatch("pc.search_web", {"query": query, "site": site}, 0.93, "pc_search_site",
                                     {"query": query, "site": site})
        for pattern in PC_SEARCH:
            if m := pattern.match(text):
                query = m["query"].strip()
                return FastPathMatch("pc.search_web", {"query": query}, 0.93, "pc_search", {"query": query})
        if m := PC_OPEN.match(text):
            target, verb = _pc_target(m["target"]), m["verb"].lower()
            playing = verb.startswith("spiel")  # „Spiel Minecraft“: nur Programme, nie Ordner („Spiel Musik“)
            if _FILE_EXT.search(target) and not playing:  # „Öffne Bewerbung.pdf“
                return FastPathMatch("pc.open_file", {"query": target, "kind": "file"}, 0.92, "pc_open_file",
                                     {"query": target})
            if target in PC_APPS and not playing:
                return FastPathMatch("pc.open_app", {"app": PC_APPS[target]}, 0.95, "pc_open_app", {"target": target})
            if target in PC_FOLDERS and not playing:
                return FastPathMatch("pc.open_folder", {"folder": PC_FOLDERS[target]}, 0.95, "pc_open_folder",
                                     {"target": target})
            url = PC_SITES.get(target) or (f"https://{target}" if _DOMAIN.match(target) else None)
            if url and not playing:
                return FastPathMatch("pc.open_url", {"url": url}, 0.94, "pc_open_url", {"target": target})
            # Unbekannter Name („Steam“, „Chefkoch“): installiertes Programm? Prüft der Orchestrator beim PC-Agenten.
            # Ohne passendes Programm öffnet er – bei Namen ohne Artikel – die passende Webseite.
            guessable = playing or verb in ("öffne", "starte", "start", "ruf", "rufe") or (verb == "mach" and m["auf"])
            if guessable and 2 <= len(target) <= 60 and not _HOME_WORDS.search(target):
                web_fallback = not playing and not m["article"] and len(target.split()) <= 4
                return FastPathMatch("pc.open_app", {"app": target}, 0.8, "pc_open_guess",
                                     {"target": target, "web_fallback": web_fallback})
        return None


def _web_query(query: str) -> str:
    """„einem Lasagne-Rezept“ -> „Lasagne-Rezept“: Artikel am Anfang stören die Suche nur."""
    return re.sub(r"^(?:einen|einem|einer|eines|eine|ein|den|dem|der|die|das|des)\s+", "", query.strip(), flags=re.I)


def _open_link(query: str, site: str | None) -> FastPathMatch:
    query = _web_query(query)
    arguments = {"query": query, **({"site": site} if site else {})}
    return FastPathMatch("pc.open_link", arguments, 0.92, "pc_open_link", {"query": query})


def _open_site(query: str) -> FastPathMatch:
    """„Öffne die Webseite von Chefkoch“: bekannte Seite oder Adresse direkt, sonst der beste Treffer."""
    target = _pc_target(query)
    url = PC_SITES.get(target) or (f"https://{target}" if _DOMAIN.match(target) else None)
    if url:
        return FastPathMatch("pc.open_url", {"url": url}, 0.94, "pc_open_url", {"target": target})
    return _open_link(query, None)


def selection_reply(text: str, count: int, *, affirm: bool = True) -> int | None:
    """Welcher Eintrag der zuletzt genannten Liste ist gemeint? 1-basiert; None = keine Auswahl."""
    normalized = normalize_utterance(text)
    if AFFIRM.match(normalized):
        return 1 if affirm else None
    m = SELECTION.match(normalized)
    if m is None:
        return None
    token = m["n"].lower()
    if token.startswith("letzt"):
        return count
    number = int(token) if token.isdigit() else _ORDINALS.get(re.sub(r"(?<=e)[nrs]$", "", token))
    return number if number and 1 <= number <= count else None
