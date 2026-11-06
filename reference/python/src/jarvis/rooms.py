"""Mehrere Räume: Durchsagen an die Geräte eines Raums und – bei mehreren Geräten – nur das nächste antwortet.

- **Durchsagen:** „Sag in der Küche, dass das Essen fertig ist“, „Durchsage an alle: Abfahrt in fünf Minuten“. Die
  Geräte des Raums (gekoppelte Tablets, offene Fenster mit diesem Raum) sprechen sie mit einem Hinweiston.
- **Wer antwortet auf „Jarvis“?** Hört mehr als ein Gerät auf „Jarvis“, meldet jedes, das es gehört hat, wie laut (Abstand
  der Stimme zum Grundrauschen in dB). JARVIS wartet einen Augenblick, bis alle gemeldet haben, und lässt nur das
  lauteste – also in der Regel das nächste – antworten. Die anderen bleiben still.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .errors import JarvisError
from .timers import Notifier
from .tools import Capability, InvocationContext, ToolRegistry

ALL_ROOMS = re.compile(r"^(?:an\s+|für\s+)?(?:alle|allen|überall|jeden|jedem\s+raum|alle\s+räume|allen\s+räumen)$", re.I)
_PREP = re.compile(r"^(?:in|im|ins|an|am|für|zur|zum)\s+(?:der|dem|die|den|das)?\s*", re.I)


def norm(text: str) -> str:
    text = text.strip().lower().replace("ß", "ss")
    text = text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", text)


def resolve_room(spoken: str, rooms: dict[str, str]) -> str | None:
    """„in der Küche“, „Kueche“, „im Wohnzimmer“ -> area_id aus ``rooms`` (area_id -> Name)."""
    wanted = norm(_PREP.sub("", spoken.strip()))
    if not wanted:
        return None
    for area_id, name in rooms.items():
        if wanted in (norm(name), norm(area_id)) or wanted.rstrip("s") == norm(name):
            return area_id
    return None


def register_room_capabilities(registry: ToolRegistry, notifier: Notifier,
                               rooms: Callable[[], dict[str, str]]) -> None:
    """``rooms()`` liefert alle bekannten Räume (Home Assistant und gekoppelte Geräte): area_id -> Name."""

    async def announce(args: dict[str, Any], ctx: InvocationContext) -> Any:
        text = re.sub(r"\s+", " ", str(args["text"])).strip()
        spoken = (args.get("room") or "").strip()
        everywhere = not spoken or bool(ALL_ROOMS.match(spoken))
        known = rooms()
        area = None if everywhere else resolve_room(spoken, known)
        if not everywhere and area is None:
            raise JarvisError("JRV-NFD-001", f"Raum {spoken} unbekannt",
                              user_message=f"Einen Raum „{_PREP.sub('', spoken)}“ kenne ich nicht.")
        message = {"type": "notification", "kind": "announcement",
                   "text": f"Durchsage: {text[0].upper()}{text[1:]}".rstrip(".!") + "."}
        reached = await notifier.send_area(area, message, exclude_actor=ctx.actor if everywhere else None)
        return {"delivered": len(reached), "area": area, "room": known.get(area or "", spoken) if area else None,
                "everywhere": everywhere}

    registry.register(Capability(
        name="message.announce", domain="message", risk_class="R0",
        description="Durchsage an die Geräte eines Raums (room: z. B. „Küche“) oder an alle Räume (room weglassen). "
                    "Die Geräte dort sprechen den Text.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["text"], "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": 300},
            "room": {"type": "string", "maxLength": 80},
        }},
        handler=announce,
    ))


# ---------------------------------------------------------------------------------------------------- Wer antwortet?
@dataclass
class _Round:
    started: float
    claims: list[tuple[str, float | None]] = field(default_factory=list)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    winner: str | None = None


class WakeArbiter:
    """Entscheidet, welches Gerät auf „Jarvis“ antwortet, wenn mehrere es gehört haben."""

    WINDOW_S = 0.6     # so lange sammeln, bis alle Geräte gemeldet haben (Erkennung braucht unterschiedlich lange)
    HOLD_S = 2.0       # später eintreffende Meldungen derselben Äußerung gehören zur entschiedenen Runde

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.listening: set[str] = set()  # Verbindungen, die gerade auf „Jarvis“ hören
        self._round: _Round | None = None

    def listen(self, client: str, on: bool) -> None:
        if on:
            self.listening.add(client)
        else:
            self.listening.discard(client)

    async def claim(self, client: str, score: float | None) -> bool:
        now = self.clock()
        current = self._round
        if current is not None and current.done.is_set() and now - current.started > self.HOLD_S:
            current = self._round = None
        if current is not None and current.done.is_set():
            return current.winner == client  # diese Äußerung ist schon vergeben
        if current is None:
            others = self.listening - {client}
            if not others:
                return True  # nur ein Gerät hört zu: sofort antworten, ohne Wartezeit
            current = self._round = _Round(started=now)
            current.claims.append((client, score))
            try:
                await asyncio.wait_for(self._all_in(current, others | {client}), timeout=self.WINDOW_S)
            except TimeoutError:
                pass
            current.winner = max(current.claims, key=lambda c: -math.inf if c[1] is None else c[1])[0]
            current.done.set()
            return current.winner == client
        current.claims.append((client, score))
        await current.done.wait()
        return current.winner == client

    @staticmethod
    async def _all_in(current: _Round, expected: set[str]) -> None:
        while {c for c, _ in current.claims} < expected:
            await asyncio.sleep(0.02)
