"""Anbieterneutrales Transkript und Provider-Schnittstelle.

Jeder Provider rendert das neutrale Transkript in sein Wire-Format. Antworten eines Providers werden als
``AssistantTurn.raw`` aufbewahrt und an *denselben* Provider unverändert zurückgegeben (wichtig z. B. für
Denk-Blöcke bei Claude); andere Provider rendern aus ``text`` und ``tool_calls``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: Any  # wird vor der Ausführung gegen das Schema geprüft


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass
class UserTurn:
    text: str
    # Situation/Erinnerungen zum Zeitpunkt der Frage. Lokale Modelle bekommen sie mit der Nachricht, damit der
    # gerenderte Verlauf unverändert bleibt und Ollama ihn nicht bei jeder Frage neu durchrechnen muss.
    context: str = ""
    # Nachgeschlagene Quellen zu dieser Frage (Wikipedia, Websuche) – jedes Modell bekommt sie direkt vor der Frage
    sources: str = ""

    def with_sources(self) -> str:
        return f"{self.sources}\n\n{self.text}" if self.sources else self.text


@dataclass
class AssistantTurn:
    text: str
    tool_calls: list[ToolCall]
    provider: str
    raw: Any = None


@dataclass
class ToolResultsTurn:
    results: list[ToolResult]


Turn = UserTurn | AssistantTurn | ToolResultsTurn

StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal"]


@dataclass
class SystemPrompt:
    static: str  # Regeln + Persona: stabil, cachebar
    dynamic: str = ""  # Situation, Erinnerungen, Zusammenfassung: pro Turn


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: StopReason
    assistant_turn: AssistantTurn
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""


OnText = Callable[[str], Awaitable[None]]


class LLMProvider(Protocol):
    name: str
    is_local: bool  # steuert, ob sensible Erinnerungen in den Kontext dürfen

    async def complete(
        self,
        *,
        system: SystemPrompt,
        transcript: list[Turn],
        tools: list[ToolSpec],
        on_text: OnText | None = None,
        effort: str | None = None,
    ) -> LLMResponse: ...
