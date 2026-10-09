"""Smart Home: Home Assistant finden, verbinden und Räume und Geräte übernehmen – ohne Handarbeit.

- **Finden:** bekannte Adressen (homeassistant.local, der PC selbst, Docker-Host) und das eigene Heimnetz (/24) auf
  Port 8123; erkannt wird Home Assistant an ``/auth/providers``.
- **Verbinden:** „Mit Home Assistant anmelden“ öffnet die HA-Anmeldung im Browser (OAuth, IndieAuth). Danach legt
  JARVIS sich selbst einen langlebigen Zugang an (``auth/long_lived_access_token``). Alternativ: Token einfügen.
  Gespeichert wird in ``data/home.json`` (Dateirechte 0600), nie an die Oberfläche zurückgegeben.
- **Übernehmen:** Räume, Geräte und Aliasse aus der HA-Registry. Die Sofortbefehle gelten sofort (``homeindex.py``),
  das Sprachmodell bekommt eine kurze Geräteliste mit Zuständen statt erfundener entity_ids.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import secrets
import socket
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from .connectors.homeassistant import HomeAssistantClient, register_home_capabilities
from .errors import JarvisError
from .homeindex import HomeIndex
from .tools import ToolRegistry

log = logging.getLogger(__name__)

PORT = 8123
# Wo Home Assistant meistens läuft: HA OS (mDNS), Docker-Profil „homeassistant“, Docker-Host, derselbe PC
CANDIDATES = ("http://homeassistant.local:8123", "http://homeassistant:8123", "http://host.docker.internal:8123",
              "http://127.0.0.1:8123")
OAUTH_TTL_S = 600
SYNC_INTERVAL_S = 300


def http_url(value: str) -> str:
    """„192.168.178.20“, „homeassistant.local:8123/“, „ws://…/api/websocket“ -> „http://192.168.178.20:8123“."""
    value = value.strip()
    if not value:
        raise JarvisError("JRV-VAL-001", "Adresse fehlt", user_message="Bitte die Adresse von Home Assistant angeben.")
    if "://" not in value:
        value = f"http://{value}"
    parts = urlsplit(value)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
    if scheme not in ("http", "https") or not parts.hostname:
        raise JarvisError("JRV-VAL-001", f"Ungültige Adresse: {value}",
                          user_message="Das ist keine gültige Adresse – etwa http://homeassistant.local:8123.")
    host = parts.hostname if ":" not in parts.hostname else f"[{parts.hostname}]"
    port = parts.port or (PORT if scheme == "http" and parts.port is None and value.count(":") == 1 else None)
    return f"{scheme}://{host}{f':{port}' if port else ''}"


def ws_url(url: str) -> str:
    return url.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/api/websocket"


