"""PC-Steuerung: Sofortbefehle, Agent-Verbindung, Richtlinien und der Weg über die API bis zum Agenten."""

import asyncio
import time
from datetime import datetime

import pytest

from jarvis.errors import JarvisError
from jarvis.fastpath import FastPath
from jarvis.orchestrator import TurnRequest
from jarvis.pc import OUTDATED, AgentHub, register_pc_capabilities
from jarvis.policy import PolicyContext
from jarvis.tools import InvocationContext, ToolRegistry

from conftest import ALEX, GUEST

CTX = InvocationContext(correlation_id="c1", actor="user:alex", session_id="s1", area="wohnzimmer")


@pytest.mark.parametrize("text, capability, arguments", [
    ("Öffne den Explorer", "pc.open_app", {"app": "explorer"}),
    ("Jarvis, öffne den Datei-Explorer.", "pc.open_app", {"app": "explorer"}),
    ("starte den Browser", "pc.open_app", {"app": "browser"}),
    ("Mach mal den Taschenrechner auf", "pc.open_app", {"app": "rechner"}),
    ("Bitte starte Spotify", "pc.open_app", {"app": "spotify"}),
    ("Öffne meine Downloads", "pc.open_folder", {"folder": "downloads"}),
    ("öffne den Ordner Dokumente", "pc.open_folder", {"folder": "documents"}),
    ("Zeig mir die Bilder", "pc.open_folder", {"folder": "pictures"}),
    ("Öffne YouTube", "pc.open_url", {"url": "https://www.youtube.com"}),
    ("öffne heise.de", "pc.open_url", {"url": "https://heise.de"}),
    ("Such im Internet nach Rezepten mit Kürbis", "pc.search_web", {"query": "Rezepten mit Kürbis"}),
    ("google Wetter Berlin", "pc.search_web", {"query": "Wetter Berlin"}),
    # Natürliche Formen: Frage, Wunsch, Verb am Ende
    ("Kannst du den Explorer öffnen?", "pc.open_app", {"app": "explorer"}),
    ("Jarvis, kannst du mir bitte mal den Datei-Explorer aufmachen?", "pc.open_app", {"app": "explorer"}),
    ("Explorer öffnen", "pc.open_app", {"app": "explorer"}),
    ("Kannst du mir die Downloads zeigen?", "pc.open_folder", {"folder": "downloads"}),
    ("Könnten Sie YouTube öffnen?", "pc.open_url", {"url": "https://www.youtube.com"}),
    # Beliebige Programme (Startmenü): der Name geht an den PC-Agenten
    ("Öffne Steam", "pc.open_app", {"app": "steam"}),
    ("Kannst du Steam starten?", "pc.open_app", {"app": "steam"}),
    ("Steam starten", "pc.open_app", {"app": "steam"}),
    ("Starte bitte Discord", "pc.open_app", {"app": "discord"}),
    ("Ich möchte Minecraft spielen", "pc.open_app", {"app": "minecraft"}),
    ("Öffne Counter-Strike 2", "pc.open_app", {"app": "counter-strike 2"}),
    ("Öffne Steam auf dem PC", "pc.open_app", {"app": "steam"}),
    # Suchen: Web, direkt auf einer Seite, Dateien
    ("Kannst du nach Kürbissuppe suchen?", "pc.search_web", {"query": "Kürbissuppe"}),
    ("Such auf YouTube nach Katzenvideos", "pc.search_web", {"query": "Katzenvideos", "site": "youtube"}),
    ("Kannst du mir Katzenvideos auf YouTube zeigen?", "pc.search_web", {"query": "Katzenvideos", "site": "youtube"}),
    ("Spiel Lofi Musik auf YouTube", "pc.open_link", {"query": "Lofi Musik", "site": "youtube"}),  # erstes Video
    ("Such auf Amazon nach Kopfhörern", "pc.search_web", {"query": "Kopfhörern", "site": "amazon"}),
    ("Such die Datei Rechnung", "pc.search_files", {"query": "Rechnung"}),
    ("Kannst du nach der Datei Steuererklärung suchen?", "pc.search_files", {"query": "Steuererklärung"}),
    ("Finde Urlaubsfotos auf meinem PC", "pc.search_files", {"query": "Urlaubsfotos"}),
    # Link heraussuchen und direkt öffnen
    ("Such mir einen Link zu Lasagne und öffne ihn", "pc.open_link", {"query": "Lasagne"}),
    ("Kannst du mir einen Link zu einem Lasagne Rezept suchen und öffnen?", "pc.open_link",
     {"query": "Lasagne Rezept"}),
    ("Such nach Pizza und öffne das erste Ergebnis", "pc.open_link", {"query": "Pizza"}),
    ("Öffne die Webseite von Media Markt", "pc.open_link", {"query": "Media Markt"}),
    ("Öffne den ersten Treffer für Wetter Berlin", "pc.open_link", {"query": "Wetter Berlin"}),
    ("Geh auf Chefkoch", "pc.open_link", {"query": "Chefkoch"}),
    ("Öffne das erste Video von Kurzgesagt", "pc.open_link", {"query": "Kurzgesagt", "site": "youtube"}),
    ("Geh auf YouTube", "pc.open_url", {"url": "https://www.youtube.com"}),
    ("Bring mich zu heise.de", "pc.open_url", {"url": "https://heise.de"}),
    ("Such mir ein paar Links zu Kürbissuppe", "web.search", {"query": "Kürbissuppe"}),
    # Dateien öffnen und finden
    ("Öffne die Datei Bewerbung", "pc.open_file", {"query": "Bewerbung", "kind": "file"}),
    ("Kannst du die Datei Bewerbung öffnen?", "pc.open_file", {"query": "Bewerbung", "kind": "file"}),
    ("Öffne die PDF Bewerbung", "pc.open_file", {"query": "Bewerbung .pdf", "kind": "file"}),
    ("Öffne Bewerbung.pdf", "pc.open_file", {"query": "bewerbung.pdf", "kind": "file"}),
    ("Öffne den Ordner Projekte", "pc.open_file", {"query": "Projekte", "kind": "folder"}),
    ("Wo ist meine Steuererklärung?", "pc.find_files", {"query": "Steuererklärung", "kind": "any"}),
    ("Wo habe ich die Datei Rechnung gespeichert?", "pc.find_files", {"query": "Rechnung", "kind": "file"}),
])
def test_pc_fast_path(text, capability, arguments):
    match = FastPath({}, {}).match(text)
    assert match is not None and match.capability == capability and match.arguments == arguments


