"""Lokaler Provider: Ollama (native ``/api/chat``-Schnittstelle mit Tool-Calling und Streaming)."""

from __future__ import annotations

import json
from typing import Any

from ..errors import JarvisError
from ..events import new_id
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


class OllamaProvider:
    name = "ollama"
    is_local = True

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        model: str = "qwen2.5:14b-instruct",
        num_ctx: int = 32768,
        timeout_s: float = 60.0,
        client: Any = None,
    ) -> None:
        import httpx  # optionale Abhängigkeit

        self._httpx = httpx
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout_s)
        self.model = model
        self.num_ctx = num_ctx

    def _render(self, system: SystemPrompt, transcript: list[Turn]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": f"{system.static}\n\n{system.dynamic}".strip()}
        ]
        for turn in transcript:
            if isinstance(turn, UserTurn):
                messages.append({"role": "user", "content": turn.text})
            elif isinstance(turn, AssistantTurn):
                msg: dict[str, Any] = {"role": "assistant", "content": turn.text}
                if turn.tool_calls:
                    msg["tool_calls"] = [
                        {"function": {"name": c.name, "arguments": c.arguments}} for c in turn.tool_calls
                    ]
                messages.append(msg)
            elif isinstance(turn, ToolResultsTurn):
                for r in turn.results:
                    messages.append({"role": "tool", "content": r.content, "tool_name": r.name})
        return messages

    async def complete(
        self,
        *,
        system: SystemPrompt,
        transcript: list[Turn],
        tools: list[ToolSpec],
        on_text: OnText | None = None,
        effort: str | None = None,  # lokal ohne Wirkung
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._render(system, transcript),
            "stream": True,
            "options": {"num_ctx": self.num_ctx, "temperature": 0.3},
        }
        if tools:
            payload["tools"] = [
                {"type": "function",
                 "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
                for t in tools
            ]

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        done_reason = None
        usage: dict[str, int] = {}
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    message = chunk.get("message") or {}
                    if message.get("content"):
                        text_parts.append(message["content"])
                        if on_text is not None:
                            await on_text(message["content"])
                    for tc in message.get("tool_calls") or []:
                        args = tc["function"].get("arguments") or {}
                        if isinstance(args, str):  # manche Modelle liefern JSON als String
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                pass  # bleibt String -> Schema-Prüfung im Orchestrator schlägt fehl
                        calls.append(ToolCall(id=new_id("call"), name=tc["function"]["name"], arguments=args))
                    if chunk.get("done"):
                        done_reason = chunk.get("done_reason")
                        usage = {
                            "input_tokens": chunk.get("prompt_eval_count", 0),
                            "output_tokens": chunk.get("eval_count", 0),
                        }
        except self._httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise JarvisError("JRV-LLM-001", f"Modell {self.model} ist nicht geladen",
                                  user_message=f"Das lokale Modell fehlt noch: 'ollama pull {self.model}' ausführen.") from exc
            raise JarvisError("JRV-LLM-001", f"Lokales Modell antwortet mit HTTP {exc.response.status_code}") from exc
        except self._httpx.HTTPError as exc:
            raise JarvisError("JRV-LLM-001", f"Lokales Modell nicht erreichbar: {exc}") from exc

        text = "".join(text_parts)
        stop: StopReason = "tool_use" if calls else ("max_tokens" if done_reason == "length" else "end_turn")
        return LLMResponse(
            text=text,
            tool_calls=calls,
            stop_reason=stop,
            assistant_turn=AssistantTurn(text=text, tool_calls=calls, provider=self.name),
            usage=usage,
            model=self.model,
        )
