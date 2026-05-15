from __future__ import annotations

import logging

from beautiful_linkedin.models import SearchResult
from beautiful_linkedin.processing.normalizer import normalize_url
from beautiful_linkedin.search.search_engine import SearchEngine

logger = logging.getLogger(__name__)


class CompositeSearchEngine(SearchEngine):
    def __init__(self, engines: list[SearchEngine]) -> None:
        self.engines = engines

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        results: list[SearchResult] = []
        seen_urls: set[str] = set()

        for engine in self.engines:
            try:
                engine_results = engine.search(query, max_results=max_results)
            except Exception as exc:  # pragma: no cover - defensive boundary
                logger.warning("Search engine failed and was skipped. %s", exc)
                continue

            for result in engine_results:
                normalized_url = normalize_url(result.url)
                if normalized_url and normalized_url not in seen_urls:
                    results.append(result)
                    seen_urls.add(normalized_url)

        return results[:max_results]
