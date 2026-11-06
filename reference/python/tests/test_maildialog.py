"""E-Mail-Assistent: Empfänger, Betreff und Text Schritt für Schritt – Adressen erkennt JARVIS selbst."""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from jarvis.api import turn_to_json
from jarvis.context import Situation
from jarvis.fastpath import FastPath, conversation_intent
from jarvis.maildialog import MailDraft, advance, compose_body
from jarvis.orchestrator import TurnRequest
from jarvis.pc import AgentHub, register_pc_capabilities, resolve_recipient, spoken_email
from jarvis.policy import Principal
from jarvis.style import JarvisStyle
from jarvis.testing import ScriptedProvider, say

DANIEL = Principal(actor="user:owner", role="adult", trust="trusted_user", name="Daniel")
NOW = datetime(2026, 9, 29, 10, 0, tzinfo=ZoneInfo("Europe/Berlin"))
CONTACTS = {"mama": "mama@example.de"}
ACTIONS = ["open_url", "open_app", "open_folder", "search_files", "find_files", "open_file", "app_search",
           "close_app", "type_text", "press_key", "click", "compose_mail", "notify"]


@pytest.mark.parametrize("spoken, address", [
    # Daniels Protokoll: das kleine Modell machte daraus „Hegemann@punkt.daniel.at.gmx.de“
    ("Hegemann Punkt Daniel at GMX, Punkt. DE. Und.", "hegemann.daniel@gmx.de"),
    ("Hegemann Punkt Daniel at GMX, Punkt. DE.", "hegemann.daniel@gmx.de"),
    ("max punkt mustermann ät web punkt de bitte", "max.mustermann@web.de"),
    ("info minus shop at-zeichen firma punkt de", "info-shop@firma.de"),
    ("die adresse ist anna unterstrich b at gmail punkt com", "anna_b@gmail.com"),
    ("max.mustermann@gmx.de", "max.mustermann@gmx.de"),
    ("Mama", None), ("Hegemann Punkt Daniel", None), ("at gmx punkt de", None),
])
def test_spoken_addresses(spoken, address):
    assert spoken_email(spoken) == address or (address == spoken and resolve_recipient(spoken) == address)


def test_contacts_resolve_names_and_ignore_broken_entries():
    contacts = {"Mama": "mama@example.de", "x": "kaputt"}
    assert resolve_recipient("an Mama", contacts) == "mama@example.de"
    assert resolve_recipient("x", contacts) is None and resolve_recipient("Onkel Heinz", contacts) is None


@pytest.mark.parametrize("text, to", [
    ("Dir eine E-Mail.", ""), ("Schreib eine E-Mail", ""), ("Eine E-Mail verfassen", ""), ("Neue E-Mail", ""),
    ("Ich möchte eine E-Mail schreiben", ""), ("Kannst du eine E-Mail schreiben?", ""),
    ("Jarvis, schreib mir bitte eine Mail an Mama", "Mama"), ("E-Mail an Mama", "Mama"),
    ("Ich will eine Mail an Max schreiben", "Max"),
    ("Schreib eine E-Mail an Hegemann Punkt Daniel at GMX, Punkt. DE.", "Hegemann Punkt Daniel at GMX Punkt DE"),
])
def test_mail_wishes_start_the_assistant(text, to):
    match = FastPath({}, {}).match(text)
    assert (match.capability, match.grammar, match.slots["to"]) == ("pc.compose_mail", "mail_dialog", to)


@pytest.mark.parametrize("text", ["Hab ich neue E-Mails?", "Lies meine E-Mails", "E-Mail an Max, dass ich später komme"])
def test_other_mail_sentences_are_no_draft(text):
    match = FastPath({}, {}).match(text)
    assert match is None or match.grammar != "mail_dialog"


def test_complete_sentences_still_open_the_draft_directly():
    match = FastPath({}, {}).match("Schreib eine Mail an Mama mit dem Betreff Sonntag")
    assert (match.grammar, match.arguments) == ("compose_mail", {"to": "Mama", "subject": "Sonntag"})


@pytest.mark.parametrize("text", ["Kannst du mich nun besser verstehen?", "Hörst du mich?", "Verstehst du mich jetzt",
                                  "Können Sie mich hören?", "Test", "Mikrofontest"])
def test_hear_check(text):
    assert conversation_intent(text) == "hear_check"


