"""LLM-Adapter gegen echte Client-Bibliotheken mit simulierten Streams (kein Netzwerk, kein API-Schlüssel)."""

import asyncio
import json

import pytest

from jarvis.llm.base import AssistantTurn, SystemPrompt, ToolCall, ToolResult, ToolResultsTurn, ToolSpec, UserTurn

anthropic = pytest.importorskip("anthropic")
httpx2 = pytest.importorskip("httpx2")
httpx = pytest.importorskip("httpx")

from jarvis.llm.claude import ClaudeProvider  # noqa: E402
from jarvis.llm.ollama import OllamaProvider  # noqa: E402


def sse(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


STREAM = sse(
    {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5", "content": [],
        "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 1200, "output_tokens": 1, "cache_read_input_tokens": 1000,
                  "cache_creation_input_tokens": 0}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Sehr wohl."}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1,
     "content_block": {"type": "tool_use", "id": "toolu_1", "name": "home__set_light", "input": {}}},
    {"type": "content_block_delta", "index": 1,
     "delta": {"type": "input_json_delta", "partial_json": "{\"entity_ids\": [\"light.kueche\"], \"on\": true}"}},
    {"type": "content_block_stop", "index": 1},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
     "usage": {"output_tokens": 42}},
    {"type": "message_stop"},
)


def test_request_shape_and_stream_parsing():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        captured["headers"] = dict(request.headers)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=STREAM)

    client = anthropic.AsyncAnthropic(
        api_key="test", http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))
    provider = ClaudeProvider(client=client)
    deltas = []

    async def on_text(delta):
        deltas.append(delta)

    tool = ToolSpec("home__set_light", "Schaltet Lichter.", {"type": "object", "properties": {}})
    transcript = [
        UserTurn("Vorher"),
        AssistantTurn("", [ToolCall("call_x", "home__get_state", {"entity_ids": ["light.kueche"]})], "ollama"),
        ToolResultsTurn([ToolResult("call_x", "home__get_state", '{"state": "off"}')]),
        UserTurn("Licht an"),
    ]
    response = asyncio.run(provider.complete(system=SystemPrompt("REGELN", "SITUATION"), transcript=transcript,
                                             tools=[tool], on_text=on_text, effort="medium"))

    body = captured["body"]
    assert body["model"] == "claude-opus-5"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "medium"}
    assert body["system"][0] == {"type": "text", "text": "REGELN", "cache_control": {"type": "ephemeral"}}
    assert body["system"][1] == {"type": "text", "text": "SITUATION"}
    assert body["tools"][0]["eager_input_streaming"] is True
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in captured["headers"]["anthropic-beta"]
    # Fremd-Provider-Turns werden als tool_use/tool_result gerendert
    assert body["messages"][1]["content"][0]["type"] == "tool_use"
    assert body["messages"][2]["content"][0] == {"type": "tool_result", "tool_use_id": "call_x",
                                                 "content": '{"state": "off"}', "is_error": False}

    assert deltas == ["Sehr wohl."]
    assert response.stop_reason == "tool_use"
    assert response.tool_calls == [ToolCall("toolu_1", "home__set_light", {"entity_ids": ["light.kueche"], "on": True})]
    assert response.usage["cache_read_tokens"] == 1000
    assert response.assistant_turn.provider == "claude" and response.assistant_turn.raw is not None

    # Eigene Antwort geht beim nächsten Aufruf unverändert (raw) zurück
    rendered = provider._render_messages([UserTurn("x"), response.assistant_turn])
    assert rendered[1]["content"] is response.assistant_turn.raw


def test_rate_limit_maps_to_retryable_error():
    def handler(request):
        return httpx2.Response(429, headers={"retry-after": "2"},
                               json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}})

    client = anthropic.AsyncAnthropic(
        api_key="test", max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))
    provider = ClaudeProvider(client=client)
    from jarvis.errors import JarvisError

    with pytest.raises(JarvisError) as exc:
        asyncio.run(provider.complete(system=SystemPrompt("R"), transcript=[UserTurn("hi")], tools=[]))
    assert exc.value.code == "JRV-RATE-001" and exc.value.retryable and exc.value.retry_after_ms == 2000


