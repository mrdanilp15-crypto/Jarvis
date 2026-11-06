"""Suchbefehle mit Dienst, Rückfragen, Programme schließen, Tasten, Klicken, Tippen, Mail-Entwürfe und Meldungen."""

import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from jarvis.errors import JarvisError
from jarvis.fastpath import SEARCH_SERVICES, FastPath
from jarvis.orchestrator import TurnRequest
from jarvis.pc import KEYS, SEARCH_SITES, AgentHub, register_pc_capabilities
from jarvis.tools import InvocationContext, ToolRegistry

from conftest import ALEX

CTX = InvocationContext(correlation_id="c1", actor="user:alex", session_id="s1")
NOW = datetime(2026, 9, 28, 14, 20, tzinfo=ZoneInfo("Europe/Berlin"))
ACTIONS = ["open_url", "open_app", "open_folder", "search_files", "find_files", "open_file", "app_search",
           "close_app", "type_text", "press_key", "click", "compose_mail", "notify"]


@pytest.mark.parametrize("text, capability, arguments", [
    # Die Frage von Daniel: der Dienst gehört nicht in den Suchbegriff
    ("Suche mir nach Arteriion auf Spotify", "pc.search_web", {"query": "Arteriion", "site": "spotify"}),
    ("Such auf Spotify nach Arteriion", "pc.search_web", {"query": "Arteriion", "site": "spotify"}),
    ("Kannst du auf Spotify nach Arteriion suchen?", "pc.search_web", {"query": "Arteriion", "site": "spotify"}),
    ("Ich möchte Arteriion auf Spotify hören", "pc.search_web", {"query": "Arteriion", "site": "spotify"}),
    ("Schau auf Netflix nach Dark", "pc.search_web", {"query": "Dark", "site": "netflix"}),
    ("Such nach Pizzeria auf Google Maps", "pc.search_web", {"query": "Pizzeria", "site": "maps"}),
    ("Such bei eBay Kleinanzeigen nach Fahrrad", "pc.search_web", {"query": "Fahrrad", "site": "kleinanzeigen"}),
    ("Such auf Google nach Pizza", "pc.search_web", {"query": "Pizza"}),
    ("Such nach Urlaub auf Mallorca", "pc.search_web", {"query": "Urlaub auf Mallorca"}),  # kein Dienst
    ("Such nach dem Wetter in Berlin", "pc.search_web", {"query": "Wetter in Berlin"}),
    # Programme schließen
    ("Schließ Steam", "pc.close_app", {"app": "steam"}),
    ("Kannst du Discord schließen?", "pc.close_app", {"app": "discord"}),
    ("Mach den Browser zu", "pc.close_app", {"app": "browser"}),
    ("Beende den Taschenrechner", "pc.close_app", {"app": "rechner"}),
    ("Schließ den Tab", "pc.press_key", {"key": "close_tab"}),
    ("Schließ dieses Fenster", "pc.press_key", {"key": "close_window"}),
    # Tasten, Kürzel, Medien
    ("Drück Enter", "pc.press_key", {"key": "enter"}),
    ("Drück zweimal Tab", "pc.press_key", {"key": "tab", "times": 2}),
    ("Drück mal die Leertaste", "pc.press_key", {"key": "space"}),
    ("Pause", "pc.press_key", {"key": "play_pause"}),
    ("Nächstes Lied", "pc.press_key", {"key": "next_track"}),
    ("Mach mal lauter", "pc.press_key", {"key": "volume_up", "times": 5}),
    ("Etwas leiser", "pc.press_key", {"key": "volume_down", "times": 3}),
    ("Ton aus", "pc.press_key", {"key": "mute"}),
    ("Mach das rückgängig", "pc.press_key", {"key": "undo"}),
    ("Öffne einen neuen Tab", "pc.press_key", {"key": "new_tab"}),
    ("Scroll runter", "pc.press_key", {"key": "page_down"}),
    # Klicken und Tippen
    ("Klick auf Anmelden", "pc.click", {"label": "Anmelden"}),
    ("Klick den Button Alle akzeptieren an", "pc.click", {"label": "Alle akzeptieren"}),
    ("Tippe Hallo Welt", "pc.type_text", {"text": "Hallo Welt"}),
    ("Tippe Pizza Berlin und drück Enter", "pc.type_text", {"text": "Pizza Berlin", "enter": True}),
    ("Schreib ins Suchfeld Arteriion", "pc.type_text", {"text": "Arteriion"}),
    ("Gib Pizza Berlin ein", "pc.type_text", {"text": "Pizza Berlin"}),
    # E-Mail-Entwurf, Anmelden
    ("Schreib eine Mail an Mama mit dem Betreff Sonntag", "pc.compose_mail", {"to": "Mama", "subject": "Sonntag"}),
    ("Schreib eine E-Mail", "pc.compose_mail", {}),
    ("Melde mich bei Netflix an", "pc.open_link", {"query": "Netflix anmelden"}),
    # Timer, Erinnerungen, Termine, Mails
    ("Stell einen Timer für eine halbe Stunde für die Pizza", "timer.start", {"duration_s": 1800, "label": "Pizza"}),
    ("Brich den Nudel-Timer ab", "timer.cancel", {"kind": "timer", "label": "Nudel"}),
    ("Erinnere mich in 10 Minuten an den Müll", "reminder.create",
     {"text": "den Müll", "at": "2026-09-28T14:30:00+02:00"}),
    ("Trag am 3. Oktober um 14 Uhr Oma besuchen ein", "calendar.add",
     {"title": "Oma besuchen", "start": "2026-10-03T14:00:00+02:00"}),
    ("Welche Termine habe ich diese Woche?", "calendar.list", {"day": "2026-09-28", "days": 7}),
    ("Sag den Friseur ab", "calendar.delete", {"title": "Friseur"}),
    ("Habe ich neue Mails?", "mail.list_unread", {}),
])
def test_commands(text, capability, arguments):
    match = FastPath({}, {}).match(text, now=NOW)
    assert match is not None and (match.capability, match.arguments) == (capability, arguments)


