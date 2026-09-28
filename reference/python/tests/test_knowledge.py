"""Wissensfragen nachschlagen statt raten – aus Daniels Protokoll: „ARTERIION ist ein deutsches Unternehmen …“
(erfunden aus dem Wikipedia-Artikel „Arterie“), „Nein, das ist ein Künstler“, „Such nach mehr Infos“."""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from jarvis.context import Situation
from jarvis.info import InfoConfig, register_info_capabilities
from jarvis.knowledge import (
    RULE,
    followup,
    knowledge_question,
    lead,
    mentions,
    sentences,
    title_matches,
    vague_query,
)
from jarvis.orchestrator import TurnRequest
from jarvis.pc import AgentHub, register_pc_capabilities
from jarvis.style import JarvisStyle
from jarvis.testing import ScriptedProvider, say
from jarvis.tools import InvocationContext
from jarvis.websearch import WebSearch, register_web_capabilities

from conftest import ALEX

NOW = datetime(2026, 9, 28, 20, 5, tzinfo=ZoneInfo("Europe/Berlin"))
ACTIONS = ["open_url", "open_app", "open_folder", "search_files", "find_files", "open_file", "app_search",
           "close_app", "type_text", "press_key", "click", "compose_mail", "notify"]
EINSTEIN = ("Albert Einstein (* 14. März 1879 in Ulm; † 18. April 1955 in Princeton, New Jersey) war ein "
            "theoretischer Physiker. Er gilt als einer der bedeutendsten Physiker der Wissenschaftsgeschichte. "
            "Seine Relativitätstheorie veränderte das Verständnis von Raum und Zeit. 1921 erhielt er den Nobelpreis. "
            "Er emigrierte 1933 in die USA.")
SPOTIFY = {"title": "ARTERIION | Spotify", "url": "https://open.spotify.com/artist/1",
           "content": "Hören Sie ARTERIION auf Spotify. Künstler · 312 monatliche Hörer."}
ARTERIE = {"title": "Arterie – Wikipedia", "url": "https://de.wikipedia.org/wiki/Arterie",
           "content": "Arterien sind Blutgefäße, die Blut vom Herzen wegführen."}


# ---------------------------------------------------------------------------------------------------------
# Erkennen
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    ("Wer ist ARTERIION?", ("ARTERIION", None, True)),
    ("Was ist ein Transistor?", ("Transistor", None, True)),
    ("Jarvis, wer ist eigentlich der Rapper Apache 207?", ("Apache 207", "Rapper", True)),
    ("Kennst du ARTERIION?", ("ARTERIION", None, True)),
    ("Weißt du, wer ARTERIION ist?", ("ARTERIION", None, True)),
    ("Was ist ARTERIION für ein Künstler?", ("ARTERIION", "Künstler", True)),
    ("Erzähl mir etwas über Ada Lovelace", ("Ada Lovelace", None, True)),
    ("Was bedeutet Photosynthese?", ("Photosynthese", None, True)),
    ("Wer sind Die Ärzte?", ("Die Ärzte", None, True)),
    ("Wer ist der Bundeskanzler?", ("Bundeskanzler", None, False)),  # fragt nach der Person: Quellen ans Modell
    ("Wer ist das eigentlich?", (None, None, True)),
    ("Was ist das für ein Künstler?", (None, "Künstler", True)),
])
def test_knowledge_questions(text, expected):
    question = knowledge_question(text)
    assert question is not None and (question.name, question.hint, question.direct) == expected


@pytest.mark.parametrize("text", ["Was ist los?", "Was ist 2 plus 2?", "Was ist das Wetter morgen?", "Wer bist du?",
                                  "Was ist dein Name?", "Was ist besser, Mac oder PC?", "Was ist mit dem Licht?",
                                  "Erklär mir, wie ein Motor funktioniert", "Öffne Steam", "Wer ist im Wohnzimmer?",
                                  "Wer ist gerade zu Hause?", "Was ist auf meiner Einkaufsliste?"])
def test_not_knowledge_questions(text):
    assert knowledge_question(text) is None


