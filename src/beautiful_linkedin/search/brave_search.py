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


class BraveSearchEngine(SearchEngine):
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def __init__(
        self,
        api_key: str,
        country: str = "br",
        search_lang: str = "pt-br",
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_key = api_key
        self.country = country
        self.search_lang = search_lang
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.sleep = sleep

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self.api_key,
        }
        params = {
            "q": query,
            "count": str(min(max_results, 20)),
            "country": self.country,
            "search_lang": self.search_lang,
        }

        context = f"query={truncate(query, 80)}"
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self.transport,
                headers=headers,
            ) as client:
                response = client.get(self.endpoint, params=params)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            log_http_error(logger, provider="Brave Search", error=exc, context=context)
            return []
        except (httpx.TransportError, ValueError) as exc:
            log_transport_error(
                logger,
                provider="Brave Search",
                error=exc,
                endpoint=self.endpoint,
                context=context,
            )
            return []

        self.sleep(0.2)
        return self._normalize_results(data)

    def _normalize_results(self, data: dict[str, Any]) -> list[SearchResult]:
        results: list[SearchResult] = []
        for item in data.get("web", {}).get("results", []):
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            results.append(
                SearchResult(
                    title=str(item.get("title") or "").strip(),
                    url=url,
                    snippet=str(item.get("description") or "").strip(),
                    source_type="search_brave",
                )
            )
        return results
