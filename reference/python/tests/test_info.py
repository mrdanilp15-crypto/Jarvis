"""Wetter, Nachrichten und Wikipedia gegen nachgebildete API-Antworten (Formate wie bei den echten Diensten)."""

import asyncio

import pytest

httpx = pytest.importorskip("httpx")

from jarvis.info import InfoConfig, parse_feed, register_info_capabilities  # noqa: E402
from jarvis.tools import InvocationContext, ToolRegistry  # noqa: E402

CTX = InvocationContext(correlation_id="c1", actor="user:alex", session_id="s1", area="wohnzimmer")

GEO = {"results": [{"name": "Berlin", "latitude": 52.52437, "longitude": 13.41053, "admin1": "Berlin",
                    "country": "Deutschland"}], "generationtime_ms": 0.6}
FORECAST = {
    "latitude": 52.52, "longitude": 13.42, "timezone": "Europe/Berlin",
    "current": {"time": "2026-09-27T10:15", "interval": 900, "temperature_2m": 14.3, "apparent_temperature": 12.9,
                "relative_humidity_2m": 71, "precipitation": 0.0, "weather_code": 3, "wind_speed_10m": 11.2},
    "daily": {"time": ["2026-09-27", "2026-09-28"], "weather_code": [3, 61], "temperature_2m_max": [16.1, 13.4],
              "temperature_2m_min": [8.2, 9.0], "precipitation_probability_max": [10, 80],
              "precipitation_sum": [0.0, 4.6], "sunrise": ["2026-09-27T07:05", "2026-09-28T07:07"],
              "sunset": ["2026-09-27T19:01", "2026-09-28T18:59"]},
    "hourly": {"time": [f"2026-09-27T{h:02d}:00" for h in range(24)] + [f"2026-09-28T{h:02d}:00" for h in range(24)],
               "temperature_2m": [10 + h / 4 for h in range(48)], "precipitation_probability": [h % 50 for h in range(48)],
               "weather_code": [3] * 48},
}
RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>tagesschau.de</title>
<item><title>Erste Meldung</title><link>https://www.tagesschau.de/a</link>
<description><![CDATA[<p>Kurztext &amp; mehr</p>]]></description><pubDate>Sun, 27 Sep 2026 09:30:00 +0200</pubDate></item>
<item><title>Zweite Meldung</title><link>https://www.tagesschau.de/b</link><description>Text B</description></item>
</channel></rss>"""
ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Atom-Meldung</title><link href="https://example.org/x"/><updated>2026-09-27T08:00:00Z</updated>
<summary>Zusammenfassung</summary></entry></feed>"""


def make_registry(calls, home_location=None):
    def handler(request):
        calls.append(str(request.url))
        host, path = request.url.host, request.url.path
        if host == "geocoding-api.open-meteo.com":
            name = request.url.params["name"]
            return httpx.Response(200, json=GEO if name == "Berlin" else {"generationtime_ms": 0.2})
        if host == "api.open-meteo.com":
            return httpx.Response(200, json=FORECAST)
        if host == "www.tagesschau.de":
            return httpx.Response(200, text=RSS)
        if path.endswith("/search/page"):
            return httpx.Response(200, json={"pages": [{"id": 1, "key": "Albert_Einstein", "title": "Albert Einstein"}]})
        if path.startswith("/api/rest_v1/page/summary/"):
            return httpx.Response(200, json={
                "title": "Albert Einstein", "description": "deutscher Physiker",
                "extract": "Albert Einstein war ein Physiker …",
                "content_urls": {"desktop": {"page": "https://de.wikipedia.org/wiki/Albert_Einstein"}}})
        return httpx.Response(404)

    registry = ToolRegistry()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    register_info_capabilities(registry, InfoConfig(home_location=home_location), client=client)
    return registry


def run(registry, name, args):
    cap = registry.get(name)
    assert cap.validate(args) == []
    return asyncio.run(cap.handler(args, CTX))