@pytest.mark.parametrize("text", [
    "Öffne die Haustür", "Was ist ein Browser?", "Öffne das Fenster im Bad", "Kannst du mir das Wetter zeigen?",
    "Spiel Musik", "Kannst du das Licht anmachen?", "Wie spät ist es?",
])
def test_pc_fast_path_ignores_other_requests(text):
    assert FastPath({}, {}).match(text) is None


def test_light_without_home_assistant_is_answered_honestly():
    # Ohne verbundenes Haus: Treffer für home.set_light – der Orchestrator meldet „nicht verfügbar“ statt zu raten
    match = FastPath({}, {}).match("Mach das Licht aus")
    assert match.capability == "home.set_light" and match.grammar == "light_unavailable"


def test_hub_round_trip_and_errors():
    async def scenario():
        hub = AgentHub(timeout_s=0.2)
        with pytest.raises(JarvisError) as not_connected:
            await hub.invoke("open_app", {"app": "explorer"})
        assert not_connected.value.code == "JRV-DEV-001" and "Desktop-Verknüpfung" in not_connected.value.user_message

        sent = []

        async def send(message):
            sent.append(message)
            if message["arguments"].get("app") == "explorer":
                hub.resolve({"id": message["id"], "ok": True, "result": {"opened": "explorer"}})
            elif message["arguments"].get("app") == "steam":
                hub.resolve({"id": message["id"], "ok": False, "error": "Unbekanntes Programm 'steam'"})

        hub.attach(send, {"name": "PC"})
        assert await hub.invoke("open_app", {"app": "explorer"}) == {"opened": "explorer"}
        with pytest.raises(JarvisError) as unknown:
            await hub.invoke("open_app", {"app": "steam"})
        assert unknown.value.user_message == "Unbekanntes Programm 'steam'"
        with pytest.raises(JarvisError) as timeout:
            await hub.invoke("open_app", {"app": "paint"})  # Agent antwortet nicht
        assert timeout.value.code == "JRV-TMO-001"
        hub.detach(send)
        assert not hub.connected and sent[0]["type"] == "agent.invoke"

    asyncio.run(scenario())


