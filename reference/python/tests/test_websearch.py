"""Websuche: DuckDuckGo-Ergebnisseite auslesen, SearXNG, Fehlerfälle und die Capability ``web.search``."""

import asyncio

import httpx
import pytest

from jarvis.errors import JarvisError
from jarvis.tools import InvocationContext, ToolRegistry
from jarvis.websearch import WebSearch, ducky_url, parse_duckduckgo, register_web_capabilities, scoped

CTX = InvocationContext(correlation_id="c1", actor="user:alex", session_id="s1")

# Aufbau wie html.duckduckgo.com (gekürzt): eine Anzeige, zwei Treffer über den /l/-Umweg, einer direkt verlinkt
DDG_PAGE = """
<div class="serp__results"><div id="links" class="results">
<div class="result results_links results_links_deep result--ad ">
  <div class="links_main links_deep result__body"><h2 class="result__title">
  <a rel="nofollow" class="result__a" href="https://duckduckgo.com/y.js?ad_domain=werbung.de&amp;u3=x">Anzeige: Lasagne kaufen</a>
  </h2><a class="result__snippet" href="https://duckduckgo.com/y.js?x">Jetzt bestellen!</a></div></div>
<div class="result results_links results_links_deep web-result ">
  <div class="links_main links_deep result__body"><h2 class="result__title">
  <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.chefkoch.de%2Frezepte%2Flasagne&amp;rut=abc">Lasagne &amp; mehr – <b>Chefkoch</b></a>
  </h2><a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">Die beste <b>Lasagne</b> – klassisch mit Béchamel.</a></div></div>
<div class="result results_links results_links_deep web-result ">
  <div class="links_main links_deep result__body"><h2 class="result__title">
  <a rel="nofollow" href="https://www.lecker.de/lasagne" class="result__a">Lasagne al forno</a>
  </h2><a class="result__snippet" href="https://www.lecker.de/lasagne">Schritt für Schritt.</a></div></div>
<div class="result results_links results_links_deep web-result ">
  <div class="links_main links_deep result__body"><h2 class="result__title">
  <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.chefkoch.de%2Frezepte%2Flasagne&amp;rut=def">Doppelt</a>
  </h2></div></div>
</div></div>
"""


def test_duckduckgo_results_without_ads_and_redirects():
    results = parse_duckduckgo(DDG_PAGE)
    assert [r.url for r in results] == ["https://www.chefkoch.de/rezepte/lasagne", "https://www.lecker.de/lasagne"]
    assert results[0].title == "Lasagne & mehr – Chefkoch"
    assert results[0].snippet == "Die beste Lasagne – klassisch mit Béchamel."
    assert parse_duckduckgo(DDG_PAGE, limit=1)[0].url.endswith("/lasagne")


def test_browser_fallback_and_site_scope():
    assert ducky_url("Lasagne Rezept") == "https://duckduckgo.com/?q=%5CLasagne+Rezept"
    assert scoped("Lofi Musik", "youtube") == "Lofi Musik site:youtube.com"
    assert scoped("Lofi Musik", None) == "Lofi Musik"


def make_search(handler, **kwargs):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return WebSearch(client=client, **kwargs)


def test_duckduckgo_request_and_cache():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=DDG_PAGE)

    search = make_search(handler)
    first = asyncio.run(search.search("Lasagne"))
    again = asyncio.run(search.search("lasagne "))  # gleiche Anfrage: aus dem Cache
    assert first == again and len(calls) == 1
    assert calls[0].method == "POST" and b"q=Lasagne" in calls[0].content and b"kl=de-de" in calls[0].content


def test_bot_check_and_network_errors_are_reported():
    blocked = make_search(lambda r: httpx.Response(202, text="<html>anomaly-modal</html>"))
    with pytest.raises(JarvisError) as exc:
        asyncio.run(blocked.search("x"))
    assert "abgelehnt" in exc.value.user_message

    def offline(request):
        raise httpx.ConnectError("keine Verbindung")

    with pytest.raises(JarvisError) as exc:
        asyncio.run(make_search(offline).search("x"))
    assert exc.value.code == "JRV-INT-001"


def test_searxng_json_api():
    def handler(request):
        assert request.url.path == "/search" and request.url.params["format"] == "json"
        return httpx.Response(200, json={"results": [
            {"title": "Lasagne", "url": "https://www.chefkoch.de/lasagne", "content": "Rezept"},
            {"title": "Kaputt", "url": "javascript:alert(1)"},
        ]})

    results = asyncio.run(make_search(handler, searxng_url="http://searxng:8080/").search("Lasagne"))
    assert [r.url for r in results] == ["https://www.chefkoch.de/lasagne"]


def test_web_search_capability_marks_results_untrusted():
    registry = ToolRegistry()
    register_web_capabilities(registry, make_search(lambda r: httpx.Response(200, text=DDG_PAGE)))
    capability = registry.get("web.search")
    assert capability.output_trust == "untrusted" and capability.risk_class == "R0"
    result = asyncio.run(capability.handler({"query": "Lasagne", "count": 1}, CTX))
    assert result["results"] == [{"title": "Lasagne & mehr – Chefkoch", "url": "https://www.chefkoch.de/rezepte/lasagne",
                                  "snippet": "Die beste Lasagne – klassisch mit Béchamel."}]
    empty = make_search(lambda r: httpx.Response(200, text="<html></html>"))
    registry = ToolRegistry()
    register_web_capabilities(registry, empty)
    assert asyncio.run(registry.get("web.search").handler({"query": "xyz"}, CTX))["results"] == []