class SmartHome:
    """Verbindung zu Home Assistant samt Geräteliste. Implementiert ``HomeApi`` für die home.*-Capabilities."""

    def __init__(self, *, path: Path, registry: ToolRegistry, env_url: str | None = None,
                 env_token: str | None = None,
                 on_state_changed: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
                 client_factory: Callable[..., Any] = HomeAssistantClient, scan_network: bool = True) -> None:
        self.path = path
        self.registry = registry
        self.fast_path: Any = None  # FastPath des Orchestrators – bekommt die Räume und Geräte
        self._env = (http_url(env_url), env_token) if env_url and env_token else None
        self._on_state_changed = on_state_changed
        self._client_factory = client_factory
        self._scan_network = scan_network
        self.client: Any = None
        self.index: HomeIndex | None = None
        self.info: dict[str, Any] = {}
        self.found: list[str] = []
        self.scanning = False
        self._oauth: dict[str, tuple[str, str, float]] = {}  # state -> (HA-Adresse, client_id, Ablauf)
        self._changed = asyncio.Event()
        self._registered = False

    # -- Zugang ----------------------------------------------------------------------------------------------------
    def _saved(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def credentials(self) -> tuple[str, str, str] | None:
        """(Adresse, Token, Quelle) – gespeichert aus der Oberfläche hat Vorrang vor deploy/.env."""
        saved = self._saved()
        if saved.get("url") and saved.get("token"):
            return saved["url"], saved["token"], "ui"
        if self._env:
            return self._env[0], self._env[1], "env"
        return None

    def _save(self, url: str, token: str, info: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"url": url, "token": token, "name": info.get("location_name"), "saved": datetime.now().isoformat()}
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, self.path)
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)
        self.info = info
        self._changed.set()

    def disconnect(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        self.index, self.info = None, {}
        if self.fast_path is not None:
            self.fast_path.set_home(None)
        self._changed.set()

    async def connect_with_token(self, url: str, token: str) -> dict[str, Any]:
        url = http_url(url)
        info = await probe(url, token.strip())
        self._save(url, token.strip(), info)
        return info

    def oauth_start(self, url: str, base: str) -> str:
        """Adresse der HA-Anmeldeseite. Nach der Anmeldung leitet HA zu ``/v1/settings/home/oauth`` zurück."""
        url = http_url(url)
        now = time.monotonic()
        self._oauth = {k: v for k, v in self._oauth.items() if v[2] > now}
        state = secrets.token_urlsafe(24)
        client_id = base.rstrip("/") + "/"
        self._oauth[state] = (url, client_id, now + OAUTH_TTL_S)
        query = urlencode({"client_id": client_id, "redirect_uri": f"{client_id}v1/settings/home/oauth",
                           "state": state, "response_type": "code"})
        return f"{url}/auth/authorize?{query}"

    async def oauth_finish(self, state: str, code: str) -> dict[str, Any]:
        pending = self._oauth.pop(state, None)
        if pending is None or pending[2] < time.monotonic():
            raise JarvisError("JRV-AUTH-001", "Unbekannter oder abgelaufener Anmeldevorgang",
                              user_message="Die Anmeldung ist abgelaufen. Bitte in JARVIS noch einmal auf "
                                           "„Mit Home Assistant anmelden“ klicken.")
        url, client_id, _ = pending
        access = await exchange_code(url, code, client_id)
        info = await probe(url, access, create_token=True)
        self._save(url, info.pop("token"), info)
        return info

    # -- Finden ----------------------------------------------------------------------------------------------------
    async def discover(self) -> list[str]:
        self.scanning = True
        try:
            hosts = list(CANDIDATES)
            if self._scan_network:
                hosts += [f"http://{ip}:{PORT}" for ip in await _open_hosts(_lan_hosts(), PORT)]
            results = await asyncio.gather(*(is_home_assistant(h) for h in hosts))
            found = [h for h, ok in zip(hosts, results, strict=True) if ok]
            self.found = await _unique(found)
            return self.found
        finally:
            self.scanning = False

    # -- Betrieb ---------------------------------------------------------------------------------------------------
    async def run(self) -> None:
        """Hält die Verbindung (neu bei geänderten Zugangsdaten) und sucht ohne Zugang einmal nach Home Assistant."""
        searched = False
        while True:
            self._changed.clear()
            creds = self.credentials()
            if creds is None:
                if not searched:
                    searched = True
                    with contextlib.suppress(Exception):
                        found = await self.discover()
                        if found:
                            log.info("Home Assistant gefunden: %s – verbinden im Zahnrad-Menü unter „Smart Home“",
                                     ", ".join(found))
                await self._changed.wait()
                continue
            url, token, _ = creds
            self.client = self._client_factory(ws_url(url), token, on_state_changed=self._state_changed,
                                               on_connected=self.sync)
            task = asyncio.create_task(self.client.run_forever())
            changed = asyncio.create_task(self._changed.wait())
            try:
                while not changed.done() and not task.done():
                    await asyncio.wait({task, changed}, timeout=SYNC_INTERVAL_S, return_when=asyncio.FIRST_COMPLETED)
                    if not changed.done() and self.connected:
                        with contextlib.suppress(Exception):
                            await self.sync()  # neue Geräte und Räume ohne Neustart übernehmen
            finally:
                for job in (task, changed):
                    job.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await job
                self.client = None
            if task.done() and not changed.done():
                await asyncio.sleep(5)

    @property
    def connected(self) -> bool:
        return bool(self.client is not None and self.client.connected.is_set())

    async def sync(self) -> None:
        """Räume und Geräte aus Home Assistant übernehmen (nach jeder Verbindung und alle fünf Minuten)."""
        client = self.client
        if client is None:
            return
        try:
            registry = await client.registry()
        except JarvisError:
            registry = None  # ältere HA-Versionen oder eingeschränkter Benutzer: Geräte ohne Räume
        with contextlib.suppress(JarvisError):
            config = await client.call({"type": "get_config"})
            self.info = {"location_name": config.get("location_name"), "version": config.get("version")}
        self.index = HomeIndex.build(client.states(), registry, self.info.get("location_name"))
        if self.fast_path is not None:
            self.fast_path.set_home(self.index)
        if not self._registered:
            register_home_capabilities(self.registry, self)
            self._registered = True
        log.info("Smart Home: %d Geräte in %d Räumen", len(self.index.devices), len(self.index.areas))

    async def _state_changed(self, data: dict[str, Any]) -> None:
        if self._on_state_changed is not None:
            await self._on_state_changed(data)

    # -- HomeApi für die home.*-Capabilities -----------------------------------------------------------------------
    def state(self, entity_id: str) -> dict[str, Any] | None:
        return self.client.state(entity_id) if self.client is not None else None

    async def call_service(self, domain: str, service: str, *, target: dict[str, Any],
                           data: dict[str, Any] | None = None) -> Any:
        if not self.connected:
            raise JarvisError("JRV-DEV-001", "Home Assistant nicht verbunden",
                              user_message="Home Assistant ist gerade nicht erreichbar.")
        return await self.client.call_service(domain, service, target=target, data=data)

    # -- Für Sprachmodell und Oberfläche ---------------------------------------------------------------------------
    def situation_lines(self) -> list[str]:
        return self.index.situation_lines(self.state) if self.index is not None and self.connected else []

    def status(self) -> dict[str, Any]:
        creds = self.credentials()
        index = self.index
        return {
            "configured": creds is not None,
            "source": creds[2] if creds else None,
            "url": creds[0] if creds else None,
            "connected": self.connected,
            "name": self.info.get("location_name") or self._saved().get("name"),
            "version": self.info.get("version"),
            "error": None if self.connected or self.client is None else getattr(self.client, "last_error", None),
            "devices": len(index.devices) if index else 0,
            "rooms": index.summary() if index else [],
            "found": self.found,
            "scanning": self.scanning,
        }


# -------------------------------------------------------------------------------------------------------------------
# Netzwerk: Home Assistant erkennen, Anmeldung, Zugang prüfen
# -------------------------------------------------------------------------------------------------------------------
async def is_home_assistant(url: str, timeout_s: float = 2.0) -> bool:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=False, trust_env=False) as http:
            response = await http.get(f"{url}/auth/providers")
    except (httpx.HTTPError, OSError):
        return False
    return response.status_code == 200 and ('"homeassistant"' in response.text or "Home Assistant" in response.text)


