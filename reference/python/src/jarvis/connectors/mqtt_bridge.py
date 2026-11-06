"""MQTT-Bridge (aiomqtt): Sensor-Ingest, Satelliten-Status, Event-Fan-out und Gerätekommandos.

Topics (Präfix jarvis/v1):
  in/sensor/<quelle>/<sensor>     -> jarvis.sensor.reading      (Gerät -> JARVIS)
  satellite/<id>/status           -> jarvis.satellite.status    (retained, LWT)
  event/<typ ohne "jarvis.">      <- Fan-out ausgewählter Events (JARVIS -> Dashboards, Node-RED)
  cmd/<device_id>                 <- Kommandos an Geräte (JARVIS -> Gerät), QoS 1
  status                          <- online/offline von jarvis-core (retained, Last Will)
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from typing import Any

from ..events import CloudEvent, EventBus

log = logging.getLogger(__name__)

FANOUT_PATTERNS = ("jarvis.action.completed", "jarvis.action.failed", "jarvis.proactive.suggestion",
                   "jarvis.automation.triggered", "jarvis.task.due")


class MqttBridge:
    def __init__(
        self,
        bus: EventBus,
        *,
        host: str,
        port: int = 8883,
        username: str | None = None,
        password: str | None = None,
        tls: bool = True,
        prefix: str = "jarvis/v1",
        client_id: str = "jarvis-core",
    ) -> None:
        self.bus = bus
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.tls = tls
        self.prefix = prefix
        self.client_id = client_id
        self._client: Any = None

    async def run_forever(self) -> None:
        import aiomqtt  # optionale Abhängigkeit

        will = aiomqtt.Will(f"{self.prefix}/status", payload=json.dumps({"state": "offline"}), qos=1, retain=True)
        backoff = 1.0
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self.host, port=self.port, username=self.username, password=self.password,
                    identifier=self.client_id, will=will,
                    tls_context=ssl.create_default_context() if self.tls else None,
                ) as client:
                    self._client = client
                    backoff = 1.0
                    await client.publish(f"{self.prefix}/status", json.dumps({"state": "online"}), qos=1, retain=True)
                    await client.subscribe(f"{self.prefix}/in/#", qos=1)
                    await client.subscribe(f"{self.prefix}/satellite/+/status", qos=1)
                    async for message in client.messages:
                        await self._handle(message.topic.value, message.payload)
            except aiomqtt.MqttError as exc:
                log.warning("mqtt connection lost", extra={"error": str(exc), "retry_in_s": backoff})
            finally:
                self._client = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _handle(self, topic: str, payload: bytes | bytearray | Any) -> None:
        parts = topic.removeprefix(self.prefix + "/").split("/")
        try:
            body = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            log.warning("dropping non-JSON mqtt payload", extra={"topic": topic})
            return
        if parts[:2] == ["in", "sensor"] and len(parts) == 4 and isinstance(body, dict) and "value" in body:
            await self.bus.publish(CloudEvent(
                type="jarvis.sensor.reading", source=f"/mqtt/{parts[2]}", subject=f"sensor/{parts[2]}/{parts[3]}",
                actor=f"system:mqtt-{parts[2]}", trust="system",
                data={"sensor": parts[3], "value": body["value"], "unit": body.get("unit"), "ts": body.get("ts")},
            ))
        elif parts[0] == "satellite" and len(parts) == 3 and parts[2] == "status":
            await self.bus.publish(CloudEvent(
                type="jarvis.satellite.status", source=f"/satellite/{parts[1]}", subject=parts[1],
                actor=f"system:satellite-{parts[1]}", trust="system", data=body,
            ))

    async def fanout(self, event: CloudEvent) -> None:
        """Als Bus-Abonnent registrieren: bus.subscribe("jarvis.*", bridge.fanout)."""
        if self._client is None or event.type not in FANOUT_PATTERNS:
            return
        topic = f"{self.prefix}/event/{event.type.removeprefix('jarvis.')}"
        await self._client.publish(topic, event.model_dump_json(exclude_none=True), qos=1)

    async def command(self, device_id: str, payload: dict[str, Any]) -> None:
        if self._client is None:
            raise ConnectionError("MQTT nicht verbunden")
        await self._client.publish(f"{self.prefix}/cmd/{device_id}", json.dumps(payload), qos=1)
