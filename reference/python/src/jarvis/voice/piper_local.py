"""Piper direkt im JARVIS-Prozess – die lokale Stimme ohne Docker (Windows-Programm).

Die Stimme (Standard ``de_DE-thorsten-high``, ~110 MB) wird beim ersten Start aus dem offiziellen Piper-Stimmen-
Archiv geladen und im Datenordner abgelegt. Schlägt das fehl, spricht die Oberfläche mit der Browserstimme weiter.
Synthese im Thread, eine zur Zeit (onnxruntime ist pro Stimme nicht für parallele Aufrufe gedacht).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import wave
from pathlib import Path
from typing import Any

from ..errors import JarvisError

log = logging.getLogger(__name__)

VOICES_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"
DEFAULT_VOICE = "de_DE-thorsten-high"


def voice_urls(name: str) -> tuple[str, str]:
    """„de_DE-thorsten-high“ -> Adressen von Modell und Konfiguration im Piper-Archiv."""
    lang_region, speaker, quality = name.split("-", 2)
    base = f"{VOICES_URL}/{lang_region.split('_')[0]}/{lang_region}/{speaker}/{quality}/{name}"
    return f"{base}.onnx", f"{base}.onnx.json"


class LocalPiperTTS:
    label = "piper"

    def __init__(self, directory: Path, voice: str | None = None, *, loader: Any = None) -> None:
        self.directory = Path(directory)
        self.voice_name = voice or os.environ.get("JARVIS_PIPER_VOICE") or DEFAULT_VOICE
        self._loader = loader  # Tests: eigene Lade-Funktion statt piper-tts
        self._voice: Any = None
        self._lock = asyncio.Lock()
        self.state = "idle"  # idle | loading | ready | error

    @property
    def model_path(self) -> Path:
        return self.directory / f"{self.voice_name}.onnx"

    def download(self) -> None:
        """Stimme laden, falls sie fehlt (Teildatei -> umbenennen, damit ein Abbruch keine kaputte Stimme hinterlässt)."""
        import httpx

        self.directory.mkdir(parents=True, exist_ok=True)
        for url, target in zip(voice_urls(self.voice_name), (self.model_path, Path(f"{self.model_path}.json")),
                               strict=True):
            if target.exists() and target.stat().st_size > 0:
                continue
            part = target.with_name(target.name + ".part")
            log.info("Lade Piper-Stimme %s …", url.rsplit("/", 1)[-1])
            with httpx.stream("GET", url, follow_redirects=True, timeout=60) as response:
                response.raise_for_status()
                with part.open("wb") as handle:
                    for chunk in response.iter_bytes(1 << 20):
                        handle.write(chunk)
            if target.suffix == ".json":
                json.loads(part.read_text(encoding="utf-8"))  # kaputte Konfiguration früh erkennen
            elif part.stat().st_size < 1_000_000:
                part.unlink()
                raise JarvisError("JRV-INT-001", f"Piper-Stimme {self.voice_name} unvollständig")
            part.replace(target)

    def load(self) -> None:
        if self._voice is not None:
            return
        self.state = "loading"
        try:
            if self._loader is not None:
                self._voice = self._loader(self.model_path)
            else:
                self.download()
                from piper import PiperVoice

                self._voice = PiperVoice.load(str(self.model_path), config_path=f"{self.model_path}.json")
            self.state = "ready"
            log.info("Piper-Stimme %s bereit", self.voice_name)
        except Exception as exc:
            self.state = "error"
            log.warning("Piper-Stimme nicht verfügbar (%s) – die Oberfläche nutzt die Browserstimme", exc)
            raise

    def _synthesize(self, text: str) -> bytes:
        self.load()
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            synthesize_wav = getattr(self._voice, "synthesize_wav", None)
            if synthesize_wav is not None:  # piper-tts ≥ 1.3
                synthesize_wav(text, wav)
            else:  # piper-tts 1.2
                self._voice.synthesize(text, wav)
        return buffer.getvalue()

    async def warm_up(self) -> None:
        try:
            await asyncio.to_thread(self.load)
        except Exception:
            pass  # Zustand „error“; /v1/tts meldet den Fehler, die Oberfläche spricht mit der Browserstimme

    async def synthesize_wav(self, text: str) -> bytes:
        async with self._lock:
            try:
                return await asyncio.to_thread(self._synthesize, text)
            except JarvisError:
                raise
            except Exception as exc:
                raise JarvisError("JRV-INT-001", f"Piper: {exc}",
                                  user_message="Die lokale JARVIS-Stimme ist gerade nicht verfügbar.") from exc
