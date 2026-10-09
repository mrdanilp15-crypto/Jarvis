"""Radiosender finden – über das freie Verzeichnis radio-browser.info (ohne Schlüssel, Community-gepflegt)."""

from __future__ import annotations

import logging
import re
from typing import Any

from .errors import JarvisError

log = logging.getLogger(__name__)

SERVERS = ("https://de1.api.radio-browser.info", "https://fi1.api.radio-browser.info",
           "https://nl1.api.radio-browser.info")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9äöüß]+", "", text.lower())


async def find_station(name: str, *, client: Any = None) -> dict[str, str]:
    """„Radio Bob“ -> {"name": "RADIO BOB!", "url": "https://…"} – bevorzugt deutsche Sender mit vielen Stimmen."""
    import httpx

    wanted = re.sub(r"^(?:den sender|sender|das radio|radio)\s+(?=\S)", "", name.strip(), flags=re.I) or name
    http = client or httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "JARVIS/2.8"})
    last: Exception | None = None
    try:
        for server in SERVERS:  # Spiegel nacheinander, falls einer ausfällt
            try:
                response = await http.get(f"{server}/json/stations/search", params={
                    "name": wanted, "limit": 15, "hidebroken": "true", "order": "votes", "reverse": "true"})
                response.raise_for_status()
                stations = response.json()
                break
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
        else:
            raise JarvisError("JRV-INT-001", f"Radio-Verzeichnis nicht erreichbar: {last}",
                              user_message="Das Radio-Verzeichnis ist gerade nicht erreichbar.")
    finally:
        if client is None:
            await http.aclose()
    candidates = [s for s in stations if s.get("url_resolved") or s.get("url")]
    if not candidates:
        raise JarvisError("JRV-NFD-001", f"Kein Sender {wanted}",
                          user_message=f"Einen Sender namens „{wanted}“ finde ich nicht.")

    def rank(station: dict[str, Any]) -> tuple[int, int, int]:
        exact = _norm(station.get("name", "")) == _norm(wanted)
        german = station.get("countrycode") == "DE"
        return (int(exact), int(german), int(station.get("votes") or 0))

    best = max(candidates, key=rank)
    return {"name": str(best.get("name") or wanted).strip(), "url": str(best.get("url_resolved") or best["url"])}
