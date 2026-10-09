"""Mehrere Räume: Timer melden sich im Raum, Durchsagen, und bei mehreren Geräten antwortet nur das nächste."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from jarvis.api import Container, create_app
from jarvis.context import Situation
from jarvis.events import InMemoryEventBus
from jarvis.fastpath import FastPath
from jarvis.llm.router import ModelRouter
from jarvis.policy import Principal
from jarvis.rooms import WakeArbiter, register_room_capabilities, resolve_room
from jarvis.style import JarvisStyle
from jarvis.testing import ScriptedProvider
from jarvis.timers import AlarmScheduler, Notifier, register_timer_capabilities
from jarvis.tools import InvocationContext, ToolRegistry

ROOMS = {"kueche": "Küche", "wohnzimmer": "Wohnzimmer", "kinderzimmer": "Kinderzimmer"}
DANIEL = Principal(actor="user:owner", role="adult", trust="trusted_user", name="Daniel", area="wohnzimmer")
TABLET = Principal(actor="device:kuechen-tablet", role="adult", trust="household", area="kueche")


@pytest.mark.parametrize("text, arguments, prep", [
    ("Sag in der Küche, dass das Essen fertig ist.", {"text": "Das Essen ist fertig", "room": "in der Küche"}, "in der"),
    ("Jarvis, Durchsage an alle: Abfahrt in fünf Minuten", {"text": "Abfahrt in fünf Minuten"}, "an"),
    ("Sag allen Bescheid, dass wir gleich essen", {"text": "Wir essen gleich"}, ""),
    ("Mach eine Durchsage im Kinderzimmer: Zähne putzen", {"text": "Zähne putzen", "room": "im Kinderzimmer"}, "im"),
])
def test_announcement_grammar(text, arguments, prep):
    found = FastPath({}).match(text)
    assert (found.capability, found.arguments, found.slots) == ("message.announce", arguments, {"prep": prep})


@pytest.mark.parametrize("text", ["Sag mir, wie spät es ist", "Sag Bescheid, wenn der Timer fertig ist", "Durchsage"])
def test_no_announcement(text):
    found = FastPath({}).match(text)
    assert found is None or found.capability != "message.announce"


def test_room_names():
    assert resolve_room("in der Küche", ROOMS) == "kueche"
    assert resolve_room("im Wohnzimmer", ROOMS) == "wohnzimmer"
    assert resolve_room("kueche", ROOMS) == "kueche"
    assert resolve_room("im Keller", ROOMS) is None


class Inbox:
    def __init__(self):
        self.messages = []

    async def __call__(self, message):
        self.messages.append(message)


def test_timer_rings_where_it_was_set():
    now = datetime(2026, 10, 9, 18, 0, tzinfo=UTC)
    notifier = Notifier(clock=lambda: now)
    pc, tablet, second_kitchen_screen = Inbox(), Inbox(), Inbox()
    notifier.attach("user:owner", pc, area="wohnzimmer")
    notifier.attach("device:kuechen-tablet", tablet, area="kueche")
    notifier.attach("device:kuechen-handy", second_kitchen_screen, area="kueche")

    async def ring(alarm):
        await notifier.send(alarm.actor, {"type": "notification", "id": alarm.id}, area=alarm.area)

    clock = {"now": now}
    scheduler = AlarmScheduler(notify=ring, clock=lambda: clock["now"])
    registry = ToolRegistry()
    register_timer_capabilities(registry, scheduler, UTC)
    kitchen = InvocationContext(correlation_id="c", actor=TABLET.actor, area="kueche")
    asyncio.run(registry.get("timer.start").handler({"duration_s": 600, "label": "Nudeln"}, kitchen))
    # „Wie lange läuft der Timer?“ – auch vom zweiten Gerät in der Küche, nicht aber vom Wohnzimmer aus
    other_kitchen = InvocationContext(correlation_id="c", actor="device:kuechen-handy", area="kueche")
    assert len(asyncio.run(registry.get("timer.list").handler({}, other_kitchen))["alarms"]) == 1
    living = InvocationContext(correlation_id="c", actor="user:owner", area="wohnzimmer")
    assert asyncio.run(registry.get("timer.list").handler({}, living))["alarms"] == []
    clock["now"] = now + timedelta(minutes=11)
    asyncio.run(scheduler.fire_due())
    assert len(tablet.messages) == len(second_kitchen_screen.messages) == 1 and pc.messages == []


def test_announcements_reach_the_room_or_everyone():
    notifier = Notifier()
    pc, tablet, kids = Inbox(), Inbox(), Inbox()
    notifier.attach("user:owner", pc, area="wohnzimmer")
    notifier.attach("device:kuechen-tablet", tablet, area="kueche")
    notifier.attach("device:kinder-tablet", kids, area="kinderzimmer")
    registry = ToolRegistry()
    register_room_capabilities(registry, notifier, lambda: ROOMS)
    announce = registry.get("message.announce").handler
    me = InvocationContext(correlation_id="c", actor="user:owner", area="wohnzimmer")

    result = asyncio.run(announce({"text": "Das Essen ist fertig", "room": "in der Küche"}, me))
    assert result == {"delivered": 1, "area": "kueche", "room": "Küche", "everywhere": False}
    assert tablet.messages == [{"type": "notification", "kind": "announcement", "text": "Durchsage: Das Essen ist fertig."}]
    assert kids.messages == [] and pc.messages == []

    result = asyncio.run(announce({"text": "Abfahrt in fünf Minuten!"}, me))
    assert result["delivered"] == 2 and pc.messages == []  # alle außer dem, der die Durchsage macht
    assert kids.messages[-1]["text"] == "Durchsage: Abfahrt in fünf Minuten."


def test_unknown_room_and_empty_room():
    notifier = Notifier()
    registry = ToolRegistry()
    register_room_capabilities(registry, notifier, lambda: {"kueche": "Küche"})
    announce = registry.get("message.announce").handler
    me = InvocationContext(correlation_id="c", actor="user:owner")
    from jarvis.errors import JarvisError

    with pytest.raises(JarvisError) as error:
        asyncio.run(announce({"text": "Hallo", "room": "im Keller"}, me))
    assert error.value.user_message == "Einen Raum „Keller“ kenne ich nicht."
    assert asyncio.run(announce({"text": "Hallo", "room": "in der Küche"}, me))["delivered"] == 0


@pytest.mark.parametrize("result, slots, text", [
    ({"delivered": 1, "room": "Küche", "everywhere": False}, {"prep": "in der"},
     "Sehr wohl. Die Durchsage ist in der Küche angekommen."),
    ({"delivered": 1, "room": "Küche", "everywhere": False}, {"prep": "in die"},
     "Sehr wohl. Die Durchsage ist in der Küche angekommen."),
    ({"delivered": 0, "room": "Bad", "everywhere": False}, {"prep": "im"}, "Im Bad ist gerade kein Gerät mit JARVIS verbunden."),
    ({"delivered": 2, "room": None, "everywhere": True}, {"prep": ""}, "Sehr wohl. Die Durchsage läuft auf 2 Geräten."),
])
def test_announcement_replies(result, slots, text):
    style = JarvisStyle()
    assert style.finalize(" ".join(filter(None, style._success("message.announce", {}, result, slots)))) == text


# -------------------------------------------------------------------------------------------- Wer antwortet?
def run(*claims, listening=("pc", "tablet"), delays=None):
    async def go():
        arbiter = WakeArbiter()
        for client in listening:
            arbiter.listen(client, True)

        async def one(client, score, delay):
            await asyncio.sleep(delay)
            return await arbiter.claim(client, score)

        return await asyncio.gather(*(one(c, s, (delays or {}).get(c, 0)) for c, s in claims))

    return asyncio.run(go())


def test_single_device_answers_at_once():
    started = asyncio.run(asyncio.sleep(0)) or datetime.now()
    assert run(("pc", 12.0), listening=("pc",)) == [True]
    assert (datetime.now() - started).total_seconds() < 0.2  # keine Wartezeit


def test_nearest_device_wins():
    assert run(("pc", 14.5), ("tablet", 31.0), delays={"tablet": 0.15}) == [False, True]
    assert run(("pc", 40.0), ("tablet", 22.0)) == [True, False]


def test_device_without_measurement_loses_and_silent_devices_do_not_block():
    assert run(("pc", None), ("tablet", 9.0)) == [False, True]
    started = datetime.now()
    assert run(("tablet", 9.0), listening=("pc", "tablet", "phone")) == [True]  # „phone“ hat nichts gehört
    assert (datetime.now() - started).total_seconds() < 1.0


def test_late_claim_of_the_same_words_is_refused():
    assert run(("pc", 20.0), ("tablet", 25.0), delays={"tablet": 0.9}) == [True, False]


def make_client(orchestrator):
    notifier = Notifier()
    register_room_capabilities(orchestrator.registry, notifier, lambda: ROOMS)
    orchestrator.style = JarvisStyle()
    container = Container(orchestrator=orchestrator, bus=InMemoryEventBus(),
                          router=ModelRouter(local=ScriptedProvider([]), cloud=None),
                          tokens={"pc": DANIEL, "tablet": TABLET}, webhook_secrets={},
                          situation=lambda who, ch: Situation(now=datetime(2026, 10, 9, 18, 0)), notifier=notifier)
    container.wake = WakeArbiter()
    return TestClient(create_app(container))


def test_stream_announcement_and_wake_vote(orchestrator):
    client = make_client(orchestrator)
    with client.websocket_connect("/v1/stream?token=pc") as pc, client.websocket_connect("/v1/stream?token=tablet") as tab:
        pc.send_json({"type": "input.text", "text": "Sag in der Küche, dass das Essen fertig ist",
                      "session_id": "s1"})
        final = next(m for m in iter(pc.receive_json, None) if m["type"] in ("output.final", "error"))
        assert tab.receive_json() == {"type": "notification", "kind": "announcement",
                                      "text": "Durchsage: Das Essen ist fertig."}
        assert final["text"] == "Sehr wohl. Die Durchsage ist in der Küche angekommen."

        for ws in (pc, tab):
            ws.send_json({"type": "wake.listen", "on": True})
        pc.send_json({"type": "wake.claim", "ref": "a", "score": 11.0})
        tab.send_json({"type": "wake.claim", "ref": "b", "score": 27.5})
        assert pc.receive_json() == {"type": "wake.result", "ref": "a", "granted": False}
        assert tab.receive_json() == {"type": "wake.result", "ref": "b", "granted": True}
