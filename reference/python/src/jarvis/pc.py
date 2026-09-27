"""PC-Steuerung über einen Agenten auf dem Rechner des Nutzers (Windows: deploy/windows/jarvis-pc-agent.ps1).

Der Kern läuft im Container und kann selbst keine Programme auf dem PC öffnen. Der Agent verbindet sich von sich
aus per WebSocket mit ``/v1/agent`` (kein offener Port auf dem PC) und führt nur eine feste Liste von Aktionen aus:
Webseite öffnen (nur http/https), Programm starten (eigene Liste oder Eintrag im Windows-Startmenü, per Name),
bekannten Ordner öffnen, Dateisuche im Explorer öffnen. Der Kern schickt nur Namen – was davon wie gestartet wird,
entscheidet der Agent; beliebige Befehle oder Pfade kann der Kern nicht ausführen lassen.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote_plus, urlsplit

from .errors import JarvisError
from .events import new_id
from .tools import Capability, InvocationContext, ToolRegistry

SendJson = Callable[[dict[str, Any]], Awaitable[None]]

DEFAULT_APPS = ["explorer", "browser", "editor", "rechner", "paint", "einstellungen", "taskmanager", "systemsteuerung",
                "kamera", "uhr", "store", "snipping", "spotify", "word", "excel", "powerpoint", "outlook"]
FOLDERS = ["desktop", "documents", "downloads", "pictures", "music", "videos", "home", "pc"]
SEARCH_SITES = {
    "google": "https://www.google.com/search?q={query}",
    "youtube": "https://www.youtube.com/results?search_query={query}",
    "amazon": "https://www.amazon.de/s?k={query}",
    "wikipedia": "https://de.wikipedia.org/w/index.php?search={query}",
    "ebay": "https://www.ebay.de/sch/i.html?_nkw={query}",
}
NOT_CONNECTED = ("Die PC-Steuerung ist nicht verbunden. Starten Sie JARVIS über die Desktop-Verknüpfung "
                 "(einrichten mit ./deploy/start.sh autostart).")
OUTDATED = ("Der PC-Agent ist veraltet. Bitte führen Sie ./deploy/start.sh autostart erneut aus – "
            "danach steht auch diese Funktion bereit.")
LEGACY_ACTIONS = ("open_url", "open_app", "open_folder")  # Agenten vor 2.1.0 melden ihre Aktionen nicht


def _key(name: str) -> str:
    """Vergleichsform für Programmnamen: „Counter-Strike 2“ -> „counter strike 2“."""
    return re.sub(r"[\W_]+", " ", name.lower()).strip()


class AgentHub:
    """Verbindung zum PC-Agenten: Aufträge senden, Antworten den wartenden Aufrufen zuordnen."""

    def __init__(self, timeout_s: float = 10.0) -> None:
        self.timeout_s = timeout_s
        self.info: dict[str, Any] = {}
        self._send: SendJson | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    @property
    def connected(self) -> bool:
        return self._send is not None

    def attach(self, send: SendJson, info: dict[str, Any]) -> SendJson:
        """Neuer Agent ersetzt einen alten (z. B. nach Neustart des PCs)."""
        self._fail_pending("PC-Agent wurde neu verbunden")
        self._send, self.info = send, info
        return send

    def detach(self, handle: SendJson) -> None:
        if self._send is handle:
            self._send, self.info = None, {}
            self._fail_pending("Verbindung zum PC-Agenten getrennt")

    def update(self, info: dict[str, Any]) -> None:
        """Der Agent meldet eine geänderte Programmliste (neu installiert/entfernt)."""
        if self._send is not None:
            self.info.update({k: v for k, v in info.items() if k in ("apps", "start_apps")})

    def find_app(self, name: str) -> str | None:
        """Welches Programm ist mit „Steam“, „minecraft“ oder „Chrome“ gemeint?

        Sucht in den Programmen, die der Agent gemeldet hat (eigene Liste und Windows-Startmenü): exakter Name vor
        Namensanfang („Minecraft“ -> „Minecraft Launcher“) vor ganzem Wort („Chrome“ -> „Google Chrome“), bei
        Gleichstand der kürzeste Name. None = kein passendes Programm installiert. Ohne Verbindung oder ohne
        Startmenü-Liste (älterer Agent) bleibt der Name unverändert – dann antwortet der Agent selbst.
        """
        if not self.connected or not self.info.get("start_apps"):
            return name
        wanted = _key(name)
        if not wanted:
            return None
        builtin = [str(a) for a in self.info.get("apps") or []]
        for app in builtin:
            if _key(app) == wanted:
                return app
        best: tuple[int, int, str] | None = None
        for app in (str(a) for a in self.info.get("start_apps") or []):
            key = _key(app)
            if key == wanted or key.replace(" ", "") == wanted.replace(" ", ""):
                rank = 0
            elif key.startswith(wanted + " "):
                rank = 1
            elif f" {wanted} " in f" {key} ":
                rank = 2
            else:
                continue
            if best is None or (rank, len(app)) < best[:2]:
                best = (rank, len(app), app)
        return best[2] if best else None

    def supports(self, action: str) -> bool:
        return action in (self.info.get("actions") or LEGACY_ACTIONS)

    def resolve(self, message: dict[str, Any]) -> None:
        future = self._pending.pop(str(message.get("id")), None)
        if future is not None and not future.done():
            future.set_result(message)

    async def invoke(self, action: str, arguments: dict[str, Any]) -> Any:
        if self._send is None:
            raise JarvisError("JRV-DEV-001", "PC-Agent nicht verbunden", user_message=NOT_CONNECTED)
        if not self.supports(action):
            raise JarvisError("JRV-INT-001", f"PC-Agent kennt die Aktion {action} nicht", user_message=OUTDATED)
        request_id = new_id("agt")
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({"type": "agent.invoke", "id": request_id, "action": action, "arguments": arguments})
            reply = await asyncio.wait_for(future, self.timeout_s)
        except TimeoutError as exc:
            raise JarvisError("JRV-TMO-001", f"PC-Agent antwortet nicht ({action})",
                              user_message="Der PC reagiert gerade nicht.") from exc
        finally:
            self._pending.pop(request_id, None)
        if not reply.get("ok"):
            error = str(reply.get("error") or "unbekannter Fehler")
            raise JarvisError("JRV-INT-001", f"PC-Agent: {error}", user_message=error)
        return reply.get("result")

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_result({"ok": False, "error": reason})
        self._pending.clear()


def register_pc_capabilities(registry: ToolRegistry, hub: AgentHub, *,
                             search_url: str = SEARCH_SITES["google"]) -> None:
    async def open_url(args: dict[str, Any], ctx: InvocationContext) -> Any:
        parts = urlsplit(args["url"])
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise JarvisError("JRV-VAL-002", "Nur http- und https-Adressen",
                              user_message="Diese Adresse kann ich nicht öffnen.")
        return await hub.invoke("open_url", {"url": args["url"]})

    async def search_web(args: dict[str, Any], ctx: InvocationContext) -> Any:
        site = args.get("site", "google")
        template = search_url if site == "google" else SEARCH_SITES[site]
        return await hub.invoke("open_url", {"url": template.format(query=quote_plus(args["query"]))})

    async def search_files(args: dict[str, Any], ctx: InvocationContext) -> Any:
        return await hub.invoke("search_files", {"query": args["query"].strip()})

    async def open_app(args: dict[str, Any], ctx: InvocationContext) -> Any:
        name = args["app"].strip()
        return await hub.invoke("open_app", {"app": hub.find_app(name) or name})

    async def open_folder(args: dict[str, Any], ctx: InvocationContext) -> Any:
        return await hub.invoke("open_folder", {"folder": args["folder"]})

    registry.register(Capability(
        name="pc.open_app", domain="pc", risk_class="R1", side_effects="reversible", timeout_s=15.0,
        description="Startet ein Programm oder Spiel auf dem PC des Nutzers – jedes, das im Windows-Startmenü steht "
                    "(z. B. Steam, Discord, Minecraft, Visual Studio Code), per Name. Feste Namen: explorer "
                    "(Datei-Explorer), browser, editor, rechner, paint, einstellungen, taskmanager, systemsteuerung.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["app"], "properties": {
            # Nur ein Name – der Agent sucht ihn in seiner Liste bzw. im Startmenü; Pfade und Befehle sind tabu
            "app": {"type": "string", "minLength": 2, "maxLength": 80, "pattern": "^[^<>|\"*?\\\\/\\u0000-\\u001f]+$"},
        }},
        handler=open_app,
    ))
    registry.register(Capability(
        name="pc.open_folder", domain="pc", risk_class="R1", side_effects="reversible", timeout_s=15.0,
        description="Öffnet einen Ordner im Datei-Explorer des PCs: desktop, documents (Dokumente), downloads, "
                    "pictures (Bilder), music (Musik), videos, home (Benutzerordner), pc (Dieser PC).",
        input_schema={"type": "object", "additionalProperties": False, "required": ["folder"], "properties": {
            "folder": {"enum": FOLDERS},
        }},
        handler=open_folder,
    ))
    registry.register(Capability(
        # R2: nach dem Lesen fremder Inhalte (Taint) nur mit Bestätigung – Schutz vor untergeschobenen Links
        name="pc.open_url", domain="pc", risk_class="R2", side_effects="reversible", timeout_s=15.0,
        description="Öffnet eine Webseite im Browser des PCs, z. B. https://www.youtube.com. Nur http/https.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["url"], "properties": {
            "url": {"type": "string", "minLength": 8, "maxLength": 2000},
        }},
        handler=open_url,
    ))
    registry.register(Capability(
        name="pc.search_web", domain="pc", risk_class="R1", side_effects="reversible", timeout_s=15.0,
        description="Sucht im Internet und zeigt die Ergebnisse im Browser des PCs: Google (Standard) oder direkt "
                    "auf YouTube, Amazon, Wikipedia bzw. eBay (site).",
        input_schema={"type": "object", "additionalProperties": False, "required": ["query"], "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 300},
            "site": {"enum": list(SEARCH_SITES)},
        }},
        handler=search_web,
    ))
    registry.register(Capability(
        name="pc.search_files", domain="pc", risk_class="R1", side_effects="reversible", timeout_s=15.0,
        description="Sucht Dateien und Ordner auf dem PC: öffnet die Explorer-Suche im Benutzerordner.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["query"], "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 200},
        }},
        handler=search_files,
    ))
