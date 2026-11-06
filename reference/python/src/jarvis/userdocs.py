"""Eigenes Wissen: Dateien in einem Ordner (Notizen, PDFs, Word) lokal durchsuchen – Antworten kommen aus den Texten,
nicht aus dem Gedächtnis des Sprachmodells. Ordner: ``JARVIS_KNOWLEDGE_DIR`` oder Dokumente\\Jarvis-Wissen.
Suche über Wörter (BM25-artig, mit Wortstämmen); ohne Sprachmodell, ohne Netz.
"""

from __future__ import annotations

import math
import os
import re
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

from .errors import JarvisError
from .tools import Capability, InvocationContext, ToolRegistry

SUFFIXES = {".txt", ".md", ".csv", ".docx", ".pdf", ".log"}
MAX_BYTES = 8_000_000
CHUNK = 700
_WORD = re.compile(r"[a-zäöüß0-9]{3,}")


def knowledge_folder() -> Path:
    return Path(os.environ.get("JARVIS_KNOWLEDGE_DIR") or Path.home() / "Documents" / "Jarvis-Wissen")


def stem(word: str) -> str:
    return word[:6]  # grob, aber für Deutsch brauchbar: „Mietvertrag“, „Mietvertrags“, „Mietverträge“ ~ „mietve“


def words(text: str) -> list[str]:
    return [stem(w) for w in _WORD.findall(text.lower())]


def read_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        return re.sub(r"<[^>]+>", "", xml.replace("</w:p>", "\n"))
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise JarvisError("JRV-INT-001", "pypdf fehlt", user_message="Für PDFs fehlt das Paket pypdf.") from None
        return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
    return path.read_text(encoding="utf-8", errors="replace")


def chunks(text: str) -> list[str]:
    out, current = [], ""
    for paragraph in re.split(r"\n\s*\n|\r\n\r\n", text):
        paragraph = re.sub(r"\s+", " ", paragraph).strip()
        if not paragraph:
            continue
        if current and len(current) + len(paragraph) > CHUNK:
            out.append(current)
            current = ""
        current = f"{current} {paragraph}".strip()
        while len(current) > CHUNK * 1.5:  # lange Absätze zerlegen
            out.append(current[:CHUNK])
            current = current[CHUNK:]
    if current:
        out.append(current)
    return out


class DocIndex:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self._sig: tuple = ()
        self._chunks: list[tuple[str, str, Counter[str]]] = []  # (Datei, Text, Wortzählung)
        self.skipped: list[str] = []

    def files(self) -> list[Path]:
        if not self.folder.is_dir():
            return []
        return sorted(p for p in self.folder.rglob("*") if p.is_file() and p.suffix.lower() in SUFFIXES
                      and p.stat().st_size <= MAX_BYTES)

    def refresh(self) -> None:
        files = self.files()
        sig = tuple((str(p), p.stat().st_mtime, p.stat().st_size) for p in files)
        if sig == self._sig:
            return
        self._sig, self._chunks, self.skipped = sig, [], []
        for path in files:
            try:
                for piece in chunks(read_text(path)):
                    self._chunks.append((path.name, piece, Counter(words(piece))))
            except Exception:  # kaputte oder geschützte Datei: überspringen, der Rest bleibt durchsuchbar
                self.skipped.append(path.name)

    def search(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        self.refresh()
        terms = list(dict.fromkeys(words(query)))
        if not terms or not self._chunks:
            return []
        total = len(self._chunks)
        df = {t: sum(1 for _, _, c in self._chunks if t in c) for t in terms}
        scored = []
        for name, text, counts in self._chunks:
            score = sum((1 + math.log(counts[t])) * math.log(1 + total / df[t]) for t in terms if counts[t])
            matched = sum(1 for t in terms if counts[t])
            if matched:
                scored.append((score * (matched / len(terms)) ** 2, name, text))
        scored.sort(key=lambda x: -x[0])
        seen, hits = set(), []
        for score, name, text in scored:
            if (name, text[:40]) in seen:
                continue
            seen.add((name, text[:40]))
            hits.append({"file": name, "text": text if len(text) <= 420 else text[:420].rsplit(" ", 1)[0] + " …"})
            if len(hits) >= limit:
                break
        return hits


def register_knowledge_capabilities(registry: ToolRegistry, index: DocIndex) -> None:
    async def search(args: dict[str, Any], ctx: InvocationContext) -> Any:
        hits = index.search(args["query"])
        return {"folder": str(index.folder), "files": len(index.files()), "hits": hits, "skipped": index.skipped}

    registry.register(Capability(
        name="knowledge.search", domain="knowledge", risk_class="R0", output_trust="untrusted", timeout_s=20.0,
        description="Durchsucht die eigenen Dokumente und Notizen des Nutzers (Ordner Jarvis-Wissen) und liefert die "
                    "passenden Textstellen. Für Fragen zu „meinen Unterlagen, Notizen, Verträgen“.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["query"], "properties": {
            "query": {"type": "string", "minLength": 2, "maxLength": 200}}},
        handler=search))
