"""Selbstdiagnose („Jarvis, prüf dich“): misst, was „es ist langsam“ wirklich bedeutet, und sagt, was zu tun ist."""

from __future__ import annotations

import asyncio
import shutil
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .llm.base import SystemPrompt, UserTurn


def check(id_: str, label: str, state: str, detail: str = "", hint: str = "") -> dict[str, str]:
    return {"id": id_, "label": label, "state": state, "detail": detail, "hint": hint}  # state: ok | warn | fail | off


async def gpu_usage(provider: Any) -> tuple[float | None, str]:
    """Anteil des geladenen Modells im Grafikspeicher (Ollama /api/ps) und sein Name."""
    try:
        reply = await provider._client.get("/api/ps")
        models = reply.json().get("models") or []
    except Exception:
        return None, ""
    for m in models:
        if m.get("size"):
            return m.get("size_vram", 0) / m["size"], str(m.get("name", ""))
    return None, ""


async def run_selftest(*, provider: Any, health: Callable[[], Awaitable[dict[str, Any]]],
                       weather: Callable[[], Awaitable[Any]] | None = None, data_dir: Any = None,
                       clock: Callable[[], float] = time.monotonic) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    # 1) Sprachmodell: kurze Antwort messen (Modell vorher geladen? erster Aufruf kann länger dauern)
    started = clock()
    try:
        await asyncio.wait_for(provider.complete(system=SystemPrompt("Antworte nur mit OK."),
                                                 transcript=[UserTurn("Bereit?")], tools=[], effort=None), timeout=60)
        seconds = clock() - started
        state = "ok" if seconds < 3 else "warn" if seconds < 8 else "fail"
        hint = "" if state == "ok" else ("Ein kleineres Modell wählen (Zahnrad → KI-Modell) oder prüfen, ob die Grafikkarte genutzt wird.")
        results.append(check("llm", "Sprachmodell", state, f"{seconds:.1f} s für eine Kurzantwort", hint))
    except Exception as exc:
        message = getattr(exc, "user_message", None) or str(exc)
        results.append(check("llm", "Sprachmodell", "fail", message[:90], "Ollama starten: Startmenü → Ollama."))
    # 2) Grafikkarte
    share, name = await gpu_usage(provider)
    if share is None:
        results.append(check("gpu", "Grafikkarte", "off", "kein Modell geladen oder Ollama nicht erreichbar"))
    elif share >= 0.9:
        results.append(check("gpu", "Grafikkarte", "ok", f"{name} läuft zu {round(share * 100)} % auf der GPU"))
    else:
        results.append(check("gpu", "Grafikkarte", "warn", f"{name} nur zu {round(share * 100)} % auf der GPU",
                             "Das Modell ist zu groß für den Grafikspeicher – ein kleineres wählen (7B/8B)."))
    state_info = await health()
    stt = state_info.get("stt", "browser")
    results.append(check("stt", "Spracherkennung", "ok" if stt in ("browser", "local:ready") else "warn",
                         "Browser (Chrome/Edge)" if stt == "browser" else str(stt),
                         "" if stt in ("browser", "local:ready") else "Das lokale Modell lädt noch oder ist ausgefallen."))
    tts = state_info.get("tts", "off")
    results.append(check("tts", "Stimme", "ok", "Browserstimme" if tts == "off" else str(tts)))
    results.append(check("pc", "PC-Steuerung", "ok" if state_info.get("pc_agent") == "connected" else "warn",
                         "" if state_info.get("pc_agent") == "connected" else "nicht verbunden",
                         "" if state_info.get("pc_agent") == "connected" else "JARVIS über die Desktop-Verknüpfung starten."))
    home = state_info.get("smart_home", "off")
    results.append(check("home", "Smart Home", "ok" if home == "connected" else "off" if home in ("off", None) else "warn",
                         {"connected": "verbunden", "disconnected": "nicht erreichbar"}.get(home, "nicht eingerichtet")))
    if weather is not None:
        try:
            await asyncio.wait_for(weather(), timeout=8)
            results.append(check("net", "Internet (Wetter)", "ok"))
        except Exception:
            results.append(check("net", "Internet (Wetter)", "warn", "keine Antwort", "Internetverbindung prüfen."))
    if data_dir is not None:
        try:
            free = shutil.disk_usage(data_dir).free / 1e9
            results.append(check("disk", "Speicherplatz", "ok" if free > 5 else "warn", f"{free:.0f} GB frei"))
        except OSError:
            pass
    return results


def summary_text(results: list[dict[str, str]], address: str = "") -> str:
    sir = f", {address}" if address else ""
    bad = [r for r in results if r["state"] in ("warn", "fail")]
    if not bad:
        return f"Alles in Ordnung{sir}. Alle {len(results)} Prüfungen bestanden."
    first = bad[0]
    text = f"{len(bad)} von {len(results)} Prüfungen mit Hinweis{sir}. Zuerst: {first['label']} – {first['detail']}."
    return text + (f" {first['hint']}" if first["hint"] else "")
