"""PC-Steuerung über einen Agenten auf dem Rechner des Nutzers (Windows: deploy/windows/jarvis-pc-agent.ps1).

Der Kern läuft im Container und kann selbst keine Programme auf dem PC öffnen. Der Agent verbindet sich von sich
aus per WebSocket mit ``/v1/agent`` (kein offener Port auf dem PC) und führt nur eine feste Liste von Aktionen aus:
Webseite öffnen (nur http/https), Programm starten (eigene Liste oder Eintrag im Windows-Startmenü, per Name),
bekannten Ordner öffnen, Dateien im Benutzerordner suchen und öffnen (Programme darunter werden nur im Explorer
markiert, nie gestartet). Der Kern schickt nur Namen, Suchbegriffe und Trefferummern – was davon wie geöffnet wird,
entscheidet der Agent; beliebige Befehle oder Pfade kann der Kern nicht ausführen lassen.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, quote_plus, urlsplit

from .errors import JarvisError
from .events import new_id
from .tools import Capability, InvocationContext, ToolRegistry
from .websearch import SITES as LINK_SITES, WebSearch, ducky_url, scoped

log = logging.getLogger(__name__)
SendJson = Callable[[dict[str, Any]], Awaitable[None]]

DEFAULT_APPS = ["explorer", "browser", "editor", "rechner", "paint", "einstellungen", "taskmanager", "systemsteuerung",
                "kamera", "uhr", "store", "snipping", "spotify", "word", "excel", "powerpoint", "outlook"]
FOLDERS = ["desktop", "documents", "downloads", "pictures", "music", "videos", "home", "pc"]
# Such-Adressen je Dienst: {query} = Formular-Kodierung, {path} = Pfad-Kodierung
SEARCH_SITES = {
    "google": "https://www.google.com/search?q={query}",
    "youtube": "https://www.youtube.com/results?search_query={query}",
    "amazon": "https://www.amazon.de/s?k={query}",
    "wikipedia": "https://de.wikipedia.org/w/index.php?search={query}",
    "ebay": "https://www.ebay.de/sch/i.html?_nkw={query}",
    "spotify": "https://open.spotify.com/search/{path}",
    "netflix": "https://www.netflix.com/search?q={query}",
    "twitch": "https://www.twitch.tv/search?term={query}",
    "github": "https://github.com/search?q={query}",
    "reddit": "https://www.reddit.com/search/?q={query}",
    "maps": "https://www.google.com/maps/search/{path}",
    "idealo": "https://www.idealo.de/preisvergleich/MainSearchProductCategory.html?q={query}",
    "chefkoch": "https://www.chefkoch.de/rs/s0/{path}/Rezepte.html",
    "tiktok": "https://www.tiktok.com/search?q={query}",
    "bing": "https://www.bing.com/search?q={query}",
    "soundcloud": "https://soundcloud.com/search?q={query}",
    "steam": "https://store.steampowered.com/search/?term={query}",
    "zalando": "https://www.zalando.de/katalog/?q={query}",
    "otto": "https://www.otto.de/suche/{path}/",
    "mediamarkt": "https://www.mediamarkt.de/de/search.html?query={query}",
    "kleinanzeigen": "https://www.kleinanzeigen.de/s-{path}/k0",
    "pinterest": "https://www.pinterest.de/search/pins/?q={query}",
    "imdb": "https://www.imdb.com/find/?q={query}",
    "duckduckgo": "https://duckduckgo.com/?q={query}",
}
SITE_NAMES = {"maps": "Google Maps", "youtube": "YouTube", "github": "GitHub", "tiktok": "TikTok", "imdb": "IMDb",
              "mediamarkt": "MediaMarkt", "soundcloud": "SoundCloud", "duckduckgo": "DuckDuckGo", "ebay": "eBay"}
APP_SEARCH = ("spotify",)  # Dienste mit eigener App: dort suchen, wenn installiert (sonst im Browser)


KEYS = ["enter", "tab", "escape", "space", "backspace", "delete", "up", "down", "left", "right", "page_up", "page_down",
        "home", "end", "refresh", "fullscreen", "copy", "paste", "cut", "undo", "redo", "select_all", "save", "find",
        "print", "new_tab", "close_tab", "reopen_tab", "next_tab", "previous_tab", "back", "forward", "zoom_in",
        "zoom_out", "switch_window", "close_window", "play_pause", "next_track", "previous_track", "stop_media",
        "volume_up", "volume_down", "mute"]
MAIL_WEB = {  # E-Mail-Entwurf im Browser statt im Mailprogramm (pc_agent.mail_compose)
    "gmail": "https://mail.google.com/mail/?view=cm&fs=1&to={to}&su={subject}&body={body}",
    "outlook": "https://outlook.live.com/mail/0/deeplink/compose?to={to}&subject={subject}&body={body}",
}
EMAIL = re.compile(r"^[^@\s<>\"]+@[^@\s<>\"]+\.[A-Za-z]{2,}$")


_SPOKEN_CHARS = ((" at-zeichen ", "@"), (" at zeichen ", "@"), (" klammeraffe ", "@"), (" at ", "@"), (" ät ", "@"),
                 (" et ", "@"), (" ett ", "@"), (" punkt ", "."), (" dot ", "."), (" minus ", "-"),
                 (" bindestrich ", "-"), (" strich ", "-"), (" unterstrich ", "_"))
_TRAILING_NOISE = re.compile(r"(?:\s(?:und|bitte|danke|so|ja|genau|fertig|das wars|das war's|ähm|äh))+\s*$")


def spoken_email(text: str) -> str | None:
    """Diktierte Adresse -> E-Mail-Adresse, so wie die Spracherkennung sie schreibt:
    „max punkt mustermann at gmail punkt com“ -> „max.mustermann@gmail.com“,
    „Hegemann Punkt Daniel at GMX, Punkt. DE. Und.“ -> „hegemann.daniel@gmx.de“ (Satzzeichen und Füllwörter fallen weg),
    „H E G E M A N N at gmx punkt de“ -> buchstabiert zusammengesetzt."""
    value = text.strip().lower()
    value = re.sub(r"[,;!?„“\"]", " ", value)
    value = re.sub(r"\.(?=\s|$)", " ", value)  # Satzpunkte der Erkennung („Punkt. DE.“); „gmx.de“ bleibt
    value = " " + re.sub(r"\s+", " ", value).strip() + " "
    value = _TRAILING_NOISE.sub(" ", value)
    value = re.sub(r"^\s*(?:an|die adresse(?: ist| lautet)?|adresse|e-?mail(?:-adresse)?(?: ist)?)\s+", " ", value)
    for _ in range(2):  # zweimal: aufeinanderfolgende Zeichen („punkt punkt“) teilen sich ein Leerzeichen
        for spoken, char in _SPOKEN_CHARS:
            value = value.replace(spoken, f" {char} ")
    value = re.sub(r"\s+", "", value)
    return value if EMAIL.match(value) else None


def resolve_recipient(text: str, contacts: dict[str, str] | None = None) -> str | None:
    """Empfänger -> Adresse: diktiert („… at gmx punkt de“), geschrieben oder ein bekannter Kontakt („Mama“)."""
    wanted = re.sub(r"^(?:an|für)\s+", "", text.strip().strip(".,!?"), flags=re.I)
    known = {k.strip().lower(): v for k, v in (contacts or {}).items() if EMAIL.match(str(v))}
    return spoken_email(wanted) or known.get(wanted.lower()) or (wanted if EMAIL.match(wanted) else None)


def build_search_url(template: str, query: str) -> str:
    return template.format(query=quote_plus(query), path=quote(query, safe=""))
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
        if best is None and len(wanted) >= 4:
            # Hörfehler der Spracherkennung („Discort“, „Minecraf“): sehr ähnlicher Name oder Namensanfang mit
            # gleichem Anfangsbuchstaben – verglichen wird auch nur der Anfang („minecraf“ ~ „minecraft“ Launcher)
            words = len(wanted.split())
            candidates: dict[str, str] = {}
            for app in [*builtin, *(str(a) for a in self.info.get("start_apps") or [])]:
                key = _key(app)
                for variant in (key, " ".join(key.split()[:words])):
                    if variant[:1] == wanted[:1]:
                        candidates.setdefault(variant, app)
            close = difflib.get_close_matches(wanted, list(candidates), n=1, cutoff=0.8)
            return candidates[close[0]] if close else None
        return best[2] if best else None

    def require(self, action: str) -> None:
        """Vorab prüfen, ob der Agent die Aktion ausführen kann (spart z. B. eine Websuche ohne Agent)."""
        if self._send is None:
            raise JarvisError("JRV-DEV-001", "PC-Agent nicht verbunden", user_message=NOT_CONNECTED)
        if not self.supports(action):
            raise JarvisError("JRV-INT-001", f"PC-Agent kennt die Aktion {action} nicht", user_message=OUTDATED)

    async def notify(self, title: str, text: str) -> bool:
        """Windows-Hinweis über den Agenten (Timer, Erinnerungen); ohne Agent oder bei Fehlern still False."""
        if not self.connected or not self.supports("notify"):
            return False
        try:
            await self.invoke("notify", {"title": title, "text": text})
        except JarvisError:
            return False
        return True

    def supports(self, action: str) -> bool:
        return action in (self.info.get("actions") or LEGACY_ACTIONS)

    def resolve(self, message: dict[str, Any]) -> None:
        future = self._pending.pop(str(message.get("id")), None)
        if future is not None and not future.done():
            future.set_result(message)

    async def invoke(self, action: str, arguments: dict[str, Any]) -> Any:
        self.require(action)
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
                             search_url: str = SEARCH_SITES["google"], web: WebSearch | None = None,
                             mail_compose: str = "mailto", contacts: dict[str, str] | None = None) -> None:
    search_url_default = search_url
    known = {k.strip().lower(): v for k, v in (contacts or {}).items() if EMAIL.match(str(v))}
    async def open_url(args: dict[str, Any], ctx: InvocationContext) -> Any:
        parts = urlsplit(args["url"])
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise JarvisError("JRV-VAL-002", "Nur http- und https-Adressen",
                              user_message="Diese Adresse kann ich nicht öffnen.")
        return await hub.invoke("open_url", {"url": args["url"]})

    async def search_web(args: dict[str, Any], ctx: InvocationContext) -> Any:
        site = args.get("site", "google")
        if site in APP_SEARCH and hub.supports("app_search"):
            # „Such Arteriion auf Spotify“: in der Spotify-App, falls installiert – sonst der Web-Player
            result = await hub.invoke("app_search", {"app": site, "query": args["query"]})
            if isinstance(result, dict) and result.get("opened"):
                return {"site": site, "in_app": True}
        template = search_url_default if site == "google" else SEARCH_SITES[site]
        await hub.invoke("open_url", {"url": build_search_url(template, args["query"])})
        return {"site": site, "in_app": False}

    async def search_files(args: dict[str, Any], ctx: InvocationContext) -> Any:
        return await hub.invoke("search_files", {"query": args["query"].strip()})

    async def open_link(args: dict[str, Any], ctx: InvocationContext) -> Any:
        """Link heraussuchen und direkt öffnen: erster Treffer der Websuche, sonst leitet DuckDuckGo weiter."""
        hub.require("open_url")
        query = scoped(args["query"], args.get("site"))
        first = None
        if web is not None:
            try:
                results = await web.search(query, limit=3)
                first = results[0] if results else None
            except JarvisError as exc:
                log.info("Websuche nicht verfügbar – Weiterleitung im Browser", extra={"detail": exc.detail})
        if first is None:
            await hub.invoke("open_url", {"url": ducky_url(query)})
            return {"query": args["query"], "via": "browser"}
        await hub.invoke("open_url", {"url": first.url})
        return {"query": args["query"], "via": "search", "url": first.url, "title": first.title}

    async def find_files(args: dict[str, Any], ctx: InvocationContext) -> Any:
        return await hub.invoke("find_files", {"query": args["query"].strip(), "kind": args.get("kind", "any")})

    async def open_file(args: dict[str, Any], ctx: InvocationContext) -> Any:
        if not args.get("query") and not args.get("id"):
            raise JarvisError("JRV-VAL-002", "query oder id erforderlich",
                              user_message="Welche Datei soll ich öffnen?")
        return await hub.invoke("open_file", {k: v for k, v in args.items() if v not in (None, "")})

    async def close_app(args: dict[str, Any], ctx: InvocationContext) -> Any:
        name = args["app"].strip()
        return await hub.invoke("close_app", {"app": hub.find_app(name) or name})

    async def type_text(args: dict[str, Any], ctx: InvocationContext) -> Any:
        return await hub.invoke("type_text", {"text": args["text"], "enter": bool(args.get("enter"))})

    async def press_key(args: dict[str, Any], ctx: InvocationContext) -> Any:
        return await hub.invoke("press_key", {"key": args["key"], "times": args.get("times", 1)})

    async def click(args: dict[str, Any], ctx: InvocationContext) -> Any:
        return await hub.invoke("click", {"label": args["label"].strip()})

    async def compose_mail(args: dict[str, Any], ctx: InvocationContext) -> Any:
        """Entwurf öffnen – abschicken muss der Nutzer selbst (JARVIS versendet nie in seinem Namen)."""
        wanted = (args.get("to") or "").strip()
        address = resolve_recipient(wanted, known) or "" if wanted else ""
        draft = {"to": address, "subject": args.get("subject", ""), "body": args.get("body", "")}
        if mail_compose in MAIL_WEB:
            url = MAIL_WEB[mail_compose].format(**{k: quote(v, safe="") for k, v in draft.items()})
            await hub.invoke("open_url", {"url": url})
        else:
            await hub.invoke("compose_mail", {k: v for k, v in draft.items() if v})
        return {"draft": True, "to": address, "unknown_recipient": wanted if wanted and not address else None}

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
        description="Sucht und zeigt die Ergebnisse auf dem PC: Google (Standard) oder direkt auf einem Dienst "
                    "(site), z. B. spotify (in der App, falls installiert), youtube, amazon, netflix, maps.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["query"], "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 300},
            "site": {"enum": list(SEARCH_SITES)},
        }},
        handler=search_web,
    ))
    registry.register(Capability(
        name="pc.search_files", domain="pc", risk_class="R1", side_effects="reversible", timeout_s=20.0,
        output_trust="untrusted",  # Dateinamen können aus fremden Quellen stammen (Downloads)
        description="Öffnet die Explorer-Suche im Benutzerordner und nennt die neuesten Treffer.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["query"], "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 200},
        }},
        handler=search_files,
    ))
    registry.register(Capability(
        name="pc.find_files", domain="pc", risk_class="R1", timeout_s=20.0, output_trust="untrusted",
        description="Findet Dateien oder Ordner im Benutzerordner des PCs nach Namen (Windows-Suchindex) und "
                    "liefert die neuesten Treffer mit Nummer (id), Ordner und Datum – ohne etwas zu öffnen. "
                    "Öffnen danach mit pc.open_file und der id.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["query"], "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 200},
            "kind": {"enum": ["file", "folder", "any"]},
        }},
        handler=find_files,
    ))
    registry.register(Capability(
        name="pc.open_file", domain="pc", risk_class="R1", side_effects="reversible", timeout_s=20.0,
        description="Öffnet eine Datei oder einen Ordner aus dem Benutzerordner mit dem passenden Programm: per "
                    "Name (query, bester und neuester Treffer) oder per Nummer (id) aus der letzten Dateisuche. "
                    "show=true markiert sie nur im Explorer. Programme und Skripte werden nie gestartet, nur markiert.",
        input_schema={"type": "object", "additionalProperties": False, "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 200},
            "id": {"type": "integer", "minimum": 1, "maximum": 50},
            "kind": {"enum": ["file", "folder", "any"]},
            "show": {"type": "boolean"},
        }},
        handler=open_file,
    ))
    registry.register(Capability(
        # R2: nach dem Lesen fremder Inhalte nur mit Bestätigung – ungespeicherte Arbeit fragt das Programm selbst ab
        name="pc.close_app", domain="pc", risk_class="R2", side_effects="reversible", timeout_s=15.0,
        description="Schließt ein Programm auf dem PC sanft (wie das X oben rechts), z. B. Steam, Chrome, Word; "
                    "„explorer“ schließt die Explorer-Fenster. JARVIS selbst bleibt offen.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["app"], "properties": {
            "app": {"type": "string", "minLength": 2, "maxLength": 80, "pattern": "^[^<>|\"*?\\\\/\\u0000-\\u001f]+$"},
        }},
        handler=close_app,
    ))
    registry.register(Capability(
        name="pc.type_text", domain="pc", risk_class="R2", side_effects="reversible", timeout_s=15.0,
        description="Fügt Text in das aktive Fenster des PCs ein (z. B. Suchfeld, Dokument, Chat); enter=true "
                    "drückt danach Enter. Nie in Konsolen, nie Passwörter.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["text"], "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": 2000},
            "enter": {"type": "boolean"},
        }},
        handler=type_text,
    ))
    registry.register(Capability(
        name="pc.press_key", domain="pc", risk_class="R1", side_effects="reversible", timeout_s=15.0,
        description="Drückt eine Taste oder ein Tastenkürzel im aktiven Fenster (Enter, Tab, Pfeile, Kopieren, "
                    "Einfügen, Rückgängig, Speichern, Tabs, Zoom, Zurück) oder eine Medientaste (Wiedergabe/Pause, "
                    "nächster/vorheriger Titel, lauter, leiser, stumm).",
        input_schema={"type": "object", "additionalProperties": False, "required": ["key"], "properties": {
            "key": {"enum": KEYS},
            "times": {"type": "integer", "minimum": 1, "maximum": 10},
        }},
        risk_rules=[{"when": {"key": "close_window"}, "risk_class": "R2"}],
        handler=press_key,
    ))
    registry.register(Capability(
        name="pc.click", domain="pc", risk_class="R2", side_effects="reversible", timeout_s=20.0,
        description="Klickt im aktiven Fenster (auch auf Webseiten) auf eine Schaltfläche, einen Link oder "
                    "Menüpunkt mit dieser Beschriftung, z. B. „Anmelden“, „Weiter“, „Alle akzeptieren“.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["label"], "properties": {
            "label": {"type": "string", "minLength": 1, "maxLength": 120},
        }},
        handler=click,
    ))
    registry.register(Capability(
        name="pc.compose_mail", domain="pc", risk_class="R1", side_effects="reversible", timeout_s=15.0,
        description="Öffnet einen E-Mail-Entwurf (Empfänger als Adresse oder bekannter Kontakt, Betreff, Text) im "
                    "Mailprogramm bzw. Webmail. Abschicken muss der Nutzer selbst.",
        input_schema={"type": "object", "additionalProperties": False, "properties": {
            "to": {"type": "string", "maxLength": 200,
                   "description": "Kontaktname oder die Adresse genau so, wie der Nutzer sie diktiert hat (z. B. "
                                  "„max punkt mustermann at gmail punkt com“) – JARVIS setzt sie selbst zusammen."},
            "subject": {"type": "string", "maxLength": 300},
            "body": {"type": "string", "maxLength": 5000},
        }},
        handler=compose_mail,
    ))
    registry.register(Capability(
        # R2 wie pc.open_url: nach dem Lesen fremder Inhalte nur mit Bestätigung. Ohne Taint (der Nutzer fragt
        # selbst) öffnet JARVIS den ersten Treffer direkt – wie „Auf gut Glück“.
        name="pc.open_link", domain="pc", risk_class="R2", side_effects="reversible", timeout_s=20.0,
        description="Sucht im Internet den passenden Link und öffnet ihn direkt im Browser des PCs (erster "
                    "Treffer), z. B. die Webseite einer Firma, ein Rezept oder mit site=youtube das erste Video.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["query"], "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 300},
            "site": {"enum": list(LINK_SITES)},
        }},
        handler=open_link,
    ))
