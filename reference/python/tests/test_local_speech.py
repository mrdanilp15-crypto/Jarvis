"""Lokale Spracherkennung: Äußerungen schneiden, „Jarvis“ erkennen, Whisper-Ergebnis als Befehl – und der Endpunkt."""

import asyncio
import math
import struct
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from jarvis.api import Container, create_app
from jarvis.context import Situation
from jarvis.events import InMemoryEventBus
from jarvis.llm.router import ModelRouter
from jarvis.policy import Principal
from jarvis.testing import ScriptedProvider
from jarvis.voice.local import RATE, AudioSession, LocalSpeech, Segmenter, WhisperTranscriber

DANIEL = Principal(actor="user:owner", role="adult", trust="trusted_user", name="Daniel")


def tone(ms: int, amplitude: int = 6000) -> bytes:
    n = RATE * ms // 1000
    return struct.pack(f"<{n}h", *(int(amplitude * math.sin(2 * math.pi * 220 * i / RATE)) for i in range(n)))


def quiet(ms: int) -> bytes:
    n = RATE * ms // 1000
    return struct.pack(f"<{n}h", *((i % 7 - 3) * 10 for i in range(n)))


def utterance(ms: int = 900) -> bytes:
    return quiet(300) + tone(ms) + quiet(1000)


class FakeWhisper:
    def __init__(self, *texts):
        self.texts, self.calls = list(texts), []

    def __call__(self, pcm, prompt):
        self.calls.append((len(pcm), prompt))
        return self.texts.pop(0)


def feed(session, audio, chunk_ms=80):
    events, step = [], RATE * chunk_ms // 1000 * 2
    for start in range(0, len(audio), step):
        events += asyncio.run(session.feed(audio[start:start + step]))
    return events


def test_segmenter_cuts_one_utterance_with_preroll():
    events = Segmenter().feed(utterance(600))
    assert [kind for kind, _ in events] == ["start", "end"]
    seconds = len(events[1][1]) / 2 / RATE
    assert 1.2 < seconds < 1.8  # Vorlauf + Sprache + Sprachende-Pause


def test_wake_word_and_command_in_one_breath():
    whisper = FakeWhisper("Jarvis, mach das Licht im Wohnzimmer an.")
    session = LocalSpeech(whisper, prompt=lambda: "Jarvis, Wohnzimmer").session()
    session.set_mode("wake")
    assert feed(session, utterance()) == [{"type": "transcript", "text": "mach das Licht im Wohnzimmer an"}]
    assert whisper.calls[0][1] == "Jarvis, Wohnzimmer"  # Wörterbuch für Räume und Namen
    assert session.mode == "idle"  # danach nichts hören, bis JARVIS geantwortet hat


def test_only_jarvis_then_the_command():
    session = LocalSpeech(FakeWhisper("Hey Jarwis!", "Wie wird das Wetter morgen?")).session()
    session.set_mode("wake")
    assert feed(session, utterance(500)) == [{"type": "wake"}]
    assert session.mode == "listen"
    assert feed(session, utterance()) == [{"type": "speech"},
                                          {"type": "transcript", "text": "Wie wird das Wetter morgen?"}]


def test_other_talk_is_ignored_and_idle_hears_nothing():
    whisper = FakeWhisper("Wir essen um sieben.")
    session = LocalSpeech(whisper).session()
    session.set_mode("wake")
    assert feed(session, utterance()) == [{"type": "heard", "text": "Wir essen um sieben."}]
    session.set_mode("idle")
    assert feed(session, utterance()) == [] and len(whisper.calls) == 1  # während JARVIS spricht: verwerfen


def test_listening_without_speech_times_out(monkeypatch):
    session = LocalSpeech(FakeWhisper()).session()
    session.set_mode("listen")
    monkeypatch.setattr(AudioSession, "LISTEN_TIMEOUT_S", 0.0)
    assert feed(session, quiet(200)) == [{"type": "nothing"}]


def test_openwakeword_triggers_before_whisper():
    session = LocalSpeech(FakeWhisper("Schalte die Kaffeemaschine an"), wake_factory=lambda: (lambda pcm: True)).session()
    session.set_mode("wake")
    events = feed(session, quiet(100))
    assert events == [{"type": "wake"}] and session.mode == "listen"


def test_transcription_errors_are_reported():
    def broken(pcm, prompt):
        raise RuntimeError("Modell fehlt")

    session = LocalSpeech(broken).session()
    session.set_mode("listen")
    events = feed(session, utterance())
    assert events[-1]["type"] == "error" and "Modell fehlt" in events[-1]["message"]


def test_missing_model_marks_error_state(monkeypatch):
    transcriber = WhisperTranscriber(model="does-not-exist")
    speech = LocalSpeech(transcriber)
    asyncio.run(speech.warm_up())
    assert speech.state == "error" and transcriber.error


def make_client(speech):
    container = Container(orchestrator=None, bus=InMemoryEventBus(),
                          router=ModelRouter(local=ScriptedProvider([]), cloud=None), tokens={"t": DANIEL},
                          webhook_secrets={}, situation=lambda who, ch: Situation(now=datetime.now()), speech=speech)

    class Style:  # create_app braucht nur den Formatter
        name, version = "jarvis", "test"

        def error_message(self, message):
            return message

    container.orchestrator = type("O", (), {"style": Style()})()
    return TestClient(create_app(container))


def test_audio_endpoint(tmp_path):
    client = make_client(LocalSpeech(FakeWhisper("Jarvis, wie spät ist es?")))
    with client.websocket_connect("/v1/audio?token=t") as ws:
        assert ws.receive_json() == {"type": "ready", "state": "ready", "wake": False}
        ws.send_text('{"type": "mode", "mode": "wake"}')
        audio = utterance()
        for start in range(0, len(audio), 2560):
            ws.send_bytes(audio[start:start + 2560])
        assert ws.receive_json() == {"type": "transcript", "text": "wie spät ist es?"}
    assert client.get("/v1/system/health").json()["stt"] == "local:ready"


@pytest.mark.parametrize("token, code", [("falsch", 4401), ("t", 4404)])
def test_audio_endpoint_refuses(token, code):
    client = make_client(None if token == "t" else LocalSpeech(FakeWhisper()))
    with client.websocket_connect(f"/v1/audio?token={token}") as ws:
        if code == 4404:
            assert "nicht installiert" in ws.receive_json()["message"]
        message = ws.receive()
        assert message["type"] == "websocket.close" and message["code"] == code