@pytest.mark.parametrize("text, expected", [
    ("Nein, das ist ein Künstler.", ("kind", "Künstler")),
    ("Das ist ein Rapper aus Berlin", ("kind", "Rapper aus Berlin")),
    ("Nein, ARTERIION ist ein Musiker", ("kind", "Musiker")),
    ("Ich meine den Künstler", ("kind", "Künstler")),
    ("Erzähl mir mehr", ("more", None)), ("Mehr Infos bitte", ("more", None)), ("Und weiter?", ("more", None)),
    ("Such nach mehr Infos.", ("search", None)), ("Such mehr darüber", ("search", None)),
    ("Google das", ("search", None)),
    ("Das ist ein Witz", None), ("Das ist falsch", None), ("Ja", None), ("Mein Hund ist ein Pudel", None),
])
def test_followups(text, expected):
    assert followup(text, "ARTERIION") == expected


def test_vague_search_terms():
    assert vague_query("mehr Infos") == "" and vague_query("mehr Informationen dazu") == ""
    assert vague_query("das") == "" and vague_query("diesen Künstler") == "Künstler"
    assert vague_query("Infos zu Minecraft") is None and vague_query("Arteriion") is None


def test_titles_sentences_and_mentions():
    assert not title_matches("Arterie", "ARTERIION")  # der Ursprung der erfundenen Firma
    assert title_matches("Albert Einstein", "Einstein") and title_matches("Queen (Band)", "Queen")
    assert sentences(EINSTEIN)[0] == "Albert Einstein war ein theoretischer Physiker."  # Lebensdaten weg
    assert sentences("Er lebte im 19. Jahrhundert, z. B. in Ulm. Dr. Müller kam 2010. Seitdem blieb er.") == [
        "Er lebte im 19. Jahrhundert, z. B. in Ulm.", "Dr. Müller kam 2010.", "Seitdem blieb er."]
    shown, rest = lead(EINSTEIN)
    assert shown.endswith("Wissenschaftsgeschichte.") and len(rest) == 3
    assert mentions("ARTERIION", "ARTERIION | Spotify") and not mentions("ARTERIION", ARTERIE["content"])


# ---------------------------------------------------------------------------------------------------------
# Wikipedia prüft den Titel
# ---------------------------------------------------------------------------------------------------------
def wikipedia_handler(request):
    path, query = request.url.path, request.url.params.get("q", "")
    if path.endswith("/search/page"):
        if "Einstein" in query:
            return httpx.Response(200, json={"pages": [{"key": "Albert_Einstein", "title": "Albert Einstein"}]})
        return httpx.Response(200, json={"pages": [{"key": "Arterie", "title": "Arterie"},
                                                   {"key": "Arteriosklerose", "title": "Arteriosklerose"}]})
    if path.endswith("/summary/Albert_Einstein"):
        return httpx.Response(200, json={"title": "Albert Einstein", "extract": EINSTEIN, "type": "standard",
                                         "content_urls": {"desktop": {
                                             "page": "https://de.wikipedia.org/wiki/Albert_Einstein"}}})
    if path.endswith("/summary/Arterie"):
        return httpx.Response(200, json={"title": "Arterie", "extract": "Arterien sind Blutgefäße.", "type": "standard"})
    return httpx.Response(404)


def test_wikipedia_ignores_similar_titles(registry):
    client = httpx.AsyncClient(transport=httpx.MockTransport(wikipedia_handler))
    register_info_capabilities(registry, InfoConfig(), client=client)
    wiki = registry.get("info.wikipedia")
    ctx = InvocationContext(correlation_id="c", actor="user:alex")
    missing = asyncio.run(wiki.handler({"query": "ARTERIION Unternehmen"}, ctx))
    assert missing["found"] is False and "Arterie" not in str(missing)
    found = asyncio.run(wiki.handler({"query": "Einstein Physiker"}, ctx))
    assert found["found"] is True and found["title"] == "Albert Einstein"
    assert asyncio.run(wiki.handler({"query": "Albert Einstein", "name": "Albert Einstein"}, ctx))["found"]


