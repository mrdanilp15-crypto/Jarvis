"""Anwesenheit über die Kamera: nur „angekommen/gegangen“ an den Server, Begrüßung mit nächstem Termin."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from jarvis.agenda import CalendarService
from jarvis.api import Container, create_app
from jarvis.context import Situation
from jarvis.events import InMemoryEventBus
from jarvis.llm.router import ModelRouter
from jarvis.policy import Principal
from jarvis.style import JarvisStyle
from jarvis.testing import ScriptedProvider

BERLIN = ZoneInfo("Europe/Berlin")
EVENING = datetime(2026, 10, 9, 18, 20, tzinfo=BERLIN)
DANIEL = Principal(actor="user:owner", role="adult", trust="trusted_user", name="Daniel", area="wohnzimmer")
GUEST = Principal(actor="guest:x", role="guest", trust="guest")


@pytest.mark.parametrize("minutes, now, event, text", [
    (25, EVENING, None, "Willkommen zurück, Sir."),
    (240, EVENING, None, "Willkommen zurück, Sir. Es ist 18:20 Uhr."),
    (600, datetime(2026, 10, 10, 7, 5, tzinfo=BERLIN), None, "Guten Morgen, Sir. Es ist 07:05 Uhr."),
    (30, EVENING, {"title": "Abendessen mit Max", "time": "19:30"},
     "Willkommen zurück, Sir. Ihr nächster Termin: Abendessen mit Max um 19:30 Uhr."),
])
def test_greeting(minutes, now, event, text):
    style = JarvisStyle()
    assert style.finalize(style.presence_text(minutes, now, event)) == text


def make_client(orchestrator, tmp_path):
    bus = InMemoryEventBus()
    calendar = CalendarService(tz=BERLIN, path=tmp_path / "calendar.json", clock=lambda: EVENING)
    calendar.add("Abendessen mit Max", EVENING + timedelta(minutes=70))
    calendar.add("Zahnarzt", EVENING + timedelta(days=1))
    orchestrator.style = JarvisStyle()
    container = Container(orchestrator=orchestrator, bus=bus, router=ModelRouter(local=ScriptedProvider([]), cloud=None),
                          tokens={"adult": DANIEL, "guest": GUEST}, webhook_secrets={},
                          situation=lambda who, ch: Situation(now=EVENING), calendar=calendar)
    published = []
    original = bus.publish

    async def publish(event):
        published.append(event)
        await original(event)

    bus.publish = publish
    return TestClient(create_app(container)), published


def test_arrival_greets_with_next_event_and_publishes(orchestrator, tmp_path):
    client, published = make_client(orchestrator, tmp_path)
    reply = client.post("/v1/presence", headers={"Authorization": "Bearer adult"},
                        json={"state": "arrived", "away_minutes": 45})
    assert reply.json()["text"] == "Willkommen zurück, Sir. Ihr nächster Termin: Abendessen mit Max um 19:30 Uhr."
    event = published[0]
    assert (event.type, event.actor, event.data) == ("jarvis.presence.changed", "user:owner",
                                                     {"state": "arrived", "away_minutes": 45, "area": "wohnzimmer"})


def test_leaving_only_publishes(orchestrator, tmp_path):
    client, published = make_client(orchestrator, tmp_path)
    reply = client.post("/v1/presence", headers={"Authorization": "Bearer adult"}, json={"state": "left"})
    assert reply.json() == {"text": None} and published[0].data["state"] == "left"


def test_guests_and_nonsense_are_refused(orchestrator, tmp_path):
    client, published = make_client(orchestrator, tmp_path)
    assert client.post("/v1/presence", headers={"Authorization": "Bearer guest"},
                       json={"state": "arrived"}).status_code == 403
    assert client.post("/v1/presence", headers={"Authorization": "Bearer adult"},
                       json={"state": "image", "away_minutes": 1}).status_code == 422
    assert published == []
