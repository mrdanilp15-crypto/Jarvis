"""Informations-Capabilities ohne API-Schlüssel: Wetter (Open-Meteo), Nachrichten (RSS), Wikipedia.

Wetterdaten sind Messwerte einer festen API (``trusted``). Nachrichten- und Wikipedia-Texte sind Fremdinhalte
(``untrusted``): Sie setzen das Taint-Flag, danach verlangen R2-Aktionen eine Bestätigung (Schutz vor
Prompt-Injection, docs/02 Abschnitt 2.6).
"""

from __future__ import annotations

import html
import re
import time
import xml.etree.ElementTree as ET  # expat >= 2.4.1 schützt vor Entity-Expansion („Billion Laughs“)
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

from .errors import JarvisError
from .knowledge import strip_kinds, title_fits, title_matches
from .tools import Capability, InvocationContext, ToolRegistry

USER_AGENT = "JARVIS-Referenz/0.1 (+https://github.com/mrdanilp15-crypto/Jarvis)"  # Wikimedia verlangt einen UA

# WMO-Wettercodes (Open-Meteo) auf Deutsch
WEATHER_CODES = {
    0: "klar", 1: "überwiegend klar", 2: "teilweise bewölkt", 3: "bedeckt", 45: "Nebel", 48: "gefrierender Nebel",
    51: "leichter Nieselregen", 53: "Nieselregen", 55: "starker Nieselregen", 56: "gefrierender Nieselregen",
    57: "starker gefrierender Nieselregen", 61: "leichter Regen", 63: "Regen", 65: "starker Regen",
    66: "gefrierender Regen", 67: "starker gefrierender Regen", 71: "leichter Schneefall", 73: "Schneefall",
    75: "starker Schneefall", 77: "Schneegriesel", 80: "leichte Regenschauer", 81: "Regenschauer",
    82: "heftige Regenschauer", 85: "leichte Schneeschauer", 86: "starke Schneeschauer", 95: "Gewitter",
    96: "Gewitter mit Hagel", 99: "schweres Gewitter mit Hagel",
}

MAX_FEED_BYTES = 2_000_000


@dataclass
class InfoConfig:
    home_location: str | None = None  # Standardort fürs Wetter, wenn weder Nutzer noch Kontext einen nennen
    news_feeds: dict[str, str] = field(default_factory=lambda: {
        "tagesschau": "https://www.tagesschau.de/index~rss2.xml",
    })
    wikipedia_language: str = "de"
    cache_ttl_s: float = 600.0


class _Fetcher:
    """HTTP-Zugriff mit kurzem Cache (Wetter ändert sich nicht minütlich, Feeds auch nicht)."""

    def __init__(self, client: Any, ttl_s: float) -> None:
        import httpx

        self._httpx = httpx
        self._client = client or httpx.AsyncClient(timeout=8.0, follow_redirects=True,
                                                   headers={"User-Agent": USER_AGENT})
        self._ttl_s = ttl_s
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, Any]] = {}

    async def get(self, url: str, params: dict[str, Any] | None = None, *, as_json: bool = True) -> Any:
        key = (url, tuple(sorted((k, str(v)) for k, v in (params or {}).items())))
        hit = self._cache.get(key)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
        try:
            response = await self._client.get(url, params=params)
            response.raise_for_status()
        except self._httpx.HTTPError as exc:
            host = self._httpx.URL(url).host
            raise JarvisError("JRV-INT-001", f"{host} nicht erreichbar: {exc}",
                              user_message="Der Dienst ist gerade nicht erreichbar.") from exc
        if len(response.content) > MAX_FEED_BYTES:
            raise JarvisError("JRV-INT-001", f"Antwort von {url} zu groß")
        value = response.json() if as_json else response.text
        self._cache[key] = (time.monotonic() + self._ttl_s, value)
        return value


def _clean(text: str | None, limit: int) -> str:
    plain = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain if len(plain) <= limit else plain[: limit - 1].rstrip() + "…"


