"""End-to-end TDD for the iterative click→extract→validate loop.

The provider drives a fake fetcher that returns a controlled sequence of
HTML snapshots (one per simulated click). After each snapshot the provider
extracts cards, marks cards without related keywords, and asks the fetcher
to keep clicking only if there are not yet ``max_results`` keyword-matched
leads.

The fake fetcher records which click index each ``on_step`` was invoked
with so we can assert the provider stopped early when satisfied.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from beautiful_linkedin.cache.sqlite_cache import SqliteJsonCache
from beautiful_linkedin.models import CompanyInput
from beautiful_linkedin.providers.linkedin_people_search import (
    LinkedInPeopleSearchProvider,
    PeopleSearchOptions,
)
from beautiful_linkedin.providers.linkedin_people_search_progress import (
    PROGRESS_NAMESPACE,
    PeopleScrapeProgress,
    ProgressStore,
)


def _card(name: str, headline: str, slug: str) -> str:
    return f"""
    <li class="org-people-profile-card__profile-card-spacing">
      <a href="/in/{slug}/">
        <div class="artdeco-entity-lockup__title">{name}</div>
      </a>
      <div class="artdeco-entity-lockup__subtitle">{headline}</div>
    </li>
    """


def _page(*cards: str) -> str:
    return f"<html><body><ul>{''.join(cards)}</ul></body></html>"


class FakeIterativeFetcher:
    """Fake that supports the iterative API."""

    needs_li_at = False

    def __init__(self, snapshots: list[str]) -> None:
        self._snapshots = snapshots
        self.steps_observed: list[int] = []

    def fetch_listing_iterative(
        self,
        url: str,
        *,
        li_at: str,
        max_clicks: int,
        on_step: Callable[[int, str], bool],
    ) -> str:
        """Walk through ``self._snapshots`` one per simulated click.

        The provider's callback decides when to stop. We surface every step
        index so the test can assert the provider asked us to halt early.
        """
        if not self._snapshots:
            return ""
        # Step 0: initial render (no click yet).
        html = self._snapshots[0]
        self.steps_observed.append(0)
        if not on_step(0, html):
            return html
        for click_index in range(1, min(len(self._snapshots), max_clicks + 1)):
            html = self._snapshots[click_index]
            self.steps_observed.append(click_index)
            if not on_step(click_index, html):
                return html
        return html

    # Compatibility with the legacy one-shot Protocol — return the last snapshot.
    def fetch_listing(self, url: str, *, li_at: str, scrolls: int) -> str:
        return self._snapshots[-1] if self._snapshots else ""


def _company() -> CompanyInput:
    return CompanyInput(
        company_name="Nubank",
        company_domain="nubank.com.br",
        linkedin_url="https://www.linkedin.com/company/nubank/",
        titles=["marketing"],
    )


def test_iterative_loop_stops_early_when_enough_valid_leads_found() -> None:
    """Step 0 already has 2 marketing leads — with max_results=2, the
    provider must tell the fetcher to stop before any extra clicks happen."""
    snapshots = [
        _page(
            _card("Ana Silva", "Head of Marketing", "ana"),
            _card("Bruno Costa", "Growth Manager", "bruno"),
        ),
        _page(
            _card("Engineer 1", "Senior Software Engineer", "eng1"),
        ),
    ]
    fetcher = FakeIterativeFetcher(snapshots)
    provider = LinkedInPeopleSearchProvider(
        cookie="li_at_test",
        fetcher=fetcher,
        options=PeopleSearchOptions(scrolls=5, min_delay_seconds=0, max_delay_seconds=0),
    )

    leads = provider.find_leads(_company(), max_results=2, include_uncertain=False)

    assert len(leads) == 2
    assert {l.person_name for l in leads} == {"Ana Silva", "Bruno Costa"}
    # Stopped at step 0 — never clicked through to the engineer snapshot.
    assert fetcher.steps_observed == [0]


def test_iterative_loop_keeps_clicking_until_enough_valid_leads_found() -> None:
    """First two snapshots are full of engineers (bad results for a marketing
    search). The provider must keep clicking until the third snapshot, where
    a marketing lead finally appears."""
    snapshots = [
        _page(
            _card("Eng A", "Backend Engineer", "enga"),
            _card("Eng B", "Frontend Engineer", "engb"),
        ),
        _page(
            _card("Eng C", "Mobile Engineer", "engc"),
        ),
        _page(
            _card("Ana", "Marketing Manager", "ana"),
        ),
    ]
    fetcher = FakeIterativeFetcher(snapshots)
    provider = LinkedInPeopleSearchProvider(
        cookie="li_at_test",
        fetcher=fetcher,
        options=PeopleSearchOptions(scrolls=10, min_delay_seconds=0, max_delay_seconds=0),
    )

    leads = provider.find_leads(_company(), max_results=1, include_uncertain=False)

    assert {lead.person_name for lead in leads} == {"Eng A", "Eng B", "Eng C", "Ana"}
    ana = next(lead for lead in leads if lead.person_name == "Ana")
    assert ana.validation_status == "valid"
    eng = next(lead for lead in leads if lead.person_name == "Eng A")
    assert eng.validation_status == "maybe_incorrect"
    assert eng.validation_note == "Encontrado porém sem keywords relacionadas."
    # Visited at least steps 0..2 to find the marketing lead.
    assert fetcher.steps_observed[:3] == [0, 1, 2]


def test_lead_consultation_note_records_clicks_and_position() -> None:
    """Each accepted lead carries provenance: clicks performed before it
    showed up + 1-indexed position in the listing across all visible cards."""
    snapshots = [
        _page(
            _card("Eng A", "Backend Engineer", "enga"),
            _card("Eng B", "Frontend Engineer", "engb"),
        ),
        _page(
            _card("Eng A", "Backend Engineer", "enga"),
            _card("Eng B", "Frontend Engineer", "engb"),
            _card("Ana", "Marketing Manager", "ana"),
        ),
    ]
    fetcher = FakeIterativeFetcher(snapshots)
    provider = LinkedInPeopleSearchProvider(
        cookie="li_at_test",
        fetcher=fetcher,
        options=PeopleSearchOptions(scrolls=5, min_delay_seconds=0, max_delay_seconds=0),
    )

    leads = provider.find_leads(_company(), max_results=1, include_uncertain=False)

    ana = next(lead for lead in leads if lead.person_name == "Ana")
    note = ana.consultation_note or ""
    assert "1 clique" in note or "1 cliques" in note
    assert "posição 3" in note or "posicao 3" in note


def test_progress_persists_keywordless_cards_as_accepted(tmp_path: Path) -> None:
    """Cards without related keywords are no longer rejected; they are returned
    and cached as accepted with a maybe_incorrect marker."""
    snapshots = [
        _page(
            _card("Eng A", "Backend Engineer", "enga"),
            _card("Ana", "Marketing Manager", "ana"),
            _card("Eng B", "Frontend Engineer", "engb"),
        ),
    ]
    cache = SqliteJsonCache(tmp_path / "progress.sqlite")
    store = ProgressStore(cache)

    # First run.
    fetcher1 = FakeIterativeFetcher(snapshots)
    provider1 = LinkedInPeopleSearchProvider(
        cookie="li_at_test",
        fetcher=fetcher1,
        options=PeopleSearchOptions(scrolls=2, min_delay_seconds=0, max_delay_seconds=0),
        progress_store=store,
    )
    leads1 = provider1.find_leads(_company(), max_results=1, include_uncertain=False)
    assert {lead.person_name for lead in leads1} == {"Eng A", "Ana"}
    eng = next(lead for lead in leads1 if lead.person_name == "Eng A")
    assert eng.validation_status == "maybe_incorrect"
    assert eng.validation_note == "Encontrado porém sem keywords relacionadas."

    saved = store.load(company_slug="nubank", titles=["marketing"])
    assert saved is not None
    assert not any("enga" in url for url in saved.rejected_urls)
    assert any("enga" in url for url in saved.accepted_urls)
    assert any("/in/ana/" in url for url in saved.accepted_urls)
