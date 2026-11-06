"""Listen, Rechnen/Einheiten/Währung, Radio im Browser, Fokus, Modellvergleich, Fehlerberichte, Tailscale-Adresse."""

import asyncio
import ipaddress

import httpx
import pytest

from jarvis.calc import convert_unit, register_calc_capabilities, safe_eval, spoken_to_expression
from jarvis.devices import local_addresses
from jarvis.fastpath import FastPath
from jarvis.feedback import FeedbackStore
from jarvis.lists import ListStore, register_list_capabilities
from jarvis.modelcompare import TESTS, compare_models
from jarvis.radio import register_radio_capabilities
from jarvis.style import JarvisStyle
from jarvis.testing import ScriptedProvider
from jarvis.tools import InvocationContext, ToolRegistry

CTX = InvocationContext(correlation_id="c", actor="user:a")


@pytest.mark.parametrize("text, capability, arguments", [
    ("Setz Milch und Brot auf die Einkaufsliste", "lists.add", {"list": "einkauf", "items": ["milch", "brot"]}),
    ("Schreib Zahnarzt anrufen auf die Todo-Liste", "lists.add", {"list": "todo", "items": ["zahnarzt anrufen"]}),
    ("Ich muss noch Eier kaufen", "lists.add", {"list": "einkauf", "items": ["eier"]}),
    ("Was steht auf meiner Einkaufsliste?", "lists.show", {"list": "einkauf"}),
    ("Streich Milch von der Einkaufsliste", "lists.remove", {"list": "einkauf", "item": "milch"}),
    ("Leere die Einkaufsliste", "lists.clear", {"list": "einkauf"}),
    ("Was sind 15 Prozent von 240?", "info.calc", {"mode": "percent", "pct": 15.0, "of": 240.0}),
    ("Was ist 12 mal 7?", "info.calc", {"mode": "calc", "expression": "12 mal 7"}),
    ("Wurzel aus 144", "info.calc", {"mode": "calc", "expression": "wurzel aus 144"}),
    ("Wie viel sind 50 Dollar in Euro", "info.calc", {"mode": "currency", "amount": 50.0, "from": "dollar", "to": "euro"}),
    ("Wie viele Meilen sind 30 Kilometer?", "info.calc", {"mode": "convert", "amount": 30.0, "from": "kilometer", "to": "meilen"}),
    ("Spiel Radio Bob", "media.radio", {"station": "bob"}),
    ("Radio aus", "media.radio_stop", {}),
    ("Starte Fokus 25 Minuten", "timer.start", {"duration_s": 1500, "label": "Fokus"}),
    ("Vergleiche meine Modelle", "system.model_compare", {}),
])
def test_grammar(text, capability, arguments):
    found = FastPath({}).match(text)
    assert (found.capability, found.arguments) == (capability, arguments)


def test_radio_in_a_room_still_goes_to_home_assistant():
    assert FastPath({}).match("Spiel Radio Bob im Wohnzimmer").capability == "home.play_radio"


def test_lists(tmp_path):
    registry = ToolRegistry()
    register_list_capabilities(registry, ListStore(tmp_path / "l.json"))
    call = lambda name, args: asyncio.run(registry.get(name).handler(args, CTX))  # noqa: E731
    first = call("lists.add", {"list": "einkauf", "items": ["milch", "Brot"]})
    assert first["items"] == ["Milch", "Brot"] and call("lists.add", {"items": ["MILCH", "käse"]})["known"] == ["MILCH"]
    assert call("lists.remove", {"item": "milch"})["removed"] == "Milch"
    assert call("lists.remove", {"item": "Wein"})["removed"] is None
    assert call("lists.add", {"list": "todo", "items": ["Steuer"]})["name"] == "To-do-Liste"
    assert ListStore(tmp_path / "l.json").items("einkauf") == ["Brot", "Käse"]  # überlebt Neustart
    style = JarvisStyle()
    assert style.finalize(style.list_text("lists.show", call("lists.show", {"list": "einkauf"}))[1]) == \
        "Auf der Einkaufsliste stehen 2 Einträge: Brot, Käse."
    assert call("lists.clear", {})["cleared"] == 2


def test_calculator_is_safe():
    assert spoken_to_expression("12 mal 7 plus 3") == "12*7+3" and safe_eval("12*7+3") == 87
    assert safe_eval(spoken_to_expression("wurzel aus 144")) == 12 and safe_eval(spoken_to_expression("1,5 mal 4")) == 6
    assert spoken_to_expression("__import__('os')") is None
    with pytest.raises(ValueError):
        safe_eval("9**9**9")  # Exponent begrenzt
    assert round(convert_unit(30, "kilometer", "meilen"), 2) == 18.64
    assert convert_unit(100, "celsius", "fahrenheit") == 212 and round(convert_unit(2, "pfund", "gramm")) == 1000


