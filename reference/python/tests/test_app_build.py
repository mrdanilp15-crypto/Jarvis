"""Verdrahtung aus der Beispielkonfiguration (ohne laufende Dienste)."""

import shutil

import pytest
import yaml

from conftest import REPO

pytest.importorskip("anthropic")
pytest.importorskip("httpx")


@pytest.fixture
def config_path(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "schemas").mkdir()
    for name in ("policies.yaml", "persona.jarvis.yaml", "persona.neutral.yaml"):
        shutil.copy(REPO / "config" / name, tmp_path / "config" / name)
    shutil.copy(REPO / "schemas" / "persona.schema.json", tmp_path / "schemas")
    cfg = yaml.safe_load((REPO / "config" / "jarvis.example.yaml").read_text(encoding="utf-8"))
    cfg["bus"]["backend"] = "memory"  # Redis wird im Test nicht benötigt
    path = tmp_path / "config" / "jarvis.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return path


def test_without_api_key_cloud_is_disabled(config_path, monkeypatch):
    from jarvis.app import build

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    container, background = build(config_path)
    assert container.router.cloud is None
    assert container.router.local.name == "ollama"
    # Smart Home (sucht Home Assistant), Timer-Planer und Vorwärmen des lokalen Modells (In-Memory-Bus)
    assert [job.__qualname__ for job in background] == ["SmartHome.run", "AlarmScheduler.run",
                                                         "build.<locals>.warm_up_local_model"]
    for job in background:
        job.close()
    assert container.router.local.num_ctx == 8192 and container.router.local.keep_alive == "24h"
    tools = {spec.name for spec in container.orchestrator.registry.tool_specs()}
    assert {"info__weather", "info__news", "info__wikipedia"} <= tools


def test_model_and_location_overrides_from_env(config_path, monkeypatch):
    from jarvis.app import build
    from jarvis.policy import Principal

    monkeypatch.setenv("JARVIS_LLM_MODEL", "qwen2.5:3b-instruct")
    monkeypatch.setenv("JARVIS_HOME_LOCATION", "Hamburg")
    container, background = build(config_path)
    for job in background:
        job.close()
    assert container.router.local.model == "qwen2.5:3b-instruct"
    who = Principal(actor="user:alex", role="adult", trust="trusted_user")
    assert "Ort des Nutzers: Hamburg" in container.situation(who, "voice").render()


def test_with_api_key_cloud_is_enabled(config_path, monkeypatch):
    from jarvis.app import build

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    container, background = build(config_path)
    for job in background:
        job.close()
    assert container.router.cloud is not None and container.router.cloud.model == "claude-opus-5-5"


def test_warm_up_retries_until_model_is_ready(config_path, monkeypatch):
    import asyncio

    from jarvis import app as app_module
    from jarvis.errors import JarvisError
    from jarvis.llm.ollama import OllamaProvider

    seen = {"attempts": [], "status_during_retry": None}
    holder = {}

    async def fake_warm_up(self, static, tools):
        seen["attempts"].append([t.name for t in tools])
        if len(seen["attempts"]) == 1:
            raise JarvisError("JRV-LLM-001", "Modell fehlt", user_message="'ollama pull …' ausführen")
        seen["status_during_retry"] = holder["container"].llm_status

    async def no_sleep(_seconds):
        return None

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(OllamaProvider, "warm_up", fake_warm_up)
    container, [smart_home, scheduler, warm_up] = app_module.build(config_path)
    smart_home.close()
    scheduler.close()
    holder["container"] = container
    monkeypatch.setattr(app_module.asyncio, "sleep", no_sleep)
    asyncio.run(warm_up)
    assert len(seen["attempts"]) == 2 and "info__weather" in seen["attempts"][0]
    assert seen["status_during_retry"] == "loading" and container.llm_status == "ready"


def test_user_name_is_never_taken_from_the_actor_id(config_path, monkeypatch):
    # Früher wurde aus „user:alex“ der Name „alex“ – der Nutzer heißt aber Daniel
    import json

    from jarvis.app import build

    monkeypatch.setenv("JARVIS_DEV_TOKENS", json.dumps({
        "tok-a": {"actor": "user:alex", "role": "adult", "trust": "trusted_user"},
        "tok-b": {"actor": "user:sam", "role": "adult", "trust": "trusted_user", "name": "Sam"},
    }))
    monkeypatch.delenv("JARVIS_USER_NAME", raising=False)
    container, background = build(config_path)
    for job in background:
        job.close()
    who = container.tokens["tok-a"]
    assert who.name is None and "alex" not in container.situation(who, "voice").render().lower()

    monkeypatch.setenv("JARVIS_USER_NAME", "Daniel")
    container, background = build(config_path)
    for job in background:
        job.close()
    assert container.tokens["tok-a"].name == "Daniel"
    assert container.tokens["tok-b"].name == "Sam"  # eigener Name im Token hat Vorrang
    assert "Sprecher: Daniel" in container.situation(container.tokens["tok-a"], "voice").render()