def test_weather_current_and_forecast():
    calls = []
    registry = make_registry(calls)
    result = run(registry, "info.weather", {"location": "Berlin", "days": 2})
    assert result["location"] == "Berlin, Deutschland"  # doppelte Teile zusammengefasst
    assert result["current"]["conditions"] == "bedeckt" and result["current"]["temperature_c"] == 14.3
    assert result["forecast"][1] == {"date": "2026-09-28", "conditions": "leichter Regen", "code": 61,
                                     "temp_max_c": 13.4, "temp_min_c": 9.0, "precipitation_probability_pct": 80,
                                     "precipitation_mm": 4.6}
    assert result["current"]["code"] == 3 and result["current"]["is_day"] is True  # für das Wettersymbol
    # Für die Anzeige: die nächsten 24 Stunden ab jetzt (10:15 -> ab 10 Uhr), Sonne, Woche
    assert len(result["hourly"]) == 24 and result["hourly"][0] == {"time": "10:00", "temp_c": 12.5, "rain_pct": 10,
                                                                    "code": 3}
    assert result["hourly"][-1]["time"] == "09:00"
    assert result["sun"] == {"rise": "2026-09-27T07:05", "set": "2026-09-27T19:01"}
    assert [d["date"] for d in result["week"]] == ["2026-09-27", "2026-09-28"]
    run(registry, "info.weather", {"location": "Berlin", "days": 2})
    assert len(calls) == 2  # zweiter Aufruf kommt aus dem Cache


def test_weather_location_fallbacks():
    calls = []
    assert "Kein Ort" in run(make_registry(calls), "info.weather", {})["error"]
    assert run(make_registry(calls, home_location="Berlin"), "info.weather", {})["location"] == "Berlin, Deutschland"
    missing = run(make_registry(calls), "info.weather", {"location": "Nirgendwo, Mond"})
    assert "nicht gefunden" in missing["error"]


def test_news_headlines_are_untrusted():
    registry = make_registry([])
    assert registry.get("info.news").output_trust == "untrusted"
    result = run(registry, "info.news", {"count": 1})
    assert result["source"] == "tagesschau"
    assert result["headlines"] == [{"title": "Erste Meldung", "summary": "Kurztext & mehr",
                                    "published": "2026-09-27T09:30:00+02:00", "link": "https://www.tagesschau.de/a"}]


def test_atom_feeds_are_parsed():
    assert parse_feed(ATOM, 5) == [{"title": "Atom-Meldung", "summary": "Zusammenfassung",
                                    "published": "2026-09-27T08:00:00Z", "link": "https://example.org/x"}]


def test_wikipedia_summary():
    calls = []
    result = run(make_registry(calls), "info.wikipedia", {"query": "Einstein Relativitätstheorie"})
    assert result["title"] == "Albert Einstein" and result["summary"].startswith("Albert Einstein war")
    assert result["url"].endswith("/wiki/Albert_Einstein")
    assert make_registry([]).get("info.wikipedia").output_trust == "untrusted"


def test_unreachable_service_is_a_clear_integration_error():
    from jarvis.errors import JarvisError

    def handler(request):
        raise httpx.ConnectError("offline", request=request)

    registry = ToolRegistry()
    register_info_capabilities(registry, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(JarvisError) as exc:
        run(registry, "info.news", {})
    assert exc.value.code == "JRV-INT-001" and exc.value.user_message


def test_news_via_orchestrator_taints_session(orchestrator, situation):
    from jarvis.orchestrator import TurnRequest
    from jarvis.testing import ScriptedProvider, call_tool, say

    from conftest import ALEX

    register = make_registry([])
    orchestrator.registry.register(register.get("info.news"))
    provider = ScriptedProvider([call_tool("info__news", {"count": 2}), say("Hier die Nachrichten, Sir.")])
    result = asyncio.run(orchestrator.handle_turn(TurnRequest(text="Was ist in der Politik los?", session_id="n1",
                                                              principal=ALEX), provider=provider,
                                                  situation=situation))
    assert result.text == "Hier die Nachrichten, Sir." and result.tainted
    assert result.actions[0].result["headlines"][0]["title"] == "Erste Meldung"


def test_weather_and_news_without_the_model(orchestrator, situation):
    """Wetter und Nachrichten laufen direkt – das kleine Sprachmodell riet hier sonst gern."""
    from jarvis.orchestrator import TurnRequest
    from jarvis.testing import ScriptedProvider

    from conftest import ALEX

    register = make_registry([])
    orchestrator.registry.register(register.get("info.news"))
    orchestrator.registry.register(register.get("info.weather"))

    def ask(text, session="w1"):
        return asyncio.run(orchestrator.handle_turn(TurnRequest(text=text, session_id=session, principal=ALEX),
                                                    provider=ScriptedProvider([]), situation=situation))

    weather = ask("Wie wird das Wetter morgen in Berlin?")
    assert weather.route == "fast_path" and weather.actions[0].arguments == {"days": 2, "location": "Berlin"}
    assert "Morgen in Berlin: leichter Regen, 9 bis 13 Grad, Regenwahrscheinlichkeit 80 Prozent." in weather.text
    news = ask("Nachrichten", session="w2")
    assert news.route == "fast_path" and "Erste Meldung" in news.text and news.tainted
