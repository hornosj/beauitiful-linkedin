"""Strict post-extraction validation of lead titles against search keywords.

The LinkedIn ``?keywords=marketing`` URL filter is fuzzy on LinkedIn's side
and frequently surfaces unrelated roles (engineers, designers, etc.) when
the keyword appears anywhere in the profile — current job, past experience,
even a bio line. The pipeline already has a soft match in
``processing.lead_extractor.match_target_title`` but it is permissive (uses
``substring`` and ``fuzz.partial_ratio``), which lets "Wholesale Analyst"
satisfy a "sales" search because the substring is present.

This module is the strict gate to run *after* leads have been emitted by
a provider. It enforces:

1. Word-boundary matching so ``sales`` does not match ``wholesale``.
2. Alias awareness via :data:`processing.title_aliases.TITLE_ALIASES`
   (so "growth" still matches a "marketing" search).
3. Explicit categorical reasons for drops, so the iterative scraper can
   know *why* a lead was rejected and re-fetch more cards.

The public entry point is :func:`validate_lead_titles`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from beautiful_linkedin.models import Lead
from beautiful_linkedin.processing.normalizer import normalize_text
from beautiful_linkedin.processing.title_aliases import (
    DEEP_SEARCH_TITLE_ALIASES,
    SEARCH_TITLE_ALIASES,
    TITLE_ALIASES,
)


def _merged_aliases() -> dict[str, list[str]]:
    """Union of all three alias maps so a marketing search picks up
    'growth', a sales search picks up 'sdr', etc."""
    merged: dict[str, list[str]] = {}
    for source in (TITLE_ALIASES, SEARCH_TITLE_ALIASES, DEEP_SEARCH_TITLE_ALIASES):
        for key, values in source.items():
            bucket = merged.setdefault(key, [])
            for value in values:
                if value not in bucket:
                    bucket.append(value)
    return merged


_ALIAS_MAP = _merged_aliases()


class ValidationReason(str, Enum):
    TITLE_MISMATCH = "title_mismatch"
    MISSING_TITLE = "missing_title"


@dataclass(frozen=True)
class InvalidLead:
    lead: Lead
    reason: ValidationReason


@dataclass
class ValidationOutcome:
    valid: list[Lead] = field(default_factory=list)
    invalid: list[InvalidLead] = field(default_factory=list)
    # profile_url -> matched keyword (so the UI can show "matched: marketing").
    matched_keyword: dict[str, str] = field(default_factory=dict)


def validate_lead_titles(
    leads: list[Lead],
    target_titles: list[str],
    *,
    strict: bool = True,
) -> ValidationOutcome:
    """Strictly validate that each lead's current title relates to a target keyword.

    Args:
        leads: leads to inspect.
        target_titles: keywords the user searched for (e.g. ``["marketing", "growth"]``).
            Empty list disables validation — everything passes through.
        strict: when ``True`` (the default), leads with empty/None titles are
            rejected because we cannot prove relevance. When ``False`` we keep
            them — matching the legacy ``include_uncertain`` behaviour so this
            module can also be used as a soft filter.
    """
    outcome = ValidationOutcome()
    if not target_titles:
        outcome.valid = list(leads)
        return outcome

    # Pre-build the keyword → [alias regex] index once. Regex uses \b so we
    # don't match substring inside an unrelated word.
    keyword_patterns: list[tuple[str, list[re.Pattern[str]]]] = []
    for raw_keyword in target_titles:
        keyword = (raw_keyword or "").strip()
        if not keyword:
            continue
        aliases = _aliases_for(keyword)
        patterns = [_word_pattern(alias) for alias in aliases]
        keyword_patterns.append((keyword, patterns))

    for lead in leads:
        title_text = (lead.title or "").strip()
        if not title_text:
            if strict:
                outcome.invalid.append(
                    InvalidLead(lead=lead, reason=ValidationReason.MISSING_TITLE)
                )
            else:
                outcome.valid.append(lead)
            continue

        normalized = normalize_text(title_text)
        matched_for_lead: str | None = None
        for keyword, patterns in keyword_patterns:
            if any(pattern.search(normalized) for pattern in patterns):
                matched_for_lead = keyword
                break

        if matched_for_lead is None:
            outcome.invalid.append(
                InvalidLead(lead=lead, reason=ValidationReason.TITLE_MISMATCH)
            )
        else:
            outcome.valid.append(lead)
            if lead.linkedin_url:
                outcome.matched_keyword[lead.linkedin_url] = matched_for_lead

    return outcome


def _aliases_for(keyword: str) -> list[str]:
    normalized = normalize_text(keyword)
    aliases = list(_ALIAS_MAP.get(normalized, []))
    if normalized and normalized not in [normalize_text(a) for a in aliases]:
        aliases.insert(0, keyword)
    return aliases or [keyword]


def _word_pattern(term: str) -> re.Pattern[str]:
    """Build a word-boundary regex for a (possibly multi-word) term.

    Multi-word terms keep the original sequence and surround the whole
    sequence with \\b so ``"head of marketing"`` matches but ``"head"`` alone
    does not when only the multi-word alias is allowed.
    """
    normalized = normalize_text(term)
    escaped = re.escape(normalized)
    return re.compile(rf"\b{escaped}\b", re.IGNORECASE)
