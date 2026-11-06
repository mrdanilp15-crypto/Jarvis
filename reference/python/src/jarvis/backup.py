"""Sicherung: Gedächtnis, Protokolle, Beobachter, Regeln, Termine in eine ZIP-Datei – ohne Zugangsdaten und Schlüssel."""

from __future__ import annotations

import os
import zipfile
from datetime import datetime
from pathlib import Path

INCLUDE = ("memory.db", "patterns.db", "protocols.json", "watchers.json", "rules.json", "calendar.json", "alarms.json",
           "feedback.jsonl", "profile.json")  # bewusst nicht: home.json, devices.json, tls/, llm.json (Tokens, Schlüssel)
KEEP = 10


def backup_folder() -> Path:
    return Path(os.environ.get("JARVIS_BACKUP_DIR") or Path.home() / "Documents" / "Jarvis-Sicherung")


def make_backup(data_dir: Path, target: Path | None = None, now: datetime | None = None) -> tuple[Path, int]:
    target = target or backup_folder()
    target.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y-%m-%d_%H-%M")
    path = target / f"jarvis-{stamp}.zip"
    count = 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in INCLUDE:
            file = data_dir / name
            if file.is_file():
                archive.write(file, name)
                count += 1
    for old in sorted(target.glob("jarvis-*.zip"))[:-KEEP]:  # nur die letzten zehn behalten
        old.unlink(missing_ok=True)
    return path, count


def restore(archive: Path, data_dir: Path) -> list[str]:
    """Stellt gesicherte Dateien wieder her (nur die bekannten Namen – nie fremde Pfade aus dem Archiv)."""
    restored = []
    with zipfile.ZipFile(archive) as z:
        for name in z.namelist():
            if name in INCLUDE:
                (data_dir / name).write_bytes(z.read(name))
                restored.append(name)
    return restored
