import asyncio
import json
from datetime import datetime

import pytest

from jarvis.automation import AutomationEngine, EvalContext, evaluate_condition, parse_duration, trigger_matches
from jarvis.events import CloudEvent
from jarvis.orchestrator import Orchestrator

from conftest import REPO

EXAMPLES = REPO / "schemas" / "examples"


def load(name: str) -> dict:
    return json.loads((EXAMPLES / f"automation.{name}.json").read_text(encoding="utf-8"))


def ctx(hhmm="23:15", presence=None, mode="normal", states=None) -> EvalContext:
    return EvalContext(states=states or {}, now=datetime.strptime(f"2026-09-26 {hhmm}", "%Y-%m-%d %H:%M"),
                       presence=presence or {}, mode=mode)


def test_nested_conditions():
    cond = load("evening-arrival")["conditions"]
    assert all(evaluate_condition(c, ctx("23:15")) for c in cond)
    assert all(evaluate_condition(c, ctx("04:30")) for c in cond)
    assert not all(evaluate_condition(c, ctx("19:00")) for c in cond)
    assert not all(evaluate_condition(c, ctx("23:15", mode="party")) for c in cond)


def test_numeric_state_ignores_unavailable():
    cond = {"type": "numeric_state", "entity_id": "sensor.co2", "above": 1000}
    assert evaluate_condition(cond, ctx(states={"sensor.co2": {"state": "1200"}}))
    assert not evaluate_condition(cond, ctx(states={"sensor.co2": {"state": "unavailable"}}))
    assert not evaluate_condition(cond, ctx(states={}))


def test_presence_anyone():
    home = {"type": "presence", "person": "anyone", "state": "home"}
    empty = {"type": "presence", "person": "anyone", "state": "not_home"}
    assert evaluate_condition(home, ctx(presence={"alex": True, "sam": False}))
    assert evaluate_condition(empty, ctx(presence={"alex": False, "sam": False}))


def state_event(entity, old, new):
    return CloudEvent(type="jarvis.sensor.state_changed", source="/test", trust="system",
                      data={"entity_id": entity, "old_state": {"state": old}, "new_state": {"state": new}})


def test_state_trigger_from_to():
    trigger = {"type": "state", "entity_id": "binary_sensor.tuer", "from": "off", "to": "on"}
    assert trigger_matches(trigger, state_event("binary_sensor.tuer", "off", "on"))
    assert not trigger_matches(trigger, state_event("binary_sensor.tuer", "on", "off"))
    assert not trigger_matches(trigger, state_event("binary_sensor.tuer", "on", "on"))


def test_event_trigger_with_match():
    trigger = load("rain-window-warning")["triggers"][0]
    event = CloudEvent(type="jarvis.info.weather_alert", source="/info", trust="system", data={"kind": "rain_soon"})
    assert trigger_matches(trigger, event)
    other = CloudEvent(type="jarvis.info.weather_alert", source="/info", trust="system", data={"kind": "storm"})
    assert not trigger_matches(trigger, other)


def test_choose_default_branch_notifies_push(orchestrator: Orchestrator):
    notes = []

    async def notifier(message, channels, target, priority):
        notes.append((message, channels, priority))

    engine = AutomationEngine([load("rain-window-warning")], gateway=orchestrator,
                              owner_roles={"user:alex": "adult"}, notifier=notifier)
    event = CloudEvent(type="jarvis.info.weather_alert", source="/info", trust="system", data={"kind": "rain_soon"})
    traces = asyncio.run(engine.on_event(event, ctx(states={"binary_sensor.fenster_bad": {"state": "on"}},
                                                    presence={"alex": False})))
    assert traces[0].status == "succeeded"
    assert notes == [("Fenster Bad offen, Regen in Kürze. Niemand ist zu Hause.", ["push"], "high")]


def test_capability_actions_go_through_policy(orchestrator: Orchestrator, home, audit):
    automation = load("evening-arrival")
    engine = AutomationEngine([automation], gateway=orchestrator, owner_roles={"user:alex": "adult"})
    event = CloudEvent(type="jarvis.presence.changed", source="/presence", trust="system",
                       data={"person": "alex", "event": "arrives", "zone": "home"})
    home.states["light.flur"] = {"state": "off", "attributes": {}}
    traces = asyncio.run(engine.on_event(event, ctx("23:15")))
    assert traces[0].status == "succeeded"
    assert home.state("light.flur")["state"] == "on"
    decision = next(e for e in audit.entries if e["event"] == "policy.decision")
    assert decision["actor"] == "automation:aut_evening_arrival"
    assert decision["via"] == "automation"


def test_unapproved_automation_does_not_run(orchestrator: Orchestrator, home):
    automation = {**load("evening-arrival"), "approval": {"status": "pending"}}
    engine = AutomationEngine([automation], gateway=orchestrator, owner_roles={"user:alex": "adult"})
    event = CloudEvent(type="jarvis.presence.changed", source="/presence", trust="system",
                       data={"person": "alex", "event": "arrives"})
    assert asyncio.run(engine.on_event(event, ctx("23:15"))) == []


def test_dry_run_has_no_side_effects(orchestrator: Orchestrator, home):
    automation = load("evening-arrival")
    home.states["light.flur"] = {"state": "off", "attributes": {}}
    engine = AutomationEngine([automation], gateway=orchestrator, owner_roles={"user:alex": "adult"})
    trace = asyncio.run(engine.run(automation, automation["triggers"][0], ctx("23:15"), dry_run=True))
    assert trace.status == "succeeded" and trace.dry_run
    assert home.state("light.flur")["state"] == "off"


def test_llm_step_output_is_templated(orchestrator: Orchestrator):
    notes = []

    async def notifier(message, *_):
        notes.append(message)

    async def llm_step(prompt, route, allow_tools):
        assert route == "local_llm" and "events.query" in allow_tools
        return "Ruhige Nacht, keine Auffälligkeiten."

    automation = load("morning-digest")
    engine = AutomationEngine([automation], gateway=orchestrator, owner_roles={}, notifier=notifier,
                              llm_step=llm_step)
    asyncio.run(engine.run(automation, automation["triggers"][0], ctx("06:45")))
    assert notes == ["Ruhige Nacht, keine Auffälligkeiten."]


@pytest.mark.parametrize(("text", "seconds"), [("45s", 45), ("10m", 600), ("1h30m", 5400)])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds
