"""Memory-System: Embeddings, Ranking, Deduplizierung, Konfliktauflösung, Vergessen.

``InMemoryMemoryStore`` implementiert die Logik vollständig (Tests, Einzelplatz); ``PostgresMemoryStore``
bildet dieselbe Schnittstelle auf PostgreSQL + pgvector ab (db/schema.sql, Tabelle memory_items).
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from array import array
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from .events import new_id

log = logging.getLogger(__name__)


@dataclass
class MemoryItem:
    content: str
    kind: str  # episodic | semantic | procedural | preference
    user_id: str | None  # None = Haushaltswissen
    importance: float
    confidence: float
    sensitivity: str = "personal"  # public | personal | sensitive
    source_type: str = "conversation"
    subject: str | None = None
    predicate: str | None = None
    object: str | None = None
    embedding: list[float] | None = None
    id: str = field(default_factory=lambda: new_id("mem"))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    valid_until: datetime | None = None
    access_count: int = 0


@dataclass(frozen=True)
class RankingWeights:
    similarity: float = 0.6
    recency: float = 0.25
    importance: float = 0.15
    half_life_days: float = 30.0


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def score(item: MemoryItem, similarity: float, now: datetime, w: RankingWeights) -> float:
    if item.kind in ("semantic", "preference", "procedural"):
        recency = 1.0  # Fakten verfallen nicht mit der Zeit, sondern durch Widerspruch
    else:
        age_days = max(0.0, (now - item.created_at).total_seconds() / 86400)
        recency = 0.5 ** (age_days / w.half_life_days)
    return w.similarity * similarity + w.recency * recency + w.importance * item.importance


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Deterministischer Bag-of-Words-Embedder für Tests und Offline-Betrieb ohne Modell."""

    def __init__(self, dimensions: int = 256) -> None:
        self.dimensions = dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vec = [0.0] * self.dimensions
            for token in re.findall(r"[a-zäöüß0-9]+", text.lower()):
                h = int(hashlib.sha256(token.encode()).hexdigest(), 16)
                vec[h % self.dimensions] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return vectors


class OllamaEmbedder:
    def __init__(self, base_url: str = "http://localhost:11434", model: str = "bge-m3",
                 keep_alive: str | int = "24h") -> None:
        import httpx

        self._client = httpx.AsyncClient(base_url=base_url, timeout=30)
        self.model = model
        self.keep_alive = keep_alive

    async def embed(self, texts: list[str]) -> list[list[float]]:
        response = await self._client.post("/api/embed", json={"model": self.model, "input": texts,
                                                              "keep_alive": self.keep_alive})
        response.raise_for_status()
        return response.json()["embeddings"]


