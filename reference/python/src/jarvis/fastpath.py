"""Fast-Path-NLU: deterministische Grammatiken für häufige Befehle – ohne LLM, < 300 ms.

Im Betrieb werden die Muster aus Home-Assistant-Intents (hassil-kompatibel) und dem Entitätsregister
generiert; dieses Modul zeigt das Prinzip an Licht-, Timer- und Bestätigungsbefehlen.
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
        return None
