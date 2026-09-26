"""Voice-Pipeline: Audio -> STT -> Kern -> Satz-Segmentierung -> TTS -> Audio.

Wake-Word (openWakeWord "hey_jarvis") und VAD laufen auf dem Satelliten; der Server erhält den Audio-Abschnitt
einer Äußerung. STT (faster-whisper) und TTS (Piper) werden über das Wyoming-Protokoll angesprochen – dieselben
Dienste, die auch Home Assistant nutzt.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

# Abkürzungen, nach denen kein Satzende vorliegt
_ABBREVIATIONS = ("z. B.", "z.B.", "d. h.", "u. a.", "bzw.", "ca.", "Dr.", "Nr.", "Mr.", "Mrs.", "etc.", "usw.", "vgl.")
_BOUNDARY = re.compile(r"(?<=[.!?…])\s+")


class SentenceSegmenter:
    """Puffert Token-Deltas und gibt vollständige Sätze aus, sobald sie sicher abgeschlossen sind.

    So kann die TTS den ersten Satz sprechen, während das LLM noch schreibt (erste Silbe < 1,5 s).
    """

    def __init__(self, min_chars: int = 12) -> None:
        self._buffer = ""
        self.min_chars = min_chars

    def feed(self, delta: str) -> list[str]:
        self._buffer += delta
        sentences: list[str] = []
        while True:
            match = self._find_boundary()
            if match is None:
                return sentences
            sentence, self._buffer = self._buffer[: match].strip(), self._buffer[match:].lstrip()
            if sentence:
                sentences.append(sentence)

    def flush(self) -> list[str]:
        rest, self._buffer = self._buffer.strip(), ""
        return [rest] if rest else []

    def _find_boundary(self) -> int | None:
        for m in _BOUNDARY.finditer(self._buffer):
            head = self._buffer[: m.start()]
            if len(head.strip()) < self.min_chars:
                continue
            if any(head.endswith(abbr) for abbr in _ABBREVIATIONS) or re.search(r"(?:^|\s)\w\.$", head):
                continue  # Abkürzung ("bzw.", "z." in "z. B.")
            if re.search(r"\d\.$", head) and re.match(r"\s*\d", self._buffer[m.end():]):
                continue  # Dezimalzahl / Datum
            return m.end()
        return None


@dataclass(frozen=True)
class AudioFormat:
    rate: int = 16000
    width: int = 2  # Bytes pro Sample (PCM16)
    channels: int = 1


class WyomingSTT:
    def __init__(self, host: str, port: int, language: str = "de") -> None:
        self.host, self.port, self.language = host, port, language

    async def transcribe(self, chunks: AsyncIterator[bytes], fmt: AudioFormat = AudioFormat()) -> str:
        from wyoming.asr import Transcribe, Transcript
        from wyoming.audio import AudioChunk, AudioStart, AudioStop
        from wyoming.client import AsyncTcpClient

        async with AsyncTcpClient(self.host, self.port) as client:
            await client.write_event(Transcribe(language=self.language).event())
            await client.write_event(AudioStart(rate=fmt.rate, width=fmt.width, channels=fmt.channels).event())
            async for chunk in chunks:
                await client.write_event(
                    AudioChunk(rate=fmt.rate, width=fmt.width, channels=fmt.channels, audio=chunk).event()
                )
            await client.write_event(AudioStop().event())
            while True:
                event = await client.read_event()
                if event is None:
                    raise ConnectionError("STT-Verbindung beendet")
                if Transcript.is_type(event.type):
                    return Transcript.from_event(event).text


class WyomingTTS:
    def __init__(self, host: str, port: int, voice: str | None = None) -> None:
        self.host, self.port, self.voice = host, port, voice

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        from wyoming.audio import AudioChunk, AudioStop
        from wyoming.client import AsyncTcpClient
        from wyoming.tts import Synthesize, SynthesizeVoice

        async with AsyncTcpClient(self.host, self.port) as client:
            voice = SynthesizeVoice(name=self.voice) if self.voice else None
            await client.write_event(Synthesize(text=text, voice=voice).event())
            while True:
                event = await client.read_event()
                if event is None or AudioStop.is_type(event.type):
                    return
                if AudioChunk.is_type(event.type):
                    yield AudioChunk.from_event(event).audio


TextHandler = Callable[[str, Callable[[str], Awaitable[None]]], Awaitable[str]]
AudioSink = Callable[[bytes], Awaitable[None]]


class VoiceSession:
    """Eine Äußerung: transkribieren, an den Kern geben, Antwort satzweise sprechen; Barge-in bricht ab."""

    def __init__(self, stt: WyomingSTT, tts: WyomingTTS, handle_text: TextHandler, play: AudioSink) -> None:
        self.stt, self.tts = stt, tts
        self.handle_text = handle_text  # (text, on_text_delta) -> finaler Text
        self.play = play
        self._speaking: asyncio.Task[None] | None = None

    async def handle_utterance(self, audio: AsyncIterator[bytes]) -> str:
        self.barge_in()
        text = await self.stt.transcribe(audio)
        segmenter = SentenceSegmenter()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def on_delta(delta: str) -> None:
            for sentence in segmenter.feed(delta):
                await queue.put(sentence)

        self._speaking = asyncio.create_task(self._speak(queue))
        await self.handle_text(text, on_delta)
        for sentence in segmenter.flush():
            await queue.put(sentence)
        await queue.put(None)
        await self._speaking
        return text

    async def _speak(self, queue: asyncio.Queue[str | None]) -> None:
        while (sentence := await queue.get()) is not None:
            async for chunk in self.tts.synthesize(sentence):
                await self.play(chunk)

    def barge_in(self) -> None:
        """Nutzer spricht dazwischen: laufende Ausgabe sofort abbrechen."""
        if self._speaking is not None and not self._speaking.done():
            self._speaking.cancel()
