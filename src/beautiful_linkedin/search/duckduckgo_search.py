from __future__ import annotations

import logging
import random
import time
import warnings
from contextlib import redirect_stderr
from collections.abc import Callable
from io import StringIO
from typing import Any

from beautiful_linkedin.models import SearchResult
from beautiful_linkedin.search.search_engine import SearchEngine

logger = logging.getLogger(__name__)

warnings.filterwarnings(
    "ignore",
    message=r".*duckduckgo_search.*renamed.*",
    category=RuntimeWarning,
)

try:
    from duckduckgo_search import DDGS
except ImportError:  # pragma: no cover - dependency is installed by project metadata
    DDGS = None  # type: ignore[assignment]

try:
    import duckduckgo_search.exceptions as ddg_exceptions

    DuckDuckGoSearchException = getattr(
        ddg_exceptions, "DuckDuckGoSearchException", Exception
    )
    RatelimitException = getattr(
        ddg_exceptions,
        "RatelimitException",
        getattr(ddg_exceptions, "RateLimitException", Exception),
    )
except Exception:  # pragma: no cover - version compatibility fallback
    DuckDuckGoSearchException = Exception
    RatelimitException = Exception


class DuckDuckGoSearchEngine(SearchEngine):
    def __init__(
        self,
        sleep_range: tuple[float, float] = (2.0, 5.0),
        rate_limit_sleep_seconds: float = 15.0,
        region: str = "br-pt",
        backend: str = "lite",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.sleep_range = sleep_range
        self.rate_limit_sleep_seconds = rate_limit_sleep_seconds
        self.region = region
        self.backend = backend
        self.sleep = sleep

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        if DDGS is None:
            logger.warning("Dependência do DuckDuckGo Search não está disponível.")
            return []

        for attempt in range(2):
            try:
                raw_results = self._run_query(query, max_results)
                self._sleep_between_queries()
                return raw_results
            except RatelimitException as exc:  # type: ignore[misc]
                if attempt == 0:
                    logger.warning(
                        "DuckDuckGo atingiu rate limit na consulta '%s'. Aguardando antes de tentar novamente.",
                        query,
                    )
                    self.sleep(self.rate_limit_sleep_seconds)
                    continue
                logger.warning(
                    "DuckDuckGo repetiu rate limit na consulta '%s'. Consulta ignorada. %s",
                    query,
                    exc,
                )
                return []
            except TimeoutError as exc:
                logger.warning("DuckDuckGo deu timeout na consulta '%s'. Consulta ignorada. %s", query, exc)
                return []
            except DuckDuckGoSearchException as exc:  # type: ignore[misc]
                logger.warning(
                    "Busca DuckDuckGo falhou na consulta '%s'. Consulta ignorada. %s",
                    query,
                    exc,
                )
                return []
            except Exception as exc:  # pragma: no cover - controlled fallback
                logger.warning(
                    "Erro inesperado na consulta '%s'. Consulta ignorada. %s",
                    query,
                    exc,
                )
                return []

        return []

    def _run_query(self, query: str, max_results: int) -> list[SearchResult]:
        results: list[SearchResult] = []

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            with redirect_stderr(StringIO()):
                ddgs_context = DDGS()  # type: ignore[operator]

        with ddgs_context as ddgs:
            try:
                raw_items = ddgs.text(
                    query,
                    region=self.region,
                    backend=self.backend,
                    max_results=max_results,
                )
            except TypeError:
                raw_items = ddgs.text(query, max_results=max_results)

            for item in raw_items:
                result = self._normalize_result(item)
                if result:
                    results.append(result)

        return results

    def _normalize_result(self, item: dict[str, Any]) -> SearchResult | None:
        url = str(item.get("href") or item.get("url") or "").strip()
        if not url:
            return None

        return SearchResult(
            title=str(item.get("title") or "").strip(),
            url=url,
            snippet=str(item.get("body") or item.get("snippet") or "").strip(),
            source_type="search_duckduckgo",
        )

    def _sleep_between_queries(self) -> None:
        low, high = self.sleep_range
        self.sleep(random.uniform(low, high))
