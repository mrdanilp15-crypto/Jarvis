"""E-Mail-Assistent: Empfänger, Betreff und Text Schritt für Schritt – ohne Sprachmodell.

Kleine Modelle setzen diktierte Adressen falsch zusammen („Hegemann@punkt.daniel.at.gmx.de“). Der Assistent fragt
nacheinander, erkennt die Adresse selbst (``pc.spoken_email``), liest sie vor und versteht Korrekturen („Die Adresse
ist falsch“). Am Ende öffnet er den Entwurf – abschicken muss der Nutzer selbst.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .pc import spoken_email

TIMEOUT_S = 180  # nach drei Minuten Pause gilt der Entwurf als vergessen

_CANCEL = re.compile(r"(?:abbrechen|abbruch|vergiss es|vergiss (?:die|das) (?:e-?mail|mail)|stopp?|lass es|"
                     r"keine (?:e-?mail|mail)|doch nicht|nicht mehr)", re.I)
_OPEN_NOW = re.compile(r"(?:öffnen|öffne (?:ihn|sie|es|den entwurf)|mach (?:ihn|sie|es) auf|fertig|das (?:war'?s|wars|ist "
                       r"alles)|den rest schreib(?:e)? ich(?: selbst)?|schreib(?:e)? ich selbst)", re.I)
_SKIP = re.compile(r"(?:ohne(?: betreff| text| inhalt)?|kein(?:en)? (?:betreff|text|inhalt)|egal|weiter|überspringen|"
                   r"lass (?:es |das )?leer|leer lassen|nichts|leer)", re.I)
_WRONG_TO = re.compile(r"(?:(?:die |der )?(?:e-?mail-?adresse|adresse|e-?mail|empfänger)\s+(?:ist|war|stimmt)\s+"
                       r"(?:nicht|falsch|so nicht)|falsche (?:adresse|e-?mail)|anderer empfänger)", re.I)
_WRONG_SUBJECT = re.compile(r"(?:(?:der )?betreff\s+(?:ist|war|stimmt)\s+(?:nicht|falsch)|falscher betreff)", re.I)
_NO = re.compile(r"(?:nein|ne|nee|falsch|stimmt nicht|das ist falsch|nein,? falsch)", re.I)
_ADDRESSY = re.compile(r"@|\b(?:at|ät|punkt|dot)\b", re.I)
_GREETING = re.compile(r"^(?:hallo|hi|hey|liebe[rs]?|sehr geehrte[rs]?|guten (?:tag|morgen|abend)|moin|servus)\b", re.I)
_CLOSING = re.compile(r"\b(?:grüße|gruß|tschüss|bis bald|bis dann|lg|mfg)\b", re.I)


@dataclass
class MailDraft:
    to: str = ""  # wie genannt („Mama“, „hegemann punkt daniel at gmx punkt de“)
    address: str | None = None  # erkannte Adresse
    subject: str | None = None  # None = noch nicht gefragt, "" = ohne Betreff
    body: str | None = None
    step: str = "to"  # to | subject | body
    updated: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.updated = time.monotonic()

    def expired(self) -> bool:
        return time.monotonic() - self.updated > TIMEOUT_S

    def card(self, state: str | None = None) -> dict[str, Any]:
        """Für die Oberfläche: der Entwurf, wie er gerade aussieht (state: done | cancel | failed | None)."""
        to = self.address or self.to
        if self.address and self.to and not _ADDRESSY.search(self.to) and self.to.lower() != self.address.lower():
            to = f"{self.to[0].upper()}{self.to[1:]} <{self.address}>"  # Kontakt: „Mama <mama@example.de>“
        return {"type": "mail", "to": to, "subject": self.subject or "",
                "body": self.body or "", "step": state or self.step}


@dataclass
class Step:
    kind: str  # ask_to | ask_to_again | bad_to | unknown_contact | ask_subject | ask_subject_again | ask_body |
    #            open | cancel
    heard: str = ""


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().strip(" ,")


def _phrase(text: str) -> str:
    return re.sub(r"[.!]+$", "", text).strip()


def _sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if re.search(r"[.!?…]$", text) else f"{text}."


def advance(draft: MailDraft, text: str, resolve: Callable[[str], str | None]) -> Step:
    """Nächster Schritt nach einer Antwort des Nutzers."""
    said = _clean(text)
    plain = _phrase(said).lower()
    if _CANCEL.fullmatch(plain):
        return Step("cancel")
    # Korrekturen: „Die Adresse ist falsch“ – oder ein bloßes „Nein“ gleich nach dem Vorlesen
    if _WRONG_TO.search(plain) or (draft.step == "subject" and _NO.fullmatch(plain)):
        draft.step, draft.address, draft.to = "to", None, ""
        return Step("ask_to_again")
    if _WRONG_SUBJECT.search(plain) or (draft.step == "body" and _NO.fullmatch(plain)):
        draft.step = "subject"
        return Step("ask_subject_again")
    if _OPEN_NOW.fullmatch(plain):
        return Step("open")
    if draft.step == "to":
        if _SKIP.fullmatch(plain):  # „weiter“: Empfänger trägt der Nutzer im Entwurf selbst ein
            draft.step = "subject"
            return Step("ask_subject")
        return take_recipient(draft, said, resolve)
    if draft.step == "subject" and spoken_email(said):
        return take_recipient(draft, said, resolve)  # die Adresse noch einmal diktiert – statt „nein“
    if draft.step == "subject":
        draft.subject = "" if _SKIP.fullmatch(plain) else _phrase(said)[:300]
        if draft.subject:
            draft.subject = draft.subject[0].upper() + draft.subject[1:]
        draft.step = "body"
        return Step("ask_body")
    draft.body = "" if _SKIP.fullmatch(plain) else _sentence(said)[:4000]
    return Step("open")


def take_recipient(draft: MailDraft, said: str, resolve: Callable[[str], str | None]) -> Step:
    address = resolve(said)
    if address:
        draft.to, draft.address, draft.step = said, address, "subject"
        return Step("ask_subject")
    name = re.sub(r"^(?:an|für)\s+", "", _phrase(said), flags=re.I)
    if not _ADDRESSY.search(name) and 1 <= len(name.split()) <= 3:
        draft.to = name  # merken: sagt der Nutzer „weiter“, trägt er die Adresse im Entwurf selbst ein
        return Step("unknown_contact", name)  # ein Name, aber kein bekannter Kontakt
    return Step("bad_to", _phrase(said))


def compose_body(body: str | None, name: str | None) -> str:
    """Anrede und Gruß ergänzen, wenn der Nutzer sie nicht selbst diktiert hat."""
    if not body:
        return ""
    text = body if _GREETING.match(body) else f"Hallo,\n\n{body}"
    if not _CLOSING.search(body):
        text += f"\n\nViele Grüße{chr(10) + name if name else ''}"
    return text
