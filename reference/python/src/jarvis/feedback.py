"""„Das war falsch“: Fehlerberichte direkt am Gespräch – Satz, Antwort, Weg und Werkzeuge, lokal in ``data/feedback.jsonl``.

Der Bericht lässt sich im Zahnrad kopieren und weitergeben; so lassen sich Fehler gezielt beheben, statt zu raten.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_KEEP = 200


class FeedbackStore:
    def __init__(self, path: Path | None) -> None:
        self.path = path

    def add(self, *, said: str, reply: str, route: str = "", tools: list[str] | None = None, note: str = "",
            actor: str = "") -> dict[str, Any]:
        entry = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "said": said[:500], "reply": reply[:1500],
                 "route": route[:60], "tools": (tools or [])[:10], "note": note[:300], "actor": actor}
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lines = self._lines()[-(MAX_KEEP - 1):] + [json.dumps(entry, ensure_ascii=False)]
            self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return entry

    def _lines(self) -> list[str]:
        try:
            return [line for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()] if self.path else []
        except OSError:
            return []

    def recent(self, count: int = 20) -> list[dict[str, Any]]:
        out = []
        for line in self._lines()[-count:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def clear(self) -> None:
        if self.path is not None and self.path.exists():
            self.path.unlink()

    def report(self, count: int = 10) -> str:
        """Lesbarer Text zum Kopieren."""
        items = self.recent(count)
        if not items:
            return "Keine Fehlerberichte gespeichert."
        from collections import Counter

        repeated = [f"{n}× „{said}“" for said, n in Counter(e["said"] for e in self.recent(MAX_KEEP)).most_common(3) if n > 1]
        parts = ["Mehrfach gemeldet: " + "; ".join(repeated)] if repeated else []
        for i, e in enumerate(items, 1):
            parts.append(f"{i}. [{e['at']}] Weg: {e.get('route') or '?'} · Werkzeuge: {', '.join(e.get('tools') or []) or '–'}\n"
                         f"   Gesagt: {e['said']}\n   Antwort: {e['reply']}" + (f"\n   Hinweis: {e['note']}" if e.get("note") else ""))
        return "\n".join(parts)
