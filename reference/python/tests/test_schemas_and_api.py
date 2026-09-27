import json

import pytest
from jsonschema import Draft202012Validator

from jarvis.events import CloudEvent
from jarvis.connectors.homeassistant import state_changed_to_event
from jarvis.webhooks import sign

from conftest import ALEX, REPO, SCHEMAS


def test_all_schemas_are_valid():
    for path in SCHEMAS.glob("*.schema.json"):
        Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("path", sorted((SCHEMAS / "examples").glob("*.json")), ids=lambda p: p.name)
def test_examples_validate(path, validator):
    schema_name = path.name.split(".")[0]
    validator(schema_name).validate(json.loads(path.read_text(encoding="utf-8")))


def test_events_from_code_match_schema(validator):
    event = state_changed_to_event({
        "entity_id": "binary_sensor.fenster_bad",
        "old_state": {"state": "off", "attributes": {}, "last_changed": "2026-09-26T07:12:40+00:00"},
        "new_state": {"state": "on", "attributes": {"device_class": "window"},
                      "last_changed": "2026-09-26T19:40:11+00:00"},
    }, area="bad")
    validator("event").validate(event.to_wire())
    derived = event.derive(type="jarvis.automation.triggered", source="/worker/automation",
                           data={"automation_id": "aut_rain_window"})
    assert derived.correlationid == event.id and derived.causationid == event.id
    validator("event").validate(derived.to_wire())


def test_invalid_event_rejected(validator):
    bad = CloudEvent(type="jarvis.input.utterance", source="/voice", trust="household",
                     data={"text": "", "language": "de", "session_id": "s"}).to_wire()
    assert list(validator("event").iter_errors(bad))


def test_plugin_manifest_and_personas(validator):
    validator("plugin-manifest").validate(
        json.loads((REPO / "reference/node/plugin-weather/plugin.json").read_text(encoding="utf-8")))
    import yaml

    for name in ("persona.jarvis.yaml", "persona.neutral.yaml"):
        validator("persona").validate(yaml.safe_load((REPO / "config" / name).read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# REST-API (FastAPI TestClient)
# ---------------------------------------------------------------------------
@pytest.fixture
def client(orchestrator):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from jarvis.api import Container, create_app
    from jarvis.context import Situation
    from jarvis.events import InMemoryEventBus
    from jarvis.llm.router import ModelRouter
    from jarvis.testing import ScriptedProvider, say
    from datetime import datetime

    bus = InMemoryEventBus()
    local = ScriptedProvider([say("Guten Abend, Sir.")])
    container = Container(
        orchestrator=orchestrator, bus=bus, router=ModelRouter(local=local, cloud=None),
        tokens={"tok_alex": ALEX}, webhook_secrets={"whk_doorbell": b"secret"},
        situation=lambda who, channel: Situation(now=datetime(2026, 9, 26, 19, 0), channel=channel),
    )
    c = fastapi_testclient.TestClient(create_app(container))
    c.bus = bus
    return c


AUTH = {"Authorization": "Bearer tok_alex"}


def test_api_requires_auth(client):
    r = client.post("/v1/conversations/c1/messages", json={"text": "Hallo"})
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "JRV-AUTH-001"
    wrong_scheme = {"Authorization": "Basic tok_alex"}
    assert client.post("/v1/conversations/c1/messages", headers=wrong_scheme, json={"text": "Hallo"}).status_code == 401


def test_api_docs_offer_bearer_login(client):
    # /docs zeigt nur mit Security-Schema den „Authorize“-Knopf und sendet das Token dann mit
    spec = client.get("/openapi.json").json()
    assert spec["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    operation = spec["paths"]["/v1/conversations/{conversation_id}/messages"]["post"]
    assert operation["security"] == [{"HTTPBearer": []}]
    assert not any(p["name"].lower() == "authorization" for p in operation.get("parameters", []))


def test_api_message_fast_path_and_llm(client):
    r = client.post("/v1/conversations/c1/messages", headers=AUTH,
                    json={"text": "Mach das Licht in der Küche an"})
    assert r.status_code == 200 and r.json()["route"] == "fast_path"
    r = client.post("/v1/conversations/c1/messages", headers=AUTH, json={"text": "Guten Abend"})
    assert r.json()["text"] == "Guten Abend, Sir."


def test_api_event_trust_is_server_assigned(client):
    r = client.post("/v1/events", headers=AUTH, json={
        "source": "/tests", "type": "jarvis.custom.ping", "trust": "system", "data": {"x": 1}})
    assert r.status_code == 202
    assert client.bus.published[-1].trust == "household"  # aus dem Token, nicht aus dem Payload


def test_api_webhook_signature(client):
    body = json.dumps({"event": "ring"}).encode()
    import time

    ts = int(time.time())
    headers = {"x-jarvis-timestamp": str(ts), "x-jarvis-signature": sign(b"secret", ts, body),
               "x-jarvis-delivery": "dlv_1", "content-type": "application/json"}
    assert client.post("/v1/webhooks/whk_doorbell", content=body, headers=headers).status_code == 202
    assert client.bus.published[-1].trust == "external_untrusted"
    assert client.post("/v1/webhooks/whk_doorbell", content=body, headers=headers).status_code == 401
    bad = {**headers, "x-jarvis-delivery": "dlv_2", "x-jarvis-signature": "sha256=00"}
    assert client.post("/v1/webhooks/whk_doorbell", content=body, headers=bad).status_code == 401


def test_web_ui_is_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "J.A.R.V.I.S" in page.text
    assert page.headers["cache-control"] == "no-cache"
    for asset in ("jarvis.js", "jarvis.css", "favicon.svg"):
        assert client.get(f"/{asset}").status_code == 200
    assert client.get("/v1/system/health").json()["status"] == "ok"  # API-Routen haben Vorrang


def test_api_websocket_rejects_bad_token(client):
    from starlette.websockets import WebSocketDisconnect

    # 4401 muss beim Browser ankommen (vor accept() würde daraus HTTP 403 bzw. Close-Code 1006)
    with client.websocket_connect("/v1/stream?token=falsch") as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 4401


def test_api_websocket_stream(client):
    with client.websocket_connect("/v1/stream?token=tok_alex") as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
        ws.send_json({"type": "input.text", "text": "Schalte das Licht im Wohnzimmer an", "session_id": "ws1"})
        final = ws.receive_json()
        assert final["type"] == "output.final" and final["route"] == "fast_path"
        ws.send_json({"type": "confirmation.resolve", "confirmation_id": "cnf_x", "decision": "approve"})
        assert ws.receive_json()["error"]["code"] == "JRV-POL-001"
