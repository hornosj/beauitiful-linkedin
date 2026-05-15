from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field, field_validator

from beautiful_linkedin.models import Lead
from beautiful_linkedin.processing.function_taxonomy import (
    JobFunction,
    classify_functions,
)
from beautiful_linkedin.processing.normalizer import normalize_text
from beautiful_linkedin.processing.seniority_taxonomy import (
    Seniority,
    classify_seniority,
)


class LeadFilter(BaseModel):
    """Post-collection filter applied to deduplicated leads.

    The empty filter is a no-op and keeps every lead. All fields are additive
    AND-combined: a lead must pass every populated criterion to be kept.
    Unknown classifications (no seniority/function detected from the title)
    are kept by default unless ``drop_unclassified`` is enabled, so the filter
    never silently drops leads with sparse data.
    """

    seniority_in: list[Seniority] = Field(default_factory=list)
    functions_in: list[JobFunction] = Field(default_factory=list)
    locations_in: list[str] = Field(default_factory=list)
    exclude_titles: list[str] = Field(default_factory=list)
    min_confidence_score: int | None = Field(default=None, ge=0, le=100)
    drop_unclassified: bool = False

    @field_validator("locations_in", "exclude_titles", mode="before")
    @classmethod
    def _coerce_str_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    def is_empty(self) -> bool:
        return not (
            self.seniority_in
            or self.functions_in
            or self.locations_in
            or self.exclude_titles
            or self.min_confidence_score is not None
        )


@dataclass
class FilterStats:
    kept: int = 0
    dropped_total: int = 0
    dropped_by_reason: dict[str, int] = field(default_factory=dict)

    def record_drop(self, reason: str) -> None:
        self.dropped_total += 1
        self.dropped_by_reason[reason] = self.dropped_by_reason.get(reason, 0) + 1


@dataclass
class FilterOutcome:
    leads: list[Lead]
    stats: FilterStats


def apply_lead_filter(leads: list[Lead], filter_: LeadFilter | None) -> FilterOutcome:
    if filter_ is None or filter_.is_empty():
        return FilterOutcome(leads=list(leads), stats=FilterStats(kept=len(leads)))

    stats = FilterStats()
    kept: list[Lead] = []

    seniority_set = set(filter_.seniority_in)
    function_set = set(filter_.functions_in)
    locations_normalized = [normalize_text(loc) for loc in filter_.locations_in if loc.strip()]
    exclude_normalized = [normalize_text(term) for term in filter_.exclude_titles if term.strip()]

    for lead in leads:
        searchable = _searchable_title(lead)

        if exclude_normalized and any(
            term and term in searchable for term in exclude_normalized
        ):
            stats.record_drop("exclude_titles")
            continue

        if (
            filter_.min_confidence_score is not None
            and lead.confidence_score < filter_.min_confidence_score
        ):
            stats.record_drop("min_confidence_score")
            continue

        if seniority_set:
            level = classify_seniority(searchable)
            if level is None:
                if filter_.drop_unclassified:
                    stats.record_drop("seniority_unclassified")
                    continue
            elif level not in seniority_set:
                stats.record_drop("seniority_not_in_set")
                continue

        if function_set:
            functions = classify_functions(searchable)
            if not functions:
                if filter_.drop_unclassified:
                    stats.record_drop("function_unclassified")
                    continue
            elif not function_set.intersection(functions):
                stats.record_drop("function_not_in_set")
                continue

        if locations_normalized:
            location_searchable = _searchable_location(lead)
            if not location_searchable:
                if filter_.drop_unclassified:
                    stats.record_drop("location_unknown")
                    continue
            elif not any(loc in location_searchable for loc in locations_normalized if loc):
                stats.record_drop("location_not_in_set")
                continue

        kept.append(lead)

    stats.kept = len(kept)
    return FilterOutcome(leads=kept, stats=stats)


def _searchable_title(lead: Lead) -> str:
    parts = [lead.title or "", lead.snippet or ""]
    return normalize_text(" ".join(part for part in parts if part))


def _searchable_location(lead: Lead) -> str:
    return normalize_text(lead.snippet or "")
