"""Börsenkurse, Online-Kalender (CalDAV) und Radio auf Lautsprechern – gegen nachgebildete Server (httpx.MockTransport)."""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from jarvis.agenda import CalendarService, register_calendar_capabilities
from jarvis.caldav import CalDav, event_ics
from jarvis.connectors.homeassistant import register_home_capabilities
from jarvis.errors import JarvisError
from jarvis.radio import find_station
from jarvis.stocks import register_stock_capabilities, resolve
from jarvis.style import JarvisStyle, stock_text
from jarvis.testing import FakeHome
from jarvis.tools import InvocationContext, ToolRegistry

CTX = InvocationContext(correlation_id="c", actor="user:owner", session_id="s")
BERLIN = ZoneInfo("Europe/Berlin")
HISTORY = "Date,Open,High,Low,Close,Volume\n2026-10-06,220,223,219,222.5,1\n2026-10-07,222,226,221,224.8,1\n" \
          "2026-10-08,225,229,224.5,227.48,1\n"


def run(registry, name, args):
    cap = registry.get(name)
    assert not cap.validate(args), args
    return asyncio.run(cap.handler(args, CTX))


# -- Börse --------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name, symbol", [("Apple", "aapl.us"), ("der DAX", "^dax"), ("Tesla-Aktie", "tsla.us"),
                                          ("SAP", "sap.de"), ("Bitcoin", "btcusd"), ("PLTR", "pltr.us")])
def test_names_become_symbols(name, symbol):
    assert resolve(name)[0] == symbol


def test_unknown_long_name_is_answered_honestly():
    with pytest.raises(JarvisError) as exc:
        resolve("Hensoldt Holding")
    assert "kein Börsenkürzel" in exc.value.user_message


def test_stock_quote_and_text():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=HISTORY)

    registry = ToolRegistry()
    register_stock_capabilities(registry, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    data = run(registry, "info.stock", {"name": "Apple"})
    assert "s=aapl.us" in seen[0]
    assert (data["close"], data["change_pct"], data["low_4w"], data["high_4w"]) == (227.48, 1.19, 219.0, 229.0)
    assert stock_text(data) == ("Apple schloss am 8. Oktober bei 227,48 Dollar – 1,2 Prozent höher als am Vortag. "
                                "Spanne der letzten vier Wochen: 219,00 bis 229,00.")


def test_stock_without_data():
    registry = ToolRegistry()
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="No data")))
    register_stock_capabilities(registry, client=client)
    with pytest.raises(JarvisError) as exc:
        run(registry, "info.stock", {"name": "Apple"})
    assert "keine Kurse" in exc.value.user_message


# -- CalDAV -------------------------------------------------------------------------------------------------------
class FakeCalDav:
    """Minimaler CalDAV-Server: PUT/DELETE je Termin, REPORT liefert alle gespeicherten."""

    def __init__(self):
        self.store, self.requests = {}, []
        self.store["fremd.ics"] = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:abc@nextcloud\r\nDTSTART:20261010T080000Z\r\n"
                                   "DTEND:20261010T090000Z\r\nSUMMARY:Elternabend\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")

    def __call__(self, request):
        self.requests.append((request.method, request.url.path, request.headers.get("authorization", "")))
        name = request.url.path.rsplit("/", 1)[-1]
        if request.method == "PUT":
            self.store[name] = request.content.decode()
            return httpx.Response(201)
        if request.method == "DELETE":
            return httpx.Response(204 if self.store.pop(name, None) else 404)
        if request.method == "REPORT":
            parts = "".join(f"<d:response><d:propstat><d:prop><c:calendar-data>{ics}</c:calendar-data></d:prop>"
                            f"</d:propstat></d:response>" for ics in self.store.values())
            return httpx.Response(207, text=f'<d:multistatus xmlns:d="DAV:" '
                                            f'xmlns:c="urn:ietf:params:xml:ns:caldav">{parts}</d:multistatus>')
        return httpx.Response(405)


def calendar_with(server, tmp_path):
    remote = CalDav("https://cloud.example.de/remote.php/dav/calendars/daniel/personal", "daniel", "app-pass",
                    tz=BERLIN, client=httpx.AsyncClient(transport=httpx.MockTransport(server)), name="Nextcloud")
    calendar = CalendarService(tz=BERLIN, path=tmp_path / "calendar.json", remote=remote,
                               clock=lambda: datetime(2026, 10, 9, 9, 0, tzinfo=BERLIN))
    registry = ToolRegistry()
    register_calendar_capabilities(registry, calendar)
    return registry, calendar