def test_ollama_streaming_tool_calls():
    captured = {}
    lines = [
        {"message": {"role": "assistant", "content": "Einen Moment."}, "done": False},
        {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "home__set_light", "arguments": {"entity_ids": ["light.kueche"], "on": True}}}]},
         "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
         "prompt_eval_count": 900, "eval_count": 30},
    ]

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content="\n".join(json.dumps(line) for line in lines).encode())

    client = httpx.AsyncClient(base_url="http://ollama:11434", transport=httpx.MockTransport(handler))
    provider = OllamaProvider(client=client, model="qwen2.5:14b-instruct")
    deltas = []

    async def on_text(delta):
        deltas.append(delta)

    tool = ToolSpec("home__set_light", "Schaltet Lichter.", {"type": "object", "properties": {}})
    response = asyncio.run(provider.complete(
        system=SystemPrompt("REGELN", "SITUATION"),
        transcript=[UserTurn("Licht an"),
                    AssistantTurn("", [ToolCall("t0", "home__get_state", {"entity_ids": ["light.kueche"]})], "claude"),
                    ToolResultsTurn([ToolResult("t0", "home__get_state", "off")])],
        tools=[tool], on_text=on_text))

    body = captured["body"]
    # stabiler Präfix (KV-Cache): nur Regeln im System-Prompt, Situation in der aktuellen Nutzernachricht
    assert body["messages"][0] == {"role": "system", "content": "REGELN"}
    assert body["messages"][1] == {"role": "user", "content": "SITUATION\n\nLicht an"}
    assert body["keep_alive"] == "24h" and body["options"]["num_ctx"] == 8192
    assert body["messages"][2]["tool_calls"][0]["function"]["name"] == "home__get_state"
    assert body["messages"][3] == {"role": "tool", "content": "off", "tool_name": "home__get_state"}
    assert body["tools"][0]["function"]["parameters"] == {"type": "object", "properties": {}}
    assert deltas == ["Einen Moment."]
    assert response.stop_reason == "tool_use"
    assert response.tool_calls[0].arguments == {"entity_ids": ["light.kueche"], "on": True}
    assert response.usage == {"input_tokens": 900, "output_tokens": 30}


def test_ollama_missing_model_gives_actionable_error():
    from jarvis.errors import JarvisError

    def handler(request):
        return httpx.Response(404, json={"error": "model 'qwen2.5:14b-instruct' not found"})

    client = httpx.AsyncClient(base_url="http://ollama:11434", transport=httpx.MockTransport(handler))
    provider = OllamaProvider(client=client, model="qwen2.5:14b-instruct")
    with pytest.raises(JarvisError) as exc:
        asyncio.run(provider.complete(system=SystemPrompt("R"), transcript=[UserTurn("hi")], tools=[]))
    assert exc.value.code == "JRV-LLM-001"
    assert "ollama pull qwen2.5:14b-instruct" in exc.value.user_message


def test_ollama_prefix_stays_stable_across_turns():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=json.dumps({"message": {"content": "ok"}, "done": True}).encode())

    client = httpx.AsyncClient(base_url="http://ollama:11434", transport=httpx.MockTransport(handler))
    provider = OllamaProvider(client=client)
    # wie im Orchestrator: jede Frage behält den Kontext ihres Zeitpunkts
    first = [UserTurn("Hallo", context="Zeit: 10:00")]
    second = [*first, AssistantTurn("Guten Tag.", [], "ollama"), UserTurn("Wie spät?", context="Zeit: 10:01")]
    asyncio.run(provider.complete(system=SystemPrompt("REGELN", "Zeit: 10:00"), transcript=first, tools=[]))
    asyncio.run(provider.complete(system=SystemPrompt("REGELN", "Zeit: 10:01"), transcript=second, tools=[]))
    one, two = bodies[0]["messages"], bodies[1]["messages"]
    assert two[: len(one)] == one  # alter Prompt ist unveränderter Präfix des neuen -> KV-Cache greift
    assert one[0] == {"role": "system", "content": "REGELN"}
    assert two[3] == {"role": "user", "content": "Zeit: 10:01\n\nWie spät?"}


def test_ollama_warm_up_loads_model_with_same_options():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": "."}, "done": True})

    client = httpx.AsyncClient(base_url="http://ollama:11434", transport=httpx.MockTransport(handler))
    provider = OllamaProvider(client=client, model="qwen2.5:7b-instruct", num_ctx=4096, keep_alive="1h")
    tool = ToolSpec("info__weather", "Wetter.", {"type": "object", "properties": {}})
    asyncio.run(provider.warm_up("REGELN", [tool]))
    body = captured["body"]
    assert captured["path"] == "/api/chat" and body["stream"] is False
    assert body["messages"][0] == {"role": "system", "content": "REGELN"}
    assert body["options"] == {"num_ctx": 4096, "temperature": 0.3, "num_predict": 1}
    assert body["keep_alive"] == "1h" and body["tools"][0]["function"]["name"] == "info__weather"
