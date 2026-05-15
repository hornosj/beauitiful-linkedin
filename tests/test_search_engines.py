import json
import logging

import httpx

from beautiful_linkedin.cli import parse_search_engines
from beautiful_linkedin.config import Settings
from beautiful_linkedin.models import SearchResult
from beautiful_linkedin.search.brave_search import BraveSearchEngine
from beautiful_linkedin.search.brave_html_search import BraveHtmlSearchEngine
from beautiful_linkedin.search.composite_search import CompositeSearchEngine
from beautiful_linkedin.search.duckduckgo_html_search import DuckDuckGoHtmlSearchEngine
from beautiful_linkedin.search.google_custom_search import GoogleCustomSearchEngine
from beautiful_linkedin.search.factory import build_search_engine
from beautiful_linkedin.search.searxng_search import SearxngSearchEngine
from beautiful_linkedin.search.serper_search import SerperSearchEngine
from beautiful_linkedin.search.smart_composite_search import SmartCompositeSearchEngine


def test_composite_search_engine_deduplicates_by_url_and_preserves_sources():
    class FakeEngine:
        def __init__(self, source_type):
            self.source_type = source_type

        def search(self, query, max_results):
            from beautiful_linkedin.models import SearchResult

            return [
                SearchResult(
                    title=f"{self.source_type} result",
                    url="https://br.linkedin.com/in/sample",
                    snippet="same result",
                    source_type=self.source_type,
                )
            ]

    engine = CompositeSearchEngine([FakeEngine("search_duckduckgo"), FakeEngine("search_brave")])

    results = engine.search("query", max_results=10)

    assert len(results) == 1
    assert results[0].source_type == "search_duckduckgo"


def test_brave_search_engine_normalizes_web_results():
    def handler(request):
        assert request.headers["X-Subscription-Token"] == "token"
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Fulana - RH - Banco Safra",
                            "url": "https://br.linkedin.com/in/fulana",
                            "description": "Public snippet",
                        }
                    ]
                }
            },
        )

    engine = BraveSearchEngine(
        api_key="token",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )

    results = engine.search("query", max_results=5)

    assert results[0].source_type == "search_brave"
    assert results[0].url == "https://br.linkedin.com/in/fulana"


def test_brave_html_search_engine_extracts_public_profile_links():
    html = """
    <html>
      <body>
        <a href="https://br.linkedin.com/in/vitor-sales-a6a82b97">
          LinkedIn br.linkedin.com › in › vitor-sales-a6a82b97
          Vitor Sales - Product Operations Lead @ Nubank
        </a>
      </body>
    </html>
    """

    def handler(request):
        assert request.url.host == "search.brave.com"
        assert request.url.params["q"] == 'site:linkedin.com/in "Nubank" "sales"'
        return httpx.Response(200, text=html)

    engine = BraveHtmlSearchEngine(
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )

    results = engine.search('site:linkedin.com/in "Nubank" "sales"', max_results=5)

    assert len(results) == 1
    assert results[0].source_type == "search_brave_html"
    assert results[0].url == "https://br.linkedin.com/in/vitor-sales-a6a82b97"
    assert results[0].title == "Vitor Sales - Product Operations Lead @ Nubank"


def test_duckduckgo_html_search_engine_decodes_public_result_links():
    html = """
    <html>
      <body>
        <div class="result">
          <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fbr.linkedin.com%2Fin%2Frafael-sales&amp;rut=abc">
            Rafael Sales - Nubank | LinkedIn
          </a>
          <a class="result__snippet">Sales Specialist at Nubank</a>
        </div>
      </body>
    </html>
    """

    def handler(request):
        assert request.url.host == "html.duckduckgo.com"
        assert request.url.params["q"] == 'site:linkedin.com/in "Nubank" "sales"'
        return httpx.Response(200, text=html)

    engine = DuckDuckGoHtmlSearchEngine(
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )

    results = engine.search('site:linkedin.com/in "Nubank" "sales"', max_results=5)

    assert len(results) == 1
    assert results[0].source_type == "search_duckduckgo_html"
    assert results[0].url == "https://br.linkedin.com/in/rafael-sales"
    assert results[0].snippet == "Sales Specialist at Nubank"


