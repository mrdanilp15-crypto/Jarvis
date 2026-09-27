import asyncio
import json

import pytest

from jarvis.errors import JarvisError
from jarvis.llm.base import AssistantTurn, LLMResponse, ToolCall, ToolResultsTurn, UserTurn
from jarvis.orchestrator import TurnRequest
from jarvis.testing import ScriptedProvider, call_tool, say

from conftest import ALEX, GUEST, SAM


def turn(orchestrator, text, principal=ALEX, session="s1", provider=None, situation=None, **kw):
    return asyncio.run(orchestrator.handle_turn(
        TurnRequest(text, session, principal, **kw), provider=provider or ScriptedProvider([]), situation=situation))


def test_fast_path_runs_without_llm(orchestrator, home, situation):
    result = turn(orchestrator, "Mach das Licht in der Küche auf vierzig Prozent", situation=situation)
    assert result.route == "fast_path"
    assert result.actions[0].status == "succeeded"
    assert home.state("light.kueche")["attributes"]["brightness"] == 102
    assert result.actions[0].verification["verified"] is True
    assert result.actions[0].undo == {"capability": "home.set_light",
                                      "arguments": {"entity_ids": ["light.kueche"], "on": False}}


def test_llm_tool_call_executes_and_returns_results_in_one_message(orchestrator, home, situation):
    llm = ScriptedProvider([
        LLMResponse(text="", stop_reason="tool_use", tool_calls=[
            ToolCall("t1", "home__set_light", {"entity_ids": ["light.kueche"], "on": True}),
            ToolCall("t2", "home__get_state", {"entity_ids": ["climate.wohnzimmer"]}),
        ], assistant_turn=AssistantTurn("", [
            ToolCall("t1", "home__set_light", {"entity_ids": ["light.kueche"], "on": True}),
            ToolCall("t2", "home__get_state", {"entity_ids": ["climate.wohnzimmer"]}),
        ], "scripted")),
        say("Licht an; im Wohnzimmer sind es 20,5 Grad."),
    ])
    result = turn(orchestrator, "Licht in der Küche an und wie warm ist es?", provider=llm, situation=situation)
    assert result.text.startswith("Licht an")
    assert home.state("light.kueche")["state"] == "on"
    second_request = llm.requests[1]["transcript"]
    assert isinstance(second_request[-1], ToolResultsTurn)
    assert {r.call_id for r in second_request[-1].results} == {"t1", "t2"}


def test_invalid_tool_input_is_not_executed(orchestrator, home, situation):
    llm = ScriptedProvider([
        call_tool("home__set_light", {"entity_ids": ["light.kueche"], "on": True, "brightness_pct": 400}),
        say("Das war ungültig."),
    ])
    turn(orchestrator, "Licht auf 400 Prozent", provider=llm, situation=situation)
    results = llm.requests[1]["transcript"][-1].results
    assert results[0].is_error and "INVALID_INPUT" in results[0].content
    assert home.calls == []


def test_unknown_tool_returns_error(orchestrator, situation):
    llm = ScriptedProvider([call_tool("door__open_all", {}), say("Kann ich nicht.")])
    turn(orchestrator, "Öffne alles", provider=llm, situation=situation)
    assert llm.requests[1]["transcript"][-1].results[0].is_error


def test_r3_requires_app_confirmation_voice_yes_is_not_enough(orchestrator, home, situation):
    llm = ScriptedProvider([call_tool("home__lock", {"entity_id": "lock.haustuer", "state": "unlocked"}),
                            say("Bitte in der App bestätigen.")])
    result = turn(orchestrator, "Öffne die Haustür", provider=llm, situation=situation)
    pending = result.pending_confirmation
    assert pending is not None and pending.method == "app_biometric"
    payload = json.loads(llm.requests[1]["transcript"][-1].results[0].content)
    assert payload["status"] == "confirmation_required"
    assert home.state("lock.haustuer")["state"] == "locked"

    yes = turn(orchestrator, "Ja", situation=situation)
    assert yes.route == "confirmation_resolver"
    assert home.state("lock.haustuer")["state"] == "locked"

    with pytest.raises(JarvisError) as exc:
        asyncio.run(orchestrator.resolve_confirmation(pending.id, approve=True, resolver=SAM, method_used="app"))
    assert exc.value.code == "JRV-POL-002"

    record = asyncio.run(orchestrator.resolve_confirmation(pending.id, approve=True, resolver=ALEX,
                                                           method_used="app_biometric"))
    assert record.status == "succeeded"
    assert home.state("lock.haustuer")["state"] == "unlocked"


def test_voice_can_reject_strong_confirmation(orchestrator, home, situation):
    llm = ScriptedProvider([call_tool("home__lock", {"entity_id": "lock.haustuer", "state": "unlocked"}),
                            say("Bitte bestätigen.")])
    turn(orchestrator, "Tür auf", provider=llm, situation=situation)
    result = turn(orchestrator, "Nein, abbrechen", situation=situation)
    assert result.actions[0].status == "rejected"
    assert home.state("lock.haustuer")["state"] == "locked"


