"""Hochwertige Sprachausgabe über Azure Speech (offizielle API, eigener Schlüssel) mit Rückfall auf Piper.

Standardstimme ist ``de-DE-ConradNeural``: tief, ruhig, klar – passend zur Jarvis-Persona. Der Text der Antworten
geht dafür an Microsoft; ohne Schlüssel bleibt JARVIS bei der lokalen Piper-Stimme.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from ..errors import JarvisError

log = logging.getLogger(__name__)

_PROSODY = re.compile(r"^[+-]?\d{1,2}(?:\.\d+)?(?:%|st)$")  # z. B. -4%, +2%, -1st


class AzureTTS:
    label = "azure"

    def __init__(self, key: str, region: str, *, voice: str = "de-DE-ConradNeural", rate: str = "-4%",
                 pitch: str = "-3%", client: Any = None) -> None:
        import httpx

        if not re.fullmatch(r"[a-z0-9]+", region):
            raise ValueError(f"Ungültige Azure-Region {region!r} (z. B. germanywestcentral, westeurope)")
        for name, value in (("rate", rate), ("pitch", pitch)):
            if not _PROSODY.fullmatch(value):
                raise ValueError(f"Ungültiger Wert für {name}: {value!r}")
        self._httpx = httpx
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._key = key
        self.url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
        self.voice, self.rate, self.pitch = voice, rate, pitch

    def ssml(self, text: str) -> str:
        return (
            '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="de-DE">'
            f"<voice name={quoteattr(self.voice)}>"
            f'<prosody rate="{self.rate}" pitch="{self.pitch}">{escape(text)}</prosody>'
            "</voice></speak>"
        )

    async def synthesize_wav(self, text: str) -> bytes:
        headers = {
            "Ocp-Apim-Subscription-Key": self._key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "riff-24khz-16bit-mono-pcm",
            "User-Agent": "JARVIS-Referenz",
        }
        try:
            response = await self._client.post(self.url, content=self.ssml(text).encode("utf-8"), headers=headers)
            response.raise_for_status()
        except self._httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            message = ("Der Azure-Schlüssel oder die Region wurde abgelehnt." if status in (401, 403)
                       else f"Azure Speech antwortet mit HTTP {status}.")
            raise JarvisError("JRV-INT-001", f"Azure TTS: HTTP {status}", user_message=message) from exc
        except self._httpx.HTTPError as exc:
            raise JarvisError("JRV-INT-001", f"Azure TTS nicht erreichbar: {exc}",
                              user_message="Azure Speech ist gerade nicht erreichbar.") from exc
        return response.content


class FallbackTTS:
    """Erst die Hauptstimme, bei Fehlern die Ersatzstimme (z. B. Azure -> Piper)."""

    def __init__(self, primary: Any, fallback: Any) -> None:
        self.primary, self.fallback = primary, fallback
        self.label = getattr(primary, "label", "configured")

    async def synthesize_wav(self, text: str) -> bytes:
        try:
            return await self.primary.synthesize_wav(text)
        except (JarvisError, OSError, TimeoutError) as exc:
            log.warning("Hauptstimme ausgefallen, nutze Ersatzstimme: %s", exc)
            return await self.fallback.synthesize_wav(text)
