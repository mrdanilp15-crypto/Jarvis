"""Kalender: ICS lesen (Zeitzonen, ganztägig, Wiederholungen), eigene Termine, Capabilities, Tagesplan."""

import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from jarvis.agenda import CalendarService, day_plan_entries, expand, parse_ics, register_calendar_capabilities
from jarvis.style import JarvisStyle
from jarvis.timers import AlarmScheduler
from jarvis.tools import InvocationContext, ToolRegistry

BERLIN = ZoneInfo("Europe/Berlin")
CTX = InvocationContext(correlation_id="c1", actor="user:owner", session_id="s1")
NOW = datetime(2026, 9, 28, 9, 0, tzinfo=BERLIN)  # Montag

ICS = """BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:zahnarzt\r
SUMMARY:Zahnarzt\\, Kontrolle\r
LOCATION:Praxis Dr. Weiß\r
DTSTART;TZID=Europe/Berlin:20260929T100000\r
DTEND;TZID=Europe/Berlin:20260929T103000\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:utc\r
SUMMARY:Call mit\r
  New York\r
DTSTART:20260929T140000Z\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:urlaub\r
SUMMARY:Urlaub\r
DTSTART;VALUE=DATE:20261001\r
DTEND;VALUE=DATE:20261003\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:sport\r
SUMMARY:Sport\r
DTSTART;TZID=Europe/Berlin:20260914T180000\r
DTEND;TZID=Europe/Berlin:20260914T190000\r
RRULE:FREQ=WEEKLY;BYDAY=MO,TH;COUNT=8\r
EXDATE;TZID=Europe/Berlin:20261001T180000\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:sport\r
RECURRENCE-ID;TZID=Europe/Berlin:20260928T180000\r
SUMMARY:Sport (verschoben)\r
DTSTART;TZID=Europe/Berlin:20260928T190000\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:abgesagt\r
SUMMARY:Abgesagt\r
STATUS:CANCELLED\r
DTSTART:20260929T080000Z\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:monat\r
SUMMARY:Miete\r
DTSTART;VALUE=DATE:20260801\r
RRULE:FREQ=MONTHLY\r
END:VEVENT\r
END:VCALENDAR\r
"""


def day(events, when):
    start = datetime.combine(when, datetime.min.time(), BERLIN)
    return sorted((e.start.strftime("%H:%M") if not e.all_day else "ganztägig", e.title)
                  for e in expand(events, start, start + timedelta(days=1), "privat"))


def test_ics_parsing_and_expansion():
    events = parse_ics(ICS, BERLIN)
    assert all(e.get("title") != "Abgesagt" for e in events)  # abgesagte Termine fallen weg
    assert day(events, date(2026, 9, 29)) == [("10:00", "Zahnarzt, Kontrolle"), ("16:00", "Call mit New York")]
    assert day(events, date(2026, 9, 28)) == [("19:00", "Sport (verschoben)")]  # Ersatz statt Original
    assert day(events, date(2026, 10, 1)) == [("ganztägig", "Miete"), ("ganztägig", "Urlaub")]  # EXDATE: kein Sport
    assert day(events, date(2026, 10, 5)) == [("18:00", "Sport")]
    assert day(events, date(2026, 10, 12)) == []  # COUNT=8 ist erreicht
    assert day(events, date(2026, 11, 1)) == [("ganztägig", "Miete")]


def make_calendar(tmp_path, ics=None, handler=None):
    fired = []

    async def notify(alarm):
        fired.append(alarm)

    scheduler = AlarmScheduler(notify=notify, clock=lambda: NOW)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler)) if handler else None
    calendar = CalendarService(tz=BERLIN, path=tmp_path / "calendar.json", ics=ics, scheduler=scheduler,
                               client=client, clock=lambda: NOW)
    return calendar, scheduler


