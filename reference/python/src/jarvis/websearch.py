"""Websuche ohne API-Schlüssel: DuckDuckGo (HTML-Ergebnisseite) oder eine eigene SearXNG-Instanz (JSON-API).

Ergebnisse sind Fremdinhalte (``untrusted``): Titel und Kurztexte stammen von beliebigen Webseiten. Wer einen Link
daraus öffnen will, nimmt ``pc.open_link`` (deterministisch der erste Treffer zur Anfrage des Nutzers) oder – nach
dem Lesen der Liste – ``pc.open_url``, das im markierten Zustand eine Bestätigung verlangt.

Ist die Suche vom Server aus nicht erreichbar (Netz, Bot-Sperre), gibt es den Weg über den Browser des Nutzers:
``ducky_url`` öffnet DuckDuckGos „I'm Feeling Ducky“, das direkt zum ersten Treffer weiterleitet.
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlsplit

from .errors import JarvisError
from .tools import Capability, InvocationContext, ToolRegistry

DDG_URL = "https://html.duckduckgo.com/html/"
USER_AGENT = "Mozilla/5.0 (compatible; JARVIS-Referenz/0.1; +https://github.com/mrdanilp15-crypto/Jarvis)"
SITES = {  # „auf YouTube“ -> nur Treffer dieser Seite
    "youtube": "youtube.com", "wikipedia": "wikipedia.org", "amazon": "amazon.de", "ebay": "ebay.de",
    "reddit": "reddit.com", "github": "github.com",
}


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _target(href: str) -> str | None:
    """DuckDuckGo verlinkt über //duckduckgo.com/l/?uddg=<Ziel>; Werbung läuft über /y.js – die fällt weg."""
    href = html.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    parts = urlsplit(href)
    if parts.hostname and parts.hostname.endswith("duckduckgo.com"):
        if parts.path.startswith("/y.js"):
            return None
        target = parse_qs(parts.query).get("uddg", [None])[0]
        return target if target and target.startswith(("http://", "https://")) else None
    return href if parts.scheme in ("http", "https") else None


def parse_duckduckgo(page: str, limit: int = 5) -> list[SearchResult]:
    """Ergebnisse aus html.duckduckgo.com: Titel-Link (result__a), Kurztext (result__snippet), ohne Anzeigen."""
    results: list[SearchResult] = []
    for block in re.split(r'<div class="result[ "]', page)[1:]:
        head = block[:300]
        if "result--ad" in head:
            continue
        tag = re.search(r'<a\s[^>]*class="result__a"[^>]*>(?P<title>.*?)</a>', block, re.S)
        if tag is None:
            continue
        href = re.search(r'href="([^"]+)"', tag.group(0))
        url = _target(href.group(1)) if href else None
        if not url or any(r.url == url for r in results):
            continue
        snippet = re.search(r'class="result__snippet"[^>]*>(.*?)</(?:a|div|td)>', block, re.S)
        results.append(SearchResult(_text(tag["title"])[:200], url, _text(snippet.group(1))[:300] if snippet else ""))
        if len(results) >= limit:
            break
    return results


def ducky_url(query: str) -> str:
    """Browser-Weg ohne Server-Suche: DuckDuckGo leitet „\\ Anfrage“ direkt zum ersten Treffer weiter."""
    return "https://duckduckgo.com/?q=" + quote_plus("\\" + query)


def scoped(query: str, site: str | None) -> str:
    return f"{query} site:{SITES[site]}" if site in SITES else query


class WebSearch:
    def __init__(self, *, searxng_url: str | None = None, region: str = "de-de", client: Any = None,
                 cache_ttl_s: float = 300.0) -> None:
        import httpx

        self._httpx = httpx
        self._client = client or httpx.AsyncClient(timeout=8.0, follow_redirects=True,
                                                   headers={"User-Agent": USER_AGENT})
        self.searxng_url = (searxng_url or "").rstrip("/") or None
        self.region = region
        self._ttl_s = cache_ttl_s
        self._cache: dict[tuple[str, int], tuple[float, list[SearchResult]]] = {}

    async def search(self, query: str, *, limit: int = 5) -> list[SearchResult]:
        key = (query.strip().lower(), limit)
        hit = self._cache.get(key)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
        try:
            results = await (self._searxng(query, limit) if self.searxng_url else self._duckduckgo(query, limit))
        except self._httpx.HTTPError as exc:
            raise JarvisError("JRV-INT-001", f"Websuche nicht erreichbar: {exc}",
                              user_message="Die Websuche ist gerade nicht erreichbar.") from exc
        self._cache[key] = (time.monotonic() + self._ttl_s, results)
        return results

    async def _duckduckgo(self, query: str, limit: int) -> list[SearchResult]:
        response = await self._client.post(DDG_URL, data={"q": query, "kl": self.region})
        response.raise_for_status()
        results = parse_duckduckgo(response.text, limit)
        if not results and ("anomaly" in response.text or response.status_code == 202):
            raise JarvisError("JRV-INT-001", "DuckDuckGo verlangt eine Bot-Prüfung",
                              user_message="Die Websuche hat die Anfrage gerade abgelehnt.")
        return results

    async def _searxng(self, query: str, limit: int) -> list[SearchResult]:
        response = await self._client.get(f"{self.searxng_url}/search",
                                          params={"q": query, "format": "json", "language": self.region[:2]})
        response.raise_for_status()
        results = []
        for item in response.json().get("results") or []:
            url = str(item.get("url") or "")
            if url.startswith(("http://", "https://")):
                results.append(SearchResult(_text(str(item.get("title") or url))[:200], url,
                                            _text(str(item.get("content") or ""))[:300]))
            if len(results) >= limit:
                break
        return results


def register_web_capabilities(registry: ToolRegistry, search: WebSearch) -> None:
    async def web_search(args: dict[str, Any], ctx: InvocationContext) -> Any:
        results = await search.search(scoped(args["query"], args.get("site")), limit=args.get("count", 5))
        if not results:
            return {"query": args["query"], "results": [], "note": "Keine Treffer."}
        return {"query": args["query"], "results": [r.to_dict() for r in results]}

    registry.register(Capability(
        name="web.search", domain="web", risk_class="R0", output_trust="untrusted", timeout_s=12.0,
        description="Sucht im Internet und liefert die besten Treffer mit Titel, Link und Kurztext (zum Vorlesen "
                    "oder Auswählen). Zum direkten Öffnen des besten Treffers pc.open_link verwenden.",
        input_schema={"type": "object", "additionalProperties": False, "required": ["query"], "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 300},
            "site": {"enum": list(SITES)},
            "count": {"type": "integer", "minimum": 1, "maximum": 10},
        }},
        handler=web_search,
    ))
