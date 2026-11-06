"""Börse: Kurse von Aktien, Indizes, Kryptowährungen, Gold und Devisen – ohne API-Schlüssel (Stooq, Tagesdaten).

``info.stock`` nimmt einen Namen („Apple“, „DAX“, „Bitcoin“) oder ein Kürzel („aapl.us“, „sap.de“) und liefert den
letzten Schlusskurs, die Veränderung zum Vortag und die Spanne der letzten Wochen. Kurse sind nicht in Echtzeit.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from typing import Any

from .errors import JarvisError
from .tools import Capability, InvocationContext, ToolRegistry

log = logging.getLogger(__name__)

STOOQ = "https://stooq.com/q/d/l/?s={symbol}&i=d"
# Gesprochene Namen -> (Stooq-Kürzel, Anzeigename, Währung)
SYMBOLS: dict[str, tuple[str, str, str]] = {
    "apple": ("aapl.us", "Apple", "USD"), "microsoft": ("msft.us", "Microsoft", "USD"),
    "nvidia": ("nvda.us", "Nvidia", "USD"), "tesla": ("tsla.us", "Tesla", "USD"),
    "amazon": ("amzn.us", "Amazon", "USD"), "alphabet": ("googl.us", "Alphabet", "USD"),
    "google": ("googl.us", "Alphabet", "USD"), "meta": ("meta.us", "Meta", "USD"),
    "facebook": ("meta.us", "Meta", "USD"), "netflix": ("nflx.us", "Netflix", "USD"),
    "amd": ("amd.us", "AMD", "USD"), "intel": ("intc.us", "Intel", "USD"), "coca cola": ("ko.us", "Coca-Cola", "USD"),
    "sap": ("sap.de", "SAP", "EUR"), "siemens": ("sie.de", "Siemens", "EUR"), "allianz": ("alv.de", "Allianz", "EUR"),
    "basf": ("bas.de", "BASF", "EUR"), "bmw": ("bmw.de", "BMW", "EUR"), "mercedes": ("mbg.de", "Mercedes-Benz", "EUR"),
    "mercedes benz": ("mbg.de", "Mercedes-Benz", "EUR"), "volkswagen": ("vow3.de", "Volkswagen", "EUR"),
    "vw": ("vow3.de", "Volkswagen", "EUR"), "telekom": ("dte.de", "Deutsche Telekom", "EUR"),
    "deutsche telekom": ("dte.de", "Deutsche Telekom", "EUR"), "deutsche bank": ("dbk.de", "Deutsche Bank", "EUR"),
    "adidas": ("ads.de", "Adidas", "EUR"), "bayer": ("bayn.de", "Bayer", "EUR"), "infineon": ("ifx.de", "Infineon", "EUR"),
    "rheinmetall": ("rhm.de", "Rheinmetall", "EUR"), "porsche": ("p911.de", "Porsche", "EUR"),
    "commerzbank": ("cbk.de", "Commerzbank", "EUR"), "lufthansa": ("lha.de", "Lufthansa", "EUR"),
    "dax": ("^dax", "DAX", "Punkte"), "dow jones": ("^dji", "Dow Jones", "Punkte"), "dow": ("^dji", "Dow Jones", "Punkte"),
    "s&p 500": ("^spx", "S&P 500", "Punkte"), "s und p 500": ("^spx", "S&P 500", "Punkte"),
    "nasdaq": ("^ndq", "Nasdaq 100", "Punkte"), "bitcoin": ("btcusd", "Bitcoin", "USD"),
    "ethereum": ("ethusd", "Ethereum", "USD"), "gold": ("xauusd", "Gold (Feinunze)", "USD"),
    "silber": ("xagusd", "Silber (Feinunze)", "USD"), "euro": ("eurusd", "Euro in Dollar", "USD"),
    "dollar": ("usdeur", "Dollar in Euro", "EUR"),
}


def resolve(name: str) -> tuple[str, str, str]:
    """Name oder Kürzel -> (Kürzel, Anzeigename, Währung). Unbekannte Wörter werden als US-Kürzel versucht."""
    wanted = re.sub(r"\s+", " ", re.sub(r"\b(?:aktie|aktien|kurs|der|die|das|von|vom)\b|[-–]", " ", name.lower())).strip()
    if wanted in SYMBOLS:
        return SYMBOLS[wanted]
    if re.fullmatch(r"\^?[a-z0-9]{1,6}(?:\.[a-z]{2})?", wanted):
        symbol = wanted if "." in wanted or wanted.startswith("^") else f"{wanted}.us"
        return symbol, wanted.upper(), "USD" if symbol.endswith(".us") else ""
    raise JarvisError("JRV-NFD-001", f"Unbekanntes Wertpapier: {name}",
                      user_message=f"Zu „{name}“ kenne ich kein Börsenkürzel. Nennen Sie das Kürzel, etwa „AAPL“.")


def parse_history(text: str) -> list[dict[str, float]]:
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            rows.append({"date": row["Date"], "close": float(row["Close"]), "high": float(row["High"]),
                         "low": float(row["Low"])})
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def register_stock_capabilities(registry: ToolRegistry, *, client: Any = None) -> None:
    async def quote(args: dict[str, Any], ctx: InvocationContext) -> Any:
        import httpx

        symbol, name, currency = resolve(args["name"])
        http = client or httpx.AsyncClient(timeout=10.0, follow_redirects=True)
        try:
            response = await http.get(STOOQ.format(symbol=symbol))
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise JarvisError("JRV-INT-001", f"Börsendaten nicht erreichbar: {exc}",
                              user_message="Die Börsendaten sind gerade nicht erreichbar.") from exc
        finally:
            if client is None:
                await http.aclose()
        rows = parse_history(response.text)
        if not rows:
            raise JarvisError("JRV-NFD-001", f"Keine Kurse für {symbol}",
                              user_message=f"Für „{name}“ finde ich keine Kurse.")
        last, previous = rows[-1], rows[-2] if len(rows) > 1 else rows[-1]
        recent = rows[-20:]
        change = (last["close"] - previous["close"]) / previous["close"] * 100 if previous["close"] else 0.0
        return {"symbol": symbol, "name": name, "currency": currency, "date": last["date"],
                "close": last["close"], "change_pct": round(change, 2),
                "low_4w": min(r["low"] for r in recent), "high_4w": max(r["high"] for r in recent),
                "history": [{"date": r["date"], "close": r["close"]} for r in rows[-30:]],  # Kurve in der Anzeige
                "source": "Stooq (Tagesschluss, nicht in Echtzeit)"}

    registry.register(Capability(
        name="info.stock", domain="info", risk_class="R0", output_trust="untrusted", timeout_s=15.0,
        description=("Börsenkurs (Tagesschluss, Veränderung zum Vortag, Spanne vier Wochen) für Aktien, Indizes (DAX, "
                     "Dow Jones, Nasdaq), Bitcoin, Gold und Devisen. name: Firmenname oder Kürzel."),
        input_schema={"type": "object", "additionalProperties": False, "required": ["name"], "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 60},
        }},
        handler=quote,
    ))
