"""Werkzeug-Auswahl je Satz und Zuspruch bei Befinden – gegen falsche Werkzeugaufrufe kleiner Modelle."""

import asyncio

import pytest

from jarvis.fastpath import conversation_intent
from jarvis.orchestrator import TurnRequest
from jarvis.testing import ScriptedProvider, say
from jarvis.toolgate import wanted_domains

KNOWN = frozenset({"home", "info", "web", "calendar", "assistant", "timer", "reminder", "mail", "memory", "pc", "system",
                   "automation", "message", "vision"})


@pytest.mark.parametrize("text, expected, absent", [
    ("Mir geht es nicht so gut", set(), {"assistant", "system", "info"}),
    ("Ja, bitte.", set(), {"system", "assistant"}),
    ("Mach es etwas heller im Wohnzimmer", {"home"}, {"system"}),
    ("Wie viel Prozent hat der Akku noch?", {"system"}, {"home"}),
    ("Erinnere mich in zehn Minuten an die Wäsche", {"reminder", "timer"}, {"system"}),
    ("Wer war Konrad Adenauer?", {"info", "web"}, {"home"}),
    ("Starte Spotify", {"pc"}, {"home"}),
])
def test_domains(text, expected, absent):
    found = wanted_domains(text, KNOWN)
    assert expected <= found and not (absent & found)


def test_short_followups_keep_the_topic_but_chat_does_not():
    before = wanted_domains("Wie wird das Wetter?", KNOWN)
    assert "info" in wanted_domains("Und morgen?", KNOWN, before)
    assert wanted_domains("Ja, bitte.", KNOWN, wanted_domains("Mir geht es nicht gut", KNOWN)) == frozenset()


@pytest.mark.parametrize("text, intent", [
    ("Mir geht es nicht so gut.", "unwell"), ("Ich bin krank", "unwell"), ("Ich habe Kopfschmerzen", "unwell"),
    ("Ich habe Brustschmerzen und Atemnot", "emergency"), ("Ich will nicht mehr leben", "crisis"),
])
def test_wellbeing(text, intent):
    assert conversation_intent(text) == intent


def test_unwell_gets_a_caring_answer_without_model_or_tools(orchestrator, situation):
    from jarvis.style import JarvisStyle

    orchestrator.style = JarvisStyle()
    provider = ScriptedProvider([])
    result = asyncio.run(orchestrator.handle_turn(TurnRequest("Mir geht es nicht so gut", "w1", __import__("conftest").ALEX),
                                                  provider=provider, situation=situation))
    assert result.route == "conversation" and not result.actions and not provider.requests
    assert "116117" in result.text and "112" in result.text
    crisis = asyncio.run(orchestrator.handle_turn(TurnRequest("Ich will nicht mehr leben", "w2", __import__("conftest").ALEX),
                                                  provider=provider, situation=situation))
    assert "0800 111 0 111" in crisis.text and "112" in crisis.text


def test_chat_turn_gets_no_tools_and_a_warning(orchestrator, situation):
    from conftest import ALEX

    llm = ScriptedProvider([say("Das klingt anstrengend, Sir.")])
    asyncio.run(orchestrator.handle_turn(TurnRequest("Ich hatte einen langen Tag", "c1", ALEX), provider=llm,
                                         situation=situation))
    request = llm.requests[0]
    assert request["tools"] == [] and "keine Werkzeuge" in request["system"].dynamic


def test_light_request_still_gets_home_tools(orchestrator, situation):
    from conftest import ALEX

    llm = ScriptedProvider([say("Erledigt.")])
    asyncio.run(orchestrator.handle_turn(TurnRequest("Könntest du bitte das Stehlicht etwas dunkler machen", "c2", ALEX),
                                         provider=llm, situation=situation))
    names = {t.name for t in llm.requests[0]["tools"]}
    assert "home__set_light" in names and "system__status" not in names
