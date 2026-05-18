"""TDD for the People scrape progress tracker.

The provider needs to know, between runs, which profile URLs were already
rejected (so it stops re-evaluating them) and which were already accepted
(so it can return them without re-clicking). It also tracks how many
"Exibir mais resultados" clicks were performed and at what position each
accepted lead appeared.
"""

from __future__ import annotations

from pathlib import Path

from beautiful_linkedin.cache.sqlite_cache import SqliteJsonCache
from beautiful_linkedin.providers.linkedin_people_search_progress import (
    PeopleScrapeProgress,
    ProgressStore,
)


def test_empty_progress_has_no_state() -> None:
    progress = PeopleScrapeProgress()
    assert progress.clicks_performed == 0
    assert progress.cards_seen_total == 0
    assert progress.accepted_urls == []
    assert progress.rejected_urls == []


def test_progress_record_acceptance_tracks_position_and_click_count() -> None:
    progress = PeopleScrapeProgress()
    progress.note_click()
    progress.note_click()
    progress.record_acceptance("https://www.linkedin.com/in/ana/", position=29)
    progress.record_acceptance("https://www.linkedin.com/in/bruno/", position=31)

    assert progress.clicks_performed == 2
    assert progress.accepted_urls == [
        "https://www.linkedin.com/in/ana/",
        "https://www.linkedin.com/in/bruno/",
    ]
    assert progress.position_for("https://www.linkedin.com/in/ana/") == 29
    assert progress.clicks_at_extraction("https://www.linkedin.com/in/ana/") == 2


def test_progress_record_rejection_adds_to_rejected_list() -> None:
    progress = PeopleScrapeProgress()
    progress.note_click()
    progress.record_rejection("https://www.linkedin.com/in/engineer/")
    assert progress.rejected_urls == ["https://www.linkedin.com/in/engineer/"]


def test_progress_is_known_returns_true_for_accepted_or_rejected_urls() -> None:
    progress = PeopleScrapeProgress()
    progress.record_acceptance("https://www.linkedin.com/in/a/", position=1)
    progress.record_rejection("https://www.linkedin.com/in/b/")
    assert progress.is_known("https://www.linkedin.com/in/a/")
    assert progress.is_known("https://www.linkedin.com/in/b/")
    assert not progress.is_known("https://www.linkedin.com/in/c/")


def test_progress_serialises_and_deserialises_via_sqlite(tmp_path: Path) -> None:
    """A round-trip through the SQLite cache must preserve every field."""
    cache = SqliteJsonCache(tmp_path / "progress.sqlite")
    store = ProgressStore(cache)

    progress = PeopleScrapeProgress()
    progress.note_click()
    progress.note_click()
    progress.record_acceptance("https://www.linkedin.com/in/a/", position=12)
    progress.record_rejection("https://www.linkedin.com/in/b/")
    progress.cards_seen_total = 24

    store.save(company_slug="nubank", titles=["marketing"], progress=progress)
    reloaded = store.load(company_slug="nubank", titles=["marketing"])

    assert reloaded is not None
    assert reloaded.clicks_performed == 2
    assert reloaded.cards_seen_total == 24
    assert reloaded.accepted_urls == ["https://www.linkedin.com/in/a/"]
    assert reloaded.rejected_urls == ["https://www.linkedin.com/in/b/"]
    assert reloaded.position_for("https://www.linkedin.com/in/a/") == 12


def test_progress_key_is_independent_of_title_order_but_sensitive_to_titles(
    tmp_path: Path,
) -> None:
    """Two searches with the same titles in different order share state,
    but a search with different titles does not."""
    cache = SqliteJsonCache(tmp_path / "progress.sqlite")
    store = ProgressStore(cache)

    progress = PeopleScrapeProgress()
    progress.record_acceptance("https://www.linkedin.com/in/a/", position=1)
    store.save(
        company_slug="nubank", titles=["marketing", "growth"], progress=progress
    )

    same_titles_other_order = store.load(
        company_slug="nubank", titles=["growth", "marketing"]
    )
    different_titles = store.load(company_slug="nubank", titles=["engineer"])

    assert same_titles_other_order is not None
    assert same_titles_other_order.accepted_urls == [
        "https://www.linkedin.com/in/a/"
    ]
    assert different_titles is None
