"""Stil-Engine v2: Test-Suite „Eingabe -> erwartete Jarvis-Antwort“ und Filter-Regeln.

Die Antworten laufen durch die echte Pipeline (Intent-Erkennung, Kontext, Policy, Formatter) mit der
Persona aus config/persona.jarvis.yaml.
"""

import asyncio
from datetime import datetime

import pytest

from jarvis.connectors.homeassistant import register_home_capabilities
from jarvis.context import ContextBuilder, Situation
from jarvis.demo import initial_states
from jarvis.fastpath import FastPath
from jarvis.orchestrator import ConfirmationStore, MemoryAuditSink, Orchestrator, TurnRequest
from jarvis.pc import DEFAULT_APPS, AgentHub, register_pc_capabilities
from jarvis.persona import Persona
from jarvis.policy import PolicyEngine, Principal
from jarvis.skills import register_assistant_capabilities
from jarvis.style import STYLE_VERSION, JarvisStyle, PlainStyle
from jarvis.testing import FakeHome, ScriptedProvider, call_tool, say
from jarvis.tools import ToolRegistry
from jarvis.websearch import SearchResult, register_web_capabilities

from conftest import REPO

DANIEL = Principal(actor="user:owner", role="adult", trust="trusted_user", area="wohnzimmer", name="Daniel")
SITUATION = Situation(now=datetime(2026, 9, 27, 19, 42), user_display="Daniel", area="wohnzimmer",
                      location="Berlin")


async def fake_weather(args, ctx):
    return {"location": "Berlin, Deutschland", "forecast": [
        {"date": "2026-09-27", "conditions": "bedeckt", "temp_max_c": 16.1, "temp_min_c": 8.2,
         "precipitation_probability_pct": 10},
        {"date": "2026-09-28", "conditions": "leichter Regen", "temp_max_c": 13.4, "temp_min_c": 9.0,
         "precipitation_probability_pct": 80},
    ]}


AGENT_ACTIONS = ("open_url", "open_app", "open_folder", "search_files", "find_files", "open_file")
FILES = [  # neueste zuerst, wie der Agent sie liefert
    {"id": 1, "name": "Bewerbung.pdf", "folder": "Desktop", "kind": "file", "modified": "2026-09-27"},
    {"id": 2, "name": "Bewerbung_Bosch.docx", "folder": "Documents\\Bewerbungen", "kind": "file",
     "modified": "2026-09-20"},
    {"id": 3, "name": "Bewerbungen", "folder": "Documents", "kind": "folder", "modified": "2026-09-01"},
]


def fake_agent(message):
    action, args = message["action"], message["arguments"]
    if action == "open_app":
        return {"opened": args["app"]}
    if action in ("find_files", "search_files"):
        hits = [f for f in FILES if args["query"].lower() in f["name"].lower()]
        return {"query": args["query"], "total": len(hits), "results": hits}
    if action == "open_file":
        item = FILES[args["id"] - 1] if "id" in args else FILES[0]
        return {"opened": item["name"], "folder": item["folder"], "kind": item["kind"], "shown": False}
    return {}


class FakeWeb:
    async def search(self, query, *, limit=5):
        return [SearchResult("Lasagne – das klassische Rezept", "https://www.chefkoch.de/rezepte/lasagne", "…"),
                SearchResult("Lasagne al forno", "https://www.lecker.de/lasagne", "…")][:limit]


def make_orchestrator(*, components=None, fast_path=None, pc_connected=True, home=True):
    registry = ToolRegistry()
    if home:
        register_home_capabilities(registry, FakeHome(initial_states()))
    hub = AgentHub()
    if pc_connected:
        async def send(message):
            # Wie der echte Agent: meldet zurück, was tatsächlich gestartet bzw. gefunden wurde
            hub.opened.append(message)
            hub.resolve({"id": message["id"], "ok": True, "result": fake_agent(message)})

        hub.opened = []
        hub.attach(send, {"name": "PC", "apps": DEFAULT_APPS, "actions": list(AGENT_ACTIONS),
                          "start_apps": ["Steam", "Minecraft Launcher", "Google Chrome", "Discord"]})
    register_pc_capabilities(registry, hub, web=FakeWeb())
    register_web_capabilities(registry, FakeWeb())
    state = components or {"sprachmodell": "ok", "pc_steuerung": "ok"}
    register_assistant_capabilities(registry, probes=lambda: dict(state), weather=fake_weather)
    return Orchestrator(
        registry=registry, policy=PolicyEngine.from_file(REPO / "config" / "policies.yaml"),
        context=ContextBuilder(persona=Persona.load(REPO / "config" / "persona.jarvis.yaml")),
        audit=MemoryAuditSink(), confirmations=ConfirmationStore(),
        fast_path=fast_path or FastPath({"wohnzimmer": ["light.wohnzimmer_stehlampe"], "kueche": ["light.kueche"]},
                                        {"küche": "kueche"}),
        app_resolver=hub.find_app,
    )