def test_events_go_to_nextcloud_and_come_back_once(tmp_path):
    server = FakeCalDav()
    registry, calendar = calendar_with(server, tmp_path)
    added = run(registry, "calendar.add", {"title": "Zahnarzt, Kontrolle", "start": "2026-10-10T15:00"})
    assert added["synced"] is True
    method, path, auth = server.requests[0]
    assert (method, auth.startswith("Basic ")) == ("PUT", True) and path.endswith(".ics")
    ics = next(v for k, v in server.store.items() if k != "fremd.ics")
    assert "SUMMARY:Zahnarzt\\, Kontrolle" in ics and "DTSTART:20261010T130000Z" in ics  # UTC
    listing = run(registry, "calendar.list", {"day": "2026-10-10"})
    titles = [e["title"] for e in listing["events"]]
    assert titles == ["Elternabend", "Zahnarzt, Kontrolle"]  # eigener Termin nicht doppelt
    assert [e["calendar"] for e in listing["events"]][0] == "Nextcloud"
    run(registry, "calendar.delete", {"title": "Zahnarzt"})
    assert list(server.store) == ["fremd.ics"]


def test_unreachable_nextcloud_keeps_the_local_event(tmp_path):
    registry, calendar = calendar_with(lambda request: httpx.Response(401), tmp_path)
    added = run(registry, "calendar.add", {"title": "Friseur", "start": "2026-10-12"})
    assert added["synced"] is False and calendar.events[0].title == "Friseur"
    from jarvis.orchestrator import ActionRecord  # noqa: F401  – Antworttext über die Stil-Engine
    text = JarvisStyle()._success("calendar.add", {"title": "Friseur"}, added, {})[1]
    assert text.endswith("Im Online-Kalender konnte ich ihn nicht eintragen – er steht vorerst nur bei mir.")


def test_all_day_event_ics():
    from jarvis.agenda import Event

    ics = event_ics(Event("Urlaub", datetime(2026, 10, 12, tzinfo=BERLIN), all_day=True), "evt_1@jarvis")
    assert "DTSTART;VALUE=DATE:20261012" in ics and "DTEND;VALUE=DATE:20261013" in ics


# -- Radio --------------------------------------------------------------------------------------------------------
STATIONS = [{"name": "Bob Marley Radio", "url_resolved": "https://a/mp3", "countrycode": "US", "votes": 900},
            {"name": "RADIO BOB!", "url_resolved": "https://streams.radiobob.de/bob-live/mp3", "countrycode": "DE",
             "votes": 400}]


def test_station_search_prefers_german_and_exact_names():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=STATIONS)))
    assert asyncio.run(find_station("Radio Bob", client=client))["url"] == "https://streams.radiobob.de/bob-live/mp3"


def test_station_search_tries_mirrors_and_reports_absence():
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json=[])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(JarvisError) as exc:
        asyncio.run(find_station("Gibtsnicht FM", client=client))
    assert len(calls) == 2 and "finde ich nicht" in exc.value.user_message


def test_radio_plays_on_the_speaker(monkeypatch):
    home = FakeHome({"media_player.wohnzimmer": {"state": "idle", "attributes": {"friendly_name": "Sonos"}}})
    registry = ToolRegistry()
    register_home_capabilities(registry, home)

    async def fake_find(name):
        return {"name": "RADIO BOB!", "url": "https://streams.radiobob.de/bob-live/mp3"}

    monkeypatch.setattr("jarvis.radio.find_station", fake_find)
    result = run(registry, "home.play_radio", {"station": "bob", "entity_id": "media_player.wohnzimmer", "volume": 0.3})
    assert result["station"] == "RADIO BOB!"
    assert home.calls == [
        ("media_player", "volume_set", {"entity_id": "media_player.wohnzimmer"}, {"volume_level": 0.3}),
        ("media_player", "play_media", {"entity_id": "media_player.wohnzimmer"},
         {"media_content_id": "https://streams.radiobob.de/bob-live/mp3", "media_content_type": "music"})]
    reply = JarvisStyle()._success("home.play_radio", {}, result, {"location": "im Wohnzimmer"})[1]
    assert JarvisStyle().finalize(reply) == "RADIO BOB! läuft im Wohnzimmer."
