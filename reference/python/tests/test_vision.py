"""Sehen: Kamerabild prüfen, lokales Bildmodell fragen, fehlendes Modell nachladen, Gäste ausschließen."""

import asyncio
import base64
import json
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.api import Container, create_app
from jarvis.context import Situation
from jarvis.errors import JarvisError
from jarvis.events import InMemoryEventBus
from jarvis.fastpath import FastPath
from jarvis.llm.router import ModelRouter
from jarvis.orchestrator import TurnRequest
from jarvis.policy import Principal
from jarvis.style import JarvisStyle
from jarvis.testing import ScriptedProvider, say
from jarvis.vision import VisionService, decode_image

DANIEL = Principal(actor="user:owner", role="adult", trust="trusted_user", name="Daniel")
GUEST = Principal(actor="guest:x", role="guest", trust="guest")
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200
DATA_URL = "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()


def ollama(status=200, answer="Ich sehe eine rote Tasse auf einem Holztisch."):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        if status != 200:
            return httpx.Response(status, json={"error": "model not found"})
        return httpx.Response(200, json={"message": {"role": "assistant", "content": answer}})

    return httpx.AsyncClient(base_url="http://ollama", transport=httpx.MockTransport(handler)), seen


def test_decode_accepts_only_images():
    assert decode_image(DATA_URL) == JPEG
    for bad in ("data:image/jpeg;base64,!!!", base64.b64encode(b"<script>").decode()):
        with pytest.raises(JarvisError):
            decode_image(bad)


def test_model_gets_image_question_and_privacy_rules():
    client, seen = ollama()
    service = VisionService("http://ollama", client=client)
    assert asyncio.run(service.describe(JPEG, "Was halte ich in der Hand?")) == \
        "Ich sehe eine rote Tasse auf einem Holztisch."
    system, user = seen[0]["messages"]
    assert "keine Personen anhand ihres Gesichts" in system["content"] and user["images"] == [
        base64.b64encode(JPEG).decode()]
    assert seen[0]["model"] == "qwen2.5vl:3b"


def test_missing_model_is_downloaded_automatically():
    client, _ = ollama(status=404)
    pulls = []
    service = VisionService("http://ollama", client=client, on_missing_model=lambda m, activate: pulls.append((m, activate)))
    with pytest.raises(JarvisError) as exc:
        asyncio.run(service.describe(JPEG, "Was siehst du?"))
    assert pulls == [("qwen2.5vl:3b", False)] and "lade ich gerade herunter" in exc.value.user_message


@pytest.mark.parametrize("text", ["Was siehst du?", "Was halte ich in der Hand?", "Lies mir das Etikett vor",
                                  "Wie viele Finger halte ich hoch?"])
def test_vision_commands(text):
    assert FastPath({}, {}).match(text).grammar == "vision"


def test_was_ist_das_stays_a_knowledge_follow_up():
    match = FastPath({}, {}).match("Was ist das?")
    assert match is None or match.grammar != "vision"


def test_orchestrator_asks_the_interface_for_a_picture(orchestrator):
    orchestrator.style = JarvisStyle()
    result = asyncio.run(orchestrator.handle_turn(
        TurnRequest(text="Was siehst du?", session_id="v", principal=DANIEL), provider=ScriptedProvider([say("-")]),
        situation=Situation(now=datetime(2026, 10, 9, 9, 0))))
    assert result.text == "Einen Moment, Sir – ich sehe nach."
    assert result.card == {"type": "vision", "question": "Was siehst du?"}


def test_vision_endpoint(orchestrator):
    client_http, _ = ollama()
    container = Container(orchestrator=orchestrator, bus=InMemoryEventBus(),
                          router=ModelRouter(local=ScriptedProvider([]), cloud=None),
                          tokens={"adult": DANIEL, "guest": GUEST}, webhook_secrets={},
                          situation=lambda who, ch: Situation(now=datetime.now()),
                          vision=VisionService("http://ollama", client=client_http))
    orchestrator.style = JarvisStyle()
    client = TestClient(create_app(container))
    ok = client.post("/v1/vision", headers={"Authorization": "Bearer adult"},
                     json={"image": DATA_URL, "question": "Was halte ich in der Hand?"})
    assert ok.status_code == 200 and ok.json()["text"] == "Ich sehe eine rote Tasse auf einem Holztisch."
    assert client.post("/v1/vision", headers={"Authorization": "Bearer guest"},
                       json={"image": DATA_URL}).status_code == 403
    bad = client.post("/v1/vision", headers={"Authorization": "Bearer adult"},
                      json={"image": "data:image/jpeg;base64," + "A" * 200})
    assert bad.status_code == 422 and bad.json()["user_message"].endswith("Das Kamerabild war nicht lesbar.")
