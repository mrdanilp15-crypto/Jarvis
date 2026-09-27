"""Orchestrator: Agent-Loop mit Policy-Gate, Bestätigungen, Taint-Tracking, Verifikation und Audit.

Einziger Weg zu einer Aktion ist ``request_action`` – genutzt vom LLM-Loop, vom Fast-Path, von Automationen
und von der REST-API. Damit gilt die Policy-Engine überall gleich.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .context import ContextBuilder, Situation
from .errors import JarvisError
from .events import CloudEvent, EventBus, new_id
from .fastpath import FastPath, confirmation_reply
from .llm.base import (
    AssistantTurn,
    LLMProvider,
    OnText,
    ToolCall,
    ToolResult,
    ToolResultsTurn,
    Turn,
    UserTurn,
)
from .memory import MemoryService
from .policy import Decision, PolicyContext, PolicyEngine, Principal
from .tools import Capability, InvocationContext, ToolRegistry

log = logging.getLogger(__name__)

STRONG_METHODS = {"app", "app_biometric", "pin"}


# ----------------------------------------------------------------------------
# Datenobjekte
# ----------------------------------------------------------------------------
@dataclass
class ActionRecord:
    action_id: str
    capability: str
    arguments: dict[str, Any]
    risk_class: str
    decision: Decision
    status: str  # succeeded | failed | denied | pending_confirmation | rejected | timed_out
    result: Any = None
    error: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    undo: dict[str, Any] | None = None
    confirmation_id: str | None = None

    def to_result_dict(self) -> dict[str, Any]:
        """Serialisierung gemäß schemas/action-result.schema.json."""
        out: dict[str, Any] = {
            "action_id": self.action_id,
            "capability": self.capability,
            "status": self.status,
            "decision": self.decision.to_dict(),
        }
        for key in ("result", "error", "verification", "undo"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


@dataclass
class PendingConfirmation:
    id: str
    record: ActionRecord
    principal: Principal
    session_id: str | None
    correlation_id: str
    method: str
    prompt: str
    expires_at: datetime


@dataclass
class TurnRequest:
    text: str
    session_id: str
    principal: Principal
    correlation_id: str = field(default_factory=lambda: new_id("cor"))
    mode: str = "normal"
    allowed_domains: frozenset[str] | None = None
    user_display: str | None = None


@dataclass
class TurnResult:
    text: str
    route: str
    actions: list[ActionRecord] = field(default_factory=list)
    pending_confirmation: PendingConfirmation | None = None
    stop_reason: str = "end_turn"
    tainted: bool = False


@dataclass
class Session:
    id: str
    transcript: list[Turn] = field(default_factory=list)
    tainted: bool = False
    last_active: datetime = field(default_factory=lambda: datetime.now(UTC))


class AuditSink(Protocol):
    async def record(self, event: str, **fields: Any) -> None: ...


class MemoryAuditSink:
    """Für Tests/Einzelplatz. Im Betrieb: Insert in jarvis.audit_log (hash-verkettet per Trigger)."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    async def record(self, event: str, **fields: Any) -> None:
        self.entries.append({"event": event, "ts": datetime.now(UTC).isoformat(), **fields})


class ConfirmationStore:
    def __init__(self, timeout_s: int = 60) -> None:
        self.timeout_s = timeout_s
        self._pending: dict[str, PendingConfirmation] = {}

    def create(self, record: ActionRecord, principal: Principal, session_id: str | None,
               correlation_id: str, method: str, prompt: str) -> PendingConfirmation:
        pending = PendingConfirmation(
            id=new_id("cnf"), record=record, principal=principal, session_id=session_id,
            correlation_id=correlation_id, method=method, prompt=prompt,
            expires_at=datetime.now(UTC) + timedelta(seconds=self.timeout_s),
        )
        self._pending[pending.id] = pending
        return pending

    def for_session(self, session_id: str) -> PendingConfirmation | None:
        self._expire()
        matches = [p for p in self._pending.values() if p.session_id == session_id]
        return matches[-1] if matches else None

    def get(self, confirmation_id: str) -> PendingConfirmation | None:
        self._expire()
        return self._pending.get(confirmation_id)

    def pop(self, confirmation_id: str) -> PendingConfirmation | None:
        return self._pending.pop(confirmation_id, None)

    def _expire(self) -> None:
        now = datetime.now(UTC)
        for key in [k for k, p in self._pending.items() if p.expires_at <= now]:
            self._pending[key].record.status = "timed_out"
            del self._pending[key]


