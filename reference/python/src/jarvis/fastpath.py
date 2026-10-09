"""Fast-Path-NLU: deterministische Grammatiken für häufige Befehle – ohne LLM, < 300 ms.

Im Betrieb werden die Muster aus Home-Assistant-Intents (hassil-kompatibel) und dem Entitätsregister
generiert; dieses Modul zeigt das Prinzip an Licht-, Timer-, PC- und Bestätigungsbefehlen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .homeindex import HomeIndex, match_home
from .when import parse_duration, parse_when

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


# PC-Befehle. Natürliche Formen werden erst auf die Befehlsform zurückgeführt („Kannst du bitte Steam starten?“
# -> „öffne Steam“), dann gegen Programme, Ordner, Webseiten und Suchen geprüft. Groß-/Kleinschreibung bleibt
# erhalten (Suchbegriffe), verglichen wird ohne Rücksicht darauf.
_OPEN_VERBS = r"öffnen|starten|aufmachen|aufrufen|anmachen|hochfahren|laden"
_SHOW_VERBS = r"zeigen|anzeigen"
_PLAY_VERBS = r"spielen|abspielen|zocken|hören|anhören"
_SEARCH_VERBS = r"suchen|googeln|finden|nachschauen|nachsehen|raussuchen|heraussuchen"
_CLOSE_VERBS = r"schließen|beenden|zumachen"
_ANY_VERB = rf"{_OPEN_VERBS}|{_SHOW_VERBS}|{_PLAY_VERBS}|{_SEARCH_VERBS}|{_CLOSE_VERBS}"
_FILLER = re.compile(r"\b(?:bitte|mal|doch|kurz|schnell|einmal|jetzt|gerade|eben|für mich|gleich|eventuell|"
                     r"vielleicht)\b", re.I)
_ASK = re.compile(rf"^(?:(?:kannst|könntest|würdest) du|(?:können|könnten|würden) sie|ich (?:möchte|will|würde gerne?|"
                  rf"hätte gerne?|muss)|lass uns|wir (?:müssen|sollten))\s+(?P<rest>.+?)\s+(?P<verb>{_ANY_VERB})$", re.I)
_VERB_LAST = re.compile(rf"^(?P<rest>.+?)\s+(?P<verb>{_ANY_VERB})$", re.I)
PC_OPEN = re.compile(r"^(?P<verb>öffne|starte|start|ruf|rufe|mach|zeig|zeige|spiel|spiele)\s+(?:mir\s+)?"
                     r"(?:(?P<article>den|die|das|einen|eine|ein|meine|meinen|mein)\s+)?(?P<target>[\wäöüß .+&'-]+?)"
                     r"(?P<auf>\s+auf)?$", re.I)
_ON_PC = r"(?:auf (?:dem|meinem) (?:pc|computer|rechner|laptop)|am (?:pc|computer|rechner)|in (?:meinen|den) " \
         r"(?:dateien|ordnern)|auf der festplatte|im (?:datei[- ]?|detail[- ]?|windows[- ]?)?explorer|im dateimanager)"
# „ein Bild namens Heizung“, „die Datei mit dem Namen Steuer“, „ein Foto, das Urlaub heißt“ -> nur der Name
_FILE_NOUNS = r"(?:datei|dateien|ordner|dokument|dokumente|bild|bilder|foto|fotos|video|videos|pdf|präsentation|tabelle|" \
              r"lied|song|musik)"
_FILE_DESCRIBED = re.compile(r"^(?:nach\s+)?(?:(?:einem|einer|einen|eine|ein|der|die|das|den|dem|meine|meinen|meiner|"
                             rf"mein|meinem)\s+)?{_FILE_NOUNS}\s+(?:namens\s+|mit (?:dem )?namen\s+|genannt\s+|"
                             r"(?:das|die|der)\s+(?=.+\s(?:heißt|heisst)$))?(?P<name>.+?)(?:\s+(?:heißt|heisst))?$", re.I)


def file_query(query: str) -> str:
    """Suchbegriff für Dateien ohne Beschreibung drumherum: „ein Bild namens Heizung“ -> „Heizung“."""
    query = query.strip(" ,.")
    if (m := _FILE_DESCRIBED.match(query)) and m["name"].strip():
        return m["name"].strip(" ,.")
    return query


PC_SEARCH_FILES = [
    re.compile(rf"^(?:such|suche|finde|find)\s+{_ON_PC}\s+(?:nach\s+)?(?P<query>.+)$", re.I),
    re.compile(rf"^(?:such|suche|finde|find)\s+(?:nach\s+)?(?:der|die|meine|meiner|meinen|eine|einer|den|dem|das)?\s*"
               rf"(?:datei|dateien|ordner|dokument|dokumente)\s+(?:namens\s+|mit dem namen\s+)?(?P<query>.+?)"
               rf"(?:\s+{_ON_PC})?$", re.I),
    re.compile(rf"^(?:such|suche|finde|find)\s+(?:nach\s+)?(?P<query>.+?)\s+{_ON_PC}$", re.I),
    # „Such ein Bild namens Heizung“, „Finde mein Foto mit dem Namen Urlaub“ – ausdrücklich ein Name: eigene Dateien
    re.compile(rf"^(?:such|suche|finde|find)\s+(?P<query>(?:nach\s+)?(?:\S+\s+)?{_FILE_NOUNS}\s+"
               r"(?:namens|mit (?:dem )?namen|genannt)\s+.+)$", re.I),
]
# Dienste für „… auf Spotify“, „bei Amazon“: gesprochene Form -> Kennung (Such-Adressen: pc.SEARCH_SITES).
# Nur bekannte Dienste – „Urlaub auf Mallorca“ bleibt ein Suchbegriff.
SEARCH_SERVICES = {
    "google": "google", "youtube": "youtube", "you tube": "youtube", "amazon": "amazon", "wikipedia": "wikipedia",
    "ebay": "ebay", "spotify": "spotify", "netflix": "netflix", "twitch": "twitch", "github": "github",
    "reddit": "reddit", "google maps": "maps", "maps": "maps", "google karten": "maps", "der karte": "maps",
    "karte": "maps", "idealo": "idealo", "chefkoch": "chefkoch", "tiktok": "tiktok", "tik tok": "tiktok",
    "bing": "bing", "soundcloud": "soundcloud", "steam": "steam", "zalando": "zalando", "otto": "otto",
    "mediamarkt": "mediamarkt", "media markt": "mediamarkt", "kleinanzeigen": "kleinanzeigen",
    "ebay kleinanzeigen": "kleinanzeigen", "pinterest": "pinterest", "imdb": "imdb", "duckduckgo": "duckduckgo",
}
_SERVICE = "|".join(sorted((re.escape(k) for k in SEARCH_SERVICES), key=len, reverse=True))
_FIND = r"(?:such|suche|google|googel|googeln|find|finde)"
_ONLINE = r"(?:im internet|online|im web|im netz)"
_AT = r"(?:auf|bei|in|über)"
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
    # ohne Verb, z. B. als Antwort auf eine Rückfrage: „Nach dem Ordner Minecraft“, „Ordner mit dem Namen Minecraft“
    re.compile(r"^(?:(?:such|suche|finde)\s+)?(?:nach\s+)?(?:(?:dem|den|der|die|das|einem|einen|eine|meinem|meinen|"
               r"meiner|meine|mein)\s+)?"
               r"(?P<kind>ordner|datei)\s+(?:mit dem namen\s+|namens\s+|mit namen\s+)?"
               r"(?!(?:.*\s)?(?:erstellen|anlegen|löschen|entfernen|umbenennen|verschieben|kopieren|speichern|"
               r"schließen|leeren|teilen|senden|schicken|drucken|packen|entpacken|zippen|hochladen|herunterladen)\.?$)"
               r"(?P<query>[\wäöüß .+&'-]+?)"
               r"(?:\s+(?:ähnlich|oder so|oder ähnlich))?$", re.I),
    re.compile(rf"{_WHERE}(?:meine|mein|meinen)\s+(?:(?P<kind>datei|dateien|ordner|dokument|dokumente)\s+)?"
               rf"(?P<query>.+?){_STORED}", re.I),
    re.compile(rf"{_WHERE}(?:die|der|das|den)\s+(?P<kind>datei|ordner|dokument|pdf)\s+(?P<query>.+?){_STORED}", re.I),
    re.compile(r"^(?:zeig|zeige|nenn|nenne|liste)\s+(?:mir\s+)?(?:alle\s+|meine\s+)?(?:dateien|dokumente)\s+"
               r"(?:mit|zu|namens|über|von)\s+(?P<query>.+)$", re.I),
]
# Auswahl aus der zuletzt genannten Liste: „die zweite“, „öffne den dritten Link“, „Nummer 2“, „ja“ (= den ersten)
_ORDINALS = {"erste": 1, "zweite": 2, "dritte": 3, "vierte": 4, "fünfte": 5, "eins": 1, "zwei": 2, "drei": 3,
             "vier": 4, "fünf": 5}
SELECTION = re.compile(r"^(?:(?:öffne|nimm|zeig|zeige|mach|spiel|lies|lese)\s+(?:mir\s+)?)?(?:(?:die|den|das|der)\s+)?"
                       r"(?:nummer\s+|nr\s+)?(?P<n>\d{1,2}|eins|zwei|drei|vier|fünf|(?:erst|zweit|dritt|viert|fünft|"
                       r"letzt)e[nrs]?)(?:\s+(?:datei|link|treffer|ergebnis|eintrag|seite|video|mail|e-mail|nachricht|"
                       r"davon))?(?:\s+(?:auf|vor))?$",
                       re.I)
AFFIRM = re.compile(r"^(?:ja(?:\s+(?:bitte|gerne|gern|mach das|öffne sie|öffne ihn|öffne es|lies sie vor))?|gerne|gern|"
                    r"(?:öffne|zeig|zeige)\s+(?:sie|ihn|es)|mach\s+(?:sie|ihn|es)\s+auf|mach das|lies sie vor)$", re.I)
PC_SEARCH = [
    # „Such auf Spotify nach Arteriion“, „Schau bei Amazon nach Kopfhörern“
    re.compile(rf"^(?:{_FIND}|schau|guck)\s+(?:{_ONLINE}\s+)?{_AT}\s+(?P<site>{_SERVICE})\s+(?:nach\s+)?(?P<query>.+)$",
               re.I),
    # „Such nach Arteriion auf Spotify“, „Such nach Pizza“
    re.compile(rf"^{_FIND}\s+(?:{_ONLINE}\s+)?nach\s+(?P<query>.+?)(?:\s+{_AT}\s+(?P<site>{_SERVICE}))?"
               rf"(?:\s+{_ONLINE})?$", re.I),
    # „Such Arteriion auf Spotify“ (ohne „nach“, aber mit Dienst)
    re.compile(rf"^{_FIND}\s+(?P<query>.+?)\s+{_AT}\s+(?P<site>{_SERVICE})$", re.I),
    re.compile(rf"^(?:such|suche)\s+{_ONLINE}\s+(?P<query>.+)$", re.I),
    re.compile(r"^(?:google|googel)\s+(?P<query>.+)$", re.I),
    # „Zeig mir Katzenvideos auf YouTube“, „Spiel Arteriion auf Spotify“ (YouTube-Spielen öffnet das erste Video)
    re.compile(rf"^(?:zeig|zeige|öffne|starte|spiel|spiele)\s+(?P<query>.+?)\s+{_AT}\s+(?P<site>{_SERVICE})(?:\s+ab)?$",
               re.I),
]
# Tasten, Tastenkürzel und Medientasten (ganze Äußerung, normalisiert)
_TIMES = r"(?:\s+(?P<times>\d{1,2}|zwei|drei|vier|fünf|zehn)\s*-?\s*mal)?"
KEY_NAMES = {
    "enter": "enter", "eingabe": "enter", "eingabetaste": "enter", "return": "enter", "tab": "tab",
    "tabulator": "tab", "escape": "escape", "esc": "escape", "leertaste": "space", "leerzeichen": "space",
    "space": "space", "rücktaste": "backspace", "backspace": "backspace", "entf": "delete", "entfernen": "delete",
    "pfeil hoch": "up", "pfeil nach oben": "up", "pfeil runter": "down", "pfeil nach unten": "down",
    "pfeil links": "left", "pfeil nach links": "left", "pfeil rechts": "right", "pfeil nach rechts": "right",
    "bild hoch": "page_up", "bild runter": "page_down", "pos1": "home", "ende": "end", "f5": "refresh",
    "f11": "fullscreen",
}
_KEY_NAME = "|".join(sorted((re.escape(k) for k in KEY_NAMES), key=len, reverse=True))
PRESS = [
    re.compile(rf"^(?:drück|drücke|press|betätige)\s+(?:auf\s+)?(?:die\s+)?(?:taste\s+)?(?P<name>{_KEY_NAME}){_TIMES}$"),
    re.compile(rf"^(?:drück|drücke)\s+(?P<times>\d{{1,2}}|zwei|drei|vier|fünf|zehn)\s*-?\s*mal\s+(?:auf\s+)?(?:die\s+)?"
               rf"(?:taste\s+)?(?P<name>{_KEY_NAME})$"),
    re.compile(rf"^(?:die\s+)?(?:taste\s+)?(?P<name>{_KEY_NAME}){_TIMES}\s+drücken$"),
]
SHORTCUTS = [(re.compile(f"^(?:{pattern})$"), key) for pattern, key in [
    (r"pause|pausiere|pausieren|musik (?:pausieren|anhalten|stoppen)|(?:stopp?|halt|pausier) die musik|mach weiter|"
     r"spiel weiter|weiterspielen|fortsetzen|play|wiedergabe (?:fortsetzen|pausieren)|musik weiter", "play_pause"),
    (r"nächste[rsn]? (?:lied|song|titel|track)|(?:lied )?überspringen|skip", "next_track"),
    (r"(?:vorherige|vorige|letzte)[rsn]? (?:lied|song|titel|track)|(?:ein )?lied zurück", "previous_track"),
    (r"(?:mach |dreh )?(?:etwas |ein bisschen |ein wenig |viel )?lauter|lautstärke (?:hoch|erhöhen|rauf)", "volume_up"),
    (r"(?:mach |dreh )?(?:etwas |ein bisschen |ein wenig |viel )?leiser|lautstärke (?:runter|verringern)", "volume_down"),
    (r"ton (?:aus|an|wieder an)|stumm(?:schalten)?|mute|stummschaltung (?:aus|an|aufheben)", "mute"),
    (r"kopier(?:e|en)?(?: das| es)?", "copy"),
    (r"füg(?:e)? (?:das |es )?ein|einfügen", "paste"),
    (r"schneid(?:e)? (?:das |es )?aus|ausschneiden", "cut"),
    (r"mach (?:das|es) rückgängig|rückgängig(?: machen)?", "undo"),
    (r"wiederherstellen", "redo"),
    (r"markier(?:e)? alles|alles markieren|alles auswählen|wähl(?:e)? alles aus", "select_all"),
    (r"speicher(?:e|n)?(?: das| es| die datei)?", "save"),
    (r"(?:öffne |mach )?(?:einen )?neuen tab(?: auf)?|neuer tab", "new_tab"),
    (r"schließ(?:e)? (?:den|diesen) tab|tab schließen", "close_tab"),
    (r"nächste[rn]? tab|tab weiter", "next_tab"),
    (r"vorherige[rn]? tab", "previous_tab"),
    (r"(?:geh |eine seite )?zurück|seite zurück", "back"),
    (r"(?:geh )?vorwärts|seite vor", "forward"),
    (r"(?:lade? )?(?:die seite )?neu(?: laden)?|aktualisier(?:e|en)?(?: die seite)?|seite neu laden", "refresh"),
    (r"vollbild(?:modus)?(?: an| aus)?", "fullscreen"),
    (r"vergrößern|zoom(?:e)? (?:rein|hinein)|größer", "zoom_in"),
    (r"verkleinern|zoom(?:e)? (?:raus|heraus)|kleiner", "zoom_out"),
    (r"scroll(?:e)? (?:runter|nach unten)|runterscrollen|nach unten scrollen|weiter runter", "page_down"),
    (r"scroll(?:e)? (?:hoch|rauf|nach oben)|hochscrollen|nach oben scrollen|weiter hoch", "page_up"),
    (r"fenster wechseln|wechsel(?:e)? das fenster", "switch_window"),
    (r"schließ(?:e)? (?:dieses|das aktive) (?:fenster|programm)|(?:dieses|das aktive) fenster schließen", "close_window"),
]]
_STEPS = {"volume_up": 5, "volume_down": 5}  # je Tastendruck 2 %
# Klicken, Tippen, Schließen, E-Mail-Entwurf, Anmelden
CLICK = [
    re.compile(r"^(?:klick|klicke|tipp|tippe|drück|drücke)\s+auf\s+(?:(?:den|die|das)\s+)?(?:(?:button|knopf|link|"
               r"schaltfläche|feld|menüpunkt|reiter)\s+)?(?P<label>.+)$", re.I),
    re.compile(r"^(?:klick|klicke)\s+(?:(?:den|die|das)\s+)?(?:(?:button|knopf|link|schaltfläche)\s+)?(?P<label>.+?)\s+an$",
               re.I),
]
_ENTER = r"(?P<enter>\s+und\s+(?:drück|drücke)\s+(?:enter|eingabe)|\s+und\s+(?:schick|sende)\s+(?:es|das|ihn)\s+ab)?"
TYPE = [
    re.compile(rf"^(?:tipp|tippe)\s+(?:(?:den text|folgendes|ein)\s+)?(?P<text>.+?){_ENTER}$", re.I),
    re.compile(rf"^(?:schreib|schreibe)\s+(?:hier|ins (?:textfeld|feld|suchfeld|dokument|fenster)|in das (?:feld|"
               rf"fenster|dokument|suchfeld))\s+(?P<text>.+?){_ENTER}$", re.I),
    re.compile(rf"^(?:gib|gebe)\s+(?P<text>.+?)\s+ein{_ENTER}$", re.I),
]
PASSWORD = re.compile(r"\b(?:passwort|passwörter|kennwort|pin|zugangsdaten)\b", re.I)
CLOSE = [
    re.compile(r"^(?:schließ|schließe|schliess|schliesse|beende|beend)\s+(?:(?:den|die|das|mein|meine|meinen)\s+)?"
               r"(?P<target>[\wäöüß .+&'-]+?)$", re.I),
    re.compile(r"^mach\s+(?:(?:den|die|das)\s+)?(?P<target>[\wäöüß .+&'-]+?)\s+zu$", re.I),
]
MAIL = re.compile(r"^(?:schreib|schreibe|verfass|verfasse|erstell|erstelle|entwirf|öffne)\s+(?:mir\s+)?"
                  r"(?:eine[nm]?\s+)?(?:neue\s+)?(?:e-?mail|mail|email|mailentwurf|e-mail-entwurf)"
                  r"(?:\s+an\s+(?P<to>.+?))?(?:\s+mit dem betreff\s+(?P<subject>.+?))?"
                  r"(?:\s+(?:mit dem text|mit dem inhalt|mit dem inhalt|und schreib(?:e)?)\s+(?P<body>.+))?$", re.I)
# Lose Anfänge ohne Inhalt („Dir eine E-Mail“, „E-Mail an Mama“, „Ich will eine Mail schreiben“) – der
# E-Mail-Assistent fragt Empfänger, Betreff und Text danach einzeln ab
MAIL_START = re.compile(r"^(?:(?:kannst|könntest|würdest) du|(?:können|könnten|würden) sie|ich (?:möchte|will|würde gerne?|"
                        r"hätte gerne?|muss)|lass uns|wir (?:müssen|sollten))?\s*"
                        r"(?:(?:schreib|schreibe|verfass|verfasse|erstell|erstelle|entwirf|mach|mache|öffne|schick|"
                        r"schicke|sende|send)\s+)?(?:(?:dir|mir|uns)\s+)?(?:eine[nm]?\s+|ne\s+)?(?:neue[nm]?\s+)?"
                        r"(?:e-?mail|mail|email|mailentwurf|e-mail-entwurf)(?:\s+an\s+(?P<to>.+?))?"
                        r"(?:\s+(?:schreiben|verfassen|erstellen|entwerfen|aufsetzen|machen|senden|schicken))?$", re.I)
LOGIN = [
    re.compile(r"^(?:melde|log|logg|logge)\s+mich\s+(?:bei|auf|in)\s+(?P<site>.+?)\s+(?:an|ein)$", re.I),
    re.compile(r"^(?:kannst du\s+)?mich\s+(?:bei|auf|in)\s+(?P<site>.+?)\s+(?:anmelden|einloggen)$", re.I),
]

# Timer, Erinnerungen, Termine, E-Mails (normalisierte Äußerung; Zeitangaben löst when.py auf)
TIMER_SET = [
    re.compile(r"^(?:stell|stelle|start|starte|mach|mache|setz|setze)\s+(?:mir\s+)?(?:(?:einen|den|nen)\s+)?"
               r"(?:(?P<label>[a-zäöüß]+)[- ]?)?timer\s+(?:auf|für|von|über)\s+(?P<dur>.+?)"
               r"(?:\s+(?:für|namens|mit dem namen)\s+(?P<label2>.+))?$", re.I),
    re.compile(r"^timer\s+(?:auf\s+|für\s+)?(?P<dur>.+?)(?:\s+(?:für|namens)\s+(?P<label2>.+))?$", re.I),
    re.compile(r"^(?P<dur>.+?)\s+timer(?:\s+(?:für|namens)\s+(?P<label2>.+))?$", re.I),
]
WAKE_ME = re.compile(r"^(?:weck|wecke)\s+mich\s+(?P<when>.+)$", re.I)
REMIND = re.compile(r"^(?:erinnere|erinner|erinnre)\s+mich\s+(?P<rest>.+)$", re.I)
TIMER_LIST = re.compile(r"^(?:wie (?:lange|viel zeit)\s+(?:läuft|hat|dauert)\s+(?:der|mein|den)\s+timer(?:\s+noch)?|"
                        r"welche (?:timer|erinnerungen) (?:laufen|habe ich|hab ich|gibt es|sind aktiv)|"
                        r"(?:laufende|meine) (?:timer|erinnerungen)|wie lange noch)$", re.I)
TIMER_CANCEL = [
    re.compile(r"^(?:stopp|stop|stoppe|beende|lösch|lösche|brich|brech|cancel|entferne)\s+(?P<all>alle\s+)?"
               r"(?:(?:den|die|meinen|meine)\s+)?(?:(?P<label>[a-zäöüß]+)[- ]?)?(?P<what>timer|erinnerungen?|wecker)"
               r"(?:\s+ab)?$", re.I),
    re.compile(r"^(?P<what>timer|wecker|erinnerung)\s+(?:aus|stopp|stop|abbrechen|löschen)$", re.I),
]
CAL_ADD = [
    re.compile(r"^(?:trag|trage)\s+(?:mir\s+)?(?P<rest>.+?)\s+(?:in (?:den|meinen) kalender\s+)?ein$", re.I),
    re.compile(r"^(?:erstell|erstelle|leg|lege|mach|mache|notier|notiere|plan|plane)\s+(?:mir\s+)?(?:einen\s+)?"
               r"(?:neuen\s+)?termin\s+(?P<rest>.+?)(?:\s+an)?$", re.I),
    re.compile(r"^(?:neuer\s+)?termin\s+(?P<rest>.+)$", re.I),
]
CAL_LIST = [
    re.compile(r"^(?:welche|was für) termine\s+(?:habe ich|hab ich|stehen an|gibt es|sind)(?:\s+(?P<when>.+?))?"
               r"(?:\s+(?:an|im kalender))?$", re.I),
    re.compile(r"^(?:was steht|was habe ich|was hab ich)\s+(?P<when>.+?)\s+(?:an|vor|im kalender)$", re.I),
    re.compile(r"^(?:zeig|zeige|lies|nenn|nenne)\s+(?:mir\s+)?(?:meine\s+)?termine(?:\s+(?P<when>.+?))?(?:\s+vor)?$", re.I),
    re.compile(r"^(?:meine\s+)?termine(?:\s+(?P<when>.+))?$", re.I),
]
CAL_NEXT = re.compile(r"^(?:wann ist|wann habe ich|wann hab ich|was ist)\s+(?:mein(?:en)?|der|den)\s+nächste[nr]?\s+"
                      r"termin$", re.I)
CAL_DELETE = [
    re.compile(r"^(?:lösch|lösche|entferne|streich|streiche)\s+(?:den\s+)?termin\s+(?P<rest>.+)$", re.I),
    re.compile(r"^sag\s+(?:den\s+)?(?:termin\s+)?(?P<rest>.+?)\s+ab$", re.I),
]
MAIL_LIST = [
    re.compile(r"^(?:habe ich|hab ich|gibt es)\s+(?:neue|ungelesene)\s+(?:e-?mails?|mails?|nachrichten im postfach)$", re.I),
    re.compile(r"^(?:lies|lese|zeig|zeige|check|prüf|prüfe)\s+(?:mir\s+)?(?:meine\s+)?(?:neuen?\s+|ungelesenen?\s+)?"
               r"(?:e-?mails?|mails?|postfach)(?:\s+vor)?$", re.I),
    re.compile(r"^was (?:ist|gibt es neues) (?:in meinem|im) postfach$", re.I),
]


def _time_text(text: str) -> str:
    """Wie normalize_utterance, aber mit Groß-/Kleinschreibung (Erinnerungstexte, Titel) und Punkten in Datums- und
    Uhrzeitangaben („3. Oktober“, „15.30“)."""
    text = re.sub(r"[!?;„“\"]+", " ", text)
    text = re.sub(r"(?<!\d)\.(?=\s|$)", " ", text)  # Satzpunkte, nicht „3.“
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(rf"^(?:(?:hey|hallo|ok|okay)\s+)?{WAKE}\s*,?\s+|^(?:sag mal|bitte)\s+", "", text, flags=re.I)
    return re.sub(rf"[\s,]+(?:bitte|{WAKE}|sir)$", "", text, flags=re.I).strip(" ,")


def _timer_label(*labels: str | None) -> str:
    for label in labels:
        if label and label not in ("einen", "den", "neuen", "kurzen"):
            return re.sub(r"^(?:die|den|das|der)\s+", "", label.strip()).capitalize()
    return ""


def _reminder_text(rest: str) -> str:
    text = re.sub(r"^(?:an|daran|dran)\b[,]?\s*(?:dass\s+)?", "", rest.strip(" ,"), flags=re.I)
    return text.strip(" ,.") or "Erinnerung"


def _event_title(rest: str) -> str:
    title = re.sub(r"^(?:(?:einen|den|neuen|ein|eine)\s+)*(?:termin\s+)?(?:(?:beim|bei|mit|für|zum|zur|als)\s+)?", "",
                   rest.strip(" ,:"), flags=re.I)
    title = re.sub(r"\s+(?:als termin|in den kalender|ein)$", "", title, flags=re.I).strip(" ,.:")
    return title[:1].upper() + title[1:] if title else ""


def _day_range(when: str | None, now: datetime) -> tuple[str, int] | None:
    """„morgen“ -> (Datum, 1 Tag), „diese Woche“ -> (heute, bis Sonntag), „nächste Woche“ -> (Montag, 7)."""
    when = (when or "heute").strip().lower()
    if re.fullmatch(r"(?:diese|in dieser) woche", when):
        return now.date().isoformat(), 7 - now.weekday()
    if re.fullmatch(r"(?:nächste|kommende|in der nächsten) woche", when):
        monday = now.date() + timedelta(days=7 - now.weekday())
        return monday.isoformat(), 7
    parsed = parse_when(when, now)
    return (parsed.at.date().isoformat(), 1) if parsed else None


# Unvollständige Befehle („Such mal …“, „Öffne“): nachfragen statt raten, der nächste Satz ergänzt den Befehl
INCOMPLETE = re.compile(rf"^(?P<verb>{_FIND}|schau|guck|öffne|starte|spiel|spiele|zeig|zeige|schließe?|beende|tippe?|"
                        rf"schreib|schreibe)(?:\s+(?:was|etwas|nach|für mich|mir))*(?:\s+{_AT}\s+(?P<site>{_SERVICE}))?"
                        rf"(?:\s+nach)?$", re.I)
# „Jarvis“ in den Schreibweisen der Spracherkennung (die Oberfläche erkennt noch unschärfer, docs/07)
WAKE = r"(?:jarvis|jarwis|javis|jervis|jarves|jarviz|jarvice|charvis|garvis|dschawis|dschavis|tschawis)"
_PREFIX = re.compile(rf"^(?:(?:hey|hallo|ok|okay)\s+)?{WAKE}\s*[,:]?\s*|^bitte\s+", re.I)
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
    text = re.sub(rf"\s+(?:{WAKE}|sir)$", "", text, flags=re.I)
    text = re.sub(r"^(\S+)\s+mir\s+(?=\S)", r"\1 ", text) if re.match(
        r"^(?:such|suche|google|finde|find|zeig|zeige|öffne|spiel|spiele|hol|hole|gib|schau|guck)\s+mir\s", text,
        re.I) else text
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
            if re.fullmatch(_CLOSE_VERBS, verb):
                return f"schließe {rest}"
            return f"öffne {rest}"
    return text


# Status und Tagesplan (Skills mit Capability) sowie Gesprächs-Intents ohne Aktion. Verglichen wird die ganze,
# normalisierte Äußerung – „Status?“ ja, „Wie ist der Status der Waschmaschine?“ nein (geht an das LLM).
# Gedächtnis: „Merk dir, dass ich Kaffee schwarz trinke“, „Was weißt du über mich?“, „Vergiss, dass …“
# Durchsagen: „Sag in der Küche, dass das Essen fertig ist“, „Durchsage an alle: Abfahrt in fünf Minuten“
_ROOM = r"(?P<room>(?:in|im|ins|an|für)\s+(?:der\s+|dem\s+|die\s+|den\s+|das\s+)?[\wäöüß-]+|an\s+alle|allen|überall)"
ANNOUNCE = [
    re.compile(r"^(?:mach\w*\s+(?:eine\s+)?)?durchsage(?:\s+" + _ROOM + r")?\s*[:,]?\s+(?P<text>.+)$", re.I),
    re.compile(r"^(?:sag|sage|richte|richt)\s+" + _ROOM + r"\s*,?\s*(?:bescheid\s*,?\s*)?(?:aus\s*,?\s*)?"
               r"(?P<dass>dass\s+)?(?P<text>.+)$", re.I),
]
REMEMBER = re.compile(r"^(?:bitte\s+)?(?:merk|merke|notier|notiere|speicher|speichere)\s+(?:dir|dir bitte)\s*[,:]?\s*"
                      r"(?:bitte\s+)?(?:(?P<dass>dass)\s+|folgendes\s*:?\s*)?(?P<what>.+?)[.!]?$", re.I)
MEMORY_LIST = re.compile(r"^(?:was weißt du (?:alles )?über mich|was hast du dir (?:alles )?(?:über mich )?gemerkt|"
                         r"was hast du (?:alles )?gespeichert|was merkst du dir (?:alles )?|woran erinnerst du dich)$")
FORGET = re.compile(r"^(?:bitte\s+)?vergiss\s*,?\s*(?:bitte\s+)?(?:dass|das mit|das mit dem|das mit der)\s+(?P<what>.+?)[.!]?$",
                    re.I)
# Börse: „Wie steht Apple?“, „Wie steht der DAX?“, „Was kostet Bitcoin?“, „Aktienkurs von SAP“
STOCK = [
    re.compile(r"^wie steht (?:es um )?(?:die |der |das )?(?:aktie (?:von |der )?)?(?P<name>.+?)(?:[- ]aktie| heute| gerade|"
               r" an der börse)?$"),
    re.compile(r"^(?:wie ist |was ist |zeig mir )?(?:der |den )?(?:aktien)?kurs (?:von |der |vom |des )?(?P<name>.+)$"),
    re.compile(r"^was macht (?:die |der )?(?P<name>.+?)[- ]aktie$"),
    re.compile(r"^was (?:kostet|ist) (?:ein |eine )?(?P<name>bitcoin|ethereum|gold|silber)(?: gerade| heute| wert)?$"),
]
# Sehen: „Was siehst du?“, „Was halte ich in der Hand?“ – die Oberfläche nimmt dann ein Kamerabild auf
VISION = re.compile(r"^(?:was siehst du(?: gerade| hier| da)?|was ist (?:das hier|hier|das in meiner hand)|"
                    r"was halte ich (?:hier |gerade )?(?:in der hand|in die kamera|hoch)|schau (?:mal )?(?:her|hin|"
                    r"in die kamera)|beschreib(?:e)? (?:mir )?(?:was du siehst|das bild|die szene|was vor dir ist)|"
                    r"(?:lies|lese) (?:mir )?(?:das|den text|das etikett|das schild)(?: hier)? vor|"
                    r"wie viele finger (?:halte ich hoch|zeige ich)|welche farbe hat (?:das|mein) .+|"
                    r"(?:wie )?sehe ich (?:aus|gut aus)|kannst du (?:mich|das) sehen)$")
# Systemmonitor und feste Systemaktionen (normalisierte Äußerung)
SYSMON = [
    ("cpu", re.compile(r"^(?:wie (?:hoch|stark|sehr) ist (?:die |der )?(?:cpu|prozessor)(?:[- ]?auslastung| ausgelastet)?|"
                       r"(?:cpu|prozessor)[- ]?auslastung|wie ist die (?:cpu|prozessor)[- ]?auslastung|"
                       r"wie ausgelastet ist (?:der pc|der rechner|die cpu|der prozessor))$")),
    ("memory", re.compile(r"^(?:wie viel (?:arbeits)?speicher ist (?:noch )?(?:frei|belegt)|(?:arbeits)?speicherauslastung|"
                          r"wie voll ist der arbeitsspeicher|wie viel ram (?:ist )?(?:noch )?(?:frei|belegt))$")),
    ("disk", re.compile(r"^(?:wie (?:viel|voll) (?:speicherplatz|platz) (?:ist |habe ich )?(?:noch )?(?:frei|auf der festplatte)|"
                        r"wie voll ist (?:die festplatte|die platte|das laufwerk|laufwerk c)|wie viel platz ist (?:noch )?frei)$")),
    ("temperature", re.compile(r"^(?:wie (?:warm|heiß) ist (?:die cpu|der prozessor|der pc|der rechner|die grafikkarte)|"
                               r"(?:cpu|gpu|pc)[- ]?temperatur(?:en)?|wie ist die temperatur (?:der cpu|des pcs|der grafikkarte))$")),
    ("all", re.compile(r"^(?:systemmonitor|zeig (?:mir )?den systemmonitor|wie geht es (?:dem pc|meinem pc|dem rechner)|"
                       r"(?:pc|rechner)[- ]?(?:status|auslastung|zustand)|wie läuft (?:der pc|mein pc|der rechner))$")),
]
SYSTEM_ACTION = [
    ("lock_screen", re.compile(r"^(?:sperr|sperre)\s+(?:den|meinen)\s+(?:bildschirm|pc|rechner|computer)$|"
                               r"^(?:bildschirm|pc|rechner) sperren$")),
    ("empty_recycle_bin", re.compile(r"^(?:leer|leere)\s+(?:den\s+)?papierkorb$|^papierkorb leeren$")),
    ("check_updates", re.compile(r"^(?:such|suche|prüf|prüfe)\s+(?:nach\s+)?(?:windows[- ]?)?updates$|"
                                 r"^(?:gibt es|sind) (?:neue )?(?:windows[- ]?)?updates(?: da)?$")),
    ("clean_temp", re.compile(r"^(?:räum|räume|lösch|lösche)\s+(?:die\s+)?(?:temporären dateien|temp[- ]?dateien|temp ordner|"
                              r"den temp[- ]?ordner)(?:\s+auf)?$")),
    ("restart", re.compile(r"^(?:starte?|start)\s+(?:den|meinen)\s+(?:pc|rechner|computer)\s+neu$|^(?:pc|rechner) neu starten$")),
    ("shutdown", re.compile(r"^(?:fahr|fahre)\s+(?:den|meinen)\s+(?:pc|rechner|computer)\s+(?:herunter|runter)$|"
                            r"^(?:pc|rechner) (?:herunterfahren|runterfahren|ausschalten)$|^schalte? (?:den )?(?:pc|rechner) aus$")),
    ("sleep", re.compile(r"^(?:schick|versetz|leg)e?\s+(?:den\s+)?(?:pc|rechner)\s+(?:in den )?(?:ruhezustand|energiesparmodus|"
                         r"schlafen)$|^(?:pc|rechner) (?:schlafen legen|in den ruhezustand)$")),
    ("cancel_shutdown", re.compile(r"^(?:brich|breche)\s+das\s+(?:herunterfahren|neustarten)\s+ab$|"
                                   r"^(?:herunterfahren|neustart) abbrechen$")),
]
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
    ("hear_check", re.compile(r"^(?:(?:kannst|verstehst|hörst) du mich|(?:können|verstehen|hören) sie mich|"
                              r"hört man mich|hörst du)(?: (?:jetzt|nun|gut|besser|wieder|richtig|überhaupt|noch))*"
                              r"(?: (?:verstehen|hören))?$|^(?:test|testen|mikrofon ?test|mikrotest|eins zwei(?: drei)?|"
                              r"1 2(?: 3)?)$")),
    ("goodnight", re.compile(r"^gute nacht$")),
    ("goodbye", re.compile(r"^(?:tschüss|tschau|ciao|bis später|bis dann|auf wiedersehen|das wars|danke das wars)$")),
]


def normalize_utterance(text: str) -> str:
    """Kleinbuchstaben, ohne Satzzeichen, ohne Anrede/Füllwörter am Rand („Jarvis, …“, „… bitte“)."""
    text = re.sub(r"[.,!?;:„“\"]+", " ", text.lower())
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(rf"^(?:(?:hey|hallo|ok|okay)\s+)?{WAKE}\s+|^(?:sag mal|bitte)\s+", "", text)
    return re.sub(rf"\s+(?:bitte|{WAKE}|sir)$", "", text).strip()


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


def main_clause(clause: str) -> str:
    """„ich meinen Kaffee schwarz trinke“ -> „ich trinke meinen Kaffee schwarz“ (Nebensatz nach „dass“ umstellen).
    Nur mit „ich“/„wir“ vorn – sonst bleibt der Satz, wie er gesagt wurde."""
    words = clause.split()
    if len(words) < 3 or not re.fullmatch(r"[a-zäöüß]+", words[-1]):
        return clause
    first = words[0].lower()
    if first in ("ich", "wir", "er", "sie", "es", "man"):
        subject = 1
    elif first in ("mein", "meine", "meinen", "unser", "unsere", "der", "die", "das", "dein", "deine") or \
            re.fullmatch(r"[A-ZÄÖÜ]\w*s", words[0]):
        subject = 2  # „mein Auto …“, „Mamas Geburtstag …“
    elif words[0][:1].isupper():
        subject = 1  # „Max Vegetarier ist“
    else:
        return clause
    if len(words) <= subject + 1:
        return clause
    return " ".join([*words[:subject], words[-1], *words[subject:-1]])


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
        self.home: HomeIndex | None = None  # Räume und Geräte aus Home Assistant (smarthome.py)

    def set_home(self, index: HomeIndex | None) -> None:
        """Neue Geräteliste aus Home Assistant: Räume und Lichter gelten sofort für die Sofortbefehle."""
        self.home = index
        if index is not None:
            self.area_lights = index.area_lights()
            self.area_aliases = index.area_aliases()

    def _area(self, spoken: str) -> str | None:
        spoken = spoken.lower()
        area = self.area_aliases.get(spoken, spoken)
        return area if area in self.area_lights else None

    def match(self, text: str, default_area: str | None = None, now: datetime | None = None) -> FastPathMatch | None:
        now = now if now is not None and now.tzinfo is not None else (now or datetime.now()).astimezone()
        if found := self._match_time(_time_text(text), now):
            return found
        return self._match_rest(text, default_area)

    def _match_time(self, plain: str, now: datetime) -> FastPathMatch | None:
        """Timer, Wecker, Erinnerungen, Termine, E-Mails."""
        for pattern in TIMER_SET:
            if (m := pattern.match(plain)) and (seconds := parse_duration(m["dur"])):
                label = _timer_label(m.groupdict().get("label"), m.groupdict().get("label2"))
                return FastPathMatch("timer.start", {"duration_s": seconds, **({"label": label} if label else {})},
                                     0.95, "timer", {"seconds": seconds})
        if m := WAKE_ME.match(plain):
            if when := parse_when(m["when"], now):
                return FastPathMatch("reminder.create", {"text": "Aufstehen", "at": when.at.isoformat()}, 0.93,
                                     "wake_me", {"at": when.at.isoformat()})
        if m := REMIND.match(plain):
            when = parse_when(m["rest"], now)
            if when is None:
                return FastPathMatch("", {}, 0.9, "incomplete", {"kind": "when", "prefix": plain})
            return FastPathMatch("reminder.create", {"text": _reminder_text(when.rest), "at": when.at.isoformat()},
                                 0.93, "reminder", {"at": when.at.isoformat()})
        if TIMER_LIST.match(plain):
            return FastPathMatch("timer.list", {}, 0.93, "timer_list")
        for pattern in TIMER_CANCEL:
            if m := pattern.match(plain):
                what = m["what"]
                arguments: dict[str, Any] = {"kind": "reminder" if what.startswith("erinnerung") else "timer"}
                if m.groupdict().get("all") or what == "erinnerungen":
                    arguments["all"] = True
                label = _timer_label(m.groupdict().get("label"))
                if label:
                    arguments["label"] = label
                return FastPathMatch("timer.cancel", arguments, 0.93, "timer_cancel")
        if DAY_PLAN.match(plain):
            return None  # „Was steht morgen an?“: Tagesplan mit Terminen und Wetter
        if CAL_NEXT.match(plain):
            return FastPathMatch("calendar.list", {"upcoming": True}, 0.92, "calendar_next")
        for pattern in CAL_LIST:
            if (m := pattern.match(plain)) and (span := _day_range(m["when"], now)):
                day, days = span
                arguments = {"day": day, **({"days": days} if days > 1 else {})}
                return FastPathMatch("calendar.list", arguments, 0.92, "calendar_list", {"when": m["when"] or "heute"})
        for pattern in CAL_DELETE:
            if m := pattern.match(plain):
                when = parse_when(m["rest"], now)
                title = _event_title(when.rest if when else m["rest"])
                if title:
                    arguments = {"title": title, **({"date": when.at.date().isoformat()} if when else {})}
                    return FastPathMatch("calendar.delete", arguments, 0.9, "calendar_delete")
        for pattern in CAL_ADD:
            if m := pattern.match(plain):
                when = parse_when(m["rest"], now)
                title = _event_title(when.rest if when else m["rest"])
                if when is None:
                    return FastPathMatch("", {}, 0.9, "incomplete", {"kind": "when_event",
                                                                     "prefix": f"termin {m['rest']}"})
                if not title:
                    return None
                start = when.at.date().isoformat() if when.all_day else when.at.isoformat()
                return FastPathMatch("calendar.add", {"title": title, "start": start}, 0.92, "calendar_add")
        for pattern in MAIL_LIST:
            if pattern.match(plain):
                return FastPathMatch("mail.list_unread", {}, 0.92, "mail_list")
        return None

    def _match_rest(self, text: str, default_area: str | None) -> FastPathMatch | None:
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
        raw = re.sub(r"\s+", " ", _PREFIX.sub("", text.strip())).strip()
        for pattern in ANNOUNCE:
            if (m := pattern.match(raw)) and len(m["text"].split()) >= 2:
                text = m["text"].strip(" ,.!?")
                if m.groupdict().get("dass"):
                    text = main_clause(text)
                room = (m["room"] or "").strip()
                everywhere = not room or bool(re.fullmatch(r"an alle|allen|überall", room, re.I))
                arguments = {"text": text[0].upper() + text[1:], **({} if everywhere else {"room": room})}
                prep = " ".join(room.split()[:-1]).lower()  # „in der“ – für die Antwort „… in der Küche angekommen“
                return FastPathMatch("message.announce", arguments, 0.95, "announce", {"prep": prep})
        if (m := REMEMBER.match(raw)) and len(m["what"].split()) >= 2:
            what = m["what"].strip(" ,")
            if m["dass"]:
                what = main_clause(what)
            return FastPathMatch("memory.remember", {"content": what[0].upper() + what[1:]}, 0.95, "remember")
        if MEMORY_LIST.match(simple):
            return FastPathMatch("memory.list", {"limit": 10}, 0.95, "memory_list")
        if (m := FORGET.match(raw)) and len(m["what"].split()) >= 2:
            return FastPathMatch("memory.forget", {"query": m["what"].strip(" ,")}, 0.95, "forget")
        if VISION.match(simple):
            return FastPathMatch("vision.describe", {}, 0.95, "vision")
        for pattern in STOCK:
            if m := pattern.match(simple):
                from .stocks import SYMBOLS

                name = re.sub(r"[- ]?aktie$", "", m["name"]).strip()
                if name in SYMBOLS or "aktie" in simple:  # sonst wäre „Wie steht der Timer?“ eine Aktie
                    return FastPathMatch("info.stock", {"name": name}, 0.93, "stock", {"name": name})
        for focus, pattern in SYSMON:
            if pattern.match(simple):
                return FastPathMatch("system.monitor", {"focus": focus}, 0.95, "sysmon", {"focus": focus})
        for name, pattern in SYSTEM_ACTION:
            if pattern.match(simple):
                return FastPathMatch("pc.system_action", {"name": name}, 0.93, "system_action", {"name": name})
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
        # Haus: Räume und Geräte aus Home Assistant („Licht im Bad aus“, „Rollläden runter“, „Heizung auf 21 Grad“)
        # (fest konfigurierte Räume ohne Home Assistant – Tests, Demo – bleiben bei den Grammatiken oben)
        plain = re.sub(r"\s+", " ", _FILLER.sub(" ", simple)).strip()
        if (self.home is not None or not self.area_lights) and (home := match_home(plain, self.home, default_area)):
            return FastPathMatch(home.capability, home.arguments, 0.94, home.grammar, home.slots)
        return self._match_pc(canonical_command(text), text)

    def _match_pc(self, text: str, raw: str | None = None) -> FastPathMatch | None:
        if m := self._match_control(text, raw or text):
            return m
        if m := INCOMPLETE.match(text):
            return _incomplete(m["verb"].lower(), SEARCH_SERVICES.get((m["site"] or "").lower()))
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
                query = file_query(m["query"])
                return FastPathMatch("pc.search_files", {"query": query}, 0.92, "pc_search_files", {"query": query})
        for pattern in PC_FIND_FILES:
            if m := pattern.match(text):
                query, kind = m["query"].strip(), (m.groupdict().get("kind") or "").lower()
                if kind == "ordner" and _pc_target(query) in PC_FOLDERS:
                    folder = PC_FOLDERS[_pc_target(query)]
                    return FastPathMatch("pc.open_folder", {"folder": folder}, 0.95, "pc_open_folder", {"target": query})
                arguments = {"query": query, "kind": {"ordner": "folder", "datei": "file"}.get(kind, "any")}
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
        for pattern in PC_SEARCH:
            if m := pattern.match(text):
                query, refers_back = clean_query(m["query"])
                site = SEARCH_SERVICES.get(re.sub(r"\s+", " ", (m.groupdict().get("site") or "").lower()))
                arguments = {"query": query, **({"site": site} if site and site != "google" else {})}
                return FastPathMatch("pc.search_web", arguments, 0.93, "pc_search_site" if site else "pc_search",
                                     {"query": query, "site": site, "refers_back": refers_back})
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


    def _match_control(self, text: str, raw: str) -> FastPathMatch | None:
        """Fenster, Tastatur, Maus, Schließen, E-Mail-Entwurf, Anmelden. Tasten werden an der Originaläußerung
        erkannt (die Befehlsform entfernt „mal“ – „Drück zweimal Tab“ braucht es)."""
        plain = normalize_utterance(raw)
        # erst wörtlich („Drück Tab 3 mal“), dann ohne Füllwörter („Drück mal Enter“, „Mach mal lauter“)
        for variant in dict.fromkeys([plain, re.sub(r"\s+", " ", _FILLER.sub(" ", plain)).strip()]):
            for pattern in PRESS:
                if m := pattern.match(variant):
                    times = m["times"]
                    count = int(times) if times and times.isdigit() else NUMBER_WORDS.get(times or "", 1)
                    return _press(KEY_NAMES[m["name"]], count)
            for pattern, key in SHORTCUTS:
                if pattern.match(variant):
                    steps = _STEPS.get(key, 1)
                    if key in _STEPS and re.search(r"\bviel\b", variant):
                        steps *= 2
                    elif key in _STEPS and re.search(r"\b(?:etwas|ein bisschen|ein wenig)\b", variant):
                        steps = 3
                    return _press(key, steps)
        if m := MAIL.match(text) or MAIL_START.match(text):
            to = (m["to"] or "").strip()
            if re.search(r"\b(?:dass|wegen|ob|weil|damit)\b", to):
                return None  # „… an Max, dass ich später komme“: den Text formuliert das Sprachmodell
            subject, body = m.groupdict().get("subject"), m.groupdict().get("body")
            if not subject and not body:  # nur Empfänger oder gar nichts: der E-Mail-Assistent fragt nach
                return FastPathMatch("pc.compose_mail", {}, 0.9, "mail_dialog", {"to": to})
            arguments = {k: v.strip() for k, v in (("to", to), ("subject", subject), ("body", body)) if v}
            return FastPathMatch("pc.compose_mail", arguments, 0.92, "compose_mail", {"to": to})
        for pattern in LOGIN:
            if m := pattern.match(text):
                site = _web_query(m["site"])
                return FastPathMatch("pc.open_link", {"query": f"{site} anmelden"}, 0.9, "login", {"site": site})
        plain = normalize_utterance(text)
        if re.match(r"^(?:tipp|tippe|schreib|schreibe|gib|gebe)\b", plain) and PASSWORD.search(plain):
            return FastPathMatch("", {}, 0.95, "refuse_password")
        for pattern in CLICK:
            if m := pattern.match(text):
                label = m["label"].strip()
                return FastPathMatch("pc.click", {"label": label}, 0.9, "click", {"label": label})
        for pattern in TYPE:
            if m := pattern.match(text):
                typed = m["text"].strip()
                if re.match(r"^(?:mir|uns|eine?[nm]?\s+(?:e-?mail|mail|nachricht|brief|sms))\b", typed, re.I):
                    return None
                arguments = {"text": typed, **({"enter": True} if m["enter"] else {})}
                return FastPathMatch("pc.type_text", arguments, 0.9, "type", {"text": typed})
        for pattern in CLOSE:
            if m := pattern.match(text):
                target = _pc_target(m["target"])
                if target in ("tab", "den tab"):
                    return _press("close_tab", 1)
                if _HOME_WORDS.search(target) or not 2 <= len(target) <= 60:
                    return None
                app = PC_APPS.get(target, target)
                return FastPathMatch("pc.close_app", {"app": app}, 0.9, "close_app", {"target": target})
        return None


def _press(key: str, times: int) -> FastPathMatch:
    arguments = {"key": key, **({"times": times} if times > 1 else {})}
    return FastPathMatch("pc.press_key", arguments, 0.93, "press_key", {"key": key})


def _incomplete(verb: str, site: str | None) -> FastPathMatch:
    """Befehl ohne Ziel: JARVIS fragt nach; ``prefix`` + nächster Satz ergibt den vollständigen Befehl."""
    if re.fullmatch(rf"{_FIND}|schau|guck", verb):
        kind, prefix = "search", f"such auf {site} nach" if site else "such nach"
    elif verb.startswith(("spiel",)):
        kind, prefix = "play", "spiel"
    elif verb.startswith(("schließ", "beende")):
        kind, prefix = "close", "schließe"
    elif verb.startswith(("tipp", "schreib")):
        kind, prefix = "type", "tippe"
    else:
        kind, prefix = "open", "zeig" if verb.startswith("zeig") else "öffne"
    return FastPathMatch("", {}, 0.9, "incomplete", {"kind": kind, "prefix": prefix, "site": site})


def incomplete_search(site: str | None = None) -> FastPathMatch:
    """„Such nach mehr Infos“ ohne Thema: nachfragen, wonach."""
    return _incomplete("such", site)


def _web_query(query: str) -> str:
    """„einem Lasagne-Rezept“ -> „Lasagne-Rezept“: Artikel am Anfang stören die Suche nur."""
    return re.sub(r"^(?:einen|einem|einer|eines|eine|ein|den|dem|der|die|das|des)\s+", "", query.strip(), flags=re.I)


# Rückbezug und Buchstabierhilfen gehören nicht in den Suchbegriff:
# „Such nach Arterien, so wie ich es dir gerade geschrieben habe, mit 2 i“ -> „Arterien“ (+ Rückbezug)
_BACKREF = re.compile(r"[\s,]*(?:so\s+)?wie ich (?:es|das|ihn|sie)\s+(?:(?:dir|ihnen)\s+)?(?:(?:gerade|eben|vorhin|zuvor|"
                      r"oben)\s+)?(?:geschrieben|getippt|gesagt|buchstabiert|eingegeben)(?:\s+(?:habe|hab))?", re.I)
_SPELLING = re.compile(r"[\s,]*(?:mit|und)\s+(?:zwei|2|drei|3|doppel(?:tem|ten)?|doppelt(?:em)?)\s*-?\s*[a-zäöüß]\b"
                       r"(?:\s+geschrieben)?|[\s,]*(?:richtig|korrekt|genau so) geschrieben", re.I)


def clean_query(query: str) -> tuple[str, bool]:
    """Suchbegriff ohne Rückbezug und Buchstabierhilfen; zweiter Wert: der Nutzer verweist auf Geschriebenes."""
    refers_back = bool(_BACKREF.search(query))
    query = _SPELLING.sub("", _BACKREF.sub("", query))
    return _web_query(query).strip(" ,.;:"), refers_back


# Korrektur direkt nach einer Suche: „ARTERIION“, „Nein, ich meinte Arteriion“, „Es schreibt sich Arteriion“
CORRECTION = re.compile(r"^(?:(?:nein|ne|nee|falsch|quatsch|nicht so)\b[\s,.]*)?(?:ich (?:meinte|meine|wollte)|"
                        r"gemeint (?:war|ist)|es (?:heißt|schreibt sich|wird geschrieben)|richtig (?:ist|wäre|heißt es)|"
                        r"(?:such|suche) lieber(?: nach)?|nimm lieber|(?:probier|versuch)(?:e)?(?: es)?(?: mal)? mit|"
                        r"nicht \S+(?: \S+)? sondern)\s+(?P<term>.+)$", re.I)
_NOT_TERMS = set("""es das der die den dem des ein eine einen ist sind war gab gibt hat habe hab und oder aber ich du er sie
wir ihr mal noch nicht ja nein so wie was wo wer wann warum gut okay ok super danke cool toll prima schön perfekt passt
genau stopp stop weiter nichts egal hallo jarvis sir bitte na hm hmm äh ähm also richtig falsch klar sicher vielleicht auch
nur jetzt hier da dort mehr weniger gerne gern sehr zu auf in im an am mit von für bei nach um wieder nochmal nochmals schon
doch fertig los alles wow krass interessant spannend wahnsinn oha aha achso echt nice geil verstehe verstanden
faszinierend unglaublich lustig schade stark heftig irre mega top witzig ah oh""".split())


def bare_term(text: str) -> str | None:
    """Ein bis drei Wörter ohne Füll- und Funktionswörter („ARTERIION“, „Arteriion Live“) – ein bloßer Begriff."""
    words = re.sub(r"[.,!;:]+$", "", _time_text(text)).split()
    if not 1 <= len(words) <= 3 or text.strip().endswith("?"):
        return None
    if any(w.lower().strip(",.!") in _NOT_TERMS for w in words):
        return None
    return " ".join(words)


def correction_term(text: str, *, explicit: bool = False) -> tuple[str, str | None] | None:
    """Neuer Suchbegriff (und ggf. Dienst), wenn die Äußerung eine Korrektur der letzten Suche ist.
    ``explicit``: nur „ich meinte …“ u. Ä. zählt, kein bloßes Wort (etwa nach dem Öffnen eines Programms)."""
    plain = re.sub(r"[.!]+$", "", _time_text(text)).strip()
    m = CORRECTION.match(plain)
    term = m["term"] if m else (None if explicit else bare_term(text))
    if not term:
        return None
    term, _ = clean_query(term)
    term = re.sub(r"^(?:such|suche|finde|find)(?:\s+nach)?\s+", "", term, flags=re.I)  # „suche Heizung“ -> „Heizung“
    site = None
    if s := re.match(rf"^(?P<q>.+?)\s+{_AT}\s+(?P<site>{_SERVICE})$", term, re.I):
        term, site = s["q"], SEARCH_SERVICES[re.sub(r"\s+", " ", s["site"].lower())]
    return (term, site) if term else None


def _open_link(query: str, site: str | None) -> FastPathMatch:
    query, _ = clean_query(query)
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