def ask(orchestrator, text, *, llm=None, session="s", situation=SITUATION, on_text=None, mode="normal"):
    request = TurnRequest(text=text, session_id=session, principal=DANIEL, mode=mode)
    return asyncio.run(orchestrator.handle_turn(request, provider=llm or ScriptedProvider([say("-")]),
                                                situation=situation, on_text=on_text))


# ---------------------------------------------------------------------------------------------------------
# Test-Suite: Eingabe -> erwartete Jarvis-Antwort
# ---------------------------------------------------------------------------------------------------------
EXPECTED = [
    # Vorgaben
    ("Mach das Licht an.", "Selbstverständlich, Sir. Das Licht ist nun eingeschaltet."),
    ("Status?", "Analyse abgeschlossen. Alle Systeme laufen stabil."),
    # Ehrlich statt „Ich habe Ihren morgigen Tag optimiert.“: ohne Kalender gibt es nichts zu optimieren
    ("Plan für morgen?", "Sehr wohl. Für morgen liegen mir keine Termine vor – ein Kalender ist noch nicht "
                         "verbunden. Das Wetter in Berlin, Deutschland: leichter Regen, 9 bis 13,4 Grad, "
                         "Regenwahrscheinlichkeit 80 Prozent. Ein Schirm wäre ratsam."),
    ("Kannst du mir helfen?", "Natürlich, Sir. Ich stehe bereit."),
    # Erweiterungen
    ("Mach das Licht in der Küche aus", "Selbstverständlich, Sir. Das Licht in der Küche ist nun ausgeschaltet."),
    ("Danke!", "Stets zu Diensten, Sir."),
    ("Wie heiße ich?", "Sie sind Daniel, Sir."),
    ("Wie spät ist es?", "Es ist 19:42 Uhr, Sir."),
    ("Welcher Tag ist heute?", "Heute ist Sonntag, der 27. September 2026."),
    ("Guten Abend", "Guten Abend, Sir. Womit kann ich dienen?"),
    ("Wie geht es dir?", "Danke der Nachfrage, Sir. Die Systeme laufen stabil."),
    ("Wer bist du?", "Ich bin J.A.R.V.I.S., Ihr persönlicher Assistent. Stets zu Diensten, Sir."),
    ("Jarvis, öffne den Explorer", "Sehr wohl. Der Datei-Explorer ist geöffnet."),
    ("Öffne meine Downloads", "Sehr wohl. Der Ordner „Downloads“ ist geöffnet."),
    ("Öffne YouTube", "Sehr wohl. YouTube ist geöffnet."),
    ("Such im Internet nach Kürbissuppe", "Sehr wohl. Die Suche nach „Kürbissuppe“ ist geöffnet."),
    ("Kannst du Steam starten?", "Sehr wohl. Steam ist geöffnet."),
    ("Ich möchte Minecraft spielen", "Sehr wohl. Minecraft Launcher ist geöffnet."),
    ("Kannst du mir Katzenvideos auf YouTube zeigen?",
     "Sehr wohl. Die YouTube-Suche nach „Katzenvideos“ ist geöffnet."),
    ("Such die Datei Steuererklärung", "Sehr wohl. Die Dateisuche nach „Steuererklärung“ ist geöffnet."),
    ("Was kannst du?", "Ich kann Programme und Spiele auf Ihrem PC starten, Ordner und Webseiten öffnen, Links "
                       "heraussuchen und direkt öffnen, Dateien finden und öffnen, Licht, Heizung und Geräte im Haus "
                       "steuern und Ihnen den Systemstatus melden. Sagen Sie einfach, was Sie benötigen, Sir."),
    ("Such mir einen Link zu Lasagne und öffne ihn",
     "Sehr wohl. „Lasagne – das klassische Rezept“ auf chefkoch.de ist geöffnet."),
    ("Öffne Chefkoch", "Sehr wohl. „Lasagne – das klassische Rezept“ auf chefkoch.de ist geöffnet."),
    ("Öffne die Datei Bewerbung", "Sehr wohl. „Bewerbung.pdf“ aus dem Ordner Desktop ist geöffnet."),
    ("Wo ist meine Bewerbung?", "Ich habe 3 Treffer gefunden, die neuesten: „Bewerbung.pdf“ in Desktop, "
                                "„Bewerbung_Bosch.docx“ in Bewerbungen und „Bewerbungen“ in Dokumente. Welchen soll "
                                "ich öffnen – den ersten, zweiten oder dritten?"),
    ("Such mir ein paar Links zu Lasagne", "Die besten Treffer zu „Lasagne“: „Lasagne – das klassische Rezept“ auf "
                                           "chefkoch.de und „Lasagne al forno“ auf lecker.de. Welchen soll ich öffnen – "
                                           "den ersten oder den zweiten?"),
    ("Stell einen Timer auf 5 Minuten", "Verzeihung, Sir. Timer stehen mir derzeit nicht zur Verfügung."),
    ("Gute Nacht", "Gute Nacht, Sir."),
    ("Tschüss", "Sehr wohl. Ich bleibe in Bereitschaft."),
]


