"""Langzeitgedächtnis in SQLite: übersteht Neustarts, Konflikte, Duplikate, Vergessen, ohne Embedding-Modell."""

import asyncio
from datetime import datetime

import pytest

from jarvis.capabilities import register_memory_capabilities
from jarvis.context import Situation
from jarvis.fastpath import FastPath, main_clause
from jarvis.memory import HashingEmbedder, MemoryItem, MemoryService, SqliteMemoryStore
from jarvis.orchestrator import TurnRequest
from jarvis.policy import Principal
from jarvis.style import JarvisStyle
from jarvis.testing import ScriptedProvider, say

DANIEL = Principal(actor="user:owner", role="adult", trust="trusted_user", name="Daniel")


class BrokenEmbedder:
    async def embed(self, texts):
        raise ConnectionError("Ollama läuft nicht")


def service(path, embedder=None) -> MemoryService:
    return MemoryService(SqliteMemoryStore(path), embedder or HashingEmbedder())


def test_memories_survive_a_restart(tmp_path):
    db = tmp_path / "memory.db"
    first = service(db)
    asyncio.run(first.remember("Ich trinke meinen Kaffee schwarz", user_id="owner"))
    asyncio.run(first.remember("Mamas Geburtstag ist am 3. Mai", user_id=None))  # Haushaltswissen
    first.store.close()

    again = service(db)
    assert asyncio.run(again.recall("Wie trinke ich Kaffee?", user_id="owner"))[0] == "Ich trinke meinen Kaffee schwarz"
    assert "Mamas Geburtstag ist am 3. Mai" in asyncio.run(again.recall("Geburtstag Mama", user_id="gast"))
    assert "Ich trinke meinen Kaffee schwarz" not in asyncio.run(again.recall("Kaffee", user_id="gast"))  # privat


def test_conflict_and_duplicate_are_written_through(tmp_path):
    db = tmp_path / "memory.db"
    memory = service(db)
    old = asyncio.run(memory.store.add(MemoryItem("Lieblingsfarbe ist blau", "preference", "owner", 0.8, 1.0,
                                                  subject="owner", predicate="lieblingsfarbe", object="blau")))
    asyncio.run(memory.store.add(MemoryItem("Lieblingsfarbe ist grün", "preference", "owner", 0.8, 1.0,
                                            subject="owner", predicate="lieblingsfarbe", object="grün")))
    first = asyncio.run(memory.remember("Ich trinke meinen Kaffee schwarz", user_id="owner"))
    second = asyncio.run(memory.remember("ich trinke meinen kaffee schwarz", user_id="owner"))
    assert first.id == second.id  # zusammengeführt statt doppelt
    memory.store.close()

    reloaded = SqliteMemoryStore(db)
    assert reloaded.items[old.id].valid_until is not None  # der alte Fakt ist beendet – auch nach dem Neustart
    assert len([i for i in reloaded.items.values() if "Kaffee" in i.content]) == 1


def test_forget_deletes_from_disk(tmp_path):
    db = tmp_path / "memory.db"
    memory = service(db)
    asyncio.run(memory.remember("Ich trinke meinen Kaffee schwarz", user_id="owner"))
    assert asyncio.run(memory.forget_matching("Grüner Tee", user_id="owner")) is None  # passt nicht: nichts löschen
    gone = asyncio.run(memory.forget_matching("ich trinke meinen Kaffee schwarz", user_id="owner"))
    assert gone.content == "Ich trinke meinen Kaffee schwarz"
    memory.store.close()
    assert SqliteMemoryStore(db).items == {}


def test_without_embedding_model_memory_still_works(tmp_path):
    memory = service(tmp_path / "memory.db", BrokenEmbedder())
    item = asyncio.run(memory.remember("Der Ersatzschlüssel liegt unter der Matte", user_id="owner"))
    assert item.embedding is None
    assert asyncio.run(memory.recall("Wo liegt der Ersatzschlüssel?", user_id="owner")) == [
        "Der Ersatzschlüssel liegt unter der Matte"]


def test_model_switch_with_other_dimensions_does_not_crash(tmp_path):
    db = tmp_path / "memory.db"
    asyncio.run(service(db, HashingEmbedder(64)).remember("Ich mag Jazz", user_id="owner"))
    later = service(db, HashingEmbedder(128))  # anderes Embedding-Modell: Vektoren passen nicht mehr
    asyncio.run(later.remember("Ich mag Jazz sehr", user_id="owner"))
    assert "Ich mag Jazz" in asyncio.run(later.recall("Jazz", user_id="owner"))


@pytest.mark.parametrize("clause, sentence", [
    ("ich meinen Kaffee schwarz trinke", "ich trinke meinen Kaffee schwarz"),
    ("Mamas Geburtstag am 3. Mai ist", "Mamas Geburtstag ist am 3. Mai"),
    ("mein Auto in der Garage steht", "mein Auto steht in der Garage"),
    ("Max Vegetarier ist", "Max ist Vegetarier"),
    ("heute schön ist", "heute schön ist"),  # unklar: so lassen, wie gesagt
])
def test_dass_clauses_become_main_clauses(clause, sentence):
    assert main_clause(clause) == sentence


@pytest.mark.parametrize("text, capability, arguments", [
    ("Jarvis, merk dir, dass ich meinen Kaffee schwarz trinke.", "memory.remember",
     {"content": "Ich trinke meinen Kaffee schwarz"}),
    ("Notiere dir: Mamas Geburtstag ist am 3. Mai", "memory.remember", {"content": "Mamas Geburtstag ist am 3. Mai"}),
    ("Was weißt du über mich?", "memory.list", {"limit": 10}),
    ("Vergiss, dass ich meinen Kaffee schwarz trinke", "memory.forget", {"query": "ich meinen Kaffee schwarz trinke"}),
])
def test_memory_commands(text, capability, arguments):
    match = FastPath({}, {}).match(text)
    assert (match.capability, match.arguments) == (capability, arguments)


def test_memory_dialog(orchestrator, tmp_path):
    memory = service(tmp_path / "memory.db")
    register_memory_capabilities(orchestrator.registry, memory)
    orchestrator.style = JarvisStyle()
    situation = Situation(now=datetime(2026, 10, 9, 9, 0), user_display="Daniel")

    def turn(text):
        return asyncio.run(orchestrator.handle_turn(TurnRequest(text=text, session_id="m", principal=DANIEL),
                                                    provider=ScriptedProvider([say("-")]), situation=situation)).text

    assert turn("Was weißt du über mich?").startswith("Bislang habe ich mir nichts über Sie gemerkt")
    assert turn("Merk dir, dass ich meinen Kaffee schwarz trinke") == "Sehr wohl. Ich habe es mir notiert."
    assert turn("Was weißt du über mich?") == "Folgendes habe ich mir gemerkt:\n- Ich trinke meinen Kaffee schwarz"
    assert turn("Vergiss, dass ich meinen Kaffee schwarz trinke") == \
        "Sehr wohl. Vergessen: „Ich trinke meinen Kaffee schwarz“."
    assert turn("Vergiss, dass ich Tee mag").startswith("Dazu habe ich nichts gespeichert")
