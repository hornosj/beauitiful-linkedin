"""Experimental 'pull more leads via free search engines' for a saved table.

Pure orchestration: takes a dict of engine_name -> SearchEngine, plus
metadata about the open saved table, and returns the *new* leads to
append. Network calls live inside the injected engines, never here.

This is intentionally separate from ``runner.run_prospecting`` because:
- It's opt-in, opportunistic, marked experimental in the UI.
- It must dedupe against an already-saved table (different invariant).
- It must never call paid providers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from beautiful_linkedin.models import CompanyInput, Lead, SearchResult
from beautiful_linkedin.processing.deduplicator import lead_dedupe_key
from beautiful_linkedin.processing.lead_extractor import (
    extract_leads_from_search_results,
)
from beautiful_linkedin.search.search_engine import SearchEngine

EXPERIMENTAL_SOURCE_TYPE = "experimental_search"
EXPERIMENTAL_NOTE = (
    "Lead encontrado por buscador experimental e adicionado como "
    "complemento da tabela salva."
)

_PER_QUERY_LIMIT = 20


@dataclass
class ExperimentalSearchSummary:
    new_leads: list[Lead] = field(default_factory=list)
    duplicates_skipped: int = 0
    candidates_total: int = 0
    engines_used: list[str] = field(default_factory=list)
    note: str | None = None


def run_experimental_search(
    *,
    engines: dict[str, SearchEngine],
    company_name: str,
    company_domain: str | None,
    keywords: list[str],
    existing_leads: list[Lead],
    linkedin_url: str | None = None,
) -> ExperimentalSearchSummary:
    summary = ExperimentalSearchSummary()
    if not engines:
        summary.note = (
            "Nenhum buscador gratuito configurado. Suba o SearxNG local ou "
            "configure uma chave gratuita para usar essa ação."
        )
        return summary

    queries = _build_queries(
        company_name=company_name,
        company_domain=company_domain,
        keywords=keywords,
    )
    if not queries:
        summary.note = "Sem metadados suficientes na tabela para montar buscas."
        return summary

    seen_existing_keys = _existing_keys(existing_leads)
    seen_new_keys: set[tuple[str, str]] = set()
    raw_results: list[SearchResult] = []
    for name, engine in engines.items():
        summary.engines_used.append(name)
        for query in queries:
            try:
                raw_results.extend(engine.search(query, _PER_QUERY_LIMIT))
            except Exception:
                # Experimental search is best-effort: a failing engine should
                # not break the whole action.
                continue

    company = CompanyInput(
        company_name=company_name,
        company_domain=company_domain,
        linkedin_url=linkedin_url,
        titles=keywords or [company_name],
    )
    extracted = extract_leads_from_search_results(
        company, raw_results, include_uncertain=False
    )
    unique_candidates: dict[tuple[str, str], Lead] = {}
    for lead in extracted:
        key = lead_dedupe_key(lead)
        if key is None:
            continue
        # Keep the highest-confidence sighting per candidate so we don't
        # report the same person multiple times due to identical hits from
        # different queries.
        current = unique_candidates.get(key)
        if current is None or lead.confidence_score > current.confidence_score:
            unique_candidates[key] = lead
    summary.candidates_total = len(unique_candidates)

    for key, lead in unique_candidates.items():
        if key in seen_existing_keys:
            summary.duplicates_skipped += 1
            continue
        seen_new_keys.add(key)
        summary.new_leads.append(
            lead.model_copy(
                update={
                    "source_type": EXPERIMENTAL_SOURCE_TYPE,
                    "consultation_note": EXPERIMENTAL_NOTE,
                }
            )
        )
    return summary


def _build_queries(
    *, company_name: str, company_domain: str | None, keywords: list[str]
) -> list[str]:
    company_terms: list[str] = [f'"{company_name}"']
    if company_domain:
        company_terms.append(f'"{company_domain}"')

    title_terms = [kw for kw in keywords if kw.strip()] or [company_name]

    queries: list[str] = []
    for company_term in company_terms:
        for title in title_terms:
            queries.append(
                f'site:linkedin.com/in {company_term} "{title}"'
            )
    return _dedupe_preserving_order(queries)


def _existing_keys(leads: list[Lead]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for lead in leads:
        key = lead_dedupe_key(lead)
        if key is not None:
            keys.add(key)
    return keys


def _dedupe_preserving_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
