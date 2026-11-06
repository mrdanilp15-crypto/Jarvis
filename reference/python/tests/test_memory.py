import asyncio
from datetime import UTC, datetime, timedelta

from jarvis.memory import HashingEmbedder, InMemoryMemoryStore, MemoryItem, MemoryService, RankingWeights, score


def service() -> MemoryService:
    return MemoryService(InMemoryMemoryStore(), HashingEmbedder())


def test_recall_prefers_relevant_items():
    mem = service()

    async def scenario():
        await mem.remember("Alex trinkt Kaffee schwarz ohne Zucker", user_id="alex", kind="preference")
        await mem.remember("Die Mülltonne wird dienstags geleert", user_id=None, kind="semantic")
        return await mem.recall("Wie trinkt Alex Kaffee?", user_id="alex", top_k=1)

    assert asyncio.run(scenario()) == ["Alex trinkt Kaffee schwarz ohne Zucker"]


def test_personal_memories_are_private():
    mem = service()

    async def scenario():
        await mem.remember("Sam hat am 3. Mai Geburtstag", user_id="sam")
        await mem.remember("Das WLAN heißt Wohnung-5G", user_id=None)
        return await mem.recall("Geburtstag WLAN", user_id="alex", top_k=5)

    assert asyncio.run(scenario()) == ["Das WLAN heißt Wohnung-5G"]


def test_sensitive_memories_excluded_unless_allowed():
    mem = service()

    async def scenario():
        await mem.remember("Alex nimmt morgens Blutdrucktabletten", user_id="alex", sensitivity="sensitive")
        cloud = await mem.recall("Tabletten morgens", user_id="alex")
        local = await mem.recall("Tabletten morgens", user_id="alex", include_sensitive=True)
        return cloud, local

    cloud, local = asyncio.run(scenario())
    assert cloud == [] and local == ["Alex nimmt morgens Blutdrucktabletten"]


def test_conflicting_fact_supersedes_old_one():
    mem = service()

    async def scenario():
        old = await mem.remember("Müll wird dienstags abgeholt", user_id=None, subject="household",
                                 predicate="trash.day", object="tuesday")
        new = await mem.remember("Müll wird ab Oktober mittwochs abgeholt", user_id=None, subject="household",
                                 predicate="trash.day", object="wednesday")
        return old, new, await mem.recall("Wann wird der Müll abgeholt", user_id=None)

    old, new, recalled = asyncio.run(scenario())
    assert old.valid_until is not None and new.valid_until is None
    assert recalled == ["Müll wird ab Oktober mittwochs abgeholt"]


def test_near_duplicates_are_merged():
    mem = service()

    async def scenario():
        a = await mem.remember("Alex mag Jazz am Abend", user_id="alex", kind="preference", importance=0.5)
        b = await mem.remember("Alex mag Jazz am Abend", user_id="alex", kind="preference", importance=0.8)
        return a, b

    a, b = asyncio.run(scenario())
    assert a is b and a.importance == 0.8
    assert len(mem.store.items) == 1


def test_recency_decay_only_for_episodes():
    now = datetime(2026, 9, 26, tzinfo=UTC)
    w = RankingWeights()
    old_episode = MemoryItem("x", "episodic", None, 0.5, 1.0, created_at=now - timedelta(days=30))
    old_fact = MemoryItem("x", "semantic", None, 0.5, 1.0, created_at=now - timedelta(days=30))
    assert round(score(old_episode, 1.0, now, w), 3) == round(0.6 + 0.25 * 0.5 + 0.15 * 0.5, 3)
    assert score(old_fact, 1.0, now, w) > score(old_episode, 1.0, now, w)


def test_recall_without_memories_skips_embedding():
    class CountingEmbedder(HashingEmbedder):
        calls = 0

        async def embed(self, texts):
            CountingEmbedder.calls += 1
            return await super().embed(texts)

    service = MemoryService(InMemoryMemoryStore(), CountingEmbedder())
    assert asyncio.run(service.recall("Wie ist das Wetter?", user_id="alex")) == []
    assert CountingEmbedder.calls == 0  # kein Embedding-Modell laden, solange nichts gespeichert ist
    asyncio.run(service.remember("Alex mag Jazz", user_id="alex"))
    assert asyncio.run(service.recall("Musik", user_id="alex")) == ["Alex mag Jazz"]
