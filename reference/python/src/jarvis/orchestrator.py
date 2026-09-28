"""Orchestrator: Agent-Loop mit Policy-Gate, Bestätigungen, Taint-Tracking, Verifikation und Audit.

Einziger Weg zu einer Aktion ist ``request_action`` – genutzt vom LLM-Loop, vom Fast-Path, von Automationen
und von der REST-API. Damit gilt die Policy-Engine überall gleich.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .context import ContextBuilder, Situation
from .errors import JarvisError
from .events import CloudEvent, EventBus, new_id
from .fastpath import (
    FastPath,
    FastPathMatch,
    bare_term,
    confirmation_reply,
    conversation_intent,
    correction_term,
    selection_reply,
)
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
from .style import JarvisStyle, PlainStyle
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
    awaiting_reply: bool = False  # JARVIS hat nachgefragt – die Oberfläche hört direkt wieder zu


@dataclass
class Offer:
    """Zuletzt genannte Trefferliste (Dateien oder Links), aus der der Nutzer im nächsten Satz wählen kann."""

    kind: str  # files | web
    items: list[dict[str, Any]]
    affirm: bool = False  # „ja“ = erster Eintrag


# Capabilities, deren Trefferliste JARVIS zur Auswahl anbietet („die zweite“, „ja“)
OFFER_SOURCES = {"pc.find_files": "files", "pc.search_files": "files", "web.search": "web", "mail.list_unread": "mail"}
# Was die Auswahl auslöst: Art -> (Capability, Argument aus dem Listeneintrag)
OFFER_ACTIONS = {"files": ("pc.open_file", "id"), "web": ("pc.open_url", "url"), "mail": ("mail.read", "id")}
# Suchen und Öffnen, die ein kurzer Folgesatz korrigieren kann („ARTERIION“, „ich meinte Steam“): Capability -> Feld
CORRECTABLE = {"pc.search_web": "query", "pc.open_link": "query", "pc.search_files": "query", "pc.find_files": "query",
               "web.search": "query", "pc.open_app": "app"}
CORRECTION_WINDOW_S = 300


@dataclass
class Session:
    id: str
    transcript: list[Turn] = field(default_factory=list)
    tainted: bool = False
    last_active: datetime = field(default_factory=lambda: datetime.now(UTC))
    offer: Offer | None = None  # gilt nur für den direkt folgenden Satz
    expect: str | None = None  # Befehlsanfang nach einer Rückfrage („such nach“), ergänzt um den nächsten Satz
    last_search: tuple[str, dict[str, Any], datetime] | None = None  # für Korrekturen im nächsten Satz
    recent_terms: list[str] = field(default_factory=list)  # zuletzt allein geschriebene Begriffe („ARTERIION“)


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
        style: PlainStyle | None = None,
        app_resolver: Callable[[str], str | None] | None = None,
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
        # Formatter: aktive Persona bestimmt den Ton (Jarvis-Stil-Engine oder neutral)
        self.style = style or JarvisStyle.from_persona(context.persona)
        # Welches installierte Programm meint „Öffne Steam“? (PC-Agent: eigene Liste + Windows-Startmenü)
        self.app_resolver = app_resolver

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
        """Einziger Ausgang: jede Antwort läuft durch den Formatter (Streaming satzweise, Endfassung komplett)."""
        stream = self.style.stream(on_text)
        result = await self._handle(req, provider=provider, situation=situation, stream=stream, effort=effort)
        await stream.flush()
        result.text = self.style.finalize(result.text)
        session = self.sessions.get(req.session_id)
        result.awaiting_reply = bool(session and (session.expect or (session.offer and session.offer.affirm)))
        return result

    async def _handle(
        self,
        req: TurnRequest,
        *,
        provider: LLMProvider,
        situation: Situation,
        stream: Any,
        effort: str | None,
    ) -> TurnResult:
        """Pipeline: Bestätigung -> Intent-Erkennung -> Kontext-Interpretation -> Ausführung bzw. LLM."""
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

        # Auswahl aus der eben genannten Liste („die zweite“, „ja“) – nur im direkt folgenden Satz
        offer, session.offer = session.offer, None
        choice = selection_reply(req.text, len(offer.items), affirm=offer.affirm) if offer is not None else None
        if offer is not None and choice is not None:
            item = offer.items[choice - 1]
            capability, key = OFFER_ACTIONS[offer.kind]
            match = FastPathMatch(capability, {key: item[key]}, 0.95, "selection")
            if self.registry.get(match.capability) is not None:
                # Der Nutzer hat den Eintrag selbst gewählt: kein Taint, auch wenn die Liste aus dem Web stammt
                record = await self.request_action(
                    capability=match.capability, arguments=match.arguments, principal=req.principal,
                    correlation_id=req.correlation_id, session_id=req.session_id, via="fast_path",
                    ctx=PolicyContext(tainted=False, allowed_domains=None, mode=req.mode, now=situation.now),
                )
                return self._fast_path_result(record, {"title": item.get("title")})

        # 2) Intent-Erkennung: Gesprächs-Intents und Befehle deterministisch, ohne LLM
        intent = conversation_intent(req.text)
        match = (self.fast_path.match(req.text, default_area=req.principal.area, now=situation.now)
                 if self.fast_path else None)
        expect, session.expect = session.expect, None
        if expect and self.fast_path and (match is None or match.grammar == "incomplete") and intent is None:
            # Antwort auf „Wonach soll ich suchen?“: „Arteriion auf Spotify“ -> „such nach Arteriion auf Spotify“
            match = self.fast_path.match(f"{expect} {req.text}", default_area=req.principal.area,
                                         now=situation.now) or match
        match = self._correct(match, intent, req.text, session, now)
        if match is not None and match.grammar == "refuse_password":
            return TurnResult(text=self.style.system_text("no_passwords"), route="fast_path")
        if match is not None and match.grammar == "incomplete":
            session.expect = match.slots["prefix"]
            return TurnResult(text=self.style.clarify(match.slots["kind"], match.slots.get("site")),
                              route="fast_path")
        if intent == "how_are_you" and self.registry.get("system.status") is not None:
            match = FastPathMatch("system.status", {}, 0.9, "how_are_you", {"intro": "how_are_you"})
        elif intent is not None and match is None:
            return TurnResult(text=self.style.conversation(intent, situation, capabilities=self.registry.names()),
                              route="conversation")
        if match is not None and match.grammar.endswith("_unavailable"):
            if self.registry.get(match.capability) is None:
                return TurnResult(text=self.style.unavailable(match.capability), route="fast_path")
            match = None  # Haus verbunden, aber ohne Raumzuordnung: das LLM klärt, welches Gerät gemeint ist
        if match is not None and match.grammar == "pc_open_guess":
            # Unbekannter Name („Öffne Steam“): nur direkt starten, wenn der PC ein passendes Programm hat –
            # sonst ist womöglich gar kein Programm gemeint („Öffne die Einkaufsliste“), das klärt das LLM.
            # Namen ohne Artikel („Öffne Chefkoch“) sind oft Webseiten: dann der beste Treffer im Browser.
            app = None
            if self.registry.get(match.capability) is not None:
                app = self.app_resolver(match.arguments["app"]) if self.app_resolver else match.arguments["app"]
            if app:
                match = replace(match, arguments={"app": app})
            elif match.slots.get("web_fallback") and self.registry.get("pc.open_link") is not None:
                match = FastPathMatch("pc.open_link", {"query": match.slots["target"]}, 0.8, "pc_open_link_fallback",
                                      {"query": match.slots["target"]})
            else:
                match = None
        if match is not None:
            # 3) Kontext-Interpretation: fehlende Angaben aus der Situation ergänzen (Ort fürs Wetter usw.)
            arguments = self._interpret(match, situation)
            if self.registry.get(match.capability) is None:
                return TurnResult(text=self.style.unavailable(match.capability), route="fast_path")
            record = await self.request_action(
                capability=match.capability, arguments=arguments, principal=req.principal,
                correlation_id=req.correlation_id, session_id=req.session_id, via="fast_path",
                ctx=PolicyContext(tainted=False, allowed_domains=None, mode=req.mode, now=situation.now),
            )
            return self._fast_path_result(record, match.slots)

        # 4) LLM-Agent-Loop (Antwort wird satzweise durch den Formatter gestreamt)
        start = len(session.transcript)
        try:
            return await asyncio.wait_for(
                self._agent_loop(req, session, provider, situation, stream, effort), timeout=self.turn_timeout_s
            )
        except TimeoutError:
            await self.audit.record("turn.timeout", correlation_id=req.correlation_id, actor=req.principal.actor)
            del session.transcript[start:]  # halbfertige Tool-Runden verwerfen; Aktionen stehen im Audit-Log
            return TurnResult(text=self.style.system_text("timeout"),
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
            if hasattr(on_text, "flush"):
                await on_text.flush()  # Text vor Tool-Aufrufen vollständig ausgeben
            if response.stop_reason in ("refusal", "max_tokens") and response.tool_calls:
                # Abgeschnittene oder abgelehnte Tool-Aufrufe niemals ausführen; Verlauf konsistent halten.
                result.text = self.style.system_text("not_processed")
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

        result.text = self.style.system_text("max_iterations")
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
                                                prompt=self.style.confirmation_prompt(cap, arguments, decision.method))
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
        record = await self._execute(cap, record, principal, correlation_id, session_id)
        self._remember(session_id, record, via)
        return record

    def _correct(self, match: FastPathMatch | None, intent: str | None, text: str, session: Session,
                 now: datetime) -> FastPathMatch | None:
        """Folgesätze zur letzten Suche: Korrektur („ARTERIION“, „ich meinte …“) und Rückbezug („so wie ich es
        geschrieben habe“). Beides gilt nur für den direkt folgenden Satz."""
        last, session.last_search = session.last_search, None
        if last is not None and (now - last[2]).total_seconds() > CORRECTION_WINDOW_S:
            last = None
        if match is None and intent is None and last is not None and (
                found := correction_term(text, explicit=last[0] == "pc.open_app")):
            capability, arguments, _ = last
            term, site = found
            arguments = {**arguments, CORRECTABLE[capability]: term}
            if site and capability == "pc.search_web":
                arguments.pop("site", None)
                arguments.update({"site": site} if site != "google" else {})
            match = FastPathMatch(capability, arguments, 0.9, "correction", {"query": term})
        elif match is not None and match.slots.get("refers_back") and session.recent_terms:
            # „Such nach …, so wie ich es geschrieben habe“: das zuletzt allein geschriebene Wort
            arguments = {**match.arguments, "query": session.recent_terms[-1]}
            if last is not None and last[0] == match.capability and "site" not in arguments and last[1].get("site"):
                arguments["site"] = last[1]["site"]
            match = replace(match, arguments=arguments)
        if term := bare_term(text):
            session.recent_terms = [*session.recent_terms, term][-3:]
        return match

    def _remember(self, session_id: str | None, record: ActionRecord, via: str) -> None:
        """Für den nächsten Satz merken: die Suche (für Korrekturen) und die Trefferliste („die zweite“).
        „Ja“ gilt nur, wenn JARVIS selbst gefragt hat (Sofortbefehl) – nach einer LLM-Antwort kann ein „Ja“ auch
        etwas anderes meinen."""
        session = self.sessions.get(session_id) if session_id else None
        key = CORRECTABLE.get(record.capability)
        if session is not None and key and record.status == "succeeded" and record.arguments.get(key):
            session.last_search = (record.capability, dict(record.arguments), datetime.now(UTC))
        kind = OFFER_SOURCES.get(record.capability)
        if session is None or kind is None or record.status != "succeeded" or not isinstance(record.result, dict):
            return
        items = [i for i in record.result.get("results") or [] if isinstance(i, dict)]
        valid = {"files": lambda i: isinstance(i.get("id"), int),
                 "web": lambda i: str(i.get("url", "")).startswith(("https://", "http://")),
                 "mail": lambda i: str(i.get("id", "")).isdigit()}[kind]
        if items and all(valid(i) for i in items):
            session.offer = Offer(kind, items, affirm=via == "fast_path")

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
            return TurnResult(text=self.style.system_text("confirm_in_app"),
                              route="confirmation_resolver", pending_confirmation=pending)
        if req.principal.actor != pending.principal.actor:
            return TurnResult(text=self.style.system_text("confirm_same_person"),
                              route="confirmation_resolver", pending_confirmation=pending)
        record = await self.resolve_confirmation(pending.id, approve=approve, resolver=req.principal,
                                                 method_used="voice")
        text = self.style.action_reply(record, via_confirmation=True)
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

    def _interpret(self, match: FastPathMatch, situation: Situation) -> dict[str, Any]:
        """Kontext-Interpretation: Angaben ergänzen, die der Nutzer nicht nennen muss."""
        arguments = dict(match.arguments)
        if match.capability == "assistant.day_plan" and situation.location:
            arguments.setdefault("location", situation.location)
        return arguments

    def _fast_path_result(self, record: ActionRecord, slots: dict[str, Any] | None = None) -> TurnResult:
        pending = self.confirmations.get(record.confirmation_id) if record.confirmation_id else None
        slots = {**(slots or {}), **({"prompt": pending.prompt} if pending else {})}
        text = self.style.action_reply(record, slots)
        return TurnResult(text=text, route="fast_path", actions=[record], pending_confirmation=pending)
