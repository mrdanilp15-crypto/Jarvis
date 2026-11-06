"""Modell-Vergleich: dieselben Testsätze mit jedem installierten Ollama-Modell – Tempo und richtige Werkzeugwahl.

So sieht man für die eigene Grafikkarte, welches Modell wirklich das beste ist, statt zu raten. Jedes Modell wird nach dem
Test wieder entladen; danach lädt JARVIS sein eigenes Modell bei der nächsten Frage neu.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from typing import Any

from .errors import JarvisError
from .llm.base import SystemPrompt, UserTurn
from .toolgate import wanted_domains

# (Satz, erlaubte Werkzeuge; leer = es darf keines aufgerufen werden)
TESTS: list[tuple[str, set[str]]] = [
    ("Mach das Licht im Wohnzimmer an", {"home__set_light"}),
    ("Wie wird das Wetter morgen in Hamburg?", {"info__weather"}),
    ("Erinnere mich in zehn Minuten an die Wäsche", {"reminder__create", "timer__start"}),
    ("Wie viel Prozent hat der Akku noch?", {"system__monitor", "system__status"}),
    ("Mir geht es heute nicht so gut", set()),
    ("Danke, das war's", set()),
]
SKIP = ("embed", "bge", "nomic", "minilm", "rerank")


async def installed_models(provider: Any) -> list[str]:
    try:
        reply = await provider._client.get("/api/tags")
        reply.raise_for_status()
    except Exception as exc:
        raise JarvisError("JRV-LLM-001", f"Ollama nicht erreichbar: {exc}",
                          user_message="Ollama ist gerade nicht erreichbar – bitte erst starten (Startmenü → Ollama).") from exc
    names = [m["name"] for m in reply.json().get("models") or []]
    return [n for n in names if not any(s in n.lower() for s in SKIP) and "vl" not in n.lower()][:6]


async def compare_models(provider: Any, registry: Any, make_provider: Callable[[str], Any],
                         clock: Callable[[], float] = time.monotonic) -> list[dict[str, Any]]:
    known = frozenset(c.domain for c in registry.all())
    results = []
    for name in await installed_models(provider):
        candidate = make_provider(name)
        times: list[float] = []
        correct, problems = 0, []
        for text, allowed in TESTS:
            tools = registry.tool_specs(wanted_domains(text, known))
            started = clock()
            try:
                response = await candidate.complete(system=SystemPrompt("Du bist JARVIS, ein höflicher Assistent. Nutze ein "
                                                                        "Werkzeug nur, wenn es die Aufgabe verlangt."),
                                                    transcript=[UserTurn(text)], tools=tools, effort=None)
            except Exception as exc:
                problems.append(str(exc)[:60])
                continue
            times.append(clock() - started)
            called = {c.name for c in response.tool_calls}
            if (not allowed and not called) or (allowed and called and called <= allowed):
                correct += 1
        results.append({"model": name, "correct": correct, "total": len(TESTS),
                        "seconds": round(statistics.median(times), 1) if times else None, "problems": problems[:1]})
    results.sort(key=lambda r: (-r["correct"], r["seconds"] if r["seconds"] is not None else 999))
    return results
