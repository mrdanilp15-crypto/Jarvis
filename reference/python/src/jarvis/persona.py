"""Persona-Schicht: lädt die Konfiguration und formt Ausgaben kanalgerecht.

Die Persona verändert nur Ton und Form. Inhalt, Warnungen und Policy-Entscheidungen bleiben unberührt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_MARKDOWN = [
    (re.compile(r"```.*?```", re.DOTALL), ""),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"\1"),
    (re.compile(r"(?<!\w)\*([^*]+)\*(?!\w)"), r"\1"),
    (re.compile(r"^#{1,6}\s*", re.MULTILINE), ""),
    (re.compile(r"^\s*[-*•]\s+", re.MULTILINE), ""),
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),
]
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ0-9„\"])")


@dataclass
class Persona:
    config: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path, schema_path: str | Path | None = None) -> Persona:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if schema_path is not None:
            import json

            from jsonschema import Draft202012Validator

            schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
            Draft202012Validator(schema).validate(data)
        return cls(data)

    @property
    def id(self) -> str:
        return self.config["id"]

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    @property
    def system_prompt_fragment(self) -> str:
        return self.config.get("system_prompt_fragment", "").strip()

    @property
    def voice(self) -> dict[str, Any]:
        return self.config.get("voice", {})

    def address_for(self, user_id: str | None) -> str:
        address = self.config.get("address", {})
        if user_id and user_id in address.get("per_user", {}):
            return address["per_user"][user_id]
        return address.get("default", "")

    def humor_allowed(self, kind: str) -> bool:
        never = self.config.get("humor_rules", {}).get("never_during", [])
        return self.config["style"]["humor"] > 0 and kind not in never

    def for_voice(self, text: str) -> str:
        """Markdown entfernen und auf die maximale Satzzahl für Sprachausgabe kürzen."""
        for pattern, repl in _MARKDOWN:
            text = pattern.sub(repl, text)
        text = re.sub(r"\s+", " ", text).strip()
        limit = self.config.get("style", {}).get("max_voice_sentences")
        if limit:
            sentences = _SENTENCE_END.split(text)
            text = " ".join(sentences[:limit])
        return text
