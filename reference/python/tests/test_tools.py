import pytest

from jarvis.tools import ToolRegistry


def test_llm_tool_names_are_api_safe(registry):
    names = [spec.name for spec in registry.tool_specs()]
    assert names == sorted(names), "deterministische Reihenfolge für stabilen Prompt-Cache"
    for name in names:
        assert all(ch.isalnum() or ch in "_-" for ch in name)
        assert len(name) <= 64
    assert ToolRegistry.capability_name(ToolRegistry.llm_name("home.set_light")) == "home.set_light"


def test_domain_filter(registry):
    web_only = registry.tool_specs(frozenset({"web"}))
    assert [s.name for s in web_only] == ["web__fetch"]


def test_schema_validation_reports_pointers(registry):
    cap = registry.get("home.set_light")
    assert cap.validate({"entity_ids": ["light.kueche"], "on": True, "brightness_pct": 40}) == []
    errors = cap.validate({"entity_ids": ["switch.kaffee"], "on": "ja", "extra": 1})
    pointers = {e["pointer"] for e in errors}
    assert "/entity_ids/0" in pointers and "/on" in pointers and "/" in pointers
    assert cap.validate("kein objekt")[0]["message"].startswith("Argumente")


def test_argument_dependent_risk(registry):
    lock = registry.get("home.lock")
    assert lock.risk_for({"entity_id": "lock.haustuer", "state": "locked"}) == "R1"
    assert lock.risk_for({"entity_id": "lock.haustuer", "state": "unlocked"}) == "R3"
    assert lock.risk_for({}) == "R3", "ohne passende Regel gilt die konservative Basis"


def test_duplicate_registration_rejected(registry):
    with pytest.raises(ValueError):
        registry.register(registry.get("home.set_light"))