START_MENU = ["Steam", "Steam Support Center", "Minecraft Launcher", "Google Chrome", "Counter-Strike 2",
              "Visual Studio Code", "Microsoft Teams (work or school)", "Microsoft Teams", "Discord"]


ACTIONS = ("open_url", "open_app", "open_folder", "search_files", "find_files", "open_file")


def connected_hub(start_apps=START_MENU, actions=ACTIONS):
    hub = AgentHub(timeout_s=1)
    sent = []

    async def send(message):
        sent.append(message)
        result = {"opened": message["arguments"].get("app")} if message["action"] == "open_app" else {}
        hub.resolve({"id": message["id"], "ok": True, "result": result})

    info = {"name": "PC", "apps": ["explorer", "browser", "spotify"], "start_apps": list(start_apps)}
    if actions is not None:
        info["actions"] = list(actions)
    hub.attach(send, info)
    hub.sent = sent
    return hub


@pytest.mark.parametrize("spoken, program", [
    ("steam", "Steam"), ("STEAM", "Steam"),                  # exakt, egal wie geschrieben
    ("minecraft", "Minecraft Launcher"),                     # Namensanfang
    ("chrome", "Google Chrome"),                             # ganzes Wort
    ("counter strike 2", "Counter-Strike 2"), ("counterstrike 2", "Counter-Strike 2"),
    ("teams", "Microsoft Teams"),                            # bei Gleichstand der kürzeste Name
    ("explorer", "explorer"),                                # feste Liste des Agenten zuerst
    ("fortnite", None), ("einkaufsliste", None), ("st", None),  # nicht installiert / kein ganzes Wort
    ("discort", "Discord"), ("minecraf", "Minecraft Launcher"),  # Hörfehler der Spracherkennung
    ("steem", "Steam"), ("teams", "Microsoft Teams"),          # „teams“ ist nicht „steam“
    ("spiel", None), ("minesweeper", None),                    # nicht ähnlich genug
])
def test_find_app_matches_installed_programs(spoken, program):
    assert connected_hub().find_app(spoken) == program


def test_find_app_passes_names_through_without_a_start_menu_list():
    assert AgentHub().find_app("steam") == "steam"  # nicht verbunden: der Aufruf meldet das dann ehrlich
    assert connected_hub(start_apps=[]).find_app("steam") == "steam"  # älterer Agent entscheidet selbst


def test_agent_can_report_new_programs():
    hub = connected_hub(start_apps=["Steam"])
    assert hub.find_app("discord") is None
    hub.update({"type": "agent.apps", "start_apps": ["Steam", "Discord"]})
    assert hub.find_app("discord") == "Discord"


def test_searches_open_the_right_pages():
    registry = ToolRegistry()
    hub = connected_hub()
    register_pc_capabilities(registry, hub)

    def run(capability, arguments):
        assert not registry.get(capability).validate(arguments)
        asyncio.run(registry.get(capability).handler(arguments, CTX))
        return hub.sent[-1]

    assert run("pc.search_web", {"query": "Wetter Berlin"})["arguments"] == {
        "url": "https://www.google.com/search?q=Wetter+Berlin"}
    assert run("pc.search_web", {"query": "Katzenvideos", "site": "youtube"})["arguments"] == {
        "url": "https://www.youtube.com/results?search_query=Katzenvideos"}
    assert run("pc.search_web", {"query": "Kopfhörer & Kabel", "site": "amazon"})["arguments"] == {
        "url": "https://www.amazon.de/s?k=Kopfh%C3%B6rer+%26+Kabel"}
    files = run("pc.search_files", {"query": "Rechnung"})
    assert files["action"] == "search_files" and files["arguments"] == {"query": "Rechnung"}
    assert run("pc.open_app", {"app": "minecraft"})["arguments"] == {"app": "Minecraft Launcher"}


