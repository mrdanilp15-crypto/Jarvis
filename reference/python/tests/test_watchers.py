"""Beobachter („Sag Bescheid, wenn …“) und zeitgesteuerte Protokolle."""

import asyncio
from datetime import datetime

import pytest

from jarvis.errors import JarvisError
from jarvis.fastpath import FastPath
from jarvis.protocols import ProtocolStore
from jarvis.watchers import Probes, WatchService


class FakeProbes(Probes):
    def __init__(self):
        self.battery_state, self.procs, self.files = (80, False), {"handbrake.exe"}, {"alt.pdf": 0.0}

    def battery(self):
        return self.battery_state

    def running(self, name):
        return any(name.lower() in p for p in self.procs)

    def downloads(self):
        return dict(self.files)


def make(tmp_path):
    told, probes = [], FakeProbes()

    async def notify(actor, text):
        told.append((actor, text))

    return WatchService(tmp_path / "w.json", notify, probes, clock=lambda: 100.0), probes, told


@pytest.mark.parametrize("text, capability, arguments", [
    ("Sag Bescheid, wenn der Akku unter 15 Prozent fällt", "automation.watch", {"kind": "battery", "value": "15"}),
    ("Warne mich bei Akku unter 20 Prozent", "automation.watch", {"kind": "battery", "value": "20"}),
    ("Sag Bescheid, wenn der Download fertig ist", "automation.watch", {"kind": "download"}),
    ("Sag mir Bescheid, wenn Handbrake fertig ist", "automation.watch", {"kind": "process", "value": "handbrake"}),
    ("Was beobachtest du gerade?", "automation.watch_list", {}),
    ("Beende alle Beobachter", "automation.watch_cancel", {}),
])
def test_grammar(text, capability, arguments):
    found = FastPath({}).match(text)
    assert (found.capability, found.arguments) == (capability, arguments)


def test_timer_is_no_watch():
    found = FastPath({}).match("Sag Bescheid, wenn der Timer fertig ist")
    assert found is None or found.capability != "automation.watch"


def test_battery_fires_once_and_not_while_charging(tmp_path):
    service, probes, told = make(tmp_path)
    service.add("battery", "user:a", "15")
    probes.battery_state = (12, True)  # lädt
    assert asyncio.run(service.check()) == [] and told == []
    probes.battery_state = (12, False)
    asyncio.run(service.check())
    asyncio.run(service.check())
    assert told == [("user:a", "Der Akku steht bei 12 Prozent – es wäre Zeit für das Ladegerät.")]


def test_process_and_download(tmp_path):
    service, probes, told = make(tmp_path)
    service.add("process", "user:a", "handbrake")
    service.add("download", "user:a")
    asyncio.run(service.check())
    assert told == []
    probes.procs.clear()
    probes.files.update({"neu.zip.crdownload": 99.0, "neu.zip": 90.0, "frisch.zip": 99.5})  # frisch.zip: noch in Arbeit
    asyncio.run(service.check())
    assert ("user:a", "handbrake ist fertig.") in told
    assert ("user:a", "Der Download „neu.zip“ ist fertig.") in told and len(told) == 2
    assert service.items == []


def test_refusals_and_persistence(tmp_path):
    service, probes, _ = make(tmp_path)
    with pytest.raises(JarvisError, match="läuft nicht"):
        service.add("process", "user:a", "word")
    service.add("download", "user:a")
    again = WatchService(tmp_path / "w.json", lambda a, t: None, probes)
    assert [w.kind for w in again.items] == ["download"] and again.cancel("user:a") == 1


def test_scheduled_protocols_run_once_a_day_and_on_time(tmp_path):
    store = ProtocolStore(tmp_path / "p.json")
    store.save(None, "Wecker", [], ["Wie spät ist es?"], {"time": "7:00", "days": "weekdays"})
    protocol = store.get("wecker")
    assert protocol.schedule == {"time": "07:00", "days": "weekdays"}
    friday = datetime(2026, 10, 9, 7, 5)
    assert protocol.due(friday, None) and not protocol.due(friday, friday.date())
    assert not protocol.due(datetime(2026, 10, 9, 6, 59), None)  # zu früh
    assert not protocol.due(datetime(2026, 10, 9, 8, 0), None)  # verpasst (> 15 min)
    assert not protocol.due(datetime(2026, 10, 10, 7, 5), None)  # Samstag
    assert ProtocolStore(tmp_path / "p.json").get("wecker").schedule["time"] == "07:00"
    assert store.save("wecker", "Wecker", [], ["Wie spät ist es?"], {"time": "25:00"}).schedule is None