async def exchange_code(url: str, code: str, client_id: str) -> str:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as http:
            response = await http.post(f"{url}/auth/token", data={"grant_type": "authorization_code", "code": code,
                                                                  "client_id": client_id})
    except (httpx.HTTPError, OSError) as exc:
        raise _unreachable(url) from exc
    if response.status_code != 200 or "access_token" not in response.text:
        raise JarvisError("JRV-AUTH-001", f"Token-Tausch fehlgeschlagen ({response.status_code})",
                          user_message="Home Assistant hat die Anmeldung nicht bestätigt. Bitte noch einmal versuchen.")
    return response.json()["access_token"]


async def probe(url: str, token: str, *, create_token: bool = False, timeout_s: float = 8.0) -> dict[str, Any]:
    """Zugang prüfen (anmelden, Konfiguration lesen); optional einen langlebigen Zugang für JARVIS anlegen."""
    import websockets

    async def run() -> dict[str, Any]:
        async with websockets.connect(ws_url(url), open_timeout=timeout_s, max_size=4 * 1024 * 1024) as ws:
            hello = json.loads(await ws.recv())
            if hello.get("type") != "auth_required":
                raise _unreachable(url)
            await ws.send(json.dumps({"type": "auth", "access_token": token}))
            if json.loads(await ws.recv()).get("type") != "auth_ok":
                raise JarvisError("JRV-AUTH-001", "Token abgelehnt",
                                  user_message="Home Assistant hat den Zugang abgelehnt. Prüfen Sie das Token – oder "
                                               "nutzen Sie „Mit Home Assistant anmelden“.")
            await ws.send(json.dumps({"id": 1, "type": "get_config"}))
            config = (json.loads(await ws.recv()).get("result") or {})
            info = {"location_name": config.get("location_name"), "version": config.get("version")}
            if create_token:
                name = f"JARVIS ({datetime.now():%d.%m.%Y %H:%M})"
                await ws.send(json.dumps({"id": 2, "type": "auth/long_lived_access_token", "client_name": name,
                                          "lifespan": 3650}))
                reply = json.loads(await ws.recv())
                if not reply.get("success") or not isinstance(reply.get("result"), str):
                    raise JarvisError("JRV-AUTH-001", f"Langlebiger Zugang abgelehnt: {reply.get('error')}",
                                      user_message="Home Assistant hat keinen dauerhaften Zugang erlaubt. Ist der "
                                                   "Benutzer ein Administrator?")
                info["token"] = reply["result"]
            return info

    try:
        return await asyncio.wait_for(run(), timeout_s + 4)
    except JarvisError:
        raise
    except (OSError, TimeoutError, ValueError, websockets.WebSocketException) as exc:
        raise _unreachable(url) from exc