@pytest.mark.parametrize("app", ["C:\\Windows\\System32\\cmd.exe", "calc.exe | whoami", "x", "a\nb"])
def test_open_app_takes_names_not_paths_or_commands(app):
    registry = ToolRegistry()
    register_pc_capabilities(registry, AgentHub())
    assert registry.get("pc.open_app").validate({"app": app})  # Schema lehnt ab
    assert not registry.get("pc.open_app").validate({"app": "Microsoft Teams (work or school)"})


def test_outdated_agent_is_named_instead_of_failing_obscurely():
    registry = ToolRegistry()
    register_pc_capabilities(registry, connected_hub(actions=None))  # Agent vor 2.1.0: keine Dateisuche
    with pytest.raises(JarvisError) as exc:
        asyncio.run(registry.get("pc.search_files").handler({"query": "Rechnung"}, CTX))
    assert exc.value.user_message == OUTDATED


def test_unknown_program_names_go_to_the_llm(orchestrator):
    from jarvis.context import Situation
    from jarvis.testing import ScriptedProvider, say

    hub = connected_hub()
    register_pc_capabilities(orchestrator.registry, hub)
    orchestrator.app_resolver = hub.find_app
    situation = Situation(now=datetime(2026, 9, 27, 12, 0))

    def turn(text):
        request = TurnRequest(text=text, session_id="guess", principal=ALEX)
        return asyncio.run(orchestrator.handle_turn(request, provider=ScriptedProvider([say("Welche Liste?")]),
                                                    situation=situation))

    steam = turn("Kannst du Steam starten?")
    assert steam.route == "fast_path" and hub.sent[-1]["arguments"] == {"app": "Steam"}
    other = turn("Öffne die Einkaufsliste")  # kein Programm dieses Namens: nicht raten, das LLM fragt nach
    assert other.route != "fast_path" and other.text == "Welche Liste?" and len(hub.sent) == 1


class FakeWeb:
    def __init__(self, results=None, error=None):
        self.results, self.error, self.queries = results or [], error, []

    async def search(self, query, *, limit=5):
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.results[:limit]


def test_open_link_opens_the_first_result():
    from jarvis.websearch import SearchResult

    registry, hub = ToolRegistry(), connected_hub()
    web = FakeWeb([SearchResult("Chefkoch", "https://www.chefkoch.de/"), SearchResult("B", "https://b.example")])
    register_pc_capabilities(registry, hub, web=web)
    result = asyncio.run(registry.get("pc.open_link").handler({"query": "Chefkoch"}, CTX))
    assert hub.sent[-1]["arguments"] == {"url": "https://www.chefkoch.de/"}
    assert result == {"query": "Chefkoch", "via": "search", "url": "https://www.chefkoch.de/", "title": "Chefkoch"}
    asyncio.run(registry.get("pc.open_link").handler({"query": "Lofi Musik", "site": "youtube"}, CTX))
    assert web.queries[-1] == "Lofi Musik site:youtube.com"


def test_open_link_falls_back_to_the_browser_redirect():
    registry, hub = ToolRegistry(), connected_hub()
    register_pc_capabilities(registry, hub, web=FakeWeb(error=JarvisError("JRV-INT-001", "blockiert")))
    result = asyncio.run(registry.get("pc.open_link").handler({"query": "Chefkoch"}, CTX))
    assert hub.sent[-1]["arguments"] == {"url": "https://duckduckgo.com/?q=%5CChefkoch"} and result["via"] == "browser"


def test_open_link_without_agent_does_not_search():
    registry, web = ToolRegistry(), FakeWeb()
    register_pc_capabilities(registry, AgentHub(), web=web)
    with pytest.raises(JarvisError) as exc:
        asyncio.run(registry.get("pc.open_link").handler({"query": "Chefkoch"}, CTX))
    assert exc.value.code == "JRV-DEV-001" and web.queries == []


