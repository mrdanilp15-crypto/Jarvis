"""E-Mails lesen per IMAP (optional). Schreiben läuft als Entwurf über den PC (pc.compose_mail) – JARVIS versendet
nie selbst.

Zugang über deploy/.env: ``JARVIS_MAIL_IMAP_HOST``, ``JARVIS_MAIL_USER``, ``JARVIS_MAIL_PASSWORD`` – bei Gmail, GMX,
Web.de u. a. ein App-Passwort. Gelesen wird schonend (``BODY.PEEK``): Nachrichten bleiben ungelesen markiert.
Absender, Betreff und Text sind Fremdinhalte (``untrusted``) – danach verlangen R2-Aktionen eine Bestätigung.
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import html
import imaplib
import re
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

from .errors import JarvisError
from .tools import Capability, InvocationContext, ToolRegistry


@dataclass
class MailConfig:
    host: str
    user: str
    password: str
    port: int = 993
    folder: str = "INBOX"


def _sender(message: email.message.EmailMessage) -> str:
    pairs = getaddresses([str(message.get("From", ""))])
    if not pairs:
        return "unbekannt"
    name, address = pairs[0]
    return name.strip() or address


def _date(message: email.message.EmailMessage) -> str | None:
    try:
        return parsedate_to_datetime(str(message.get("Date"))).isoformat(timespec="minutes")
    except (TypeError, ValueError):
        return None


def plain_text(message: email.message.EmailMessage, limit: int = 2500) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content().replace("\r\n", "\n").replace("\r", "\n")
    if part.get_content_type() == "text/html":
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"(?m)^>.*$", "", text)  # zitierte Antworten weglassen
    text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class MailReader:
    def __init__(self, config: MailConfig, *, connect: Callable[[str, int], Any] | None = None) -> None:
        self.config = config
        self._connect = connect or (lambda host, port: imaplib.IMAP4_SSL(host, port, timeout=15))

    def _session(self) -> Any:
        try:
            imap = self._connect(self.config.host, self.config.port)
            imap.login(self.config.user, self.config.password)
            imap.select(self.config.folder, readonly=True)
            return imap
        except (imaplib.IMAP4.error, OSError) as exc:
            raise JarvisError("JRV-INT-001", f"IMAP: {exc}",
                              user_message="Das Postfach ist gerade nicht erreichbar – stimmen Server und "
                                           "App-Passwort?") from exc

    def _unread(self, count: int) -> dict[str, Any]:
        imap = self._session()
        try:
            status, data = imap.uid("SEARCH", None, "UNSEEN")
            uids = (data[0] or b"").split() if status == "OK" else []
            results = []
            for uid in reversed(uids[-count:]):  # neueste zuerst
                status, parts = imap.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                raw = next((p[1] for p in parts if isinstance(p, tuple)), b"")
                message = email.message_from_bytes(raw, policy=email.policy.default)
                results.append({"id": uid.decode(), "from": _sender(message),
                                "subject": str(message.get("Subject", "")).strip() or "(ohne Betreff)",
                                "date": _date(message)})
            return {"total": len(uids), "results": results}
        finally:
            imap.logout()

    def _read(self, uid: str) -> dict[str, Any]:
        imap = self._session()
        try:
            status, parts = imap.uid("FETCH", uid.encode(), "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None) if status == "OK" else None
            if raw is None:
                raise JarvisError("JRV-NFD-001", f"Mail {uid} nicht gefunden",
                                  user_message="Diese E-Mail finde ich nicht mehr.")
            message = email.message_from_bytes(raw, policy=email.policy.default)
            return {"id": uid, "from": _sender(message), "subject": str(message.get("Subject", "")).strip(),
                    "date": _date(message), "text": plain_text(message)}
        finally:
            imap.logout()

    async def unread(self, count: int = 5) -> dict[str, Any]:
        return await asyncio.to_thread(self._unread, count)

    async def read(self, uid: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._read, uid)


def register_mail_capabilities(registry: ToolRegistry, reader: MailReader) -> None:
    async def unread(args: dict[str, Any], ctx: InvocationContext) -> Any:
        return await reader.unread(int(args.get("count", 5)))

    async def read(args: dict[str, Any], ctx: InvocationContext) -> Any:
        return await reader.read(str(args["id"]))

    registry.register(Capability(
        name="mail.list_unread", domain="mail", risk_class="R1", output_trust="untrusted", timeout_s=25.0,
        description="Nennt die neuesten ungelesenen E-Mails (Absender, Betreff, Datum, id), ohne sie als gelesen "
                    "zu markieren.",
        input_schema={"type": "object", "additionalProperties": False, "properties": {
            "count": {"type": "integer", "minimum": 1, "maximum": 20},
        }},
        handler=unread,
    ))
    registry.register(Capability(
        name="mail.read", domain="mail", risk_class="R1", output_trust="untrusted", timeout_s=25.0,
        description="Liest eine E-Mail (id aus mail.list_unread) vor: Absender, Betreff und Text.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["id"], "properties": {
            "id": {"type": "string", "pattern": "^\\d{1,12}$"},
        }},
        handler=read,
    ))
