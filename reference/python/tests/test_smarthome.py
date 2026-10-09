"""Smart Home: Home Assistant finden, per Anmeldung verbinden, Räume und Geräte übernehmen, Sofortbefehle.

Gegen einen nachgebildeten Home Assistant (HTTP + WebSocket wie das Original: /auth/providers, /auth/token,
/api/websocket mit auth, get_states, Registry, call_service, state_changed).
"""

import asyncio
import json
import os
import socket
import stat
import threading
import time
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import pytest
import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.testclient import TestClient

from jarvis.api import Container, create_app
from jarvis.context import ContextBuilder, Situation
from jarvis.errors import JarvisError
from jarvis.events import InMemoryEventBus
from jarvis.fastpath import FastPath
from jarvis.homeindex import HomeIndex, match_home
from jarvis.llm.router import ModelRouter
from jarvis.orchestrator import ConfirmationStore, MemoryAuditSink, Orchestrator, TurnRequest
from jarvis.persona import Persona
from jarvis.policy import PolicyEngine, Principal
from jarvis.smarthome import SmartHome, http_url, is_home_assistant, probe, ws_url
from jarvis.testing import ScriptedProvider, say
from jarvis.tools import ToolRegistry

from conftest import REPO

DANIEL = Principal(actor="user:owner", role="adult", trust="trusted_user", name="Daniel")
STATES = [
    {"entity_id": "light.kueche_decke", "state": "off", "attributes": {"friendly_name": "Küche Decke"}},
    {"entity_id": "light.stehlampe", "state": "on", "attributes": {"friendly_name": "Stehlampe"}},
    {"entity_id": "light.wohnzimmer_decke", "state": "on", "attributes": {"friendly_name": "Wohnzimmer Decke"}},
    {"entity_id": "switch.kaffeemaschine", "state": "off", "attributes": {"friendly_name": "Kaffeemaschine"}},
    {"entity_id": "cover.wohnzimmer", "state": "open", "attributes": {"friendly_name": "Rollladen Wohnzimmer"}},
    {"entity_id": "cover.schlafzimmer", "state": "open", "attributes": {"friendly_name": "Rollladen Schlafzimmer"}},
    {"entity_id": "climate.bad", "state": "heat", "attributes": {"friendly_name": "Thermostat Bad"}},
    {"entity_id": "climate.wohnzimmer", "state": "heat", "attributes": {"friendly_name": "Thermostat Wohnzimmer"}},
    {"entity_id": "scene.filmabend", "state": "2026-10-01", "attributes": {"friendly_name": "Filmabend"}},
    {"entity_id": "lock.haustuer", "state": "locked", "attributes": {"friendly_name": "Haustür"}},
    {"entity_id": "sensor.bad_temperatur", "state": "21.5", "attributes": {
        "friendly_name": "Bad Temperatur", "device_class": "temperature", "unit_of_measurement": "°C"}},
    {"entity_id": "sensor.firmware", "state": "1.2", "attributes": {"friendly_name": "Firmware"}},
    {"entity_id": "automation.licht", "state": "on", "attributes": {"friendly_name": "Automation"}},
]
REGISTRY = {
    "areas": [{"area_id": "kueche", "name": "Küche"}, {"area_id": "wohnzimmer", "name": "Wohnzimmer"},
              {"area_id": "bad", "name": "Bad"}, {"area_id": "schlafzimmer", "name": "Schlafzimmer"}],
    "devices": [{"id": "lampe1", "area_id": "wohnzimmer"}],
    "entities": [
        {"entity_id": "light.kueche_decke", "area_id": "kueche"},
        {"entity_id": "light.stehlampe", "device_id": "lampe1", "aliases": ["Leselampe"]},
        {"entity_id": "light.wohnzimmer_decke", "area_id": "wohnzimmer"},
        {"entity_id": "cover.wohnzimmer", "area_id": "wohnzimmer"},
        {"entity_id": "cover.schlafzimmer", "area_id": "schlafzimmer"},
        {"entity_id": "climate.bad", "area_id": "bad"}, {"entity_id": "climate.wohnzimmer", "area_id": "wohnzimmer"},
        {"entity_id": "sensor.bad_temperatur", "area_id": "bad"},
        {"entity_id": "sensor.firmware", "entity_category": "diagnostic"},
    ],
}