# ----------------------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------------------
class Orchestrator:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        policy: PolicyEngine,
        context: ContextBuilder,
        audit: AuditSink,
        confirmations: ConfirmationStore | None = None,
        fast_path: FastPath | None = None,
        memory: MemoryService | None = None,
        bus: EventBus | None = None,
        max_iterations: int = 8,
        turn_timeout_s: float = 60.0,
        session_idle_timeout_s: float = 300.0,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.context = context
        self.audit = audit
        self.confirmations = confirmations or ConfirmationStore()
        self.fast_path = fast_path
        self.memory = memory
        self.bus = bus
        self.max_iterations = max_iterations
        self.turn_timeout_s = turn_timeout_s
        self.session_idle_timeout_s = session_idle_timeout_s
        self.sessions: dict[str, Session] = {}

    # ------------------------------------------------------------------
    # Einstieg für Sprache/Text
    # ------------------------------------------------------------------
    async def handle_turn(
        self,
        req: TurnRequest,
        *,
        provider: LLMProvider,
        situation: Situation,
        on_text: OnText | None = None,
        effort: str | None = None,
    ) -> TurnResult:
        now = datetime.now(UTC)
        session = self.sessions.get(req.session_id)
        if session is None or (now - session.last_active).total_seconds() > self.session_idle_timeout_s:
            # Neuer Dialog-Kontext: Verlauf und Taint werden zurückgesetzt, das Langzeitgedächtnis bleibt.
            session = self.sessions[req.session_id] = Session(req.session_id)
        session.last_active = now

        # 1) Offene Bestätigung? Nur deterministisch auflösen – nie über das LLM.
        pending = self.confirmations.for_session(req.session_id)
        if pending is not None:
            reply = confirmation_reply(req.text)
            if reply is not None:
                return await self._resolve_by_voice(pending, req, approve=reply)

        # 2) Fast-Path ohne LLM
        if self.fast_path is not None and (match := self.fast_path.match(req.text)):
            record = await self.request_action(
                capability=match.capability, arguments=match.arguments, principal=req.principal,
                correlation_id=req.correlation_id, session_id=req.session_id, via="fast_path",
                ctx=PolicyContext(tainted=False, allowed_domains=None, mode=req.mode, now=situation.now),
            )
            return self._fast_path_result(record)

        # 3) LLM-Agent-Loop
        start = len(session.transcript)
        try:
            return await asyncio.wait_for(
                self._agent_loop(req, session, provider, situation, on_text, effort), timeout=self.turn_timeout_s
            )
        except TimeoutError:
            await self.audit.record("turn.timeout", correlation_id=req.correlation_id, actor=req.principal.actor)
            del session.transcript[start:]  # halbfertige Tool-Runden verwerfen; Aktionen stehen im Audit-Log
            return TurnResult(text="Das dauert länger als erwartet; ich habe den Vorgang abgebrochen.",
                              route="llm", stop_reason="timeout", tainted=session.tainted)
        except BaseException:
            # z. B. Provider-Ausfall: Verlauf zurücksetzen, damit ein Retry (anderer Provider) sauber startet
            del session.transcript[start:]
            raise

    async def _agent_loop(
        self, req: TurnRequest, session: Session, provider: LLMProvider, situation: Situation,
        on_text: OnText | None, effort: str | None,
    ) -> TurnResult:
        memories: list[str] = []
        if self.memory is not None:
            user_id = req.principal.actor.split(":", 1)[1] if req.principal.actor.startswith("user:") else None
            try:
                # Sensible Erinnerungen nur für lokale Modelle
                memories = await self.memory.recall(req.text, user_id=user_id,
                                                    include_sensitive=getattr(provider, "is_local", False))
            except Exception:  # degradierter Modus: ohne Erinnerungen weiterarbeiten
                log.warning("memory recall failed; continuing without memories", exc_info=True,
                            extra={"correlation_id": req.correlation_id})
        system = self.context.system_prompt(situation, memories)
        session.transcript.append(UserTurn(req.text, context=system.dynamic))
        tools = self.registry.tool_specs(req.allowed_domains)
        result = TurnResult(text="", route=f"llm:{provider.name}", tainted=session.tainted)

        for _ in range(self.max_iterations):
            transcript = self.context.trim_history(session.transcript)
            response = await provider.complete(system=system, transcript=transcript, tools=tools,
                                               on_text=on_text, effort=effort)

            if response.stop_reason in ("refusal", "max_tokens") and response.tool_calls:
                # Abgeschnittene oder abgelehnte Tool-Aufrufe niemals ausführen; Verlauf konsistent halten.
                result.text = "Das konnte ich nicht vollständig verarbeiten. Bitte formulieren Sie es anders."
                session.transcript.append(AssistantTurn(text=result.text, tool_calls=[], provider="system"))
                result.stop_reason = response.stop_reason
                return result

            session.transcript.append(response.assistant_turn)
            if not response.tool_calls:
                result.text = response.text
                result.stop_reason = response.stop_reason
                result.tainted = session.tainted
                return result

            outcomes = await asyncio.gather(
                *(self._invoke_tool(call, req, session, situation) for call in response.tool_calls)
            )
            results: list[ToolResult] = []
            for tool_result, record, pending in outcomes:
                results.append(tool_result)
                if record is not None:
                    result.actions.append(record)
                if pending is not None:
                    result.pending_confirmation = pending
            session.transcript.append(ToolResultsTurn(results))  # alle Ergebnisse in *einer* Nachricht

        result.text = "Ich habe die Aufgabe nach mehreren Schritten angehalten, um nichts Unbeabsichtigtes zu tun."
        result.stop_reason = "max_iterations"
        result.tainted = session.tainted
        return result

    # ------------------------------------------------------------------
    # Tool-Aufruf durch das LLM
    # ------------------------------------------------------------------
    async def _invoke_tool(
        self, call: ToolCall, req: TurnRequest, session: Session, situation: Situation
    ) -> tuple[ToolResult, ActionRecord | None, PendingConfirmation | None]:
        cap_name = ToolRegistry.capability_name(call.name)
        cap = self.registry.get(cap_name)
        if cap is None:
            return self._tool_error(call, "JRV-NFD-001", f"Unbekanntes Tool {call.name}"), None, None

        errors = cap.validate(call.arguments)
        if errors:
            # Auch abgeschnittene/ungültige Eingaben (eager input streaming) landen hier.
            err = JarvisError("JRV-VAL-002", "Tool-Eingabe verletzt das Schema", errors=errors)
            return ToolResult(call.id, call.name, json.dumps({"INVALID_INPUT": err.to_problem()}, ensure_ascii=False),
                              is_error=True), None, None

        record = await self.request_action(
            capability=cap_name, arguments=call.arguments, principal=req.principal,
            correlation_id=req.correlation_id, session_id=req.session_id, via="llm",
            ctx=PolicyContext(tainted=session.tainted, allowed_domains=req.allowed_domains,
                              mode=req.mode, now=situation.now),
        )
        pending = self.confirmations.get(record.confirmation_id) if record.confirmation_id else None

        if record.status == "pending_confirmation" and pending is not None:
            payload = {
                "status": "confirmation_required",
                "confirmation_id": pending.id,
                "method": pending.method,
                "instruction": ("Bitte den Nutzer um Bestätigung per App." if pending.method in STRONG_METHODS
                                else "Frage den Nutzer mit Ja/Nein. Führe die Aktion nicht erneut aus."),
            }
            return ToolResult(call.id, call.name, json.dumps(payload, ensure_ascii=False)), record, pending
        if record.status == "denied":
            payload = {"status": "denied", "reason": record.decision.reason}
            return ToolResult(call.id, call.name, json.dumps(payload, ensure_ascii=False), is_error=True), record, None
        if record.status != "succeeded":
            return ToolResult(call.id, call.name, json.dumps({"status": record.status, "error": record.error},
                                                             ensure_ascii=False), is_error=True), record, None

        content = json.dumps({"status": "succeeded", "result": record.result,
                              "verification": record.verification}, ensure_ascii=False, default=str)
        if cap.output_trust == "untrusted":
            session.tainted = True  # ab jetzt: Aktionen >= R2 nur mit Bestätigung
            content = f'<untrusted_content source="{cap.name}">\n{content}\n</untrusted_content>'
        return ToolResult(call.id, call.name, content), record, None

    def _tool_error(self, call: ToolCall, code: str, detail: str) -> ToolResult:
        return ToolResult(call.id, call.name, json.dumps(JarvisError(code, detail).to_problem(), ensure_ascii=False),
                          is_error=True)

    # ------------------------------------------------------------------
    # Zentrale Aktionsschnittstelle
    # ------------------------------------------------------------------
    async def request_action(
        self,
        *,
        capability: str,
        arguments: dict[str, Any],
        principal: Principal,
        correlation_id: str,
        session_id: str | None,
        via: str,
        ctx: PolicyContext,
        dry_run: bool = False,
    ) -> ActionRecord:
        cap = self.registry.get(capability)
        if cap is None:
            raise JarvisError("JRV-NFD-001", f"Capability {capability} ist nicht registriert")
        errors = cap.validate(arguments)
        if errors:
            raise JarvisError("JRV-VAL-001", f"Ungültige Argumente für {capability}", errors=errors)

        risk = cap.risk_for(arguments)
        decision = self.policy.evaluate(principal, cap.name, cap.domain, risk, arguments, ctx)
        record = ActionRecord(action_id=new_id("act"), capability=capability, arguments=arguments,
                              risk_class=risk, decision=decision, status="denied")
        await self.audit.record("policy.decision", correlation_id=correlation_id, actor=principal.actor,
                                trust=principal.trust, via=via, capability=capability, arguments=arguments,
                                tainted=ctx.tainted, **decision.to_dict())

        if decision.effect == "deny":
            record.error = JarvisError("JRV-POL-002", decision.reason).to_problem(correlation_id)
            return record
        if decision.effect == "confirm":
            record.status = "pending_confirmation"
            pending = self.confirmations.create(record, principal, session_id, correlation_id, decision.method,
                                                prompt=self._confirmation_prompt(cap, arguments))
            record.confirmation_id = pending.id
            await self._publish("jarvis.confirmation.requested", correlation_id, principal,
                                {"confirmation_id": pending.id, "action": record.to_result_dict(),
                                 "method": pending.method, "prompt": pending.prompt,
                                 "expires_at": pending.expires_at.isoformat()})
            return record
        if dry_run:
            record.status = "succeeded"
            record.result = {"dry_run": True}
            return record
        return await self._execute(cap, record, principal, correlation_id, session_id)

    async def resolve_confirmation(self, confirmation_id: str, *, approve: bool, resolver: Principal,
                                   method_used: str) -> ActionRecord:
        """Auflösung über einen authentifizierten Kanal (App, PIN, Sprache desselben Sprechers)."""
        pending = self.confirmations.get(confirmation_id)
        if pending is None:
            raise JarvisError("JRV-POL-001", "Bestätigung unbekannt oder abgelaufen")
        if resolver.actor != pending.principal.actor and resolver.role != "admin":
            raise JarvisError("JRV-POL-002", "Nur die anfragende Person (oder ein Admin) kann bestätigen")
        if approve and pending.method in STRONG_METHODS and method_used not in STRONG_METHODS:
            raise JarvisError("JRV-POL-002", f"Diese Aktion verlangt eine Bestätigung per {pending.method}")
        self.confirmations.pop(confirmation_id)
        record = pending.record
        await self.audit.record("confirmation.resolved", correlation_id=pending.correlation_id,
                                actor=resolver.actor, confirmation_id=confirmation_id, approved=approve,
                                method=method_used, capability=record.capability)
        if not approve:
            record.status = "rejected"
            return record
        cap = self.registry.get(record.capability)
        assert cap is not None
        return await self._execute(cap, record, pending.principal, pending.correlation_id, pending.session_id)

    async def _resolve_by_voice(self, pending: PendingConfirmation, req: TurnRequest, *, approve: bool) -> TurnResult:
        if approve and pending.method in STRONG_METHODS:
            return TurnResult(text="Für diese Aktion benötige ich Ihre Bestätigung in der App.",
                              route="confirmation_resolver", pending_confirmation=pending)
        if req.principal.actor != pending.principal.actor:
            return TurnResult(text="Diese Bestätigung muss von der Person kommen, die den Auftrag gegeben hat.",
                              route="confirmation_resolver", pending_confirmation=pending)
        record = await self.resolve_confirmation(pending.id, approve=approve, resolver=req.principal,
                                                 method_used="voice")
        text = {"succeeded": "Erledigt.", "rejected": "In Ordnung, ich habe es abgebrochen."}.get(
            record.status, "Das hat leider nicht funktioniert.")
        return TurnResult(text=text, route="confirmation_resolver", actions=[record])

    async def _execute(self, cap: Capability, record: ActionRecord, principal: Principal,
                       correlation_id: str, session_id: str | None) -> ActionRecord:
        invocation = InvocationContext(correlation_id=correlation_id, actor=principal.actor,
                                       session_id=session_id, area=principal.area)
        try:
            record.result = await asyncio.wait_for(cap.handler(record.arguments, invocation), timeout=cap.timeout_s)
            record.status = "succeeded"
            if cap.verify is not None:
                verification = await cap.verify(record.arguments, record.result)
                record.verification = verification.to_dict()
                if not verification.verified:
                    record.status = "failed"
                    record.error = JarvisError("JRV-DEV-002", verification.note).to_problem(correlation_id)
            if record.status == "succeeded" and cap.undo is not None:
                record.undo = cap.undo(record.arguments, record.result)
        except TimeoutError:
            record.status = "timed_out"
            record.error = JarvisError("JRV-TMO-001", f"{cap.name} nach {cap.timeout_s}s").to_problem(correlation_id)
        except JarvisError as exc:
            record.status = "failed"
            record.error = exc.to_problem(correlation_id)
        except Exception as exc:  # unerwartet: loggen, aber keine Interna an das LLM geben
            log.exception("capability failed", extra={"capability": cap.name, "correlation_id": correlation_id})
            record.status = "failed"
            record.error = JarvisError("JRV-SYS-001", type(exc).__name__).to_problem(correlation_id)

        await self.audit.record("action.executed", correlation_id=correlation_id, actor=principal.actor,
                                capability=cap.name, action_id=record.action_id, status=record.status,
                                error_code=(record.error or {}).get("code"))
        event_type = "jarvis.action.completed" if record.status == "succeeded" else "jarvis.action.failed"
        await self._publish(event_type, correlation_id, principal, record.to_result_dict())
        return record

    # ------------------------------------------------------------------
    async def _publish(self, event_type: str, correlation_id: str, principal: Principal, data: Any) -> None:
        if self.bus is None:
            return
        await self.bus.publish(CloudEvent(
            type=event_type, source="/core/orchestrator", correlationid=correlation_id, actor=principal.actor,
            trust=principal.trust, data=data,
        ))

    def _confirmation_prompt(self, cap: Capability, arguments: dict[str, Any]) -> str:
        return f"{cap.description.split('.')[0]}: {json.dumps(arguments, ensure_ascii=False)} – ausführen?"

    def _fast_path_result(self, record: ActionRecord) -> TurnResult:
        if record.status == "succeeded":
            text = "Erledigt."
        elif record.status == "pending_confirmation":
            text = "Soll ich das wirklich tun?"
        elif record.status == "denied":
            text = f"Das darf ich nicht: {record.decision.reason}."
        else:
            text = ((record.error or {}).get("user_message")
                    or "Das hat leider nicht funktioniert.")
        pending = self.confirmations.get(record.confirmation_id) if record.confirmation_id else None
        return TurnResult(text=text, route="fast_path", actions=[record], pending_confirmation=pending)
