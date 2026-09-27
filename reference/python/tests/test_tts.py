"""JARVIS-Stimme: Piper über das Wyoming-Protokoll und der Endpunkt /v1/tts."""

import asyncio
import io
import wave
from datetime import datetime

import pytest

wyoming = pytest.importorskip("wyoming")

from jarvis.voice.pipeline import WyomingTTS  # noqa: E402

from conftest import ALEX  # noqa: E402


async def fake_piper(requests):
    """Minimaler Wyoming-TTS-Server: beantwortet Synthesize mit zwei Audio-Blöcken (16-bit, mono, 22,05 kHz)."""
    from wyoming.audio import AudioChunk, AudioStart, AudioStop
    from wyoming.event import async_read_event, async_write_event
    from wyoming.tts import Synthesize

    async def handle(reader, writer):
        event = await async_read_event(reader)
        requests.append(Synthesize.from_event(event).text)
        await async_write_event(AudioStart(rate=22050, width=2, channels=1).event(), writer)
        for _ in range(2):
            await async_write_event(AudioChunk(rate=22050, width=2, channels=1, audio=b"\x01\x00" * 1000).event(),
                                    writer)
        await async_write_event(AudioStop().event(), writer)
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


def test_piper_sentence_becomes_wav():
    async def scenario():
        requests = []
        server = await fake_piper(requests)
        port = server.sockets[0].getsockname()[1]
        async with server:
            audio = await WyomingTTS("127.0.0.1", port).synthesize_wav("Guten Abend, Sir.")
        return requests, audio

    requests, audio = asyncio.run(scenario())
    assert requests == ["Guten Abend, Sir."]
    with wave.open(io.BytesIO(audio)) as wav:
        assert (wav.getframerate(), wav.getsampwidth(), wav.getnchannels(), wav.getnframes()) == (22050, 2, 1, 2000)


@pytest.fixture
def make_client(orchestrator):
    testclient = pytest.importorskip("fastapi.testclient")
    from jarvis.api import Container, create_app
    from jarvis.context import Situation
    from jarvis.events import InMemoryEventBus
    from jarvis.llm.router import ModelRouter
    from jarvis.testing import ScriptedProvider

    def make(tts):
        container = Container(
            orchestrator=orchestrator, bus=InMemoryEventBus(), router=ModelRouter(local=ScriptedProvider([]),
                                                                                  cloud=None),
            tokens={"tok_alex": ALEX}, webhook_secrets={}, tts=tts,
            situation=lambda who, channel: Situation(now=datetime(2026, 9, 27, 12, 0), channel=channel),
        )
        return testclient.TestClient(create_app(container))

    return make


AUTH = {"Authorization": "Bearer tok_alex"}


def test_tts_endpoint(make_client):
    class FakeTTS:
        async def synthesize_wav(self, text):
            return b"RIFF-" + text.encode()

    client = make_client(FakeTTS())
    response = client.post("/v1/tts", headers=AUTH, json={"text": "Sehr wohl."})
    assert response.status_code == 200 and response.headers["content-type"] == "audio/wav"
    assert response.content == "RIFF-Sehr wohl.".encode()
    assert client.post("/v1/tts", json={"text": "x"}).status_code == 401
    assert client.get("/v1/system/health").json()["tts"] == "configured"


def test_tts_unavailable_is_a_clear_error(make_client):
    class DownTTS:
        async def synthesize_wav(self, text):
            raise ConnectionRefusedError("wyoming-piper:10200")

    response = make_client(DownTTS()).post("/v1/tts", headers=AUTH, json={"text": "Hallo"})
    assert response.status_code == 502 and response.json()["user_message"].startswith("Die JARVIS-Stimme")
    assert make_client(None).get("/v1/system/health").json()["tts"] == "off"


def test_azure_conrad_request_and_escaping():
    httpx = pytest.importorskip("httpx")
    from jarvis.voice.cloud import AzureTTS

    captured = {}

    def handler(request):
        captured["url"], captured["headers"], captured["body"] = str(request.url), request.headers, request.content
        return httpx.Response(200, content=b"RIFF-azure")

    tts = AzureTTS("geheim", "germanywestcentral", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert asyncio.run(tts.synthesize_wav("Tür & Tor <offen>")) == b"RIFF-azure"
    assert captured["url"] == "https://germanywestcentral.tts.speech.microsoft.com/cognitiveservices/v1"
    assert captured["headers"]["ocp-apim-subscription-key"] == "geheim"
    assert captured["headers"]["x-microsoft-outputformat"] == "riff-24khz-16bit-mono-pcm"
    body = captured["body"].decode()
    assert "<voice name=\"de-DE-ConradNeural\">" in body and 'pitch="-3%"' in body
    assert "Tür &amp; Tor &lt;offen&gt;" in body  # Text kann das SSML nicht aufbrechen


def test_azure_rejects_bad_settings_and_maps_errors():
    httpx = pytest.importorskip("httpx")
    from jarvis.errors import JarvisError
    from jarvis.voice.cloud import AzureTTS

    with pytest.raises(ValueError):
        AzureTTS("k", "evil.example.com/x")
    with pytest.raises(ValueError):
        AzureTTS("k", "westeurope", pitch='-3%"><audio src="x')
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(401)))
    with pytest.raises(JarvisError) as exc:
        asyncio.run(AzureTTS("falsch", "westeurope", client=client).synthesize_wav("Hallo"))
    assert "abgelehnt" in exc.value.user_message


def test_fallback_uses_piper_when_azure_fails():
    from jarvis.errors import JarvisError
    from jarvis.voice.cloud import FallbackTTS

    class Down:
        label = "azure"

        async def synthesize_wav(self, text):
            raise JarvisError("JRV-INT-001", "Azure weg")

    class Piper:
        label = "piper"

        async def synthesize_wav(self, text):
            return b"RIFF-piper"

    tts = FallbackTTS(Down(), Piper())
    assert tts.label == "azure" and asyncio.run(tts.synthesize_wav("Hallo")) == b"RIFF-piper"


def test_voice_selection_from_config(monkeypatch):
    from jarvis.app import _tts

    cfg = {"voice": {"tts": {"uri": "tcp://wyoming-piper:10200", "cloud": {
        "provider": "azure", "key": "env:AZURE_SPEECH_KEY", "region": "env:AZURE_SPEECH_REGION",
        "voice": "de-DE-ConradNeural"}}}}
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    assert _tts(cfg).label == "piper"
    monkeypatch.setenv("AZURE_SPEECH_KEY", "k")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "GermanyWestCentral")
    monkeypatch.setenv("JARVIS_TTS_VOICE", "de-DE-FlorianMultilingualNeural")
    tts = _tts(cfg)
    assert tts.label == "azure" and tts.primary.voice == "de-DE-FlorianMultilingualNeural"
    assert tts.fallback.label == "piper"