# ----------------------------------------------------------------------------------------------------------------
# Nachgebildeter Home Assistant
# ----------------------------------------------------------------------------------------------------------------
class FakeHomeAssistant:
    def __init__(self) -> None:
        self.states = {s["entity_id"]: json.loads(json.dumps(s)) for s in STATES}
        self.calls: list[tuple[str, str, dict, dict]] = []
        self.tokens = {"long-lived-token-for-tests-123"}
        self.created: list[str] = []
        app = FastAPI()

        @app.get("/auth/providers")
        async def providers():
            return {"providers": [{"name": "Home Assistant Local", "type": "homeassistant", "id": None}]}

        @app.post("/auth/token")
        async def token(request: Request):
            form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
            grant_type, code, client_id = form.get("grant_type"), form.get("code"), form.get("client_id", "")
            if grant_type != "authorization_code" or code != "good-code" or not client_id.startswith("http"):
                return {"error": "invalid_request"}
            self.tokens.add("short-access")
            return {"access_token": "short-access", "token_type": "Bearer", "expires_in": 1800,
                    "refresh_token": "refresh"}

        @app.websocket("/api/websocket")
        async def websocket(ws: WebSocket):
            await ws.accept()
            await ws.send_json({"type": "auth_required", "ha_version": "2026.10.0"})
            auth = await ws.receive_json()
            if auth.get("access_token") not in self.tokens:
                await ws.send_json({"type": "auth_invalid", "message": "Invalid access token"})
                await ws.close()
                return
            await ws.send_json({"type": "auth_ok", "ha_version": "2026.10.0"})
            subscribed: list[int] = []
            while True:
                try:
                    msg = await ws.receive_json()
                except Exception:
                    return
                kind, msg_id = msg["type"], msg["id"]
                result: object = None
                if kind == "get_states":
                    result = list(self.states.values())
                elif kind == "subscribe_events":
                    subscribed.append(msg_id)
                elif kind == "get_config":
                    result = {"location_name": "Zuhause", "version": "2026.10.0"}
                elif kind.startswith("config/") and kind.endswith("_registry/list"):
                    result = REGISTRY[{"area": "areas", "device": "devices", "entity": "entities"}[
                        kind.split("/")[1].removesuffix("_registry")]]
                elif kind == "auth/long_lived_access_token":
                    token = f"long-lived-{len(self.created)}-{msg['client_name']}"
                    self.created.append(msg["client_name"])
                    self.tokens.add(token)
                    result = token
                elif kind == "call_service":
                    self.calls.append((msg["domain"], msg["service"], msg["target"], msg.get("service_data") or {}))
                await ws.send_json({"id": msg_id, "type": "result", "success": True, "result": result})
                if kind == "call_service":
                    for entity_id in msg["target"]["entity_id"] if isinstance(msg["target"]["entity_id"], list) \
                            else [msg["target"]["entity_id"]]:
                        new = self._apply(entity_id, msg["service"], msg.get("service_data") or {})
                        for sub in subscribed:
                            await ws.send_json({"id": sub, "type": "event", "event": {
                                "event_type": "state_changed",
                                "data": {"entity_id": entity_id, "old_state": None, "new_state": new}}})

        self.app = app

    def _apply(self, entity_id: str, service: str, data: dict) -> dict:
        state = self.states[entity_id]
        state["state"] = {"turn_on": "on", "turn_off": "off", "lock": "locked", "unlock": "unlocked"}.get(
            service, state["state"])
        if "brightness_pct" in data:
            state["attributes"]["brightness"] = round(data["brightness_pct"] * 255 / 100)
        return state


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def fake_ha():
    ha = FakeHomeAssistant()
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(ha.app, host="127.0.0.1", port=port, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    ha.url = f"http://127.0.0.1:{port}"
    yield ha
    server.should_exit = True
    thread.join(5)


# ----------------------------------------------------------------------------------------------------------------
# Geräteliste und Sofortbefehle
# ----------------------------------------------------------------------------------------------------------------
def index() -> HomeIndex:
    return HomeIndex.build(STATES, REGISTRY)


def test_index_takes_rooms_devices_and_aliases_from_the_registry():
    home = index()
    assert home.areas["kueche"] == "Küche"
    by_id = {d.entity_id: d for d in home.devices}
    assert by_id["light.stehlampe"].area == "wohnzimmer"  # Raum über das Gerät
    assert "sensor.firmware" not in by_id and "automation.licht" not in by_id  # Diagnose, nicht steuerbar
    assert home.area_of("kueche") == home.area_of("der Küche") == "kueche"
    assert [d.entity_id for d in home.named("Leselampe", "light")] == ["light.stehlampe"]
    assert home.situation_lines()[0] == "Küche Decke (light.kueche_decke, Küche)"


@pytest.mark.parametrize("text, capability, arguments, default_area", [
    ("Mach das Licht in der Küche an", "home.set_light", {"entity_ids": ["light.kueche_decke"], "on": True}, None),
    ("Licht im Wohnzimmer aus", "home.set_light",
     {"entity_ids": ["light.stehlampe", "light.wohnzimmer_decke"], "on": False}, None),
    ("Jarvis, schalte bitte alle Lichter aus", "home.set_light",
     {"entity_ids": ["light.kueche_decke", "light.stehlampe", "light.wohnzimmer_decke"], "on": False}, None),
    ("Mach das Licht an", "home.set_light", {"entity_ids": ["light.kueche_decke"], "on": True}, "kueche"),
    ("Dimm das Licht im Wohnzimmer auf dreißig Prozent", "home.set_light",
     {"entity_ids": ["light.stehlampe", "light.wohnzimmer_decke"], "on": True, "brightness_pct": 30}, None),
    ("Mach die Leselampe aus", "home.set_light", {"entity_ids": ["light.stehlampe"], "on": False}, None),
    ("Kaffeemaschine an", "home.set_switch", {"entity_ids": ["switch.kaffeemaschine"], "on": True}, None),
    ("Rollläden runter", "home.set_cover", {"entity_ids": ["cover.wohnzimmer", "cover.schlafzimmer"],
                                            "position": 0}, None),
    ("Fahr die Rollos im Schlafzimmer hoch", "home.set_cover", {"entity_ids": ["cover.schlafzimmer"],
                                                                "position": 100}, None),
    ("Heizung im Bad auf einundzwanzig Grad", "home.set_climate", {"entity_id": "climate.bad",
                                                                   "temperature": 21.0}, None),
    ("Mach die Heizung im Wohnzimmer aus", "home.set_climate", {"entity_id": "climate.wohnzimmer",
                                                                "hvac_mode": "off"}, None),
    ("Starte Filmabend", "home.activate_scene", {"scene_id": "scene.filmabend"}, None),
    ("Schließ die Haustür ab", "home.lock", {"entity_id": "lock.haustuer", "state": "locked"}, None),
])
def test_home_commands_without_language_model(text, capability, arguments, default_area):
    fast_path = FastPath({}, {})
    fast_path.set_home(index())
    match = fast_path.match(text, default_area=default_area)
    assert (match.capability, match.arguments) == (capability, arguments)


@pytest.mark.parametrize("text", ["Ton aus", "Mach Musik an", "Öffne Steam", "Schließ Steam", "Licht im Keller an",
                                  "Mach das Licht an", "Heizung auf 21 Grad"])  # mehrdeutig/fremd: nicht fürs Haus
def test_other_sentences_are_not_taken_for_the_house(text):
    fast_path = FastPath({}, {})
    fast_path.set_home(index())
    match = fast_path.match(text)
    assert match is None or not match.capability.startswith("home.")


@pytest.mark.parametrize("text", ["Rollläden runter", "Schließ die Haustür ab", "Heizung auf 21 Grad",
                                  "Alle Lichter aus", "Aktiviere die Szene Filmabend"])
def test_without_home_assistant_jarvis_says_so(text):
    assert match_home(text.lower(), None).grammar == "home_unavailable"


# ----------------------------------------------------------------------------------------------------------------
# Finden, Anmelden, Zugang
# ----------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("given, url", [
    ("192.168.178.20", "http://192.168.178.20:8123"), ("homeassistant.local:8123/", "http://homeassistant.local:8123"),
    ("ws://homeassistant:8123/api/websocket", "http://homeassistant:8123"), ("https://ha.example.org", "https://ha.example.org"),
])
def test_addresses_are_normalized(given, url):
    assert http_url(given) == url
    assert ws_url(url).endswith("/api/websocket")


