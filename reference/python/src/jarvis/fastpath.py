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

_AREA = r"(?:in der|im|in dem|in)\s+(?P<area>[a-zäöüß]+)"
LIGHT_ON_OFF = re.compile(rf"^(?:mach|schalte?)\s+(?:das\s+)?licht\s+{_AREA}\s+(?P<state>an|aus|ein)$", re.I)
LIGHT_PCT = re.compile(
    rf"^(?:mach|stell|dimm|setz)e?\s+(?:das\s+)?licht\s+{_AREA}\s+auf\s+(?P<num>\d{{1,3}}|[a-zäöüß]+)\s*(?:prozent|%)$",
    re.I,
)
TIMER = re.compile(
    r"^(?:stell|starte?)e?\s+(?:einen\s+)?timer\s+(?:auf|für)\s+(?P<num>\d{1,3}|[a-zäöüß]+)\s+(?P<unit>minuten?|sekunden?|stunden?)$",
    re.I,
)


# PC-Befehle („Öffne den Explorer“, „Starte Spotify“, „Öffne YouTube“, „Such im Internet nach …“)
PC_OPEN = re.compile(r"^(?:öffne|öffnen|starte|start|mach|zeig)\s+(?:mir\s+)?(?:mal\s+)?"
                     r"(?:den|die|das|einen|eine|ein|meine|meinen|mein)?\s*(?P<target>[a-zäöüß0-9 .\-]+?)(?:\s+auf)?$",
                     re.I)
PC_SEARCH = re.compile(r"^(?:such(?:e)?|google)\s+(?:mal\s+)?(?:im internet|online|im web|bei google|in google)?\s*"
                       r"nach\s+(?P<query>.+)$", re.I)
PC_GOOGLE = re.compile(r"^google\s+(?P<query>.+)$", re.I)
_PREFIX = re.compile(r"^(?:(?:hey|hallo|ok|okay)\s+)?jarvis\s*[,:]?\s*|^bitte\s+", re.I)
_DOMAIN = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:de|com|org|net|io|eu|at|ch|tv|info)$")

PC_APPS = {
    "explorer": "explorer", "datei explorer": "explorer", "dateiexplorer": "explorer", "datei-explorer": "explorer",
    "windows explorer": "explorer", "file explorer": "explorer",
    "browser": "browser", "webbrowser": "browser", "internetbrowser": "browser", "internet": "browser",
    "chrome": "browser", "edge": "browser", "firefox": "browser",
    "editor": "editor", "texteditor": "editor", "notepad": "editor", "notizblock": "editor",
    "rechner": "rechner", "taschenrechner": "rechner", "paint": "paint",
    "einstellungen": "einstellungen", "windows einstellungen": "einstellungen", "systemeinstellungen": "einstellungen",
    "taskmanager": "taskmanager", "task manager": "taskmanager", "task-manager": "taskmanager",
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
    "twitch": "https://www.twitch.tv", "ebay": "https://www.ebay.de", "whatsapp": "https://web.whatsapp.com",
    "instagram": "https://www.instagram.com", "facebook": "https://www.facebook.com",
}


def _pc_target(raw: str) -> str:
    target = re.sub(r"\s+", " ", raw.lower()).strip(" .-")
    target = re.sub(r"^(?:ordner|programm|app|webseite|seite)\s+", "", target)
    return re.sub(r"[\s-]*(?:ordner|programm|app)$", "", target)


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

    def match(self, text: str) -> FastPathMatch | None:
        normalized = re.sub(r"[.!?]+$", "", text.strip())
        if m := LIGHT_ON_OFF.match(normalized):
            area = self._area(m["area"])
            if area:
                return FastPathMatch(
                    "home.set_light",
                    {"entity_ids": self.area_lights[area], "on": m["state"].lower() in ("an", "ein")},
                    0.97, "light_on_off", {"area": area},
                )
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
        return self._match_pc(_PREFIX.sub("", normalized).strip())

    def _match_pc(self, text: str) -> FastPathMatch | None:
        if m := PC_SEARCH.match(text) or PC_GOOGLE.match(text):
            query = m["query"].strip()
            return FastPathMatch("pc.search_web", {"query": query}, 0.93, "pc_search", {"query": query})
        if m := PC_OPEN.match(text):
            target = _pc_target(m["target"])
            if target in PC_APPS:
                return FastPathMatch("pc.open_app", {"app": PC_APPS[target]}, 0.95, "pc_open_app", {"target": target})
            if target in PC_FOLDERS:
                return FastPathMatch("pc.open_folder", {"folder": PC_FOLDERS[target]}, 0.95, "pc_open_folder",
                                     {"target": target})
            url = PC_SITES.get(target) or (f"https://{target}" if _DOMAIN.match(target) else None)
            if url:
                return FastPathMatch("pc.open_url", {"url": url}, 0.94, "pc_open_url", {"target": target})
        return None
