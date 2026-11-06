"""KI-Modell in der Oberfläche wählen: lokales Modell (Ollama), Claude-Schlüssel und -Modell, Modus."""

import asyncio
import json
import stat
import time
from datetime import datetime

import httpx
import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

from jarvis.api import Container, create_app  # noqa: E402
from jarvis.context import Situation  # noqa: E402
from jarvis.errors import JarvisError  # noqa: E402
from jarvis.events import InMemoryEventBus  # noqa: E402
from jarvis.llm.ollama import OllamaProvider  # noqa: E402
from jarvis.llm.router import Classification, ModelRouter  # noqa: E402
from jarvis.llm_settings import LLMSettings  # noqa: E402

from conftest import ALEX, GUEST  # noqa: E402

GOOD_KEY = "sk-ant-api03-richtig-0123456789"
AUTH = {"Authorization": "Bearer tok_alex"}


class FakeClaude:
    name = "claude"
    is_local = False

    def __init__(self, key, model):
        self.key, self.model = key, model

    async def check(self):
        if self.key not in (GOOD_KEY, "sk-ant-aus-der-env-datei-000"):
            raise JarvisError("JRV-LLM-003", "API-Schlüssel ungültig",
                              user_message="Dieser API-Schlüssel wird von Anthropic nicht angenommen.")
        return {"claude-opus-5-5": "Claude Opus 5.5", "claude-sonnet-5-5": "Claude Sonnet 5.5"}[self.model]


def fake_ollama(state):
    def handler(request):
        path = request.url.path
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": n, "size": 4_700_000_000} for n in state["installed"]]})
        if path == "/api/pull":
            name = json.loads(request.content)["model"]
            if name == "gibt-es-nicht:1b":
                lines = [{"status": "pulling manifest"}, {"error": "pull model manifest: file does not exist"}]
            else:
                lines = [{"status": "pulling manifest"}, {"status": "downloading", "total": 100, "completed": 40},
                         {"status": "downloading", "total": 100, "completed": 100}, {"status": "success"}]
                state["installed"].append(name)
            return httpx.Response(200, content="\n".join(json.dumps(line) for line in lines).encode())
        if path == "/api/generate":
            state["unloaded"].append(json.loads(request.content)["model"])
            return httpx.Response(200, json={})
        return httpx.Response(404)

    client = httpx.AsyncClient(base_url="http://ollama:11434", transport=httpx.MockTransport(handler))
    return OllamaProvider(client=client, model="qwen2.5:3b-instruct")


@pytest.fixture
def setup(orchestrator, tmp_path):
    state = {"installed": ["qwen2.5:3b-instruct", "llama3.1:8b"], "unloaded": [], "warm": []}
    local = fake_ollama(state)
    router = ModelRouter(local=local, cloud=None)
    status = {"value": "ready"}

    async def warm_up():
        state["warm"].append(local.model)
        status["value"] = "ready"

    def make(env_key=None):
        return LLMSettings(path=tmp_path / "llm.json", router=router, local=local, env_key=env_key,
                           cloud_options={}, make_cloud=FakeClaude, warm_up=warm_up, status=lambda: status["value"])

    settings = make()
    container = Container(orchestrator=orchestrator, bus=InMemoryEventBus(), router=router,
                          tokens={"tok_alex": ALEX, "tok_gast": GUEST}, webhook_secrets={},
                          situation=lambda who, channel: Situation(now=datetime(2026, 9, 28, 21, 0)),
                          llm_settings=settings)
    with fastapi_testclient.TestClient(create_app(container)) as client:
        yield client, state, router, make, tmp_path


def test_only_adults_of_the_household(setup):
    client, *_ = setup
    assert client.get("/v1/settings/llm").status_code == 401
    assert client.get("/v1/settings/llm", headers={"Authorization": "Bearer tok_gast"}).status_code == 403


def test_overview(setup):
    client, *_ = setup
    data = client.get("/v1/settings/llm", headers=AUTH).json()
    assert data["mode"] == "auto" and data["local"]["model"] == "qwen2.5:3b-instruct"
    assert [m["name"] for m in data["local"]["installed"]] == ["qwen2.5:3b-instruct", "llama3.1:8b"]
    suggested = {m["name"]: m["installed"] for m in data["local"]["suggested"]}
    assert suggested["qwen2.5:3b-instruct"] and suggested["llama3.1:8b"] and not suggested["qwen2.5:7b-instruct"]
    assert data["cloud"] == {**data["cloud"], "configured": False, "key_hint": None, "model": "claude-opus-5-5"}
    assert [m["id"] for m in data["cloud"]["models"]] == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"]


def test_wrong_key_is_rejected_and_not_saved(setup):
    client, state, router, make, tmp_path = setup
    response = client.put("/v1/settings/llm/claude-key", headers=AUTH, json={"api_key": "sk-ant-falsch-9999999999"})
    assert response.status_code == 400
    assert "nicht angenommen" in response.json()["user_message"]
    assert router.cloud is None and not (tmp_path / "llm.json").exists()
    typo = client.put("/v1/settings/llm/claude-key", headers=AUTH, json={"api_key": "mein-passwort-123"})
    assert typo.status_code == 422 and "sk-ant-" in typo.json()["user_message"]


