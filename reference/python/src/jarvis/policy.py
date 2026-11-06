"""Deterministische Policy-Engine: RBAC + ABAC + Risikoklassen.

Das LLM kann Aktionen nur *beantragen*; diese Engine entscheidet über allow / confirm / deny.
Reihenfolge der Prüfung (siehe auch config/policies.yaml):

1. R4                           -> deny
2. Rolle: Capability erlaubt?   -> sonst deny
3. Rolle: Raum erlaubt?         -> sonst deny
4. Intent-Bindung (Domänen)     -> sonst deny
5. Obergrenze aus Rolle & Trust (inkl. Stimmkonfidenz) -> sonst deny
6. Explizite Regeln (erste passende): deny | confirm | allow (allow ab R3 wird zu confirm)
7. Standard: R3 -> confirm, R2 -> confirm bei Taint/Modus, sonst allow

Explizite Regeln können damit nur *innerhalb* dessen entscheiden, was Rolle und Trust ohnehin erlauben.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Any, Literal

import yaml

RISK_ORDER = {"R0": 0, "R1": 1, "R2": 2, "R3": 3, "R4": 4}
Effect = Literal["allow", "confirm", "deny"]


def risk_gt(a: str, b: str) -> bool:
    return RISK_ORDER[a] > RISK_ORDER[b]


def risk_min(a: str, b: str) -> str:
    return a if RISK_ORDER[a] <= RISK_ORDER[b] else b


@dataclass(frozen=True)
class Principal:
    """Wer handelt – aufgelöst aus Token, Sprecher-ID oder Automations-Besitzer."""

    actor: str  # user:alex, automation:aut_x, guest:unknown, system:scheduler
    role: str  # admin | adult | child | guest | service
    trust: str  # system | trusted_user | household | guest | external_untrusted
    voice_confidence: float | None = None
    area: str | None = None
    name: str | None = None  # Anzeigename („Daniel“); die Actor-ID ist nur eine interne Kennung


@dataclass(frozen=True)
class PolicyContext:
    tainted: bool = False
    allowed_domains: frozenset[str] | None = None
    mode: str = "normal"
    now: datetime | None = None  # lokale Zeit (für Zeitfenster-Regeln)


@dataclass(frozen=True)
class Decision:
    effect: Effect
    risk_class: str
    reason: str
    rule_id: str
    method: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect": self.effect,
            "risk_class": self.risk_class,
            "reason": self.reason,
            "rule_id": self.rule_id,
            "method": self.method,
        }


@dataclass
class PolicyEngine:
    config: dict[str, Any]
    _rules: list[dict[str, Any]] = field(init=False)

    def __post_init__(self) -> None:
        self._rules = list(self.config.get("rules", []))

    @classmethod
    def from_file(cls, path: str | Path) -> PolicyEngine:
        return cls(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    # ------------------------------------------------------------------
    def evaluate(
        self,
        principal: Principal,
        capability: str,
        domain: str,
        risk: str,
        arguments: dict[str, Any],
        ctx: PolicyContext,
    ) -> Decision:
        if risk == "R4":
            return Decision("deny", risk, "Risikoklasse R4 ist nie autonom zulässig", "risk.R4")

        role_cfg = self.config.get("roles", {}).get(principal.role)
        if role_cfg is None:
            return Decision("deny", risk, f"Unbekannte Rolle {principal.role}", "role.unknown")

        if not _glob_any(capability, role_cfg.get("allow", [])) or _glob_any(capability, role_cfg.get("deny", [])):
            return Decision("deny", risk, f"Rolle {principal.role} darf {capability} nicht nutzen",
                            f"role.{principal.role}.not_allowed")

        areas = role_cfg.get("areas")
        if areas is not None and principal.area not in areas:
            return Decision("deny", risk, f"Raum {principal.area} ist für Rolle {principal.role} nicht freigegeben",
                            f"role.{principal.role}.area")

        if ctx.allowed_domains is not None and domain not in ctx.allowed_domains:
            return Decision("deny", risk, f"Domäne {domain} passt nicht zur ursprünglichen Anfrage",
                            "intent_binding")

        ceiling = self._ceiling(principal, role_cfg)
        if risk_gt(risk, ceiling):
            return Decision("deny", risk,
                            f"{risk} überschreitet die Obergrenze {ceiling} für {principal.trust}/{principal.role}",
                            f"ceiling.{principal.trust}")

        rule = self._first_matching_rule(principal, capability, arguments, ctx)
        if rule is not None:
            effect: Effect = rule["effect"]
            reason = rule.get("reason", rule["id"])
            if effect == "deny":
                return Decision("deny", risk, reason, rule["id"])
            if effect == "confirm" or risk_gt(risk, "R2"):
                method = rule.get("method") or self._method_for(risk)
                return Decision("confirm", risk, reason, rule["id"], method)
            return Decision("allow", risk, reason, rule["id"])

        if risk == "R3":
            return Decision("confirm", risk, "R3 erfordert immer eine starke Bestätigung", "risk.R3",
                            self._method_for("R3"))
        if risk == "R2":
            confirm_when = self.config.get("risk_classes", {}).get("R2", {}).get("confirm_when", {})
            if ctx.tainted and confirm_when.get("tainted", True):
                return Decision("confirm", risk, "Kontext enthält nicht vertrauenswürdige Inhalte",
                                "risk.R2.tainted", self._method_for("R2"))
            if ctx.mode in confirm_when.get("modes", []):
                return Decision("confirm", risk, f"Modus {ctx.mode} verlangt Bestätigung",
                                "risk.R2.mode", self._method_for("R2"))
        return Decision("allow", risk, "Innerhalb der Berechtigungen", f"role.{principal.role}.default")

    # ------------------------------------------------------------------
    def _ceiling(self, principal: Principal, role_cfg: dict[str, Any]) -> str:
        trust_cfg = self.config.get("trust_levels", {}).get(principal.trust, {"max_risk": "R0"})
        trust_max = trust_cfg.get("max_risk", "R0")
        min_conf = trust_cfg.get("min_voice_confidence")
        if min_conf is not None and principal.voice_confidence is not None and principal.voice_confidence < min_conf:
            trust_max = trust_cfg.get("max_risk_below_confidence", "R0")
        return risk_min(role_cfg.get("max_risk", "R0"), trust_max)

    def _method_for(self, risk: str) -> str:
        return self.config.get("confirmation", {}).get("methods", {}).get(risk, "app")

    def _first_matching_rule(
        self, principal: Principal, capability: str, arguments: dict[str, Any], ctx: PolicyContext
    ) -> dict[str, Any] | None:
        for rule in self._rules:
            if _rule_matches(rule.get("match", {}), principal, capability, arguments, ctx):
                return rule
        return None


def _glob_any(value: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(value, p) for p in patterns)


def _rule_matches(
    match: dict[str, Any], principal: Principal, capability: str, arguments: dict[str, Any], ctx: PolicyContext
) -> bool:
    if "capability" in match and not fnmatch.fnmatchcase(capability, match["capability"]):
        return False
    if "actor" in match and not fnmatch.fnmatchcase(principal.actor, match["actor"]):
        return False
    if "role" in match and principal.role != match["role"]:
        return False
    if "trust" in match:
        allowed = match["trust"] if isinstance(match["trust"], list) else [match["trust"]]
        if principal.trust not in allowed:
            return False
    if "mode" in match and ctx.mode != match["mode"]:
        return False
    for key, expected in match.get("arguments", {}).items():
        if arguments.get(key) != expected:
            return False
    if "hours" in match:
        if ctx.now is None or not in_time_window(ctx.now.time(), match["hours"]):
            return False
    return True


def in_time_window(now: time, window: str) -> bool:
    """``"22:00-07:00"`` – Fenster über Mitternacht werden unterstützt; Start inklusiv, Ende exklusiv."""
    start_s, end_s = window.split("-")
    start, end = time.fromisoformat(start_s), time.fromisoformat(end_s)
    if start <= end:
        return start <= now < end
    return now >= start or now < end
