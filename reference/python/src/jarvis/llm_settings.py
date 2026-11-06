"""KI-Modell zur Laufzeit wählen: lokales Modell (Ollama), Claude-Schlüssel und -Modell, wann Claude antwortet.

Gespeichert in ``data/llm.json`` (Docker-Volume ``jarvis-data``, nur für den JARVIS-Dienst lesbar). Der Schlüssel
verlässt den Server nach dem Speichern nie wieder – die Oberfläche sieht nur die letzten vier Zeichen. Ein Schlüssel
aus der Oberfläche hat Vorrang vor ``ANTHROPIC_API_KEY`` in ``deploy/.env``; wird er entfernt, gilt wieder die Datei.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .errors import JarvisError
from .llm.claude import DEFAULT_MODEL, MODELS as CLAUDE_MODELS

log = logging.getLogger("jarvis.llm_settings")

# Vorschläge für das lokale Modell: (Ollama-Name, Download-Größe, Einordnung)
LOCAL_MODELS = [
    ("qwen2.5:3b-instruct", "1,9 GB", "schnell, versteht aber weniger – für schwache Rechner"),
    ("qwen2.5:7b-instruct", "4,7 GB", "empfohlen ab 16 GB Arbeitsspeicher"),
    ("qwen2.5:14b-instruct", "9 GB", "am klügsten – Grafikkarte ab 12 GB empfohlen"),
    ("llama3.1:8b", "4,9 GB", "Alternative von Meta"),
]
MODES = ("auto", "cloud", "local")
_NAME = r"^[a-z0-9][a-z0-9._/-]{0,80}(?::[a-z0-9._-]{1,40})?$"  # Ollama-Modellname („qwen2.5:7b-instruct“)


@dataclass
class Pull:
    model: str
    status: str = "startet"
    completed: int = 0
    total: int = 0
    done: bool = False
    error: str | None = None
    activate: bool = True


class LLMSettings:
    def __init__(self, *, path: Path, router: Any, local: Any, env_key: str | None, cloud_options: dict[str, Any],
                 make_cloud: Callable[..., Any], warm_up: Callable[[], Awaitable[None]] | None = None,
                 status: Callable[[], str] = lambda: "unknown") -> None:
        self.path = path
        self.router = router
        self.local = local
        self.env_key = env_key or None
        self.cloud_options = cloud_options  # max_tokens, default_effort, server_side_fallbacks
        self.make_cloud = make_cloud  # (api_key, model) -> Provider mit check()
        self.warm_up = warm_up  # lokales Modell laden und vorwärmen (setzt den Status)
        self.status = status
        self.pull: Pull | None = None
        self._pull_task: asyncio.Task[None] | None = None
        self._warm_task: asyncio.Task[None] | None = None
        self.saved: dict[str, Any] = self._load()
        self._apply_saved()

    # -- Datei ---------------------------------------------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.warning("llm.json nicht lesbar – Standardwerte", exc_info=True)
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # enthält ggf. den API-Schlüssel
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(self.saved, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def _apply_saved(self) -> None:
        if model := self.saved.get("local_model"):
            self.local.model = model
        if self.saved.get("mode") in MODES:
            self.router.mode = self.saved["mode"]
        key = self.saved.get("claude_key") or self.env_key
        if key:
            self.router.cloud = self.make_cloud(key, self.claude_model)

    # -- Anzeige --------------------------------------------------------------------------------------------
    @property
    def claude_model(self) -> str:
        model = self.saved.get("claude_model") or getattr(self.router.cloud, "model", None) or DEFAULT_MODEL
        return model if any(m[0] == model for m in CLAUDE_MODELS) else DEFAULT_MODEL

    async def snapshot(self) -> dict[str, Any]:
        try:
            installed = await self.local.list_models()
            reachable = True
        except JarvisError:
            installed, reachable = [], False
        names = {m["name"] for m in installed}
        key = self.saved.get("claude_key") or self.env_key
        return {
            "mode": self.router.mode,
            "local": {
                "model": self.local.model,
                "status": self.status(),
                "reachable": reachable,
                "installed": [{"name": m["name"], "size_gb": round(m["size"] / 1e9, 1)} for m in installed],
                "suggested": [{"name": n, "size": size, "note": note, "installed": _installed(n, names)}
                              for n, size, note in LOCAL_MODELS],
                "pull": asdict(self.pull) if self.pull else None,
            },
            "cloud": {
                "configured": self.router.cloud is not None,
                "source": "ui" if self.saved.get("claude_key") else ("env" if self.env_key else None),
                "key_hint": f"…{key[-4:]}" if key else None,
                "model": self.claude_model,
                "models": [{"id": i, "name": n, "note": note, "price_in": p_in, "price_out": p_out}
                           for i, n, note, p_in, p_out in CLAUDE_MODELS],
                "state": self.router.cloud_breaker.state,
            },
        }

    # -- Lokales Modell -------------------------------------------------------------------------------------
    async def use_local(self, model: str) -> None:
        installed = {m["name"] for m in await self.local.list_models()}
        if not _installed(model, installed):
            raise JarvisError("JRV-VAL-001", f"Modell {model} nicht installiert",
                              user_message=f"{model} ist noch nicht heruntergeladen.")
        previous = self.local.model
        self.local.model = model
        self.saved["local_model"] = model
        self._save()
        if previous != model:
            await self.local.unload(previous)  # Speicher freigeben, sonst liegen beide im RAM
        self._restart_warm_up()

    def start_pull(self, model: str, *, activate: bool = True) -> None:
        model = model.strip().lower()
        if not re.match(_NAME, model):
            raise JarvisError("JRV-VAL-001", "Ungültiger Modellname",
                              user_message="Das sieht nicht nach einem Ollama-Modellnamen aus (z. B. qwen2.5:7b-instruct).")
        if self._pull_task is not None and not self._pull_task.done():
            raise JarvisError("JRV-VAL-001", "Download läuft bereits",
                              user_message=f"Es wird gerade {self.pull.model if self.pull else 'ein Modell'} geladen.")
        self.pull = Pull(model, activate=activate)
        self._pull_task = asyncio.create_task(self._run_pull(self.pull))

    async def _run_pull(self, pull: Pull) -> None:
        def progress(chunk: dict[str, Any]) -> None:
            pull.status = str(chunk.get("status") or pull.status)
            if chunk.get("total"):
                pull.total, pull.completed = int(chunk["total"]), int(chunk.get("completed") or 0)

        try:
            await self.local.pull(pull.model, progress)
            pull.status, pull.done = "fertig", True
            if pull.activate:
                await self.use_local(pull.model)
        except JarvisError as exc:
            pull.error, pull.done = exc.user_message or exc.detail, True
        except Exception as exc:  # Download-Fehler nie den Server stören lassen
            log.exception("Download fehlgeschlagen")
            pull.error, pull.done = type(exc).__name__, True

    def _restart_warm_up(self) -> None:
        if self.warm_up is None:
            return
        if self._warm_task is not None and not self._warm_task.done():
            self._warm_task.cancel()
        self._warm_task = asyncio.create_task(self.warm_up())

    # -- Claude ---------------------------------------------------------------------------------------------
    async def set_claude_key(self, key: str) -> str:
        key = key.strip()
        if not key.startswith("sk-ant-") or len(key) < 20 or any(c.isspace() for c in key):
            raise JarvisError("JRV-VAL-001", "Kein Anthropic-Schlüssel",
                              user_message="Ein Anthropic-API-Schlüssel beginnt mit „sk-ant-“.")
        provider = self.make_cloud(key, self.claude_model)
        name = await provider.check()  # wirft mit verständlicher Meldung, wenn der Schlüssel nicht passt
        self.router.cloud = provider
        self.router.cloud_breaker.record_success()  # neuer Schlüssel: alte Fehlversuche zählen nicht
        self.saved["claude_key"] = key
        self._save()
        return name

    def remove_claude_key(self) -> None:
        self.saved.pop("claude_key", None)
        self._save()
        self.router.cloud = self.make_cloud(self.env_key, self.claude_model) if self.env_key else None

    async def set_claude_model(self, model: str) -> str:
        if not any(m[0] == model for m in CLAUDE_MODELS):
            raise JarvisError("JRV-VAL-001", f"Unbekanntes Modell {model}")
        name = next(m[1] for m in CLAUDE_MODELS if m[0] == model)
        key = self.saved.get("claude_key") or self.env_key
        if key:
            provider = self.make_cloud(key, model)
            name = await provider.check()  # z. B. Fable 5.1 verlangt passende Datenaufbewahrung
            self.router.cloud = provider
        self.saved["claude_model"] = model
        self._save()
        return name

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise JarvisError("JRV-VAL-001", f"Unbekannter Modus {mode}")
        self.router.mode = mode
        self.saved["mode"] = mode
        self._save()


def _installed(model: str, names: set[str]) -> bool:
    """„llama3.1:8b“ ist installiert, wenn Ollama es so oder als „llama3.1:8b“ ohne/mit „:latest“ führt."""
    return model in names or f"{model}:latest" in names or (model.endswith(":latest") and model[:-7] in names)
