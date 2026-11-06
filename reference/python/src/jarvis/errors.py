"""Fehler-Taxonomie (RFC 9457 Problem Details), Retry mit Jitter und Circuit-Breaker."""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")

# code: (title, category, http_status, retryable)
ERROR_CATALOG: dict[str, tuple[str, str, int, bool]] = {
    "JRV-VAL-001": ("Ungültige Eingabe", "validation", 422, False),
    "JRV-VAL-002": ("Ungültige Tool-Eingabe", "validation", 422, False),
    "JRV-AUTH-001": ("Nicht authentifiziert", "auth", 401, False),
    "JRV-AUTH-002": ("Ungültige Signatur", "auth", 401, False),
    "JRV-POL-001": ("Bestätigung abgelehnt oder abgelaufen", "policy", 403, False),
    "JRV-POL-002": ("Aktion nicht erlaubt", "policy", 403, False),
    "JRV-NFD-001": ("Nicht gefunden", "not_found", 404, False),
    "JRV-CNF-001": ("Konflikt", "conflict", 409, False),
    "JRV-DEV-001": ("Gerät nicht erreichbar", "device", 503, True),
    "JRV-DEV-002": ("Zielzustand nicht erreicht", "device", 502, False),
    "JRV-INT-001": ("Fehler in einer Integration", "integration", 502, True),
    "JRV-LLM-001": ("Sprachmodell nicht verfügbar", "llm", 503, True),
    "JRV-LLM-002": ("Sprachmodell hat die Anfrage abgelehnt", "llm", 422, False),
    "JRV-LLM-003": ("Ungültige Anfrage an das Sprachmodell", "llm", 400, False),
    "JRV-TMO-001": ("Zeitüberschreitung", "timeout", 504, True),
    "JRV-RATE-001": ("Zu viele Anfragen", "rate_limit", 429, True),
    "JRV-SYS-001": ("Interner Fehler", "internal", 500, False),
}


class JarvisError(Exception):
    def __init__(
        self,
        code: str,
        detail: str | None = None,
        *,
        user_message: str | None = None,
        retry_after_ms: int | None = None,
        errors: list[dict[str, str]] | None = None,
    ) -> None:
        title, category, status, retryable = ERROR_CATALOG[code]
        super().__init__(f"{code}: {detail or title}")
        self.code = code
        self.title = title
        self.category = category
        self.status = status
        self.retryable = retryable
        self.detail = detail
        self.user_message = user_message
        self.retry_after_ms = retry_after_ms
        self.errors = errors

    def to_problem(self, correlation_id: str | None = None, instance: str | None = None) -> dict[str, Any]:
        """Serialisierung gemäß schemas/error.schema.json."""
        problem: dict[str, Any] = {
            "type": f"https://docs.jarvis.local/errors/{self.code.lower()}",
            "title": self.title,
            "status": self.status,
            "code": self.code,
            "category": self.category,
            "retryable": self.retryable,
        }
        optional = {
            "detail": self.detail,
            "instance": instance,
            "retry_after_ms": self.retry_after_ms,
            "correlation_id": correlation_id,
            "user_message": self.user_message,
            "errors": self.errors,
        }
        problem.update({k: v for k, v in optional.items() if v is not None})
        return problem


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay_s: float = 0.5,
    max_delay_s: float = 8.0,
    is_retryable: Callable[[BaseException], bool] = lambda e: getattr(e, "retryable", False),
) -> T:
    """Exponentielles Backoff mit Full-Jitter; nur für als retryable markierte Fehler."""
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except Exception as exc:
            if attempt == attempts or not is_retryable(exc):
                raise
            hint_ms = getattr(exc, "retry_after_ms", None)
            delay = hint_ms / 1000 if hint_ms else random.uniform(0, min(max_delay_s, base_delay_s * 2**attempt))
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


class CircuitBreaker:
    """Öffnet nach ``failure_threshold`` Fehlern innerhalb von ``window_s``; nach ``cooldown_s`` ein Probeaufruf."""

    def __init__(
        self,
        failure_threshold: int = 5,
        window_s: float = 30.0,
        cooldown_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.window_s = window_s
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._failures: deque[float] = deque()
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self.cooldown_s:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        return self.state != "open"

    def record_success(self) -> None:
        self._failures.clear()
        self._opened_at = None

    def record_failure(self) -> None:
        now = self._clock()
        if self.state == "half_open":
            self._opened_at = now  # Probeaufruf gescheitert -> erneut öffnen
            return
        self._failures.append(now)
        while self._failures and now - self._failures[0] > self.window_s:
            self._failures.popleft()
        if len(self._failures) >= self.failure_threshold:
            self._opened_at = now
