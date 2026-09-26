"""Home-Assistant-Connector: WebSocket-Client, Event-Mapping und Smart-Home-Capabilities.

HA-WebSocket-Protokoll: auth_required -> auth -> auth_ok, danach Befehle mit fortlaufender ``id``
(``subscribe_events``, ``get_states``, ``call_service``); Antworten kommen als ``result``, Events als ``event``.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ..errors import JarvisError
from ..events import CloudEvent
from ..tools import Capability, InvocationContext, ToolRegistry, Verification

log = logging.getLogger(__name__)


class HomeApi(Protocol):
    """Von Capabilities genutzte Schnittstelle – implementiert vom echten Client und von FakeHome (Tests)."""

    def state(self, entity_id: str) -> dict[str, Any] | None: ...

    async def call_service(self, domain: str, service: str, *, target: dict[str, Any],
                           data: dict[str, Any] | None = None) -> Any: ...


class HomeAssistantClient:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        on_state_changed: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        request_timeout_s: float = 10.0,
    ) -> None:
        self.url = url
        self._token = token
        self._on_state_changed = on_state_changed
        self._timeout = request_timeout_s
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._states: dict[str, dict[str, Any]] = {}
        self._ws: Any = None
        self.connected = asyncio.Event()

    # -- Lebenszyklus ------------------------------------------------------
    async def run_forever(self) -> None:
        """Verbindet, authentifiziert, abonniert Zustandsänderungen; reconnect mit Backoff (1 s … 60 s)."""
        import websockets  # optionale Abhängigkeit

        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.url, max_size=16 * 1024 * 1024) as ws:
                    self._ws = ws
                    await self._authenticate(ws)
                    reader = asyncio.create_task(self._read_loop(ws))
                    await self._bootstrap()
                    self.connected.set()
                    backoff = 1.0
                    await reader
            except (OSError, JarvisError, websockets.ConnectionClosed) as exc:
                log.warning("home assistant connection lost", extra={"error": str(exc), "retry_in_s": backoff})
            finally:
                self.connected.clear()
                self._ws = None
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(JarvisError("JRV-INT-001", "Home Assistant getrennt"))
                self._pending.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _authenticate(self, ws: Any) -> None:
        hello = json.loads(await ws.recv())
        if hello.get("type") != "auth_required":
            raise JarvisError("JRV-INT-001", f"Unerwartete Begrüßung: {hello.get('type')}")
        await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        reply = json.loads(await ws.recv())
        if reply.get("type") != "auth_ok":
            raise JarvisError("JRV-AUTH-001", "Home Assistant hat das Token abgelehnt")

    async def _bootstrap(self) -> None:
        for state in await self.call({"type": "get_states"}):
            self._states[state["entity_id"]] = state
        await self.call({"type": "subscribe_events", "event_type": "state_changed"})

    async def _read_loop(self, ws: Any) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "result":
                future = self._pending.pop(msg["id"], None)
                if future is not None and not future.done():
                    if msg.get("success"):
                        future.set_result(msg.get("result"))
                    else:
                        error = msg.get("error", {})
                        future.set_exception(JarvisError("JRV-INT-001", f"{error.get('code')}: {error.get('message')}"))
            elif msg.get("type") == "event":
                event = msg["event"]
                if event.get("event_type") == "state_changed":
                    data = event["data"]
                    if data.get("new_state") is not None:
                        self._states[data["entity_id"]] = data["new_state"]
                    else:
                        self._states.pop(data["entity_id"], None)
                    if self._on_state_changed is not None:
                        await self._on_state_changed(data)

    # -- API ------------------------------------------------------------------
    async def call(self, payload: dict[str, Any]) -> Any:
        if self._ws is None:
            raise JarvisError("JRV-DEV-001", "Home Assistant nicht verbunden")
        msg_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        await self._ws.send(json.dumps({"id": msg_id, **payload}))
        try:
            return await asyncio.wait_for(future, timeout=self._timeout)
        except TimeoutError as exc:
            self._pending.pop(msg_id, None)
            raise JarvisError("JRV-TMO-001", f"Home Assistant antwortet nicht ({payload['type']})") from exc

    async def call_service(self, domain: str, service: str, *, target: dict[str, Any],
                           data: dict[str, Any] | None = None) -> Any:
        return await self.call({"type": "call_service", "domain": domain, "service": service,
                                "target": target, "service_data": data or {}})

    def state(self, entity_id: str) -> dict[str, Any] | None:
        return self._states.get(entity_id)


def state_changed_to_event(data: dict[str, Any], area: str | None = None) -> CloudEvent:
    """HA ``state_changed`` -> ``jarvis.sensor.state_changed`` (schemas/event.schema.json)."""

    def slim(state: dict[str, Any] | None) -> dict[str, Any] | None:
        if state is None:
            return None
        return {"state": state["state"], "attributes": state.get("attributes", {}),
                "last_changed": state.get("last_changed")}

    payload = {"entity_id": data["entity_id"], "old_state": slim(data.get("old_state")),
               "new_state": slim(data.get("new_state"))}
    if area:
        payload["area"] = area
    return CloudEvent(type="jarvis.sensor.state_changed", source="/connector/homeassistant",
                      subject=data["entity_id"], actor="system:homeassistant", trust="system", data=payload)


# ----------------------------------------------------------------------------
# Capabilities
# ----------------------------------------------------------------------------
def _entities(domain: str, max_items: int = 20) -> dict[str, Any]:
    return {"type": "array", "minItems": 1, "maxItems": max_items, "uniqueItems": True,
            "items": {"type": "string", "pattern": rf"^{domain}\.[a-z0-9_]+$"}}


def _entity(domain: str) -> dict[str, Any]:
    return {"type": "string", "pattern": rf"^{domain}\.[a-z0-9_]+$"}


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


async def _settle(ha: HomeApi, check: Callable[[], bool], timeout_s: float = 2.0) -> bool:
    """Wartet, bis der Zustands-Cache (per state_changed-Event aktualisiert) die Erwartung erfüllt."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        if check():
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.1)


