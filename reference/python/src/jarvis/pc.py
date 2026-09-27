"""PC-Steuerung über einen Agenten auf dem Rechner des Nutzers (Windows: deploy/windows/jarvis-pc-agent.ps1).

Der Kern läuft im Container und kann selbst keine Programme auf dem PC öffnen. Der Agent verbindet sich von sich
aus per WebSocket mit ``/v1/agent`` (kein offener Port auf dem PC) und führt nur eine feste Liste von Aktionen aus:
Webseite öffnen (nur http/https), Programm aus seiner eigenen Liste starten, bekannten Ordner öffnen. Welche
Programme erlaubt sind, entscheidet der Agent – der Kern kann ihm keine beliebigen Befehle schicken.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote_plus, urlsplit

from .errors import JarvisError
from .events import new_id
from .tools import Capability, InvocationContext, ToolRegistry

SendJson = Callable[[dict[str, Any]], Awaitable[None]]

DEFAULT_APPS = ["explorer", "browser", "editor", "rechner", "paint", "einstellungen", "taskmanager", "spotify",
                "word", "excel", "powerpoint", "outlook"]
FOLDERS = ["desktop", "documents", "downloads", "pictures", "music", "videos", "home", "pc"]
NOT_CONNECTED = ("Die PC-Steuerung ist nicht verbunden. Starten Sie JARVIS über die Desktop-Verknüpfung "
                 "(einrichten mit ./deploy/start.sh autostart).")


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

    def resolve(self, message: dict[str, Any]) -> None:
        future = self._pending.pop(str(message.get("id")), None)
        if future is not None and not future.done():
            future.set_result(message)

    async def invoke(self, action: str, arguments: dict[str, Any]) -> Any:
        if self._send is None:
            raise JarvisError("JRV-DEV-001", "PC-Agent nicht verbunden", user_message=NOT_CONNECTED)
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
                             search_url: str = "https://www.google.com/search?q={query}") -> None:
    async def open_url(args: dict[str, Any], ctx: InvocationContext) -> Any:
        parts = urlsplit(args["url"])
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise JarvisError("JRV-VAL-002", "Nur http- und https-Adressen",
                              user_message="Diese Adresse kann ich nicht öffnen.")
        return await hub.invoke("open_url", {"url": args["url"]})

    async def search_web(args: dict[str, Any], ctx: InvocationContext) -> Any:
        url = search_url.format(query=quote_plus(args["query"]))
        return await hub.invoke("open_url", {"url": url})

    async def open_app(args: dict[str, Any], ctx: InvocationContext) -> Any:
        return await hub.invoke("open_app", {"app": args["app"].strip().lower()})

    async def open_folder(args: dict[str, Any], ctx: InvocationContext) -> Any:
        return await hub.invoke("open_folder", {"folder": args["folder"]})

    registry.register(Capability(
        name="pc.open_app", domain="pc", risk_class="R1", side_effects="reversible", timeout_s=15.0,
        description="Öffnet ein Programm auf dem PC des Nutzers. Namen: explorer (Datei-Explorer), browser "
                    "(Webbrowser), editor, rechner, paint, einstellungen, taskmanager, spotify, word, excel, "
                    "powerpoint, outlook – weitere, falls der Nutzer sie im PC-Agenten eingetragen hat.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["app"], "properties": {
            "app": {"type": "string", "pattern": "^[A-Za-zÄÖÜäöüß0-9 _.-]{2,40}$"},
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
        description="Sucht im Internet: öffnet die Google-Suche mit dem Suchbegriff im Browser des PCs.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["query"], "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 300},
        }},
        handler=search_web,
    ))
