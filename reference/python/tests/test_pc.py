"""PC-Steuerung: Sofortbefehle, Agent-Verbindung, Richtlinien und der Weg über die API bis zum Agenten."""

import asyncio
import time
from datetime import datetime

import pytest

from jarvis.errors import JarvisError
from jarvis.fastpath import FastPath
from jarvis.orchestrator import TurnRequest
from jarvis.pc import AgentHub, register_pc_capabilities
from jarvis.policy import PolicyContext
from jarvis.tools import InvocationContext, ToolRegistry

from conftest import ALEX, GUEST

CTX = InvocationContext(correlation_id="c1", actor="user:alex", session_id="s1", area="wohnzimmer")


@pytest.mark.parametrize("text, capability, arguments", [
    ("Öffne den Explorer", "pc.open_app", {"app": "explorer"}),
    ("Jarvis, öffne den Datei-Explorer.", "pc.open_app", {"app": "explorer"}),
    ("starte den Browser", "pc.open_app", {"app": "browser"}),
    ("Mach mal den Taschenrechner auf", "pc.open_app", {"app": "rechner"}),
    ("Bitte starte Spotify", "pc.open_app", {"app": "spotify"}),
    ("Öffne meine Downloads", "pc.open_folder", {"folder": "downloads"}),
    ("öffne den Ordner Dokumente", "pc.open_folder", {"folder": "documents"}),
    ("Zeig mir die Bilder", "pc.open_folder", {"folder": "pictures"}),
    ("Öffne YouTube", "pc.open_url", {"url": "https://www.youtube.com"}),
    ("öffne heise.de", "pc.open_url", {"url": "https://heise.de"}),
    ("Such im Internet nach Rezepten mit Kürbis", "pc.search_web", {"query": "Rezepten mit Kürbis"}),
    ("google Wetter Berlin", "pc.search_web", {"query": "Wetter Berlin"}),
])
def test_pc_fast_path(text, capability, arguments):
    match = FastPath({}, {}).match(text)
    assert match is not None and match.capability == capability and match.arguments == arguments


@pytest.mark.parametrize("text", [
    "Mach das Licht aus", "Öffne die Haustür", "Was ist ein Browser?", "Öffne das Fenster im Bad",
])
def test_pc_fast_path_ignores_other_requests(text):
    assert FastPath({}, {}).match(text) is None


def test_hub_round_trip_and_errors():
    async def scenario():
        hub = AgentHub(timeout_s=0.2)
        with pytest.raises(JarvisError) as not_connected:
            await hub.invoke("open_app", {"app": "explorer"})
        assert not_connected.value.code == "JRV-DEV-001" and "Desktop-Verknüpfung" in not_connected.value.user_message

        sent = []

        async def send(message):
            sent.append(message)
            if message["arguments"].get("app") == "explorer":
                hub.resolve({"id": message["id"], "ok": True, "result": {"opened": "explorer"}})
            elif message["arguments"].get("app") == "steam":
                hub.resolve({"id": message["id"], "ok": False, "error": "Unbekanntes Programm 'steam'"})

        hub.attach(send, {"name": "PC"})
        assert await hub.invoke("open_app", {"app": "explorer"}) == {"opened": "explorer"}
        with pytest.raises(JarvisError) as unknown:
            await hub.invoke("open_app", {"app": "steam"})
        assert unknown.value.user_message == "Unbekanntes Programm 'steam'"
        with pytest.raises(JarvisError) as timeout:
            await hub.invoke("open_app", {"app": "paint"})  # Agent antwortet nicht
        assert timeout.value.code == "JRV-TMO-001"
        hub.detach(send)
        assert not hub.connected and sent[0]["type"] == "agent.invoke"

    asyncio.run(scenario())


