"""Signierte Webhooks (ein- und ausgehend): HMAC-SHA256 über ``<timestamp>.<body>`` mit Replay-Schutz.

Header:
  X-Jarvis-Timestamp: Unix-Sekunden
  X-Jarvis-Signature: sha256=<hex>
  X-Jarvis-Delivery:  eindeutige Zustell-ID (Replay-Cache)
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import OrderedDict

from .errors import JarvisError


def sign(secret: bytes, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret, f"{timestamp}.".encode() + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


class ReplayCache:
    def __init__(self, ttl_s: int = 600, max_entries: int = 50_000) -> None:
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._seen: OrderedDict[str, float] = OrderedDict()

    def check_and_add(self, delivery_id: str, now: float) -> bool:
        """False, wenn die Zustellung bereits gesehen wurde."""
        while self._seen and (now - next(iter(self._seen.values())) > self.ttl_s or len(self._seen) > self.max_entries):
            self._seen.popitem(last=False)
        if delivery_id in self._seen:
            return False
        self._seen[delivery_id] = now
        return True


def verify(
    secret: bytes,
    *,
    body: bytes,
    timestamp_header: str | None,
    signature_header: str | None,
    delivery_id: str | None,
    replay_cache: ReplayCache,
    max_skew_s: int = 300,
    now: float | None = None,
) -> None:
    """Wirft JRV-AUTH-002 bei fehlender/falscher Signatur, zu alter Nachricht oder Replay."""
    now = time.time() if now is None else now
    if not (timestamp_header and signature_header and delivery_id):
        raise JarvisError("JRV-AUTH-002", "Signatur-Header fehlen")
    try:
        timestamp = int(timestamp_header)
    except ValueError as exc:
        raise JarvisError("JRV-AUTH-002", "Ungültiger Zeitstempel") from exc
    if abs(now - timestamp) > max_skew_s:
        raise JarvisError("JRV-AUTH-002", "Zeitstempel außerhalb des erlaubten Fensters")
    if not hmac.compare_digest(sign(secret, timestamp, body), signature_header):
        raise JarvisError("JRV-AUTH-002", "Signatur stimmt nicht")
    if not replay_cache.check_and_add(delivery_id, now):
        raise JarvisError("JRV-AUTH-002", "Zustellung bereits verarbeitet (Replay)")
