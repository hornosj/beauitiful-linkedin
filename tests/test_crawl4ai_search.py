from __future__ import annotations

from types import SimpleNamespace

from beautiful_linkedin.cli import parse_search_engines
from beautiful_linkedin.config import Settings
from beautiful_linkedin.search.crawl4ai_search import (
    Crawl4aiSearchEngine,
    build_candidate_urls,
    extract_profiles_from_results,
)
from beautiful_linkedin.search.factory import build_search_engine


def test_parse_search_engines_accepts_crawl4ai_aliases():
    assert parse_search_engines("crawl4ai,crawl-4-ai") == ["crawl4ai", "crawl-4-ai"]


def test_search_factory_creates_crawl4ai_engine():
    engine = build_search_engine(Settings(), ["crawl4ai"])

    assert isinstance(engine, Crawl4aiSearchEngine)


def test_build_candidate_urls_extracts_domain_and_expands_paths():
    query = 'site:linkedin.com/in "Nubank" "marketing" "nubank.com.br"'

    urls = build_candidate_urls(
        query, team_paths=("/", "/team", "/sobre"), max_paths_per_site=3
    )

    assert urls == [
        "https://nubank.com.br/",
        "https://nubank.com.br/team",
        "https://nubank.com.br/sobre",
    ]


def test_build_candidate_urls_filters_blocked_hosts():
    query = '"linkedin.com" "google.com" "x.com"'

    assert build_candidate_urls(query, team_paths=("/",), max_paths_per_site=1) == []


def test_build_candidate_urls_returns_empty_when_no_domain_in_query():
    assert build_candidate_urls(
        'site:linkedin.com/in "Acme" "marketing"',
        team_paths=("/",),
        max_paths_per_site=1,
    ) == []


def test_extract_profiles_finds_linkedin_in_markdown_and_links():
    fake_result = SimpleNamespace(
        url="https://nubank.com.br/team",
        success=True,
        metadata={"title": "Equipe — Nubank"},
        markdown=(
            "Conheca nosso time. Visite o perfil em "
            "https://www.linkedin.com/in/fulana-da-silva e tambem "
            "https://br.linkedin.com/in/joao-souza."
        ),
        cleaned_html=None,
        html=None,
        links={
            "external": [
                {"href": "https://www.linkedin.com/in/maria-rh"},
                {"href": "https://www.linkedin.com/company/nubank"},
            ]
        },
    )

    results = extract_profiles_from_results([fake_result, None])

    urls = [result.url for result in results]
    assert "https://www.linkedin.com/in/fulana-da-silva" in urls
    assert "https://br.linkedin.com/in/joao-souza" in urls
    assert "https://www.linkedin.com/in/maria-rh" in urls
    assert all(result.source_type == "search_crawl4ai" for result in results)
    # Company page should NOT be picked up.
    assert all("/company/" not in result.url for result in results)


def test_extract_profiles_deduplicates_repeated_urls():
    text = (
        "https://www.linkedin.com/in/duplicada  e novamente "
        "https://www.linkedin.com/in/duplicada"
    )
    fake_result = SimpleNamespace(
        url="https://acme.com/team",
        success=True,
        metadata={"title": "Team"},
        markdown=text,
        cleaned_html=None,
        html=None,
        links={},
    )

    results = extract_profiles_from_results([fake_result])

    assert len(results) == 1


def test_crawl4ai_engine_search_uses_injected_factory():
    captured_urls: list[str] = []

    class FakeCrawler:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def arun(self, url: str):
            captured_urls.append(url)
            return SimpleNamespace(
                url=url,
                success=True,
                metadata={"title": f"Page {url}"},
                markdown=(
                    "Equipe: https://www.linkedin.com/in/fulana"
                    if url.endswith("/team")
                    else "Sobre a empresa, sem perfis."
                ),
                cleaned_html=None,
                html=None,
                links={},
            )

    engine = Crawl4aiSearchEngine(
        max_paths_per_site=2,
        team_paths=("/", "/team"),
        crawler_factory=lambda: FakeCrawler(),
    )

    results = engine.search('"Nubank" "marketing" "nubank.com.br"', max_results=5)

    assert captured_urls == [
        "https://nubank.com.br/",
        "https://nubank.com.br/team",
    ]
    assert len(results) == 1
    assert results[0].url == "https://www.linkedin.com/in/fulana"
    assert results[0].source_type == "search_crawl4ai"


def test_crawl4ai_engine_returns_empty_when_no_domain_in_query():
    def factory():  # pragma: no cover - should never be called
        raise AssertionError("crawler factory should not be invoked")

    engine = Crawl4aiSearchEngine(crawler_factory=factory)

    assert engine.search('"Acme" "marketing"', max_results=10) == []
