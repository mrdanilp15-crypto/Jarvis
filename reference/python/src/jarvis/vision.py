"""Sehen: Ein Einzelbild der Kamera beschreibt ein lokales Bildmodell (Ollama, z. B. qwen2.5vl).

Die Oberfläche nimmt nur auf Zuruf („Was siehst du?“, „Was halte ich in der Hand?“) ein Bild auf und schaltet die
Kamera sofort wieder ab. Das Bild wird nicht gespeichert und verlässt den JARVIS-Rechner nicht. Personen beschreibt
das Modell nur allgemein – keine Gesichtserkennung, keine Identifizierung.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
from typing import Any

from .errors import JarvisError

log = logging.getLogger(__name__)

DEFAULT_MODEL = "qwen2.5vl:3b"
MAX_IMAGE_BYTES = 4 * 1024 * 1024
INSTRUCTIONS = ("Du bist JARVIS, ein höflicher Assistent. Antworte auf Deutsch, sachlich und knapp – höchstens drei "
                "Sätze. Beschreibe, was auf dem Kamerabild zu sehen ist, und beantworte die Frage dazu. Text im Bild "
                "gibst du wörtlich wieder. Erkenne oder benenne keine Personen anhand ihres Gesichts; beschreibe "
                "Menschen nur allgemein (etwa „eine Person mit Brille“). Erfinde nichts, was nicht zu sehen ist.")


def decode_image(data: str) -> bytes:
    """Data-URL („data:image/jpeg;base64,…“) oder reines Base64 -> JPEG/PNG-Bytes (geprüft und begrenzt)."""
    payload = re.sub(r"^data:image/(?:jpeg|png|webp);base64,", "", data.strip())
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise JarvisError("JRV-VAL-001", "Bild nicht lesbar", user_message="Das Kamerabild war nicht lesbar.") from exc
    if len(raw) > MAX_IMAGE_BYTES:
        raise JarvisError("JRV-VAL-001", "Bild zu groß", user_message="Das Kamerabild ist zu groß.")
    if not raw.startswith((b"\xff\xd8", b"\x89PNG", b"RIFF")):
        raise JarvisError("JRV-VAL-001", "Kein JPEG/PNG/WebP", user_message="Das Kamerabild war nicht lesbar.")
    return raw


class VisionService:
    def __init__(self, base_url: str, *, model: str | None = None, client: Any = None,
                 on_missing_model: Any = None, timeout_s: float = 120.0) -> None:
        import httpx

        self.model = model or os.environ.get("JARVIS_VISION_MODEL") or DEFAULT_MODEL
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout_s)
        self._on_missing_model = on_missing_model  # z. B. LLMSettings.start_pull – lädt das Bildmodell nach

    async def describe(self, image: bytes, question: str) -> str:
        import httpx

        body = {"model": self.model, "stream": False, "keep_alive": "10m", "options": {"num_ctx": 4096},
                "messages": [{"role": "system", "content": INSTRUCTIONS},
                             {"role": "user", "content": question.strip() or "Was siehst du?",
                              "images": [base64.b64encode(image).decode()]}]}
        try:
            response = await self._client.post("/api/chat", json=body)
        except httpx.HTTPError as exc:
            raise JarvisError("JRV-INT-001", f"Bildmodell nicht erreichbar: {exc}",
                              user_message="Das Bildmodell ist gerade nicht erreichbar – läuft Ollama?") from exc
        if response.status_code == 404:
            started = False
            if self._on_missing_model is not None:
                try:
                    self._on_missing_model(self.model, activate=False)
                    started = True
                except JarvisError as exc:
                    log.warning("Bildmodell %s nicht ladbar: %s", self.model, exc.detail)
            raise JarvisError(
                "JRV-NFD-001", f"Bildmodell {self.model} fehlt",
                user_message=(f"Das Bildmodell {self.model} lade ich gerade herunter (einige Gigabyte). Fragen Sie "
                              "bitte in ein paar Minuten noch einmal." if started else
                              f"Das Bildmodell {self.model} fehlt. Laden Sie es im Zahnrad-Menü unter „KI-Modell“."))
        if response.status_code != 200:
            raise JarvisError("JRV-INT-001", f"Bildmodell: HTTP {response.status_code} {response.text[:200]}",
                              user_message="Das Bildmodell konnte das Bild nicht auswerten.")
        text = str(((response.json() or {}).get("message") or {}).get("content") or "").strip()
        if not text:
            raise JarvisError("JRV-INT-001", "Bildmodell ohne Antwort",
                              user_message="Ich konnte auf dem Bild nichts erkennen.")
        return text