@pytest.mark.parametrize("text, grammar", [
    ("Such mal", "incomplete"), ("Such auf Spotify", "incomplete"), ("Öffne", "incomplete"),
    ("Erinnere mich an den Müll", "incomplete"), ("Trag Zahnarzt ein", "incomplete"),
    ("Tippe mein Passwort", "refuse_password"), ("Gib das Kennwort ein", "refuse_password"),
])
def test_follow_ups_and_refusals(text, grammar):
    assert FastPath({}, {}).match(text, now=NOW).grammar == grammar


@pytest.mark.parametrize("text", [
    "Schreib ein Gedicht über den Herbst",       # schreiben lassen, nicht tippen
    "Schreib eine Mail an Max dass ich später komme",  # Text formuliert das Sprachmodell
    "Schließ das Fenster",                       # Haus oder PC? Das klärt das Sprachmodell
    "Wie spät ist es?",
])
def test_ambiguous_requests_go_to_the_llm(text):
    assert FastPath({}, {}).match(text, now=NOW) is None


def test_every_spoken_service_has_a_search_address():
    assert set(SEARCH_SERVICES.values()) <= set(SEARCH_SITES)


def agent(start_apps=("Steam", "Spotify"), actions=ACTIONS, results=None):
    hub = AgentHub(timeout_s=1)
    hub.sent = []

    async def send(message):
        hub.sent.append(message)
        result = (results or {}).get(message["action"], {})
        hub.resolve({"id": message["id"], "ok": True, "result": result(message) if callable(result) else result})

    hub.attach(send, {"name": "PC", "apps": ["explorer"], "start_apps": list(start_apps), "actions": list(actions)})
    return hub


def run(registry, capability, arguments):
    assert not registry.get(capability).validate(arguments), arguments
    return asyncio.run(registry.get(capability).handler(arguments, CTX))


def test_spotify_search_uses_the_app_when_installed():
    registry, hub = ToolRegistry(), agent(results={"app_search": {"opened": True}})
    register_pc_capabilities(registry, hub)
    assert run(registry, "pc.search_web", {"query": "Arteriion", "site": "spotify"})["in_app"]
    assert [m["action"] for m in hub.sent] == ["app_search"]

    registry, hub = ToolRegistry(), agent(results={"app_search": {"opened": False}})  # nicht installiert
    register_pc_capabilities(registry, hub)
    run(registry, "pc.search_web", {"query": "Arteriion Live", "site": "spotify"})
    assert hub.sent[-1]["arguments"] == {"url": "https://open.spotify.com/search/Arteriion%20Live"}

    registry, hub = ToolRegistry(), agent(actions=ACTIONS[:4])  # älterer Agent: gleich im Browser
    register_pc_capabilities(registry, hub)
    run(registry, "pc.search_web", {"query": "Pizza & Pasta", "site": "maps"})
    assert hub.sent[-1]["arguments"] == {"url": "https://www.google.com/maps/search/Pizza%20%26%20Pasta"}


def test_close_type_keys_and_click_reach_the_agent():
    registry, hub = ToolRegistry(), agent()
    register_pc_capabilities(registry, hub)
    run(registry, "pc.close_app", {"app": "steem"})  # Hörfehler: das installierte Programm heißt „Steam“
    assert hub.sent[-1] == {**hub.sent[-1], "action": "close_app", "arguments": {"app": "Steam"}}
    run(registry, "pc.type_text", {"text": "Hallo", "enter": True})
    assert hub.sent[-1]["arguments"] == {"text": "Hallo", "enter": True}
    run(registry, "pc.press_key", {"key": "volume_up", "times": 5})
    assert hub.sent[-1]["arguments"] == {"key": "volume_up", "times": 5}
    run(registry, "pc.click", {"label": " Anmelden "})
    assert hub.sent[-1]["arguments"] == {"label": "Anmelden"}
    assert registry.get("pc.press_key").validate({"key": "format_c"})  # nur bekannte Tasten
    assert registry.get("pc.press_key").risk_for({"key": "close_window"}) == "R2"
    assert registry.get("pc.type_text").risk_class == "R2" and registry.get("pc.click").risk_class == "R2"
    assert {"enter", "play_pause", "volume_up", "close_tab"} <= set(KEYS)


