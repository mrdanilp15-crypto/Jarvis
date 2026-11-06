"""Systemmonitor und sichere Systemaktionen.

- ``system.monitor``: Prozessor, Arbeitsspeicher, Laufwerke, Netz, Temperaturen, Grafikkarte, Akku. Bevorzugt vom
  PC-Agenten (der Windows-PC des Nutzers – auch wenn JARVIS in Docker läuft), sonst vom JARVIS-Rechner selbst (psutil).
- ``pc.system_action``: eine feste Freigabeliste (Bildschirm sperren, Papierkorb leeren, …). JARVIS führt nie frei
  formulierte Befehle aus – eine Prompt-Injection aus einer Webseite oder Mail könnte sonst den PC übernehmen.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import time
from typing import Any

from .errors import JarvisError
from .pc import AgentHub
from .tools import Capability, InvocationContext, ToolRegistry

log = logging.getLogger(__name__)

# Aktion -> (Risikoklasse, Beschreibung). R3 verlangt eine Bestätigung in der App, R2 nach Fremdinhalten.
SYSTEM_ACTIONS = {
    "lock_screen": ("R1", "Bildschirm sperren"),
    "sleep": ("R2", "Energiesparmodus"),
    "open_task_manager": ("R1", "Task-Manager öffnen"),
    "check_updates": ("R1", "nach Windows-Updates suchen"),
    "empty_recycle_bin": ("R2", "Papierkorb leeren"),
    "clean_temp": ("R2", "temporäre Dateien (älter als ein Tag) löschen"),
    "restart": ("R3", "Neustart in einer Minute"),
    "shutdown": ("R3", "Herunterfahren in einer Minute"),
    "cancel_shutdown": ("R1", "geplanten Neustart bzw. Herunterfahren abbrechen"),
}


def local_snapshot(sample_s: float = 0.4) -> dict[str, Any]:
    """Messwerte dieses Rechners über psutil (CPU wird über ``sample_s`` gemittelt, Netz als Durchsatz)."""
    try:
        import psutil
    except ImportError as exc:
        raise JarvisError("JRV-INT-001", "psutil fehlt",
                          user_message="Der Systemmonitor braucht das Paket psutil (pip install psutil).") from exc
    net_before = psutil.net_io_counters()
    started = time.monotonic()
    cpu = psutil.cpu_percent(interval=sample_s)
    elapsed = max(time.monotonic() - started, 0.001)
    net_after = psutil.net_io_counters()
    memory = psutil.virtual_memory()
    disks = []
    for part in psutil.disk_partitions(all=False):
        if part.fstype in ("", "squashfs", "tmpfs", "overlay") or "cdrom" in part.opts:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        if usage.total < 2e9 or "ro" in part.opts.split(","):
            continue  # kleine bzw. schreibgeschützte Einhängepunkte (Docker, Snap, Wiederherstellung) auslassen
        disks.append({"name": part.device if platform.system() == "Windows" else part.mountpoint,
                      "total_gb": round(usage.total / 1e9, 1), "free_gb": round(usage.free / 1e9, 1)})
    temperatures = []
    sensors = getattr(psutil, "sensors_temperatures", None)
    if sensors is not None:
        try:
            for chip, entries in (sensors() or {}).items():
                for entry in entries[:2]:
                    if entry.current:
                        temperatures.append({"label": entry.label or chip, "celsius": round(entry.current)})
        except OSError:
            pass
    battery = None
    sensors_battery = getattr(psutil, "sensors_battery", None)
    if sensors_battery is not None:
        try:
            if (state := sensors_battery()) is not None:
                battery = {"percent": round(state.percent), "plugged": bool(state.power_plugged)}
        except OSError:
            pass
    processes = []
    for proc in psutil.process_iter(["name", "memory_info"]):
        try:
            processes.append((proc.info["memory_info"].rss, proc.info["name"] or "?"))
        except (psutil.Error, AttributeError, TypeError):
            continue
    top = [{"name": name, "memory_mb": round(rss / 1e6)} for rss, name in sorted(processes, reverse=True)[:5]]
    return {
        "source": "server", "host": platform.node(),
        "cpu_percent": round(cpu), "cpu_cores": psutil.cpu_count() or 0, "cpu_name": platform.processor() or "",
        "memory_total_gb": round(memory.total / 1e9, 1), "memory_used_gb": round((memory.total - memory.available) / 1e9, 1),
        "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
        "net_down_mbit": round((net_after.bytes_recv - net_before.bytes_recv) * 8 / elapsed / 1e6, 1),
        "net_up_mbit": round((net_after.bytes_sent - net_before.bytes_sent) * 8 / elapsed / 1e6, 1),
        "disks": disks, "temperatures": temperatures[:6], "gpus": [], "battery": battery, "top_processes": top,
    }


def register_sysmon_capabilities(registry: ToolRegistry, hub: AgentHub | None) -> None:
    async def monitor(args: dict[str, Any], ctx: InvocationContext) -> Any:
        if hub is not None and hub.connected and hub.supports("system_info"):
            try:
                data = await hub.invoke("system_info", {})
                return {**(data or {}), "focus": args.get("focus", "all")}
            except JarvisError:
                log.warning("Systemwerte vom PC-Agenten nicht lesbar – messe auf dem JARVIS-Rechner", exc_info=True)
        data = await asyncio.to_thread(local_snapshot)
        return {**data, "focus": args.get("focus", "all")}

    registry.register(Capability(
        name="system.monitor", domain="system", risk_class="R0",
        description=("Liest Prozessor-Auslastung, Arbeitsspeicher, freien Speicherplatz, Netz-Durchsatz, Temperaturen, "
                     "Grafikkarte und Akku des PCs. focus: cpu | memory | disk | temperature | gpu | network | all."),
        input_schema={"type": "object", "additionalProperties": False, "properties": {
            "focus": {"enum": ["cpu", "memory", "disk", "temperature", "gpu", "network", "all"]},
        }},
        handler=monitor,
    ))

    if hub is None:
        return

    async def system_action(args: dict[str, Any], ctx: InvocationContext) -> Any:
        return await hub.invoke("system_action", {"name": args["name"]})

    registry.register(Capability(
        name="pc.system_action", domain="pc", risk_class="R1", side_effects="irreversible",
        risk_rules=[{"when": {"name": name}, "risk_class": risk} for name, (risk, _) in SYSTEM_ACTIONS.items()
                    if risk != "R1"],
        description="Feste Systemaktionen am PC: " + "; ".join(f"{k} = {v}" for k, (_, v) in SYSTEM_ACTIONS.items()),
        input_schema={"type": "object", "additionalProperties": False, "required": ["name"], "properties": {
            "name": {"enum": list(SYSTEM_ACTIONS)},
        }},
        handler=system_action,
    ))
