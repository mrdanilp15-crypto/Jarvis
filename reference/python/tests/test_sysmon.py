"""Systemmonitor (PC-Agent oder dieser Rechner) und feste Systemaktionen mit Risikoklassen."""

import asyncio
from datetime import datetime

import pytest

from jarvis.context import Situation
from jarvis.fastpath import FastPath
from jarvis.orchestrator import TurnRequest
from jarvis.pc import AgentHub
from jarvis.policy import Principal
from jarvis.style import JarvisStyle, sysmon_text
from jarvis.sysmon import SYSTEM_ACTIONS, local_snapshot, register_sysmon_capabilities
from jarvis.testing import ScriptedProvider, say

DANIEL = Principal(actor="user:owner", role="adult", trust="trusted_user", name="Daniel")
PC = {"source": "pc", "host": "DANIEL-PC", "cpu_percent": 23, "cpu_cores": 16, "cpu_name": "Ryzen 7",
      "memory_total_gb": 31.9, "memory_used_gb": 9.8, "uptime_hours": 5.5,
      "disks": [{"name": "C:", "total_gb": 476.0, "free_gb": 120.4}],
      "temperatures": [], "battery": None,
      "gpus": [{"name": "RTX 4070", "percent": 12, "celsius": 48, "memory_used_gb": 2.1, "memory_total_gb": 12.0}]}


def connected_agent():
    hub = AgentHub(timeout_s=1)
    hub.sent = []

    async def send(message):
        hub.sent.append(message)
        result = PC if message["action"] == "system_info" else {"action": message["arguments"]["name"], "done": True}
        hub.resolve({"id": message["id"], "ok": True, "result": result})

    hub.attach(send, {"name": "PC", "apps": [], "start_apps": [], "actions": ["system_info", "system_action"]})
    return hub


def test_local_snapshot_has_real_values():
    data = local_snapshot(sample_s=0.05)
    assert 0 <= data["cpu_percent"] <= 100 and data["memory_total_gb"] > 0 and data["cpu_cores"] >= 1
    assert data["source"] == "server" and isinstance(data["disks"], list) and len(data["top_processes"]) <= 5


@pytest.mark.parametrize("focus, text", [
    ("cpu", "Der Prozessor ist zu 23 Prozent ausgelastet. Grafikkarte RTX 4070: 12 Prozent Last, 48 Grad, "
            "2,1 von 12 GB Grafikspeicher."),
    ("memory", "Arbeitsspeicher: 9,8 von 31,9 GB belegt, 22,1 GB frei."),
    ("disk", "Laufwerk C: 120,4 von 476 GB frei."),
    ("temperature", "Grafikkarte: 48 Grad."),
    ("all", "Der Prozessor ist zu 23 Prozent ausgelastet. Arbeitsspeicher: 9,8 von 31,9 GB belegt, 22,1 GB frei. "
            "Laufwerk C: 120,4 von 476 GB frei. Grafikkarte bei 12 Prozent und 48 Grad."),
])
def test_monitor_answers_what_was_asked(focus, text):
    assert sysmon_text({**PC, "focus": focus}) == text


def test_without_temperatures_jarvis_says_why():
    assert "Administratorrechten" in sysmon_text({**PC, "gpus": [], "focus": "temperature"})


@pytest.mark.parametrize("text, capability, arguments", [
    ("Wie hoch ist die CPU-Auslastung?", "system.monitor", {"focus": "cpu"}),
    ("Wie viel Arbeitsspeicher ist noch frei?", "system.monitor", {"focus": "memory"}),
    ("Wie voll ist die Festplatte?", "system.monitor", {"focus": "disk"}),
    ("Wie warm ist die Grafikkarte?", "system.monitor", {"focus": "temperature"}),
    ("Systemmonitor", "system.monitor", {"focus": "all"}),
    ("Sperr den Bildschirm", "pc.system_action", {"name": "lock_screen"}),
    ("Leere den Papierkorb", "pc.system_action", {"name": "empty_recycle_bin"}),
    ("Such nach Updates", "pc.system_action", {"name": "check_updates"}),
    ("Starte den PC neu", "pc.system_action", {"name": "restart"}),
    ("Fahr den Rechner herunter", "pc.system_action", {"name": "shutdown"}),
    ("Brich das Herunterfahren ab", "pc.system_action", {"name": "cancel_shutdown"}),
])
def test_system_commands(text, capability, arguments):
    match = FastPath({}, {}).match(text)
    assert (match.capability, match.arguments) == (capability, arguments)


def run_dialog(orchestrator, *texts):
    hub = connected_agent()
    register_sysmon_capabilities(orchestrator.registry, hub)
    orchestrator.style = JarvisStyle()
    situation = Situation(now=datetime(2026, 10, 9, 9, 0), user_display="Daniel")
    results = []
    for text in texts:
        request = TurnRequest(text=text, session_id="sys", principal=DANIEL)
        results.append(asyncio.run(orchestrator.handle_turn(request, provider=ScriptedProvider([say("-")]),
                                                            situation=situation)))
    return hub, results


def test_monitor_and_safe_actions_through_the_policy(orchestrator):
    hub, (monitor, lock, restart) = run_dialog(orchestrator, "Wie viel Arbeitsspeicher ist noch frei?",
                                               "Sperr den Bildschirm", "Starte den PC neu")
    assert monitor.text == "Arbeitsspeicher: 9,8 von 31,9 GB belegt, 22,1 GB frei."
    assert lock.text == "Sehr wohl. Der Bildschirm ist gesperrt."
    # Neustart ist R3: erst nach Bestätigung in der App – der PC bekommt noch keinen Befehl
    assert restart.pending_confirmation is not None and "Soll ich den PC neu starten?" in restart.text
    assert [m["arguments"].get("name") for m in hub.sent if m["action"] == "system_action"] == ["lock_screen"]


def test_actions_are_a_fixed_list_with_risk_classes(orchestrator):
    register_sysmon_capabilities(orchestrator.registry, connected_agent())
    cap = orchestrator.registry.get("pc.system_action")
    assert cap.validate({"name": "format_c"}) and cap.validate({"name": "lock_screen", "command": "del *"})
    assert {name: cap.risk_for({"name": name}) for name in SYSTEM_ACTIONS} == {
        name: risk for name, (risk, _) in SYSTEM_ACTIONS.items()}
    assert cap.risk_for({"name": "shutdown"}) == "R3"