def test_file_capabilities_reach_the_agent():
    registry, hub = ToolRegistry(), connected_hub()
    register_pc_capabilities(registry, hub)
    asyncio.run(registry.get("pc.find_files").handler({"query": "Rechnung"}, CTX))
    assert hub.sent[-1]["action"] == "find_files" and hub.sent[-1]["arguments"] == {"query": "Rechnung", "kind": "any"}
    asyncio.run(registry.get("pc.open_file").handler({"id": 2}, CTX))
    assert hub.sent[-1]["action"] == "open_file" and hub.sent[-1]["arguments"] == {"id": 2}
    with pytest.raises(JarvisError) as exc:
        asyncio.run(registry.get("pc.open_file").handler({}, CTX))
    assert exc.value.user_message == "Welche Datei soll ich öffnen?"
    assert registry.get("pc.find_files").output_trust == "untrusted"  # Dateinamen sind Fremdinhalt
    assert registry.get("pc.open_link").risk_class == "R2"  # nach Fremdinhalten nur mit Bestätigung


def dialog(orchestrator, hub, web=None):
    """Orchestrator mit verbundenem Agenten, der Dateilisten liefert und Öffnen bestätigt."""
    from jarvis.context import Situation
    from jarvis.testing import ScriptedProvider, say

    files = [{"id": 1, "name": "Steuer_2025.pdf", "folder": "Documents", "kind": "file"},
             {"id": 2, "name": "Steuer_2024.pdf", "folder": "Documents\\Archiv", "kind": "file"}]

    last = []

    async def send(message):
        hub.sent.append(message)
        action, args = message["action"], message["arguments"]
        if action in ("find_files", "search_files"):
            # wie der echte Agent: Treffer je Suche neu nummeriert, die Liste bleibt für „die zweite“ gemerkt
            last[:] = [{**f, "id": i + 1} for i, f in enumerate(f for f in files
                                                               if args["query"].lower() in f["name"].lower())]
            result = {"query": args["query"], "total": len(last), "results": last}
        elif action == "open_file":
            item = last[args.get("id", 1) - 1]
            result = {"opened": item["name"], "folder": item["folder"], "kind": "file"}
        else:
            result = {}
        hub.resolve({"id": message["id"], "ok": True, "result": result})

    hub.sent = []
    hub.attach(send, {"name": "PC", "apps": ["explorer"], "start_apps": ["Steam"], "actions": list(ACTIONS)})
    register_pc_capabilities(orchestrator.registry, hub, web=web)
    orchestrator.app_resolver = hub.find_app
    situation = Situation(now=datetime(2026, 9, 27, 12, 0))

    def turn(text, llm=None):
        request = TurnRequest(text=text, session_id="d", principal=ALEX)
        return asyncio.run(orchestrator.handle_turn(request, provider=llm or ScriptedProvider([say("-")]),
                                                    situation=situation))
    return turn


def test_choose_from_a_file_list(orchestrator):
    hub = AgentHub(timeout_s=1)
    turn = dialog(orchestrator, hub)
    listing = turn("Wo ist meine Steuer?")
    assert listing.route == "fast_path" and hub.sent[-1]["action"] == "find_files"
    chosen = turn("Öffne die zweite")
    assert chosen.route == "fast_path" and hub.sent[-1]["arguments"] == {"id": 2}
    # Die Liste gilt nur für den direkt folgenden Satz
    assert turn("die erste").route != "fast_path" and hub.sent[-1]["arguments"] == {"id": 2}