@pytest.mark.parametrize("text", ["Was geht ab?", "Was geht", "Na?", "Wie siehts aus?"])
def test_small_talk_is_answered_without_the_model(text):
    """Lockere Begrüßung ohne Sprachmodell – das erfand dazu sonst Termine und Wetter."""
    assert conversation_intent(text) == "how_are_you"


def test_steps_without_the_orchestrator():
    draft = MailDraft()
    resolve = lambda text: resolve_recipient(text, CONTACTS)  # noqa: E731
    assert advance(draft, "Onkel Heinz", resolve).kind == "unknown_contact"
    assert advance(draft, "Mama", resolve).kind == "ask_subject" and draft.address == "mama@example.de"
    assert advance(draft, "nein", resolve).kind == "ask_to_again" and draft.address is None  # nach dem Vorlesen
    assert advance(draft, "an Mama", resolve).kind == "ask_subject"
    assert advance(draft, "kein Betreff", resolve).kind == "ask_body" and draft.subject == ""
    assert advance(draft, "der Betreff ist falsch", resolve).kind == "ask_subject_again"
    assert advance(draft, "Sonntag.", resolve).kind == "ask_body" and draft.subject == "Sonntag"
    assert advance(draft, "wir kommen um drei", resolve).kind == "open" and draft.body == "Wir kommen um drei."
    assert advance(MailDraft(), "abbrechen", resolve).kind == "cancel"
    assert advance(MailDraft(step="subject", address="a@b.de"), "fertig", resolve).kind == "open"


@pytest.mark.parametrize("body, expected", [
    ("Ich komme am Samstag.", "Hallo,\n\nIch komme am Samstag.\n\nViele Grüße\nDaniel"),
    ("Hallo Mama, ich komme am Samstag.", "Hallo Mama, ich komme am Samstag.\n\nViele Grüße\nDaniel"),
    ("Ich komme. Liebe Grüße, Daniel.", "Hallo,\n\nIch komme. Liebe Grüße, Daniel."),
    ("", ""),
])
def test_greeting_and_closing_are_added_once(body, expected):
    assert compose_body(body, "Daniel") == expected


def mail_turns(orchestrator, *, connected=True):
    """Orchestrator mit verbundenem PC; turn(text) -> TurnResult, hub.sent = was beim PC ankam."""
    hub = AgentHub(timeout_s=1)
    hub.sent = []

    async def send(message):
        hub.sent.append(message)
        hub.resolve({"id": message["id"], "ok": True, "result": {}})

    if connected:
        hub.attach(send, {"name": "PC", "apps": ["explorer"], "start_apps": [], "actions": ACTIONS})
        register_pc_capabilities(orchestrator.registry, hub, contacts=CONTACTS)
    orchestrator.recipient_resolver = lambda text: resolve_recipient(text, CONTACTS)
    orchestrator.style = JarvisStyle()
    situation = Situation(now=NOW, user_display="Daniel")

    def turn(text):
        request = TurnRequest(text=text, session_id="mail", principal=DANIEL)
        return asyncio.run(orchestrator.handle_turn(request, provider=ScriptedProvider([say("LLM")]),
                                                    situation=situation))
    return turn, hub


def test_daniels_log_with_the_assistant(orchestrator):
    turn, hub = mail_turns(orchestrator)
    first = turn("Dir eine E-Mail.")
    assert first.text.startswith("An wen soll die E-Mail gehen, Sir?") and first.awaiting_reply
    assert first.card == {"type": "mail", "to": "", "subject": "", "body": "", "step": "to"}
    heard = turn("Hegemann Punkt Daniel at GMX, Punkt. DE. Und.")
    assert heard.text == "An hegemann.daniel@gmx.de, Sir. Wie lautet der Betreff?"
    assert heard.card["to"] == "hegemann.daniel@gmx.de" and heard.card["step"] == "subject"
    again = turn("Die E-Mail ist falsch.")
    assert again.text.startswith("Verzeihung, Sir. Wie lautet die Adresse?")
    assert (again.card["step"], again.card["to"]) == ("to", "")  # die falsche Adresse ist weg
    assert turn("Hegemann Punkt Daniel at GMX, Punkt. DE.").card["to"] == "hegemann.daniel@gmx.de"
    subject = turn("Besuch am Samstag")
    assert subject.text == "Betreff: „Besuch am Samstag“. Was soll in der E-Mail stehen, Sir?"
    done = turn("ich komme gegen drei vorbei")
    assert done.text == ("Sehr wohl. Der E-Mail-Entwurf an hegemann.daniel@gmx.de ist geöffnet – Sie müssen ihn nur "
                         "noch absenden.")
    assert done.card["step"] == "done" and not done.awaiting_reply
    assert (hub.sent[-1]["action"], hub.sent[-1]["arguments"]) == ("compose_mail", {
        "to": "hegemann.daniel@gmx.de", "subject": "Besuch am Samstag",
        "body": "Hallo,\n\nIch komme gegen drei vorbei.\n\nViele Grüße\nDaniel"})
    assert len(hub.sent) == 1  # vorher nichts geöffnet – und nichts abgeschickt
    assert turn_to_json(done)["card"]["step"] == "done"


