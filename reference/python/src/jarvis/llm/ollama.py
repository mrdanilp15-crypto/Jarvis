"""Lokaler Provider: Ollama (native ``/api/chat``-Schnittstelle mit Tool-Calling und Streaming)."""

from __future__ import annotations

import json
import re
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


# Kleine lokale Modelle schreiben Tool-Aufrufe manchmal als Text statt über die Tool-Schnittstelle:
# {"name": "pc.open_folder", "arguments": {...}}, <tool_call>…</tool_call> oder ```json …```.
_CALL_MARKER = re.compile(r'<tool_call>|```(?:json)?\s*\{|\{\s*"(?:name|function)"\s*:')


def extract_text_tool_calls(text: str, known: set[str]) -> tuple[list[ToolCall], str]:
    """Tool-Aufrufe im Text finden (nur bekannte Tools) und aus dem Text entfernen.

    Übrig bleibender Rest von höchstens drei Wörtern gilt als Beiwerk des Modells („Dorf.“) und fällt weg.
    """
    decoder = json.JSONDecoder()
    calls: list[ToolCall] = []
    spans: list[tuple[int, int]] = []
    index = 0
    while (start := text.find("{", index)) != -1:
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        index = end
        if not isinstance(obj, dict):
            continue
        function = obj.get("function") if isinstance(obj.get("function"), dict) else obj
        name = str(function.get("name") or "").strip().replace(".", "__")
        args = function.get("arguments", function.get("parameters", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                continue
        if not name or not isinstance(args, dict):
            continue
        spans.append((start, end))  # auch unbekannte Aufrufe nie vorlesen
        if name in known:
            calls.append(ToolCall(id=new_id("call"), name=name, arguments=args))
    if not spans:
        return [], text
    rest = "".join(text[a:b] for a, b in zip([0] + [e for _, e in spans], [s for s, _ in spans] + [len(text)]))
    rest = re.sub(r"</?tool_call>|```(?:json)?", " ", rest)
    rest = re.sub(r"\s+", " ", rest).strip(" .,:;")
    if not calls:
        return [], rest
    return calls, ("" if len(rest.split()) <= 3 else rest)


class _StreamGate:
    """Leitet gestreamten Text erst weiter, wenn klar ist, dass kein Tool-Aufruf als Text kommt – sonst würde
    JARVIS JSON vorlesen. Die ersten Zeichen werden kurz zurückgehalten, ab einem Tool-Aufruf-Muster nichts mehr."""

    HOLD = 40

    def __init__(self, on_text: OnText | None) -> None:
        self.on_text = on_text
        self.buffer = ""
        self.tail = ""
        self.open = False
        self.blocked = False

    async def feed(self, delta: str) -> None:
        if self.on_text is None or self.blocked:
            return
        if not self.open:
            self.buffer += delta
            if _CALL_MARKER.search(self.buffer) or self.buffer.lstrip().startswith(("{", "<tool", "```")):
                self.blocked = True
            elif len(self.buffer.strip()) >= self.HOLD:
                self.open = True
                await self._forward(self.buffer)
            return
        combined = self.tail + delta
        if m := _CALL_MARKER.search(combined):
            self.blocked = True
            if (cut := m.start() - len(self.tail)) > 0:
                await self.on_text(delta[:cut])
            return
        await self._forward(delta)

    async def _forward(self, text: str) -> None:
        self.tail = (self.tail + text)[-30:]
        await self.on_text(text)

    async def finish(self, *, speak_rest: bool) -> None:
        if self.on_text is not None and speak_rest and not self.open and not self.blocked and self.buffer:
            await self.on_text(self.buffer)


class OllamaProvider:
    name = "ollama"
    is_local = True

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        model: str = "qwen2.5:14b-instruct",
        num_ctx: int = 8192,
        timeout_s: float = 180.0,
        keep_alive: str | int = "24h",
        client: Any = None,
    ) -> None:
        import httpx  # optionale Abhängigkeit

        self._httpx = httpx
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout_s)
        self.model = model
        self.num_ctx = num_ctx  # größerer Kontext = mehr Speicher und langsamer, vor allem ohne GPU
        self.keep_alive = keep_alive  # Modell im Speicher halten; Standard von Ollama wären 5 Minuten

    def _render(self, system: SystemPrompt, transcript: list[Turn]) -> list[dict[str, Any]]:
        # Ollama rechnet nur den Teil des Prompts neu, der sich gegenüber der letzten Anfrage geändert hat
        # (KV-Cache). Deshalb bleibt vorne nur der stabile Teil (Regeln, Persona; danach die Tools), und der
        # dynamische Teil (Uhrzeit, Erinnerungen) steht in der Nutzernachricht, zu der er gehört – jede frühere
        # Nachricht behält ihren Kontext von damals. So bleibt der gerenderte Verlauf Zeichen für Zeichen gleich
        # und pro Frage kommen nur die neuen Tokens hinzu. Stünde der Kontext im System-Prompt, müsste das Modell
        # bei jeder Frage System-Prompt, Tools und Verlauf komplett neu lesen.
        current = max((i for i, t in enumerate(transcript) if isinstance(t, UserTurn)), default=None)
        system_text = system.static if current is not None else f"{system.static}\n\n{system.dynamic}"
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_text.strip()}]
        for i, turn in enumerate(transcript):
            if isinstance(turn, UserTurn):
                context = turn.context or (system.dynamic if i == current else "")
                text = f"{context}\n\n{turn.with_sources()}" if context else turn.with_sources()
                messages.append({"role": "user", "content": text})
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
        payload = self._payload(self._render(system, transcript), tools, stream=True)
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        gate = _StreamGate(on_text)
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
                        await gate.feed(message["content"])
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
        except self._httpx.HTTPError as exc:
            raise self._error(exc) from exc

        text = "".join(text_parts)
        from_text = False
        if not calls and tools:
            calls, text = extract_text_tool_calls(text, {t.name for t in tools})
            from_text = bool(calls)
        await gate.finish(speak_rest=not from_text)  # „Einen Moment.“ vor einem echten Tool-Aufruf darf gesprochen werden
        stop: StopReason = "tool_use" if calls else ("max_tokens" if done_reason == "length" else "end_turn")
        return LLMResponse(
            text=text,
            tool_calls=calls,
            stop_reason=stop,
            assistant_turn=AssistantTurn(text=text, tool_calls=calls, provider=self.name),
            usage=usage,
            model=self.model,
        )

    async def warm_up(self, system_static: str, tools: list[ToolSpec]) -> None:
        """Lädt das Modell und rechnet System-Prompt und Tools einmal vorab durch (landet im KV-Cache).
        Damit ist schon die erste Frage nach dem Start schnell. Wirft ``JarvisError`` wie ``complete``."""
        messages = [{"role": "system", "content": system_static.strip()}, {"role": "user", "content": "."}]
        payload = self._payload(messages, tools, stream=False)
        payload["options"]["num_predict"] = 1
        try:
            response = await self._client.post("/api/chat", json=payload)
            response.raise_for_status()
        except self._httpx.HTTPError as exc:
            raise self._error(exc) from exc

    def _payload(self, messages: list[dict[str, Any]], tools: list[ToolSpec], *, stream: bool) -> dict[str, Any]:
        # num_ctx muss bei allen Anfragen gleich sein – ein anderer Wert lädt das Modell neu
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "keep_alive": self.keep_alive,
            "options": {"num_ctx": self.num_ctx, "temperature": 0.3},
        }
        if tools:
            payload["tools"] = [
                {"type": "function",
                 "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
                for t in tools
            ]
        return payload

    def _error(self, exc: Exception) -> JarvisError:
        if isinstance(exc, self._httpx.HTTPStatusError):
            if exc.response.status_code == 404:
                return JarvisError("JRV-LLM-001", f"Modell {self.model} ist nicht geladen",
                                   user_message=f"Das lokale Modell fehlt noch: 'ollama pull {self.model}' ausführen.")
            return JarvisError("JRV-LLM-001", f"Lokales Modell antwortet mit HTTP {exc.response.status_code}")
        return JarvisError("JRV-LLM-001", f"Lokales Modell nicht erreichbar: {exc}")
