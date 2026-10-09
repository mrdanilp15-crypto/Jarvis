"""CloudEvents-Umschlag und Event-Bus (In-Memory für Tests, Redis Streams für den Betrieb)."""

from __future__ import annotations

import fnmatch
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)

Trust = Literal["system", "trusted_user", "household", "guest", "external_untrusted"]
Priority = Literal["low", "normal", "high", "critical"]

EVENT_TYPE_PATTERN = r"^jarvis(\.[a-z][a-z0-9_]*){2,4}$"


def new_id(prefix: str) -> str:
    """Kurze, sortierfreie ID mit Präfix, z. B. evt_3f9a…"""
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


class CloudEvent(BaseModel):
    """Entspricht schemas/event.schema.json."""

    model_config = ConfigDict(extra="forbid")

    specversion: Literal["1.0"] = "1.0"
    id: str = Field(default_factory=lambda: new_id("evt"))
    source: str
    type: str = Field(pattern=EVENT_TYPE_PATTERN)
    subject: str | None = None
    time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    datacontenttype: str = "application/json"
    correlationid: str | None = None
    causationid: str | None = None
    actor: str | None = None
    trust: Trust
    priority: Priority = "normal"
    expiresat: datetime | None = None
    data: Any = Field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    def derive(self, *, type: str, source: str, data: Any, subject: str | None = None) -> CloudEvent:
        """Folge-Event: übernimmt Korrelation, Actor und Trust, verweist per causationid auf dieses Event."""
        return CloudEvent(
            type=type,
            source=source,
            subject=subject,
            data=data,
            actor=self.actor,
            trust=self.trust,
            correlationid=self.correlationid or self.id,
            causationid=self.id,
        )


Handler = Callable[[CloudEvent], Awaitable[None]]


class EventBus(Protocol):
    async def publish(self, event: CloudEvent) -> None: ...

    def subscribe(self, pattern: str, handler: Handler) -> None: ...


class InMemoryEventBus:
    """Synchrone Zustellung im selben Prozess – für Tests und Einzelprozess-Betrieb."""

    def __init__(self) -> None:
        self._subscriptions: list[tuple[str, Handler]] = []
        self.published: list[CloudEvent] = []

    def subscribe(self, pattern: str, handler: Handler) -> None:
        self._subscriptions.append((pattern, handler))

    async def publish(self, event: CloudEvent) -> None:
        self.published.append(event)
        for pattern, handler in list(self._subscriptions):
            if fnmatch.fnmatchcase(event.type, pattern):
                try:
                    await handler(event)
                except Exception:  # ein fehlerhafter Abonnent darf andere nicht blockieren
                    log.exception("event handler failed", extra={"event_type": event.type})


class RedisStreamEventBus:
    """Ein Stream je Event-Bereich (jarvis:events:<bereich>) mit Consumer-Group.

    Mehrere Instanzen desselben Dienstes teilen sich eine Consumer-Group und verarbeiten jede Nachricht genau
    einmal. Nachrichten, deren Verarbeitung scheitert, landen im Dead-Letter-Stream ``<prefix>:dead``.
    """

    def __init__(
        self,
        url: str,
        *,
        prefix: str = "jarvis:events",
        group: str = "jarvis-core",
        consumer: str | None = None,
        max_len: int = 200_000,
    ) -> None:
        import redis.asyncio as redis  # optionale Abhängigkeit

        self._redis = redis.Redis.from_url(url, decode_responses=True)
        self._prefix = prefix
        self._group = group
        self._consumer = consumer or new_id("consumer")
        self._max_len = max_len
        self._subscriptions: list[tuple[str, Handler]] = []

    def _stream_for(self, event_type: str) -> str:
        return f"{self._prefix}:{event_type.split('.')[1]}"

    async def publish(self, event: CloudEvent) -> None:
        await self._redis.xadd(
            self._stream_for(event.type),
            {"event": event.model_dump_json(exclude_none=True)},
            maxlen=self._max_len,
            approximate=True,
        )

    def subscribe(self, pattern: str, handler: Handler) -> None:
        self._subscriptions.append((pattern, handler))

    async def run(self, areas: list[str]) -> None:
        """Konsumiert die Streams der angegebenen Bereiche (z. B. ["input", "sensor", "action"])."""
        from redis.exceptions import ResponseError

        streams = [f"{self._prefix}:{area}" for area in areas]
        for stream in streams:
            try:
                await self._redis.xgroup_create(stream, self._group, id="$", mkstream=True)
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
        while True:
            response = await self._redis.xreadgroup(
                self._group, self._consumer, {s: ">" for s in streams}, count=100, block=5000
            )
            for stream, entries in response or []:
                for entry_id, fields in entries:
                    await self._process(stream, entry_id, fields)

    async def _process(self, stream: str, entry_id: str, fields: dict[str, str]) -> None:
        try:
            event = CloudEvent.model_validate_json(fields["event"])
            for pattern, handler in self._subscriptions:
                if fnmatch.fnmatchcase(event.type, pattern):
                    await handler(event)
        except Exception as exc:
            log.exception("event processing failed; moving to dead-letter stream")
            await self._redis.xadd(
                f"{self._prefix}:dead",
                {"stream": stream, "entry_id": entry_id, "event": fields.get("event", ""), "error": repr(exc)},
                maxlen=10_000,
                approximate=True,
            )
        finally:
            await self._redis.xack(stream, self._group, entry_id)
