"""Rechnen, Einheiten, Währungen – deterministisch, ohne Sprachmodell. Wechselkurse: Europäische Zentralbank (kostenlos,
über frankfurter.dev)."""

from __future__ import annotations

import ast
import operator
import re
from typing import Any

from .errors import JarvisError
from .tools import Capability, InvocationContext, ToolRegistry

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}
WORD_OPS = [(r"\bgeteilt durch\b|\bdurch\b|:", "/"), (r"\bmal\b|\bx\b|×", "*"), (r"\bplus\b", "+"), (r"\bminus\b", "-"),
            (r"\bhoch\b|\^", "**")]


def safe_eval(expression: str) -> float:
    def walk(node: ast.AST) -> float:
        if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("Exponent zu groß")
            return _OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](walk(node.operand))
        raise ValueError("nicht erlaubt")

    return walk(ast.parse(expression, mode="eval").body)


def spoken_to_expression(text: str) -> str | None:
    """„12 mal 7 plus 3“ -> „12*7+3“ (Dezimalkomma erlaubt); None, wenn es kein reiner Rechenausdruck ist."""
    expression = re.sub(r"^die\s+", "", text.lower()).replace(",", ".")
    expression = re.sub(r"(?<=\d)\.(?=\d{3}\b)", "", expression)  # Tausenderpunkt
    for pattern, symbol in WORD_OPS:
        expression = re.sub(pattern, f" {symbol} ", expression)
    expression = re.sub(r"wurzel (?:aus|von)\s*(\d+(?:\.\d+)?)", r"(\1)**0.5", expression)
    expression = re.sub(r"\s+", "", expression)
    return expression if re.fullmatch(r"[\d.+\-*/()]+", expression) and re.search(r"\d", expression) else None


# Einheiten -> (Gruppe, Faktor zur Basiseinheit); Temperatur gesondert
UNITS: dict[str, tuple[str, float]] = {}
for group, base, names in [
    ("länge", 1.0, {"meter": 1, "m": 1, "kilometer": 1000, "km": 1000, "zentimeter": 0.01, "cm": 0.01, "millimeter": 0.001,
                    "mm": 0.001, "meile": 1609.344, "meilen": 1609.344, "zoll": 0.0254, "inch": 0.0254, "fuß": 0.3048,
                    "fuss": 0.3048, "yard": 0.9144}),
    ("masse", 1.0, {"gramm": 1, "g": 1, "kilogramm": 1000, "kilo": 1000, "kg": 1000, "pfund": 500, "unze": 28.3495,
                    "unzen": 28.3495, "tonne": 1_000_000, "tonnen": 1_000_000}),
    ("volumen", 1.0, {"liter": 1, "l": 1, "milliliter": 0.001, "ml": 0.001, "gallone": 3.78541, "gallonen": 3.78541,
                      "tasse": 0.25, "tassen": 0.25}),
    ("tempo", 1.0, {"km/h": 1, "kmh": 1, "meilen pro stunde": 1.609344, "mph": 1.609344, "m/s": 3.6}),
]:
    for name, factor in names.items():
        UNITS[name] = (group, factor * base)
TEMPS = {"celsius": "c", "grad celsius": "c", "°c": "c", "fahrenheit": "f", "grad fahrenheit": "f", "°f": "f", "kelvin": "k"}
CURRENCIES = {"euro": "EUR", "eur": "EUR", "dollar": "USD", "usd": "USD", "us-dollar": "USD", "pfund": "GBP", "gbp": "GBP",
              "franken": "CHF", "chf": "CHF", "yen": "JPY", "jpy": "JPY", "zloty": "PLN", "pln": "PLN", "kronen": "SEK",
              "schwedische kronen": "SEK", "kanadische dollar": "CAD", "australische dollar": "AUD", "lira": "TRY",
              "türkische lira": "TRY"}


def to_celsius(value: float, unit: str) -> float:
    return value if unit == "c" else (value - 32) * 5 / 9 if unit == "f" else value - 273.15


def from_celsius(value: float, unit: str) -> float:
    return value if unit == "c" else value * 9 / 5 + 32 if unit == "f" else value + 273.15


def convert_unit(value: float, source: str, target: str) -> float:
    s, t = source.lower().strip(), target.lower().strip()
    if s in TEMPS and t in TEMPS:
        return from_celsius(to_celsius(value, TEMPS[s]), TEMPS[t])
    if s in UNITS and t in UNITS and UNITS[s][0] == UNITS[t][0]:
        return value * UNITS[s][1] / UNITS[t][1]
    raise JarvisError("JRV-VAL-002", f"{source} -> {target}", user_message=f"Von {source} nach {target} kann ich nicht umrechnen.")


async def fetch_rate(source: str, target: str, *, client: Any = None) -> tuple[float, str]:
    import httpx

    http = client or httpx.AsyncClient(timeout=8.0)
    try:
        reply = await http.get("https://api.frankfurter.dev/v1/latest", params={"base": source, "symbols": target})
        reply.raise_for_status()
        data = reply.json()
        return float(data["rates"][target]), str(data.get("date", ""))
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise JarvisError("JRV-INT-001", f"Wechselkurs nicht erreichbar: {exc}",
                          user_message="Die Wechselkurse sind gerade nicht erreichbar.") from exc
    finally:
        if client is None:
            await http.aclose()


def register_calc_capabilities(registry: ToolRegistry, *, client: Any = None) -> None:
    async def calc(args: dict[str, Any], ctx: InvocationContext) -> Any:
        mode = args["mode"]
        if mode == "percent":
            return {"mode": mode, "value": args["pct"] / 100 * args["of"], "pct": args["pct"], "of": args["of"]}
        if mode == "calc":
            expression = spoken_to_expression(args["expression"])
            try:
                return {"mode": mode, "value": safe_eval(expression or ""), "expression": args["expression"]}
            except (ValueError, SyntaxError, ZeroDivisionError, OverflowError):
                raise JarvisError("JRV-VAL-002", "Rechnung unverständlich",
                                  user_message="Diese Rechnung verstehe ich nicht – oder sie geht nicht auf.") from None
        if mode == "convert":
            return {"mode": mode, "value": convert_unit(args["amount"], args["from"], args["to"]), **{k: args[k] for k in ("amount", "from", "to")}}
        source = CURRENCIES.get(args["from"].lower(), args["from"].upper())
        target = CURRENCIES.get(args["to"].lower(), args["to"].upper())
        rate, day = await fetch_rate(source, target, client=client)
        return {"mode": mode, "value": args["amount"] * rate, "amount": args["amount"], "from": source, "to": target, "date": day}

    registry.register(Capability(
        name="info.calc", domain="info", risk_class="R0", timeout_s=15.0,
        description="Rechnet: mode calc (expression „12 mal 7 plus 3“), percent (pct von of), convert (amount, from, to – "
                    "Einheiten) oder currency (amount, from, to – Währungen, Kurs der EZB).",
        input_schema={"type": "object", "additionalProperties": False, "required": ["mode"], "properties": {
            "mode": {"enum": ["calc", "percent", "convert", "currency"]}, "expression": {"type": "string", "maxLength": 100},
            "pct": {"type": "number"}, "of": {"type": "number"}, "amount": {"type": "number"},
            "from": {"type": "string", "maxLength": 40}, "to": {"type": "string", "maxLength": 40}}},
        handler=calc))
