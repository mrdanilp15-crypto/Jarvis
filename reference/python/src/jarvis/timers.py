"""Timer und Erinnerungen: laufen im Kern, überstehen Neustarts (JSON-Datei) und melden sich über alle Kanäle.

Ist ein Timer abgelaufen, spricht die Weboberfläche die Meldung (Push über ``/v1/stream``) und der PC-Agent zeigt
einen Windows-Hinweis. Ist gerade kein Fenster offen, bleibt die Meldung liegen und kommt beim nächsten Öffnen.
Was während einer Abschaltung fällig wurde, meldet JARVIS nach dem Start als verspätet.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from .errors import JarvisError
from .events import new_id
from .tools import Capability, InvocationContext, ToolRegistry

log = logging.getLogger(__name__)
MAX_ALARMS = 50


@dataclass
class Alarm:
    id: str
    kind: str  # timer | reminder | event
    due: datetime  # mit Zeitzone
    label: str
    actor: str
    duration_s: int | None = None
    created: datetime = field(default_factory=lambda: datetime.now(UTC))
    late: bool = False  # während einer Abschaltung fällig geworden

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["due"], data["created"] = self.due.isoformat(), self.created.isoformat()
        data.pop("late")
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Alarm:
        return cls(**{**data, "due": datetime.fromisoformat(data["due"]),
                      "created": datetime.fromisoformat(data["created"])})


class AlarmScheduler:
    def __init__(self, *, notify: Callable[[Alarm], Awaitable[None]], path: Path | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.notify = notify
        self.path = path
        self.clock = clock
        self.alarms: dict[str, Alarm] = {}
        self._wake: asyncio.Event | None = None
        self._load()

    # -- Verwaltung ------------------------------------------------------------------------------------
    def add(self, kind: str, due: datetime, label: str, actor: str, *, duration_s: int | None = None) -> Alarm:
        if due.tzinfo is None:
            raise ValueError("due braucht eine Zeitzone")
        if len(self.alarms) >= MAX_ALARMS:
            raise JarvisError("JRV-VAL-001", "Zu viele Timer", user_message="Es laufen bereits sehr viele Timer.")
        alarm = Alarm(new_id("alm"), kind, due, label.strip(), actor, duration_s)
        self.alarms[alarm.id] = alarm
        self._changed()
        return alarm

    def active(self, actor: str | None = None, kinds: tuple[str, ...] = ("timer", "reminder", "event")) -> list[Alarm]:
        return sorted((a for a in self.alarms.values() if (actor is None or a.actor == actor) and a.kind in kinds),
                      key=lambda a: a.due)

    def cancel(self, *, actor: str | None = None, label: str | None = None, everything: bool = False,
               kinds: tuple[str, ...] = ("timer", "reminder")) -> list[Alarm]:
        """Alle, passende nach Bezeichnung oder – ohne Angabe – den nächsten fälligen."""
        candidates = self.active(actor, kinds)
        if label:
            wanted = label.lower()
            candidates = [a for a in candidates if wanted in a.label.lower()]
        elif not everything:
            candidates = candidates[:1]
        for alarm in candidates:
            self.alarms.pop(alarm.id, None)
        if candidates:
            self._changed()
        return candidates

    def remove_event_reminders(self, title: str) -> None:
        """Erinnerungen eines gelöschten Termins (Bezeichnung „<Minuten>|<Titel>“)."""
        for alarm in [a for a in self.alarms.values() if a.kind == "event" and a.label.split("|", 1)[-1] == title]:
            self.alarms.pop(alarm.id, None)
        self._changed()

    # -- Ablauf ----------------------------------------------------------------------------------------
    async def fire_due(self) -> list[Alarm]:
        now = self.clock()
        due = sorted((a for a in self.alarms.values() if a.due <= now), key=lambda a: a.due)
        for alarm in due:
            self.alarms.pop(alarm.id, None)
            alarm.late = now - alarm.due > timedelta(seconds=90)  # z. B. während einer Abschaltung fällig
        if due:
            self._save()
        for alarm in due:
            try:
                await self.notify(alarm)
            except Exception:  # eine fehlgeschlagene Meldung darf die übrigen nicht aufhalten
                log.exception("Meldung fehlgeschlagen", extra={"alarm": alarm.id})
        return due

    async def run(self) -> None:
        self._wake = asyncio.Event()
        while True:
            await self.fire_due()
            upcoming = min((a.due for a in self.alarms.values()), default=None)
            wait = 60.0 if upcoming is None else max(0.05, min(60.0, (upcoming - self.clock()).total_seconds()))
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=wait)
            except TimeoutError:
                pass

    # -- Speicher --------------------------------------------------------------------------------------
    def _changed(self) -> None:
        self._save()
        if self._wake is not None:
            self._wake.set()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            for item in json.loads(self.path.read_text(encoding="utf-8")):
                alarm = Alarm.from_json(item)
                self.alarms[alarm.id] = alarm
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("Timer-Datei nicht lesbar – beginne leer", extra={"error": str(exc)})

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps([a.to_json() for a in self.alarms.values()], ensure_ascii=False, indent=1)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".alarms-")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        os.replace(tmp, self.path)  # atomar: nie eine halb geschriebene Datei


def register_timer_capabilities(registry: ToolRegistry, scheduler: AlarmScheduler, tz: tzinfo) -> None:
    def local(moment: datetime) -> str:
        return moment.astimezone(tz).isoformat(timespec="minutes")

    async def start(args: dict[str, Any], ctx: InvocationContext) -> Any:
        seconds = int(args["duration_s"])
        alarm = scheduler.add("timer", scheduler.clock() + timedelta(seconds=seconds), args.get("label") or "",
                              ctx.actor, duration_s=seconds)
        return {"id": alarm.id, "label": alarm.label, "duration_s": seconds, "due": local(alarm.due),
                "now": local(scheduler.clock())}

    async def remind(args: dict[str, Any], ctx: InvocationContext) -> Any:
        if args.get("in_s"):
            due = scheduler.clock() + timedelta(seconds=int(args["in_s"]))
        elif args.get("at"):
            due = datetime.fromisoformat(args["at"])
            due = due.replace(tzinfo=tz) if due.tzinfo is None else due
        else:
            raise JarvisError("JRV-VAL-002", "at oder in_s fehlt", user_message="Wann soll ich Sie erinnern?")
        if due <= scheduler.clock():
            raise JarvisError("JRV-VAL-002", "Zeitpunkt liegt in der Vergangenheit",
                              user_message="Dieser Zeitpunkt liegt bereits in der Vergangenheit.")
        alarm = scheduler.add("reminder", due, args["text"], ctx.actor)
        return {"id": alarm.id, "text": alarm.label, "due": local(alarm.due), "now": local(scheduler.clock())}

    async def listing(args: dict[str, Any], ctx: InvocationContext) -> Any:
        now = scheduler.clock()
        return {"now": local(now), "alarms": [{"kind": a.kind, "label": a.label, "due": local(a.due),
                                               "duration_s": a.duration_s,
                                               "remaining_s": max(0, int((a.due - now).total_seconds()))}
                                              for a in scheduler.active(ctx.actor, ("timer", "reminder"))]}

    async def cancel(args: dict[str, Any], ctx: InvocationContext) -> Any:
        kinds = ("reminder",) if args.get("kind") == "reminder" else ("timer",) if args.get("kind") == "timer" \
            else ("timer", "reminder")
        cancelled = scheduler.cancel(actor=ctx.actor, label=args.get("label"), everything=bool(args.get("all")),
                                     kinds=kinds)
        return {"cancelled": [{"kind": a.kind, "label": a.label, "duration_s": a.duration_s} for a in cancelled]}

    registry.register(Capability(
        name="timer.start", domain="timer", risk_class="R0", side_effects="reversible",
        description="Stellt einen Timer (Dauer in Sekunden, optional mit Bezeichnung wie „Nudeln“). Beim Ablauf "
                    "meldet sich JARVIS in der Oberfläche und als Windows-Hinweis.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["duration_s"], "properties": {
            "duration_s": {"type": "integer", "minimum": 1, "maximum": 86400},
            "label": {"type": "string", "maxLength": 100},
        }},
        handler=start,
    ))
    registry.register(Capability(
        name="reminder.create", domain="reminder", risk_class="R0", side_effects="reversible",
        description="Erinnert den Nutzer zu einem Zeitpunkt (at: ISO-Datum mit Uhrzeit, Ortszeit) oder nach einer "
                    "Dauer (in_s) an einen Text.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["text"], "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": 300},
            "at": {"type": "string", "minLength": 10, "maxLength": 40},
            "in_s": {"type": "integer", "minimum": 1, "maximum": 31_536_000},
        }},
        handler=remind,
    ))
    registry.register(Capability(
        name="timer.list", domain="timer", risk_class="R0",
        description="Nennt laufende Timer und anstehende Erinnerungen mit Restzeit.",
        input_schema={"type": "object", "additionalProperties": False, "properties": {}},
        handler=listing,
    ))
    registry.register(Capability(
        name="timer.cancel", domain="timer", risk_class="R0", side_effects="reversible",
        description="Stoppt einen Timer oder eine Erinnerung: nach Bezeichnung (label), alle (all=true) oder – ohne "
                    "Angabe – den nächsten fälligen.",
        input_schema={"type": "object", "additionalProperties": False, "properties": {
            "label": {"type": "string", "maxLength": 100},
            "all": {"type": "boolean"},
            "kind": {"enum": ["timer", "reminder"]},
        }},
        handler=cancel,
    ))


class Notifier:
    """Meldungen an die offenen Oberflächen eines Nutzers; ohne offenes Fenster bleiben sie bis zu 12 h liegen."""

    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.clock = clock
        self.listeners: dict[str, list[Callable[[dict[str, Any]], Awaitable[None]]]] = {}
        self.pending: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}

    def attach(self, actor: str, send: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self.listeners.setdefault(actor, []).append(send)

    def detach(self, actor: str, send: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        if send in self.listeners.get(actor, []):
            self.listeners[actor].remove(send)

    def drain(self, actor: str) -> list[dict[str, Any]]:
        cutoff = self.clock() - timedelta(hours=12)
        return [message for at, message in self.pending.pop(actor, []) if at >= cutoff]

    async def send(self, actor: str, message: dict[str, Any]) -> bool:
        delivered = False
        for send in list(self.listeners.get(actor, [])):
            try:
                await send(message)
                delivered = True
            except Exception:  # Verbindung gerade getrennt
                self.detach(actor, send)
        if not delivered:
            self.pending.setdefault(actor, []).append((self.clock(), message))
            del self.pending[actor][:-20]
        return delivered
