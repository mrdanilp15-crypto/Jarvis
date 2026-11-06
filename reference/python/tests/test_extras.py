"""Fehlerberichte, Selbsttest, Sicherung, Wenn-dann-Regeln, eigenes Wissen und die neuen Sprachbefehle."""

import asyncio
import zipfile
from datetime import datetime

import pytest

from jarvis.backup import INCLUDE, make_backup, restore
from jarvis.fastpath import FastPath
from jarvis.feedback import FeedbackStore
from jarvis.orchestrator import TurnRequest
from jarvis.rules import RuleService
from jarvis.selftest import run_selftest, summary_text
from jarvis.testing import ScriptedProvider, say
from jarvis.userdocs import DocIndex

from conftest import ALEX


@pytest.mark.parametrize("text, capability, arguments", [
    ("Prüf dich", "system.selftest", {}), ("Warum bist du so langsam?", "system.selftest", {}),
    ("Sicher meine Daten", "system.backup", {}), ("Nenn mich bitte Boss", "assistant.set_address", {"name": "Boss"}),
    ("Lern ein Protokoll Feierabend: Alle Lichter aus, Bildschirm sperren", "automation.protocol_create",
     {"name": "Feierabend", "steps": ["Alle Lichter aus", "Bildschirm sperren"]}),
    ("Such in meinen Notizen nach Mietvertrag", "knowledge.search", {"query": "mietvertrag"}),
    ("Wenn es morgen regnet, erinner mich um 8 Uhr an den Schirm", "automation.rule_add", {"kind": "rain", "at": "08:00"}),
    ("Wenn es morgen friert, sag mir Bescheid", "automation.rule_add", {"kind": "frost"}),
    ("Wenn ich nach 23 Uhr noch am PC bin, sag es mir", "automation.rule_add", {"kind": "late", "at": "23:00"}),
    ("Alle Fenster minimieren", "pc.window", {"action": "show_desktop"}),
    ("Fenster links andocken", "pc.window", {"action": "snap_left"}),
    ("Lies mir vor, was ich kopiert habe", "pc.clipboard_read", {}),
    ("Dunkelmodus an", "pc.theme", {"mode": "dark"}), ("Dunkelmodus aus", "pc.theme", {"mode": "light"}),
])
def test_grammar(text, capability, arguments):
    found = FastPath({}).match(text)
    assert (found.capability, found.arguments) == (capability, arguments)


def test_ruf_mich_an_is_not_a_name():
    found = FastPath({}).match("Ruf mich an")
    assert found is None or found.capability != "assistant.set_address"


def test_feedback_store_and_voice(orchestrator, situation, tmp_path):
    from jarvis.style import JarvisStyle

    orchestrator.style = JarvisStyle()
    orchestrator.feedback = store = FeedbackStore(tmp_path / "fb.jsonl")

    def ask(text):
        return asyncio.run(orchestrator.handle_turn(TurnRequest(text, "f1", ALEX), provider=ScriptedProvider([say("Hm.")]),
                                                    situation=situation))

    assert "noch nichts" in ask("Das war falsch").text  # es gab noch keine Antwort
    ask("Mach das Licht in der Küche an")
    assert "Notiert" in ask("Das war falsch").text
    [entry] = store.recent()
    assert entry["said"] == "Mach das Licht in der Küche an" and entry["tools"] == ["home.set_light"]
    assert "Gesagt: Mach das Licht in der Küche an" in store.report() and entry["route"] == "fast_path"
    store.clear()
    assert store.recent() == []


def test_selftest_reports_slow_model_and_cpu_fallback():
    class Provider:
        def __init__(self, delay):
            self.delay = delay
            self._client = type("C", (), {"get": self.get})()

        async def complete(self, **kw):
            await asyncio.sleep(self.delay)

        async def get(self, path):
            return type("R", (), {"json": lambda s: {"models": [{"name": "qwen", "size": 100, "size_vram": 40}]}})()

    async def health():
        return {"stt": "browser", "tts": "off", "pc_agent": "disconnected", "smart_home": "off"}

    ticks = iter([0.0, 9.5])
    checks = asyncio.run(run_selftest(provider=Provider(0), health=health, clock=lambda: next(ticks)))
    by = {c["id"]: c for c in checks}
    assert by["llm"]["state"] == "fail" and "9.5 s" in by["llm"]["detail"]
    assert by["gpu"]["state"] == "warn" and "40 %" in by["gpu"]["detail"] and "kleineres" in by["gpu"]["hint"]
    assert by["pc"]["state"] == "warn" and by["home"]["state"] == "off"
    assert summary_text(checks, "Sir").startswith("3 von") or "Prüfungen mit Hinweis" in summary_text(checks, "Sir")


