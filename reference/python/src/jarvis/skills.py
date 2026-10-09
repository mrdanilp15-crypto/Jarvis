"""Assistenz-Skills: Systemstatus und Tagesplan.

Beide melden nur, was tatsächlich geprüft wurde. Der Tagesplan behauptet keine Termine oder Optimierungen, solange
kein Kalender verbunden ist – er nennt dann ehrlich das Fehlende und liefert, was verfügbar ist (Wetter).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import Any

from .errors import JarvisError
from .tools import Capability, InvocationContext, ToolRegistry

Probes = Callable[[], dict[str, str]]  # Komponente -> ok | loading | missing | unavailable | disconnected
Weather = Callable[[dict[str, Any], InvocationContext], Awaitable[Any]]
Calendar = Callable[[date, InvocationContext], Awaitable[list[str]]]


def register_assistant_capabilities(registry: ToolRegistry, *, probes: Probes, weather: Weather | None = None,
                                    calendar: Calendar | None = None) -> None:
    async def status(args: dict[str, Any], ctx: InvocationContext) -> Any:
        return {"components": probes()}

    async def day_plan(args: dict[str, Any], ctx: InvocationContext) -> Any:
        day = args.get("day", "tomorrow")
        target = date.today() + timedelta(days=1 if day == "tomorrow" else 0)
        result: dict[str, Any] = {"day": day, "date": target.isoformat(),
                                  "events": await calendar(target, ctx) if calendar else None}
        location = args.get("location")
        if weather is None:
            return result
        if not location:
            result["weather_note"] = "Für eine Wettervorschau nennen Sie mir bitte Ihren Ort."
            return result
        try:
            data = await weather({"location": location, "days": 2}, ctx)
        except JarvisError:
            result["weather_note"] = "Die Wettervorschau ist gerade nicht erreichbar."
            return result
        if "error" in data:
            result["weather_note"] = data["error"]
            return result
        forecast = data.get("forecast") or []
        index = 1 if day == "tomorrow" else 0
        if len(forecast) > index:
            result["forecast"], result["location"] = forecast[index], data.get("location", location)
        return result

    registry.register(Capability(
        name="system.status", domain="system", risk_class="R0",
        description="Prüft den Zustand von JARVIS (Sprachmodell, PC-Steuerung, Stimme) und meldet Einschränkungen.",
        input_schema={"type": "object", "additionalProperties": False, "properties": {}},
        handler=status,
    ))
    registry.register(Capability(
        name="assistant.day_plan", domain="assistant", risk_class="R0", timeout_s=25.0,
        description="Tagesübersicht für heute oder morgen: Termine (falls ein Kalender verbunden ist) und Wetter.",
        input_schema={"type": "object", "additionalProperties": False, "properties": {
            "day": {"enum": ["today", "tomorrow"]},
            "location": {"type": "string", "minLength": 2, "maxLength": 100},
        }},
        handler=day_plan,
    ))