def test_finds_home_assistant_and_ignores_other_servers(fake_ha):
    assert asyncio.run(is_home_assistant(fake_ha.url))
    assert not asyncio.run(is_home_assistant(f"http://127.0.0.1:{free_port()}"))


def test_wrong_token_and_wrong_address_get_clear_messages(fake_ha):
    with pytest.raises(JarvisError) as exc:
        asyncio.run(probe(fake_ha.url, "falsch"))
    assert "abgelehnt" in exc.value.user_message
    with pytest.raises(JarvisError) as exc:
        asyncio.run(probe(f"http://127.0.0.1:{free_port()}", "egal"))
    assert "antwortet kein Home Assistant" in exc.value.user_message


def make_home(tmp_path, fake_ha) -> SmartHome:
    home = SmartHome(path=tmp_path / "home.json", registry=ToolRegistry(), scan_network=False)
    return home


def test_login_in_the_browser_creates_a_lasting_access(tmp_path, fake_ha):
    home = make_home(tmp_path, fake_ha)
    authorize = home.oauth_start(fake_ha.url, "http://127.0.0.1:8080/")
    query = parse_qs(urlsplit(authorize).query)
    assert authorize.startswith(f"{fake_ha.url}/auth/authorize?")
    assert query["client_id"] == ["http://127.0.0.1:8080/"]
    assert query["redirect_uri"] == ["http://127.0.0.1:8080/v1/settings/home/oauth"]
    with pytest.raises(JarvisError):
        asyncio.run(home.oauth_finish("fremder-state", "good-code"))
    info = asyncio.run(home.oauth_finish(query["state"][0], "good-code"))
    assert info["location_name"] == "Zuhause" and fake_ha.created[-1].startswith("JARVIS (")
    saved = json.loads((tmp_path / "home.json").read_text())
    assert saved["url"] == fake_ha.url and saved["token"].startswith("long-lived-")  # nicht der 30-Minuten-Zugang
    assert stat.S_IMODE(os.stat(tmp_path / "home.json").st_mode) == 0o600
    with pytest.raises(JarvisError):  # ein state gilt genau einmal
        asyncio.run(home.oauth_finish(query["state"][0], "good-code"))
    assert "token" not in json.dumps(home.status())


