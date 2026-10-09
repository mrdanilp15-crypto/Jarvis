"""Aus Daniels Gesprächsprotokoll: Korrekturen nach einer Suche, Rückbezug auf Geschriebenes, Ordner ohne Verb,
Tool-Aufrufe, die ein kleines Modell als Text schreibt, und Initialen im Formatter."""

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from jarvis.fastpath import FastPath, bare_term, clean_query, correction_term
from jarvis.orchestrator import TurnRequest
from jarvis.pc import AgentHub, register_pc_capabilities
from jarvis.style import JarvisStyle, split_sentences

from conftest import ALEX

NOW = datetime(2026, 9, 28, 15, 47, tzinfo=ZoneInfo("Europe/Berlin"))
ACTIONS = ["open_url", "open_app", "open_folder", "search_files", "find_files", "open_file", "app_search",
           "close_app", "type_text", "press_key", "click", "compose_mail", "notify"]


@pytest.mark.parametrize("text, capability, arguments", [
    ("Suche im Detail Explorer nach Minecraft.", "pc.search_files", {"query": "Minecraft"}),  # „Datei-Explorer“ verhört
    ("Such im Datei-Explorer nach Minecraft", "pc.search_files", {"query": "Minecraft"}),
    ("Nach dem Ordner Minecraft.", "pc.find_files", {"query": "Minecraft", "kind": "folder"}),
    ("Nach dem Ordner Minecraft ähnlich.", "pc.find_files", {"query": "Minecraft", "kind": "folder"}),
    ("Ordner mit dem Namen Minecraft.", "pc.find_files", {"query": "Minecraft", "kind": "folder"}),
    ("Die Datei Bewerbung", "pc.find_files", {"query": "Bewerbung", "kind": "file"}),
    ("Ordner Dokumente", "pc.open_folder", {"folder": "documents"}),
    ("Suche nach Arterien so wie ich es dir gerade geschrieben habe mit 2 i.", "pc.search_web", {"query": "Arterien"}),
])
def test_commands_from_the_protocol(text, capability, arguments):
    match = FastPath({}, {}).match(text, now=NOW)
    assert match is not None and (match.capability, match.arguments) == (capability, arguments)


@pytest.mark.parametrize("text", ["Ordner erstellen", "Datei löschen", "Die Datei Rechnung speichern",
                                  "Datei umbenennen"])
def test_file_actions_are_not_searches(text):
    match = FastPath({}, {}).match(text, now=NOW)  # „Ordner erstellen“ ist keine Suche nach „erstellen“
    assert match is None or match.capability != "pc.find_files"


def test_query_cleanup():
    assert clean_query("Arterien, so wie ich es dir gerade geschrieben habe, mit 2 i") == ("Arterien", True)
    assert clean_query("Arteriion mit doppel i") == ("Arteriion", False)
    assert clean_query("einem Lasagne Rezept") == ("Lasagne Rezept", False)


@pytest.mark.parametrize("text, expected", [
    ("ARTERII.", ("ARTERII", None)),
    ("ARTERIION", ("ARTERIION", None)),
    ("Nein, ich meinte Arteriion", ("Arteriion", None)),
    ("Es schreibt sich Arteriion", ("Arteriion", None)),
    ("Ich meinte Arteriion auf YouTube", ("Arteriion", "youtube")),
    ("Arteriion Live", ("Arteriion Live", None)),
    ("suche Heizung", ("Heizung", None)),  # Suchwort gehört nicht in den Begriff
    ("Gab es.", None), ("Gut", None), ("Danke", None), ("Was macht er so?", None),
    ("Peppe, was macht er so?", None), ("Die Ärzte", None),  # Artikel: nur mit „ich meinte …“
])
def test_correction_terms(text, expected):
    assert correction_term(text) == expected


def test_bare_terms():
    assert bare_term("ARTERIION") == "ARTERIION" and bare_term("Gab es.") is None
    assert bare_term("Kannst du Steam starten?") is None


def dialog(orchestrator, llm_texts=()):
    from jarvis.context import Situation
    from jarvis.testing import ScriptedProvider, say

    hub = AgentHub(timeout_s=1)
    hub.sent = []

    async def send(message):
        hub.sent.append(message)
        result = {"opened": True} if message["action"] == "app_search" else {}
        hub.resolve({"id": message["id"], "ok": True, "result": result})

    hub.attach(send, {"name": "PC", "apps": ["explorer"], "start_apps": ["Steam", "Spotify"], "actions": ACTIONS})
    register_pc_capabilities(orchestrator.registry, hub)
    orchestrator.app_resolver = hub.find_app
    llm = ScriptedProvider([say(t) for t in llm_texts] or [say("-")] * 10)

    def turn(text):
        request = TurnRequest(text=text, session_id="protokoll", principal=ALEX)
        return asyncio.run(orchestrator.handle_turn(request, provider=llm, situation=Situation(now=NOW)))
    return turn, hub


