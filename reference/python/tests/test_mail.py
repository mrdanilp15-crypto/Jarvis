"""E-Mails lesen per IMAP – gegen ein nachgebautes Postfach."""

import asyncio
import imaplib

import pytest

from jarvis.errors import JarvisError
from jarvis.mail import MailConfig, MailReader, register_mail_capabilities
from jarvis.style import JarvisStyle
from jarvis.tools import InvocationContext, ToolRegistry

CTX = InvocationContext(correlation_id="c1", actor="user:owner", session_id="s1")
MAILS = {
    b"41": (b"From: Amazon <versand@amazon.de>\r\nSubject: =?utf-8?q?Ihre_Bestellung_ist_unterwegs?=\r\n"
            b"Date: Mon, 28 Sep 2026 08:15:00 +0200\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
            b"<html><style>p{}</style><p>Hallo Daniel,</p><p>dein Paket kommt <b>morgen</b>.</p></html>"),
    b"42": (b"From: Max Mustermann <max@example.de>\r\nSubject: Treffen morgen\r\n"
            b"Date: Mon, 28 Sep 2026 09:30:00 +0200\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Hi Daniel,\r\nhast du morgen um 18 Uhr Zeit?\r\n\r\n> alte Nachricht\r\nMax"),
}


class FakeImap:
    def __init__(self, host, port):
        self.selected, self.logged_out = None, False
        FakeImap.last = self

    def login(self, user, password):
        if password != "app-passwort":
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    def select(self, folder, readonly=False):
        self.selected = (folder, readonly)

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [b" ".join(MAILS)]
        uid, what = args
        raw = MAILS.get(uid if isinstance(uid, bytes) else uid.encode())
        if raw is None:
            return "OK", [None]
        if "HEADER.FIELDS" in what:
            raw = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
        assert "PEEK" in what  # nie als gelesen markieren
        return "OK", [(b"1 (UID " + uid + b" BODY[] {1}", raw), b")"]

    def logout(self):
        self.logged_out = True


def reader(password="app-passwort"):
    return MailReader(MailConfig("imap.example.de", "daniel", password), connect=FakeImap)


def test_unread_newest_first_and_read_only():
    result = asyncio.run(reader().unread(5))
    assert result["total"] == 2
    assert [(m["id"], m["from"], m["subject"]) for m in result["results"]] == [
        ("42", "Max Mustermann", "Treffen morgen"), ("41", "Amazon", "Ihre Bestellung ist unterwegs")]
    assert FakeImap.last.selected == ("INBOX", True) and FakeImap.last.logged_out


def test_read_plain_and_html():
    plain = asyncio.run(reader().read("42"))
    assert plain["text"] == "Hi Daniel,\nhast du morgen um 18 Uhr Zeit?\nMax"  # Zitat entfernt
    html = asyncio.run(reader().read("41"))
    assert html["text"] == "Hallo Daniel, dein Paket kommt morgen ."
    with pytest.raises(JarvisError) as missing:
        asyncio.run(reader().read("99"))
    assert missing.value.code == "JRV-NFD-001"


def test_wrong_password_is_explained():
    with pytest.raises(JarvisError) as exc:
        asyncio.run(reader("falsch").unread())
    assert "App-Passwort" in exc.value.user_message


def test_capabilities_are_untrusted_and_replies_keep_the_mail_text():
    registry = ToolRegistry()
    register_mail_capabilities(registry, reader())
    assert registry.get("mail.read").output_trust == "untrusted"
    listing = asyncio.run(registry.get("mail.list_unread").handler({}, CTX))
    mail = asyncio.run(registry.get("mail.read").handler({"id": "42"}, CTX))

    class Record:
        status = "succeeded"

    style = JarvisStyle()
    record = Record()
    record.capability, record.arguments, record.result = "mail.list_unread", {}, listing
    assert style.finalize(style.action_reply(record)) == (
        "Sie haben 2 ungelesene E-Mails. Die neuesten: von Max Mustermann: „Treffen morgen“ und von Amazon: "
        "„Ihre Bestellung ist unterwegs“. Welche soll ich vorlesen – die erste oder die zweite?")
    record.capability, record.arguments, record.result = "mail.read", {"id": "42"}, mail
    # Der Mailtext bleibt wörtlich – kein „Sie“ statt „du“, kein Umformulieren
    assert style.finalize(style.action_reply(record)) == (
        "E-Mail von Max Mustermann, Betreff „Treffen morgen“: Hi Daniel,\nhast du morgen um 18 Uhr Zeit?\nMax")
