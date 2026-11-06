"""Protokolle: mehrere Sofortbefehle nacheinander, Richtlinien gelten je Schritt, Unpassendes wird übersprungen."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from jarvis.api import Container, create_app
from jarvis.context import Situation
from jarvis.errors import JarvisError
from jarvis.events import InMemoryEventBus
from jarvis.llm.router import ModelRouter
from jarvis.orchestrator import TurnRequest
from jarvis.policy import Principal
from jarvis.protocols import ProtocolStore
from jarvis.testing import ScriptedProvider

from conftest import ALEX, GUEST


@pytest.mark.parametrize("text, protocol_id", [
    ("Gute Nacht", "gute-nacht"), ("Jarvis, gute Nacht!", "gute-nacht"), ("Ich gehe schlafen", "gute-nacht"),
    ("Protokoll Gute Nacht", "gute-nacht"), ("Starte Protokoll Morgenbriefing", "morgenbriefing"),
    ("Briefing", "morgenbriefing"),
])
def test_triggers(text, protocol_id):
    assert ProtocolStore(None).match(text).protocol.id == protocol_id


def test_no_protocol_and_unknown_name():
    store = ProtocolStore(None)
    assert store.match("Gute Nacht zusammen") is None and store.match("Licht an") is None
    unknown = store.match("Protokoll Hausparty")
    assert unknown.protocol is None and unknown.spoken_name == "Hausparty"


def test_store_edit_persist_and_conflicts(tmp_path):
    store = ProtocolStore(tmp_path / "protocols.json")
    party = store.save(None, "Hausparty", ["Party!"], ["Alle Lichter an", "  ", "Spiel Radio Bob im Wohnzimmer"])
    assert party.id == "hausparty" and party.steps == ["Alle Lichter an", "Spiel Radio Bob im Wohnzimmer"]
    with pytest.raises(JarvisError, match="Auslöser doppelt"):
        store.save(None, "Nachtruhe", ["Gute Nacht"], ["Alle Lichter aus"])
    with pytest.raises(JarvisError):
        store.save(None, "Leer", [], [])
    again = ProtocolStore(tmp_path / "protocols.json")
    assert again.match("Party").protocol.name == "Hausparty"
    assert again.remove("hausparty") and not again.remove("hausparty")
    assert [p.id for p in ProtocolStore(tmp_path / "protocols.json").items] == ["gute-nacht", "morgenbriefing"]


def run(orchestrator, text, principal=ALEX, situation=None):
    return asyncio.run(orchestrator.handle_turn(TurnRequest(text, "s1", principal), provider=ScriptedProvider([]),
                                                situation=situation))


def test_protocol_runs_steps_and_skips_the_rest(orchestrator, home, situation, tmp_path):
    orchestrator.protocols = ProtocolStore(tmp_path / "p.json")
    orchestrator.protocols.save(None, "Kino", ["Filmabend"], ["Mach das Licht in der Küche aus", "Mach das Licht im Wohnzimmer aus",
                                                              "Schreib ein Gedicht über Katzen"])
    home.states["light.kueche"]["state"] = "on"
    result = run(orchestrator, "Filmabend", situation=situation)
    assert result.route == "protocol" and home.state("light.kueche")["state"] == "off"
    steps = result.card["steps"]
    assert [s["status"] for s in steps] == ["succeeded", "succeeded", "skipped"]
    assert steps[2]["text"] == "nicht verstanden"  # nie ans Sprachmodell
    assert result.text.startswith("Protokoll „Kino“ ausgeführt") and "1 von 3" in result.text
    assert [a.capability for a in result.actions] == ["home.set_light", "home.set_light"]


def test_policies_apply_to_every_step(orchestrator, home, situation, tmp_path):
    orchestrator.protocols = ProtocolStore(tmp_path / "p.json")
    orchestrator.protocols.save(None, "Tür", ["Abschließen"], ["Mach das Licht in der Küche aus"])
    home.states["light.kueche"]["state"] = "on"
    result = run(orchestrator, "Protokoll Tür", principal=GUEST, situation=situation)
    # Gast darf Licht nur in bestimmten Räumen – das Protokoll erbt genau diese Rechte
    assert [s["status"] for s in result.card["steps"]] == [result.actions[0].status]


def test_unknown_protocol_lists_existing(orchestrator, situation, tmp_path):
    orchestrator.protocols = ProtocolStore(tmp_path / "p.json")
    result = run(orchestrator, "Protokoll Hausparty", situation=situation)
    assert "Hausparty" in result.text and "„Gute Nacht“" in result.text and result.route == "protocol"


def test_protocol_api(tmp_path):
    store = ProtocolStore(tmp_path / "p.json")
    daniel = Principal(actor="user:owner", role="adult", trust="trusted_user")

    class Style:
        name, version = "jarvis", "test"

        def error_message(self, message):
            return message

    container = Container(orchestrator=type("O", (), {"style": Style()})(), bus=InMemoryEventBus(),
                          router=ModelRouter(local=ScriptedProvider([]), cloud=None), tokens={"a": daniel},
                          webhook_secrets={}, situation=lambda who, ch: Situation(now=None), protocols=store)
    client = TestClient(create_app(container))
    headers = {"Authorization": "Bearer a"}
    assert [p["id"] for p in client.get("/v1/settings/protocols", headers=headers).json()["protocols"]] == [
        "gute-nacht", "morgenbriefing"]
    made = client.post("/v1/settings/protocols", headers=headers,
                       json={"name": "Arbeit", "triggers": ["An die Arbeit"], "steps": ["Öffne Outlook"]}).json()
    assert made["protocol"]["id"] == "arbeit"
    changed = client.put("/v1/settings/protocols/arbeit", headers=headers,
                         json={"name": "Arbeit", "triggers": [], "steps": ["Öffne Outlook", "Öffne Teams"]}).json()
    assert changed["protocol"]["steps"] == ["Öffne Outlook", "Öffne Teams"]
    clash = client.post("/v1/settings/protocols", headers=headers,
                        json={"name": "Nacht", "triggers": ["gute nacht"], "steps": ["Alle Lichter aus"]})
    assert clash.status_code == 422 and "startet bereits" in clash.json()["user_message"]
    assert client.delete("/v1/settings/protocols/arbeit", headers=headers).json() == {"removed": "arbeit"}
    assert client.put("/v1/settings/protocols/arbeit", headers=headers,
                      json={"name": "x", "steps": ["y"]}).status_code == 404


def test_night_remark_once_per_night(orchestrator, situation, tmp_path):
    from datetime import datetime

    from jarvis.style import JarvisStyle

    orchestrator.style = JarvisStyle()
    orchestrator.protocols = ProtocolStore(tmp_path / "p.json")
    night = situation.__class__(now=datetime(2026, 10, 10, 2, 14), area="wohnzimmer")
    first = run(orchestrator, "Wie spät ist es?", situation=night)
    assert "02:14 Uhr" in first.text and first.text.count("Uhr") >= 2  # Uhrzeit plus Bemerkung
    second = run(orchestrator, "Wie spät ist es?", situation=night)
    assert second.text.count("02:14") == 1  # nur einmal pro Nacht
    day = situation.__class__(now=datetime(2026, 10, 10, 14, 0), area="wohnzimmer")
    assert run(orchestrator, "Wie spät ist es?", situation=day).text.count("Uhr") == 1


def test_good_night_at_night_is_praised(orchestrator, situation, tmp_path):
    from datetime import datetime

    from jarvis.style import JarvisStyle

    orchestrator.style = JarvisStyle()
    orchestrator.protocols = ProtocolStore(tmp_path / "p.json")
    night = situation.__class__(now=datetime(2026, 10, 10, 1, 30), area="wohnzimmer")
    assert "Eine weise Entscheidung angesichts der Uhrzeit" in run(orchestrator, "Gute Nacht", situation=night).text
