"""Wenn-dann-Regeln: „Wenn es morgen regnet, sag mir Bescheid“, „Wenn es morgen friert …“, „Wenn ich nach 23 Uhr noch am PC bin …“.

Jede Regel prüft einmal am Tag zur genannten Uhrzeit (Wetter über das vorhandene Wetter-Werkzeug, ohne Sprachmodell) und
meldet sich dann mit einem kurzen Satz. Vor 12 Uhr gilt die Vorhersage für heute, sonst für morgen.
Gespeichert in ``data/rules.json``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import JarvisError
from .events import new_id
from .tools import Capability, InvocationContext, ToolRegistry

log = logging.getLogger(__name__)
MAX_RULES = 10
DEFAULT_AT = {"rain": "20:00", "frost": "20:00", "late": "23:00"}


@dataclass
class Rule:
    id: str
    actor: str
    kind: str  # rain | frost | late
    at: str  # HH:MM
    last: str = ""  # Datum der letzten Prüfung

    def describe(self) -> str:
        if self.kind == "late":
            return f"Sie nach {self.at} Uhr noch am Rechner sind"
        what = "Regen" if self.kind == "rain" else "Frost"
        if self.at < "12:00":
            return f"für den Tag {what} angesagt ist (Prüfung jeden Morgen um {self.at} Uhr)"
        return f"für den nächsten Tag {what} angesagt ist (Prüfung jeden Abend um {self.at} Uhr)"

    def due(self, now: datetime) -> bool:
        if self.last == now.date().isoformat():
            return False
        h, m = map(int, self.at.split(":"))
        minutes = now.hour * 60 + now.minute - (h * 60 + m)
        return 0 <= minutes < 20  # nach einem Neustart bis 20 Minuten später


class RuleService:
    def __init__(self, path: Path | None, notify: Callable[[str, str], Awaitable[None]],
                 forecast: Callable[[], Awaitable[dict[str, Any] | None]],
                 online: Callable[[str], bool] = lambda actor: True) -> None:
        self.path, self.notify, self.forecast, self.online = path, notify, forecast, online
        self.items: list[Rule] = self._load()

    def _load(self) -> list[Rule]:
        try:
            return [Rule(**r) for r in json.loads(self.path.read_text(encoding="utf-8"))] if self.path else []
        except (OSError, ValueError, TypeError):
            return []

    def _save(self) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps([asdict(r) for r in self.items], ensure_ascii=False, indent=1), encoding="utf-8")

    def add(self, actor: str, kind: str, at: str | None) -> Rule:
        at = at or DEFAULT_AT[kind]
        if not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", at):
            raise JarvisError("JRV-VAL-002", "Uhrzeit ungültig", user_message="Diese Uhrzeit verstehe ich nicht.")
        if len([r for r in self.items if r.actor == actor]) >= MAX_RULES:
            raise JarvisError("JRV-VAL-001", "Zu viele Regeln", user_message="Es gibt schon sehr viele Regeln.")
        h, m = at.split(":")
        rule = Rule(new_id("rul"), actor, kind, f"{int(h):02d}:{m}")
        self.items.append(rule)
        self._save()
        return rule

    def mine(self, actor: str) -> list[Rule]:
        return [r for r in self.items if r.actor == actor]

    def cancel(self, actor: str) -> int:
        mine = self.mine(actor)
        for r in mine:
            self.items.remove(r)
        if mine:
            self._save()
        return len(mine)

    async def check(self, now: datetime) -> list[str]:
        sent = []
        for rule in list(self.items):
            if not rule.due(now):
                continue
            rule.last = now.date().isoformat()
            self._save()
            try:
                text = await self._evaluate(rule)
                if text:
                    await self.notify(rule.actor, text)
                    sent.append(text)
            except Exception:
                log.exception("Regel %s fehlgeschlagen", rule.kind)
        return sent

    async def _evaluate(self, rule: Rule) -> str | None:
        if rule.kind == "late":
            return (f"Es ist nach {rule.at} Uhr und Sie sind noch hier. Darf ich zum Feierabend raten?"
                    if self.online(rule.actor) else None)
        data = await self.forecast()
        week = (data or {}).get("week") or (data or {}).get("forecast") or []
        index = 0 if rule.at < "12:00" else 1
        if index >= len(week):
            return None
        day, label = week[index], "Heute" if index == 0 else "Morgen"
        if rule.kind == "rain":
            p = day.get("precipitation_probability_pct") or 0
            return f"{label} wird es voraussichtlich regnen ({p} Prozent) – denken Sie an den Schirm." if p >= 50 else None
        low = day.get("temp_min_c")
        if isinstance(low, int | float) and low <= 0:
            return f"{label} gibt es Frost ({round(low)} Grad) – warm anziehen, und Pflanzen reinholen."
        return None

    async def run(self, now: Callable[[], datetime], interval: float = 60.0) -> None:
        while True:
            if self.items:
                await self.check(now())
            await asyncio.sleep(interval)


def register_rule_capabilities(registry: ToolRegistry, service: RuleService) -> None:
    async def add(args: dict[str, Any], ctx: InvocationContext) -> Any:
        rule = service.add(ctx.actor, args["kind"], args.get("at"))
        return {"id": rule.id, "describe": rule.describe()}

    async def listing(args: dict[str, Any], ctx: InvocationContext) -> Any:
        return {"rules": [r.describe() for r in service.mine(ctx.actor)]}

    async def cancel(args: dict[str, Any], ctx: InvocationContext) -> Any:
        return {"cancelled": service.cancel(ctx.actor)}

    registry.register(Capability(
        name="automation.rule_add", domain="automation", risk_class="R0", side_effects="reversible",
        description="Wenn-dann-Regel: täglich zur Uhrzeit (at, HH:MM) prüfen und sich melden, wenn es regnet (rain), "
                    "friert (frost) oder der Nutzer noch am Rechner ist (late).",
        input_schema={"type": "object", "additionalProperties": False, "required": ["kind"], "properties": {
            "kind": {"enum": ["rain", "frost", "late"]}, "at": {"type": "string", "maxLength": 5}}},
        handler=add))
    registry.register(Capability(
        name="automation.rule_list", domain="automation", risk_class="R0", description="Nennt die Wenn-dann-Regeln.",
        input_schema={"type": "object", "additionalProperties": False, "properties": {}}, handler=listing))
    registry.register(Capability(
        name="automation.rule_cancel", domain="automation", risk_class="R0", side_effects="reversible",
        description="Löscht alle Wenn-dann-Regeln des Nutzers.",
        input_schema={"type": "object", "additionalProperties": False, "properties": {}}, handler=cancel))