def _unreachable(url: str) -> JarvisError:
    return JarvisError("JRV-DEV-001", f"Home Assistant unter {url} nicht erreichbar",
                       user_message=f"Unter {url} antwortet kein Home Assistant. Stimmt die Adresse, und ist "
                                    "Home Assistant eingeschaltet?")


def _lan_hosts() -> list[str]:
    """Alle Adressen im eigenen Heimnetz (/24). Docker-Netze (172.16/12) und Fremdes werden übersprungen."""
    own: set[str] = set()
    with contextlib.suppress(OSError):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe_socket:
            probe_socket.connect(("192.0.2.1", 9))  # kein Paket wird gesendet – nur die ausgehende Adresse ermitteln
            own.add(probe_socket.getsockname()[0])
    with contextlib.suppress(OSError):
        own.update(socket.gethostbyname_ex(socket.gethostname())[2])
    hosts: list[str] = []
    for address in sorted(own):
        ip = ipaddress.ip_address(address)
        if not ip.is_private or ip.is_loopback or ip in ipaddress.ip_network("172.16.0.0/12"):
            continue
        network = ipaddress.ip_network(f"{address}/24", strict=False)
        hosts += [str(h) for h in network.hosts() if str(h) != address]
    return list(dict.fromkeys(hosts))


async def _open_hosts(hosts: list[str], port: int, timeout_s: float = 0.6) -> list[str]:
    limit = asyncio.Semaphore(128)

    async def check(host: str) -> str | None:
        async with limit:
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout_s)
            except (OSError, TimeoutError):
                return None
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return host

    return [h for h in await asyncio.gather(*(check(h) for h in hosts)) if h]


async def _unique(urls: list[str]) -> list[str]:
    """Dieselbe Instanz unter Name und IP nur einmal – der Name (homeassistant.local) gewinnt."""
    seen: set[str] = set()
    out = []
    for url in urls:
        host = urlsplit(url).hostname or ""
        try:
            infos = await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo, host, PORT, socket.AF_INET), 2)
            key = infos[0][4][0]
        except (OSError, TimeoutError, IndexError):
            key = host
        if key in ("127.0.0.1",) and any(u for u in out):
            continue
        if key not in seen:
            seen.add(key)
            out.append(url)
    return out
