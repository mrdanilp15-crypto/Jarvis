"""Timer und Erinnerungen: Planer, Speicher, Meldungen, Capabilities."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from jarvis.errors import JarvisError
from jarvis.style import JarvisStyle, PlainStyle
from jarvis.timers import AlarmScheduler, Notifier, register_timer_capabilities
from jarvis.tools import InvocationContext, ToolRegistry

BERLIN = ZoneInfo("Europe/Berlin")
CTX = InvocationContext(correlation_id="c1", actor="user:owner", session_id="s1")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now


def make(tmp_path=None):
    clock, fired = Clock(), []

    async def notify(alarm):
        fired.append(alarm)

    scheduler = AlarmScheduler(notify=notify, path=tmp_path / "alarms.json" if tmp_path else None, clock=clock)
    return scheduler, clock, fired


def test_alarms_fire_in_order_and_only_once():
    scheduler, clock, fired = make()
    scheduler.add("timer", clock.now + timedelta(minutes=5), "", "user:owner", duration_s=300)
    scheduler.add("reminder", clock.now + timedelta(minutes=2), "Müll", "user:owner")
    assert asyncio.run(scheduler.fire_due()) == []
    clock.now += timedelta(minutes=10)
    asyncio.run(scheduler.fire_due())
    assert [a.label for a in fired] == ["Müll", ""] and not scheduler.alarms
    asyncio.run(scheduler.fire_due())
    assert len(fired) == 2


def test_cancel_by_label_all_or_next():
    scheduler, clock, _ = make()
    for minutes, label in ((8, "Nudeln"), (3, "Tee"), (20, "")):
        scheduler.add("timer", clock.now + timedelta(minutes=minutes), label, "user:owner")
    assert [a.label for a in scheduler.cancel(actor="user:owner", label="nudel")] == ["Nudeln"]
    assert [a.label for a in scheduler.cancel(actor="user:owner")] == ["Tee"]  # der nächste fällige
    assert scheduler.cancel(actor="user:other", everything=True) == []  # fremde Timer bleiben
    assert len(scheduler.cancel(actor="user:owner", everything=True)) == 1 and not scheduler.alarms


def test_alarms_survive_restarts_and_late_ones_are_marked(tmp_path):
    scheduler, clock, _ = make(tmp_path)
    scheduler.add("reminder", clock.now + timedelta(minutes=5), "Müll", "user:owner")
    scheduler.add("timer", clock.now + timedelta(hours=2), "Braten", "user:owner", duration_s=7200)
    assert len(json.loads((tmp_path / "alarms.json").read_text(encoding="utf-8"))) == 2

    later, _, fired = make(tmp_path)  # Neustart eine Stunde später
    later.clock.now = clock.now + timedelta(hours=1)
    asyncio.run(later.fire_due())
    assert [(a.label, a.late) for a in fired] == [("Müll", True)]
    assert [a.label for a in later.active()] == ["Braten"]


def test_broken_store_starts_empty(tmp_path):
    (tmp_path / "alarms.json").write_text("{kaputt", encoding="utf-8")
    scheduler, _, _ = make(tmp_path)
    assert scheduler.alarms == {}


def test_run_loop_fires_due_alarms():
    async def scenario():
        fired = []

        async def notify(alarm):
            fired.append(alarm.label)

        scheduler = AlarmScheduler(notify=notify)
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0)
        scheduler.add("timer", datetime.now(UTC) + timedelta(milliseconds=100), "Ei", "user:owner")
        await asyncio.sleep(0.4)
        task.cancel()
        return fired

    assert asyncio.run(scenario()) == ["Ei"]


def test_timer_capabilities():
    scheduler, clock, _ = make()
    registry = ToolRegistry()
    register_timer_capabilities(registry, scheduler, BERLIN)

    def call(name, args):
        assert not registry.get(name).validate(args)
        return asyncio.run(registry.get(name).handler(args, CTX))

    started = call("timer.start", {"duration_s": 480, "label": "Nudeln"})
    assert started["due"] == "2026-09-28T14:08+02:00" and started["now"] == "2026-09-28T14:00+02:00"
    reminder = call("reminder.create", {"text": "Müll", "at": "2026-09-28T18:00"})  # ohne Zone: Ortszeit
    assert reminder["due"] == "2026-09-28T18:00+02:00"
    with pytest.raises(JarvisError) as past:
        call("reminder.create", {"text": "Müll", "at": "2026-09-28T09:00:00+02:00"})
    assert "Vergangenheit" in past.value.user_message
    listing = call("timer.list", {})
    assert [(a["kind"], a["label"], a["remaining_s"]) for a in listing["alarms"]] == [
        ("timer", "Nudeln", 480), ("reminder", "Müll", 4 * 3600)]
    assert call("timer.cancel", {"kind": "reminder"})["cancelled"][0]["label"] == "Müll"
    assert call("timer.cancel", {"label": "Tee"})["cancelled"] == []


@pytest.mark.parametrize("style, kind, label, duration, late, expected", [
    (JarvisStyle(), "timer", "", 300, False, "Sir, Ihr Timer über 5 Minuten ist abgelaufen."),
    (JarvisStyle(), "timer", "Nudeln", 480, False, "Sir, der Timer „Nudeln“ ist abgelaufen."),
    (JarvisStyle(), "reminder", "den Müll rauszubringen", None, False,
     "Sir, Sie wollten erinnert werden: den Müll rauszubringen."),
    (JarvisStyle(), "event", "15|Zahnarzt", None, False, "Sir, in 15 Minuten: Zahnarzt."),
    (JarvisStyle(), "reminder", "Müll", None, True,
     "Während ich nicht erreichbar war, wurde fällig: Sir, Sie wollten erinnert werden: Müll."),
    (PlainStyle(), "timer", "", 60, False, "Ihr Timer über 1 Minute ist abgelaufen."),
])
def test_alarm_texts(style, kind, label, duration, late, expected):
    assert style.alarm_text(kind, label, duration_s=duration, late=late) == expected


def test_wake_up_text():
    due = datetime(2026, 9, 29, 7, 0, tzinfo=BERLIN)
    assert JarvisStyle().alarm_text("reminder", "Aufstehen", due=due) == (
        "Guten Morgen, Sir. Es ist 7 Uhr – Zeit aufzustehen.")


def test_notifier_delivers_or_keeps_messages():
    async def scenario():
        notifier, received = Notifier(), []

        async def send(message):
            received.append(message)

        assert not await notifier.send("user:owner", {"text": "verpasst"})  # kein Fenster offen
        notifier.attach("user:owner", send)
        assert notifier.drain("user:owner") == [{"text": "verpasst"}]
        assert await notifier.send("user:owner", {"text": "live"})

        async def broken(message):
            raise RuntimeError("Verbindung weg")

        notifier.attach("user:owner", broken)
        await notifier.send("user:owner", {"text": "noch einmal"})
        notifier.detach("user:owner", send)
        return received, notifier.listeners["user:owner"]

    received, listeners = asyncio.run(scenario())
    assert [m["text"] for m in received] == ["live", "noch einmal"] and listeners == []
