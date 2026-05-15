from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from beautiful_linkedin.models import SearchResult
from beautiful_linkedin.search.search_engine import SearchEngine

logger = logging.getLogger(__name__)

DUCKDUCKGO_HTML_URL = "https://html.duckduckgo.com/html/"
PUBLIC_SEARCH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)


class DuckDuckGoHtmlSearchEngine(SearchEngine):
    """Scrape DuckDuckGo's public HTML search page without API keys or cookies."""

    def __init__(
        self,
        sleep_range: tuple[float, float] = (0.8, 1.8),
        timeout_seconds: float = 15.0,
        region: str = "br-pt",
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.sleep_range = sleep_range
        self.timeout_seconds = timeout_seconds
        self.region = region
        self.transport = transport
        self.sleep = sleep

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        try:
            html = self._fetch_html(query)
            results = self._parse_html(html, max_results=max_results)
            self._sleep_between_queries()
            return results
        except SearchBlockedError as exc:
            logger.warning("DuckDuckGo HTML bloqueou a automacao para '%s'. %s", query, exc)
            return []
        except httpx.HTTPError as exc:
            logger.warning("DuckDuckGo HTML falhou na consulta '%s'. %s", query, exc)
            return []
        except Exception as exc:  # pragma: no cover - defensive boundary
            logger.warning("Erro inesperado no DuckDuckGo HTML para '%s'. %s", query, exc)
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
            response = client.get(
                DUCKDUCKGO_HTML_URL,
                params={"q": query, "kl": self.region},
            )
            if response.status_code in {202, 403, 429} or _looks_like_challenge(response.text):
                raise SearchBlockedError(f"HTTP {response.status_code}; desafio anti-bot detectado")
            response.raise_for_status()
            return response.text

    def _parse_html(self, html: str, max_results: int) -> list[SearchResult]:
        soup = BeautifulSoup(html, "lxml")
        results: list[SearchResult] = []

        for node in soup.select(".result"):
            link = node.select_one("a.result__a")
            if link is None:
                continue

            url = _decode_duckduckgo_url(str(link.get("href") or ""))
            if not url:
                continue

            snippet_node = node.select_one(".result__snippet")
            results.append(
                SearchResult(
                    title=link.get_text(" ", strip=True),
                    url=url,
                    snippet=snippet_node.get_text(" ", strip=True) if snippet_node else "",
                    source_type="search_duckduckgo_html",
                )
            )
            if len(results) >= max_results:
                break

        return results

    def _sleep_between_queries(self) -> None:
        low, high = self.sleep_range
        if high <= 0:
            return
        self.sleep(random.uniform(max(0, low), high))


def _decode_duckduckgo_url(value: str) -> str:
    if not value:
        return ""

    if value.startswith("//"):
        value = f"https:{value}"

    parsed = urlparse(value)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path == "/l/":
        uddg = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(uddg).strip()

    return value.strip()


class SearchBlockedError(Exception):
    pass


def _looks_like_challenge(html: str) -> bool:
    lowered = html.lower()
    return (
        "bots use duckduckgo too" in lowered
        or "anomaly.js" in lowered
        or "complete the following challenge" in lowered
    )
