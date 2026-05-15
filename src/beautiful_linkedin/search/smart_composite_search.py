"""Smart composite search — query all engines per call, dedupe, attribute to first finder.

Behavior:
1. **Always queries every engine on every call** (in parallel by default), so users
   see leads from each configured engine instead of only from whichever one
   happens to return enough results first.
2. **Merges results in priority order**: when two engines return the same URL,
   credit goes to whichever engine appears first in the priority order
   (typically the highest-quality / API-backed engine).
3. **Tracks per-engine health** so that engines failing repeatedly are
   deprioritized but never silently skipped while they still respond.
"""

from __future__ import annotations

import logging
import random
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from beautiful_linkedin.models import SearchResult
from beautiful_linkedin.processing.normalizer import normalize_url
from beautiful_linkedin.search.search_engine import SearchEngine

logger = logging.getLogger(__name__)


class SmartCompositeSearchEngine(SearchEngine):
    """Run every engine on every query, merge results respecting priority order."""

    def __init__(self, engines: list[SearchEngine], parallel: bool = True) -> None:
        self.engines = list(engines)
        self.parallel = parallel
        self._call_index = 0
        self._consecutive_failures: dict[int, int] = defaultdict(int)
        self._total_successes: dict[int, int] = defaultdict(int)
        self._total_failures: dict[int, int] = defaultdict(int)
        self._stats_lock = threading.Lock()

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        if not self.engines:
            return []

        ordered_indices = self._engine_priority_order()
        engine_results_map = self._collect_from_all_engines(
            query=query,
            max_results=max_results,
            ordered_indices=ordered_indices,
        )

        results: list[SearchResult] = []
        seen_urls: set[str] = set()

        for engine_idx in ordered_indices:
            for result in engine_results_map.get(engine_idx, []):
                normalized = normalize_url(result.url)
                if not normalized or normalized in seen_urls:
                    continue
                results.append(result)
                seen_urls.add(normalized)

        with self._stats_lock:
            self._call_index += 1
        return results

    def _collect_from_all_engines(
        self,
        query: str,
        max_results: int,
        ordered_indices: list[int],
    ) -> dict[int, list[SearchResult]]:
        if self.parallel and len(ordered_indices) > 1:
            return self._collect_parallel(query, max_results, ordered_indices)
        return self._collect_sequential(query, max_results, ordered_indices)

    def _collect_sequential(
        self,
        query: str,
        max_results: int,
        ordered_indices: list[int],
    ) -> dict[int, list[SearchResult]]:
        results_by_engine: dict[int, list[SearchResult]] = {}
        for engine_idx in ordered_indices:
            results_by_engine[engine_idx] = self._safe_engine_search(
                engine_idx, query, max_results
            )
        return results_by_engine

    def _collect_parallel(
        self,
        query: str,
        max_results: int,
        ordered_indices: list[int],
    ) -> dict[int, list[SearchResult]]:
        results_by_engine: dict[int, list[SearchResult]] = {}
        max_workers = max(1, len(ordered_indices))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {
                executor.submit(
                    self._safe_engine_search, engine_idx, query, max_results
                ): engine_idx
                for engine_idx in ordered_indices
            }
            for future in as_completed(future_to_idx):
                engine_idx = future_to_idx[future]
                try:
                    results_by_engine[engine_idx] = future.result()
                except Exception as exc:
                    logger.warning(
                        "Motor de busca %s lançou exceção inesperada. %s",
                        type(self.engines[engine_idx]).__name__, exc,
                    )
                    results_by_engine[engine_idx] = []
        return results_by_engine

    def _safe_engine_search(
        self,
        engine_idx: int,
        query: str,
        max_results: int,
    ) -> list[SearchResult]:
        engine = self.engines[engine_idx]
        try:
            engine_results = engine.search(query, max_results=max_results)
        except Exception as exc:
            logger.warning(
                "Motor de busca %s falhou e será pulado. %s",
                type(engine).__name__, exc,
            )
            with self._stats_lock:
                self._consecutive_failures[engine_idx] += 1
                self._total_failures[engine_idx] += 1
            return []

        with self._stats_lock:
            if engine_results:
                self._consecutive_failures[engine_idx] = 0
                self._total_successes[engine_idx] += 1
            else:
                self._consecutive_failures[engine_idx] += 1
        return engine_results

    def _engine_priority_order(self) -> list[int]:
        n = len(self.engines)
        if n == 0:
            return []

        with self._stats_lock:
            consecutive = dict(self._consecutive_failures)
            call_index = self._call_index

        healthy: list[int] = []
        degraded: list[int] = []
        dead: list[int] = []

        for i in range(n):
            failures = consecutive.get(i, 0)
            if failures >= 10:
                dead.append(i)
            elif failures >= 3:
                degraded.append(i)
            else:
                healthy.append(i)

        if healthy:
            start = call_index % len(healthy)
            healthy = healthy[start:] + healthy[:start]

        random.shuffle(degraded)

        return healthy + degraded + dead