def test_duckduckgo_html_search_engine_reports_bot_challenge(caplog):
    def handler(request):
        return httpx.Response(
            202,
            text="Unfortunately, bots use DuckDuckGo too. Please complete the following challenge.",
        )

    engine = DuckDuckGoHtmlSearchEngine(
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )

    results = engine.search('site:linkedin.com/in "Nubank" "sales"', max_results=5)

    assert results == []
    assert "bloqueou a automacao" in caplog.text


def test_google_custom_search_engine_normalizes_items():
    def handler(request):
        assert request.url.params["key"] == "key"
        assert request.url.params["cx"] == "cx"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "title": "Fulana - RH - Banco Safra",
                        "link": "https://br.linkedin.com/in/fulana",
                        "snippet": "Public snippet",
                    }
                ]
            },
        )

    engine = GoogleCustomSearchEngine(
        api_key="key",
        search_engine_id="cx",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )

    results = engine.search("query", max_results=5)

    assert results[0].source_type == "search_google_cse"
    assert results[0].snippet == "Public snippet"


class _RecordingEngine:
    """Test fake that always returns the URLs it's seeded with and records calls."""

    def __init__(self, source_type: str, urls: list[str]) -> None:
        self.source_type = source_type
        self.urls = urls
        self.calls = 0

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        self.calls += 1
        return [
            SearchResult(
                title=f"{self.source_type} - {url}",
                url=url,
                snippet=f"snippet from {self.source_type}",
                source_type=self.source_type,
            )
            for url in self.urls[:max_results]
        ]


def test_smart_composite_queries_every_engine_even_when_first_returns_enough():
    brave = _RecordingEngine(
        "search_brave_html",
        [f"https://br.linkedin.com/in/brave-{i}" for i in range(20)],
    )
    duck = _RecordingEngine(
        "search_duckduckgo_html",
        [f"https://br.linkedin.com/in/duck-{i}" for i in range(15)],
    )
    serper = _RecordingEngine(
        "search_serper",
        [f"https://br.linkedin.com/in/serper-{i}" for i in range(15)],
    )

    composite = SmartCompositeSearchEngine([brave, duck, serper], parallel=False)
    results = composite.search("query", max_results=20)

    assert brave.calls == 1, "Brave engine must be queried"
    assert duck.calls == 1, "DuckDuckGo engine must be queried"
    assert serper.calls == 1, "Serper engine must be queried"

    sources = {result.source_type for result in results}
    assert "search_brave_html" in sources
    assert "search_duckduckgo_html" in sources
    assert "search_serper" in sources

    urls = [result.url for result in results]
    assert len(urls) == len(set(urls)), "Composite must dedupe URLs across engines"


def test_smart_composite_attributes_duplicates_to_first_engine_in_priority_order():
    duplicate_url = "https://br.linkedin.com/in/duplicate"
    brave = _RecordingEngine("search_brave_html", [duplicate_url])
    duck = _RecordingEngine("search_duckduckgo_html", [duplicate_url])

    composite = SmartCompositeSearchEngine([brave, duck], parallel=False)
    results = composite.search("query", max_results=10)

    assert len(results) == 1
    assert results[0].source_type == "search_brave_html", (
        "First engine in priority order must own the duplicate URL"
    )


def test_smart_composite_keeps_querying_when_one_engine_fails():
    class FailingEngine:
        def __init__(self) -> None:
            self.calls = 0

        def search(self, query: str, max_results: int) -> list[SearchResult]:
            self.calls += 1
            raise RuntimeError("simulated outage")

    failing = FailingEngine()
    healthy = _RecordingEngine(
        "search_serper", ["https://br.linkedin.com/in/healthy-1"]
    )

    composite = SmartCompositeSearchEngine([failing, healthy], parallel=False)
    results = composite.search("query", max_results=5)

    assert failing.calls == 1
    assert healthy.calls == 1
    assert len(results) == 1
    assert results[0].source_type == "search_serper"