@pytest.mark.parametrize("mode, to, expected_action, expected", [
    ("mailto", "max punkt mustermann at gmail punkt com", "compose_mail",
     {"to": "max.mustermann@gmail.com", "subject": "Hallo"}),
    ("mailto", "Mama", "compose_mail", {"to": "mama@example.de", "subject": "Hallo"}),  # Kontakt
    ("mailto", "Onkel Heinz", "compose_mail", {"subject": "Hallo"}),  # unbekannt: Adresse bleibt leer
    ("gmail", "Mama", "open_url",
     {"url": "https://mail.google.com/mail/?view=cm&fs=1&to=mama%40example.de&su=Hallo&body="}),
])
def test_mail_drafts(mode, to, expected_action, expected):
    registry, hub = ToolRegistry(), agent()
    register_pc_capabilities(registry, hub, mail_compose=mode, contacts={"mama": "mama@example.de", "x": "kaputt"})
    result = run(registry, "pc.compose_mail", {"to": to, "subject": "Hallo"})
    assert (hub.sent[-1]["action"], hub.sent[-1]["arguments"]) == (expected_action, expected)
    assert result["draft"] and result["unknown_recipient"] == ("Onkel Heinz" if to == "Onkel Heinz" else None)


def test_notify_is_quiet_without_agent():
    assert asyncio.run(AgentHub().notify("JARVIS", "Timer")) is False
    hub = agent()
    assert asyncio.run(hub.notify("JARVIS", "Timer")) is True and hub.sent[-1]["action"] == "notify"


def test_outdated_agent_gets_a_clear_message():
    registry = ToolRegistry()
    register_pc_capabilities(registry, agent(actions=ACTIONS[:6]))
    with pytest.raises(JarvisError) as exc:
        run(registry, "pc.close_app", {"app": "Steam"})
    assert "veraltet" in exc.value.user_message


def test_follow_up_question_completes_the_command(orchestrator):
    from jarvis.context import Situation
    from jarvis.testing import ScriptedProvider, say

    hub = agent(results={"app_search": {"opened": True}})
    register_pc_capabilities(orchestrator.registry, hub)
    situation = Situation(now=NOW)

    def turn(text):
        request = TurnRequest(text=text, session_id="f", principal=ALEX)
        return asyncio.run(orchestrator.handle_turn(request, provider=ScriptedProvider([say("-")]),
                                                    situation=situation))

    asked = turn("Such mal")
    assert asked.text == "Wonach soll ich suchen?" and asked.awaiting_reply  # Oberfläche hört direkt zu
    done = turn("Arteriion auf Spotify")
    assert done.actions[0].capability == "pc.search_web"
    assert hub.sent[-1]["arguments"] == {"app": "spotify", "query": "Arteriion"} and not done.awaiting_reply
    assert turn("Tippe mein Passwort").actions == []


def test_timer_message_reaches_the_open_window(orchestrator):
    testclient = pytest.importorskip("fastapi.testclient")
    from jarvis.api import Container, create_app
    from jarvis.context import Situation
    from jarvis.events import InMemoryEventBus
    from jarvis.llm.router import ModelRouter
    from jarvis.testing import ScriptedProvider, say
    from jarvis.timers import Notifier

    notifier = Notifier()
    container = Container(
        orchestrator=orchestrator, bus=InMemoryEventBus(), notifier=notifier, tokens={"tok_alex": ALEX},
        router=ModelRouter(local=ScriptedProvider([say("-")]), cloud=None), webhook_secrets={},
        situation=lambda who, channel: Situation(now=NOW, channel=channel))
    client = testclient.TestClient(create_app(container))
    asyncio.run(notifier.send("user:alex", {"type": "notification", "kind": "timer", "text": "verpasst"}))
    with client.websocket_connect("/v1/stream?token=tok_alex") as ws:
        assert ws.receive_json()["text"] == "verpasst"  # lag bereit, weil kein Fenster offen war
        for _ in range(50):
            if notifier.listeners.get("user:alex"):
                break
            time.sleep(0.02)
        assert notifier.listeners["user:alex"]  # sonst würde receive_json unten ewig warten
        ws.portal.call(notifier.send, "user:alex", {"type": "notification", "kind": "timer", "text": "live"})
        assert ws.receive_json() == {"type": "notification", "kind": "timer", "text": "live"}
    assert notifier.listeners["user:alex"] == []