def test_open_url_accepts_only_web_addresses():
    registry = ToolRegistry()
    hub = AgentHub()
    register_pc_capabilities(registry, hub)
    with pytest.raises(JarvisError) as exc:
        asyncio.run(registry.get("pc.open_url").handler({"url": "file:///C:/Windows/System32/cmd.exe"}, CTX))
    assert exc.value.code == "JRV-VAL-002"


def test_guests_may_not_control_the_pc(orchestrator):
    register_pc_capabilities(orchestrator.registry, AgentHub())
    ctx = PolicyContext(tainted=False, allowed_domains=None, mode="normal", now=datetime(2026, 9, 27, 12, 0))
    record = asyncio.run(orchestrator.request_action(
        capability="pc.open_app", arguments={"app": "explorer"}, principal=GUEST, correlation_id="c",
        session_id="g", via="fast_path", ctx=ctx))
    assert record.status == "denied"


def test_open_url_needs_confirmation_after_untrusted_content(orchestrator):
    register_pc_capabilities(orchestrator.registry, AgentHub())
    ctx = PolicyContext(tainted=True, allowed_domains=None, mode="normal", now=datetime(2026, 9, 27, 12, 0))
    record = asyncio.run(orchestrator.request_action(
        capability="pc.open_url", arguments={"url": "https://example.org"}, principal=ALEX, correlation_id="c",
        session_id="t", via="llm", ctx=ctx))
    assert record.status == "pending_confirmation"


@pytest.fixture
def pc_client(orchestrator):
    testclient = pytest.importorskip("fastapi.testclient")
    from jarvis.api import Container, create_app
    from jarvis.context import Situation
    from jarvis.events import InMemoryEventBus
    from jarvis.llm.router import ModelRouter
    from jarvis.testing import ScriptedProvider, say

    hub = AgentHub(timeout_s=5)
    register_pc_capabilities(orchestrator.registry, hub)
    container = Container(
        orchestrator=orchestrator, bus=InMemoryEventBus(), router=ModelRouter(local=ScriptedProvider([say("-")]),
                                                                              cloud=None),
        tokens={"tok_alex": ALEX}, webhook_secrets={}, agents=hub,
        situation=lambda who, channel: Situation(now=datetime(2026, 9, 27, 12, 0), channel=channel),
    )
    return testclient.TestClient(create_app(container))


def test_voice_command_reaches_the_pc_agent(pc_client):
    with pc_client.websocket_connect("/v1/stream?token=tok_alex") as chat:
        chat.send_json({"type": "input.text", "text": "Öffne den Explorer", "session_id": "pc1"})
        final = chat.receive_json()
        assert final["route"] == "fast_path" and "nicht verbunden" in final["text"]  # noch kein Agent

        with pc_client.websocket_connect("/v1/agent?token=tok_alex") as agent:
            agent.send_json({"type": "agent.hello", "name": "PC", "apps": ["explorer"], "folders": ["downloads"]})
            for _ in range(50):
                if pc_client.get("/v1/system/health").json()["pc_agent"] == "connected":
                    break
                time.sleep(0.02)
            chat.send_json({"type": "input.text", "text": "Öffne den Explorer", "session_id": "pc1"})
            invoke = agent.receive_json()
            assert invoke["type"] == "agent.invoke" and invoke["action"] == "open_app"
            assert invoke["arguments"] == {"app": "explorer"}
            agent.send_json({"type": "agent.result", "id": invoke["id"], "ok": True, "result": {"opened": "explorer"}})
            final = chat.receive_json()
            assert final["text"] == "Erledigt." and final["actions"][0]["status"] == "succeeded"
    assert pc_client.get("/v1/system/health").json()["pc_agent"] == "disconnected"


def test_agent_needs_a_valid_token(pc_client):
    from starlette.websockets import WebSocketDisconnect

    with pc_client.websocket_connect("/v1/agent?token=falsch") as agent:
        with pytest.raises(WebSocketDisconnect) as closed:
            agent.receive_json()
    assert closed.value.code == 4401
