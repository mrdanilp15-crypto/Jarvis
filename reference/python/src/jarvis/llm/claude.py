"""Cloud-Provider: Anthropic Claude über das offizielle SDK.

- Streaming (Text-Deltas gehen sofort an ``on_text`` -> satzweise TTS)
- adaptives Denken + Aufwandssteuerung über ``output_config.effort``
- Prompt-Caching: Tools + statischer Systemprompt bilden den cachebaren Präfix
- ``eager_input_streaming`` für Tools; die Eingaben werden danach im Orchestrator gegen das Schema geprüft
- serverseitiger Refusal-Fallback (Beta ``server-side-fallback-2026-07-01``)
- Denk-Blöcke sind an Modell und Gesprächsverlauf gebunden. Der Situationsteil des System-Prompts (Uhrzeit) ändert
  sich bei jeder Frage, und alte Runden werden vorne gekürzt – deshalb gehen Denk-Blöcke nur innerhalb der laufenden
  Runde (Tool-Schleife) zurück, frühere nicht. Passt trotzdem einer nicht mehr, verwirft die API ihn statt mit 400
  abzulehnen (``prefix_mismatch_behavior: drop_block``, Beta ``thinking-binding-controls-2026-08-01``).
"""

from __future__ import annotations

from typing import Any

from ..errors import JarvisError
from .base import (
    AssistantTurn,
    LLMResponse,
    OnText,
    StopReason,
    SystemPrompt,
    ToolCall,
    ToolResultsTurn,
    ToolSpec,
    Turn,
    UserTurn,
)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
BINDING_BETA = "thinking-binding-controls-2026-08-01"
_THINKING = ("thinking", "redacted_thinking")

# Auswahl in der Oberfläche: (Modell-ID, Name, Einordnung, $ pro Mio. Eingabe-/Ausgabe-Tokens)
MODELS = [
    ("claude-opus-5-5", "Claude Opus 5.5", "empfohlen", 4.0, 20.0),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5", "schnell und günstiger", 2.0, 10.0),
    ("claude-fable-5-1", "Claude Fable 5.1", "stärkstes Modell, teuer", 10.0, 50.0),
]
DEFAULT_MODEL = MODELS[0][0]


