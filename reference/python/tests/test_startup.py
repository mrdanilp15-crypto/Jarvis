"""Start-Sequenz: Systemcheck, Begrüßung mit Wetter und nächstem Termin."""

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
from jarvis.tools import Capability, ToolRegistry

BERLIN = ZoneInfo("Europe/Berlin")
MORNING = datetime(2026, 10, 9, 7, 30, tzinfo=BERLIN)
DANIEL = Principal(actor="user:owner", role="adult", trust="trusted_user", name="Daniel")
WEATHER = {"location": "Memmingen", "current": {"temperature_c": 8.4, "conditions": "bewölkt", "code": 3}}


@pytest.mark.parametrize("now, problems, weather, event, today, text", [
    (MORNING, [], WEATHER, {"title": "Zahnarzt", "time": "09:00"}, 2,
     "Guten Morgen, Sir. Alle Systeme einsatzbereit. Draußen 8 Grad, bewölkt. Ihr erster Termin: Zahnarzt um 09:00 Uhr."),
    (MORNING.replace(hour=14), ["die PC-Steuerung"], None, None, 0,
     "Guten Tag, Sir. Alle Systeme einsatzbereit – bis auf die PC-Steuerung. Heute stehen keine weiteren Termine an."),
    (MORNING.replace(hour=20), ["das Sprachmodell", "das Smart Home"], None, None, None,
     "Guten Abend, Sir. Einsatzbereit, allerdings ohne das Sprachmodell und das Smart Home."),
])
def test_startup_text(now, problems, weather, event, today, text):
    style = JarvisStyle()
    assert style.finalize(style.startup_text(now, problems, weather, event, today)) == text


def test_startup_endpoint(tmp_path):
    registry = ToolRegistry()
    seen = {}

    async def weather(args, ctx):
        seen.update(args)
        return WEATHER

    registry.register(Capability(name="info.weather", domain="info", risk_class="R0", description="Wetter",
                                 input_schema={"type": "object", "properties": {}}, handler=weather))
    calendar = CalendarService(tz=BERLIN, path=tmp_path / "calendar.json", clock=lambda: MORNING)
    calendar.add("Zahnarzt", MORNING + timedelta(minutes=90))
    orchestrator = type("O", (), {"style": JarvisStyle(), "registry": registry})()
    container = Container(orchestrator=orchestrator, bus=InMemoryEventBus(),
                          router=ModelRouter(local=ScriptedProvider([]), cloud=None), tokens={"a": DANIEL},
                          webhook_secrets={}, situation=lambda who, ch: Situation(now=MORNING), calendar=calendar)
    client = TestClient(create_app(container))
    data = client.get("/v1/startup?location=Memmingen", headers={"Authorization": "Bearer a"}).json()
    assert seen == {"location": "Memmingen"} and data["weather"] == WEATHER
    states = {c["id"]: c["state"] for c in data["checks"]}
    assert states["net"] == "ok" and states["calendar"] == "ok" and states["home"] == "off"
    assert "Zahnarzt um 09:00 Uhr" in data["text"] and data["text"].startswith("Guten Morgen, Sir.")
    assert all("problem" not in c for c in data["checks"])
    assert client.get("/v1/startup").status_code == 401
