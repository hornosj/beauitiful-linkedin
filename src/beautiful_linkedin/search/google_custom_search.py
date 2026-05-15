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


class GoogleCustomSearchEngine(SearchEngine):
    endpoint = "https://www.googleapis.com/customsearch/v1"

    def __init__(
        self,
        api_key: str,
        search_engine_id: str,
        gl: str = "br",
        lr: str = "lang_pt",
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_key = api_key
        self.search_engine_id = search_engine_id
        self.gl = gl
        self.lr = lr
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.sleep = sleep

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        params = {
            "key": self.api_key,
            "cx": self.search_engine_id,
            "q": query,
            "num": str(min(max_results, 10)),
            "gl": self.gl,
            "lr": self.lr,
        }

        context = f"query={truncate(query, 80)}"
        try:
            with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
                response = client.get(self.endpoint, params=params)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            log_http_error(
                logger, provider="Google Custom Search", error=exc, context=context
            )
            return []
        except (httpx.TransportError, ValueError) as exc:
            log_transport_error(
                logger,
                provider="Google Custom Search",
                error=exc,
                endpoint=self.endpoint,
                context=context,
            )
            return []

        self.sleep(0.2)
        return self._normalize_results(data)

    def _normalize_results(self, data: dict[str, Any]) -> list[SearchResult]:
        results: list[SearchResult] = []
        for item in data.get("items", []):
            url = str(item.get("link") or "").strip()
            if not url:
                continue
            results.append(
                SearchResult(
                    title=str(item.get("title") or "").strip(),
                    url=url,
                    snippet=str(item.get("snippet") or "").strip(),
                    source_type="search_google_cse",
                )
            )
        return results