def test_smart_composite_runs_engines_in_parallel_when_enabled():
    import threading
    import time

    barrier = threading.Barrier(3, timeout=2.0)

    class BarrierEngine:
        def __init__(self, source_type: str) -> None:
            self.source_type = source_type
            self.calls = 0

        def search(self, query: str, max_results: int) -> list[SearchResult]:
            self.calls += 1
            barrier.wait()
            return [
                SearchResult(
                    title=self.source_type,
                    url=f"https://br.linkedin.com/in/{self.source_type}",
                    snippet="",
                    source_type=self.source_type,
                )
            ]

    engines = [
        BarrierEngine("search_a"),
        BarrierEngine("search_b"),
        BarrierEngine("search_c"),
    ]
    composite = SmartCompositeSearchEngine(engines, parallel=True)
    results = composite.search("query", max_results=5)

    assert len(results) == 3
    for engine in engines:
        assert engine.calls == 1


def test_serper_search_engine_normalizes_organic_results():
    def handler(request):
        assert request.headers["X-API-KEY"] == "token"
        return httpx.Response(
            200,
            json={
                "organic": [
                    {
                        "title": "Fulana - RH - Banco Safra",
                        "link": "https://br.linkedin.com/in/fulana",
                        "snippet": "Public snippet",
                    }
                ]
            },
        )

    engine = SerperSearchEngine(
        api_key="token",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )

    results = engine.search("query", max_results=5)

    assert results[0].source_type == "search_serper"


def test_serper_search_engine_retries_advanced_query_as_simplified_query_on_400():
    calls = []

    def handler(request):
        payload = json.loads(request.content.decode())
        calls.append(payload["q"])
        if len(calls) == 1:
            return httpx.Response(
                400,
                request=request,
                json={"message": "Query not accepted", "statusCode": 400},
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "organic": [
                    {
                        "title": "Fulana - RH - Banco Safra",
                        "link": "https://br.linkedin.com/in/fulana",
                        "snippet": "Public snippet",
                    }
                ]
            },
        )

    engine = SerperSearchEngine(
        api_key="token",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )

    results = engine.search('site:linkedin.com/in "Banco Safra" "rh"', max_results=5)

    assert len(results) == 1
    assert calls == [
        'site:linkedin.com/in "Banco Safra" "rh"',
        "Banco Safra rh linkedin.com/in",
    ]


def test_serper_search_engine_skips_blank_query_without_http_call(caplog):
    def handler(request):
        raise AssertionError("Blank queries should not call Serper")

    engine = SerperSearchEngine(
        api_key="token",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )

    results = engine.search("   ", max_results=5)

    assert results == []
    assert "query vazia" in caplog.text


def test_searxng_search_engine_normalizes_results():
    def handler(request):
        assert request.url.path == "/search"
        assert request.url.params["q"] == "Banco Safra rh linkedin.com/in"
        assert request.url.params["format"] == "json"
        assert request.headers["Accept"] == "application/json"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://br.linkedin.com/in/fulana",
                        "title": "Fulana Silva - RH - Banco Safra",
                        "content": "Public snippet",
                    }
                ]
            },
        )

    engine = SearxngSearchEngine(
        base_url="http://localhost:8080/",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )

    results = engine.search("Banco Safra rh linkedin.com/in", max_results=5)

    assert len(results) == 1
    assert results[0].source_type == "search_searxng"
    assert results[0].snippet == "Public snippet"


def test_searxng_search_engine_returns_empty_when_base_url_blank(caplog):
    def handler(request):
        raise AssertionError("SearxNG sem base_url não deve chamar HTTP")

    engine = SearxngSearchEngine(
        base_url="",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )

    with caplog.at_level(logging.WARNING, logger="beautiful_linkedin.search.searxng_search"):
        results = engine.search("query", max_results=5)

    assert results == []
    assert "SEARXNG_BASE_URL não configurada" in caplog.text


def test_searxng_logs_http_error_with_body_excerpt(caplog):
    def handler(request):
        return httpx.Response(502, text="bad gateway")

    engine = SearxngSearchEngine(
        base_url="http://localhost:8080",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )

    with caplog.at_level(logging.WARNING, logger="beautiful_linkedin.search.searxng_search"):
        results = engine.search("query", max_results=5)

    assert results == []
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "SearxNG retornou HTTP 502" in messages
    assert "bad gateway" in messages


