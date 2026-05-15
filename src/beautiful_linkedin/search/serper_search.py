from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from beautiful_linkedin.api_logging import log_http_error, log_transport_error, truncate
from beautiful_linkedin.models import SearchResult
from beautiful_linkedin.search.search_engine import SearchEngine

logger = logging.getLogger(__name__)


class SerperSearchEngine(SearchEngine):
    endpoint = "https://google.serper.dev/search"

    def __init__(
        self,
        api_key: str,
        gl: str = "br",
        hl: str = "pt-br",
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_key = api_key
        self.gl = gl
        self.hl = hl
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.sleep = sleep

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        query = " ".join((query or "").split())
        if not query:
            logger.warning("Serper ignorado: query vazia recebida.")
            return []

        headers = {"X-API-KEY": self.api_key, "Content-Type": "application/json"}
        payload = {"q": query, "num": min(max_results, 100), "gl": self.gl, "hl": self.hl}

        context = f"query={truncate(query, 80)}"
        try:
            data = self._post_search(headers=headers, payload=payload)
        except httpx.HTTPStatusError as exc:
            fallback_query = _simplify_query_for_serper(query)
            if exc.response.status_code == 400 and fallback_query != query:
                logger.warning(
                    "Serper rejeitou a query avançada. Tentando query simplificada: %s",
                    truncate(fallback_query, 100),
                )
                try:
                    fallback_payload = {**payload, "q": fallback_query}
                    data = self._post_search(headers=headers, payload=fallback_payload)
                except httpx.HTTPStatusError as fallback_exc:
                    log_http_error(
                        logger,
                        provider="Serper",
                        error=fallback_exc,
                        context=f"query={truncate(fallback_query, 80)}",
                    )
                    return []
                except (httpx.TransportError, ValueError) as fallback_exc:
                    log_transport_error(
                        logger,
                        provider="Serper",
                        error=fallback_exc,
                        endpoint=self.endpoint,
                        context=f"query={truncate(fallback_query, 80)}",
                    )
                    return []
            else:
                log_http_error(logger, provider="Serper", error=exc, context=context)
                return []
        except (httpx.TransportError, ValueError) as exc:
            log_transport_error(
                logger,
                provider="Serper",
                error=exc,
                endpoint=self.endpoint,
                context=context,
            )
            return []

        self.sleep(0.2)
        return self._normalize_results(data)

    def _post_search(self, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        with httpx.Client(
            timeout=self.timeout_seconds,
            transport=self.transport,
            headers=headers,
        ) as client:
            response = client.post(self.endpoint, json=payload)
            response.raise_for_status()
            return response.json()

    def _normalize_results(self, data: dict[str, Any]) -> list[SearchResult]:
        results: list[SearchResult] = []
        for item in data.get("organic", []):
            url = str(item.get("link") or "").strip()
            if not url:
                continue
            results.append(
                SearchResult(
                    title=str(item.get("title") or "").strip(),
                    url=url,
                    snippet=str(item.get("snippet") or "").strip(),
                    source_type="search_serper",
                )
            )
        return results


def _simplify_query_for_serper(query: str) -> str:
    """Fallback for Serper 400 responses on advanced operator-heavy queries."""

    simplified = query
    simplified = re.sub(r"\bsite:\S+", " ", simplified, flags=re.IGNORECASE)
    simplified = re.sub(r"\binurl:\S+", " ", simplified, flags=re.IGNORECASE)
    simplified = simplified.replace("(", " ").replace(")", " ")
    simplified = re.sub(r"\bOR\b", " ", simplified, flags=re.IGNORECASE)
    simplified = simplified.replace('"', " ")
    simplified = " ".join(simplified.split())
    if "linkedin.com/in" not in simplified.lower():
        simplified = f"{simplified} linkedin.com/in".strip()
    return simplified
