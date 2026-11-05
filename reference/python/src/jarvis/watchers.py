"""Beobachter: „Sag Bescheid, wenn der Akku unter 15 Prozent fällt / der Download fertig ist / Handbrake fertig ist“.

Läuft auf dem JARVIS-Rechner (Windows-Programm): geprüft wird lokal mit psutil und dem Downloads-Ordner – ohne Netz,
ohne Sprachmodell. Ein Beobachter meldet sich genau einmal und verschwindet dann. Gespeichert in
``data/watchers.json``; sie überstehen also einen Neustart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import JarvisError
from .events import new_id
from .tools import Capability, InvocationContext, ToolRegistry

log = logging.getLogger(__name__)
MAX_WATCHES = 10
PARTIAL = (".crdownload", ".part", ".tmp", ".opdownload", ".download")


@dataclass
class Watch:
    id: str
    kind: str  # battery | process | download
    actor: str
    value: str = ""  # Prozent bzw. Programmname
    baseline: list[str] = field(default_factory=list)  # download: Dateien, die schon da waren
    seen: bool = False  # process: lief mindestens einmal
    created: float = field(default_factory=time.time)

    def describe(self) -> str:
        if self.kind == "battery":
            return f"der Akku unter {self.value} Prozent fällt"
        if self.kind == "process":
            return f"{self.value} fertig ist"
        return "der Download fertig ist"


class Probes:
    """Was geprüft wird – austauschbar für Tests."""

    def battery(self) -> tuple[int, bool] | None:
        import psutil

        info = psutil.sensors_battery()
        return None if info is None else (round(info.percent), bool(info.power_plugged))

    def running(self, name: str) -> bool:
        import psutil

        wanted = re.sub(r"\W+", "", name.lower())
        return any(wanted in re.sub(r"\W+", "", (p.info.get("name") or "").lower())
                   for p in psutil.process_iter(["name"]))

    def downloads(self) -> dict[str, float]:
        folder = Path(os.environ.get("JARVIS_DOWNLOADS") or Path.home() / "Downloads")
        try:
            return {e.name: e.stat().st_mtime for e in os.scandir(folder) if e.is_file()}
        except OSError:
            return {}


class WatchService:
    def __init__(self, path: Path | None, notify: Callable[[str, str], Awaitable[None]],
                 probes: Probes | None = None, clock: Callable[[], float] = time.time) -> None:
        self.path, self.notify, self.probes, self.clock = path, notify, probes or Probes(), clock
        self.items: list[Watch] = self._load()

    def _load(self) -> list[Watch]:
        try:
            return [Watch(**w) for w in json.loads(self.path.read_text(encoding="utf-8"))] if self.path else []
        except (OSError, ValueError, TypeError):
            return []

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([asdict(w) for w in self.items], ensure_ascii=False, indent=1), encoding="utf-8")

    def add(self, kind: str, actor: str, value: str = "") -> Watch:
        if len([w for w in self.items if w.actor == actor]) >= MAX_WATCHES:
            raise JarvisError("JRV-VAL-001", "Zu viele Beobachter", user_message="Es laufen schon sehr viele Beobachter.")
        watch = Watch(new_id("wch"), kind, actor, value)
        if kind == "process":
            if not self.probes.running(value):
                raise JarvisError("JRV-NFD-001", f"{value} läuft nicht",
                                  user_message=f"„{value}“ läuft gerade gar nicht – dann gibt es nichts abzuwarten.")
            watch.seen = True
        elif kind == "download":
            watch.baseline = sorted(self.probes.downloads())
        elif kind == "battery" and self.probes.battery() is None:
            raise JarvisError("JRV-NFD-001", "Kein Akku", user_message="Dieser Rechner hat keinen Akku, den ich überwachen könnte.")
        self.items.append(watch)
        self._save()
        return watch

    def cancel(self, actor: str) -> int:
        mine = [w for w in self.items if w.actor == actor]
        for w in mine:
            self.items.remove(w)
        if mine:
            self._save()
        return len(mine)

    def mine(self, actor: str) -> list[Watch]:
        return [w for w in self.items if w.actor == actor]

    async def check(self) -> list[Watch]:
        """Alle Beobachter einmal prüfen; ausgelöste melden und entfernen."""
        fired: list[tuple[Watch, str]] = []
        changed = False
        for w in list(self.items):
            try:
                text = self._evaluate(w)
            except Exception:  # eine kaputte Prüfung darf die anderen nicht aufhalten
                log.exception("Beobachter %s fehlgeschlagen", w.kind)
                continue
            if text:
                fired.append((w, text))
        for w, text in fired:
            self.items.remove(w)
            changed = True
            try:
                await self.notify(w.actor, text)
            except Exception:
                log.exception("Meldung des Beobachters fehlgeschlagen")
        if changed:
            self._save()
        return [w for w, _ in fired]

    def _evaluate(self, w: Watch) -> str | None:
        if w.kind == "battery":
            state = self.probes.battery()
            if state and state[0] <= int(w.value) and not state[1]:
                return f"Der Akku steht bei {state[0]} Prozent – es wäre Zeit für das Ladegerät."
        elif w.kind == "process":
            running = self.probes.running(w.value)
            if running:
                w.seen = True
            elif w.seen:
                return f"{w.value} ist fertig."
        elif w.kind == "download":
            now = self.clock()
            for name, mtime in sorted(self.probes.downloads().items(), key=lambda kv: kv[1]):
                if name in w.baseline or name.lower().endswith(PARTIAL):
                    continue
                if now - mtime > 3:  # seit ein paar Sekunden unverändert = Browser ist fertig
                    return f"Der Download „{name}“ ist fertig."
        return None

    async def run(self, interval: float = 20.0) -> None:
        while True:
            if self.items:
                await self.check()
            await asyncio.sleep(interval)


def register_watch_capabilities(registry: ToolRegistry, service: WatchService) -> None:
    async def add(args: dict[str, Any], ctx: InvocationContext) -> Any:
        kind, value = args["kind"], str(args.get("value") or "").strip()
        if kind == "battery":
            if not value.isdigit() or not 1 <= int(value) <= 99:
                raise JarvisError("JRV-VAL-002", "Prozentwert fehlt", user_message="Bei wie viel Prozent soll ich Bescheid sagen?")
        elif kind == "process" and not value:
            raise JarvisError("JRV-VAL-002", "Programm fehlt", user_message="Auf welches Programm soll ich warten?")
        watch = service.add(kind, ctx.actor, value)
        return {"id": watch.id, "describe": watch.describe()}

    async def listing(args: dict[str, Any], ctx: InvocationContext) -> Any:
        return {"watches": [w.describe() for w in service.mine(ctx.actor)]}

    async def cancel(args: dict[str, Any], ctx: InvocationContext) -> Any:
        return {"cancelled": service.cancel(ctx.actor)}

    registry.register(Capability(
        name="automation.watch", domain="automation", risk_class="R0", side_effects="reversible",
        description="Sagt einmalig Bescheid, wenn der Akku unter einen Prozentwert fällt (kind battery, value Prozent), "
                    "ein laufendes Programm fertig ist (process, value Programmname) oder ein Download fertig ist (download).",
        input_schema={"type": "object", "additionalProperties": False, "required": ["kind"], "properties": {
            "kind": {"enum": ["battery", "process", "download"]}, "value": {"type": "string", "maxLength": 60}}},
        handler=add))
    registry.register(Capability(
        name="automation.watch_list", domain="automation", risk_class="R0", description="Nennt die laufenden Beobachter.",
        input_schema={"type": "object", "additionalProperties": False, "properties": {}}, handler=listing))
    registry.register(Capability(
        name="automation.watch_cancel", domain="automation", risk_class="R0", side_effects="reversible",
        description="Beendet alle Beobachter des Nutzers.",
        input_schema={"type": "object", "additionalProperties": False, "properties": {}}, handler=cancel))
