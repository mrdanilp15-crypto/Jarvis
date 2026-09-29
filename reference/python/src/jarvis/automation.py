"""Automations-Engine: Trigger -> Bedingungen -> Aktionen (schemas/automation.schema.json).

Bedingungen werden rein funktional gegen einen Zustands-Snapshot ausgewertet (testbar, deterministisch).
Capability-Aktionen laufen über ``ActionGateway.request_action`` und damit durch die Policy-Engine.
Zeit-, Cron- und Sonnen-Trigger feuert der Scheduler als Event ``jarvis.schedule.fired``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from .events import CloudEvent, new_id
from .policy import PolicyContext, Principal, in_time_window

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass(frozen=True)
class EvalContext:
    states: Mapping[str, Mapping[str, Any]]  # entity_id -> {"state": ..., "attributes": {...}}
    now: datetime  # lokale Zeit
    presence: Mapping[str, bool] = field(default_factory=dict)  # person -> zu Hause?
    mode: str = "normal"


class ActionGateway(Protocol):
    async def request_action(self, *, capability: str, arguments: dict[str, Any], principal: Principal,
                             correlation_id: str, session_id: str | None, via: str, ctx: PolicyContext,
                             dry_run: bool = False) -> Any: ...


Notifier = Callable[[str, list[str], str, str], Awaitable[None]]  # message, channels, target, priority
LLMStep = Callable[[str, str, list[str]], Awaitable[str]]  # prompt, route, allow_tools -> text


# ----------------------------------------------------------------------------
# Bedingungen
# ----------------------------------------------------------------------------
def evaluate_condition(cond: Mapping[str, Any], ctx: EvalContext) -> bool:
    kind = cond["type"]
    if kind == "and":
        return all(evaluate_condition(c, ctx) for c in cond["conditions"])
    if kind == "or":
        return any(evaluate_condition(c, ctx) for c in cond["conditions"])
    if kind == "not":
        return not evaluate_condition(cond["condition"], ctx)
    if kind == "state":
        entity = ctx.states.get(cond["entity_id"])
        if entity is None:
            return False
        value = entity.get("attributes", {}).get(cond["attribute"]) if "attribute" in cond else entity.get("state")
        expected = cond["state"] if isinstance(cond["state"], list) else [cond["state"]]
        return str(value) in expected
    if kind == "numeric_state":
        entity = ctx.states.get(cond["entity_id"])
        if entity is None:
            return False
        raw = entity.get("attributes", {}).get(cond["attribute"]) if "attribute" in cond else entity.get("state")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return False  # unavailable/unknown zählt nie als erfüllt
        if "above" in cond and not value > cond["above"]:
            return False
        if "below" in cond and not value < cond["below"]:
            return False
        return True
    if kind == "time":
        if "weekdays" in cond and WEEKDAYS[ctx.now.weekday()] not in cond["weekdays"]:
            return False
        after, before = cond.get("after"), cond.get("before")
        if after and before:
            return in_time_window(ctx.now.time(), f"{after}-{before}")
        if after:
            return ctx.now.strftime("%H:%M") >= after
        if before:
            return ctx.now.strftime("%H:%M") < before
        return True
    if kind == "presence":
        person = cond["person"]  # Person oder "anyone"; "niemand zu Hause" = anyone + not_home
        is_home = any(ctx.presence.values()) if person == "anyone" else ctx.presence.get(person, False)
        return is_home if cond["state"] == "home" else not is_home
    if kind == "mode":
        return ctx.mode == cond["mode"]
    raise ValueError(f"unknown condition type {kind}")


# ----------------------------------------------------------------------------
# Trigger
# ----------------------------------------------------------------------------
def trigger_matches(trigger: Mapping[str, Any], event: CloudEvent) -> bool:
    kind = trigger["type"]
    data = event.data if isinstance(event.data, dict) else {}
    if kind == "state":
        if event.type != "jarvis.sensor.state_changed" or data.get("entity_id") != trigger["entity_id"]:
            return False
        old = (data.get("old_state") or {}).get("state")
        new = (data.get("new_state") or {}).get("state")
        if old == new:
            return False  # reine Attributänderung
        if "from" in trigger and old != trigger["from"]:
            return False
        if "to" in trigger and new != trigger["to"]:
            return False
        return True  # "for" (Mindestdauer) prüft der Scheduler über einen verzögerten Re-Check
    if kind == "event":
        if not fnmatch.fnmatchcase(event.type, trigger["event_type"]):
            return False
        return all(_dig(data, key) == value for key, value in trigger.get("match", {}).items())
    if kind == "presence":
        return (event.type == "jarvis.presence.changed" and data.get("person") == trigger["person"]
                and data.get("event") == trigger["event"] and data.get("zone", "home") == trigger.get("zone", "home"))
    if kind == "webhook":
        return event.type == "jarvis.webhook.received" and data.get("webhook_id") == trigger["webhook_id"]
    if kind in ("time", "cron", "sun"):
        return event.type == "jarvis.schedule.fired" and data.get("trigger_ref") == _trigger_ref(trigger)
    return False


def _trigger_ref(trigger: Mapping[str, Any]) -> str:
    return "|".join(f"{k}={trigger[k]}" for k in sorted(trigger))


def _dig(data: Mapping[str, Any], dotted: str) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(part)
    return cur


# ----------------------------------------------------------------------------
# Engine
# ----------------------------------------------------------------------------
@dataclass
class RunTrace:
    automation_id: str
    status: str  # succeeded | failed | skipped
    trigger: dict[str, Any]
    dry_run: bool
    steps: list[dict[str, Any]] = field(default_factory=list)
    correlation_id: str = field(default_factory=lambda: new_id("cor"))


class AutomationEngine:
    def __init__(
        self,
        automations: list[dict[str, Any]],
        *,
        gateway: ActionGateway,
        owner_roles: Mapping[str, str],  # user:alex -> adult
        notifier: Notifier | None = None,
        llm_step: LLMStep | None = None,
    ) -> None:
        self.automations = {a["id"]: a for a in automations}
        self.gateway = gateway
        self.owner_roles = owner_roles
        self.notifier = notifier
        self.llm_step = llm_step
        self._running: set[str] = set()

    def runnable(self, automation: Mapping[str, Any]) -> bool:
        approval = automation.get("approval", {}).get("status", "pending")
        return automation.get("enabled", False) and approval in ("approved", "not_required")

    async def on_event(self, event: CloudEvent, ctx: EvalContext) -> list[RunTrace]:
        traces = []
        for automation in self.automations.values():
            if not self.runnable(automation):
                continue
            trigger = next((t for t in automation["triggers"] if trigger_matches(t, event)), None)
            if trigger is not None:
                traces.append(await self.run(automation, trigger, ctx, cause=event))
        return traces

    async def run(self, automation: Mapping[str, Any], trigger: Mapping[str, Any], ctx: EvalContext, *,
                  cause: CloudEvent | None = None, dry_run: bool = False) -> RunTrace:
        trace = RunTrace(automation["id"], "succeeded", dict(trigger), dry_run,
                         correlation_id=(cause.correlationid or cause.id) if cause else new_id("cor"))
        mode = automation.get("mode", "single")
        if mode == "single" and automation["id"] in self._running:
            trace.status = "skipped"
            trace.steps.append({"step": "mode", "result": "already running"})
            return trace
        for i, cond in enumerate(automation.get("conditions", [])):
            ok = evaluate_condition(cond, ctx)
            trace.steps.append({"step": f"condition[{i}]", "type": cond["type"], "result": ok})
            if not ok:
                trace.status = "skipped"
                return trace
        self._running.add(automation["id"])
        try:
            variables: dict[str, str] = {}
            ok = await self._run_actions(automation, automation["actions"], ctx, trace, variables, dry_run)
            trace.status = "succeeded" if ok else "failed"
        finally:
            self._running.discard(automation["id"])
        return trace

    async def _run_actions(self, automation: Mapping[str, Any], actions: list[Mapping[str, Any]],
                           ctx: EvalContext, trace: RunTrace, variables: dict[str, str], dry_run: bool) -> bool:
        owner = automation.get("owner", "user:unknown")
        principal = Principal(actor=f"automation:{automation['id']}",
                              role=self.owner_roles.get(owner, "service"), trust="system")
        for action in actions:
            kind = action["type"]
            if kind == "capability":
                arguments = _render(action["arguments"], variables)
                record = await self.gateway.request_action(
                    capability=action["capability"], arguments=arguments, principal=principal,
                    correlation_id=trace.correlation_id, session_id=None, via="automation",
                    ctx=PolicyContext(mode=ctx.mode, now=ctx.now), dry_run=dry_run,
                )
                status = getattr(record, "status", "unknown")
                trace.steps.append({"step": "capability", "capability": action["capability"], "status": status})
                if status != "succeeded" and not action.get("continue_on_error", False):
                    return False
            elif kind == "delay":
                trace.steps.append({"step": "delay", "duration": action["duration"]})
                if not dry_run:
                    await asyncio.sleep(parse_duration(action["duration"]))
            elif kind == "notify":
                message = _render(action["message"], variables)
                trace.steps.append({"step": "notify", "message": message})
                if self.notifier is not None and not dry_run:
                    await self.notifier(message, action.get("channels", ["push"]),
                                        action.get("target", "household"), action.get("priority", "normal"))
            elif kind == "choose":
                branch = next((c for c in action["choices"]
                               if all(evaluate_condition(x, ctx) for x in c["conditions"])), None)
                chosen = branch["actions"] if branch else action.get("default", [])
                trace.steps.append({"step": "choose", "branch": "match" if branch else "default"})
                if not await self._run_actions(automation, chosen, ctx, trace, variables, dry_run):
                    return False
            elif kind == "llm_step":
                if self.llm_step is None or dry_run:
                    output = "[llm_step übersprungen]"
                else:
                    output = await self.llm_step(action["prompt"], action.get("route", "local_llm"),
                                                 action.get("allow_tools", []))
                if "output_var" in action:
                    variables[action["output_var"]] = output
                trace.steps.append({"step": "llm_step", "chars": len(output)})
            else:
                raise ValueError(f"unknown action type {kind}")
        return True


def parse_duration(value: str) -> float:
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", value)
    if not match or not any(match.groups()):
        raise ValueError(f"invalid duration {value!r}")
    h, m, s = (int(g) if g else 0 for g in match.groups())
    return h * 3600 + m * 60 + s


def _render(value: Any, variables: Mapping[str, str]) -> Any:
    """Minimal-Templating: {{ name }} -> Variable. Bewusst ohne Ausdrücke/Code-Ausführung."""
    if isinstance(value, str):
        return re.sub(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}", lambda m: variables.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _render(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, variables) for v in value]
    return value
