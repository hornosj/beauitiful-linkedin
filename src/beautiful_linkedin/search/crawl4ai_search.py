"""Crawler-based search engine powered by crawl4ai.

This engine does not query a SERP. It receives a query string (typically
produced by ``query_builder``), tries to identify a candidate company
website from quoted domain hints or LinkedIn company slugs, crawls a few
common "team/about/people" paths via crawl4ai's headless browser, and
extracts ``linkedin.com/in/<handle>`` URLs from the rendered content.

crawl4ai is an optional dependency. The engine imports it lazily so the
rest of the project can run without playwright/chromium installed.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse, urlunparse

from beautiful_linkedin.models import SearchResult
from beautiful_linkedin.processing.normalizer import (
    contains_linkedin_profile_url,
    normalize_url,
)
from beautiful_linkedin.search.search_engine import SearchEngine

logger = logging.getLogger(__name__)


DEFAULT_TEAM_PATHS: tuple[str, ...] = (
    "/",
    "/about",
    "/about-us",
    "/team",
    "/our-team",
    "/people",
    "/leadership",
    "/management",
    "/sobre",
    "/sobre-nos",
    "/quem-somos",
    "/equipe",
    "/lideranca",
)

# Hosts we never treat as candidate company sites.
_BLOCKED_HOSTS: frozenset[str] = frozenset(
    {
        "linkedin.com",
        "www.linkedin.com",
        "br.linkedin.com",
        "google.com",
        "www.google.com",
        "bing.com",
        "www.bing.com",
        "duckduckgo.com",
        "html.duckduckgo.com",
        "facebook.com",
        "twitter.com",
        "x.com",
        "youtube.com",
        "instagram.com",
    }
)

# Domains found inside quoted query terms. Accepts patterns like nubank.com,
# nubank.com.br, foo-bar.io. Excludes obvious paths/punctuation.
_DOMAIN_RE = re.compile(
    r"\b((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,})\b",
    re.IGNORECASE,
)

# linkedin.com/in/<handle> with optional country subdomain.
_LINKEDIN_PROFILE_RE = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9\-_%.]+",
    re.IGNORECASE,
)


CrawlerFactory = Callable[[], Any]


class Crawl4aiSearchEngine(SearchEngine):
    """Search engine that crawls likely employee pages and harvests LinkedIn URLs.

    The engine is intentionally conservative: if it cannot infer a target
    domain from the query, it returns an empty list rather than guessing
    aggressively. Pair it with another engine (e.g. via the composite) so
    the caller still has a fallback.
    """

    def __init__(
        self,
        max_paths_per_site: int = 6,
        timeout_seconds: float = 30.0,
        headless: bool = True,
        team_paths: tuple[str, ...] = DEFAULT_TEAM_PATHS,
        crawler_factory: CrawlerFactory | None = None,
    ) -> None:
        self.max_paths_per_site = max(1, max_paths_per_site)
        self.timeout_seconds = timeout_seconds
        self.headless = headless
        self.team_paths = team_paths
        self._crawler_factory = crawler_factory

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        if max_results <= 0:
            return []
        candidates = build_candidate_urls(
            query, self.team_paths, self.max_paths_per_site
        )
        if not candidates:
            logger.info(
                "Crawl4ai: nenhum domínio candidato extraído da query '%s'.", query
            )
            return []

        factory = self._resolve_factory()
        if factory is None:
            return []

        try:
            crawl_results = _run_async(self._crawl_all(factory, candidates))
        except Exception as exc:  # pragma: no cover - defensive boundary
            logger.warning("Crawl4ai falhou ao executar crawl. %s", exc)
            return []

        profiles = extract_profiles_from_results(crawl_results)
        return profiles[:max_results]

    def _resolve_factory(self) -> CrawlerFactory | None:
        if self._crawler_factory is not None:
            return self._crawler_factory
        try:
            from crawl4ai import AsyncWebCrawler, BrowserConfig  # type: ignore[import-not-found]
        except Exception as exc:
            logger.warning(
                "Crawl4ai indisponível: instale com `pip install beautiful-linkedin[crawl4ai]` "
                "ou diretamente `pip install crawl4ai`. Detalhe: %s",
                exc,
            )
            return None

        headless = self.headless

        def _factory() -> Any:
            return AsyncWebCrawler(config=BrowserConfig(headless=headless))

        return _factory

    async def _crawl_all(
        self,
        factory: CrawlerFactory,
        urls: list[str],
    ) -> list[Any]:
        crawler = factory()
        async with crawler:
            tasks = [self._crawl_one(crawler, url) for url in urls]
            return await asyncio.gather(*tasks, return_exceptions=True)

    async def _crawl_one(self, crawler: Any, url: str) -> Any:
        try:
            return await asyncio.wait_for(
                crawler.arun(url=url), timeout=self.timeout_seconds
            )
        except asyncio.TimeoutError:
            logger.info("Crawl4ai: timeout ao crawlear %s", url)
            return None
        except Exception as exc:
            logger.info("Crawl4ai: falha ao crawlear %s. %s", url, exc)
            return None


def build_candidate_urls(
    query: str,
    team_paths: tuple[str, ...],
    max_paths_per_site: int,
) -> list[str]:
    """Derive candidate page URLs from a search query string.

    Strategy: find every plausible domain inside the query, filter out
    blocked hosts (LinkedIn, search engines, social), then expand each
    domain across the configured team-page paths.
    """
    domains = _extract_domains(query)
    if not domains:
        return []

    paths = list(team_paths)[:max_paths_per_site]
    candidates: list[str] = []
    seen: set[str] = set()
    for domain in domains:
        for path in paths:
            url = f"https://{domain}{path}"
            normalized = normalize_url(url)
            if normalized in seen:
                continue
            seen.add(normalized)
            candidates.append(url)
    return candidates


def extract_profiles_from_results(crawl_results: list[Any]) -> list[SearchResult]:
    """Extract unique LinkedIn /in/ profile URLs from crawl results."""
    seen: set[str] = set()
    out: list[SearchResult] = []
    for result in crawl_results:
        if result is None or isinstance(result, BaseException):
            continue
        if not getattr(result, "success", True):
            continue
        source_url = getattr(result, "url", "") or ""
        page_title = _extract_title(result)
        text = _extract_text(result)
        for match in _LINKEDIN_PROFILE_RE.finditer(text):
            raw = match.group(0).strip().rstrip(".,);]")
            if not contains_linkedin_profile_url(raw):
                continue
            normalized = normalize_url(raw)
            if normalized in seen:
                continue
            seen.add(normalized)
            snippet = _snippet_around(text, match.start(), match.end())
            if source_url:
                snippet = f"{snippet} [{source_url}]" if snippet else source_url
            out.append(
                SearchResult(
                    title=page_title or raw,
                    url=raw,
                    snippet=snippet,
                    source_type="search_crawl4ai",
                )
            )
        # Also scan the structured links collection when available.
        for url in _iter_link_urls(result):
            if not contains_linkedin_profile_url(url):
                continue
            normalized = normalize_url(url)
            if normalized in seen:
                continue
            seen.add(normalized)
            out.append(
                SearchResult(
                    title=page_title or url,
                    url=url,
                    snippet=source_url,
                    source_type="search_crawl4ai",
                )
            )
    return out


def _extract_domains(query: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in _DOMAIN_RE.finditer(query):
        raw = match.group(1).lower().strip(".")
        if not raw or "." not in raw:
            continue
        host = raw[4:] if raw.startswith("www.") else raw
        if host in _BLOCKED_HOSTS or _has_blocked_root(host):
            continue
        if host in seen:
            continue
        seen.add(host)
        found.append(host)
    return found


def _has_blocked_root(host: str) -> bool:
    parts = host.split(".")
    if len(parts) < 2:
        return False
    root = ".".join(parts[-2:])
    return root in _BLOCKED_HOSTS


def _extract_title(result: Any) -> str:
    metadata = getattr(result, "metadata", None) or {}
    if isinstance(metadata, dict):
        title = metadata.get("title") or metadata.get("og:title") or ""
        if isinstance(title, str) and title.strip():
            return title.strip()
    url = getattr(result, "url", "") or ""
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _extract_text(result: Any) -> str:
    markdown = getattr(result, "markdown", None)
    if markdown is not None:
        try:
            text = str(markdown)
        except Exception:
            text = ""
        if text:
            return text
    cleaned = getattr(result, "cleaned_html", None) or getattr(result, "html", None)
    return cleaned or ""


def _iter_link_urls(result: Any):
    links = getattr(result, "links", None)
    if not isinstance(links, dict):
        return
    for bucket in ("internal", "external"):
        for entry in links.get(bucket, []) or []:
            if isinstance(entry, dict):
                href = entry.get("href") or entry.get("url")
            else:
                href = entry
            if isinstance(href, str) and href:
                yield href


def _snippet_around(text: str, start: int, end: int, radius: int = 120) -> str:
    if not text:
        return ""
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    snippet = text[lo:hi]
    snippet = re.sub(r"\s+", " ", snippet).strip()
    return snippet[:300]


def _run_async(coro: Awaitable[Any]) -> Any:
    """Run an async coroutine from a sync caller, even inside a worker thread."""
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
