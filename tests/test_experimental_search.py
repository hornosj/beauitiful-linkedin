"""TDD for the experimental 'puxar mais leads com buscadores' feature.

Pure-logic tests only. The function takes a fake ``SearchEngine`` and a
saved table; it returns the new leads after deduping against the table's
existing rows. No network.
"""

from __future__ import annotations

from beautiful_linkedin.models import Lead, SearchResult
from beautiful_linkedin.search.search_engine import SearchEngine
from beautiful_linkedin.storage.experimental_search import (
    EXPERIMENTAL_SOURCE_TYPE,
    ExperimentalSearchSummary,
    run_experimental_search,
)


class FakeEngine(SearchEngine):
    def __init__(self, results: list[SearchResult]) -> None:
        self.results = results
        self.queries: list[str] = []

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        self.queries.append(query)
        return self.results


def _existing(person: str, linkedin: str, *, confidence: int = 80) -> Lead:
    return Lead(
        company_name="Acme",
        person_name=person,
        title="Head of Marketing",
        linkedin_url=linkedin,
        source_url=linkedin,
        source_type="linkedin_people_search",
        snippet="",
        confidence_score=confidence,
    )


def _search_hit(name: str, slug: str) -> SearchResult:
    return SearchResult(
        title=f"{name} - Head of Marketing - Acme | LinkedIn",
        url=f"https://www.linkedin.com/in/{slug}/",
        snippet="Acme · Head of Marketing",
        source_type="experimental_search",
    )


def test_run_returns_new_leads_after_deduping_against_existing() -> None:
    existing = [_existing("Ana Silva", "https://www.linkedin.com/in/ana-silva/")]
    engine = FakeEngine(
        [
            _search_hit("Ana Silva", "ana-silva"),  # duplicate of existing
            _search_hit("Bruno Costa", "bruno-costa"),  # new
        ]
    )

    summary = run_experimental_search(
        engines={"fake": engine},
        company_name="Acme",
        company_domain="acme.com",
        keywords=["marketing"],
        existing_leads=existing,
    )

    assert isinstance(summary, ExperimentalSearchSummary)
    persons = {lead.person_name for lead in summary.new_leads}
    assert persons == {"Bruno Costa"}
    assert summary.duplicates_skipped == 1
    assert summary.engines_used == ["fake"]
    # Source-typing is part of the contract — the table needs to know that
    # the leads came from the experimental flow, not from the original search.
    for lead in summary.new_leads:
        assert lead.source_type == EXPERIMENTAL_SOURCE_TYPE
        assert lead.consultation_note
        assert "experimental" in lead.consultation_note.lower()


def test_run_with_no_engines_returns_empty_summary() -> None:
    summary = run_experimental_search(
        engines={},
        company_name="Acme",
        company_domain=None,
        keywords=["marketing"],
        existing_leads=[],
    )
    assert summary.new_leads == []
    assert summary.engines_used == []
    assert summary.note  # explains why nothing happened


def test_run_emits_descriptive_queries_using_table_metadata() -> None:
    engine = FakeEngine([])
    run_experimental_search(
        engines={"fake": engine},
        company_name="Acme",
        company_domain="acme.com",
        keywords=["marketing", "growth"],
        existing_leads=[],
    )
    # The query builder should mention the company and at least one keyword
    # and bias toward LinkedIn profile URLs.
    assert engine.queries
    combined = " | ".join(engine.queries).lower()
    assert "acme" in combined
    assert "marketing" in combined
    assert "linkedin.com/in" in combined


def test_run_returns_empty_when_results_all_overlap() -> None:
    existing = [_existing("Ana Silva", "https://www.linkedin.com/in/ana-silva/")]
    engine = FakeEngine([_search_hit("Ana Silva", "ana-silva")])
    summary = run_experimental_search(
        engines={"fake": engine},
        company_name="Acme",
        company_domain=None,
        keywords=["marketing"],
        existing_leads=existing,
    )
    assert summary.new_leads == []
    assert summary.duplicates_skipped == 1