def test_contact_skip_subject_and_open_early(orchestrator):
    turn, hub = mail_turns(orchestrator)
    started = turn("Schreib eine Mail an Mama")
    assert started.text == "An Mama (mama@example.de), Sir. Wie lautet der Betreff?"
    assert started.card["to"] == "Mama <mama@example.de>"
    assert turn("ohne Betreff").text == "Ohne Betreff. Was soll in der E-Mail stehen, Sir?"
    assert turn("den Rest schreib ich selbst").card["step"] == "done"
    assert hub.sent[-1]["arguments"] == {"to": "mama@example.de"}


def test_repeating_the_address_corrects_it(orchestrator):
    turn, hub = mail_turns(orchestrator)
    turn("E-Mail an max punkt muster at gmx punkt de")
    assert turn("max punkt mustermann at gmx punkt de").text == "An max.mustermann@gmx.de, Sir. Wie lautet der Betreff?"


def test_unknown_contact_can_be_filled_in_later(orchestrator):
    turn, hub = mail_turns(orchestrator)
    assert turn("Schreib eine E-Mail an Onkel Heinz").text.startswith("„Onkel Heinz“ steht nicht in meinen Kontakten")
    assert turn("weiter").text == "An „Onkel Heinz“ – die Adresse tragen Sie im Entwurf ein, Sir. Wie lautet der Betreff?"
    turn("Grillen")
    done = turn("fertig")
    assert "Die Adresse von „Onkel Heinz“ kenne ich noch nicht" in done.text
    assert hub.sent[-1]["arguments"] == {"subject": "Grillen"}


def test_cancel_discards_everything(orchestrator):
    turn, hub = mail_turns(orchestrator)
    turn("Neue E-Mail")
    turn("an Mama")
    cancelled = turn("Abbrechen")
    assert cancelled.text == "Sehr wohl. Die E-Mail ist verworfen." and cancelled.card["step"] == "cancel"
    assert not cancelled.awaiting_reply and hub.sent == []
    assert orchestrator.sessions["mail"].mail is None


@pytest.mark.parametrize("text, answered", [
    ("Wie spät ist es?", "Es ist 10:00 Uhr, Sir."),
    ("Öffne den Explorer", "Sehr wohl. Der Datei-Explorer ist geöffnet."),
    ("Was ist ein Transistor", None),  # Wissensfrage: geht an Nachschlagen bzw. Sprachmodell
])
def test_a_different_wish_instead_of_a_recipient_ends_the_assistant(orchestrator, text, answered):
    turn, hub = mail_turns(orchestrator)
    turn("Schreib eine E-Mail")
    reply = turn(text)
    assert reply.card is None and orchestrator.sessions["mail"].mail is None
    assert reply.text == answered if answered else "Adresse" not in reply.text
    assert all(m["action"] != "compose_mail" for m in hub.sent)


def test_bad_address_is_read_back_and_asked_again(orchestrator):
    turn, hub = mail_turns(orchestrator)
    turn("Schreib eine E-Mail")
    reply = turn("hegemann punkt daniel at gmx")
    assert reply.text.startswith("Daraus wird keine gültige Adresse, Sir: „hegemann punkt daniel at gmx“.")
    assert reply.card["step"] == "to" and reply.awaiting_reply


def test_a_forgotten_draft_expires(orchestrator):
    turn, hub = mail_turns(orchestrator)
    turn("Schreib eine E-Mail")
    orchestrator.sessions["mail"].mail.updated -= 600
    assert turn("Hörst du mich?").text.startswith("Laut und deutlich")
    assert orchestrator.sessions["mail"].mail is None


def test_without_pc_the_assistant_does_not_start(orchestrator):
    turn, hub = mail_turns(orchestrator, connected=False)
    reply = turn("Schreib eine E-Mail")
    assert "PC-Steuerung ist nicht eingerichtet" in reply.text and reply.card is None
    assert orchestrator.sessions["mail"].mail is None