def test_yes_opens_a_single_hit_only_after_jarvis_asked(orchestrator):
    from jarvis.testing import ScriptedProvider, call_tool, say

    hub = AgentHub(timeout_s=1)
    turn = dialog(orchestrator, hub)
    assert turn("Wo ist meine Steuer_2024?").route == "fast_path"
    assert turn("Ja").route == "fast_path" and hub.sent[-1]["arguments"] == {"id": 1}
    # Nach einer Liste des Sprachmodells ist „ja“ keine Auswahl – „die zweite“ schon
    llm = ScriptedProvider([call_tool("pc__find_files", {"query": "Steuer"}), say("Zwei Treffer. Mehr dazu?")])
    turn("Hast du meine Steuerunterlagen irgendwo gesehen?", llm)
    assert turn("Ja", ScriptedProvider([say("Gern.")])).text == "Gern."
    llm = ScriptedProvider([call_tool("pc__find_files", {"query": "Steuer"}), say("Zwei Treffer.")])
    turn("Hast du meine Steuerunterlagen irgendwo gesehen?", llm)
    assert turn("die zweite").route == "fast_path" and hub.sent[-1]["arguments"] == {"id": 2}


def test_choose_a_link_from_a_web_list_without_confirmation(orchestrator):
    from jarvis.websearch import SearchResult, register_web_capabilities

    hub = AgentHub(timeout_s=1)
    web = FakeWeb([SearchResult("Lasagne", "https://www.chefkoch.de/lasagne"),
                   SearchResult("Lasagne al forno", "https://www.lecker.de/lasagne")])
    register_web_capabilities(orchestrator.registry, web)
    turn = dialog(orchestrator, hub, web)
    assert turn("Such mir ein paar Links zu Lasagne").route == "fast_path"
    chosen = turn("den zweiten Link")
    # Der Nutzer hat selbst gewählt: keine Rückfrage, obwohl die Liste aus dem Web stammt
    assert chosen.actions[0].status == "succeeded"
    assert hub.sent[-1]["arguments"] == {"url": "https://www.lecker.de/lasagne"}


def test_unknown_names_without_article_open_the_website(orchestrator):
    from jarvis.websearch import SearchResult

    hub = AgentHub(timeout_s=1)
    turn = dialog(orchestrator, hub, FakeWeb([SearchResult("Chefkoch", "https://www.chefkoch.de/")]))
    result = turn("Öffne Chefkoch")  # kein Programm dieses Namens installiert
    assert result.actions[0].capability == "pc.open_link"
    assert hub.sent[-1]["arguments"] == {"url": "https://www.chefkoch.de/"}
    assert turn("Öffne Steam").actions[0].capability == "pc.open_app"


def test_open_url_accepts_only_web_addresses():
    registry = ToolRegistry()
    hub = AgentHub()
    register_pc_capabilities(registry, hub)
    with pytest.raises(JarvisError) as exc:
        asyncio.run(registry.get("pc.open_url").handler({"url": "file:///C:/Windows/System32/cmd.exe"}, CTX))
    assert exc.value.code == "JRV-VAL-002"


def test_guests_may_not_control_the_pc(orchestrator):
    register_pc_capabilities(orchestrator.registry, AgentHub())
    ctx = PolicyContext(tainted=False, allowed_domains=None, mode="normal", now=datetime(2026, 9, 27, 12, 0))
    record = asyncio.run(orchestrator.request_action(
        capability="pc.open_app", arguments={"app": "explorer"}, principal=GUEST, correlation_id="c",
        session_id="g", via="fast_path", ctx=ctx))
    assert record.status == "denied"


def test_open_url_needs_confirmation_after_untrusted_content(orchestrator):
    register_pc_capabilities(orchestrator.registry, AgentHub())
    ctx = PolicyContext(tainted=True, allowed_domains=None, mode="normal", now=datetime(2026, 9, 27, 12, 0))
    record = asyncio.run(orchestrator.request_action(
        capability="pc.open_url", arguments={"url": "https://example.org"}, principal=ALEX, correlation_id="c",
        session_id="t", via="llm", ctx=ctx))
    assert record.status == "pending_confirmation"


@pytest.fixture
def pc_client(orchestrator):
    testclient = pytest.importorskip("fastapi.testclient")
    from jarvis.api import Container, create_app
    from jarvis.context import Situation
    from jarvis.events import InMemoryEventBus
    from jarvis.llm.router import ModelRouter
    from jarvis.testing import ScriptedProvider, say

    hub = AgentHub(timeout_s=5)
    register_pc_capabilities(orchestrator.registry, hub)
    orchestrator.app_resolver = hub.find_app
    container = Container(
        orchestrator=orchestrator, bus=InMemoryEventBus(), router=ModelRouter(local=ScriptedProvider([say("-")]),
                                                                              cloud=None),
        tokens={"tok_alex": ALEX}, webhook_secrets={}, agents=hub,
        situation=lambda who, channel: Situation(now=datetime(2026, 9, 27, 12, 0), channel=channel),
    )
    return testclient.TestClient(create_app(container))


