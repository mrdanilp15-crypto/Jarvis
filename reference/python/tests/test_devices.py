"""Raum-Satelliten: Tablets koppeln (Token je Gerät, Raum, widerrufbar), Heimnetz-Zertifikat, QR-Code."""

import ipaddress
import json
import os
import stat
from urllib.parse import unquote

import pytest
from cryptography import x509
from fastapi.testclient import TestClient

from jarvis import devices as devices_module
from jarvis.api import Container, create_app
from jarvis.devices import DeviceRegistry, ensure_certificate, qr_svg
from jarvis.events import InMemoryEventBus
from jarvis.llm.router import ModelRouter
from jarvis.policy import Principal
from jarvis.testing import ScriptedProvider

DANIEL = Principal(actor="user:owner", role="adult", trust="trusted_user", name="Daniel")
CHILD = Principal(actor="user:kind", role="child", trust="household")


@pytest.fixture(autouse=True)
def lan_address(monkeypatch):
    monkeypatch.setenv("JARVIS_LAN_ADDRESS", "192.168.178.20")


def test_registry_creates_persists_and_revokes(tmp_path):
    tokens = {}
    registry = DeviceRegistry(tmp_path / "devices.json", tokens, lan_enabled=True)
    device = registry.create("  Küchen-Tablet ", "kueche", "Küche", user_name="Daniel")
    assert device["id"] == "kuchen-tablet" and device["token"].startswith("dev-")
    who = tokens[device["token"]]
    assert (who.actor, who.role, who.area, who.name) == ("device:kuchen-tablet", "adult", "kueche", "Daniel")
    assert stat.S_IMODE(os.stat(tmp_path / "devices.json").st_mode) == 0o600
    assert registry.create("Küchen-Tablet", "kueche")["id"].startswith("kuchen-tablet-")
    assert "token" not in registry.public(device)
    link = registry.link(device)
    assert link.startswith("https://192.168.178.20:8443/#token=dev-") and unquote(link).endswith("&wake=1&room=Küche")

    reloaded = {}
    again = DeviceRegistry(tmp_path / "devices.json", reloaded)
    assert len(again.devices) == 2 and reloaded[device["token"]].area == "kueche"
    assert again.lan_urls() == [] and again.link(device) is None  # Heimnetz aus
    assert again.remove("kuchen-tablet") and device["token"] not in reloaded
    assert not again.remove("kuchen-tablet")
    assert len(json.loads((tmp_path / "devices.json").read_text(encoding="utf-8"))) == 1


def test_certificate_covers_lan_address_and_is_reused(tmp_path):
    cert_path, key_path = ensure_certificate(tmp_path)
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "192.168.178.20" in {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
    assert "localhost" in san.get_values_for_type(x509.DNSName)
    assert stat.S_IMODE(os.stat(key_path).st_mode) == 0o600
    before = cert_path.read_bytes()
    assert ensure_certificate(tmp_path) and cert_path.read_bytes() == before  # gleiche Adresse: bleibt


def test_certificate_is_renewed_for_new_address(tmp_path, monkeypatch):
    cert_path, _ = ensure_certificate(tmp_path)
    before = cert_path.read_bytes()
    monkeypatch.setenv("JARVIS_LAN_ADDRESS", "10.0.0.7")
    ensure_certificate(tmp_path)
    assert cert_path.read_bytes() != before


def test_qr_code_is_plain_svg():
    svg = qr_svg("https://192.168.178.20:8443/#token=dev-x&wake=1&room=K%C3%BCche")
    assert svg.startswith("<svg") and "<script" not in svg and "style=" not in svg


def test_addresses_without_override_skip_docker_networks(monkeypatch):
    monkeypatch.delenv("JARVIS_LAN_ADDRESS")
    for address in devices_module.local_addresses():
        ip = ipaddress.ip_address(address)
        assert ip.is_private and not ip.is_loopback and ip not in ipaddress.ip_network("172.16.0.0/12")


class Areas:
    areas = {"kueche": "Küche", "wohnzimmer": "Wohnzimmer"}


def make_client(tmp_path, *, lan=True):
    tokens = {"adult": DANIEL, "kind": CHILD}
    registry = DeviceRegistry(tmp_path / "devices.json", tokens, lan_enabled=lan)

    class Style:
        name, version = "jarvis", "test"

        def error_message(self, message):
            return message

    container = Container(orchestrator=type("O", (), {"style": Style()})(), bus=InMemoryEventBus(),
                          router=ModelRouter(local=ScriptedProvider([]), cloud=None), tokens=tokens,
                          webhook_secrets={}, situation=lambda who, ch: None, devices=registry)
    container.smarthome = type("S", (), {"index": Areas()})()
    return TestClient(create_app(container)), tokens


def test_pairing_api(tmp_path):
    client, tokens = make_client(tmp_path)
    adult = {"Authorization": "Bearer adult"}
    listed = client.get("/v1/settings/devices", headers=adult).json()
    assert listed["lan"] == {"enabled": True, "urls": ["https://192.168.178.20:8443"]}
    assert {"id": "kueche", "name": "Küche"} in listed["rooms"] and listed["devices"] == []

    paired = client.post("/v1/settings/devices", headers=adult, json={"name": "Küchen-Tablet", "area": "küche"}).json()
    assert paired["device"]["area"] == "kueche" and paired["device"]["area_name"] == "Küche"
    assert "token" not in paired["device"] and paired["qr_svg"].startswith("<svg")
    device_token = paired["url"].split("#token=")[1].split("&")[0]
    assert tokens[device_token].area == "kueche"

    # Das Tablet ist angemeldet, koppelt aber keine weiteren Geräte
    tablet = {"Authorization": f"Bearer {device_token}"}
    assert client.get("/v1/settings/devices", headers=tablet).status_code == 403
    assert client.get("/v1/settings/devices", headers={"Authorization": "Bearer kind"}).status_code == 403

    assert client.delete("/v1/settings/devices/kuchen-tablet", headers=adult).json() == {"removed": "kuchen-tablet"}
    assert device_token not in tokens
    assert client.get("/v1/settings/devices", headers=tablet).status_code == 401
    assert client.delete("/v1/settings/devices/kuchen-tablet", headers=adult).status_code == 404


def test_pairing_needs_lan(tmp_path):
    client, _ = make_client(tmp_path, lan=False)
    reply = client.post("/v1/settings/devices", headers={"Authorization": "Bearer adult"}, json={"name": "Tablet"})
    assert reply.status_code == 404 and "JARVIS_LAN=on" in reply.json()["user_message"]
    assert client.get("/v1/settings/devices", headers={"Authorization": "Bearer adult"}).json()["lan"]["urls"] == []