def build_orchestrator(home: SmartHome) -> Orchestrator:
    persona = Persona.load(REPO / "config" / "persona.jarvis.yaml", schema_path=REPO / "schemas" / "persona.schema.json")
    orchestrator = Orchestrator(registry=home.registry, policy=PolicyEngine.from_file(REPO / "config" / "policies.yaml"),
                                context=ContextBuilder(persona=persona), audit=MemoryAuditSink(),
                                confirmations=ConfirmationStore(), fast_path=FastPath({}, {}))
    home.fast_path = orchestrator.fast_path
    return orchestrator


async def until(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        await asyncio.sleep(0.05)
    return False


def test_connected_house_is_controlled_by_voice(tmp_path, fake_ha):
    home = make_home(tmp_path, fake_ha)
    orchestrator = build_orchestrator(home)
    situation = Situation(now=datetime(2026, 10, 9, 20, 0), user_display="Daniel")

    async def scenario():
        await home.connect_with_token(fake_ha.url, "long-lived-token-for-tests-123")
        runner = asyncio.create_task(home.run())
        assert await until(lambda: home.connected and home.index is not None)

        async def say_(text):
            request = TurnRequest(text=text, session_id="haus", principal=DANIEL)
            return await orchestrator.handle_turn(request, provider=ScriptedProvider([say("LLM")]),
                                                  situation=situation)

        replies = [(await say_(t)).text for t in ("Licht im Wohnzimmer aus", "Kaffeemaschine an",
                                                   "Rollläden im Schlafzimmer runter", "Stell die Heizung im Bad auf 22 Grad",
                                                   "Starte Filmabend")]
        status = home.status()
        lines = home.situation_lines()
        runner.cancel()
        return replies, status, lines

    replies, status, lines = asyncio.run(scenario())
    assert replies == [
        "Selbstverständlich, Sir. Das Licht im Wohnzimmer ist nun ausgeschaltet.",
        "Selbstverständlich, Sir. Kaffeemaschine ist nun eingeschaltet.",
        "Sehr wohl. Die Rollläden im Schlafzimmer fahren herunter.",
        "Sehr wohl. Die Heizung im Bad ist auf 22 Grad eingestellt.",
        "Sehr wohl. Die Szene „Filmabend“ ist aktiviert.",
    ]
    assert ("light", "turn_off", {"entity_id": ["light.stehlampe", "light.wohnzimmer_decke"]}, {}) in fake_ha.calls
    assert ("climate", "set_temperature", {"entity_id": "climate.bad"}, {"temperature": 22.0}) in fake_ha.calls
    assert status["connected"] and status["name"] == "Zuhause" and status["devices"] == 11
    assert [room["name"] for room in status["rooms"]][:2] == ["Küche", "Wohnzimmer"]
    assert "Stehlampe (light.stehlampe, Wohnzimmer): off" in lines  # Zustand fürs Sprachmodell, live


def test_settings_api(tmp_path, fake_ha):
    home = make_home(tmp_path, fake_ha)
    orchestrator = build_orchestrator(home)
    container = Container(orchestrator=orchestrator, bus=InMemoryEventBus(),
                          router=ModelRouter(local=ScriptedProvider([]), cloud=None),
                          tokens={"adult": DANIEL, "guest": Principal(actor="guest:x", role="guest", trust="guest")},
                          webhook_secrets={}, situation=lambda who, ch: Situation(now=datetime.now()), smarthome=home)
    client = TestClient(create_app(container))
    adult = {"Authorization": "Bearer adult"}
    assert client.get("/v1/settings/home", headers={"Authorization": "Bearer guest"}).status_code == 403
    assert client.get("/v1/settings/home", headers=adult).json()["configured"] is False
    bad = client.put("/v1/settings/home/token", headers=adult, json={"url": fake_ha.url, "token": "x" * 30})
    assert bad.status_code == 401 and "abgelehnt" in bad.json()["user_message"]
    started = client.post("/v1/settings/home/oauth", headers=adult, json={"url": fake_ha.url}).json()
    state = parse_qs(urlsplit(started["authorize_url"]).query)["state"][0]
    page = client.get(f"/v1/settings/home/oauth?state={state}&code=good-code")
    assert page.status_code == 200 and "Verbunden mit Zuhause" in page.text and "/#home=ok" in page.text
    assert client.get("/v1/settings/home", headers=adult).json()["source"] == "ui"
    failed = client.get("/v1/settings/home/oauth?state=abc&code=good-code")
    assert "abgelaufen" in failed.text and "/#home=error" in failed.text
    assert client.delete("/v1/settings/home", headers=adult).json()["configured"] is False
    assert client.get("/v1/system/health").json()["smart_home"] == "off"