def test_backup_excludes_secrets_and_restores(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    for name in ("memory.db", "protocols.json", "home.json", "devices.json", "llm.json"):
        (data / name).write_text(name)
    path, count = make_backup(data, tmp_path / "out", now=datetime(2026, 11, 6, 9, 30))
    assert path.name == "jarvis-2026-11-06_09-30.zip" and count == 2
    with zipfile.ZipFile(path) as z:
        assert sorted(z.namelist()) == ["memory.db", "protocols.json"] and set(z.namelist()) <= set(INCLUDE)
    (data / "memory.db").unlink()
    assert restore(path, data) == ["memory.db", "protocols.json"] and (data / "memory.db").read_text() == "memory.db"
    for day in range(12):  # nur die letzten zehn bleiben
        make_backup(data, tmp_path / "out", now=datetime(2026, 12, 1 + day, 9, 0))
    assert len(list((tmp_path / "out").glob("jarvis-*.zip"))) == 10


def test_rules_fire_once_per_day(tmp_path):
    told = []
    week = [{"precipitation_probability_pct": 80, "temp_min_c": -2}, {"precipitation_probability_pct": 20, "temp_min_c": 4}]

    async def notify(actor, text):
        told.append(text)

    async def forecast():
        return {"week": week}

    service = RuleService(tmp_path / "r.json", notify, forecast, online=lambda actor: actor == "user:a")
    service.add("user:a", "rain", "08:00")  # vor 12 Uhr: heute
    service.add("user:a", "frost", None)  # 20:00: morgen
    service.add("user:a", "late", "23:00")
    service.add("user:b", "late", "23:00")
    morning = datetime(2026, 11, 6, 8, 5)
    assert asyncio.run(service.check(morning)) == ["Heute wird es voraussichtlich regnen (80 Prozent) – denken Sie an den Schirm."]
    assert asyncio.run(service.check(morning)) == []  # schon geprüft
    assert asyncio.run(service.check(datetime(2026, 11, 6, 20, 1))) == []  # morgen: 4 Grad – kein Frost
    week[1]["temp_min_c"] = -3
    service.items[1].last = ""
    assert "Morgen gibt es Frost (-3 Grad)" in asyncio.run(service.check(datetime(2026, 11, 6, 20, 2)))[0]
    late = asyncio.run(service.check(datetime(2026, 11, 6, 23, 3)))
    assert len(late) == 1 and "nach 23:00 Uhr" in late[0]  # nur user:a ist online
    assert RuleService(tmp_path / "r.json", notify, forecast).items[0].kind == "rain"  # überlebt Neustart
    assert service.cancel("user:a") == 3


def test_user_documents_are_searched_locally(tmp_path):
    (tmp_path / "Mietvertrag.txt").write_text("§ 5 Kaution\n\nDie Kaution beträgt drei Monatsmieten und wird auf einem "
                                              "Sparkonto angelegt.\n\n§ 6 Kündigung\n\nDie Kündigungsfrist beträgt drei Monate.")
    (tmp_path / "Rezept.md").write_text("Pfannkuchen: 250 g Mehl, 3 Eier, Milch.")
    (tmp_path / "kaputt.pdf").write_bytes(b"kein pdf")
    index = DocIndex(tmp_path)
    [first, *_] = index.search("Wie hoch ist die Kaution?")
    assert first["file"] == "Mietvertrag.txt" and "drei Monatsmieten" in first["text"]
    assert index.search("Mehl")[0]["file"] == "Rezept.md"
    assert index.search("Raumschiff") == [] and "kaputt.pdf" in index.skipped
    (tmp_path / "Neu.txt").write_text("Der Wifi-Schlüssel lautet Sonnenschein42.")
    assert index.search("Wifi Schlüssel")[0]["file"] == "Neu.txt"  # neue Datei wird ohne Neustart gefunden
