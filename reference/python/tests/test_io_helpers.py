"""Webhooks, Kontext-Budget, Persona-Formatierung, Satz-Segmentierung, Logging-Redaktion."""

import json
import logging
from datetime import datetime

import pytest

from jarvis.context import ContextBudgets, ContextBuilder, Situation
from jarvis.errors import JarvisError
from jarvis.fastpath import FastPath, confirmation_reply
from jarvis.llm.base import AssistantTurn, ToolCall, ToolResult, ToolResultsTurn, UserTurn
from jarvis.logging_setup import JsonFormatter, bind, redact
from jarvis.persona import Persona
from jarvis.voice.pipeline import SentenceSegmenter
from jarvis.webhooks import ReplayCache, sign, verify

from conftest import REPO

SECRET = b"s3cr3t"


def test_webhook_signature_roundtrip_and_replay():
    body = b'{"event":"ring"}'
    cache = ReplayCache()
    sig = sign(SECRET, 1_790_000_000, body)
    verify(SECRET, body=body, timestamp_header="1790000000", signature_header=sig, delivery_id="d1",
           replay_cache=cache, now=1_790_000_010)
    with pytest.raises(JarvisError, match="Replay"):
        verify(SECRET, body=body, timestamp_header="1790000000", signature_header=sig, delivery_id="d1",
               replay_cache=cache, now=1_790_000_011)


@pytest.mark.parametrize(("ts", "sig_body", "now"), [
    ("1790000000", b'{"event":"ring"}', 1_790_000_400),   # zu alt
    ("1790000000", b'{"event":"open"}', 1_790_000_010),   # manipulierter Body
    ("abc", b'{"event":"ring"}', 1_790_000_010),          # kaputter Zeitstempel
])
def test_webhook_rejections(ts, sig_body, now):
    sig = sign(SECRET, 1_790_000_000, sig_body)
    with pytest.raises(JarvisError) as exc:
        verify(SECRET, body=b'{"event":"ring"}', timestamp_header=ts, signature_header=sig, delivery_id="d2",
               replay_cache=ReplayCache(), now=now)
    assert exc.value.code == "JRV-AUTH-002"


def test_history_trim_keeps_tool_rounds_intact():
    call = ToolCall("t1", "home__get_state", {"entity_ids": ["light.kueche"]})
    transcript = [
        UserTurn("alt " * 400),
        AssistantTurn("", [call], "x"),
        ToolResultsTurn([ToolResult("t1", "home__get_state", "x" * 400)]),
        AssistantTurn("Fertig.", [], "x"),
        UserTurn("neu"),
        AssistantTurn("ok", [], "x"),
    ]
    trimmed = ContextBuilder(budgets=ContextBudgets(history=200)).trim_history(transcript)
    assert isinstance(trimmed[0], UserTurn) and trimmed[0].text == "neu"
    full = ContextBuilder(budgets=ContextBudgets(history=10_000)).trim_history(transcript)
    assert full == transcript


def test_system_prompt_static_part_is_stable():
    persona = Persona.load(REPO / "config" / "persona.jarvis.yaml")
    builder = ContextBuilder(persona=persona)
    a = builder.system_prompt(Situation(now=datetime(2026, 9, 26, 8, 0)), ["Alex mag Tee"])
    b = builder.system_prompt(Situation(now=datetime(2026, 9, 26, 9, 30), area="kueche"), [])
    assert a.static == b.static, "volatile Inhalte dürfen den cachebaren Präfix nicht verändern"
    assert "J.A.R.V.I.S." in a.static and "<untrusted_content>" in a.static
    assert "Alex mag Tee" in a.dynamic and "Alex mag Tee" not in b.dynamic


def test_persona_voice_formatting():
    persona = Persona.load(REPO / "config" / "persona.jarvis.yaml",
                           schema_path=REPO / "schemas" / "persona.schema.json")
    text = "**Erledigt.** Die Küche ist auf 40 %.\n- Punkt eins. Noch ein Satz. Und noch einer."
    spoken = persona.for_voice(text)
    assert "*" not in spoken and "- " not in spoken
    assert spoken.count(".") == 3  # max_voice_sentences = 3
    assert persona.address_for("sam") == "Ma'am"
    assert not persona.humor_allowed("warning") and persona.humor_allowed("status")


def test_sentence_segmenter_streams_complete_sentences():
    seg = SentenceSegmenter()
    out = []
    for delta in ["Sehr wohl. Die Temperatur beträgt z. B. 21", ".5 Grad im Wohnzimmer. Außer", "dem regnet es."]:
        out += seg.feed(delta)
    out += seg.flush()
    assert out == ["Sehr wohl. Die Temperatur beträgt z. B. 21.5 Grad im Wohnzimmer.", "Außerdem regnet es."]


@pytest.mark.parametrize(("text", "expected"), [
    ("Ja", True), ("ja, bitte!", True), ("Nein danke.", False), ("Mach das Licht an", None), ("Jaguar", None),
])
def test_confirmation_replies(text, expected):
    assert confirmation_reply(text) is expected


def test_fast_path_grammar():
    fp = FastPath({"kueche": ["light.kueche"]}, {"küche": "kueche"})
    assert fp.match("Mach das Licht in der Küche aus").arguments == {"entity_ids": ["light.kueche"], "on": False}
    assert fp.match("Dimme das Licht in der Küche auf 30 %").arguments["brightness_pct"] == 30
    assert fp.match("Stell einen Timer auf zwölf Minuten").arguments == {"duration_s": 720}
    assert fp.match("Mach das Licht im Keller an") is None  # unbekannter Raum -> LLM


def test_log_redaction():
    record = logging.LogRecord("jarvis", logging.INFO, __file__, 1, "call with Bearer abcdefghijkl", None, None)
    record.headers = {"Authorization": "Bearer xyz", "code": "JRV-DEV-001"}
    with bind(correlation_id="cor_1"):
        line = json.loads(JsonFormatter("test").format(record))
    assert line["correlation_id"] == "cor_1"
    assert "abcdefghijkl" not in line["msg"]
    assert line["headers"] == {"Authorization": "[REDACTED]", "code": "JRV-DEV-001"}
    assert redact("PIN 1234 bitte") == "[REDACTED] bitte"
