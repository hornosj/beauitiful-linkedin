from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from beautiful_linkedin.api_logging import log_http_error, log_transport_error, truncate
from beautiful_linkedin.models import SearchResult
from beautiful_linkedin.search.search_engine import SearchEngine

logger = logging.getLogger(__name__)

SEARXNG_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


class SearxngSearchEngine(SearchEngine):
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 15.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        language: str = "pt-BR",
    ) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.sleep = sleep
        self.language = language

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        query = " ".join((query or "").split())
        if not self.base_url:
            logger.warning("SearxNG ignorado: SEARXNG_BASE_URL não configurada.")
            return []
        if not query:
            logger.warning("SearxNG ignorado: query vazia recebida.")
            return []

        endpoint = f"{self.base_url}/search"
        headers = {"Accept": "application/json", "User-Agent": SEARXNG_USER_AGENT}
        params = {
            "q": query,
            "format": "json",
            "language": self.language,
            "safesearch": "0",
        }
        context = f"query={truncate(query, 80)}"
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self.transport,
                headers=headers,
            ) as client:
                response = client.get(endpoint, params=params)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            log_http_error(logger, provider="SearxNG", error=exc, context=context)
            return []
        except (httpx.TransportError, ValueError) as exc:
            log_transport_error(
                logger,
                provider="SearxNG",
                error=exc,
                endpoint=endpoint,
                context=context,
            )
            return []

        self.sleep(0.1)
        results = self._normalize_results(data, max_results)
        self._log_engine_health(query=query, data=data, returned=len(results))
        return results

    def _normalize_results(
        self, data: dict[str, Any], max_results: int
    ) -> list[SearchResult]:
        # Filter to LinkedIn-related URLs *before* hitting the max_results cap.
        # Engines like mwmbl/marginalia frequently pollute the response with
        # totally unrelated pages (museum catalogs, dictionaries) when the
        # query uses operators like `site:` that they don't honour. Without
        # this filter, those junk URLs eat the cap and starve the lead
        # extractor of real profile URLs.
        results: list[SearchResult] = []
        items = data.get("results") if isinstance(data, dict) else []
        if not isinstance(items, list):
            return results
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url or not _looks_linkedin_related(url):
                continue
            results.append(
                SearchResult(
                    title=str(item.get("title") or "").strip(),
                    url=url,
                    snippet=str(item.get("content") or "").strip(),
                    source_type="search_searxng",
                )
            )
            if len(results) >= max_results:
                break
        return results

    def _log_engine_health(
        self, *, query: str, data: dict[str, Any], returned: int
    ) -> None:
        # Each entry is typically [engine_name, reason] (e.g. "Suspended: CAPTCHA").
        # Surface this even when results were returned, so silent degradation is
        # visible when investigating "0 leads" reports.
        unresponsive = data.get("unresponsive_engines") if isinstance(data, dict) else None
        if not isinstance(unresponsive, list):
            unresponsive = []

        formatted = []
        for entry in unresponsive:
            if isinstance(entry, (list, tuple)) and entry:
                name = str(entry[0])
                reason = str(entry[1]) if len(entry) > 1 else ""
                formatted.append(f"{name}: {reason}" if reason else name)
            elif isinstance(entry, str):
                formatted.append(entry)

        truncated_query = truncate(query, 80)

        if returned == 0:
            if formatted:
                logger.warning(
                    "SearxNG: 0 resultados para '%s'. Engines mortas: %s",
                    truncated_query,
                    ", ".join(formatted),
                )
            else:
                logger.info(
                    "SearxNG: 0 resultados para '%s' (engines responderam, sem matches).",
                    truncated_query,
                )
        elif formatted:
            logger.info(
                "SearxNG: %d resultados para '%s' (degradado — engines fora: %s)",
                returned,
                truncated_query,
                ", ".join(formatted),
            )


# URL hosts that the lead extractor downstream knows how to read. Anything
# else from SearxNG (mwmbl/marginalia "general web" hits) is dropped before
# eating slots in `max_results`.
_LINKEDIN_HOST_HINTS = ("linkedin.com", "linkedin.cn")


def _looks_linkedin_related(url: str) -> bool:
    lowered = url.lower()
    return any(hint in lowered for hint in _LINKEDIN_HOST_HINTS)
