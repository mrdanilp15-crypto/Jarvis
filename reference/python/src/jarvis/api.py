"""REST- und WebSocket-API (FastAPI) gemäß api/openapi.yaml. Benötigt das Extra ``server``."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import Body, Depends, FastAPI, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .context import Situation
from .errors import JarvisError
from .events import CloudEvent, EventBus
from .llm.base import OnText
from .llm.router import HeuristicClassifier, ModelRouter
from .orchestrator import Orchestrator, TurnRequest, TurnResult
from .policy import Principal
from .webhooks import ReplayCache, verify

log = logging.getLogger(__name__)

@dataclass
class Container:
    orchestrator: Orchestrator
    bus: EventBus
    router: ModelRouter
    tokens: dict[str, Principal]  # Referenz: statische Tokens. Betrieb: OIDC-JWT-Prüfung + Geräte-Binding
    webhook_secrets: dict[str, bytes]
    situation: Callable[[Principal, str], Situation]
    classifier: HeuristicClassifier = field(default_factory=HeuristicClassifier)
    replay_cache: ReplayCache = field(default_factory=ReplayCache)

    async def run_turn(self, req: TurnRequest, *, channel: str, on_text: OnText | None = None) -> TurnResult:
        classification = self.classifier.classify(req.text)
        decision = self.router.decide(classification)
        provider = self.router.provider_for(decision)
        effort = "high" if classification.complexity == "complex" else "medium"
        situation = self.situation(req.principal, channel)
        try:
            result = await self.orchestrator.handle_turn(req, provider=provider, situation=situation,
                                                         on_text=on_text, effort=effort)
        except JarvisError as exc:
            if provider is not self.router.cloud or not exc.retryable:
                raise
            self.router.cloud_breaker.record_failure()
            # Degradierter Modus: gleiche Anfrage lokal beantworten
            return await self.orchestrator.handle_turn(req, provider=self.router.local, situation=situation,
                                                       on_text=on_text)
        if provider is self.router.cloud:
            self.router.cloud_breaker.record_success()
        return result


def turn_to_json(result: TurnResult) -> dict[str, Any]:
    out: dict[str, Any] = {
        "text": result.text,
        "route": result.route,
        "stop_reason": result.stop_reason,
        "tainted": result.tainted,
        "actions": [a.to_result_dict() for a in result.actions],
    }
    if result.pending_confirmation is not None:
        p = result.pending_confirmation
        out["pending_confirmation"] = {"confirmation_id": p.id, "method": p.method, "prompt": p.prompt,
                                       "expires_at": p.expires_at.isoformat()}
    return out


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=32000)
    mode: Literal["normal", "night", "away", "guest", "party", "vacation"] = "normal"
    channel: Literal["app", "desktop", "web", "api"] = "app"


class ConfirmationIn(BaseModel):
    decision: Literal["approve", "reject"]
    # Im Betrieb belegt die App die Methode mit einer signierten Challenge (Geräteschlüssel + Biometrie).
    method: Literal["app", "app_biometric", "pin"]


def create_app(container: Container) -> FastAPI:
    # Hinweis: FastAPI löst Annotationen per get_type_hints auf – daher Modul-Imports statt lokaler Imports.
    app = FastAPI(title="JARVIS API", version="1.0.0")

    @app.exception_handler(JarvisError)
    async def problem_handler(request: Request, exc: JarvisError) -> JSONResponse:
        return JSONResponse(exc.to_problem(instance=request.url.path), status_code=exc.status,
                            media_type="application/problem+json")

    @app.exception_handler(Exception)
    async def unexpected_handler(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", extra={"path": request.url.path})
        problem = JarvisError("JRV-SYS-001", type(exc).__name__,
                              user_message="Da ist bei mir etwas schiefgegangen.").to_problem(instance=request.url.path)
        return JSONResponse(problem, status_code=500, media_type="application/problem+json")

    def principal(authorization: str | None = Header(default=None)) -> Principal:
        token = authorization.removeprefix("Bearer ").strip() if authorization else ""
        who = container.tokens.get(token)
        if who is None:
            raise JarvisError("JRV-AUTH-001", "Bearer-Token fehlt oder ist ungültig")
        return who

    @app.post("/v1/conversations/{conversation_id}/messages")
    async def post_message(conversation_id: str, body: MessageIn, who: Principal = Depends(principal)) -> dict:
        result = await container.run_turn(
            TurnRequest(text=body.text, session_id=conversation_id, principal=who, mode=body.mode),
            channel=body.channel,
        )
        return turn_to_json(result)

    @app.post("/v1/confirmations/{confirmation_id}")
    async def resolve(confirmation_id: str, body: ConfirmationIn, who: Principal = Depends(principal)) -> dict:
        record = await container.orchestrator.resolve_confirmation(
            confirmation_id, approve=body.decision == "approve", resolver=who, method_used=body.method)
        return record.to_result_dict()

    @app.post("/v1/events", status_code=202)
    async def ingest(event: dict[str, Any] = Body(...), who: Principal = Depends(principal)) -> dict:
        # Trust und Actor bestimmt der Server aus dem Token – nie aus dem Payload.
        ce = CloudEvent.model_validate({**event, "trust": who.trust, "actor": who.actor})
        await container.bus.publish(ce)
        return {"accepted": ce.id}

    @app.post("/v1/webhooks/{hook_id}", status_code=202)
    async def webhook(hook_id: str, request: Request) -> dict:
        secret = container.webhook_secrets.get(hook_id)
        if secret is None:
            raise JarvisError("JRV-NFD-001", f"Webhook {hook_id} unbekannt")
        body = await request.body()
        verify(secret, body=body, timestamp_header=request.headers.get("x-jarvis-timestamp"),
               signature_header=request.headers.get("x-jarvis-signature"),
               delivery_id=request.headers.get("x-jarvis-delivery"), replay_cache=container.replay_cache)
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            raise JarvisError("JRV-VAL-001", "Body ist kein JSON") from exc
        event = CloudEvent(type="jarvis.webhook.received", source=f"/webhooks/{hook_id}",
                           actor=f"service:{hook_id}", trust="external_untrusted",
                           data={"webhook_id": hook_id, "payload": payload})
        await container.bus.publish(event)
        return {"accepted": event.id}

    @app.get("/v1/system/health")
    async def health() -> dict:
        return {"status": "ok", "cloud_llm": container.router.cloud_breaker.state}

    @app.websocket("/v1/stream")
    async def stream(ws: WebSocket) -> None:
        # Browser können beim WebSocket-Handshake keine Header setzen -> kurzlebiges Token als Query-Parameter
        who = container.tokens.get(ws.query_params.get("token", ""))
        if who is None:
            await ws.close(code=4401)
            return
        await ws.accept()

        async def handle(msg: dict[str, Any]) -> None:
            kind = msg.get("type")
            if kind == "input.text":
                async def on_text(delta: str) -> None:
                    await ws.send_json({"type": "output.text_delta", "delta": delta})

                result = await container.run_turn(
                    TurnRequest(text=msg["text"], session_id=msg["session_id"], principal=who),
                    channel=msg.get("channel", "app"), on_text=on_text)
                await ws.send_json({"type": "output.final", **turn_to_json(result)})
            elif kind == "confirmation.resolve":
                record = await container.orchestrator.resolve_confirmation(
                    msg["confirmation_id"], approve=msg["decision"] == "approve", resolver=who,
                    method_used=msg.get("method", "app"))
                await ws.send_json({"type": "action.update", "action": record.to_result_dict()})
            elif kind == "ping":
                await ws.send_json({"type": "pong"})
            else:
                raise JarvisError("JRV-VAL-001", f"Unbekannter Nachrichtentyp {kind}")

        try:
            while True:
                msg = await ws.receive_json()
                try:
                    await handle(msg)
                except JarvisError as exc:
                    await ws.send_json({"type": "error", "error": exc.to_problem()})
        except WebSocketDisconnect:
            return

    return app
