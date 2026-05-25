"""Offline tests for _merge_phone_verification.

Mirrors test_saved_leads_enrichment's merge_email cases. The contract
both layers depend on:
- primary phone is NEVER overwritten by the helper,
- same digits → source appended to verified_by (drives the badge),
- different digits → entry appended to alternatives (deduped by
  (phone, source)).
"""

from __future__ import annotations

from beautiful_linkedin.storage.saved_leads import _merge_phone_verification


NOW = "2026-05-20T00:00:00+00:00"


def test_merge_first_write_seeds_verified_by() -> None:
    verified, alternatives = _merge_phone_verification(
        existing_phone=None,
        existing_verified_by=[],
        existing_alternatives=[],
        incoming_phone="+5511999999999",
        incoming_source="internal",
        incoming_confidence=80,
        now=NOW,
    )
    assert verified == ["internal"]
    assert alternatives == []


def test_merge_same_digits_adds_to_verified_by_regardless_of_format() -> None:
    """``+55 11 99999-9999`` and ``11999999999`` are the same number;
    the helper compares digits only so different formats don't fork
    the trail."""
    verified, alternatives = _merge_phone_verification(
        existing_phone="+55 11 99999-9999",
        existing_verified_by=["internal"],
        existing_alternatives=[],
        incoming_phone="11999999999",
        incoming_source="apollo",
        incoming_confidence=92,
        now=NOW,
    )
    assert verified == ["internal", "apollo"]
    assert alternatives == []


def test_merge_diverging_digits_become_alternatives() -> None:
    verified, alternatives = _merge_phone_verification(
        existing_phone="+5511999999999",
        existing_verified_by=["internal"],
        existing_alternatives=[],
        incoming_phone="+5511888888888",
        incoming_source="apollo",
        incoming_confidence=70,
        now=NOW,
    )
    assert verified == ["internal"]
    assert len(alternatives) == 1
    assert alternatives[0]["phone"] == "+5511888888888"
    assert alternatives[0]["source"] == "apollo"
    assert alternatives[0]["confidence"] == 70


def test_merge_dedupes_alternative_by_phone_and_source() -> None:
    """Apollo proposing the same alternative twice across two runs
    should not push two entries — the verification trail must be
    idempotent so the UI doesn't grow forever."""
    verified, alternatives = _merge_phone_verification(
        existing_phone="+5511999999999",
        existing_verified_by=["internal"],
        existing_alternatives=[
            {
                "phone": "+5511888888888",
                "source": "apollo",
                "confidence": 70,
                "found_at": NOW,
            }
        ],
        incoming_phone="+5511888888888",
        incoming_source="apollo",
        incoming_confidence=80,
        now=NOW,
    )
    assert verified == ["internal"]
    assert len(alternatives) == 1


def test_merge_different_source_with_same_alternative_lands_separately() -> None:
    """Two paid providers disagreeing with the primary should both be
    visible to the user — we keep one entry per (phone, source)."""
    verified, alternatives = _merge_phone_verification(
        existing_phone="+5511999999999",
        existing_verified_by=["internal"],
        existing_alternatives=[
            {
                "phone": "+5511888888888",
                "source": "apollo",
                "confidence": 70,
                "found_at": NOW,
            }
        ],
        incoming_phone="+5511888888888",
        incoming_source="lusha",
        incoming_confidence=85,
        now=NOW,
    )
    assert verified == ["internal"]
    assert {alt["source"] for alt in alternatives} == {"apollo", "lusha"}


def test_merge_blank_incoming_is_noop() -> None:
    verified, alternatives = _merge_phone_verification(
        existing_phone="+5511999999999",
        existing_verified_by=["internal"],
        existing_alternatives=[],
        incoming_phone=None,
        incoming_source="apollo",
        incoming_confidence=None,
        now=NOW,
    )
    assert verified == ["internal"]
    assert alternatives == []


def test_merge_does_not_append_same_source_twice_to_verified_by() -> None:
    verified, alternatives = _merge_phone_verification(
        existing_phone="+5511999999999",
        existing_verified_by=["internal", "apollo"],
        existing_alternatives=[],
        incoming_phone="+5511999999999",
        incoming_source="apollo",
        incoming_confidence=92,
        now=NOW,
    )
    assert verified == ["internal", "apollo"]
    assert alternatives == []
