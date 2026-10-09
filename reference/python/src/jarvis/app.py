"""Dienst-Einstiegspunkt ``jarvis-core``: verdrahtet Komponenten aus config/jarvis.example.yaml.

    JARVIS_CONFIG=config/jarvis.example.yaml python -m jarvis.app

Secret-Referenzen: ``env:NAME`` liest eine Umgebungsvariable. ``vault:<pfad>#<feld>`` wird im Referenzcode
über die Umgebungsvariable ``JARVIS_SECRET_<PFAD>_<FELD>`` aufgelöst (Großbuchstaben, / -> _); im Betrieb
ersetzt ein Vault-Client (z. B. hvac) diese Funktion.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from collections.abc import AsyncIterator, Coroutine
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from .agenda import CalendarService, day_plan_entries, register_calendar_capabilities
from .api import Container, create_app
from .capabilities import register_memory_capabilities
from .connectors.homeassistant import state_changed_to_event
from .context import ContextBuilder, Situation
from .errors import CircuitBreaker, JarvisError
from .events import InMemoryEventBus, RedisStreamEventBus
from .fastpath import FastPath
from .info import InfoConfig, register_info_capabilities
from .llm.ollama import OllamaProvider
from .llm.router import ModelRouter
from .llm_settings import LLMSettings
from .logging_setup import configure_logging
from .mail import MailConfig, MailReader, register_mail_capabilities
from .memory import InMemoryMemoryStore, MemoryService, OllamaEmbedder, RankingWeights, SqliteMemoryStore
from .orchestrator import ConfirmationStore, Orchestrator
from .pc import AgentHub, register_pc_capabilities, resolve_recipient
from .smarthome import SmartHome
from .sysmon import register_sysmon_capabilities
from .voice.local import create_local_speech
from .skills import register_assistant_capabilities
from .timers import Alarm, AlarmScheduler, Notifier, register_timer_capabilities
from .persona import Persona
from .policy import PolicyEngine, Principal
from .tools import ToolRegistry
from .websearch import WebSearch, register_web_capabilities

log = logging.getLogger(__name__)

def resolve_ref(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if value.startswith("env:"):
        return os.environ.get(value[4:])
    if value.startswith("vault:"):
        name = "JARVIS_SECRET_" + re.sub(r"[^A-Z0-9]+", "_", value[6:].upper()).strip("_")
        return os.environ.get(name)
    return value


def build(config_path: Path) -> tuple[Container, list[Coroutine[Any, Any, None]]]:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    root = config_path.resolve().parent.parent  # Pfade in der Konfiguration sind relativ zum Repo
    tz = ZoneInfo(cfg["system"]["timezone"])
    background: list[Coroutine[Any, Any, None]] = []

    # Datenordner: Timer, Termine, Modellwahl, Smart-Home-Zugang (Docker: Volume „jarvis-data“)
    data_dir = Path(os.environ.get("JARVIS_DATA_DIR") or (root / "data"))
    # JARVIS als Programm ohne Docker: kein Redis, Ollama auf diesem PC (JARVIS_BUS=memory, JARVIS_OLLAMA_URL)
    ollama_url = os.environ.get("JARVIS_OLLAMA_URL") or None

    bus_cfg = cfg["bus"]
    if (os.environ.get("JARVIS_BUS") or bus_cfg["backend"]) == "redis_streams":
        bus: Any = RedisStreamEventBus(bus_cfg["url"], prefix=bus_cfg["stream_prefix"],
                                       group=bus_cfg["consumer_group"], max_len=bus_cfg["max_len"])
        background.append(bus.run(["input", "sensor", "webhook", "schedule"]))
    else:
        bus = InMemoryEventBus()

    registry = ToolRegistry()
    # Smart Home: Home Assistant wird gesucht und in der Oberfläche verbunden (Zahnrad → Smart Home). Ein Token in
    # deploy/.env (JARVIS_SECRET_KV_JARVIS_HOMEASSISTANT_TOKEN, Adresse JARVIS_HA_URL) gilt weiterhin.
    ha_cfg = cfg.get("homeassistant") or {}

    async def forward(data: dict[str, Any]) -> None:
        await bus.publish(state_changed_to_event(data))

    smarthome = SmartHome(path=data_dir / "home.json", registry=registry,
                          env_url=os.environ.get("JARVIS_HA_URL") or ha_cfg.get("websocket_url"),
                          env_token=resolve_ref(ha_cfg.get("token")), on_state_changed=forward)
    background.append(smarthome.run())

    emb_cfg = cfg["llm"]["embeddings"]
    mem_cfg = cfg["memory"]
    memory = MemoryService(
        # Langzeitgedächtnis in data/memory.db (übersteht Neustarts); memory.backend: memory = nur im Arbeitsspeicher
        SqliteMemoryStore(data_dir / "memory.db", dedup_similarity=mem_cfg["dedup_similarity"])
        if mem_cfg.get("backend", "sqlite") == "sqlite"
        else InMemoryMemoryStore(dedup_similarity=mem_cfg["dedup_similarity"]),
        OllamaEmbedder(ollama_url or emb_cfg["base_url"], emb_cfg["model"], keep_alive=emb_cfg.get("keep_alive", "24h")),
        RankingWeights(**mem_cfg["retrieval"]["weights"], half_life_days=mem_cfg["retrieval"]["half_life_days"]),
    )
    register_memory_capabilities(registry, memory)

    info_cfg = cfg.get("info") or {}
    home_location = os.environ.get("JARVIS_HOME_LOCATION") or info_cfg.get("home_location") or None
    if info_cfg.get("enabled", True):
        defaults = InfoConfig()
        register_info_capabilities(registry, InfoConfig(
            home_location=home_location,
            news_feeds=info_cfg.get("news_feeds") or defaults.news_feeds,
            wikipedia_language=info_cfg.get("wikipedia_language", defaults.wikipedia_language),
        ))

    web = None
    web_cfg = info_cfg.get("web_search") or {}
    if info_cfg.get("enabled", True) and web_cfg.get("enabled", True):
        web = WebSearch(searxng_url=os.environ.get("JARVIS_SEARXNG_URL") or web_cfg.get("searxng_url") or None,
                        region=web_cfg.get("region", "de-de"))
        register_web_capabilities(registry, web)

    assistant_cfg = cfg.get("assistant") or {}
    contacts = {**(assistant_cfg.get("contacts") or {}), **_named(os.environ.get("JARVIS_CONTACTS"), "@")}
    agents = AgentHub()
    pc_cfg = cfg.get("pc_agent") or {}
    pc_enabled = pc_cfg.get("enabled", True)
    if pc_enabled:
        register_pc_capabilities(
            registry, agents, web=web, contacts=contacts,
            search_url=pc_cfg.get("search_url", "https://www.google.com/search?q={query}"),
            mail_compose=os.environ.get("JARVIS_MAIL_COMPOSE") or pc_cfg.get("mail_compose", "mailto"))

    # Systemmonitor (PC-Agent oder dieser Rechner) und feste Systemaktionen (Freigabeliste, keine freien Befehle)
    register_sysmon_capabilities(registry, agents if pc_enabled else None)

    providers = cfg["llm"]["providers"]
    local_cfg = providers[cfg["llm"]["default_local"]]
    # JARVIS_LLM_MODEL (deploy/.env) überschreibt das Modell – start.sh wählt es passend zur Hardware
    local = OllamaProvider(base_url=ollama_url or local_cfg["base_url"],
                           model=os.environ.get("JARVIS_LLM_MODEL") or local_cfg["model"],
                           num_ctx=local_cfg["num_ctx"], timeout_s=local_cfg["timeout_s"],
                           keep_alive=local_cfg.get("keep_alive", "24h"))
    cloud = None
    cloud_cfg = providers.get(cfg["llm"]["default_cloud"]) or {}
    api_key = resolve_ref(cloud_cfg.get("api_key")) if cloud_cfg.get("type") == "anthropic" else None

    def make_cloud(key: str, model: str | None = None) -> Any:
        """Claude-Provider – beim Start aus deploy/.env, später auch mit einem Schlüssel aus der Oberfläche."""
        from .llm.claude import DEFAULT_MODEL, ClaudeProvider

        return ClaudeProvider(model=model or cloud_cfg.get("model") or DEFAULT_MODEL,
                              max_tokens=cloud_cfg.get("max_tokens", 64000),
                              default_effort=(cloud_cfg.get("effort") or {}).get("dialog", "medium"), api_key=key,
                              server_side_fallbacks=cloud_cfg.get("server_side_fallbacks", "default") == "default")

    if api_key:
        cloud = make_cloud(api_key)
    else:
        log.info("Cloud-LLM ohne API-Schlüssel – JARVIS arbeitet lokal (Schlüssel: Zahnrad → KI-Modell)")
    breaker = CircuitBreaker(**{k: v for k, v in cfg["router"]["cloud_circuit_breaker"].items()
                                if k in ("failure_threshold", "window_s", "cooldown_s")})
    router = ModelRouter(local=local, cloud=cloud, cloud_breaker=breaker,
                         fast_path_min_confidence=cfg["router"]["fast_path_min_confidence"])

    persona_cfg = cfg["persona"]
    persona = Persona.load(root / persona_cfg["files"][persona_cfg["active"]],
                           schema_path=root / "schemas" / "persona.schema.json")
    orchestrator = Orchestrator(
        registry=registry,
        policy=PolicyEngine.from_file(root / cfg["security"]["policies_file"]),
        context=ContextBuilder(persona=persona),
        audit=_audit_sink(),
        confirmations=ConfirmationStore(timeout_s=cfg["security"].get("confirmation_timeout_s", 60)),
        memory=memory,
        bus=bus,
        # Sofortbefehle ohne LLM („Öffne den Explorer“, Timer); Licht-Grammatiken brauchen Räume aus Home Assistant
        fast_path=FastPath({}, {}),
        app_resolver=agents.find_app if pc_enabled else None,
        recipient_resolver=lambda text: resolve_recipient(text, contacts),
        max_iterations=cfg["orchestrator"]["max_tool_iterations"],
        turn_timeout_s=cfg["orchestrator"]["turn_timeout_s"],
        session_idle_timeout_s=cfg["context"]["session_idle_timeout_s"],
    )

    # Entwicklungs-Tokens: JARVIS_DEV_TOKENS='{"<token>": {"actor": "user:owner", "role": "adult",
    # "trust": "trusted_user", "name": "Daniel"}}'. JARVIS_USER_NAME gilt für Tokens ohne eigenen Namen.
    default_name = (os.environ.get("JARVIS_USER_NAME") or "").strip() or None
    tokens = {tok: Principal(**{**p, "name": p.get("name") or default_name})
              for tok, p in json.loads(os.environ.get("JARVIS_DEV_TOKENS", "{}")).items()}

    def situation(principal: Principal, channel: str) -> Situation:
        # Nie die Actor-ID als Namen zeigen (daher kam „Alex“): ohne Namen spricht JARVIS nur mit „Sir“ an
        return Situation(now=datetime.now(tz), user_display=principal.name, area=principal.area, channel=channel,
                         location=home_location, home_state=smarthome.situation_lines())

    smarthome.fast_path = orchestrator.fast_path

    def stt_prompt() -> str:
        """Wörterbuch für Whisper: Aktivierungswort, Name, Räume und Geräte – so schreibt es sie richtig."""
        words = ["Jarvis", default_name or ""]
        if smarthome.index is not None:
            words += list(smarthome.index.areas.values()) + [d.name for d in smarthome.index.devices[:40]]
        return ", ".join(w for w in words if w)

    tts = _tts(cfg, data_dir)
    for engine in (tts, getattr(tts, "fallback", None)):
        if hasattr(engine, "warm_up"):
            background.append(engine.warm_up())  # lokale Piper-Stimme laden (beim ersten Start: Download)
    speech = create_local_speech(stt_prompt)
    if speech is not None:
        background.append(speech.warm_up())  # Modell laden (beim ersten Start: Download)
    container = Container(orchestrator=orchestrator, bus=bus, router=router, tokens=tokens,
                          webhook_secrets={}, situation=situation, agents=agents, tts=tts, smarthome=smarthome,
                          speech=speech)

    pc_enabled = registry.get("pc.open_app") is not None

    def probes() -> dict[str, str]:  # Systemstatus: nur, was tatsächlich geprüft wird
        llm = {"ready": "ok", "unknown": "ok", "loading": "loading", "missing_model": "missing",
               "unavailable": "unavailable"}.get(container.llm_status, "ok")
        components = {"sprachmodell": llm}
        if pc_enabled:
            components["pc_steuerung"] = "ok" if agents.connected else "disconnected"
        components["stimme"] = getattr(container.tts, "label", "ok") if container.tts is not None else "browser"
        if smarthome.credentials() is not None:
            components["smart_home"] = "ok" if smarthome.connected else "disconnected"
        return components

    # Timer, Erinnerungen, Kalender, E-Mail – Daten im Datenordner
    notifier = container.notifier = Notifier()

    async def announce(alarm: Alarm) -> None:
        style = orchestrator.style
        text = style.finalize(style.alarm_text(alarm.kind, alarm.label, duration_s=alarm.duration_s,
                                               due=alarm.due.astimezone(tz), late=alarm.late))
        await notifier.send(alarm.actor, {"type": "notification", "kind": alarm.kind, "id": alarm.id, "text": text})
        await agents.notify("JARVIS", text)  # Windows-Hinweis, auch ohne offenes JARVIS-Fenster

    scheduler = AlarmScheduler(notify=announce, path=data_dir / "alarms.json")
    register_timer_capabilities(registry, scheduler, tz)
    background.append(scheduler.run())
    cal_cfg = assistant_cfg.get("calendar") or {}
    calendar = CalendarService(tz=tz, path=data_dir / "calendar.json", scheduler=scheduler,
                               ics={**(cal_cfg.get("ics") or {}), **_named(os.environ.get("JARVIS_CALENDAR_ICS"), "://")},
                               remind_minutes=int(cal_cfg.get("remind_minutes", 15)))
    register_calendar_capabilities(registry, calendar)
    mail_host = os.environ.get("JARVIS_MAIL_IMAP_HOST") or (assistant_cfg.get("mail") or {}).get("imap_host")
    if mail_host and os.environ.get("JARVIS_MAIL_USER") and os.environ.get("JARVIS_MAIL_PASSWORD"):
        register_mail_capabilities(registry, MailReader(MailConfig(
            mail_host, os.environ["JARVIS_MAIL_USER"], os.environ["JARVIS_MAIL_PASSWORD"],
            port=int(os.environ.get("JARVIS_MAIL_IMAP_PORT") or 993))))

    async def calendar_entries(day: date, ctx: Any) -> list[str]:
        return await day_plan_entries(calendar, day)

    weather = registry.get("info.weather")
    register_assistant_capabilities(registry, probes=probes, weather=weather.handler if weather else None,
                                    calendar=calendar_entries)

    async def warm_up_local_model() -> None:
        """Lädt das lokale Modell beim Start und rechnet Regeln und Tools vorab durch, damit schon die erste
        Frage schnell beantwortet wird. Wiederholt, bis Ollama läuft und das Modell geladen ist."""
        static = orchestrator.context.system_prompt(Situation(now=datetime.now(tz)), []).static
        delay = 5.0
        while True:
            container.llm_status = "loading"
            try:
                await local.warm_up(static, registry.tool_specs())
            except JarvisError as exc:
                # user_message ist nur gesetzt, wenn das Modell fehlt (HTTP 404, „ollama pull …“)
                container.llm_status = "missing_model" if exc.user_message else "unavailable"
                log.info("lokales Modell noch nicht bereit (%s) – neuer Versuch in %.0f s", exc.detail, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 15.0)
                continue
            container.llm_status = "ready"
            log.info("lokales Modell %s geladen und vorgewärmt", local.model)
            return

    # KI-Modell zur Laufzeit wählen (Zahnrad → KI-Modell): gespeicherte Auswahl gilt schon ab dem ersten Start
    container.llm_settings = LLMSettings(
        path=data_dir / "llm.json", router=router, local=local, env_key=api_key, cloud_options=cloud_cfg,
        make_cloud=make_cloud, warm_up=warm_up_local_model, status=lambda: container.llm_status)

    background.append(warm_up_local_model())
    return container, background


def _named(value: str | None, marker: str) -> dict[str, str]:
    """„Mama=mama@example.de; Max=max@example.de“ bzw. „Arbeit=https://…, https://…“ -> {Name: Wert}.
    Einträge ohne Namen werden durchnummeriert; ``marker`` erkennt den Wert (Adressen enthalten selbst „=“)."""
    out: dict[str, str] = {}
    for number, item in enumerate(re.split(r"[;,\n]\s*(?=[^;,\n]*" + re.escape(marker) + ")", value or ""), 1):
        item = item.strip().strip(",;")
        if not item:
            continue
        name, sep, rest = item.partition("=")
        if sep and marker not in name and marker in rest:
            out[name.strip()] = rest.strip()
        elif marker in item:
            out[f"Kalender {number}"] = item
    return out


def _tts(cfg: dict[str, Any], data_dir: Path | None = None) -> Any:
    """JARVIS-Stimme: Azure Speech (Conrad), wenn ein Schlüssel hinterlegt ist, sonst bzw. als Rückfall Piper.
    JARVIS_PIPER=local: Piper im eigenen Prozess (Programm ohne Docker), Stimme im Datenordner."""
    tts_cfg = (cfg.get("voice") or {}).get("tts") or {}
    piper = None
    mode = os.environ.get("JARVIS_PIPER", "").lower()
    if mode == "local":
        try:
            import piper as _piper  # noqa: F401  – optionales Extra „voice-local“
        except ImportError:
            log.info("Lokale Piper-Stimme aus: Paket 'piper-tts' fehlt")
        else:
            from .voice.piper_local import LocalPiperTTS

            piper = LocalPiperTTS((data_dir or Path("data")) / "voices")
    elif tts_cfg.get("uri") and mode not in ("off", "0", "false"):
        try:
            import wyoming  # noqa: F401  – optionales Extra „voice“
        except ImportError:
            log.info("Piper deaktiviert: Paket 'wyoming' fehlt")
        else:
            from urllib.parse import urlsplit

            from .voice.pipeline import WyomingTTS

            parts = urlsplit(tts_cfg["uri"])
            piper = WyomingTTS(parts.hostname or "wyoming-piper", parts.port or 10200)

    cloud_cfg = tts_cfg.get("cloud") or {}
    key, region = resolve_ref(cloud_cfg.get("key")), resolve_ref(cloud_cfg.get("region"))
    if cloud_cfg.get("provider") != "azure" or not key or not region:
        return piper
    from .voice.cloud import AzureTTS, FallbackTTS

    try:
        azure = AzureTTS(key, region.strip().lower(), voice=os.environ.get("JARVIS_TTS_VOICE") or cloud_cfg.get(
            "voice", "de-DE-ConradNeural"), rate=cloud_cfg.get("rate", "-4%"), pitch=cloud_cfg.get("pitch", "-3%"))
    except ValueError as exc:
        log.warning("Azure-Stimme nicht aktiviert: %s", exc)
        return piper
    log.info("JARVIS-Stimme: Azure %s%s", azure.voice, " (Rückfall: Piper)" if piper else "")
    return FallbackTTS(azure, piper) if piper else azure


def _audit_sink() -> Any:
    from .orchestrator import MemoryAuditSink

    return MemoryAuditSink()  # Betrieb: Postgres-Sink (INSERT INTO jarvis.audit_log …)


def main() -> None:
    import uvicorn

    configure_logging("jarvis-core", os.environ.get("JARVIS_LOG_LEVEL", "INFO"))
    config_path = Path(os.environ.get("JARVIS_CONFIG", "config/jarvis.example.yaml"))
    container, background = build(config_path)
    app = create_app(container)

    @contextlib.asynccontextmanager
    async def lifespan(_: Any) -> AsyncIterator[None]:
        tasks = [asyncio.create_task(job) for job in background]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()

    app.router.lifespan_context = lifespan
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    uvicorn.run(app, host=os.environ.get("JARVIS_HOST") or cfg["api"]["host"],
                port=int(os.environ.get("JARVIS_PORT") or cfg["api"]["port"]))


if __name__ == "__main__":
    main()
