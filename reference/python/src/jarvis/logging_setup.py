"""Strukturiertes JSON-Logging mit Korrelations-Kontext und Redaktion sensibler Werte."""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import re
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("jarvis_log_context", default={})

REDACT_KEYS = {"authorization", "token", "password", "api_key", "access_token", "secret", "alarm_code", "pin"}
REDACT_PATTERNS = [
    re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]{8,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"(?i)\b(?:pin|alarmcode)\s*[:=]?\s*\d{4,8}\b"),
]
_STANDARD_ATTRS = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


@contextlib.contextmanager
def bind(**fields: Any) -> Iterator[None]:
    """Hängt Felder (correlation_id, session_id, actor, …) an alle Logzeilen im Block."""
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: "[REDACTED]" if k.lower() in REDACT_KEYS else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        for pattern in REDACT_PATTERNS:
            value = pattern.sub("[REDACTED]", value)
    return value


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
            **_context.get(),
        }
        extras = {k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS}
        entry.update(extras)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(redact(entry), ensure_ascii=False, default=str)


def configure_logging(service: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