def final_of(chat):
    """Antwort abwarten; davor kommen Zwischenstände („action“ mit der Capability) für die Oberfläche."""
    while (message := chat.receive_json())["type"] == "status":
        assert message["phase"] in ("action", "tool", "research")
    return message


def test_voice_command_reaches_the_pc_agent(pc_client):
    with pc_client.websocket_connect("/v1/stream?token=tok_alex") as chat:
        chat.send_json({"type": "input.text", "text": "Öffne den Explorer", "session_id": "pc1"})
        final = final_of(chat)
        assert final["route"] == "fast_path" and "nicht verbunden" in final["text"]  # noch kein Agent

        with pc_client.websocket_connect("/v1/agent?token=tok_alex") as agent:
            agent.send_json({"type": "agent.hello", "name": "PC", "apps": ["explorer"], "folders": ["downloads"]})
            for _ in range(50):
                if pc_client.get("/v1/system/health").json()["pc_agent"] == "connected":
                    break
                time.sleep(0.02)
            chat.send_json({"type": "input.text", "text": "Öffne den Explorer", "session_id": "pc1"})
            invoke = agent.receive_json()
            assert invoke["type"] == "agent.invoke" and invoke["action"] == "open_app"
            assert invoke["arguments"] == {"app": "explorer"}
            agent.send_json({"type": "agent.result", "id": invoke["id"], "ok": True, "result": {"opened": "explorer"}})
            final = final_of(chat)
            assert final["text"] == "Erledigt." and final["actions"][0]["status"] == "succeeded"
    assert pc_client.get("/v1/system/health").json()["pc_agent"] == "disconnected"


def test_any_installed_program_reaches_the_pc_agent(pc_client):
    # „Kannst du Steam starten?“: Name aus dem Startmenü des PCs, ohne LLM
    with pc_client.websocket_connect("/v1/agent?token=tok_alex") as agent, \
            pc_client.websocket_connect("/v1/stream?token=tok_alex") as chat:
        agent.send_json({"type": "agent.hello", "name": "PC", "version": "2.1.0", "apps": ["explorer"],
                         "start_apps": ["Steam", "Discord"], "folders": ["downloads"],
                         "actions": ["open_url", "open_app", "open_folder", "search_files"]})
        for _ in range(50):
            if pc_client.get("/v1/system/health").json()["pc_agent"] == "connected":
                break
            time.sleep(0.02)
        chat.send_json({"type": "input.text", "text": "Kannst du Steam starten?", "session_id": "pc2"})
        invoke = agent.receive_json()
        assert invoke["action"] == "open_app" and invoke["arguments"] == {"app": "Steam"}
        agent.send_json({"type": "agent.result", "id": invoke["id"], "ok": True, "result": {"opened": "Steam"}})
        final = final_of(chat)
        assert final["route"] == "fast_path" and final["actions"][0]["status"] == "succeeded"

        chat.send_json({"type": "input.text", "text": "Such die Datei Rechnung", "session_id": "pc2"})
        invoke = agent.receive_json()
        assert invoke["action"] == "search_files" and invoke["arguments"] == {"query": "Rechnung"}
        agent.send_json({"type": "agent.result", "id": invoke["id"], "ok": True, "result": {"searched": "Rechnung"}})
        assert final_of(chat)["actions"][0]["capability"] == "pc.search_files"


def test_agent_needs_a_valid_token(pc_client):
    from starlette.websockets import WebSocketDisconnect

    with pc_client.websocket_connect("/v1/agent?token=falsch") as agent:
        with pytest.raises(WebSocketDisconnect) as closed:
            agent.receive_json()
    assert closed.value.code == 4401