def test_corrections_repeat_the_search_with_the_new_word(orchestrator):
    turn, hub = dialog(orchestrator)
    assert turn("Suche auf Spotify nach Atherion.").route == "fast_path"
    assert hub.sent[-1]["arguments"] == {"app": "spotify", "query": "Atherion"}
    fixed = turn("ARTERII.")  # nicht mehr ans Sprachmodell („akademisches Netzwerk …“)
    assert fixed.route == "fast_path" and hub.sent[-1]["arguments"] == {"app": "spotify", "query": "ARTERII"}
    turn("ARTERIION")  # auch eine zweite Korrektur
    assert hub.sent[-1]["arguments"] == {"app": "spotify", "query": "ARTERIION"}
    # Rückbezug: „so wie ich es dir geschrieben habe“ -> das geschriebene Wort, gleicher Dienst
    turn("Suche nach Arterien so wie ich es dir gerade geschrieben habe mit 2 i.")
    assert hub.sent[-1]["arguments"] == {"app": "spotify", "query": "ARTERIION"}


def test_corrections_only_right_after_a_search(orchestrator):
    turn, hub = dialog(orchestrator)
    turn("Öffne den Explorer")
    count = len(hub.sent)
    assert turn("ARTERIION").route != "fast_path" and len(hub.sent) == count  # keine Suche davor
    turn("Suche auf Spotify nach Atherion.")
    assert turn("Gab es.").route != "fast_path"  # kein Begriff: normales Gespräch
    assert turn("ARTERIION").route != "fast_path"  # die Korrektur gilt nur im direkt folgenden Satz


def test_wrong_program_name_can_be_corrected(orchestrator):
    turn, hub = dialog(orchestrator)
    turn("Öffne Steam")
    assert hub.sent[-1]["arguments"] == {"app": "Steam"}
    turn("Nein, ich meinte Spotify")
    assert hub.sent[-1] == {**hub.sent[-1], "action": "open_app", "arguments": {"app": "Spotify"}}


def test_text_tool_calls_become_real_calls_and_are_never_spoken():
    httpx = pytest.importorskip("httpx")
    from jarvis.llm.base import SystemPrompt, ToolSpec, UserTurn
    from jarvis.llm.ollama import OllamaProvider, extract_text_tool_calls

    pieces = ["Dorf", ".\n", '{"name": "pc.open_folder", ', '"arguments": {"folder": "documents"}}', "."]
    lines = [{"message": {"role": "assistant", "content": p}, "done": False} for p in pieces]
    lines.append({"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop"})

    def handler(request):
        return httpx.Response(200, content="\n".join(json.dumps(line) for line in lines).encode())

    client = httpx.AsyncClient(base_url="http://ollama:11434", transport=httpx.MockTransport(handler))
    provider = OllamaProvider(client=client, model="llama3.2:3b")
    spoken = []

    async def on_text(delta):
        spoken.append(delta)

    tool = ToolSpec("pc__open_folder", "Ordner öffnen.", {"type": "object", "properties": {}})
    response = asyncio.run(provider.complete(system=SystemPrompt("R"), transcript=[UserTurn("Ordner Minecraft")],
                                             tools=[tool], on_text=on_text))
    assert spoken == []  # weder „Dorf.“ noch JSON vorgelesen
    assert response.stop_reason == "tool_use" and response.text == ""
    assert [(c.name, c.arguments) for c in response.tool_calls] == [("pc__open_folder", {"folder": "documents"})]

    known = {"pc__search_files"}
    qwen = '<tool_call>\n{"name": "pc__search_files", "arguments": {"query": "Minecraft"}}\n</tool_call>'
    assert extract_text_tool_calls(qwen, known)[0][0].arguments == {"query": "Minecraft"}
    assert extract_text_tool_calls('Gern. {"name": "gibt.es_nicht", "arguments": {}}', known) == ([], "Gern")
    assert extract_text_tool_calls("Die Menge {1, 2} ist klein.", known) == ([], "Die Menge {1, 2} ist klein.")


def test_long_answers_still_stream():
    httpx = pytest.importorskip("httpx")
    from jarvis.llm.base import SystemPrompt, UserTurn
    from jarvis.llm.ollama import OllamaProvider

    pieces = ["Ein Transistor ist ein Halbleiterbauteil, ", "das Ströme schaltet ", "oder verstärkt."]
    lines = [{"message": {"role": "assistant", "content": p}, "done": False} for p in pieces]
    lines.append({"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop"})
    client = httpx.AsyncClient(base_url="http://ollama:11434", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content="\n".join(json.dumps(line) for line in lines).encode())))
    spoken = []

    async def on_text(delta):
        spoken.append(delta)

    response = asyncio.run(OllamaProvider(client=client, model="m").complete(
        system=SystemPrompt("R"), transcript=[UserTurn("Transistor?")], tools=[], on_text=on_text))
    assert spoken == pieces and response.text == "".join(pieces)  # nach den ersten 40 Zeichen live weiter


def test_initials_are_not_sentence_ends():
    assert split_sentences("Mit A.R.T.E.R.I.I. meinen Sie etwas anderes. Gut.") == [
        "Mit A.R.T.E.R.I.I. meinen Sie etwas anderes.", "Gut."]
    assert JarvisStyle().finalize("Was meinen Sie mit A.R.T.E.R.I.I. genau?") == "Was meinen Sie mit A.R.T.E.R.I.I. genau?"
