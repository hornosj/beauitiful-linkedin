from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from beautiful_linkedin.models import CompanyInput, Lead, SearchResult
from beautiful_linkedin.processing.deduplicator import deduplicate_leads
from beautiful_linkedin.processing.lead_extractor import extract_leads_from_search_results
from beautiful_linkedin.search.query_builder import build_balanced_queries_for_company
from beautiful_linkedin.search.search_engine import SearchEngine
from beautiful_linkedin.providers.lead_provider import LeadProvider

logger = logging.getLogger(__name__)


class PublicSearchLeadProvider(LeadProvider):
    name = "web_search"

    def __init__(
        self,
        search_engine: SearchEngine,
        parallelism: int = 3,
        query_limit: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.search_engine = search_engine
        self.parallelism = max(1, parallelism)
        self.query_limit = query_limit
        self.sleep = sleep

    def find_leads(
        self,
        company: CompanyInput,
        max_results: int,
        include_uncertain: bool,
        search_depth: str = "standard",
        offset: int = 0,
    ) -> list[Lead]:
        diagnostic = self._new_diagnostic(company)
        queries = build_balanced_queries_for_company(company, depth=search_depth)
        if offset:
            queries = queries[offset:]
        if self.query_limit is not None:
            queries = queries[: self.query_limit]

        logger.info(
            "PublicSearch: %d queries geradas para '%s' (depth=%s, limit=%s)",
            len(queries), company.company_name, search_depth,
            self.query_limit,
        )
        diagnostic.notes.append(f"queries geradas: {len(queries)} (depth={search_depth})")

        raw_leads: list[Lead] = []
        consecutive_empty = 0
        max_consecutive_empty = 8  # Stop if too many queries return nothing
        queries_with_results = 0
        queries_empty = 0
        last_query_error: str | None = None

        # SERP-only mode: run SEQUENTIALLY to avoid getting rate-limited
        # The search engines already have their own internal delays
        for i, query in enumerate(queries):
            results, query_error = self._safe_search(query, max_results)
            if query_error:
                last_query_error = query_error

            if results:
                queries_with_results += 1
                consecutive_empty = 0
                leads = extract_leads_from_search_results(
                    company=company,
                    results=results,
                    include_uncertain=include_uncertain,
                )
                raw_leads.extend(leads)
                logger.info(
                    "Query %d/%d: '%s' → %d resultados, %d leads extraídos",
                    i + 1, len(queries), query[:80], len(results), len(leads),
                )
            else:
                queries_empty += 1
                consecutive_empty += 1
                logger.debug(
                    "Query %d/%d: '%s' → 0 resultados (consecutivos vazios: %d)",
                    i + 1, len(queries), query[:80], consecutive_empty,
                )

            # Check if we hit target
            deduped = deduplicate_leads(raw_leads)
            if len(deduped) >= max_results:
                logger.info(
                    "Meta de %d leads atingida com %d leads deduplicados. Parando.",
                    max_results, len(deduped),
                )
                break

            # Stop if too many consecutive empty results (engine is probably blocked)
            if consecutive_empty >= max_consecutive_empty:
                logger.warning(
                    "Parando busca após %d consultas consecutivas sem resultados.",
                    max_consecutive_empty,
                )
                diagnostic.notes.append(
                    f"abortado após {max_consecutive_empty} queries consecutivas sem resultado"
                    " — engine provavelmente bloqueada"
                )
                break

            # Small jitter between queries to not hammer the engine
            # (engines already have internal delays, this is extra safety)
            if i < len(queries) - 1:
                self.sleep(random.uniform(0.3, 1.0))

        deduped = deduplicate_leads(raw_leads)
        logger.info(
            "PublicSearch concluído: %d leads brutos → %d deduplicados",
            len(raw_leads), len(deduped),
        )
        diagnostic.raw_records = len(raw_leads)
        diagnostic.leads_returned = len(deduped)
        diagnostic.notes.append(
            f"queries com resultado: {queries_with_results}, vazias: {queries_empty}"
        )
        if last_query_error:
            diagnostic.last_error = last_query_error
        return deduped

    def _safe_search(self, query: str, max_results: int) -> tuple[list[SearchResult], str | None]:
        try:
            return self.search_engine.search(query, max_results=max_results), None
        except Exception as exc:  # pragma: no cover - defensive boundary
            logger.warning("Public search query failed and was skipped. %s", exc)
            return [], f"{type(exc).__name__}: {exc}"
