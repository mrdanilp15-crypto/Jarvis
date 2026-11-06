import asyncio

import pytest

from jarvis.errors import CircuitBreaker, JarvisError, retry_async
from jarvis.llm.router import Classification, HeuristicClassifier, ModelRouter
from jarvis.testing import ScriptedProvider


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def router(breaker=None):
    return ModelRouter(local=ScriptedProvider([]), cloud=ScriptedProvider([]), cloud_breaker=breaker)


def test_classifier():
    c = HeuristicClassifier()
    assert c.classify("Licht an").complexity == "trivial"
    assert c.classify("Recherchiere die besten Wärmepumpen für einen Altbau").complexity == "complex"
    assert c.classify("Zeig mir das Bild der Kamera an der Haustür").sensitivity == "sensitive"


@pytest.mark.parametrize(("cls", "kwargs", "route"), [
    (Classification("public", "complex"), {"fast_path_confidence": 0.95}, "fast_path"),
    (Classification("public", "complex"), {"confirmation_reply": True}, "confirmation_resolver"),
    (Classification("sensitive", "complex"), {}, "local_llm"),
    (Classification("sensitive", "complex"), {"user_override": "cloud"}, "local_llm"),
    (Classification("public", "simple"), {}, "local_llm"),
    (Classification("public", "complex"), {}, "cloud_llm"),
    (Classification("public", "simple"), {"user_override": "cloud"}, "cloud_llm"),
])
def test_routing_table(cls, kwargs, route):
    assert router().decide(cls, **kwargs).route == route


def test_open_circuit_routes_local():
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=2, window_s=30, cooldown_s=60, clock=clock)
    r = router(breaker)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "open"
    assert r.decide(Classification("public", "complex")).route == "local_llm"
    clock.t = 61
    assert breaker.state == "half_open"
    assert r.decide(Classification("public", "complex")).route == "cloud_llm"
    breaker.record_failure()
    assert breaker.state == "open"
    clock.t = 200
    breaker.record_success()
    assert breaker.state == "closed"


def test_failures_outside_window_do_not_open():
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=2, window_s=10, clock=clock)
    breaker.record_failure()
    clock.t = 20
    breaker.record_failure()
    assert breaker.state == "closed"


def test_retry_only_retryable():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise JarvisError("JRV-DEV-001", "noch nicht", retry_after_ms=1)
        return "ok"

    assert asyncio.run(retry_async(flaky, attempts=3)) == "ok"

    async def denied():
        calls["n"] += 1
        raise JarvisError("JRV-POL-002")

    calls["n"] = 0
    with pytest.raises(JarvisError):
        asyncio.run(retry_async(denied, attempts=3))
    assert calls["n"] == 1


def test_problem_details_shape(validator):
    problem = JarvisError("JRV-DEV-001", "Zigbee offline", user_message="Das Thermostat antwortet nicht.",
                          retry_after_ms=30000).to_problem(correlation_id="cor_1", instance="/v1/actions/act_1")
    validator("error").validate(problem)
    assert problem["status"] == 503 and problem["retryable"] is True