def test_key_is_checked_saved_privately_and_never_returned(setup):
    client, state, router, make, tmp_path = setup
    response = client.put("/v1/settings/llm/claude-key", headers=AUTH, json={"api_key": f"  {GOOD_KEY} "})
    assert response.status_code == 200 and response.json()["checked"] == "Claude Opus 5.5"
    data = client.get("/v1/settings/llm", headers=AUTH)
    assert data.json()["cloud"]["key_hint"] == "…6789" and data.json()["cloud"]["source"] == "ui"
    assert GOOD_KEY not in data.text and GOOD_KEY not in response.text
    assert router.cloud.key == GOOD_KEY and router.cloud.model == "claude-opus-5-5"
    path = tmp_path / "llm.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600  # nur der JARVIS-Dienst darf die Datei lesen


def test_model_and_mode_survive_a_restart(setup):
    client, state, router, make, tmp_path = setup
    client.put("/v1/settings/llm/claude-key", headers=AUTH, json={"api_key": GOOD_KEY})
    data = client.put("/v1/settings/llm", headers=AUTH, json={"claude_model": "claude-sonnet-5-5", "mode": "cloud",
                                                               "local_model": "llama3.1:8b"}).json()
    assert data["mode"] == "cloud" and data["cloud"]["model"] == "claude-sonnet-5-5"
    assert router.cloud.model == "claude-sonnet-5-5" and router.local.model == "llama3.1:8b"
    assert state["unloaded"] == ["qwen2.5:3b-instruct"]  # altes Modell aus dem Speicher genommen
    router.mode, router.cloud, router.local.model = "auto", None, "x"
    make()  # Neustart: gespeicherte Auswahl gilt sofort
    assert (router.mode, router.cloud.model, router.cloud.key, router.local.model) == (
        "cloud", "claude-sonnet-5-5", GOOD_KEY, "llama3.1:8b")


def test_removing_the_key_falls_back_to_the_env_file(setup):
    client, state, router, make, tmp_path = setup
    settings = make(env_key="sk-ant-aus-der-env-datei-000")  # Schlüssel aus deploy/.env
    assert router.cloud.key == "sk-ant-aus-der-env-datei-000"
    asyncio.run(settings.set_claude_key(GOOD_KEY))
    assert router.cloud.key == GOOD_KEY
    settings.remove_claude_key()
    assert router.cloud.key == "sk-ant-aus-der-env-datei-000"
    assert asyncio.run(settings.snapshot())["cloud"]["source"] == "env"


def test_download_with_progress_then_switch(setup):
    client, state, router, *_ = setup
    response = client.post("/v1/settings/llm/pull", headers=AUTH, json={"model": "Qwen2.5:14b-instruct"})
    assert response.status_code == 202
    for _ in range(50):
        pull = client.get("/v1/settings/llm", headers=AUTH).json()["local"]["pull"]
        if pull["done"]:
            break
        time.sleep(0.02)
    assert pull == {**pull, "model": "qwen2.5:14b-instruct", "done": True, "error": None, "completed": 100,
                    "total": 100}
    assert router.local.model == "qwen2.5:14b-instruct" and state["warm"][-1] == "qwen2.5:14b-instruct"


def test_download_errors_are_shown(setup):
    client, state, router, *_ = setup
    client.post("/v1/settings/llm/pull", headers=AUTH, json={"model": "gibt-es-nicht:1b"})
    for _ in range(50):
        pull = client.get("/v1/settings/llm", headers=AUTH).json()["local"]["pull"]
        if pull["done"]:
            break
        time.sleep(0.02)
    assert "file does not exist" in pull["error"] and router.local.model == "qwen2.5:3b-instruct"
    bad = client.post("/v1/settings/llm/pull", headers=AUTH, json={"model": "rm -rf /"})
    assert bad.status_code == 422


def test_uninstalled_model_cannot_be_selected(setup):
    client, *_ = setup
    response = client.put("/v1/settings/llm", headers=AUTH, json={"local_model": "qwen2.5:7b-instruct"})
    assert response.status_code == 422 and "noch nicht heruntergeladen" in response.json()["user_message"]


def test_mode_steers_the_router():
    router = ModelRouter(local=object(), cloud=object())
    simple, complex_ = Classification("public", "simple"), Classification("public", "complex")
    router.mode = "cloud"
    assert router.decide(simple, user_override=router.override).route == "cloud_llm"
    assert router.decide(Classification("sensitive", "simple"), user_override=router.override).route == "local_llm"
    router.mode = "local"
    assert router.decide(complex_, user_override=router.override).route == "local_llm"
    router.mode = "auto"
    assert router.decide(complex_, user_override=router.override).route == "cloud_llm"


def test_health_names_the_models(setup):
    client, *_ = setup
    assert client.get("/v1/system/health").json()["llm"] == {"local_model": "qwen2.5:3b-instruct",
                                                             "cloud_model": None, "mode": "auto"}
