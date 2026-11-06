"""Lokale Piper-Stimme ohne Docker: Stimmen-Adresse, Synthese als WAV, Fehler werden zu einer klaren Meldung."""

import asyncio
import io
import wave

import pytest

from jarvis.errors import JarvisError
from jarvis.voice.piper_local import LocalPiperTTS, voice_urls


def test_voice_urls_point_to_the_piper_archive():
    model, config = voice_urls("de_DE-thorsten-high")
    assert model.endswith("/de/de_DE/thorsten/high/de_DE-thorsten-high.onnx")
    assert config == model + ".json"


class FakeVoice:
    def synthesize_wav(self, text, wav):
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        wav.writeframes(b"\x00\x01" * len(text))


def test_synthesis_returns_a_wav(tmp_path):
    tts = LocalPiperTTS(tmp_path, loader=lambda path: FakeVoice())
    data = asyncio.run(tts.synthesize_wav("Guten Abend, Sir."))
    with wave.open(io.BytesIO(data)) as wav:
        assert wav.getframerate() == 22050 and wav.getnframes() == len("Guten Abend, Sir.")
    assert tts.state == "ready"


def test_failing_voice_is_reported_clearly(tmp_path):
    def broken(path):
        raise OSError("Download fehlgeschlagen")

    tts = LocalPiperTTS(tmp_path, loader=broken)
    asyncio.run(tts.warm_up())  # darf den Start nicht abbrechen
    assert tts.state == "error"
    with pytest.raises(JarvisError) as exc:
        asyncio.run(tts.synthesize_wav("Hallo"))
    assert exc.value.user_message == "Die lokale JARVIS-Stimme ist gerade nicht verfügbar."