def test_searxng_filters_non_linkedin_urls():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": "http://www.victorianweb.org/sculpture/x.html", "title": "junk"},
                    {"url": "https://br.linkedin.com/in/fulana", "title": "Fulana"},
                    {"url": "https://financial-dictionary.thefreedictionary.com/ml", "title": "noise"},
                    {"url": "https://www.linkedin.com/in/sicrano", "title": "Sicrano"},
                    {"url": "https://www.hse.ru/en/edu/courses/646525970", "title": "noise"},
                ]
            },
        )

    engine = SearxngSearchEngine(
        base_url="http://localhost:8080",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )

    results = engine.search("anything", max_results=10)

    urls = [r.url for r in results]
    assert urls == [
        "https://br.linkedin.com/in/fulana",
        "https://www.linkedin.com/in/sicrano",
    ]


def test_searxng_filter_runs_before_max_results_cap():
    # If junk URLs were applied AFTER the cap, max_results=2 with the input
    # below would produce 0 LinkedIn results (cap takes the first two junk
    # entries). The pre-filter must give us both linkedin profiles.
    def handler(request):
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": "http://junk1.example.com", "title": "junk"},
                    {"url": "http://junk2.example.com", "title": "junk"},
                    {"url": "https://br.linkedin.com/in/a", "title": "A"},
                    {"url": "https://br.linkedin.com/in/b", "title": "B"},
                ]
            },
        )

    engine = SearxngSearchEngine(
        base_url="http://localhost:8080",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )

    results = engine.search("anything", max_results=2)
    assert [r.url for r in results] == [
        "https://br.linkedin.com/in/a",
        "https://br.linkedin.com/in/b",
    ]


def test_searxng_logs_unresponsive_engines_when_zero_results(caplog):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "results": [],
                "unresponsive_engines": [
                    ["duckduckgo", "Suspended: CAPTCHA"],
                    ["wikipedia", "HTTP error"],
                ],
            },
        )

    engine = SearxngSearchEngine(
        base_url="http://localhost:8080",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )

    with caplog.at_level(logging.WARNING, logger="beautiful_linkedin.search.searxng_search"):
        results = engine.search("Banco Safra rh", max_results=5)

    assert results == []
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "0 resultados" in messages
    assert "Engines mortas" in messages
    assert "duckduckgo: Suspended: CAPTCHA" in messages
    assert "wikipedia: HTTP error" in messages


def test_searxng_logs_degraded_when_some_results_returned(caplog):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://br.linkedin.com/in/fulana",
                        "title": "Fulana - RH",
                        "content": "Snippet",
                    }
                ],
                "unresponsive_engines": [["brave", "Suspended: too many requests"]],
            },
        )

    engine = SearxngSearchEngine(
        base_url="http://localhost:8080",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )

    with caplog.at_level(logging.INFO, logger="beautiful_linkedin.search.searxng_search"):
        results = engine.search("query", max_results=5)

    assert len(results) == 1
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "degradado" in messages
    assert "brave: Suspended: too many requests" in messages


def test_searxng_quiet_when_zero_results_but_all_engines_responded(caplog):
    def handler(request):
        return httpx.Response(200, json={"results": [], "unresponsive_engines": []})

    engine = SearxngSearchEngine(
        base_url="http://localhost:8080",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )

    with caplog.at_level(logging.INFO, logger="beautiful_linkedin.search.searxng_search"):
        results = engine.search("query", max_results=5)

    assert results == []
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "engines responderam, sem matches" in messages
    # Nada deve ter saído como WARNING quando todos responderam.
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)


def test_parse_search_engines_accepts_searxng_aliases():
    assert parse_search_engines("searxng,searx") == ["searxng", "searx"]


def test_search_factory_creates_searxng_engine_when_configured():
    engine = build_search_engine(Settings(searxng_base_url="http://localhost:8080"), ["searxng"])

    assert isinstance(engine, SearxngSearchEngine)


def test_search_factory_skips_searxng_without_base_url():
    engine = build_search_engine(Settings(), ["searxng"])

    assert not isinstance(engine, SearxngSearchEngine)


def test_search_factory_auto_prioritizes_searxng_when_configured():
    engine = build_search_engine(
        Settings(searxng_base_url="http://localhost:8080", serper_api_key="serper"),
        ["auto"],
    )

    assert isinstance(engine, SmartCompositeSearchEngine)
    assert isinstance(engine.engines[0], SearxngSearchEngine)
