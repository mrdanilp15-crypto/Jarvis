"""CalDAV: Termine in Nextcloud, iCloud, mailbox.org, Posteo … eintragen, löschen und lesen.

Konfiguration über ``JARVIS_CALDAV_URL`` (Adresse des Kalenders, z. B.
``https://cloud.example.de/remote.php/dav/calendars/daniel/personal/``), ``JARVIS_CALDAV_USER`` und
``JARVIS_CALDAV_PASSWORD`` (App-Passwort, nie das Hauptpasswort). Jeder Termin ist eine eigene ``.ics``-Ressource
(``PUT``/``DELETE``); gelesen wird mit einem ``calendar-query``-REPORT über den gefragten Zeitraum.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from xml.etree import ElementTree

from .agenda import Event, parse_ics
from .errors import JarvisError

log = logging.getLogger(__name__)

_NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}
REPORT_BODY = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><c:calendar-data/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">
    <c:time-range start="{start}" end="{end}"/>
  </c:comp-filter></c:comp-filter></c:filter>
</c:calendar-query>"""


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def event_ics(event: Event, uid: str) -> str:
    """Ein VEVENT in einem VCALENDAR – Zeiten in UTC, ganztägig als DATE."""
    if event.all_day:
        start = event.start.date()
        end = (event.end.date() if event.end else start + timedelta(days=1))
        timing = [f"DTSTART;VALUE=DATE:{start:%Y%m%d}", f"DTEND;VALUE=DATE:{end:%Y%m%d}"]
    else:
        end = event.end or event.start + timedelta(hours=1)
        timing = [f"DTSTART:{_utc(event.start)}", f"DTEND:{_utc(end)}"]
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//JARVIS//Kalender//DE", "BEGIN:VEVENT", f"UID:{uid}",
             f"DTSTAMP:{_utc(datetime.now(UTC))}", *timing, f"SUMMARY:{_escape(event.title)}"]
    if event.location:
        lines.append(f"LOCATION:{_escape(event.location)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


class CalDav:
    def __init__(self, url: str, user: str, password: str, *, tz: tzinfo, client: Any = None,
                 name: str = "CalDAV") -> None:
        if not url.startswith(("https://", "http://")):
            raise ValueError("CalDAV-Adresse muss mit https:// beginnen")
        self.url = url if url.endswith("/") else f"{url}/"
        self.auth = (user, password)
        self.tz = tz
        self.name = name
        self._client = client

    def _uid(self, event: Event) -> str:
        return f"{event.id}@jarvis"

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        import httpx

        client = self._client or httpx.AsyncClient(timeout=15.0, follow_redirects=True)
        try:
            response = await client.request(method, url, auth=self.auth, **kwargs)
        except httpx.HTTPError as exc:
            raise JarvisError("JRV-INT-001", f"CalDAV nicht erreichbar: {exc}",
                              user_message="Der Online-Kalender ist gerade nicht erreichbar.") from exc
        finally:
            if self._client is None:
                await client.aclose()
        if response.status_code in (401, 403):
            raise JarvisError("JRV-AUTH-001", f"CalDAV {response.status_code}",
                              user_message="Der Online-Kalender lehnt den Zugang ab – stimmen Benutzer und "
                                           "App-Passwort (JARVIS_CALDAV_USER/JARVIS_CALDAV_PASSWORD)?")
        return response

    async def put(self, event: Event) -> None:
        response = await self._request("PUT", f"{self.url}{event.id}.ics", content=event_ics(event, self._uid(event)),
                                       headers={"Content-Type": "text/calendar; charset=utf-8"})
        if response.status_code not in (200, 201, 204):
            raise JarvisError("JRV-INT-001", f"CalDAV PUT {response.status_code}",
                              user_message="Der Online-Kalender hat den Termin nicht angenommen.")

    async def delete(self, event: Event) -> None:
        response = await self._request("DELETE", f"{self.url}{event.id}.ics")
        if response.status_code not in (200, 204, 404):
            raise JarvisError("JRV-INT-001", f"CalDAV DELETE {response.status_code}")

    async def between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Termine im Zeitraum (roh wie ``parse_ics``) – Wiederholungen löst ``agenda.expand`` auf."""
        body = REPORT_BODY.format(start=_utc(start), end=_utc(end))
        response = await self._request("REPORT", self.url, content=body,
                                       headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
        if response.status_code != 207:
            raise JarvisError("JRV-INT-001", f"CalDAV REPORT {response.status_code}")
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise JarvisError("JRV-INT-001", f"CalDAV-Antwort unlesbar: {exc}") from exc
        events: list[dict[str, Any]] = []
        for data in root.iterfind(".//c:calendar-data", _NS):
            if data.text:
                for item in parse_ics(data.text, self.tz):
                    item["uid"] = item.get("uid") or ""
                    events.append(item)
        return events


def jarvis_ids(raw: list[dict[str, Any]]) -> set[str]:
    """Von JARVIS selbst eingetragene Termine (UID evt_…@jarvis) – liegen schon lokal vor."""
    return {m[1] for item in raw if (m := re.match(r"^(evt_[\w-]+)@jarvis$", str(item.get("uid") or "")))}
