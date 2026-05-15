"""Bing HTML SERP scraper — reliable secondary search engine.

Bing is remarkably tolerant of automated queries compared to Google/Brave/DDG.
It rarely serves CAPTCHAs and returns solid ``site:linkedin.com/in`` results.
It also has good support for ``intitle:`` and other operators.

This engine uses the same session + fingerprint rotation approach as the Google
engine to maintain stealth.
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from beautiful_linkedin.models import SearchResult
from beautiful_linkedin.processing.normalizer import (
    contains_linkedin_profile_url,
    normalize_url,
)
from beautiful_linkedin.search.search_engine import SearchEngine
from beautiful_linkedin.search.user_agents import build_headers, random_profile

logger = logging.getLogger(__name__)

BING_SEARCH_URL = "https://www.bing.com/search"


class BingHtmlSearchEngine(SearchEngine):
    """Scrape Bing Search HTML results without API keys."""

    def __init__(
        self,
        sleep_range: tuple[float, float] = (2.0, 5.0),
        timeout_seconds: float = 15.0,
        max_pages: int = 3,
        results_per_page: int = 10,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.sleep_range = sleep_range
        self.timeout_seconds = timeout_seconds
        self.max_pages = max_pages
        self.results_per_page = results_per_page
        self.transport = transport
        self.sleep = sleep
        self._profile = random_profile()
        self._client: httpx.Client | None = None
        self._query_count = 0

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        all_results: list[SearchResult] = []
        seen_urls: set[str] = set()

        pages = min(self.max_pages, max(1, (max_results + self.results_per_page - 1) // self.results_per_page))

        for page in range(pages):
            first = page * self.results_per_page + 1  # Bing uses 1-based "first"
            try:
                html = self._fetch_html(query, first=first)
                if not html:
                    break
                page_results = self._parse_html(html)
                new_count = 0
                for r in page_results:
                    nurl = normalize_url(r.url)
                    if nurl not in seen_urls:
                        all_results.append(r)
                        seen_urls.add(nurl)
                        new_count += 1
                if new_count == 0:
                    break
                if len(all_results) >= max_results:
                    break
                # Sleep between pages
                self.sleep(random.uniform(1.5, 3.0))
            except _BingBlockedError as exc:
                logger.warning("Bing bloqueou a busca para '%s'. %s", query, exc)
                break
            except httpx.HTTPError as exc:
                logger.warning("Bing falhou na consulta '%s'. %s", query, exc)
                break
            except Exception as exc:
                logger.warning("Erro inesperado no Bing para '%s'. %s", query, exc)
                break

        self._sleep_between_queries()
        return all_results[:max_results]

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _get_client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed or self._query_count > 20:
            if self._client is not None and not self._client.is_closed:
                self._client.close()
            self._rotate_session()
        return self._client  # type: ignore[return-value]

    def _rotate_session(self) -> None:
        self._profile = random_profile()
        self._query_count = 0
        headers = build_headers(self._profile)
        # Don't use brotli - httpx may not decompress it properly
        headers["Accept-Encoding"] = "gzip, deflate"
        self._client = httpx.Client(
            headers=headers,
            timeout=self.timeout_seconds,
            follow_redirects=True,
            transport=self.transport,
        )
        # Pre-warm with a Bing visit
        try:
            self._client.get("https://www.bing.com/")
            self.sleep(random.uniform(0.3, 0.8))
        except Exception:
            pass

    def _fetch_html(self, query: str, first: int = 1) -> str:
        client = self._get_client()
        self._query_count += 1

        params = {
            "q": query,
            "first": str(first),
            "count": str(self.results_per_page),
            "setlang": "pt-BR",
            "cc": "BR",
        }

        response = client.get(BING_SEARCH_URL, params=params)

        if response.status_code in {403, 429, 503}:
            raise _BingBlockedError(f"HTTP {response.status_code}")

        if _bing_captcha(response.text):
            self._rotate_session()
            raise _BingBlockedError("CAPTCHA detectado no Bing")

        response.raise_for_status()
        return response.text

    # ------------------------------------------------------------------
    # HTML parsing
    # ------------------------------------------------------------------

    def _parse_html(self, html: str) -> list[SearchResult]:
        soup = BeautifulSoup(html, "lxml")
        results: list[SearchResult] = []
        seen: set[str] = set()

        # Bing organic results live in <li class="b_algo">
        for li in soup.select("li.b_algo"):
            result = self._parse_algo_block(li)
            if result:
                nurl = normalize_url(result.url)
                if nurl not in seen:
                    results.append(result)
                    seen.add(nurl)

        # Fallback: any link to linkedin.com/in/
        for link in soup.select('a[href*="linkedin.com/in/"]'):
            url = str(link.get("href") or "")
            if not url or not contains_linkedin_profile_url(url):
                continue
            nurl = normalize_url(url)
            if nurl in seen:
                continue
            title = link.get_text(" ", strip=True)
            if title:
                results.append(
                    SearchResult(
                        title=_clean_title(title),
                        url=url,
                        snippet="",
                        source_type="search_bing_html",
                    )
                )
                seen.add(nurl)

        return results

    def _parse_algo_block(self, li: Tag) -> SearchResult | None:
        link = li.select_one("h2 a[href]")
        if link is None:
            link = li.select_one("a[href]")
        if link is None:
            return None

        url = str(link.get("href") or "")
        if not url or not contains_linkedin_profile_url(url):
            return None

        title = link.get_text(" ", strip=True)
        snippet_node = li.select_one("div.b_caption p, p")
        snippet = snippet_node.get_text(" ", strip=True)[:500] if snippet_node else ""

        return SearchResult(
            title=_clean_title(title),
            url=url,
            snippet=snippet,
            source_type="search_bing_html",
        )

    def _sleep_between_queries(self) -> None:
        low, high = self.sleep_range
        if high <= 0:
            return
        self.sleep(random.uniform(max(0, low), high))


class _BingBlockedError(Exception):
    pass


def _bing_captcha(html: str) -> bool:
    lowered = html.lower()
    return (
        "captcha" in lowered
        or "verify you're a human" in lowered
        or "solve the challenge" in lowered
        or "please solve the challenge" in lowered
    )


def _clean_title(title: str) -> str:
    title = re.sub(r"\s*[-–—|]\s*LinkedIn\s*$", "", title, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", title).strip()