@pytest.mark.parametrize("text, expected", EXPECTED, ids=[t for t, _ in EXPECTED])
def test_jarvis_answers(text, expected):
    assert ask(make_orchestrator(), text).text == expected


@pytest.mark.parametrize("expected", [e for _, e in EXPECTED])
def test_formatter_keeps_jarvis_sentences_unchanged(expected):
    assert JarvisStyle().finalize(expected) == expected  # der Formatter ist für fertige Jarvis-Sätze idempotent


def test_status_reports_real_problems():
    degraded = make_orchestrator(components={"sprachmodell": "ok", "pc_steuerung": "disconnected"})
    assert ask(degraded, "Status?").text == ("Analyse abgeschlossen. Die Kernsysteme laufen stabil. "
                                             "Hinweis: Die PC-Steuerung ist nicht verbunden.")
    loading = make_orchestrator(components={"sprachmodell": "loading"})
    assert ask(loading, "Systemstatus").text == ("Analyse abgeschlossen. Das Sprachmodell wird noch geladen. "
                                                 "Ich überwache die Situation.")


def test_nothing_is_claimed_without_a_connection():
    no_home = make_orchestrator(fast_path=FastPath({}, {}), home=False)
    assert ask(no_home, "Mach das Licht an.").text == (
        "Verzeihung, Sir. Für das Haus ist noch keine Steuerung verbunden – dafür wird Home Assistant benötigt.")
    no_pc = make_orchestrator(pc_connected=False)
    assert ask(no_pc, "Öffne den Explorer").text.startswith("Verzeihung, Sir. Die PC-Steuerung ist nicht verbunden.")


def test_unknown_name_is_not_invented():
    anonymous = Situation(now=datetime(2026, 9, 27, 9, 0), user_display=None)
    answer = ask(make_orchestrator(), "Wie heiße ich?", situation=anonymous).text
    assert answer.startswith("Ihren Namen kenne ich noch nicht, Sir.") and "Alex" not in answer


def test_confirmation_dialogue_in_jarvis_tone():
    orchestrator = make_orchestrator()
    tool = call_tool("home__set_climate", {"entity_id": "climate.wohnzimmer", "temperature": 23})
    first = ask(orchestrator, "Mach es wärmer", llm=ScriptedProvider([tool, say("Das erfordert Ihre Zustimmung.")]),
                mode="away")  # abwesend: R2 nur mit Bestätigung
    assert first.pending_confirmation.prompt == "Soll ich die Heizung auf 23 Grad stellen? Ein Ja genügt, Sir."
    assert ask(orchestrator, "Ja").text == "Wie Sie wünschen. Die Heizung ist auf 23 Grad eingestellt."
    ask(orchestrator, "Mach es wärmer", llm=ScriptedProvider([tool, say("Bitte bestätigen.")]), mode="away",
        session="s2")
    assert ask(orchestrator, "Nein", session="s2").text == "Wie Sie wünschen. Ich habe den Vorgang abgebrochen."