class ClaudeProvider:
    name = "claude"
    is_local = False

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 64000,
        default_effort: str = "medium",
        server_side_fallbacks: bool = True,
        api_key: str | None = None,
        client: Any = None,
    ) -> None:
        import anthropic  # optionale Abhängigkeit

        self._anthropic = anthropic
        # Ohne expliziten Schlüssel: Credentials aus Umgebung bzw. ant-Profil
        self._client = client or (anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic())
        self.model = model
        self.max_tokens = max_tokens
        self.default_effort = default_effort
        self.server_side_fallbacks = server_side_fallbacks

    # ------------------------------------------------------------------
    def _render_messages(self, transcript: list[Turn]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        current = max((i for i, t in enumerate(transcript) if isinstance(t, UserTurn)), default=-1)
        for i, turn in enumerate(transcript):
            if isinstance(turn, UserTurn):
                messages.append({"role": "user", "content": turn.with_sources()})
            elif isinstance(turn, AssistantTurn):
                if turn.provider == self.name and turn.raw is not None:
                    # Laufende Runde: unverändert zurückgeben (Denk-Blöcke bleiben an die Tool-Schleife gebunden).
                    # Frühere Runden ohne Denk-Blöcke – ihr Verlauf hat sich seitdem geändert (Situation, Kürzung).
                    content = turn.raw if i > current else [b for b in turn.raw if _block_type(b) not in _THINKING]
                    if content:
                        messages.append({"role": "assistant", "content": content})
                        continue
                content: list[dict[str, Any]] = []
                if turn.text:
                    content.append({"type": "text", "text": turn.text})
                for call in turn.tool_calls:
                    content.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments})
                if content:  # leere Assistenten-Nachrichten lehnt die API ab; Nutzer-Nachrichten verschmelzen dann
                    messages.append({"role": "assistant", "content": content})
            elif isinstance(turn, ToolResultsTurn):
                # Alle Ergebnisse eines Schritts in *einer* Nachricht zurückgeben
                messages.append({
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content, "is_error": r.is_error}
                        for r in turn.results
                    ],
                })
        return messages

    def _render_system(self, system: SystemPrompt) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = [
            # Cache-Breakpoint: deckt Tools (werden vorher gerendert) und statischen Prompt ab
            {"type": "text", "text": system.static, "cache_control": {"type": "ephemeral"}},
        ]
        if system.dynamic:
            blocks.append({"type": "text", "text": system.dynamic})  # volatil -> hinter dem Breakpoint
        return blocks

    # ------------------------------------------------------------------
    async def complete(
        self,
        *,
        system: SystemPrompt,
        transcript: list[Turn],
        tools: list[ToolSpec],
        on_text: OnText | None = None,
        effort: str | None = None,
    ) -> LLMResponse:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self._render_system(system),
            "messages": self._render_messages(transcript),
            "thinking": {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
            "output_config": {"effort": effort or self.default_effort},
            "betas": [BINDING_BETA],
        }
        if tools:
            params["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                    "eager_input_streaming": True,
                }
                for t in tools
            ]
        if self.server_side_fallbacks:
            params["betas"].append(FALLBACK_BETA)
            params["fallbacks"] = "default"

        message = await self._stream_with_json_retry(params, on_text)

        tool_calls = [
            ToolCall(id=block.id, name=block.name, arguments=block.input)
            for block in message.content
            if block.type == "tool_use"
        ]
        text = "".join(block.text for block in message.content if block.type == "text")
        stop: StopReason
        if message.stop_reason == "refusal":
            stop = "refusal"
        elif message.stop_reason == "max_tokens":
            stop = "max_tokens"
        elif tool_calls:
            stop = "tool_use"
        else:
            stop = "end_turn"

        usage = message.usage
        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop,
            assistant_turn=AssistantTurn(text=text, tool_calls=tool_calls, provider=self.name, raw=message.content),
            usage={
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_input_tokens or 0,
                "cache_write_tokens": usage.cache_creation_input_tokens or 0,
            },
            model=message.model,
        )

    async def check(self) -> str:
        """Schlüssel und Modellzugang prüfen, ohne Tokens zu verbrauchen (Models-API). Gibt den Anzeigenamen zurück."""
        anthropic = self._anthropic
        try:
            model = await self._client.models.retrieve(self.model)
        except anthropic.AuthenticationError as exc:
            raise JarvisError("JRV-LLM-003", "API-Schlüssel ungültig",
                              user_message="Dieser API-Schlüssel wird von Anthropic nicht angenommen.") from exc
        except anthropic.PermissionDeniedError as exc:
            raise JarvisError("JRV-LLM-003", "Keine Berechtigung",
                              user_message="Der Schlüssel hat keinen Zugriff auf dieses Modell.") from exc
        except anthropic.NotFoundError as exc:
            raise JarvisError("JRV-LLM-003", f"Modell {self.model} unbekannt",
                              user_message=f"Das Modell {self.model} ist für diesen Schlüssel nicht verfügbar.") from exc
        except anthropic.APIStatusError as exc:
            raise JarvisError("JRV-LLM-001", f"HTTP {exc.status_code}",
                              user_message="Anthropic antwortet gerade nicht. Bitte später erneut versuchen.") from exc
        except anthropic.APIConnectionError as exc:
            raise JarvisError("JRV-LLM-001", "Verbindung fehlgeschlagen",
                              user_message="Anthropic ist nicht erreichbar. Besteht eine Internetverbindung?") from exc
        return str(getattr(model, "display_name", None) or self.model)

    async def _stream_with_json_retry(self, params: dict[str, Any], on_text: OnText | None) -> Any:
        anthropic = self._anthropic
        for attempt in range(3):
            try:
                async with self._client.beta.messages.stream(**params) as stream:
                    async for event in stream:
                        if event.type == "text" and on_text is not None:
                            await on_text(event.text)
                    return await stream.get_final_message()
            except ValueError:
                # Mit eager_input_streaming kann ein Tool-Input unparsebar sein; es gibt dann keine
                # tool_use_id, auf die man antworten könnte -> Turn neu anfordern (begrenzt).
                if attempt == 2:
                    raise JarvisError("JRV-LLM-003", "Tool-Eingabe wiederholt nicht parsebar") from None
            except anthropic.RateLimitError as exc:
                retry_after = exc.response.headers.get("retry-after")
                raise JarvisError(
                    "JRV-RATE-001", "Anthropic Rate-Limit",
                    retry_after_ms=int(float(retry_after) * 1000) if retry_after else None,
                ) from exc
            except anthropic.BadRequestError as exc:
                raise JarvisError("JRV-LLM-003", exc.message) from exc
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
                raise JarvisError("JRV-LLM-003", "Zugangsdaten für das Cloud-Modell ungültig") from exc
            except anthropic.APIStatusError as exc:
                raise JarvisError("JRV-LLM-001", f"HTTP {exc.status_code}") from exc
            except anthropic.APIConnectionError as exc:
                raise JarvisError("JRV-LLM-001", "Verbindung zum Cloud-Modell fehlgeschlagen") from exc
        raise AssertionError("unreachable")


def _block_type(block: Any) -> str | None:
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
