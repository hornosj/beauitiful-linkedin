"""Persistable scrape progress for ``LinkedInPeopleSearchProvider``.

The People-tab scraper now operates in an iterative click → extract →
validate → click loop. To make that loop resumable across runs (and so the
UI can show "this lead came in after N clicks at position P"), we track:

- ``clicks_performed`` — how many "Exibir mais resultados" presses have
  happened in the current/last run.
- ``cards_seen_total`` — total cards seen across all clicks (including
  rejected ones).
- ``accepted_urls`` — profile URLs that passed the strict title validator,
  in the order they were emitted.
- ``rejected_urls`` — profile URLs we already dropped (engineer-for-a-
  marketing-search and similar). A re-run skips these without burning the
  validator on them again.
- Per-URL bookkeeping for the UI: at which click the lead was extracted
  and at which position in the listing.

State is keyed by ``(company_slug, titles)`` so two different searches on
the same company keep separate progress files. Title order is normalised
so ``["marketing", "growth"]`` and ``["growth", "marketing"]`` collide.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from beautiful_linkedin.cache.sqlite_cache import SqliteJsonCache


PROGRESS_NAMESPACE = "linkedin_people_search.progress"


@dataclass
class PeopleScrapeProgress:
    clicks_performed: int = 0
    cards_seen_total: int = 0
    accepted_urls: list[str] = field(default_factory=list)
    rejected_urls: list[str] = field(default_factory=list)
    # profile_url -> position in the listing (1-indexed)
    positions: dict[str, int] = field(default_factory=dict)
    # profile_url -> clicks_performed at the moment of acceptance
    clicks_at_acceptance: dict[str, int] = field(default_factory=dict)

    def note_click(self) -> None:
        self.clicks_performed += 1

    def record_acceptance(self, profile_url: str, *, position: int) -> None:
        if not profile_url:
            return
        if profile_url not in self.accepted_urls:
            self.accepted_urls.append(profile_url)
        self.positions[profile_url] = position
        self.clicks_at_acceptance[profile_url] = self.clicks_performed

    def record_rejection(self, profile_url: str) -> None:
        if not profile_url:
            return
        if profile_url not in self.rejected_urls:
            self.rejected_urls.append(profile_url)

    def is_known(self, profile_url: str) -> bool:
        return profile_url in self.positions or profile_url in self.rejected_urls

    def position_for(self, profile_url: str) -> int | None:
        return self.positions.get(profile_url)

    def clicks_at_extraction(self, profile_url: str) -> int | None:
        return self.clicks_at_acceptance.get(profile_url)

    def to_json(self) -> dict[str, Any]:
        return {
            "clicks_performed": self.clicks_performed,
            "cards_seen_total": self.cards_seen_total,
            "accepted_urls": list(self.accepted_urls),
            "rejected_urls": list(self.rejected_urls),
            "positions": dict(self.positions),
            "clicks_at_acceptance": dict(self.clicks_at_acceptance),
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "PeopleScrapeProgress":
        return cls(
            clicks_performed=int(payload.get("clicks_performed") or 0),
            cards_seen_total=int(payload.get("cards_seen_total") or 0),
            accepted_urls=list(payload.get("accepted_urls") or []),
            rejected_urls=list(payload.get("rejected_urls") or []),
            positions={
                str(k): int(v) for k, v in (payload.get("positions") or {}).items()
            },
            clicks_at_acceptance={
                str(k): int(v)
                for k, v in (payload.get("clicks_at_acceptance") or {}).items()
            },
        )


class ProgressStore:
    """Thin wrapper around :class:`SqliteJsonCache` for progress payloads."""

    def __init__(self, cache: SqliteJsonCache) -> None:
        self._cache = cache

    def load(
        self, *, company_slug: str, titles: list[str]
    ) -> PeopleScrapeProgress | None:
        raw = self._cache.get_json(
            PROGRESS_NAMESPACE, _key_payload(company_slug, titles)
        )
        if not isinstance(raw, dict):
            return None
        return PeopleScrapeProgress.from_json(raw)

    def save(
        self,
        *,
        company_slug: str,
        titles: list[str],
        progress: PeopleScrapeProgress,
    ) -> None:
        self._cache.set_json(
            PROGRESS_NAMESPACE,
            _key_payload(company_slug, titles),
            progress.to_json(),
        )


def _key_payload(company_slug: str, titles: list[str]) -> dict[str, Any]:
    return {
        "slug": (company_slug or "").lower().strip(),
        # Normalised so ["a","b"] and ["b","a"] hit the same cache key.
        "titles": sorted({(t or "").strip().lower() for t in titles if t}),
    }
