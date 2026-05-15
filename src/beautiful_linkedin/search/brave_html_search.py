from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable

import httpx
from bs4 import BeautifulSoup

from beautiful_linkedin.models import SearchResult
from beautiful_linkedin.processing.normalizer import contains_linkedin_profile_url, normalize_url
from beautiful_linkedin.search.search_engine import SearchEngine

logger = logging.getLogger(__name__)

BRAVE_SEARCH_URL = "https://search.brave.com/search"
PUBLIC_SEARCH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)


class BraveHtmlSearchEngine(SearchEngine):
    """Scrape Brave Search public HTML without the Brave Search API."""

    def __init__(
        self,
        sleep_range: tuple[float, float] = (0.8, 1.8),
        timeout_seconds: float = 15.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.sleep_range = sleep_range
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.sleep = sleep

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        try:
            html = self._fetch_html(query)
            results = self._parse_html(html, max_results=max_results)
            self._sleep_between_queries()
            return results
        except SearchBlockedError as exc:
            logger.warning("Brave HTML bloqueou a automacao para '%s'. %s", query, exc)
            return []
        except httpx.HTTPError as exc:
            logger.warning("Brave HTML falhou na consulta '%s'. %s", query, exc)
            return []
        except Exception as exc:  # pragma: no cover - defensive boundary
            logger.warning("Erro inesperado no Brave HTML para '%s'. %s", query, exc)
            return []

    def _fetch_html(self, query: str) -> str:
        headers = {
            "User-Agent": PUBLIC_SEARCH_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        with httpx.Client(
            headers=headers,
            timeout=self.timeout_seconds,
            follow_redirects=True,
            transport=self.transport,
        ) as client:
            response = client.get(BRAVE_SEARCH_URL, params={"q": query, "source": "web"})
            if response.status_code in {403, 429} or _looks_like_challenge(response.text):
                raise SearchBlockedError(f"HTTP {response.status_code}; desafio anti-bot detectado")
            response.raise_for_status()
            return response.text

    def _parse_html(self, html: str, max_results: int) -> list[SearchResult]:
        soup = BeautifulSoup(html, "lxml")
        results: list[SearchResult] = []
        seen_urls: set[str] = set()

        for link in soup.select('a[href*="linkedin.com/in"]'):
            url = normalize_url(str(link.get("href") or ""))
            if not contains_linkedin_profile_url(url) or url in seen_urls:
                continue

            title = _clean_result_title(link.get_text(" ", strip=True), url)
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=_snippet_for_link(link) or title,
                    source_type="search_brave_html",
                )
            )
            seen_urls.add(url)
            if len(results) >= max_results:
                break

        return results

    def _sleep_between_queries(self) -> None:
        low, high = self.sleep_range
        if high <= 0:
            return
        self.sleep(random.uniform(max(0, low), high))


class SearchBlockedError(Exception):
    pass


def _looks_like_challenge(html: str) -> bool:
    lowered = html.lower()
    return (
        "pow captcha" in lowered
        or "flagged as being suspicious" in lowered
        or "schedule a captcha" in lowered
    )


def _snippet_for_link(link) -> str:
    parent = link.find_parent(["article", "li", "div"])
    if parent is None:
        return ""
    return parent.get_text(" ", strip=True)[:500]


def _clean_result_title(value: str, url: str) -> str:
    title = re.sub(r"\s+", " ", value).strip()
    parsed_slug = normalize_url(url).rstrip("/").split("/")[-1]
    breadcrumb_pattern = (
        r"^LinkedIn\s+"
        r"(?:[a-z]{2}\.)?linkedin\.com\s+"
        r"(?:›|>)\s+in\s+"
        r"(?:›|>)\s+"
        + re.escape(parsed_slug)
        + r"\s+"
    )
    title = re.sub(breadcrumb_pattern, "", title, flags=re.IGNORECASE).strip()
    return title or url
