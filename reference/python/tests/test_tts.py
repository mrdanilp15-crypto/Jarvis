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
