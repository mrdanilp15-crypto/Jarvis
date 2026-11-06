"""Klassifikation (Sensitivität, Komplexität) und deterministische Routing-Entscheidung (Tabelle 2.3.2)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..errors import CircuitBreaker
from .base import LLMProvider

SENSITIVE_PATTERNS = [
    r"\bkamera", r"\bvideo", r"\bgesundheit", r"\barzt", r"\bdiagnose\b", r"\bmedikament", r"\bblutdruck",
    r"\bpasswort", r"\bpin\b", r"\bzugangsdaten", r"\bkonto", r"\biban\b", r"\bkreditkarte",
]
COMPLEX_PATTERNS = [
    r"\brecherchier", r"\bvergleich", r"\bplane\b", r"\bplanung", r"\banalysier", r"\bzusammenfass",
    r"\bschreib(e)?\b.*\b(mail|brief|text|code|skript)", r"\bcode\b", r"\bskript", r"\bprogrammier",
    r"\bfehler(analyse)?\b", r"\bwarum\b", r"\berklär",
]


@dataclass(frozen=True)
class Classification:
    sensitivity: str  # public | personal | sensitive | secret
    complexity: str  # trivial | simple | complex


class HeuristicClassifier:
    """Referenz-Klassifikator. Im Betrieb ergänzt durch ein kleines lokales Modell (gleiche Ausgabe)."""

    def classify(self, text: str) -> Classification:
        lower = text.lower()
        sensitivity = "sensitive" if any(re.search(p, lower) for p in SENSITIVE_PATTERNS) else "public"
        words = len(lower.split())
        if any(re.search(p, lower) for p in COMPLEX_PATTERNS) or words > 40:
            complexity = "complex"
        elif words <= 6:
            complexity = "trivial"
        else:
            complexity = "simple"
        return Classification(sensitivity, complexity)


@dataclass(frozen=True)
class RouteDecision:
    route: str  # fast_path | confirmation_resolver | local_llm | cloud_llm
    reason: str


class ModelRouter:
    def __init__(
        self,
        *,
        local: LLMProvider,
        cloud: LLMProvider | None,
        cloud_breaker: CircuitBreaker | None = None,
        fast_path_min_confidence: float = 0.9,
    ) -> None:
        self.local = local
        self.cloud = cloud
        self.cloud_breaker = cloud_breaker or CircuitBreaker()
        self.fast_path_min_confidence = fast_path_min_confidence
        self.mode = "auto"  # auto | cloud (immer Claude, außer Sensibles) | local – umstellbar in der Oberfläche

    @property
    def override(self) -> str | None:
        return None if self.mode == "auto" else self.mode

    def decide(
        self,
        classification: Classification,
        *,
        fast_path_confidence: float | None = None,
        confirmation_reply: bool = False,
        user_override: str | None = None,
    ) -> RouteDecision:
        if fast_path_confidence is not None and fast_path_confidence >= self.fast_path_min_confidence:
            return RouteDecision("fast_path", f"grammar confidence {fast_path_confidence:.2f}")
        if confirmation_reply:
            return RouteDecision("confirmation_resolver", "pending confirmation answered")
        if classification.sensitivity in ("sensitive", "secret"):
            return RouteDecision("local_llm", f"sensitivity={classification.sensitivity}")
        cloud_ok = self.cloud is not None and self.cloud_breaker.allow()
        if user_override == "cloud" and cloud_ok:
            return RouteDecision("cloud_llm", "user override")
        if not cloud_ok:
            return RouteDecision("local_llm", "cloud unavailable (circuit open or not configured)")
        if user_override == "local" or classification.complexity in ("trivial", "simple"):
            return RouteDecision("local_llm", f"complexity={classification.complexity}")
        return RouteDecision("cloud_llm", "complexity=complex")

    def provider_for(self, decision: RouteDecision) -> LLMProvider:
        if decision.route == "cloud_llm" and self.cloud is not None:
            return self.cloud
        return self.local
