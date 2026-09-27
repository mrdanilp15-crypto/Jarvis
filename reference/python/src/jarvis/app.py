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
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from .api import Container, create_app
from .capabilities import register_memory_capabilities
from .connectors.homeassistant import HomeAssistantClient, register_home_capabilities, state_changed_to_event
from .context import ContextBuilder, Situation
from .errors import CircuitBreaker, JarvisError
from .events import InMemoryEventBus, RedisStreamEventBus
from .fastpath import FastPath
from .info import InfoConfig, register_info_capabilities
from .llm.ollama import OllamaProvider
from .llm.router import ModelRouter
from .logging_setup import configure_logging
from .memory import InMemoryMemoryStore, MemoryService, OllamaEmbedder, RankingWeights
from .orchestrator import ConfirmationStore, Orchestrator
from .pc import AgentHub, register_pc_capabilities
from .persona import Persona
from .policy import PolicyEngine, Principal
from .tools import ToolRegistry

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

    bus_cfg = cfg["bus"]
    if bus_cfg["backend"] == "redis_streams":
        bus: Any = RedisStreamEventBus(bus_cfg["url"], prefix=bus_cfg["stream_prefix"],
                                       group=bus_cfg["consumer_group"], max_len=bus_cfg["max_len"])
        background.append(bus.run(["input", "sensor", "webhook", "schedule"]))
    else:
        bus = InMemoryEventBus()

    registry = ToolRegistry()
    ha_cfg = cfg["homeassistant"]
    ha_token = resolve_ref(ha_cfg["token"])
    if ha_token:
        async def forward(data: dict[str, Any]) -> None:
            await bus.publish(state_changed_to_event(data))

        ha = HomeAssistantClient(ha_cfg["websocket_url"], ha_token, on_state_changed=forward)
        register_home_capabilities(registry, ha)
        background.append(ha.run_forever())

    emb_cfg = cfg["llm"]["embeddings"]
    mem_cfg = cfg["memory"]
    memory = MemoryService(
        InMemoryMemoryStore(dedup_similarity=mem_cfg["dedup_similarity"]),  # Betrieb: PostgresMemoryStore
        OllamaEmbedder(emb_cfg["base_url"], emb_cfg["model"], keep_alive=emb_cfg.get("keep_alive", "24h")),
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

    agents = AgentHub()
    if (cfg.get("pc_agent") or {}).get("enabled", True):
        register_pc_capabilities(registry, agents, search_url=(cfg.get("pc_agent") or {}).get(
            "search_url", "https://www.google.com/search?q={query}"))

    providers = cfg["llm"]["providers"]
    local_cfg = providers[cfg["llm"]["default_local"]]
    # JARVIS_LLM_MODEL (deploy/.env) überschreibt das Modell – start.sh wählt es passend zur Hardware
    local = OllamaProvider(base_url=local_cfg["base_url"],
                           model=os.environ.get("JARVIS_LLM_MODEL") or local_cfg["model"],
                           num_ctx=local_cfg["num_ctx"], timeout_s=local_cfg["timeout_s"],
                           keep_alive=local_cfg.get("keep_alive", "24h"))
    cloud = None
    cloud_cfg = providers.get(cfg["llm"]["default_cloud"])
    api_key = resolve_ref(cloud_cfg.get("api_key")) if cloud_cfg else None
    if cloud_cfg and cloud_cfg["type"] == "anthropic" and api_key:
        from .llm.claude import ClaudeProvider

        cloud = ClaudeProvider(model=cloud_cfg["model"], max_tokens=cloud_cfg["max_tokens"],
                               default_effort=cloud_cfg["effort"]["dialog"], api_key=api_key,
                               server_side_fallbacks=cloud_cfg.get("server_side_fallbacks") == "default")
    elif cloud_cfg:
        log.info("Cloud-LLM deaktiviert (kein API-Schlüssel) – JARVIS arbeitet nur mit dem lokalen Modell")
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
        max_iterations=cfg["orchestrator"]["max_tool_iterations"],
        turn_timeout_s=cfg["orchestrator"]["turn_timeout_s"],
        session_idle_timeout_s=cfg["context"]["session_idle_timeout_s"],
    )

    # Entwicklungs-Tokens: JARVIS_DEV_TOKENS='{"<token>": {"actor": "user:alex", "role": "adult", "trust": "trusted_user"}}'
    tokens = {tok: Principal(**p) for tok, p in json.loads(os.environ.get("JARVIS_DEV_TOKENS", "{}")).items()}

    def situation(principal: Principal, channel: str) -> Situation:
        return Situation(now=datetime.now(tz), user_display=principal.actor.split(":", 1)[-1],
                         area=principal.area, channel=channel, location=home_location)

    container = Container(orchestrator=orchestrator, bus=bus, router=router, tokens=tokens,
                          webhook_secrets={}, situation=situation, agents=agents, tts=_tts(cfg))

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

    background.append(warm_up_local_model())
    return container, background


def _tts(cfg: dict[str, Any]) -> Any:
    """JARVIS-Stimme: Azure Speech (Conrad), wenn ein Schlüssel hinterlegt ist, sonst bzw. als Rückfall Piper."""
    tts_cfg = (cfg.get("voice") or {}).get("tts") or {}
    piper = None
    if tts_cfg.get("uri"):
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
    uvicorn.run(app, host=cfg["api"]["host"], port=cfg["api"]["port"])


if __name__ == "__main__":
    main()