def test_currency_uses_the_ecb_rate():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json={"amount": 1, "base": "USD", "date": "2026-11-05", "rates": {"EUR": 0.862}})

    registry = ToolRegistry()
    register_calc_capabilities(registry, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = asyncio.run(registry.get("info.calc").handler({"mode": "currency", "amount": 50, "from": "dollar", "to": "euro"}, CTX))
    assert seen == {"base": "USD", "symbols": "EUR"} and round(result["value"], 2) == 43.1
    assert JarvisStyle().calc_text(result) == "50 USD sind rund 43,1 EUR (Kurs vom 05.11.)."
    assert JarvisStyle().calc_text({"mode": "percent", "pct": 15.0, "of": 240.0, "value": 36.0}) == "15 Prozent von 240 sind 36."


def test_radio_returns_a_stream_for_the_browser():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"name": "RADIO BOB!", "url_resolved": "https://stream.example/bob.mp3", "votes": 900,
                                          "countrycode": "DE", "bitrate": 128, "codec": "MP3"}])

    registry = ToolRegistry()
    register_radio_capabilities(registry, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert asyncio.run(registry.get("media.radio").handler({"station": "bob"}, CTX)) == {
        "station": "RADIO BOB!", "url": "https://stream.example/bob.mp3", "alternatives": []}


def test_model_compare_scores_tool_choice(orchestrator):
    from jarvis.llm.base import AssistantTurn, LLMResponse, ToolCall

    class Model:
        def __init__(self, good):
            self.good, self.calls = good, 0

        async def complete(self, *, system, transcript, tools, on_text=None, effort=None):
            text = transcript[-1].text
            wants = {"Licht": "home__set_light", "Wetter": "info__weather"}
            hit = next((tool for key, tool in wants.items() if key in text), None) if self.good else "system__status"
            calls = [ToolCall("t", hit, {})] if hit else []
            return LLMResponse(text="ok", stop_reason="tool_use" if calls else "end_turn", tool_calls=calls,
                               assistant_turn=AssistantTurn("ok", calls, "fake"))

    class Ollama:
        _client = type("C", (), {"get": staticmethod(lambda path: _tags())})()

    async def _tags():
        return type("R", (), {"json": lambda s: {"models": [{"name": "gut:8b"}, {"name": "schlecht:3b"}, {"name": "bge-m3"}]},
                              "raise_for_status": lambda s: None})()

    rows = asyncio.run(compare_models(Ollama(), orchestrator.registry, lambda name: Model(name.startswith("gut"))))
    assert [r["model"] for r in rows] == ["gut:8b", "schlecht:3b"] and rows[0]["correct"] > rows[1]["correct"]
    assert rows[0]["total"] == len(TESTS)


def test_feedback_report_groups_repeats(tmp_path):
    store = FeedbackStore(tmp_path / "f.jsonl")
    for _ in range(2):
        store.add(said="Wie spät ist es", reply="Falsch", route="llm:ollama")
    store.add(said="Hallo", reply="x")
    assert store.report().startswith("Mehrfach gemeldet: 2× „Wie spät ist es“")


def test_tailscale_addresses_are_offered(monkeypatch):
    monkeypatch.setenv("JARVIS_LAN_ADDRESS", "")
    monkeypatch.setattr("socket.gethostbyname_ex", lambda host: (host, [], ["100.101.102.103", "192.168.1.5", "172.17.0.2"]))
    found = local_addresses()
    assert "100.101.102.103" in found and "172.17.0.2" not in found
    assert ipaddress.ip_address("100.101.102.103").is_private is False  # darum die Sonderbehandlung


@pytest.mark.parametrize("text, station", [
    ("Spiele Antenne Bayern", "antenne bayern"), ("Spiel Antenne Bayern an", "antenne bayern"),
    ("Hör Deutschlandfunk", "deutschlandfunk"), ("Spiel 1Live", "1live"), ("Spiel Bayern 3", "bayern 3"),
])
def test_stations_without_the_word_radio(text, station):
    found = FastPath({}).match(text)
    assert (found.capability, found.arguments) == ("media.radio", {"station": station})


@pytest.mark.parametrize("text", ["Spiel Bob Marley", "Spiel Minecraft", "Spiel Arteriion auf Spotify"])
def test_music_and_games_are_no_radio(text):
    found = FastPath({}).match(text)
    assert found is None or found.capability != "media.radio"


def test_invented_tool_calls_are_retracted(orchestrator, situation):
    from jarvis.orchestrator import TurnRequest
    from jarvis.style import JarvisStyle
    from jarvis.testing import say

    from conftest import ALEX

    orchestrator.style = JarvisStyle()
    llm = ScriptedProvider([say("Sehr wohl. (Tool-Aufruf: media.play_radio) Die Sendung läuft jetzt.")])
    result = asyncio.run(orchestrator.handle_turn(TurnRequest("Mach mir was Schönes", "k1", ALEX),
                                                  provider=llm, situation=situation))
    assert result.retracted and "nicht ausgeführt" in result.text and "läuft jetzt" not in result.text


def test_radio_prefers_direct_streams_and_offers_alternatives():
    import httpx

    from jarvis.radio import find_station

    stations = [{"name": "ANTENNE BAYERN", "url_resolved": "https://a/live.m3u8", "hls": 1, "votes": 9000, "countrycode": "DE"},
                {"name": "ANTENNE BAYERN", "url_resolved": "http://b/stream.mp3", "votes": 500, "countrycode": "DE"},
                {"name": "ANTENNE BAYERN", "url_resolved": "https://c/stream.aac", "votes": 100, "countrycode": "DE"}]
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=stations)))
    found = asyncio.run(find_station("Antenne Bayern", client=client))
    assert found["url"] == "https://c/stream.aac" and found["alternatives"] == ["http://b/stream.mp3"]
