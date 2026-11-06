"""Kalender: eigene Termine (gespeichert im Datenordner) und abonnierte Kalender im ICS-Format (nur lesen).

ICS-Adressen bieten alle großen Dienste an – Google Kalender („Privatadresse im iCal-Format“), Outlook
(„Kalender veröffentlichen“), iCloud (öffentlicher Kalender). Wiederholungen (RRULE) werden für die gängigen Fälle
aufgelöst: täglich, wöchentlich (auch an mehreren Tagen), monatlich, jährlich, mit INTERVAL, COUNT, UNTIL und
EXDATE; einzeln verschobene Termine (RECURRENCE-ID) ersetzen ihr Original.

Neue Termine legt JARVIS im eigenen Kalender an und erinnert kurz vorher daran. Abonnierte Kalender ändert er nicht.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time as clock_time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import JarvisError
from .events import new_id
from .timers import AlarmScheduler
from .tools import Capability, InvocationContext, ToolRegistry

log = logging.getLogger(__name__)
MAX_ICS_BYTES = 5_000_000
WEEKDAY_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


@dataclass
class Event:
    title: str
    start: datetime  # mit Zeitzone; ganztägig: 00:00 Ortszeit
    end: datetime | None = None
    all_day: bool = False
    location: str = ""
    source: str = "jarvis"
    id: str = field(default_factory=lambda: new_id("evt"))

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "start": self.start.isoformat(),
                "end": self.end.isoformat() if self.end else None, "all_day": self.all_day, "location": self.location}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Event:
        return cls(title=data["title"], start=datetime.fromisoformat(data["start"]),
                   end=datetime.fromisoformat(data["end"]) if data.get("end") else None,
                   all_day=bool(data.get("all_day")), location=data.get("location") or "", id=data["id"])


# ---------------------------------------------------------------------------------------------------------------
# ICS lesen
# ---------------------------------------------------------------------------------------------------------------
def _unescape(value: str) -> str:
    return re.sub(r"\\([\\,;nN])", lambda m: "\n" if m[1] in "nN" else m[1], value).strip()


def _parse_dt(value: str, params: dict[str, str], tz: tzinfo) -> tuple[datetime, bool]:
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        day = datetime.strptime(value[:8], "%Y%m%d").date()
        return datetime.combine(day, time(0, 0), tz), True
    moment = datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return moment.replace(tzinfo=UTC).astimezone(tz), False
    zone: tzinfo = tz
    if params.get("TZID"):
        try:
            zone = ZoneInfo(params["TZID"].strip('"'))
        except (ZoneInfoNotFoundError, ValueError):
            zone = tz  # Windows-Zonennamen („W. Europe Standard Time“) u. Ä.: Ortszeit annehmen
    return moment.replace(tzinfo=zone).astimezone(tz), False


def _lines(text: str) -> list[tuple[str, dict[str, str], str]]:
    unfolded = re.sub(r"\r?\n[ \t]", "", text)  # gefaltete Zeilen zusammenführen
    out = []
    for line in unfolded.splitlines():
        if ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, *raw_params = head.split(";")
        params = {}
        for item in raw_params:
            if "=" in item:
                key, val = item.split("=", 1)
                params[key.upper()] = val
        out.append((name.upper(), params, value))
    return out


def parse_ics(text: str, tz: tzinfo) -> list[dict[str, Any]]:
    """VEVENTs als Rohdaten (mit RRULE/EXDATE), noch nicht aufgelöst."""
    events, current = [], None
    for name, params, value in _lines(text):
        if name == "BEGIN" and value.strip().upper() == "VEVENT":
            current = {"exdates": set()}
        elif name == "END" and value.strip().upper() == "VEVENT" and current is not None:
            if "start" in current and current.get("status") != "CANCELLED":
                events.append(current)
            current = None
        elif current is None:
            continue
        elif name == "SUMMARY":
            current["title"] = _unescape(value)
        elif name == "LOCATION":
            current["location"] = _unescape(value)
        elif name == "UID":
            current["uid"] = value.strip()
        elif name == "STATUS":
            current["status"] = value.strip().upper()
        elif name in ("DTSTART", "DTEND", "RECURRENCE-ID"):
            try:
                moment, all_day = _parse_dt(value, params, tz)
            except ValueError:
                continue
            key = {"DTSTART": "start", "DTEND": "end", "RECURRENCE-ID": "recurrence_id"}[name]
            current[key] = moment
            if name == "DTSTART":
                current["all_day"] = all_day
        elif name == "RRULE":
            current["rrule"] = dict(part.split("=", 1) for part in value.strip().split(";") if "=" in part)
        elif name == "EXDATE":
            for item in value.split(","):
                try:
                    current["exdates"].add(_parse_dt(item, params, tz)[0])
                except ValueError:
                    continue
    return events


def _occurrences(event: dict[str, Any], window_start: datetime, window_end: datetime) -> list[datetime]:
    start: datetime = event["start"]
    rule = event.get("rrule")
    if not rule:
        return [start]
    freq = rule.get("FREQ", "")
    interval = max(1, int(rule.get("INTERVAL", "1") or 1))
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    until = None
    if rule.get("UNTIL"):
        try:
            until = _parse_dt(rule["UNTIL"], {}, start.tzinfo or UTC)[0]
            if len(rule["UNTIL"]) == 8:  # nur Datum: ganzer Tag gilt noch
                until += timedelta(days=1)
        except ValueError:
            until = None
    by_day = [d[-2:] for d in rule.get("BYDAY", "").split(",") if d]
    out: list[datetime] = []
    produced = 0

    def emit(moment: datetime) -> bool:  # False = Ende erreicht
        nonlocal produced
        if moment < start:
            return True
        if (until and moment > until) or (count is not None and produced >= count) or moment > window_end:
            return False
        produced += 1
        if moment not in event["exdates"] and moment >= window_start - timedelta(days=1):
            out.append(moment)
        return True

    step = 0
    while step < 5000:
        if freq == "DAILY":
            if not emit(start + timedelta(days=step * interval)):
                break
        elif freq == "WEEKLY":
            week = start + timedelta(weeks=step * interval)
            monday = week - timedelta(days=week.weekday())
            days = sorted(WEEKDAY_CODES.index(d) for d in by_day if d in WEEKDAY_CODES) or [start.weekday()]
            if not all(emit(monday + timedelta(days=d)) for d in days):
                break
        elif freq in ("MONTHLY", "YEARLY"):
            months = step * interval * (12 if freq == "YEARLY" else 1)
            year, month = start.year + (start.month - 1 + months) // 12, (start.month - 1 + months) % 12 + 1
            try:
                moment = start.replace(year=year, month=month)
            except ValueError:  # 31. in einem kürzeren Monat: auslassen
                step += 1
                continue
            if not emit(moment):
                break
        else:
            return [start]
        step += 1
    return out


def expand(events: list[dict[str, Any]], window_start: datetime, window_end: datetime, source: str) -> list[Event]:
    overridden = {(e.get("uid"), e["recurrence_id"]) for e in events if e.get("recurrence_id")}
    out = []
    for raw in events:
        duration = (raw["end"] - raw["start"]) if raw.get("end") else None
        for start in _occurrences(raw, window_start, window_end):
            if not raw.get("recurrence_id") and (raw.get("uid"), start) in overridden:
                continue
            end = start + duration if duration else None
            if start < window_end and (end or start + timedelta(minutes=1)) > window_start:
                out.append(Event(raw.get("title") or "(ohne Titel)", start, end, bool(raw.get("all_day")),
                                 raw.get("location") or "", source))
    return out


# ---------------------------------------------------------------------------------------------------------------
# Kalenderdienst
# ---------------------------------------------------------------------------------------------------------------
class CalendarService:
    def __init__(self, *, tz: tzinfo, path: Path | None = None, ics: dict[str, str] | None = None,
                 scheduler: AlarmScheduler | None = None, remind_minutes: int = 15, client: Any = None,
                 cache_s: float = 600.0, clock: Callable[[], datetime] | None = None, remote: Any = None) -> None:
        self.tz = tz
        self.remote = remote  # CalDAV (caldav.CalDav): Nextcloud/iCloud … – Termine dort eintragen und lesen
        self.path = path
        self.ics = {name: url for name, url in (ics or {}).items() if url}
        self.scheduler = scheduler
        self.remind_minutes = remind_minutes
        self.clock = clock or (lambda: datetime.now(tz))
        self._client = client
        self._cache_s = cache_s
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self.events: list[Event] = self._load()

    async def between(self, start: datetime, end: datetime) -> list[Event]:
        found = [e for e in self.events if e.start < end and (e.end or e.start + timedelta(minutes=1)) > start]
        for name, url in self.ics.items():
            try:
                found += expand(await self._fetch(url), start, end, name)
            except JarvisError as exc:
                log.warning("Kalender nicht erreichbar", extra={"calendar": name, "error": exc.detail})
        if self.remote is not None:
            try:
                raw = await self.remote.between(start, end)
                local = {e.id for e in self.events}  # von JARVIS eingetragene Termine stehen schon lokal
                raw = [r for r in raw if str(r.get("uid", "")).removesuffix("@jarvis") not in local]
                found += expand(raw, start, end, self.remote.name)
            except JarvisError as exc:
                log.warning("Online-Kalender nicht erreichbar", extra={"error": exc.detail})
        return sorted(found, key=lambda e: (not e.all_day, e.start, e.title))

    async def day(self, day: date) -> list[Event]:
        start = datetime.combine(day, time(0, 0), self.tz)
        return await self.between(start, start + timedelta(days=1))

    def add(self, title: str, start: datetime, *, end: datetime | None = None, all_day: bool = False,
            location: str = "", actor: str = "user:owner", remind_minutes: int | None = None) -> Event:
        event = Event(title.strip() or "Termin", start, end, all_day, location)
        self.events.append(event)
        self._save()
        minutes = self.remind_minutes if remind_minutes is None else remind_minutes
        if self.scheduler and not all_day and minutes >= 0:
            due = start - timedelta(minutes=minutes)
            if due > self.clock():
                self.scheduler.add("event", due, f"{minutes}|{event.title}", actor)
        return event

    def delete(self, title: str, day: date | None = None) -> list[Event]:
        wanted = title.lower().strip()
        gone = [e for e in self.events if wanted in e.title.lower()
                and (day is None or e.start.astimezone(self.tz).date() == day)]
        self.events = [e for e in self.events if e not in gone]
        if gone:
            self._save()
            for event in gone:
                if self.scheduler:
                    self.scheduler.remove_event_reminders(event.title)
        return gone

    async def _fetch(self, url: str) -> list[dict[str, Any]]:
        hit = self._cache.get(url)
        if hit and hit[0] > clock_time.monotonic():
            return hit[1]
        import httpx

        client = self._client or httpx.AsyncClient(timeout=10.0, follow_redirects=True)
        try:
            response = await client.get(url.replace("webcal://", "https://", 1))
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise JarvisError("JRV-INT-001", f"Kalender nicht erreichbar: {exc}",
                              user_message="Der Kalender ist gerade nicht erreichbar.") from exc
        finally:
            if self._client is None:
                await client.aclose()
        if len(response.content) > MAX_ICS_BYTES:
            raise JarvisError("JRV-INT-001", "Kalender zu groß")
        events = parse_ics(response.text, self.tz)
        self._cache[url] = (clock_time.monotonic() + self._cache_s, events)
        return events

    def _load(self) -> list[Event]:
        if self.path is None or not self.path.exists():
            return []
        try:
            return [Event.from_json(item) for item in json.loads(self.path.read_text(encoding="utf-8"))]
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("Kalenderdatei nicht lesbar", extra={"error": str(exc)})
            return []

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".calendar-")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump([e.to_json() for e in self.events], handle, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)


def describe(event: Event, tz: tzinfo) -> dict[str, Any]:
    start = event.start.astimezone(tz)
    return {"title": event.title, "date": start.date().isoformat(), "time": None if event.all_day else f"{start:%H:%M}",
            "end": f"{event.end.astimezone(tz):%H:%M}" if event.end and not event.all_day else None,
            "location": event.location or None, "calendar": event.source}


def register_calendar_capabilities(registry: ToolRegistry, calendar: CalendarService) -> None:
    tz = calendar.tz

    def parse_day(value: str | None) -> date:
        today = calendar.clock().astimezone(tz).date()
        if not value or value == "today":
            return today
        if value == "tomorrow":
            return today + timedelta(days=1)
        return date.fromisoformat(value)

    async def listing(args: dict[str, Any], ctx: InvocationContext) -> Any:
        first = parse_day(args.get("day"))
        days = int(args.get("days", 1))
        start = datetime.combine(first, time(0, 0), tz)
        if args.get("upcoming"):
            start, days = calendar.clock(), 30
        events = await calendar.between(start, datetime.combine(first, time(0, 0), tz) + timedelta(days=days))
        if args.get("upcoming"):
            events = [e for e in events if not e.all_day][:1] or events[:1]
        return {"from": first.isoformat(), "days": days, "events": [describe(e, tz) for e in events],
                "now": calendar.clock().astimezone(tz).isoformat(timespec="minutes")}

    async def add(args: dict[str, Any], ctx: InvocationContext) -> Any:
        start = datetime.fromisoformat(args["start"])
        all_day = bool(args.get("all_day")) or len(args["start"]) == 10
        start = start.replace(tzinfo=tz) if start.tzinfo is None else start
        end = datetime.fromisoformat(args["end"]).replace(tzinfo=tz) if args.get("end") else None
        event = calendar.add(args["title"], start, end=end, all_day=all_day, location=args.get("location", ""),
                             actor=ctx.actor, remind_minutes=args.get("remind_minutes"))
        synced = None
        if calendar.remote is not None:  # zusätzlich in Nextcloud/iCloud …, lokal bleibt er für die Erinnerung
            try:
                await calendar.remote.put(event)
                synced = True
            except JarvisError as exc:
                log.warning("Termin nicht im Online-Kalender eingetragen", extra={"error": exc.detail})
                synced = False
        return {"event": describe(event, tz), "synced": synced,
                "now": calendar.clock().astimezone(tz).isoformat(timespec="minutes"),
                "reminder_minutes": None if all_day else
                (calendar.remind_minutes if args.get("remind_minutes") is None else args["remind_minutes"])}

    async def delete(args: dict[str, Any], ctx: InvocationContext) -> Any:
        gone = calendar.delete(args["title"], date.fromisoformat(args["date"]) if args.get("date") else None)
        if calendar.remote is not None:
            for event in gone:
                try:
                    await calendar.remote.delete(event)
                except JarvisError as exc:
                    log.warning("Termin im Online-Kalender nicht gelöscht", extra={"error": exc.detail})
        return {"deleted": [describe(e, tz) for e in gone]}

    registry.register(Capability(
        name="calendar.list", domain="calendar", risk_class="R0", output_trust="untrusted", timeout_s=20.0,
        description="Termine eines Tages (day: today, tomorrow oder YYYY-MM-DD; days: Anzahl Tage) aus dem eigenen "
                    "JARVIS-Kalender und abonnierten Kalendern; upcoming=true liefert den nächsten Termin.",
        input_schema={"type": "object", "additionalProperties": False, "properties": {
            "day": {"type": "string", "pattern": "^(today|tomorrow|\\d{4}-\\d{2}-\\d{2})$"},
            "days": {"type": "integer", "minimum": 1, "maximum": 31},
            "upcoming": {"type": "boolean"},
        }},
        handler=listing,
    ))
    registry.register(Capability(
        name="calendar.add", domain="calendar", risk_class="R1", side_effects="reversible",
        description="Trägt einen Termin in den JARVIS-Kalender ein (start: ISO-Datum, mit Uhrzeit oder nur Datum "
                    "für ganztägig; Ortszeit). JARVIS erinnert kurz vorher.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["title", "start"], "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "start": {"type": "string", "minLength": 10, "maxLength": 40},
            "end": {"type": "string", "minLength": 10, "maxLength": 40},
            "all_day": {"type": "boolean"},
            "location": {"type": "string", "maxLength": 200},
            "remind_minutes": {"type": "integer", "minimum": 0, "maximum": 10080},
        }},
        handler=add,
    ))
    registry.register(Capability(
        name="calendar.delete", domain="calendar", risk_class="R1", side_effects="irreversible",
        description="Löscht Termine aus dem JARVIS-Kalender, deren Titel den Text enthält (optional nur an date).",
        input_schema={"type": "object", "additionalProperties": False, "required": ["title"], "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
        }},
        handler=delete,
    ))


async def day_plan_entries(calendar: CalendarService, day: date) -> list[str]:
    """Für den Tagesplan: „10:00 Zahnarzt“, „ganztägig: Urlaub“."""
    entries = []
    for event in await calendar.day(day):
        start = event.start.astimezone(calendar.tz)
        entries.append(f"ganztägig {event.title}" if event.all_day else f"{start:%H:%M} {event.title}")
    return entries
