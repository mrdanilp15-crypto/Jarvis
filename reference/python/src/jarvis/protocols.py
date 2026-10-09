"""Protokolle wie im Film: „Jarvis, Protokoll Gute Nacht“ – mehrere Schritte nacheinander, sichtbar abgehakt.

Jeder Schritt ist ein ganz normaler Sofortbefehl, so wie man ihn sagen würde („Alle Lichter aus“, „Wie wird das
Wetter morgen?“, „Sperr den Bildschirm“). Er läuft durch dieselbe Erkennung und dieselben Richtlinien wie ein
gesprochener Befehl – ein Protokoll kann also nichts, was der Nutzer nicht auch selbst sagen dürfte. Was keinem
Sofortbefehl entspricht, wird übersprungen (nie dem Sprachmodell überlassen).

Auslöser: „Protokoll <Name>“, „Starte Protokoll <Name>“ oder eigene Sätze je Protokoll („Gute Nacht“, „Ich gehe
schlafen“). Gespeichert in ``data/protocols.json``, bearbeitbar im Zahnrad → Protokolle.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import JarvisError

MAX_STEPS = 12
DEFAULTS = [
    {"id": "gute-nacht", "name": "Gute Nacht", "triggers": ["Gute Nacht", "Ich gehe schlafen"],
     "steps": ["Wie wird das Wetter morgen?", "Was steht morgen an?", "Alle Lichter aus"]},
    {"id": "morgenbriefing", "name": "Morgenbriefing", "triggers": ["Briefing", "Was liegt heute an?"],
     "steps": ["Wie ist das Wetter?", "Was steht heute an?", "Nachrichten"]},
]
_START = re.compile(r"^(?:(?:starte|start|aktiviere|führe|fahre|initiiere)\s+)?(?:das\s+)?protokoll\s+(?P<name>.+?)"
                    r"(?:\s+(?:aus|starten|aktivieren|ausführen|an))?$", re.I)


def norm(text: str, *, keep_case: bool = False) -> str:
    text = re.sub(r"[„“\"'.,!?:;]+", " ", text if keep_case else text.lower())
    text = re.sub(r"^\s*(?:hey\s+)?jarvis\b\s*", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Protocol:
    id: str
    name: str
    triggers: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)


@dataclass
class ProtocolMatch:
    protocol: Protocol | None  # None: „Protokoll X“ gesagt, aber X gibt es nicht
    spoken_name: str = ""


class ProtocolStore:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.items: list[Protocol] = self._load()

    def _load(self) -> list[Protocol]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path else None
        except (OSError, ValueError):
            data = None
        if not isinstance(data, list):
            data = DEFAULTS
        out = []
        for item in data:
            try:
                out.append(Protocol(id=str(item["id"]), name=str(item["name"]),
                                    triggers=[str(t) for t in item.get("triggers", [])],
                                    steps=[str(s) for s in item.get("steps", [])]))
            except (KeyError, TypeError):
                continue
        return out

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(p) for p in self.items], ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, protocol_id: str) -> Protocol | None:
        return next((p for p in self.items if p.id == protocol_id), None)

    def match(self, text: str) -> ProtocolMatch | None:
        said = norm(text)
        if not said:
            return None
        for protocol in self.items:
            if said in {norm(t) for t in protocol.triggers if t.strip()}:
                return ProtocolMatch(protocol)
        if m := _START.match(said):
            wanted = norm(m["name"])
            for protocol in self.items:
                name = norm(protocol.name)
                if wanted in (name, protocol.id.replace("-", " ")) or wanted.replace(" ", "") == name.replace(" ", ""):
                    return ProtocolMatch(protocol)
            spoken = _START.match(norm(text, keep_case=True))
            return ProtocolMatch(None, (spoken["name"] if spoken else m["name"]).strip())
        return None

    def save(self, protocol_id: str | None, name: str, triggers: list[str], steps: list[str]) -> Protocol:
        name = re.sub(r"\s+", " ", name).strip()[:40]
        steps = [re.sub(r"\s+", " ", s).strip()[:120] for s in steps if s.strip()][:MAX_STEPS]
        triggers = [re.sub(r"\s+", " ", t).strip()[:60] for t in triggers if t.strip()][:5]
        if not name or not steps:
            raise JarvisError("JRV-VAL-001", "Name oder Schritte fehlen",
                              user_message="Ein Protokoll braucht einen Namen und mindestens einen Schritt.")
        taken = {norm(t): p.id for p in self.items for t in [p.name, *p.triggers]}
        for phrase in [name, *triggers]:
            owner = taken.get(norm(phrase))
            if owner and owner != protocol_id:
                raise JarvisError("JRV-VAL-001", "Auslöser doppelt",
                                  user_message=f"„{phrase}“ startet bereits ein anderes Protokoll.")
        existing = self.get(protocol_id) if protocol_id else None
        if existing is None:
            base = re.sub(r"[^a-z0-9]+", "-", name.lower().translate(str.maketrans("äöüß", "aous"))).strip("-")
            new_id, n = base or "protokoll", 2
            while self.get(new_id):
                new_id, n = f"{base}-{n}", n + 1
            existing = Protocol(id=new_id, name=name)
            self.items.append(existing)
        existing.name, existing.triggers, existing.steps = name, triggers, steps
        self._save()
        return existing

    def remove(self, protocol_id: str) -> bool:
        protocol = self.get(protocol_id)
        if protocol is None:
            return False
        self.items.remove(protocol)
        self._save()
        return True

    def public(self) -> list[dict[str, Any]]:
        return [asdict(p) for p in self.items]
