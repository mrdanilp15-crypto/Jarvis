"""Lernende Routinen: Gewohnheiten erkennen, nur mit Zustimmung ausführen, Uhrzeit gleitet mit."""

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from jarvis.api import Container, create_app
from jarvis.context import Situation
from jarvis.events import InMemoryEventBus
from jarvis.llm.router import ModelRouter
from jarvis.patterns import PatternLearner, action_call, describe, find_patterns
from jarvis.policy import Principal
from jarvis.testing import ScriptedProvider

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 10, 9, 20, 0, tzinfo=BERLIN)  # Freitag
DANIEL = Principal(actor="user:owner", role="adult", trust="trusted_user", name="Daniel")


def weekday_mornings(n=12, minute=45, jitter=(0, 4, -6, 9, -3)):
    events, day = [], NOW.date() - timedelta(days=1)
    while len(events) < n:
        if day.weekday() < 5:
            offset = jitter[len(events) % len(jitter)]
            moment = datetime(day.year, day.month, day.day, 6, minute, tzinfo=BERLIN) + timedelta(minutes=offset)
            events.append(("light.kueche", "on", moment))
        day -= timedelta(days=1)
    return events


def test_weekday_habit_is_found():
    found = find_patterns(weekday_mornings(), NOW, {"light.kueche": "Küchenlicht"})
    assert len(found) == 1
    habit = found[0]
    assert (habit.entity_id, habit.action, habit.daytype, habit.clock) == ("light.kueche", "on", "weekday", "6:45")
    assert habit.days == 12 and habit.of_days == 15
    assert describe(habit) == "Küchenlicht werktags gegen 6:45 Uhr einschalten (an 12 von 15 Tagen beobachtet)"


def test_rare_or_scattered_use_is_no_habit():
    scattered = [("light.bad", "on", NOW - timedelta(days=d, hours=d * 3)) for d in range(1, 9)]
    assert find_patterns(weekday_mornings(n=4) + scattered, NOW) == []  # 4 Tage zu wenig, Rest verstreut


def test_learning_from_home_assistant_events_and_consent(tmp_path):
    clock = {"now": NOW - timedelta(days=22)}
    learner = PatternLearner(tmp_path / "patterns.db", tz=BERLIN, clock=lambda: clock["now"])
    for _, action, moment in reversed(weekday_mornings()):
        clock["now"] = moment
        learner.observe({"entity_id": "light.kueche", "old_state": {"state": "off"}, "new_state": {"state": action}})
        learner.observe({"entity_id": "light.kueche", "old_state": {"state": "on"}, "new_state": {"state": "on"}})
        learner.observe({"entity_id": "sensor.temp", "old_state": {"state": "20"}, "new_state": {"state": "21"}})
    clock["now"] = NOW
    fresh = learner.analyse({"light.kueche": "Küchenlicht"})
    assert [s.id for s in fresh] == ["light.kueche|on|weekday"]
    assert learner.analyse() == []  # einmal angekündigt reicht
    monday = datetime(2026, 10, 12, 6, 45, tzinfo=BERLIN)
    assert learner.due(monday) == []  # ohne Zustimmung passiert nichts
    learner.decide("light.kueche|on|weekday", True)
    due = learner.due(monday)
    assert [s.id for s in due] == ["light.kueche|on|weekday"]
    assert action_call(due[0]) == ("home.set_light", {"entity_ids": ["light.kueche"], "on": True})
    clock["now"] = monday
    learner.mark_run(due[0])
    assert learner.due(monday + timedelta(minutes=1)) == []  # einmal pro Tag
    # eigene Schaltung zählt nicht als Gewohnheit
    learner.observe({"entity_id": "light.kueche", "old_state": {"state": "off"}, "new_state": {"state": "on"}})
    assert len(learner.events()) == 12


def test_accepted_routine_follows_later_habits(tmp_path):
    clock = {"now": NOW}
    learner = PatternLearner(tmp_path / "patterns.db", tz=BERLIN, clock=lambda: clock["now"])
    for entity, action, moment in weekday_mornings():
        learner._db.execute("INSERT INTO usage VALUES (?, ?, ?)", (entity, action, moment.isoformat()))
    learner.analyse()
    learner.decide("light.kueche|on|weekday", True)
    learner._db.execute("DELETE FROM usage")
    for entity, action, moment in weekday_mornings(minute=15):  # neuerdings eine halbe Stunde früher
        learner._db.execute("INSERT INTO usage VALUES (?, ?, ?)", (entity, action, moment.isoformat()))
    learner.analyse()
    [routine] = learner.suggestions()
    assert routine.status == "accepted" and routine.clock == "6:15"


def test_suggestions_api(tmp_path):
    learner = PatternLearner(tmp_path / "patterns.db", tz=BERLIN, clock=lambda: NOW)
    for entity, action, moment in weekday_mornings():
        learner._db.execute("INSERT INTO usage VALUES (?, ?, ?)", (entity, action, moment.isoformat()))
    learner.analyse({"light.kueche": "Küchenlicht"})

    class Style:
        name, version = "jarvis", "test"

        def error_message(self, message):
            return message

    container = Container(orchestrator=type("O", (), {"style": Style()})(), bus=InMemoryEventBus(),
                          router=ModelRouter(local=ScriptedProvider([]), cloud=None), tokens={"a": DANIEL},
                          webhook_secrets={}, situation=lambda who, ch: Situation(now=NOW), learner=learner)
    client = TestClient(create_app(container))
    headers = {"Authorization": "Bearer a"}
    listed = client.get("/v1/automations/suggestions", headers=headers).json()["suggestions"]
    assert listed[0]["text"].startswith("Küchenlicht werktags gegen 6:45 Uhr") and listed[0]["status"] == "new"
    decided = client.post("/v1/automations/suggestions/light.kueche|on|weekday", headers=headers,
                          json={"accept": False}).json()
    assert decided["suggestion"]["status"] == "rejected"
    assert client.get("/v1/automations/suggestions", headers=headers).json()["suggestions"] == []
    assert asyncio.run(asyncio.sleep(0)) is None
