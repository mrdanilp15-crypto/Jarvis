"""Lernende Automationen: Gewohnheiten aus der Nutzung erkennen und – nach Zustimmung – selbst übernehmen.

1. **Protokoll:** Jede Zustandsänderung steuerbarer Geräte (Licht, Schalter, Rollläden, Heizung an/aus) landet in
   ``data/patterns.db`` – Zeitpunkt, Gerät, neuer Zustand. Änderungen, die JARVIS selbst aus einer Routine auslöst,
   zählen nicht (sonst bestätigt sich eine Routine selbst).
2. **Muster:** Je Gerät, Aktion und Tagesart (Werktag/Wochenende) sucht ``find_patterns`` das dichteste Zeitfenster
   (±20 min) der letzten 21 Tage. Zählt es an mindestens fünf verschiedenen Tagen, wird es ein Vorschlag mit
   Häufigkeit („an 9 von 15 Werktagen“).
3. **Zustimmung:** Vorschläge erscheinen in der Oberfläche; JARVIS kündigt neue einmal an. Erst angenommene Vorschläge
   werden zur Routine. Keine starre Uhrzeit: Die Zeit folgt gleitend dem Median der jüngsten Nutzung.
4. **Ausführung:** Über ``request_action`` mit der Rolle ``service`` (Policy, Audit, höchstens R2). Ist das Gerät schon
   im Zielzustand, passiert nichts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import statistics
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TRACKED = ("light", "switch", "cover", "climate")
WINDOW_MIN = 20
MIN_DAYS = 5
HISTORY_DAYS = 21


@dataclass
class Suggestion:
    id: str
    entity_id: str
    name: str
    action: str  # on | off | open | close
    daytype: str  # weekday | weekend
    minute: int  # Minute des Tages (Median)
    days: int  # an so vielen Tagen beobachtet
    of_days: int  # von so vielen Tagen dieser Art
    status: str = "new"  # new | accepted | rejected
    last_run: str | None = None

    @property
    def clock(self) -> str:
        return f"{self.minute // 60}:{self.minute % 60:02d}"

    def to_json(self) -> dict[str, Any]:
        return {**asdict(self), "clock": self.clock}


def action_of(entity_id: str, state: str) -> str | None:
    domain = entity_id.split(".", 1)[0]
    if domain in ("light", "switch"):
        return {"on": "on", "off": "off"}.get(state)
    if domain == "cover":
        return {"open": "open", "closed": "close"}.get(state)
    if domain == "climate":
        return "off" if state == "off" else ("on" if state in ("heat", "auto", "heat_cool", "cool") else None)
    return None


def daytype(day: date) -> str:
    return "weekend" if day.weekday() >= 5 else "weekday"


def find_patterns(events: list[tuple[str, str, datetime]], now: datetime, names: dict[str, str] | None = None,
                  *, history_days: int = HISTORY_DAYS, min_days: int = MIN_DAYS) -> list[Suggestion]:
    """events: (entity_id, action, Zeitpunkt in Ortszeit). Liefert Vorschläge, stärkste zuerst."""
    start = (now - timedelta(days=history_days)).date()
    groups: dict[tuple[str, str, str], list[tuple[date, int]]] = {}
    for entity_id, action, moment in events:
        if moment.date() < start or moment > now:
            continue
        key = (entity_id, action, daytype(moment.date()))
        groups.setdefault(key, []).append((moment.date(), moment.hour * 60 + moment.minute))
    available = {"weekday": 0, "weekend": 0}
    for offset in range(history_days):
        available[daytype((now - timedelta(days=offset + 1)).date())] += 1
    out = []
    for (entity_id, action, kind), samples in groups.items():
        samples.sort(key=lambda s: s[1])
        best: list[tuple[date, int]] = []
        for i, (_, minute) in enumerate(samples):  # dichtestes Fenster ±WINDOW_MIN um jeden Zeitpunkt
            window = [s for s in samples[i:] if s[1] - minute <= 2 * WINDOW_MIN]
            if len({d for d, _ in window}) > len({d for d, _ in best}):
                best = window
        days = len({d for d, _ in best})
        if days < min_days:
            continue
        per_day = {}
        for day, minute in best:  # je Tag nur das erste Ereignis im Fenster
            per_day.setdefault(day, minute)
        median = int(statistics.median(per_day.values()))
        out.append(Suggestion(id=f"{entity_id}|{action}|{kind}", entity_id=entity_id,
                              name=(names or {}).get(entity_id, entity_id), action=action, daytype=kind,
                              minute=median, days=days, of_days=max(available[kind], days)))
    return sorted(out, key=lambda s: (s.days / s.of_days, s.days), reverse=True)


class PatternLearner:
    def __init__(self, path: Path, *, tz: tzinfo, clock: Callable[[], datetime] | None = None) -> None:
        self.tz = tz
        self.clock = clock or (lambda: datetime.now(tz))
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS usage (entity_id TEXT, action TEXT, at TEXT)")
        self._db.execute("CREATE INDEX IF NOT EXISTS usage_at ON usage(at)")
        self._db.execute("CREATE TABLE IF NOT EXISTS suggestions (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self._db.commit()
        self._own: dict[str, datetime] = {}  # entity_id -> wann JARVIS es zuletzt per Routine geschaltet hat

    # -- Protokoll ---------------------------------------------------------------------------------------------
    def observe(self, data: dict[str, Any]) -> None:
        """HA-``state_changed``: nur echte Wechsel steuerbarer Geräte, keine eigenen Routine-Schaltungen."""
        entity_id = str(data.get("entity_id") or "")
        new, old = data.get("new_state") or {}, data.get("old_state") or {}
        if entity_id.split(".", 1)[0] not in TRACKED or new.get("state") == old.get("state"):
            return
        action = action_of(entity_id, str(new.get("state")))
        if action is None or old.get("state") in (None, "unavailable", "unknown"):
            return
        now = self.clock()
        if (own := self._own.get(entity_id)) and now - own < timedelta(minutes=2):
            return
        self._db.execute("INSERT INTO usage VALUES (?, ?, ?)", (entity_id, action, now.isoformat()))
        self._db.commit()

    def events(self, days: int = HISTORY_DAYS + 1) -> list[tuple[str, str, datetime]]:
        since = (self.clock() - timedelta(days=days)).isoformat()
        rows = self._db.execute("SELECT entity_id, action, at FROM usage WHERE at >= ?", (since,)).fetchall()
        return [(e, a, datetime.fromisoformat(at).astimezone(self.tz)) for e, a, at in rows]

    def prune(self, keep_days: int = 60) -> None:
        self._db.execute("DELETE FROM usage WHERE at < ?", ((self.clock() - timedelta(days=keep_days)).isoformat(),))
        self._db.commit()

    # -- Vorschläge --------------------------------------------------------------------------------------------
    def suggestions(self) -> list[Suggestion]:
        return [Suggestion(**json.loads(data)) for (data,) in self._db.execute("SELECT data FROM suggestions")]

    def _store(self, suggestion: Suggestion) -> None:
        data = {k: v for k, v in asdict(suggestion).items()}
        self._db.execute("INSERT OR REPLACE INTO suggestions VALUES (?, ?)", (suggestion.id, json.dumps(data)))
        self._db.commit()

    def analyse(self, names: dict[str, str] | None = None) -> list[Suggestion]:
        """Muster neu berechnen; angenommene Routinen gleiten zur neuen Uhrzeit. -> neu entdeckte Vorschläge."""
        known = {s.id: s for s in self.suggestions()}
        fresh = []
        for found in find_patterns(self.events(), self.clock(), names):
            old = known.get(found.id)
            if old is None:
                self._store(found)
                fresh.append(found)
            elif old.status != "rejected":
                old.minute, old.days, old.of_days, old.name = found.minute, found.days, found.of_days, found.name
                self._store(old)
        return fresh

    def decide(self, suggestion_id: str, accept: bool) -> Suggestion | None:
        for suggestion in self.suggestions():
            if suggestion.id == suggestion_id:
                suggestion.status = "accepted" if accept else "rejected"
                self._store(suggestion)
                return suggestion
        return None

    def due(self, now: datetime | None = None) -> list[Suggestion]:
        """Angenommene Routinen, deren Zeit jetzt ist (einmal pro Tag)."""
        now = now or self.clock()
        minute, today = now.hour * 60 + now.minute, now.date().isoformat()
        return [s for s in self.suggestions() if s.status == "accepted" and s.daytype == daytype(now.date())
                and 0 <= minute - s.minute < 2 and s.last_run != today]

    def mark_run(self, suggestion: Suggestion) -> None:
        suggestion.last_run = self.clock().date().isoformat()
        self._store(suggestion)
        self._own[suggestion.entity_id] = self.clock()


# Routine -> (Capability, Argumente)
def action_call(suggestion: Suggestion) -> tuple[str, dict[str, Any]]:
    domain = suggestion.entity_id.split(".", 1)[0]
    if domain == "light":
        return "home.set_light", {"entity_ids": [suggestion.entity_id], "on": suggestion.action == "on"}
    if domain == "switch":
        return "home.set_switch", {"entity_ids": [suggestion.entity_id], "on": suggestion.action == "on"}
    if domain == "cover":
        return "home.set_cover", {"entity_ids": [suggestion.entity_id],
                                  "position": 100 if suggestion.action == "open" else 0}
    return "home.set_climate", {"entity_id": suggestion.entity_id,
                                "hvac_mode": "off" if suggestion.action == "off" else "heat"}


def describe(suggestion: Suggestion) -> str:
    when = "werktags" if suggestion.daytype == "weekday" else "am Wochenende"
    verb = {"on": "einschalten", "off": "ausschalten", "open": "hochfahren", "close": "herunterfahren"}[suggestion.action]
    return (f"{suggestion.name} {when} gegen {suggestion.clock} Uhr {verb} "
            f"(an {suggestion.days} von {suggestion.of_days} Tagen beobachtet)")


async def run_routines(learner: PatternLearner, execute: Callable[[Suggestion], Awaitable[None]],
                       analyse: Callable[[], Awaitable[None]], *, interval_s: float = 30.0) -> None:
    """Hintergrund: Routinen zur gelernten Zeit ausführen, einmal pro Stunde neu lernen."""
    last_analysis = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            if loop.time() - last_analysis > 3600:
                last_analysis = loop.time()
                await analyse()
            for suggestion in learner.due():
                learner.mark_run(suggestion)
                await execute(suggestion)
        except Exception:
            log.exception("Routinen: Durchlauf fehlgeschlagen")
        await asyncio.sleep(interval_s)