def test_free_llm_text_is_filtered_and_streamed_in_style():
    streamed = []

    async def on_text(delta):
        streamed.append(delta)

    llm = ScriptedProvider([say("Okay, klar! Das mach ich gern für dich 😊 Hast du noch Fragen, Sir? "
                                "Sorry, Sir, das Wetter ist mega gut!")])
    result = ask(make_orchestrator(), "Erzähl mir was", llm=llm, on_text=on_text)
    text = result.text
    assert result.route.startswith("llm") and "Haben Sie noch Fragen, Sir?" in text
    for forbidden in ("Okay", "klar!", "dich", "du ", "Sorry", "mega", "😊", "!"):
        assert forbidden not in text, forbidden
    assert text.count("Sir") == 1  # Anrede höchstens einmal
    assert "".join(streamed).strip() == text  # gestreamte Sätze = Endfassung (Stimme spricht dasselbe)


@pytest.mark.parametrize("raw, expected", [
    ("Hey! Klar kann ich dir helfen. Was brauchst du?", "Selbstverständlich kann ich Ihnen helfen. Was brauchen Sie?"),
    ("Sorry, das hat nicht geklappt. Hast du das Kabel geprüft?",
     "Verzeihung, das hat nicht funktioniert. Haben Sie das Kabel geprüft?"),
    ("Als KI-Sprachmodell kann ich das nicht fühlen. Ich hoffe, das hilft dir.", "Ich kann das nicht fühlen."),
    ("Nutze `git pull`! Dann läuft es.", "Nutzen Sie `git pull`. Dann läuft es."),
    ("Das kostet ca. drei Euro, z. B. bei youtube.com.", "Das kostet ca. drei Euro, z. B. bei youtube.com."),
    ("Hier:\n```python\nprint('Hallo du!')\n```\nViel Spaß!", "Hier:\n```python\nprint('Hallo du!')\n```\nViel Spaß."),
    ("- öffne den Browser\n- klick auf Einstellungen", "- Öffnen Sie den Browser\n- Klicken Sie auf Einstellungen"),
    ("Das ist echt total easy.", "Das ist wirklich ausgesprochen mühelos."),  # Ersetzungen aus der Persona
])
def test_free_text_filter(raw, expected):
    persona = Persona.load(REPO / "config" / "persona.jarvis.yaml")
    assert JarvisStyle.from_persona(persona).finalize(raw) == expected


def test_error_messages_are_styled():
    style = JarvisStyle()
    assert style.error_message("Der Dienst ist gerade nicht erreichbar.") == (
        "Verzeihung, Sir. Der Dienst ist gerade nicht erreichbar.")
    assert style.error_message("Verzeihung, Sir. Schon höflich.") == "Verzeihung, Sir. Schon höflich."


def test_persona_selects_style_and_version():
    jarvis = Persona.load(REPO / "config" / "persona.jarvis.yaml", schema_path=REPO / "schemas" / "persona.schema.json")
    neutral = Persona.load(REPO / "config" / "persona.neutral.yaml")
    assert isinstance(JarvisStyle.from_persona(jarvis), JarvisStyle)
    assert jarvis.config["version"] == STYLE_VERSION == "2.2.0"
    assert type(JarvisStyle.from_persona(neutral)) is PlainStyle


def test_light_without_room_mapping_goes_to_the_llm():
    # Haus verbunden, aber keine Raumzuordnung: kein Validierungsfehler, das LLM klärt die Rückfrage
    orchestrator = make_orchestrator(fast_path=FastPath({}, {}))
    result = ask(orchestrator, "Mach das Licht an.", llm=ScriptedProvider([say("Welches Licht meinen Sie, Sir?")]))
    assert result.route.startswith("llm") and result.text == "Welches Licht meinen Sie, Sir?"
