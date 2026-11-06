"""Deutsche Zeitangaben für Timer, Erinnerungen und Termine.

„5 Minuten“, „eine halbe Stunde“, „anderthalb Stunden“ (Dauer) sowie „in 10 Minuten“, „morgen um halb acht“,
„heute Abend um 7“, „am Freitag um 10“, „am 3. Oktober um 14 Uhr“, „übermorgen“ (Zeitpunkt). ``parse_when``
liefert den Zeitpunkt, ob nur ein Tag genannt war (ganztägig) und den restlichen Text („Zahnarzt“).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

NUMBERS = {
    "null": 0, "ein": 1, "eine": 1, "einen": 1, "einer": 1, "eins": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5,
    "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10, "elf": 11, "zwölf": 12, "dreizehn": 13,
    "vierzehn": 14, "fünfzehn": 15, "sechzehn": 16, "siebzehn": 17, "achtzehn": 18, "neunzehn": 19, "zwanzig": 20,
    "dreißig": 30, "vierzig": 40, "fünfzig": 50, "sechzig": 60, "siebzig": 70, "achtzig": 80, "neunzig": 90,
    "hundert": 100, "anderthalb": 1.5, "eineinhalb": 1.5, "zweieinhalb": 2.5, "dreieinhalb": 3.5, "halbe": 0.5,
}
WEEKDAYS = {"montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4, "samstag": 5, "sonntag": 6}
MONTHS = {"januar": 1, "jänner": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5, "juni": 6, "juli": 7,
          "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12}

_NUM = r"\d+(?:[.,]\d+)?|[a-zäöüß]+"
_UNIT = r"sekunden?|sek|minuten?|min|stunden?|std|tagen?|tage"
DURATION = re.compile(rf"(?:(?P<half>eine\s+halbe)\s+(?:stunde|minute)|(?P<quarter>eine\s+(?:drei)?viertelstunde)|"
                      rf"(?P<num>{_NUM})\s+(?P<unit>{_UNIT})\b)", re.I)


def number(token: str) -> float | None:
    token = token.lower().strip()
    if re.fullmatch(r"\d+(?:[.,]\d+)?", token):
        return float(token.replace(",", "."))
    if token in NUMBERS:
        return float(NUMBERS[token])
    if m := re.fullmatch(r"([a-zäöüß]+?)und([a-zäöüß]+ig)", token):  # „fünfundzwanzig“
        ones, tens = NUMBERS.get(m[1].removesuffix("s") if m[1] == "eins" else m[1]), NUMBERS.get(m[2])
        if ones is not None and tens is not None:
            return float(ones + tens)
    return None


def parse_duration(text: str) -> int | None:
    """Summe aller Dauerangaben: „1 Stunde und 20 Minuten“ -> 4800. None, wenn keine gefunden."""
    total, found = 0.0, False
    for m in DURATION.finditer(text):
        found = True
        if m["half"]:
            total += 1800 if "stunde" in m[0].lower() else 30
        elif m["quarter"]:
            total += 2700 if "drei" in m[0].lower() else 900
        else:
            value = number(m["num"])
            if value is None:
                return None
            unit = m["unit"].lower()
            total += value * (1 if unit.startswith("sek") else 60 if unit.startswith("min") else
                              3600 if unit.startswith("st") else 86400)
    return int(round(total)) if found and total > 0 else None


@dataclass
class When:
    at: datetime
    all_day: bool  # nur ein Tag genannt („morgen“, „am Freitag“) – für Termine ganztägig
    rest: str  # Text ohne die Zeitangabe


_HOUR = r"\d{1,2}|[a-zäöüß]+"
_TIME = [  # (Muster, Funktion(match) -> (Stunde, Minute))
    (re.compile(r"\bum\s+(?P<h>\d{1,2})[:.](?P<m>\d{2})(?:\s*uhr)?\b"), lambda m: (int(m["h"]), int(m["m"]))),
    (re.compile(r"\bum\s+(?P<h>\d{1,2})\s*uhr\s+(?P<m>\d{1,2})\b"), lambda m: (int(m["h"]), int(m["m"]))),
    (re.compile(rf"\bum\s+halb\s+(?P<h>{_HOUR})\b"), lambda m: (_hour(m["h"]) - 1, 30)),
    (re.compile(rf"\bum\s+viertel\s+nach\s+(?P<h>{_HOUR})\b"), lambda m: (_hour(m["h"]), 15)),
    (re.compile(rf"\bum\s+(?:viertel\s+vor|dreiviertel)\s+(?P<h>{_HOUR})\b"), lambda m: (_hour(m["h"]) - 1, 45)),
    (re.compile(rf"\bum\s+(?P<h>{_HOUR})(?:\s*uhr)?\b"), lambda m: (_hour(m["h"]), 0)),
]
_PART = re.compile(r"\b(?P<part>früh|morgens|am morgen|(?:am\s+)?vormittags?|mittags?|(?:am\s+)?nachmittags?|"
                   r"abends|(?:am\s+)?abend|heute abend|nachts|nacht|in der nacht)\b")
_PART_HOUR = {"früh": 8, "morgens": 8, "am morgen": 8, "vormittag": 10, "mittag": 12, "nachmittag": 15, "abend": 19,
              "heute abend": 19, "nacht": 22}
_EVENING = ("nachmittag", "abend", "heute abend")


def _hour(token: str) -> int:
    value = number(token)
    return int(value) if value is not None and 0 <= value <= 24 else -99


def _cut(text: str, span: tuple[int, int]) -> str:
    return text[: span[0]] + " " + text[span[1]:]


def parse_when(text: str, now: datetime) -> When | None:
    """Zeitpunkt aus dem Text; ``now`` muss eine Zeitzone haben. None, wenn keine Zeitangabe vorkommt."""
    lower = text.lower()
    rest = text
    # „in 10 Minuten“, „in einer halben Stunde“
    if m := re.search(rf"\bin\s+(?P<d>(?:(?:{_NUM})\s+(?:{_UNIT})\b(?:\s+und\s+)?)+|einer\s+halben\s+stunde|"
                      rf"einer\s+(?:drei)?viertelstunde)", lower):
        seconds = parse_duration(re.sub(r"\beiner\b", "eine", m["d"]).replace("halben", "halbe"))
        if seconds:
            return When(now + timedelta(seconds=seconds), False, _clean(_cut(rest, m.span())))

    day: date | None = None
    if m := re.search(r"\bübermorgen\b", lower):
        day, rest, lower = now.date() + timedelta(days=2), _cut(rest, m.span()), _cut(lower, m.span())
    elif m := re.search(r"\bmorgen\b(?!s)", lower):
        day, rest, lower = now.date() + timedelta(days=1), _cut(rest, m.span()), _cut(lower, m.span())
    elif m := re.search(r"\bheute\b", lower):
        day, rest, lower = now.date(), _cut(rest, m.span()), _cut(lower, m.span())
    elif m := re.search(r"\b(?:am\s+)?(?P<d>\d{1,2})\.\s*(?:(?P<mon>[a-zäöü]+)|(?P<mn>\d{1,2})\.?)\s*(?P<y>\d{4})?",
                        lower):
        month = MONTHS.get(m["mon"] or "") or (int(m["mn"]) if m["mn"] else None)
        if month and 1 <= month <= 12:
            year = int(m["y"]) if m["y"] else now.year
            try:
                day = date(year, month, int(m["d"]))
            except ValueError:
                day = None
            if day and not m["y"] and day < now.date():
                day = date(year + 1, month, int(m["d"]))
            if day:
                rest, lower = _cut(rest, m.span()), _cut(lower, m.span())
    if day is None and (m := re.search(r"\b(?:am\s+|(?P<next>nächsten|kommenden|nächste)\s+|diesen\s+)?"
                                       r"(?P<wd>montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag)\b", lower)):
        ahead = (WEEKDAYS[m["wd"]] - now.weekday()) % 7
        if ahead == 0 and m["next"]:
            ahead = 7
        day = now.date() + timedelta(days=ahead)
        rest, lower = _cut(rest, m.span()), _cut(lower, m.span())

    hour = minute = None
    for pattern, extract in _TIME:
        if m := pattern.search(lower):
            hour, minute = extract(m)
            if 0 <= hour <= 24 and 0 <= minute < 60:
                rest, lower = _cut(rest, m.span()), _cut(lower, m.span())
                break
            hour = minute = None
    part = None
    if m := _PART.search(lower):
        part = re.sub(r"^am\s+|^in der\s+", "", m["part"])
        part = {"vormittags": "vormittag", "mittags": "mittag", "nachmittags": "nachmittag", "abends": "abend",
                "nachts": "nacht"}.get(part, part)
        rest, lower = _cut(rest, m.span()), _cut(lower, m.span())
        if part == "heute abend" and day is None:
            day = now.date()
    if hour is None and part is not None:
        hour, minute = _PART_HOUR[part], 0
    elif hour is not None and part in _EVENING and hour < 12:
        hour += 12
    elif hour is not None and part == "nacht" and 7 <= hour < 12:
        hour += 12

    if day is None and hour is None:
        return None
    if hour is None:
        return When(datetime.combine(day, time(9, 0), now.tzinfo), True, _clean(rest))
    hour %= 24
    if day is None:
        candidate = datetime.combine(now.date(), time(hour, minute), now.tzinfo)
        if part is None and 1 <= hour <= 11 and candidate <= now and candidate + timedelta(hours=12) > now:
            candidate += timedelta(hours=12)  # „um 3“ am Nachmittag: 15 Uhr, nicht morgen früh
        if candidate <= now:
            candidate += timedelta(days=1)
        return When(candidate, False, _clean(rest))
    if part is None and 1 <= hour <= 6:
        hour += 12  # „am Freitag um 3“: nachmittags – außer „früh/morgens“ ist genannt
    return When(datetime.combine(day, time(hour, minute), now.tzinfo), False, _clean(rest))


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip(" ,.:;-")
    return re.sub(r"\s+([,.:;])", r"\1", text)