# ---------------------------------------------------------------------------------------------------------
# Dialoge über die echte Pipeline
# ---------------------------------------------------------------------------------------------------------
def setup(orchestrator, steps=(), *, web_results=None, offline=False):
    searches = []

    def web_handler(request):
        if offline:
            raise httpx.ConnectError("offline")
        query = request.url.params["q"]
        searches.append(query)
        results = (web_results or {}).get(query, [ARTERIE])
        return httpx.Response(200, json={"results": results})

    def wiki_handler(request):
        if offline:
            raise httpx.ConnectError("offline")
        return wikipedia_handler(request)

    register_info_capabilities(orchestrator.registry, InfoConfig(),
                               client=httpx.AsyncClient(transport=httpx.MockTransport(wiki_handler)))
    search = WebSearch(searxng_url="http://searx",
                       client=httpx.AsyncClient(transport=httpx.MockTransport(web_handler)))
    register_web_capabilities(orchestrator.registry, search)

    hub = AgentHub(timeout_s=1)
    hub.sent = []

    async def send(message):
        hub.sent.append(message)
        result = {"opened": True} if message["action"] == "app_search" else {}
        hub.resolve({"id": message["id"], "ok": True, "result": result})

    hub.attach(send, {"name": "PC", "apps": ["explorer"], "start_apps": ["Steam", "Spotify"], "actions": ACTIONS})
    register_pc_capabilities(orchestrator.registry, hub)
    orchestrator.app_resolver = hub.find_app
    orchestrator.style = JarvisStyle()
    llm = ScriptedProvider(list(steps))

    def turn(text):
        request = TurnRequest(text=text, session_id="wissen", principal=ALEX)
        return asyncio.run(orchestrator.handle_turn(request, provider=llm, situation=Situation(now=NOW)))
    return turn, llm, hub, searches


def test_protocol_unknown_artist_is_looked_up_not_invented(orchestrator):
    web = {"ARTERIION": [ARTERIE, SPOTIFY], "ARTERIION Künstler": [SPOTIFY]}
    turn, llm, hub, searches = setup(orchestrator, [
        say("Den Suchergebnissen nach ist ARTERIION ein Künstler auf Spotify."),
        say("Laut Spotify ist ARTERIION ein Künstler mit rund 300 Hörern."),
    ], web_results=web)

    first = turn("Wer ist ARTERIION?")
    assert first.route == "llm:scripted:research"
    asked = llm.requests[-1]["transcript"][-1]
    assert "open.spotify.com" in asked.sources and RULE in asked.sources
    assert "Arterie" not in asked.sources  # der ähnlich geschriebene Treffer gehört nicht zur Frage
    assert "kein passender Artikel" in asked.sources

    second = turn("Nein, das ist ein Künstler.")  # Klarstellung: neu nachschlagen, mit Art
    assert second.route == "llm:scripted:research" and searches[-1] == "ARTERIION Künstler"
    assert "Die vorige Antwort dazu war falsch" in llm.requests[-1]["transcript"][-1].sources

    third = turn("Such nach mehr Infos.")  # nicht mehr „mehr Infos“ googeln
    assert third.route == "fast_path"
    assert hub.sent[-1]["action"] == "open_url" and "ARTERIION+K%C3%BCnstler" in hub.sent[-1]["arguments"]["url"]


def test_nothing_found_is_said_honestly_and_offers_the_browser(orchestrator):
    turn, llm, hub, _ = setup(orchestrator)  # Websuche liefert nur „Arterie“
    result = turn("Was ist ARTERIION?")
    assert result.route == "research" and llm.requests == []  # kein Sprachmodell, nichts erfunden
    assert result.text.startswith("Zu „ARTERIION“ finde ich nichts Verlässliches, Sir")
    assert result.text.endswith("Soll ich im Browser danach suchen?") and result.awaiting_reply
    turn("Ja")
    assert hub.sent[-1]["action"] == "open_url" and "ARTERIION" in hub.sent[-1]["arguments"]["url"]


