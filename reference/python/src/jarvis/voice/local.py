"""Lokale Spracherkennung: Aktivierungswort und Whisper auf dem JARVIS-Rechner – kein Audio verlässt das Haus.

Die Oberfläche schickt Mikrofon-Audio (16 kHz, mono, 16 Bit) über ``WS /v1/audio``. Pro Verbindung schneidet
``AudioSession`` mit einer Energie-Sprachende-Erkennung Äußerungen heraus und lässt sie von faster-whisper
transkribieren:

- Modus ``wake``: Jede Äußerung wird geprüft. Beginnt sie mit „Jarvis“ (unscharf, auch „Hey Jarvis“, „Jarwis“), ist
  der Rest der Befehl; nur „Jarvis“ heißt „Ja, Sir?“ und danach zuhören. Mit openWakeWord (optional) löst schon
  „Hey Jarvis“ allein aus, bevor Whisper rechnet.
- Modus ``listen``: Die nächste Äußerung ist der Befehl (nach „Ja, Sir?“, Rückfragen, Mikrofon-Knopf).
- Modus ``idle``: Audio wird verworfen (JARVIS spricht oder denkt – er soll sich nicht selbst hören).

Ereignisse an die Oberfläche: ready, speech, wake, transcript, heard (ohne „Jarvis“), nothing, error.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
from array import array
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

RATE = 16000
FRAME = 480  # 30 ms
WAKE_WORD = re.compile(
    r"^\W*(?:(?:hey|hallo|ok|okay|he)\W+)?(?:jarvis|jarwis|javis|jervis|jarves|jarviz|jarvice|charvis|garvis|"
    r"dschawis|dschavis|tschawis|jarvas|travis)\b[\s,.:;!?-]*", re.I)


class Transcriber(Protocol):
    def __call__(self, pcm: bytes, prompt: str | None) -> str: ...


class WhisperTranscriber:
    """faster-whisper, beim ersten Aufruf geladen. Modell/Rechenwerk über JARVIS_STT_MODEL / JARVIS_STT_DEVICE."""

    def __init__(self, model: str | None = None, device: str | None = None, language: str = "de") -> None:
        self.model_name = model or os.environ.get("JARVIS_STT_MODEL") or "small"
        self.device = device or os.environ.get("JARVIS_STT_DEVICE") or "cpu"
        self.language = language
        self._model: Any = None
        self.state = "idle"  # idle | loading | ready | error
        self.error: str | None = None

    def load(self) -> None:
        if self._model is not None:
            return
        self.state = "loading"
        try:
            from faster_whisper import WhisperModel

            compute = "float16" if self.device == "cuda" else "int8"
            started = time.monotonic()
            self._model = WhisperModel(self.model_name, device=self.device, compute_type=compute)
            self.state = "ready"
            log.info("Whisper-Modell %s (%s) geladen in %.1f s", self.model_name, self.device,
                     time.monotonic() - started)
        except Exception as exc:  # Download fehlgeschlagen, Paket kaputt, keine GPU-Bibliotheken …
            self.state, self.error = "error", str(exc)
            log.error("Whisper-Modell %s nicht ladbar: %s", self.model_name, exc)
            raise

    def __call__(self, pcm: bytes, prompt: str | None) -> str:
        import numpy as np

        self.load()
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = self._model.transcribe(audio, language=self.language, beam_size=1, best_of=1,
                                             condition_on_previous_text=False, initial_prompt=prompt,
                                             vad_filter=False, without_timestamps=True)
        return " ".join(segment.text.strip() for segment in segments).strip()


class WakeDetector(Protocol):
    def __call__(self, pcm: bytes) -> bool: ...


def openwakeword_detector(threshold: float = 0.5) -> Callable[[], WakeDetector] | None:
    """„Hey Jarvis“ mit openWakeWord (vortrainiertes Modell hey_jarvis) – optional, None ohne Paket/Modell."""
    try:
        import numpy as np
        from openwakeword.model import Model
        from openwakeword.utils import download_models
    except ImportError:
        return None
    try:
        download_models(["hey_jarvis"])
    except Exception as exc:
        log.warning("openWakeWord-Modell nicht ladbar (%s) – Aktivierung nur über Whisper", exc)
        return None

    def factory() -> WakeDetector:
        model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        pending = bytearray()

        def detect(pcm: bytes) -> bool:
            pending.extend(pcm)
            hit = False
            while len(pending) >= 2560:  # 80 ms – Blockgröße von openWakeWord
                chunk = np.frombuffer(bytes(pending[:2560]), dtype=np.int16)
                del pending[:2560]
                scores = model.predict(chunk)
                hit = hit or max(scores.values(), default=0.0) >= threshold
            if hit:
                model.reset()
            return hit

        return detect

    return factory


def snr_db(pcm: bytes, noise: float, frame: int = 480) -> float:
    """Lautester 30-ms-Abschnitt gegenüber dem Grundrauschen (16-Bit-PCM) in dB – das Maß für „wie nah“."""
    samples = array("h", pcm[: len(pcm) - len(pcm) % 2])
    peak = 0.0
    for start in range(0, max(0, len(samples) - frame + 1), frame):
        chunk = samples[start:start + frame]
        peak = max(peak, math.sqrt(sum(s * s for s in chunk) / len(chunk)))
    return round(20 * math.log10(max(peak, 1.0) / max(noise, 1.0)), 1)


@dataclass
class Segmenter:
    """Sprachende-Erkennung über die Energie (Grundrauschen wird laufend nachgeführt)."""

    start_frames: int = 3            # 90 ms Sprache, bis eine Äußerung beginnt
    end_silence_ms: int = 800        # so lange Stille beendet sie
    max_ms: int = 15000              # Höchstlänge (dann wird trotzdem geschnitten)
    preroll_ms: int = 300
    min_level: float = 300.0         # absolute Untergrenze (16-Bit-RMS)
    noise: float = 150.0
    _voiced: int = 0
    _silence: int = 0
    _active: bool = False
    _buffer: bytearray = field(default_factory=bytearray)
    _preroll: deque[bytes] = field(default_factory=lambda: deque(maxlen=10))
    _pending: bytearray = field(default_factory=bytearray)

    def feed(self, pcm: bytes) -> list[tuple[str, bytes | None]]:
        """-> Ereignisse: ("start", None), ("end", audio)."""
        events: list[tuple[str, bytes | None]] = []
        self._pending.extend(pcm)
        while len(self._pending) >= FRAME * 2:
            frame = bytes(self._pending[:FRAME * 2])
            del self._pending[:FRAME * 2]
            samples = array("h", frame)
            level = math.sqrt(sum(s * s for s in samples) / len(samples))
            speech = level > max(self.min_level, self.noise * 3)
            if not self._active:
                self.noise = 0.95 * self.noise + 0.05 * min(level, 4000.0) if not speech else self.noise
                self._preroll.append(frame)
                self._voiced = self._voiced + 1 if speech else 0
                if self._voiced >= self.start_frames:
                    self._active, self._silence = True, 0
                    self._buffer = bytearray(b"".join(self._preroll))
                    events.append(("start", None))
                continue
            self._buffer.extend(frame)
            self._silence = 0 if speech else self._silence + 1
            too_long = len(self._buffer) >= self.max_ms * RATE * 2 // 1000
            if self._silence * 30 >= self.end_silence_ms or too_long:
                events.append(("end", bytes(self._buffer)))
                self.reset()
        return events

    def reset(self) -> None:
        self._active, self._voiced, self._silence = False, 0, 0
        self._buffer = bytearray()
        self._preroll.clear()


class LocalSpeech:
    """Gemeinsame Teile für alle Verbindungen: Whisper (ein Modell, eine Rechnung zur Zeit) und Wake-Word."""

    def __init__(self, transcriber: Transcriber, *, wake_factory: Callable[[], WakeDetector] | None = None,
                 prompt: Callable[[], str] | None = None) -> None:
        self.transcriber = transcriber
        self.wake_factory = wake_factory
        self.prompt = prompt or (lambda: "Jarvis")
        self._lock = asyncio.Lock()

    @property
    def state(self) -> str:
        return getattr(self.transcriber, "state", "ready")

    async def warm_up(self) -> None:
        loader = getattr(self.transcriber, "load", None)
        if loader is not None:
            try:
                await asyncio.to_thread(loader)
            except Exception:
                pass  # Zustand „error“ zeigt die Oberfläche; Browser-Erkennung bleibt nutzbar

    async def transcribe(self, pcm: bytes) -> str:
        async with self._lock:
            started = time.monotonic()
            text = await asyncio.to_thread(self.transcriber, pcm, self.prompt())
            log.info("Whisper: %.1f s Audio in %.2f s", len(pcm) / 2 / RATE, time.monotonic() - started)
            return text

    def session(self) -> AudioSession:
        return AudioSession(self, self.wake_factory() if self.wake_factory else None)


class AudioSession:
    """Eine Oberfläche: Audio rein, Ereignisse raus."""

    LISTEN_TIMEOUT_S = 7.0  # „Ja, Sir?“ und dann nichts gesagt

    def __init__(self, speech: LocalSpeech, wake: WakeDetector | None = None) -> None:
        self.speech = speech
        self.wake = wake
        self.mode = "idle"
        self.segmenter = Segmenter()
        self._listen_since = 0.0
        self._utterance = False
        self._recent = bytearray()  # die letzten 1,5 s – für „wie laut kam ‚Jarvis‘ hier an?“

    def set_mode(self, mode: str) -> None:
        if mode not in ("idle", "wake", "listen"):
            raise ValueError(f"Unbekannter Modus {mode}")
        if mode != self.mode:
            self.segmenter.reset()
            self._utterance = False
        self.mode = mode
        self._listen_since = time.monotonic() if mode == "listen" else 0.0

    async def feed(self, pcm: bytes) -> list[dict[str, Any]]:
        if self.mode == "idle":
            return []
        out: list[dict[str, Any]] = []
        self._recent.extend(pcm)
        del self._recent[:-RATE * 3]  # 1,5 s bei 16 Bit
        if self.mode == "wake" and self.wake is not None and not self._utterance:
            if await asyncio.to_thread(self.wake, pcm):
                score = snr_db(bytes(self._recent), self.segmenter.noise)
                self.set_mode("listen")
                return [{"type": "wake", "score": score}]
        for kind, audio in self.segmenter.feed(pcm):
            if kind == "start":
                self._utterance = True
                if self.mode == "listen":
                    out.append({"type": "speech"})
                continue
            self._utterance = False
            out += await self._utterance_done(audio or b"")
        if self.mode == "listen" and not self._utterance and \
                time.monotonic() - self._listen_since > self.LISTEN_TIMEOUT_S:
            self.set_mode("idle")
            out.append({"type": "nothing"})
        return out

    async def _utterance_done(self, audio: bytes) -> list[dict[str, Any]]:
        mode = self.mode
        try:
            text = (await self.speech.transcribe(audio)).strip()
        except Exception as exc:
            log.exception("Transkription fehlgeschlagen")
            return [{"type": "error", "message": f"Die lokale Spracherkennung ist ausgefallen: {exc}"}]
        text = re.sub(r"\s+", " ", text)
        if mode == "listen":
            command = WAKE_WORD.sub("", text).strip(" ,.")
            self.set_mode("idle")
            return [{"type": "transcript", "text": command}] if re.search(r"\w{2,}", command) else [{"type": "nothing"}]
        if not (match := WAKE_WORD.match(text)):
            return [{"type": "heard", "text": text}] if text else []
        command = text[match.end():].strip(" ,.")
        score = snr_db(audio, self.segmenter.noise)  # Abstimmung zwischen mehreren Geräten (rooms.WakeArbiter)
        if re.search(r"\w{2,}", command):
            self.set_mode("idle")
            return [{"type": "transcript", "text": command, "wake": True, "score": score}]
        self.set_mode("listen")
        return [{"type": "wake", "score": score}]


def create_local_speech(prompt: Callable[[], str] | None = None) -> LocalSpeech | None:
    """Lokale Spracherkennung, wenn faster-whisper installiert ist (Extra „voice-local“) – sonst None."""
    if os.environ.get("JARVIS_STT", "").lower() in ("off", "browser"):
        return None
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        log.info("Lokale Spracherkennung aus: faster-whisper fehlt (pip install 'jarvis-core[voice-local]')")
        return None
    return LocalSpeech(WhisperTranscriber(), wake_factory=openwakeword_detector(), prompt=prompt)
