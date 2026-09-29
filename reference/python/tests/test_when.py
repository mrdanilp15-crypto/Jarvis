"""Deutsche Zeitangaben für Timer, Erinnerungen und Termine."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from jarvis.when import parse_duration, parse_when

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 9, 28, 14, 20, tzinfo=BERLIN)  # Montag, 14:20


@pytest.mark.parametrize("text, seconds", [
    ("5 Minuten", 300), ("eine halbe Stunde", 1800), ("anderthalb Stunden", 5400),
    ("1 Stunde und 20 Minuten", 4800), ("90 Sekunden", 90), ("zwei Stunden", 7200),
    ("fünfundzwanzig Minuten", 1500), ("eine Viertelstunde", 900), ("eine Dreiviertelstunde", 2700),
    ("10 min", 600), ("1,5 Stunden", 5400), ("Nudeln", None), ("zwölf Tage", 12 * 86400),
])
def test_durations(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text, expected, all_day, rest", [
    ("in 10 Minuten den Müll rausbringen", "2026-09-28 14:30", False, "den Müll rausbringen"),
    ("in einer halben Stunde Pizza", "2026-09-28 14:50", False, "Pizza"),
    ("morgen um halb acht Zahnarzt", "2026-09-29 07:30", False, "Zahnarzt"),
    ("heute Abend um 7 Kino", "2026-09-28 19:00", False, "Kino"),
    ("heute Abend Kino", "2026-09-28 19:00", False, "Kino"),
    ("morgen Nachmittag Sport", "2026-09-29 15:00", False, "Sport"),
    ("Zahnarzt morgen früh", "2026-09-29 08:00", False, "Zahnarzt"),
    ("am Freitag um 10 Meeting", "2026-10-02 10:00", False, "Meeting"),
    ("am Freitag um 3 Meeting", "2026-10-02 15:00", False, "Meeting"),  # 1–6 Uhr ohne „früh“: nachmittags
    ("morgen früh um 6 Flughafen", "2026-09-29 06:00", False, "Flughafen"),
    ("am 3. Oktober um 14 Uhr Oma besuchen", "2026-10-03 14:00", False, "Oma besuchen"),
    ("am 03.10. um 15.30 Uhr Oma", "2026-10-03 15:30", False, "Oma"),
    ("am 1. Januar Neujahr", "2027-01-01 09:00", True, "Neujahr"),  # vergangenes Datum: nächstes Jahr
    ("übermorgen Friseur", "2026-09-30 09:00", True, "Friseur"),
    ("nächsten Montag Steuer", "2026-10-05 09:00", True, "Steuer"),
    ("um 15:30 Anruf bei Max", "2026-09-28 15:30", False, "Anruf bei Max"),
    ("um 3 Wäsche", "2026-09-28 15:00", False, "Wäsche"),  # heute, nächstes Vorkommen
    ("um viertel nach drei Tee", "2026-09-28 15:15", False, "Tee"),
    ("um 10 Uhr Bäcker", "2026-09-28 22:00", False, "Bäcker"),  # 10 Uhr ist vorbei: 22 Uhr heute
    ("um 20 Uhr Film", "2026-09-28 20:00", False, "Film"),
])
def test_points_in_time(text, expected, all_day, rest):
    when = parse_when(text, NOW)
    assert when is not None
    assert (when.at.strftime("%Y-%m-%d %H:%M"), when.all_day, when.rest) == (expected, all_day, rest)
    assert when.at.tzinfo is not None


def test_no_time_mentioned():
    assert parse_when("Zahnarzt", NOW) is None