def test_wikipedia_answer_without_language_model_and_more(orchestrator):
    turn, llm, hub, _ = setup(orchestrator, [say("Er war 76 Jahre alt.")])
    result = turn("Wer war Albert Einstein?")
    assert result.route == "research" and llm.requests == []
    assert result.text == ("Laut Wikipedia: Albert Einstein war ein theoretischer Physiker. Er gilt als einer der "
                           "bedeutendsten Physiker der Wissenschaftsgeschichte.")
    more = turn("Erzähl mir mehr")
    assert more.route == "research" and more.text.startswith("Seine Relativitätstheorie")
    done = turn("Und weiter?")
    assert done.text == "Mehr steht in meinen Quellen nicht, Sir. Soll ich den Wikipedia-Artikel öffnen?"
    turn("Ja")
    assert hub.sent[-1]["arguments"] == {"url": "https://de.wikipedia.org/wiki/Albert_Einstein"}
    # Folgefrage ans Sprachmodell: Es sieht die nachgeschlagenen Quellen und die eigenen Antworten im Verlauf
    turn("Wie alt ist er geworden?")
    seen = llm.requests[-1]["transcript"]
    assert any("Princeton" in getattr(t, "sources", "") for t in seen)
    assert any(getattr(t, "text", "").startswith("Laut Wikipedia") for t in seen)


def test_who_is_that_after_a_search(orchestrator):
    turn, llm, hub, searches = setup(orchestrator, [say("Laut Spotify ein Künstler.")],
                                     web_results={"ARTERIION": [SPOTIFY]})
    turn("Suche auf Spotify nach ARTERIION")
    assert turn("Wer ist das eigentlich?").route == "llm:scripted:research" and searches[-1] == "ARTERIION"


def test_vague_search_without_topic_asks_back(orchestrator):
    turn, llm, hub, _ = setup(orchestrator)
    result = turn("Such nach mehr Infos")
    assert result.text == "Wonach soll ich suchen, Sir?" and result.awaiting_reply and hub.sent == []


def test_correcting_the_name_looks_up_again(orchestrator):
    turn, llm, hub, searches = setup(orchestrator, [say("Den Suchergebnissen nach ein Künstler.")],
                                     web_results={"ARTERIION": [SPOTIFY]})
    turn("Wer ist Arterion?")  # nichts gefunden
    assert turn("ARTERIION").route == "llm:scripted:research" and searches[-1] == "ARTERIION"


def test_blocked_web_search_is_not_claimed_as_searched(orchestrator):
    turn, llm, hub, _ = setup(orchestrator)
    orchestrator.registry.get("web.search").handler = broken  # DuckDuckGo verlangt eine Bot-Prüfung
    result = turn("Was ist ARTERIION?")
    assert result.text.startswith("Zu „ARTERIION“ steht nichts in der Wikipedia, Sir, und die Websuche ist gerade")


async def broken(args, ctx):
    from jarvis.errors import JarvisError
    raise JarvisError("JRV-INT-001", "Bot-Prüfung", user_message="Die Websuche hat die Anfrage gerade abgelehnt.")


def test_interjections_after_an_answer_are_not_new_lookups(orchestrator):
    turn, llm, hub, searches = setup(orchestrator, [say("Freut mich."), say("Ja.")])
    turn("Wer war Albert Einstein?")
    count = len(searches)
    assert turn("Krass").route.startswith("llm") and turn("Interessant.").route.startswith("llm")
    assert len(searches) == count


def test_offline_lets_the_model_answer_carefully(orchestrator):
    turn, llm, hub, _ = setup(orchestrator, [say("Das weiß ich leider nicht sicher.")], offline=True)
    assert turn("Wer ist ARTERIION?").route == "llm:scripted:research"
    assert "erfinde nichts" in llm.requests[-1]["transcript"][-1].sources


def test_commands_are_part_of_the_history(orchestrator):
    turn, llm, hub, _ = setup(orchestrator, [say("Gern.")])
    turn("Öffne Steam")
    turn("Was hast du gerade gemacht?")
    texts = [getattr(t, "text", "") for t in llm.requests[-1]["transcript"]]
    assert texts[:2] == ["Öffne Steam", "Sehr wohl. Steam ist geöffnet."]
