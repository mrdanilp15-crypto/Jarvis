"""Test-Doubles: simuliertes Home Assistant und ein skriptbarer LLM-Provider (für Tests und Demo)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .events import new_id
from .llm.base import AssistantTurn, LLMResponse, OnText, SystemPrompt, ToolCall, ToolSpec, Turn


class FakeHome:
    """Implementiert ``HomeApi``: Service-Aufrufe ändern den Zustand sofort."""

    def __init__(self, states: dict[str, dict[str, Any]]) -> None:
        self.states = states
        self.calls: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []

    def state(self, entity_id: str) -> dict[str, Any] | None:
        return self.states.get(entity_id)

    async def call_service(self, domain: str, service: str, *, target: dict[str, Any],
                           data: dict[str, Any] | None = None) -> Any:
        data = data or {}
        self.calls.append((domain, service, target, data))
        ids = target["entity_id"] if isinstance(target["entity_id"], list) else [target["entity_id"]]
        for eid in ids:
            st = self.states.setdefault(eid, {"state": "unknown", "attributes": {}})
            attrs = st.setdefault("attributes", {})
            if (domain, service) in (("light", "turn_on"), ("switch", "turn_on")):
                st["state"] = "on"
                if "brightness_pct" in data:
                    attrs["brightness"] = round(data["brightness_pct"] / 100 * 255)
            elif (domain, service) in (("light", "turn_off"), ("switch", "turn_off")):
                st["state"] = "off"
            elif domain == "lock":
                st["state"] = "locked" if service == "lock" else "unlocked"
            elif (domain, service) == ("cover", "set_cover_position"):
                attrs["current_position"] = data["position"]
                st["state"] = "open" if data["position"] > 0 else "closed"
            elif (domain, service) == ("climate", "set_temperature"):
                attrs["temperature"] = data["temperature"]
            elif (domain, service) == ("climate", "set_hvac_mode"):
                st["state"] = data["hvac_mode"]
        return []


def say(text: str) -> LLMResponse:
    return LLMResponse(text=text, tool_calls=[], stop_reason="end_turn",
                       assistant_turn=AssistantTurn(text=text, tool_calls=[], provider="scripted"))


def call_tool(name: str, arguments: Any, text: str = "") -> LLMResponse:
    call = ToolCall(id=new_id("toolu"), name=name, arguments=arguments)
    return LLMResponse(text=text, tool_calls=[call], stop_reason="tool_use",
                       assistant_turn=AssistantTurn(text=text, tool_calls=[call], provider="scripted"))


Step = LLMResponse | Callable[[list[Turn]], LLMResponse]


class ScriptedProvider:
    """Gibt vorab festgelegte Antworten zurück und protokolliert, was das LLM gesehen hätte."""

    name = "scripted"
    is_local = True

    def __init__(self, steps: list[Step]) -> None:
        self.steps = list(steps)
        self.requests: list[dict[str, Any]] = []

    async def complete(self, *, system: SystemPrompt, transcript: list[Turn], tools: list[ToolSpec],
                       on_text: OnText | None = None, effort: str | None = None) -> LLMResponse:
        self.requests.append({"system": system, "transcript": list(transcript), "tools": tools, "effort": effort})
        if not self.steps:
            raise AssertionError("ScriptedProvider: keine Antworten mehr")
        step = self.steps.pop(0)
        response = step(transcript) if callable(step) else step
        if on_text is not None and response.text:
            await on_text(response.text)
        return response
