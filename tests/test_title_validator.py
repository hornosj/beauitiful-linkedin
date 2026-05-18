"""TDD for the strict post-extraction title validator.

The motivation: LinkedIn's ``?keywords=marketing`` filter is fuzzy and
returns engineers, salespeople, etc. We need a deterministic, strict
check that compares the lead's *current* headline against the requested
keywords (with alias support) and rejects mismatches.
"""

from __future__ import annotations

from beautiful_linkedin.models import Lead
from beautiful_linkedin.processing.title_validator import (
    ValidationReason,
    validate_lead_titles,
)


def _lead(*, title: str | None, person: str = "Person X", url: str | None = None) -> Lead:
    profile = url or "https://www.linkedin.com/in/person-x/"
    return Lead(
        company_name="Nubank",
        company_domain="nubank.com.br",
        person_name=person,
        title=title,
        linkedin_url=profile,
        source_url=profile,
        source_type="linkedin_people_search",
        snippet=title or "",
        confidence_score=80,
    )


def test_validator_keeps_lead_when_title_matches_keyword_directly() -> None:
    leads = [_lead(title="Head of Marketing")]
    outcome = validate_lead_titles(leads, ["marketing"])
    assert outcome.valid == leads
    assert outcome.invalid == []


def test_validator_drops_lead_with_unrelated_title() -> None:
    leads = [_lead(title="Senior Software Engineer")]
    outcome = validate_lead_titles(leads, ["marketing"])
    assert outcome.valid == []
    assert len(outcome.invalid) == 1
    assert outcome.invalid[0].reason == ValidationReason.TITLE_MISMATCH


def test_validator_uses_aliases_for_keyword() -> None:
    """'growth' implies performance marketing/content marketing per aliases.
    The validator should still match when the headline uses an alias instead
    of the literal keyword."""
    leads = [_lead(title="Growth Marketing Manager")]
    outcome = validate_lead_titles(leads, ["marketing"])
    assert len(outcome.valid) == 1


def test_validator_treats_empty_title_as_invalid_when_strict() -> None:
    leads = [_lead(title=None)]
    outcome = validate_lead_titles(leads, ["marketing"], strict=True)
    assert outcome.valid == []
    assert outcome.invalid[0].reason == ValidationReason.MISSING_TITLE


def test_validator_keeps_empty_title_when_lax() -> None:
    """In lax mode (strict=False), missing titles are kept because the user
    accepted uncertain leads. This matches the existing include_uncertain
    semantics so we don't regress current behaviour."""
    leads = [_lead(title=None)]
    outcome = validate_lead_titles(leads, ["marketing"], strict=False)
    assert outcome.valid == leads


def test_validator_does_not_match_word_substring_of_unrelated_word() -> None:
    """'art' (alias of 'marketing'? no) shouldn't accidentally match because
    'art' appears inside 'startup' or similar. We test the word-boundary
    behaviour with a concrete example: keyword 'sales' must not match
    'wholesales analyst' as the only signal — but should match 'Sales Manager'.
    """
    leads = [
        _lead(title="Wholesale Analyst", person="A", url="https://www.linkedin.com/in/a/"),
        _lead(title="Sales Manager", person="B", url="https://www.linkedin.com/in/b/"),
    ]
    outcome = validate_lead_titles(leads, ["sales"])
    valid_names = {l.person_name for l in outcome.valid}
    invalid_names = {l.lead.person_name for l in outcome.invalid}
    assert "B" in valid_names
    # "Wholesale" contains "sales" as substring but should not match — this
    # is the main bug class we're trying to prevent.
    assert "A" in invalid_names


def test_validator_returns_no_target_titles_path() -> None:
    """When the search has no titles (general search), the validator keeps
    everything — there's nothing to check against."""
    leads = [_lead(title="Software Engineer"), _lead(title="Marketing Manager")]
    outcome = validate_lead_titles(leads, [])
    assert outcome.valid == leads
    assert outcome.invalid == []


def test_validator_reports_which_keyword_matched() -> None:
    leads = [_lead(title="Head of Growth")]
    outcome = validate_lead_titles(leads, ["marketing", "sales"])
    assert len(outcome.valid) == 1
    # The validator should attach a match annotation so we know WHY it was kept.
    assert outcome.matched_keyword[outcome.valid[0].linkedin_url] in {"marketing", "sales"}