def test_own_events_are_stored_and_remind_before(tmp_path):
    calendar, scheduler = make_calendar(tmp_path)
    calendar.add("Zahnarzt", datetime(2026, 9, 29, 10, 0, tzinfo=BERLIN))
    calendar.add("Friseur", datetime(2026, 9, 30, 0, 0, tzinfo=BERLIN), all_day=True)
    reminders = scheduler.active(kinds=("event",))
    assert [(a.label, a.due.astimezone(BERLIN).strftime("%d. %H:%M")) for a in reminders] == [
        ("15|Zahnarzt", "29. 09:45")]
    again, _ = make_calendar(tmp_path)  # Neustart: aus der Datei
    assert [e.title for e in again.events] == ["Zahnarzt", "Friseur"]
    assert [e.title for e in calendar.delete("zahn")] == ["Zahnarzt"] and not scheduler.active(kinds=("event",))


def test_capabilities_and_day_plan(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, text=ICS)

    calendar, _ = make_calendar(tmp_path, {"privat": "webcal://example.org/cal.ics"}, handler)
    registry = ToolRegistry()
    register_calendar_capabilities(registry, calendar)

    def call(name, args):
        assert not registry.get(name).validate(args), args
        return asyncio.run(registry.get(name).handler(args, CTX))

    added = call("calendar.add", {"title": "Meeting", "start": "2026-09-29T15:00"})
    assert added["event"]["time"] == "15:00" and added["reminder_minutes"] == 15
    tomorrow = call("calendar.list", {"day": "tomorrow"})
    assert [(e["time"], e["title"]) for e in tomorrow["events"]] == [
        ("10:00", "Zahnarzt, Kontrolle"), ("15:00", "Meeting"), ("16:00", "Call mit New York")]
    assert str(calls[0]).startswith("https://example.org/")  # webcal:// wird https://
    call("calendar.list", {"day": "2026-09-30"})
    assert len(calls) == 1  # zwischengespeichert
    assert call("calendar.list", {"upcoming": True})["events"][0]["title"] == "Sport (verschoben)"  # heute 19 Uhr
    assert call("calendar.delete", {"title": "Meeting"})["deleted"][0]["title"] == "Meeting"
    assert call("calendar.delete", {"title": "Zahnarzt"})["deleted"] == []  # abonnierte Kalender bleiben unberührt
    entries = asyncio.run(day_plan_entries(calendar, date(2026, 10, 1)))
    assert entries == ["ganztägig Miete", "ganztägig Urlaub"]
    assert registry.get("calendar.list").output_trust == "untrusted"  # Einladungen von Fremden


def test_unreachable_subscription_does_not_break_own_events(tmp_path):
    calendar, _ = make_calendar(tmp_path, {"privat": "https://example.org/cal.ics"},
                                lambda request: httpx.Response(503))
    calendar.add("Zahnarzt", datetime(2026, 9, 29, 10, 0, tzinfo=BERLIN))
    assert [e.title for e in asyncio.run(calendar.day(date(2026, 9, 29)))] == ["Zahnarzt"]


def test_calendar_replies():
    style = JarvisStyle()

    def reply(capability, args, result):
        class Record:
            status = "succeeded"

        record = Record()
        record.capability, record.arguments, record.result = capability, args, result
        return style.finalize(style.action_reply(record))

    now = "2026-09-28T09:00+02:00"
    assert reply("calendar.list", {"day": "2026-09-29"}, {"from": "2026-09-29", "days": 1, "now": now, "events": [
        {"title": "Zahnarzt", "date": "2026-09-29", "time": "10:00"},
        {"title": "Urlaub", "date": "2026-09-29", "time": None}]}) == (
        "Morgen stehen 2 Termine an: um 10 Uhr Zahnarzt und ganztägig Urlaub.")
    assert reply("calendar.list", {"day": "2026-09-28"}, {"from": "2026-09-28", "days": 1, "now": now,
                                                          "events": []}) == "Heute sind keine Termine eingetragen."
    assert reply("calendar.add", {"title": "Zahnarzt"}, {"now": now, "reminder_minutes": 15, "event": {
        "title": "Zahnarzt", "date": "2026-10-02", "time": "10:30"}}) == (
        "Sehr wohl. Eingetragen: Zahnarzt am Freitag um 10:30 Uhr. Ich erinnere Sie 15 Minuten vorher.")
    assert reply("calendar.list", {"upcoming": True}, {"now": now, "events": [
        {"title": "Zahnarzt", "date": "2026-09-29", "time": "10:00"}]}) == (
        "Ihr nächster Termin: Zahnarzt, morgen um 10 Uhr.")
