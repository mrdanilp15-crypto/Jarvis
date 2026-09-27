"""Tool-Registry: Capabilities mit JSON-Schema, Risikoklasse, Seiteneffekten und Handler."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from .llm.base import ToolSpec
from .policy import RISK_ORDER


@dataclass(frozen=True)
class InvocationContext:
    correlation_id: str
    actor: str
    session_id: str | None = None
    area: str | None = None
    locale: str = "de-DE"


@dataclass(frozen=True)
class Verification:
    verified: bool
    expected: Any = None
    observed: Any = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"verified": self.verified}
        for key in ("expected", "observed", "note"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        return out


Handler = Callable[[dict[str, Any], InvocationContext], Awaitable[Any]]
Verifier = Callable[[dict[str, Any], Any], Awaitable[Verification]]
UndoBuilder = Callable[[dict[str, Any], Any], dict[str, Any] | None]


@dataclass
class Capability:
    name: str  # kanonisch: home.set_light
    description: str
    input_schema: dict[str, Any]
    risk_class: str  # konservativer Standard; risk_rules können argumentabhängig abweichen
    domain: str
    handler: Handler
    side_effects: str = "none"  # none | reversible | irreversible
    output_trust: str = "trusted"  # untrusted setzt das Taint-Flag
    timeout_s: float = 10.0
    risk_rules: list[dict[str, Any]] = field(default_factory=list)
    verify: Verifier | None = None
    undo: UndoBuilder | None = None

    def __post_init__(self) -> None:
        if self.risk_class not in RISK_ORDER:
            raise ValueError(f"invalid risk class {self.risk_class}")
        Draft202012Validator.check_schema(self.input_schema)
        self._validator = Draft202012Validator(self.input_schema)

    def risk_for(self, arguments: dict[str, Any]) -> str:
        """Erste passende Regel gilt, sonst die (konservative) Basis-Risikoklasse."""
        for rule in self.risk_rules:
            if all(arguments.get(k) == v for k, v in rule["when"].items()):
                return rule["risk_class"]
        return self.risk_class

    def validate(self, arguments: Any) -> list[dict[str, str]]:
        if not isinstance(arguments, dict):
            return [{"pointer": "", "message": "Argumente müssen ein JSON-Objekt sein"}]
        return [
            {"pointer": "/" + "/".join(str(p) for p in err.absolute_path), "message": err.message}
            for err in sorted(self._validator.iter_errors(arguments), key=lambda e: list(e.absolute_path))
        ]


class ToolRegistry:
    """Zentrale Registry. LLM-Tool-Namen ersetzen '.' durch '__' (erlaubt sind nur [a-zA-Z0-9_-])."""

    def __init__(self) -> None:
        self._caps: dict[str, Capability] = {}

    def register(self, cap: Capability) -> None:
        if cap.name in self._caps:
            raise ValueError(f"capability {cap.name} already registered")
        self._caps[cap.name] = cap

    def get(self, name: str) -> Capability | None:
        return self._caps.get(name)

    def names(self) -> list[str]:
        return [c.name for c in self.all()]

    def all(self) -> list[Capability]:
        return [self._caps[k] for k in sorted(self._caps)]

    @staticmethod
    def llm_name(capability: str) -> str:
        return capability.replace(".", "__")

    @staticmethod
    def capability_name(llm_name: str) -> str:
        return llm_name.replace("__", ".")

    def tool_specs(self, domains: frozenset[str] | None = None) -> list[ToolSpec]:
        """Deterministisch sortiert (stabiler Prompt-Cache-Präfix), optional nach Domänen gefiltert."""
        return [
            ToolSpec(name=self.llm_name(c.name), description=c.description, input_schema=c.input_schema)
            for c in self.all()
            if domains is None or c.domain in domains
        ]