class InMemoryMemoryStore:
    def __init__(self, dedup_similarity: float = 0.92) -> None:
        self.items: dict[str, MemoryItem] = {}
        self.dedup_similarity = dedup_similarity

    async def add(self, item: MemoryItem, now: datetime | None = None) -> MemoryItem:
        now = now or datetime.now(UTC)
        if item.sensitivity not in ("public", "personal", "sensitive"):
            raise ValueError("secrets are never stored as memories")
        # 1) Konflikt: gleicher Fakt (Subjekt/Prädikat) mit neuem Objekt -> alten Fakt beenden
        if item.subject and item.predicate:
            for old in self._active(item.user_id):
                if (old.subject, old.predicate) == (item.subject, item.predicate) and old.object != item.object:
                    old.valid_until = now
        # 2) Duplikat: sehr ähnlicher Inhalt -> zusammenführen statt doppelt speichern
        if item.embedding is not None:
            for old in self._active(item.user_id):
                if old.kind == item.kind and old.embedding is not None and len(old.embedding) == len(item.embedding):
                    if cosine(old.embedding, item.embedding) >= self.dedup_similarity:
                        old.importance = max(old.importance, item.importance)
                        old.confidence = min(1.0, max(old.confidence, item.confidence) + 0.05)
                        return old
        self.items[item.id] = item
        return item

    async def search(
        self,
        query_embedding: list[float],
        *,
        user_id: str | None,
        now: datetime | None = None,
        top_k: int = 8,
        include_sensitive: bool = False,
        weights: RankingWeights = RankingWeights(),
    ) -> list[tuple[MemoryItem, float]]:
        now = now or datetime.now(UTC)
        candidates = [
            i for i in self._visible(user_id)
            if i.embedding is not None and (include_sensitive or i.sensitivity != "sensitive")
        ]
        ranked = sorted(
            ((i, score(i, cosine(i.embedding, query_embedding), now, weights)) for i in candidates),  # type: ignore[arg-type]
            key=lambda pair: pair[1],
            reverse=True,
        )[:top_k]
        for item, _ in ranked:
            item.access_count += 1
        return ranked

    async def has_memories(self, user_id: str | None) -> bool:
        return bool(self._visible(user_id))

    async def forget(self, item_id: str, *, user_id: str | None) -> bool:
        item = self.items.get(item_id)
        if item is None or item.user_id not in (user_id, None):
            return False
        del self.items[item_id]
        return True

    def _active(self, user_id: str | None) -> list[MemoryItem]:
        return [i for i in self.items.values() if i.user_id == user_id and i.valid_until is None]

    def _visible(self, user_id: str | None) -> list[MemoryItem]:
        return [i for i in self.items.values() if i.valid_until is None and i.user_id in (user_id, None)]


class PostgresMemoryStore:
    """Gleiche Semantik wie InMemoryMemoryStore, Ranking in SQL (pgvector-Cosinus + Recency + Importance)."""

    SEARCH_SQL = """
        WITH candidates AS (
            SELECT id, content, kind, importance, created_at,
                   1 - (embedding <=> $1::text::vector) AS similarity
            FROM jarvis.memory_items
            WHERE valid_until IS NULL
              AND embedding IS NOT NULL
              AND (expires_at IS NULL OR expires_at > now())
              AND (user_id = $2::text OR user_id IS NULL)
              AND ($3::boolean OR sensitivity <> 'sensitive')
            ORDER BY embedding <=> $1::text::vector
            LIMIT $4::int
        )
        SELECT id, content, kind,
               $5::float8 * similarity
             + $6::float8 * CASE WHEN kind = 'episodic'
                                 THEN power(0.5, extract(epoch FROM now() - created_at)::float8 / 86400 / $7::float8)
                                 ELSE 1 END
             + $8::float8 * importance AS score
        FROM candidates
        ORDER BY score DESC
        LIMIT $9::int
    """

    def __init__(self, pool: Any) -> None:
        self.pool = pool  # asyncpg.Pool

    async def has_memories(self, user_id: str | None) -> bool:
        return bool(await self.pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM jarvis.memory_items WHERE valid_until IS NULL"
            " AND (user_id = $1::text OR user_id IS NULL))", user_id))

    async def search(
        self,
        query_embedding: list[float],
        *,
        user_id: str | None,
        top_k: int = 8,
        candidate_pool: int = 50,
        include_sensitive: bool = False,
        weights: RankingWeights = RankingWeights(),
    ) -> list[tuple[MemoryItem, float]]:
        vector_literal = "[" + ",".join(f"{v:.6f}" for v in query_embedding) + "]"
        rows = await self.pool.fetch(
            self.SEARCH_SQL, vector_literal, user_id, include_sensitive, candidate_pool,
            weights.similarity, weights.recency, weights.half_life_days, weights.importance, top_k,
        )
        return [
            (MemoryItem(id=r["id"], content=r["content"], kind=r["kind"], user_id=user_id,
                        importance=0.0, confidence=0.0), float(r["score"]))
            for r in rows
        ]


_WORD = re.compile(r"[a-zäöüß0-9]{3,}")


