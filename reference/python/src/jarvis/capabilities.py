"""Kern-Capabilities, die nicht von einem Connector stammen: Gedächtnis und Timer."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .memory import MemoryService
from .tools import Capability, InvocationContext, ToolRegistry

TimerCallback = Callable[[str, int, InvocationContext], Awaitable[None]]  # timer_id, seconds, ctx


def _user_id(ctx: InvocationContext) -> str | None:
    return ctx.actor.split(":", 1)[1] if ctx.actor.startswith("user:") else None


def register_memory_capabilities(registry: ToolRegistry, memory: MemoryService) -> None:
    async def remember(args: dict[str, Any], ctx: InvocationContext) -> Any:
        item = await memory.remember(args["content"], user_id=None if args.get("household") else _user_id(ctx),
                                     kind=args.get("kind", "semantic"), source_type="user_explicit",
                                     sensitivity=args.get("sensitivity", "personal"))
        return {"memory_id": item.id}

    async def search(args: dict[str, Any], ctx: InvocationContext) -> Any:
        return {"memories": await memory.recall(args["query"], user_id=_user_id(ctx), top_k=args.get("top_k", 5))}

    registry.register(Capability(
        name="memory.remember", domain="memory", risk_class="R1", side_effects="reversible",
        description="Speichert einen Fakt oder eine Präferenz dauerhaft, wenn der Nutzer ausdrücklich darum bittet.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["content"], "properties": {
            "content": {"type": "string", "minLength": 3, "maxLength": 1000},
            "kind": {"enum": ["semantic", "preference", "episodic", "procedural"]},
            "sensitivity": {"enum": ["public", "personal", "sensitive"]},
            "household": {"type": "boolean", "description": "true = für alle Haushaltsmitglieder sichtbar"},
        }},
        handler=remember,
    ))
    async def list_all(args: dict[str, Any], ctx: InvocationContext) -> Any:
        items = await memory.everything(user_id=_user_id(ctx), limit=args.get("limit", 10))
        return {"memories": [{"id": i.id, "content": i.content, "kind": i.kind,
                              "created": i.created_at.date().isoformat()} for i in items]}

    async def forget(args: dict[str, Any], ctx: InvocationContext) -> Any:
        item = await memory.forget_matching(args["query"], user_id=_user_id(ctx))
        return {"forgotten": item.content if item else None}

    registry.register(Capability(
        name="memory.list", domain="memory", risk_class="R0",
        description="Listet, was JARVIS sich über den Nutzer und den Haushalt gemerkt hat (neueste zuerst).",
        input_schema={"type": "object", "additionalProperties": False, "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        }},
        handler=list_all,
    ))
    registry.register(Capability(
        name="memory.forget", domain="memory", risk_class="R1", side_effects="irreversible",
        description="Löscht den gemerkten Eintrag, der am besten zur Beschreibung passt („Vergiss, dass …“).",
        input_schema={"type": "object", "additionalProperties": False, "required": ["query"], "properties": {
            "query": {"type": "string", "minLength": 3, "maxLength": 500},
        }},
        handler=forget,
    ))
    registry.register(Capability(
        name="memory.search", domain="memory", risk_class="R0",
        description="Durchsucht das Langzeitgedächtnis nach Fakten, Präferenzen und früheren Ereignissen.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["query"], "properties": {
            "query": {"type": "string", "minLength": 2},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
        }},
        handler=search,
    ))


def register_timer_capability(registry: ToolRegistry, on_elapsed: TimerCallback) -> None:
    tasks: dict[str, asyncio.Task[None]] = {}

    async def start(args: dict[str, Any], ctx: InvocationContext) -> Any:
        timer_id = f"tmr_{len(tasks) + 1}"

        async def run() -> None:
            await asyncio.sleep(args["duration_s"])
            await on_elapsed(timer_id, args["duration_s"], ctx)

        # Im Betrieb persistiert der Scheduler Timer in der Tabelle tasks (überlebt Neustarts).
        tasks[timer_id] = asyncio.create_task(run())
        return {"timer_id": timer_id, "duration_s": args["duration_s"], "label": args.get("label")}

    registry.register(Capability(
        name="timer.start", domain="home", risk_class="R1", side_effects="reversible",
        description="Startet einen Timer im aktuellen Raum; bei Ablauf erfolgt eine Ansage.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["duration_s"], "properties": {
            "duration_s": {"type": "integer", "minimum": 1, "maximum": 86400},
            "label": {"type": "string", "maxLength": 60},
        }},
        handler=start,
    ))
