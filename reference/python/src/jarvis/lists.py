"""Listen per Sprache: Einkaufsliste und To-dos – „Setz Milch auf die Einkaufsliste“, „Was steht auf meiner Liste?“.

Lokal in ``data/lists.json``, je Nutzer getrennt nur über den Haushalt geteilt (eine Einkaufsliste für alle).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .errors import JarvisError
from .tools import Capability, InvocationContext, ToolRegistry

NAMES = {"einkauf": "Einkaufsliste", "todo": "To-do-Liste"}
MAX_ITEMS = 60


def list_key(spoken: str | None) -> str:
    text = (spoken or "").lower()
    return "todo" if re.search(r"todo|to-do|aufgabe", text) else "einkauf"


class ListStore:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.data: dict[str, list[str]] = self._load()

    def _load(self) -> dict[str, list[str]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8")) if self.path else {}
            return {k: [str(i) for i in v] for k, v in raw.items() if k in NAMES and isinstance(v, list)}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save(self) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")

    def items(self, key: str) -> list[str]:
        return list(self.data.get(key, []))

    def add(self, key: str, items: list[str]) -> tuple[list[str], list[str]]:
        """-> (neu hinzugefügt, schon vorhanden)"""
        current = self.data.setdefault(key, [])
        added, known = [], []
        for item in (re.sub(r"\s+", " ", i).strip(" .,")[:60] for i in items):
            if not item:
                continue
            if item.lower() in (c.lower() for c in current):
                known.append(item)
            elif len(current) >= MAX_ITEMS:
                raise JarvisError("JRV-VAL-001", "Liste voll", user_message="Die Liste ist voll – bitte erst Einträge streichen.")
            else:
                current.append(item[:1].upper() + item[1:])
                added.append(item)
        self._save()
        return added, known

    def remove(self, key: str, item: str) -> str | None:
        wanted = item.lower().strip()
        current = self.data.get(key, [])
        found = next((c for c in current if c.lower() == wanted), None) or \
            next((c for c in current if wanted in c.lower() or c.lower() in wanted), None)
        if found:
            current.remove(found)
            self._save()
        return found

    def clear(self, key: str) -> int:
        count = len(self.data.get(key, []))
        self.data[key] = []
        self._save()
        return count


def split_items(text: str) -> list[str]:
    return [p for p in re.split(r"\s*(?:,|;|\bund\b)\s*", text) if p.strip()]


def register_list_capabilities(registry: ToolRegistry, store: ListStore) -> None:
    def view(key: str) -> dict[str, Any]:
        return {"list": key, "name": NAMES[key], "items": store.items(key)}

    async def add(args: dict[str, Any], ctx: InvocationContext) -> Any:
        key = list_key(args.get("list"))
        added, known = store.add(key, args["items"])
        return {**view(key), "added": added, "known": known}

    async def remove(args: dict[str, Any], ctx: InvocationContext) -> Any:
        key = list_key(args.get("list"))
        return {**view(key), "removed": store.remove(key, args["item"]), "wanted": args["item"]}

    async def show(args: dict[str, Any], ctx: InvocationContext) -> Any:
        return view(list_key(args.get("list")))

    async def clear(args: dict[str, Any], ctx: InvocationContext) -> Any:
        key = list_key(args.get("list"))
        return {**view(key), "cleared": store.clear(key)}

    lst = {"list": {"enum": ["einkauf", "todo"]}}
    schema = lambda extra, req: {"type": "object", "additionalProperties": False, "required": req,  # noqa: E731
                                 "properties": {**lst, **extra}}
    for name, handler, desc, extra, req in [
        ("lists.add", add, "Setzt Einträge auf die Einkaufsliste (einkauf) oder To-do-Liste (todo).",
         {"items": {"type": "array", "minItems": 1, "maxItems": 10, "items": {"type": "string", "maxLength": 60}}}, ["items"]),
        ("lists.remove", remove, "Streicht einen Eintrag von einer Liste.", {"item": {"type": "string", "maxLength": 60}}, ["item"]),
        ("lists.show", show, "Zeigt eine Liste.", {}, []),
        ("lists.clear", clear, "Leert eine Liste.", {}, []),
    ]:
        registry.register(Capability(name=name, domain="lists", risk_class="R0", side_effects="reversible",
                                     description=desc, input_schema=schema(extra, req), handler=handler))
