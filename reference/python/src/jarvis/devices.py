"""Geräte im Heimnetz: Tablets und Handys als Raum-Satelliten.

- **Koppeln:** Ein Erwachsener erzeugt in der Oberfläche einen Gerätezugang für einen Raum. Das Tablet öffnet den
  Link bzw. QR-Code und ist danach „die Küche“: „Licht an“ meint die Küche, Timer melden sich dort.
- **Zugang:** eigener Token je Gerät (``device:<id>``), gespeichert in ``data/devices.json`` (Dateirechte 0600),
  jederzeit widerrufbar. Ohne Token sieht ein Gerät im Netz nur die Anmeldeseite.
- **Verschlüsselt:** Im Heimnetz läuft JARVIS über HTTPS (Port 8443) mit einem selbst ausgestellten Zertifikat –
  Browser geben Mikrofon und Kamera nur über HTTPS frei. Das Tablet fragt deshalb einmal nach, ob es der Adresse
  vertrauen soll.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .errors import JarvisError
from .policy import Principal

log = logging.getLogger(__name__)

LAN_PORT = 8443


def local_addresses() -> list[str]:
    """IPv4-Adressen dieses Rechners im Heimnetz (ohne Loopback und Docker-Netze).

    ``JARVIS_LAN_ADDRESS`` legt sie fest – nötig im Docker-Container, der die Adressen des Rechners nicht sieht."""
    fixed = [a.strip() for a in os.environ.get("JARVIS_LAN_ADDRESS", "").split(",") if a.strip()]
    if fixed:
        return [a for a in fixed if _is_ip(a)]
    found: set[str] = set()
    with contextlib.suppress(OSError):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # es wird nichts gesendet – nur die ausgehende Adresse ermitteln
            found.add(probe.getsockname()[0])
    with contextlib.suppress(OSError):
        found.update(socket.gethostbyname_ex(socket.gethostname())[2])
    out = []
    for address in sorted(found):
        ip = ipaddress.ip_address(address)
        if ip.is_private and not ip.is_loopback and ip not in ipaddress.ip_network("172.16.0.0/12"):
            out.append(address)
    return out


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def ensure_certificate(directory: Path) -> tuple[Path, Path]:
    """Selbst ausgestelltes Zertifikat für das Heimnetz (10 Jahre, alle lokalen Adressen) – einmal erzeugt."""
    directory.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = directory / "jarvis-cert.pem", directory / "jarvis-key.pem"
    addresses = local_addresses()
    marker = directory / "addresses.json"
    if cert_path.exists() and key_path.exists():
        try:
            if set(addresses) <= set(json.loads(marker.read_text(encoding="utf-8"))):
                return cert_path, key_path
        except (OSError, ValueError):
            pass  # unbekannt, für welche Adressen es gilt: neu ausstellen
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    host = socket.gethostname()
    names: list[x509.GeneralName] = [x509.DNSName("localhost"), x509.DNSName(host)]
    names += [x509.IPAddress(ipaddress.ip_address(a)) for a in ["127.0.0.1", *addresses]]
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"JARVIS ({host})")])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    key_bytes = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption())
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key_bytes)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    marker.write_text(json.dumps(addresses), encoding="utf-8")
    log.info("Zertifikat für das Heimnetz ausgestellt: %s", ", ".join(addresses) or "nur localhost")
    return cert_path, key_path


def qr_svg(text: str) -> str | None:
    """QR-Code als SVG (für das Koppeln per Tablet-Kamera) – None ohne das Paket segno."""
    try:
        import segno
    except ImportError:
        return None
    return segno.make(text, error="m").svg_inline(scale=5, dark="#03121a", light="#ffffff", border=3)


class DeviceRegistry:
    """Gekoppelte Geräte. Die Tokens landen zusätzlich in ``tokens`` (dem Token-Verzeichnis der API)."""

    def __init__(self, path: Path, tokens: dict[str, Principal], *, lan_enabled: bool = False,
                 lan_port: int = LAN_PORT) -> None:
        self.path = path
        self.tokens = tokens
        self.lan_enabled = lan_enabled
        self.lan_port = lan_port
        self.devices: list[dict[str, Any]] = self._load()
        for device in self.devices:
            self.tokens[device["token"]] = self._principal(device)

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [d for d in data if isinstance(d, dict) and d.get("token") and d.get("id")]
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(self.devices, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    @staticmethod
    def _principal(device: dict[str, Any]) -> Principal:
        # Ein Raumgerät handelt wie ein Erwachsener des Haushalts – im Raum, dem es zugeordnet ist
        return Principal(actor=f"device:{device['id']}", role="adult", trust="household", area=device.get("area"),
                         name=device.get("user_name"))

    def create(self, name: str, area: str | None, area_name: str | None = None,
               user_name: str | None = None) -> dict[str, Any]:
        name = re.sub(r"\s+", " ", name).strip()[:40]
        if not name:
            raise JarvisError("JRV-VAL-001", "Gerätename fehlt", user_message="Bitte einen Namen für das Gerät angeben.")
        base = re.sub(r"[^a-z0-9]+", "-", name.lower().translate(str.maketrans("äöüß", "aous"))).strip("-") or "geraet"
        device_id = base
        while any(d["id"] == device_id for d in self.devices):
            device_id = f"{base}-{secrets.token_hex(2)}"
        device = {"id": device_id, "name": name, "area": area or None, "area_name": area_name or None,
                  "user_name": user_name, "token": f"dev-{secrets.token_urlsafe(24)}",
                  "created": datetime.now(UTC).isoformat(timespec="seconds")}
        self.devices.append(device)
        self.tokens[device["token"]] = self._principal(device)
        self._save()
        return device

    def remove(self, device_id: str) -> bool:
        device = next((d for d in self.devices if d["id"] == device_id), None)
        if device is None:
            return False
        self.devices.remove(device)
        self.tokens.pop(device["token"], None)
        self._save()
        return True

    def lan_urls(self) -> list[str]:
        return [f"https://{address}:{self.lan_port}" for address in local_addresses()] if self.lan_enabled else []

    def link(self, device: dict[str, Any]) -> str | None:
        urls = self.lan_urls()
        if not urls:
            return None
        from urllib.parse import quote

        room = quote(device.get("area_name") or device.get("name") or "")
        return f"{urls[0]}/#token={device['token']}&wake=1&room={room}"

    def public(self, device: dict[str, Any]) -> dict[str, Any]:
        """Für die Oberfläche – ohne Token."""
        return {k: device.get(k) for k in ("id", "name", "area", "area_name", "created")}