def parse_feed(xml_text: str, limit: int) -> list[dict[str, str | None]]:
    """RSS 2.0 und Atom: Titel, Kurztext, Datum, Link."""
    root = ET.fromstring(xml_text)
    atom = "{http://www.w3.org/2005/Atom}"
    entries = list(root.iter("item")) or list(root.iter(f"{atom}entry"))
    out: list[dict[str, str | None]] = []
    for entry in entries[:limit]:
        def text(*tags: str) -> str | None:
            for tag in tags:
                node = entry.find(tag)
                if node is not None and (node.text or node.get("href")):
                    return node.text or node.get("href")
            return None

        published = text("pubDate", f"{atom}updated", f"{atom}published")
        if published and "," in published:  # RFC 822 (RSS) -> ISO 8601
            try:
                published = parsedate_to_datetime(published).isoformat()
            except (TypeError, ValueError):
                pass
        out.append({
            "title": _clean(text("title", f"{atom}title"), 200),
            "summary": _clean(text("description", f"{atom}summary"), 300),
            "published": published,
            "link": text("link", f"{atom}link"),
        })
    return out


def register_info_capabilities(registry: ToolRegistry, config: InfoConfig | None = None, *,
                               client: Any = None) -> None:
    config = config or InfoConfig()
    fetch = _Fetcher(client, config.cache_ttl_s)

    async def geocode(place: str) -> tuple[float, float, str] | None:
        for name in dict.fromkeys([place, place.split(",")[0].strip()]):  # „Köln, Deutschland“ -> „Köln“
            data = await fetch.get("https://geocoding-api.open-meteo.com/v1/search",
                                   {"name": name, "count": 1, "language": "de", "format": "json"})
            if data.get("results"):
                hit = data["results"][0]
                parts = [hit.get("name"), hit.get("admin1"), hit.get("country")]
                label = ", ".join(dict.fromkeys(p for p in parts if p))
                return hit["latitude"], hit["longitude"], label
        return None

    async def weather(args: dict[str, Any], ctx: InvocationContext) -> Any:
        place = (args.get("location") or config.home_location or "").strip()
        if not place:
            return {"error": "Kein Ort bekannt. Frage den Nutzer, für welchen Ort er das Wetter wissen möchte."}
        found = await geocode(place)
        if found is None:
            return {"error": f"Ort „{place}“ nicht gefunden. Frage nach einer genaueren Ortsangabe."}
        latitude, longitude, label = found
        data = await fetch.get("https://api.open-meteo.com/v1/forecast", {
            "latitude": latitude, "longitude": longitude, "timezone": "auto",
            "forecast_days": args.get("days", 1),
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,"
                       "wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                     "precipitation_sum",
        })
        current = data.get("current") or {}
        daily = data.get("daily") or {}

        def day_value(key: str, i: int) -> Any:
            values = daily.get(key) or []
            return values[i] if i < len(values) else None

        return {
            "location": label,
            "current": {
                "time": current.get("time"),
                "conditions": WEATHER_CODES.get(current.get("weather_code"), "unbekannt"),
                "temperature_c": current.get("temperature_2m"),
                "feels_like_c": current.get("apparent_temperature"),
                "humidity_pct": current.get("relative_humidity_2m"),
                "precipitation_mm": current.get("precipitation"),
                "wind_kmh": current.get("wind_speed_10m"),
            },
            "forecast": [
                {
                    "date": date,
                    "conditions": WEATHER_CODES.get(day_value("weather_code", i), "unbekannt"),
                    "temp_max_c": day_value("temperature_2m_max", i),
                    "temp_min_c": day_value("temperature_2m_min", i),
                    "precipitation_probability_pct": day_value("precipitation_probability_max", i),
                    "precipitation_mm": day_value("precipitation_sum", i),
                }
                for i, date in enumerate(daily.get("time") or [])
            ],
            "source": "Open-Meteo",
        }

    async def news(args: dict[str, Any], ctx: InvocationContext) -> Any:
        source = args.get("source") or next(iter(config.news_feeds))
        url = config.news_feeds.get(source)
        if url is None:
            return {"error": f"Unbekannte Quelle {source}", "sources": list(config.news_feeds)}
        feed = await fetch.get(url, as_json=False)
        try:
            headlines = parse_feed(feed, args.get("count", 5))
        except ET.ParseError as exc:
            raise JarvisError("JRV-INT-001", f"Feed {source} nicht lesbar: {exc}") from exc
        return {"source": source, "headlines": headlines}

    async def wikipedia(args: dict[str, Any], ctx: InvocationContext) -> Any:
        base = f"https://{config.wikipedia_language}.wikipedia.org"
        name = args.get("name") or strip_kinds(args["query"])
        fits = (lambda t: title_matches(t, name)) if args.get("name") else (lambda t: title_fits(t, args["query"]))
        search = await fetch.get(f"{base}/w/rest.php/v1/search/page", {"q": args["query"], "limit": 3})
        # Die Volltextsuche liefert auch ähnlich geschriebene Artikel („ARTERIION“ -> „Arterie“). Nur ein Artikel,
        # dessen Titel zum Namen passt, zählt – sonst mischt ein kleines Modell daraus erfundene Fakten.
        page = next((p for p in search.get("pages") or []
                     if any(fits(str(p.get(key) or "")) for key in ("title", "matched_title"))), None)
        if page is None:
            return {"found": False, "query": args["query"],
                    "note": f"Kein Wikipedia-Artikel zu „{name}“. Ähnlich geschriebene Artikel meinen etwas anderes "
                            "– nichts daraus übernehmen und nichts erfinden."}
        summary = await fetch.get(f"{base}/api/rest_v1/page/summary/{quote(page['key'], safe='')}")
        if summary.get("type") == "disambiguation":
            return {"found": False, "ambiguous": True, "title": summary.get("title"),
                    "note": "Begriffsklärung: Der Name hat mehrere Bedeutungen – nachfragen, welche gemeint ist."}
        return {
            "found": True,
            "title": summary.get("title"),
            "description": summary.get("description"),
            "summary": _clean(summary.get("extract"), 1500),
            "url": ((summary.get("content_urls") or {}).get("desktop") or {}).get("page"),
            "source": "Wikipedia",
        }

    registry.register(Capability(
        name="info.weather", domain="info", risk_class="R0", timeout_s=20.0,
        description="Aktuelles Wetter und Vorhersage (bis 7 Tage) für einen Ort. Immer verwenden, wenn nach Wetter, "
                    "Temperatur, Regen, Schnee, Wind oder passender Kleidung gefragt wird – nie raten.",
        input_schema={"type": "object", "additionalProperties": False, "properties": {
            "location": {"type": "string", "minLength": 2, "maxLength": 100,
                         "description": "Ort, z. B. „Hamburg“. Weglassen = Ort aus dem Kontext bzw. Wohnort."},
            "days": {"type": "integer", "minimum": 1, "maximum": 7, "description": "Vorhersagetage, 1 = heute"},
        }},
        handler=weather,
    ))
    registry.register(Capability(
        name="info.news", domain="info", risk_class="R0", output_trust="untrusted", timeout_s=15.0,
        description="Aktuelle Nachrichten-Schlagzeilen mit Kurztext. Verwenden bei Fragen nach Nachrichten, "
                    "News oder was in der Welt passiert.",
        input_schema={"type": "object", "additionalProperties": False, "properties": {
            "count": {"type": "integer", "minimum": 1, "maximum": 10},
            "source": {"enum": list(config.news_feeds)},
        }},
        handler=news,
    ))
    registry.register(Capability(
        name="info.wikipedia", domain="info", risk_class="R0", output_trust="untrusted", timeout_s=15.0,
        description="Schlägt Fakten zu Personen, Orten, Begriffen und Ereignissen in Wikipedia nach. Verwenden, "
                    "wenn nach Wissen gefragt wird, bei dem Genauigkeit zählt. found=false heißt: kein passender "
                    "Artikel – dann nichts erfinden.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["query"], "properties": {
            "query": {"type": "string", "minLength": 2, "maxLength": 200},
            "name": {"type": "string", "minLength": 1, "maxLength": 120,
                     "description": "Genauer Name, der im Artikeltitel stehen muss (wenn query Zusätze enthält)"},
        }},
        handler=wikipedia,
    ))
