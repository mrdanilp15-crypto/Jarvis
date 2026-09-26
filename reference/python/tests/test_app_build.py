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
    assert background == []  # kein HA-Token, In-Memory-Bus


def test_with_api_key_cloud_is_enabled(config_path, monkeypatch):
    from jarvis.app import build

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    container, _ = build(config_path)
    assert container.router.cloud is not None and container.router.cloud.model == "claude-opus-5"