def test_prompt_injection_taints_session_and_escalates_r2(orchestrator, home, situation, audit):
    llm = ScriptedProvider([
        call_tool("web__fetch", {"url": "https://example.org"}),
        call_tool("home__set_climate", {"entity_id": "climate.wohnzimmer", "temperature": 30}),
        say("Soll ich die Heizung wirklich verstellen?"),
    ])
    result = turn(orchestrator, "Fasse example.org zusammen", provider=llm, situation=situation)
    assert result.tainted
    fetched = llm.requests[1]["transcript"][-1].results[0].content
    assert fetched.startswith("<untrusted_content")
    climate = next(a for a in result.actions if a.capability == "home.set_climate")
    assert climate.status == "pending_confirmation"
    assert climate.decision.rule_id == "risk.R2.tainted"
    assert home.state("climate.wohnzimmer")["attributes"]["temperature"] == 21

    # Derselbe Sprecher bestätigt per Stimme -> wird ausgeführt
    confirmed = turn(orchestrator, "Ja bitte", situation=situation)
    assert confirmed.actions[0].status == "succeeded"
    assert home.state("climate.wohnzimmer")["attributes"]["temperature"] == 30
    assert any(e["event"] == "confirmation.resolved" for e in audit.entries)


def test_other_speaker_cannot_confirm(orchestrator, situation):
    llm = ScriptedProvider([
        call_tool("web__fetch", {"url": "https://example.org"}),
        call_tool("home__set_climate", {"entity_id": "climate.wohnzimmer", "temperature": 25}),
        say("Bestätigen?"),
    ])
    turn(orchestrator, "Lies example.org", provider=llm, situation=situation)
    result = turn(orchestrator, "Ja", principal=SAM, situation=situation)
    assert result.pending_confirmation is not None
    assert result.actions == []


def test_confirmation_word_inside_command_is_not_a_confirmation(orchestrator, home, situation):
    llm = ScriptedProvider([call_tool("home__lock", {"entity_id": "lock.haustuer", "state": "unlocked"}),
                            say("Bitte bestätigen.")])
    turn(orchestrator, "Tür auf", provider=llm, situation=situation)
    result = turn(orchestrator, "Mach das Licht im Wohnzimmer an", situation=situation)
    assert result.route == "fast_path"
    assert home.state("lock.haustuer")["state"] == "locked"


def test_guest_denied(orchestrator, home, situation):
    llm = ScriptedProvider([call_tool("home__lock", {"entity_id": "lock.haustuer", "state": "unlocked"}),
                            say("Das darf ich nicht.")])
    result = turn(orchestrator, "Tür auf", principal=GUEST, provider=llm, situation=situation)
    assert result.actions[0].status == "denied"
    assert result.pending_confirmation is None


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_truncated_or_refused_tool_calls_never_run(orchestrator, home, situation, stop_reason):
    call = ToolCall("t1", "home__set_light", {"entity_ids": ["light.kueche"], "on": True})
    llm = ScriptedProvider([LLMResponse(text="", tool_calls=[call], stop_reason=stop_reason,
                                        assistant_turn=AssistantTurn("", [call], "scripted"))])
    result = turn(orchestrator, "Licht an", provider=llm, situation=situation)
    assert result.stop_reason == stop_reason
    assert home.calls == []
    last = orchestrator.sessions["s1"].transcript[-1]
    assert isinstance(last, AssistantTurn) and last.tool_calls == []


def test_iteration_limit(orchestrator, situation):
    llm = ScriptedProvider([call_tool("home__get_state", {"entity_ids": ["light.kueche"]}) for _ in range(4)])
    result = turn(orchestrator, "Schau immer wieder nach", provider=llm, situation=situation)
    assert result.stop_reason == "max_iterations"


def test_provider_failure_rolls_back_transcript(orchestrator, situation):
    def boom(_):
        raise JarvisError("JRV-LLM-001", "offline")

    with pytest.raises(JarvisError):
        turn(orchestrator, "Hallo", provider=ScriptedProvider([boom]), situation=situation)
    assert orchestrator.sessions["s1"].transcript == []
    ok = turn(orchestrator, "Hallo", provider=ScriptedProvider([say("Guten Abend.")]), situation=situation)
    assert ok.text == "Guten Abend."
    assert isinstance(orchestrator.sessions["s1"].transcript[0], UserTurn)


def test_action_results_match_schema(orchestrator, situation, validator):
    llm = ScriptedProvider([call_tool("home__lock", {"entity_id": "lock.haustuer", "state": "unlocked"}),
                            say("Bitte bestätigen.")])
    result = turn(orchestrator, "Tür auf", provider=llm, situation=situation)
    fast = turn(orchestrator, "Schalte das Licht im Wohnzimmer aus", session="s2", situation=situation)
    check = validator("action-result")
    for record in result.actions + fast.actions:
        check.validate(record.to_result_dict())


def test_memory_outage_degrades_gracefully(orchestrator, situation):
    class BrokenMemory:
        async def recall(self, *args, **kwargs):
            raise ConnectionError("embedding model not available")

    orchestrator.memory = BrokenMemory()
    result = turn(orchestrator, "Guten Abend", provider=ScriptedProvider([say("Guten Abend.")]), situation=situation)
    assert result.text == "Guten Abend."


def test_each_question_keeps_its_own_context(orchestrator, situation):
    # Grundlage für den KV-Cache lokaler Modelle: der Verlauf ändert sich nachträglich nicht
    provider = ScriptedProvider([say("Guten Abend."), say("Gern.")])
    for text in ("Hallo", "Danke"):
        asyncio.run(orchestrator.handle_turn(TurnRequest(text=text, session_id="ctx", principal=ALEX),
                                             provider=provider, situation=situation))
    users = [t for t in orchestrator.sessions["ctx"].transcript if isinstance(t, UserTurn)]
    assert [u.text for u in users] == ["Hallo", "Danke"]
    assert all("<situation>" in u.context for u in users)