def register_home_capabilities(registry: ToolRegistry, ha: HomeApi) -> None:
    def snapshot(entity_ids: list[str]) -> dict[str, Any]:
        out = {}
        for eid in entity_ids:
            st = ha.state(eid)
            if st is None:
                raise JarvisError("JRV-NFD-001", f"Unbekannte Entität {eid}",
                                  user_message=f"Das Gerät {eid} kenne ich nicht.")
            if st["state"] == "unavailable":
                raise JarvisError("JRV-DEV-001", f"{eid} ist nicht erreichbar",
                                  user_message=f"{st.get('attributes', {}).get('friendly_name', eid)} antwortet nicht.")
            out[eid] = {"state": st["state"], "brightness": st.get("attributes", {}).get("brightness")}
        return out

    # -- Lesen ---------------------------------------------------------------
    async def get_state(args: dict[str, Any], _: InvocationContext) -> Any:
        result = []
        for eid in args["entity_ids"]:
            st = ha.state(eid)
            result.append({"entity_id": eid, "state": st["state"] if st else "unknown",
                           "attributes": {k: v for k, v in (st or {}).get("attributes", {}).items()
                                          if k in ("friendly_name", "brightness", "temperature",
                                                   "current_temperature", "unit_of_measurement", "current_position")}})
        return result

    registry.register(Capability(
        name="home.get_state", domain="home", risk_class="R0",
        description="Liest den aktuellen Zustand von Smart-Home-Entitäten (Licht, Sensoren, Heizung, Fenster …).",
        input_schema=_obj({"entity_ids": {"type": "array", "minItems": 1, "maxItems": 50,
                                          "items": {"type": "string", "pattern": r"^[a-z_]+\.[a-z0-9_]+$"}}},
                          ["entity_ids"]),
        handler=get_state,
    ))

    # -- Licht ---------------------------------------------------------------
    async def set_light(args: dict[str, Any], _: InvocationContext) -> Any:
        previous = snapshot(args["entity_ids"])
        target = {"entity_id": args["entity_ids"]}
        if args["on"]:
            data = {k: args[k] for k in ("brightness_pct", "color_temp_kelvin", "rgb_color", "transition") if k in args}
            await ha.call_service("light", "turn_on", target=target, data=data)
        else:
            await ha.call_service("light", "turn_off", target=target,
                                  data={"transition": args["transition"]} if "transition" in args else None)
        return {"entity_ids": args["entity_ids"], "previous": previous}

    async def verify_light(args: dict[str, Any], _: Any) -> Verification:
        expected = "on" if args["on"] else "off"

        def ok() -> bool:
            for eid in args["entity_ids"]:
                st = ha.state(eid) or {}
                if st.get("state") != expected:
                    return False
                if args["on"] and "brightness_pct" in args:
                    brightness = st.get("attributes", {}).get("brightness")
                    if brightness is None or abs(round(brightness / 255 * 100) - args["brightness_pct"]) > 5:
                        return False
            return True

        verified = await _settle(ha, ok)
        observed = {eid: (ha.state(eid) or {}).get("state") for eid in args["entity_ids"]}
        return Verification(verified, expected={"state": expected}, observed=observed,
                            note=None if verified else "Mindestens ein Licht hat den Zielzustand nicht erreicht")

    def undo_light(args: dict[str, Any], result: Any) -> dict[str, Any] | None:
        previous_on = {v["state"] == "on" for v in result["previous"].values()}
        if len(previous_on) != 1:
            return None
        return {"capability": "home.set_light", "arguments": {"entity_ids": args["entity_ids"], "on": previous_on.pop()}}

    registry.register(Capability(
        name="home.set_light", domain="home", risk_class="R1", side_effects="reversible",
        description=("Schaltet Lichter ein oder aus und setzt optional Helligkeit (Prozent), Farbtemperatur (Kelvin) "
                     "oder RGB-Farbe. Verwende exakte entity_ids aus dem Situationskontext."),
        input_schema=_obj({
            "entity_ids": _entities("light"),
            "on": {"type": "boolean"},
            "brightness_pct": {"type": "integer", "minimum": 1, "maximum": 100},
            "color_temp_kelvin": {"type": "integer", "minimum": 2000, "maximum": 6500},
            "rgb_color": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 255},
                          "minItems": 3, "maxItems": 3},
            "transition": {"type": "number", "minimum": 0, "maximum": 60},
        }, ["entity_ids", "on"]),
        handler=set_light, verify=verify_light, undo=undo_light,
    ))

    # -- Schalter, Rollläden, Szenen, Medien ---------------------------------
    async def set_switch(args: dict[str, Any], _: InvocationContext) -> Any:
        snapshot(args["entity_ids"])
        await ha.call_service("switch", "turn_on" if args["on"] else "turn_off", target={"entity_id": args["entity_ids"]})
        return {"entity_ids": args["entity_ids"], "on": args["on"]}

    registry.register(Capability(
        name="home.set_switch", domain="home", risk_class="R1", side_effects="reversible",
        description="Schaltet Steckdosen oder Schalter ein oder aus.",
        input_schema=_obj({"entity_ids": _entities("switch"), "on": {"type": "boolean"}}, ["entity_ids", "on"]),
        handler=set_switch,
    ))

    async def set_cover(args: dict[str, Any], _: InvocationContext) -> Any:
        snapshot(args["entity_ids"])
        await ha.call_service("cover", "set_cover_position", target={"entity_id": args["entity_ids"]},
                              data={"position": args["position"]})
        return {"entity_ids": args["entity_ids"], "position": args["position"]}

    registry.register(Capability(
        name="home.set_cover", domain="home", risk_class="R1", side_effects="reversible",
        description="Fährt Rollläden oder Jalousien auf eine Position (0 = geschlossen, 100 = offen).",
        input_schema=_obj({"entity_ids": _entities("cover"),
                           "position": {"type": "integer", "minimum": 0, "maximum": 100}}, ["entity_ids", "position"]),
        handler=set_cover,
    ))

    async def activate_scene(args: dict[str, Any], _: InvocationContext) -> Any:
        await ha.call_service("scene", "turn_on", target={"entity_id": args["scene_id"]})
        return {"scene_id": args["scene_id"]}

    registry.register(Capability(
        name="home.activate_scene", domain="home", risk_class="R1", side_effects="reversible",
        description="Aktiviert eine Szene, z. B. scene.filmabend oder scene.guten_morgen.",
        input_schema=_obj({"scene_id": _entity("scene")}, ["scene_id"]),
        handler=activate_scene,
    ))

    media_services = {"play": "media_play", "pause": "media_pause", "stop": "media_stop",
                      "next": "media_next_track", "previous": "media_previous_track", "volume_set": "volume_set"}

    async def media_control(args: dict[str, Any], _: InvocationContext) -> Any:
        # Tool-Schemas bleiben ohne Top-Level-Kombinatoren (anbieterübergreifend kompatibel) -> hier prüfen
        if args["command"] == "volume_set" and "volume" not in args:
            raise JarvisError("JRV-VAL-001", "volume_set benötigt volume")
        snapshot([args["entity_id"]])
        data = {"volume_level": args["volume"]} if args["command"] == "volume_set" else None
        await ha.call_service("media_player", media_services[args["command"]],
                              target={"entity_id": args["entity_id"]}, data=data)
        return {"entity_id": args["entity_id"], "command": args["command"]}

    registry.register(Capability(
        name="home.media_control", domain="home", risk_class="R1", side_effects="reversible",
        description="Steuert Mediaplayer: play, pause, stop, next, previous oder volume_set (volume 0.0–1.0).",
        input_schema=_obj({"entity_id": _entity("media_player"),
                           "command": {"enum": list(media_services)},
                           "volume": {"type": "number", "minimum": 0, "maximum": 1}}, ["entity_id", "command"]),
        handler=media_control,
    ))

    # -- Heizung/Klima (R2) --------------------------------------------------
    async def set_climate(args: dict[str, Any], _: InvocationContext) -> Any:
        if "temperature" not in args and "hvac_mode" not in args:
            raise JarvisError("JRV-VAL-001", "temperature oder hvac_mode angeben")
        snapshot([args["entity_id"]])
        if "hvac_mode" in args:
            await ha.call_service("climate", "set_hvac_mode", target={"entity_id": args["entity_id"]},
                                  data={"hvac_mode": args["hvac_mode"]})
        if "temperature" in args:
            await ha.call_service("climate", "set_temperature", target={"entity_id": args["entity_id"]},
                                  data={"temperature": args["temperature"]})
        return {"entity_id": args["entity_id"], **{k: args[k] for k in ("temperature", "hvac_mode") if k in args}}

    registry.register(Capability(
        name="home.set_climate", domain="home", risk_class="R2", side_effects="reversible",
        description="Setzt Solltemperatur (5–30 °C) und/oder Modus (heat, cool, auto, off) eines Thermostats.",
        input_schema=_obj({"entity_id": _entity("climate"),
                           "temperature": {"type": "number", "minimum": 5, "maximum": 30},
                           "hvac_mode": {"enum": ["heat", "cool", "heat_cool", "auto", "dry", "fan_only", "off"]}},
                          ["entity_id"]),
        handler=set_climate,
    ))

    # -- Sicherheit (argumentabhängige Risikoklasse) -------------------------
    async def set_lock(args: dict[str, Any], _: InvocationContext) -> Any:
        snapshot([args["entity_id"]])
        await ha.call_service("lock", "lock" if args["state"] == "locked" else "unlock",
                              target={"entity_id": args["entity_id"]})
        return {"entity_id": args["entity_id"], "state": args["state"]}

    async def verify_lock(args: dict[str, Any], _: Any) -> Verification:
        verified = await _settle(ha, lambda: (ha.state(args["entity_id"]) or {}).get("state") == args["state"], 5.0)
        return Verification(verified, expected=args["state"], observed=(ha.state(args["entity_id"]) or {}).get("state"),
                            note=None if verified else "Schloss hat den Zielzustand nicht erreicht")

    registry.register(Capability(
        name="home.lock", domain="home", risk_class="R3", side_effects="reversible",
        risk_rules=[{"when": {"state": "locked"}, "risk_class": "R1"}],
        description="Verriegelt (locked) oder entriegelt (unlocked) ein Türschloss. Entriegeln erfordert Bestätigung.",
        input_schema=_obj({"entity_id": _entity("lock"), "state": {"enum": ["locked", "unlocked"]}},
                          ["entity_id", "state"]),
        handler=set_lock, verify=verify_lock, timeout_s=15.0,
    ))