def lexical_similarity(query: str, content: str) -> float:
    """Wortüberlappung (0..1) – Rückfall, wenn kein Embedding-Modell läuft oder die Dimension nicht passt."""
    q, c = set(_WORD.findall(query.lower())), set(_WORD.findall(content.lower()))
    return len(q & c) / len(q) if q and c else 0.0


def _similarity(item: MemoryItem, embedding: list[float] | None, query: str) -> float:
    if embedding is not None and item.embedding is not None and len(item.embedding) == len(embedding):
        return cosine(item.embedding, embedding)
    return lexical_similarity(query, item.content)


class SqliteMemoryStore(InMemoryMemoryStore):
    """Langzeitgedächtnis in einer SQLite-Datei (``data/memory.db``) – ohne Datenbankserver, übersteht Neustarts.

    Logik (Konflikte, Duplikate, Ranking) wie ``InMemoryMemoryStore``; jede Änderung wird sofort geschrieben.
    Embeddings liegen als float32-BLOB; gesucht wird im Speicher (für einige tausend Einträge im Millisekundenbereich).
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: Any, dedup_similarity: float = 0.92) -> None:
        import sqlite3
        from pathlib import Path

        super().__init__(dedup_similarity)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("""CREATE TABLE IF NOT EXISTS memory_items (
            id TEXT PRIMARY KEY, content TEXT NOT NULL, kind TEXT NOT NULL, user_id TEXT,
            importance REAL NOT NULL, confidence REAL NOT NULL, sensitivity TEXT NOT NULL, source_type TEXT NOT NULL,
            subject TEXT, predicate TEXT, object TEXT, embedding BLOB, created_at TEXT NOT NULL, valid_until TEXT,
            access_count INTEGER NOT NULL DEFAULT 0)""")
        self._db.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
        self._db.commit()
        for row in self._db.execute("SELECT * FROM memory_items"):
            item = self._from_row(row)
            self.items[item.id] = item
        log.info("Gedächtnis geladen: %d Einträge aus %s", len(self.items), self.path)

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> MemoryItem:
        (item_id, content, kind, user_id, importance, confidence, sensitivity, source_type, subject, predicate,
         obj, blob, created_at, valid_until, access_count) = row
        embedding = list(array("f", blob)) if blob else None
        return MemoryItem(content=content, kind=kind, user_id=user_id, importance=importance, confidence=confidence,
                          sensitivity=sensitivity, source_type=source_type, subject=subject, predicate=predicate,
                          object=obj, embedding=embedding, id=item_id, created_at=datetime.fromisoformat(created_at),
                          valid_until=datetime.fromisoformat(valid_until) if valid_until else None,
                          access_count=access_count)

    def _write(self, items: list[MemoryItem]) -> None:
        self._db.executemany(
            "INSERT OR REPLACE INTO memory_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(i.id, i.content, i.kind, i.user_id, i.importance, i.confidence, i.sensitivity, i.source_type, i.subject,
              i.predicate, i.object, array("f", i.embedding).tobytes() if i.embedding is not None else None,
              i.created_at.isoformat(), i.valid_until.isoformat() if i.valid_until else None, i.access_count)
             for i in items])
        self._db.commit()

    async def add(self, item: MemoryItem, now: datetime | None = None) -> MemoryItem:
        before = {i.id: (i.valid_until, i.importance, i.confidence) for i in self._active(item.user_id)}
        stored = await super().add(item, now)
        changed = [i for i in self.items.values()
                   if i.id in before and before[i.id] != (i.valid_until, i.importance, i.confidence)]
        self._write([stored, *[i for i in changed if i.id != stored.id]])
        return stored

    async def forget(self, item_id: str, *, user_id: str | None) -> bool:
        if not await super().forget(item_id, user_id=user_id):
            return False
        self._db.execute("DELETE FROM memory_items WHERE id = ?", (item_id,))
        self._db.commit()
        return True

    def close(self) -> None:
        self._db.close()


class MemoryService:
    """Fassade für Orchestrator und Tools: merken, erinnern, vergessen.

    Fällt das Embedding-Modell aus (Ollama aus, Modell fehlt), wird trotzdem gespeichert und per Wortüberlappung
    gesucht – das Gedächtnis bleibt nutzbar, statt mit einem Fehler abzubrechen."""

    def __init__(self, store: InMemoryMemoryStore, embedder: Embedder, weights: RankingWeights = RankingWeights()):
        self.store = store
        self.embedder = embedder
        self.weights = weights

    async def _embed(self, text: str) -> list[float] | None:
        try:
            [embedding] = await self.embedder.embed([text])
            return embedding
        except Exception as exc:  # Modell nicht erreichbar: ohne Vektor weiter
            log.warning("Embedding nicht verfügbar (%s) – Gedächtnis arbeitet mit Wortsuche", exc)
            return None

    async def remember(self, content: str, *, user_id: str | None, kind: str = "semantic",
                       importance: float = 0.9, confidence: float = 1.0, **fields: Any) -> MemoryItem:
        item = MemoryItem(content=content, kind=kind, user_id=user_id, importance=importance,
                          confidence=confidence, embedding=await self._embed(content), **fields)
        return await self.store.add(item)

    async def search_items(self, query: str, *, user_id: str | None, top_k: int = 8,
                           include_sensitive: bool = False, now: datetime | None = None,
                           min_similarity: float = 0.0) -> list[tuple[MemoryItem, float]]:
        """Treffer mit Ähnlichkeit (0..1) – Vektor, wo möglich, sonst Wortüberlappung."""
        if not await self.store.has_memories(user_id):
            return []  # nichts gespeichert: kein Embedding nötig (spart ein zweites Modell im Speicher)
        now = now or datetime.now(UTC)
        embedding = await self._embed(query)
        if isinstance(self.store, InMemoryMemoryStore):
            ranked = []
            for item in self.store._visible(user_id):
                if item.sensitivity == "sensitive" and not include_sensitive:
                    continue
                similarity = _similarity(item, embedding, query)
                vector = embedding is not None and item.embedding is not None and \
                    len(item.embedding) == len(embedding)
                if similarity >= min_similarity and (vector or similarity > 0):  # Wortsuche: nur echte Treffer
                    ranked.append((item, similarity, score(item, similarity, now, self.weights)))
            ranked.sort(key=lambda entry: entry[2], reverse=True)
            return [(item, similarity) for item, similarity, _ in ranked[:top_k]]
        if embedding is None:
            return []
        results = await self.store.search(embedding, user_id=user_id, top_k=top_k,
                                          include_sensitive=include_sensitive, weights=self.weights)
        return [(item, value) for item, value in results if value >= min_similarity]

    async def recall(self, query: str, *, user_id: str | None, top_k: int = 8,
                     include_sensitive: bool = False, now: datetime | None = None) -> list[str]:
        results = await self.search_items(query, user_id=user_id, top_k=top_k, include_sensitive=include_sensitive,
                                          now=now)
        return [item.content for item, _ in results]

    async def everything(self, *, user_id: str | None, limit: int = 20) -> list[MemoryItem]:
        """„Was weißt du über mich?“ – die gültigen Einträge, neueste zuerst."""
        if not isinstance(self.store, InMemoryMemoryStore):
            return []
        items = [i for i in self.store._visible(user_id) if i.sensitivity != "sensitive"]
        return sorted(items, key=lambda i: i.created_at, reverse=True)[:limit]

    async def forget_matching(self, text: str, *, user_id: str | None) -> MemoryItem | None:
        """„Vergiss, dass …“: den am besten passenden eigenen Eintrag löschen (nur bei deutlicher Übereinstimmung)."""
        candidates = await self.search_items(text, user_id=user_id, top_k=3, include_sensitive=True,
                                             min_similarity=0.5)
        for item, _ in candidates:
            if await self.store.forget(item.id, user_id=user_id):
                return item
        return None
