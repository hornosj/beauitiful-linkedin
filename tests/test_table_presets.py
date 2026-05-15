"""TDD for saved-leads filter presets.

Presets group keyword aliases used to filter and rank a saved table into
focused subsets (RH/People/Tech/Vendas/Marketing/Customizada). The match
must rely on the lead's *actual* headline/title — never on the keyword
the user originally searched with — so prospecting for 'marketing' at a
small startup can still surface only the people whose own LinkedIn
headline calls them marketing.
"""

from __future__ import annotations

import pytest

from beautiful_linkedin.models import Lead
from beautiful_linkedin.processing.table_presets import (
    PRESET_ALIASES,
    Preset,
    apply_preset,
    list_presets,
    matches_preset,
)


def _lead(*, title: str | None, person: str = "Anon") -> Lead:
    return Lead(
        company_name="Co",
        person_name=person,
        title=title,
        linkedin_url=f"https://www.linkedin.com/in/{person.lower()}/",
        source_url=f"https://www.linkedin.com/in/{person.lower()}/",
        source_type="linkedin_people_search",
        snippet="",
        matched_title=None,
        validation_status="valid",
        confidence_score=80,
    )


def test_list_presets_returns_documented_options() -> None:
    presets = {preset.value for preset in list_presets()}
    assert presets >= {"rh", "tech", "sales", "marketing", "people", "custom"}


def test_aliases_cover_required_terms() -> None:
    assert "recursos humanos" in PRESET_ALIASES["rh"]
    assert "developer" in PRESET_ALIASES["tech"]
    assert "sdr" in PRESET_ALIASES["sales"]
    assert "growth" in PRESET_ALIASES["marketing"]
    assert "talent acquisition" in PRESET_ALIASES["people"]


@pytest.mark.parametrize(
    "preset,title,expected",
    [
        ("rh", "Analista de Recursos Humanos", True),
        ("rh", "Head of People", True),  # cross-aliases acceptable
        ("rh", "Backend Developer", False),
        ("tech", "Senior Software Engineer", True),
        ("tech", "Desenvolvedor Full Stack", True),
        ("tech", "CMO", False),
        ("sales", "Account Executive", True),
        ("sales", "SDR LATAM", True),
        ("sales", "Software Engineer", False),
        ("marketing", "Growth Lead", True),
        ("marketing", "Performance Marketing", True),
        ("marketing", "DevOps", False),
        ("custom", "Anything", True),  # custom matches every lead
    ],
)
def test_matches_preset(preset: str, title: str, expected: bool) -> None:
    assert matches_preset(_lead(title=title), preset) is expected


def test_apply_preset_filters_and_marks_match() -> None:
    leads = [
        _lead(title="Head of Marketing", person="Ana"),
        _lead(title="Backend Engineer", person="Bruno"),
        _lead(title="Growth Marketing Manager", person="Carla"),
    ]
    out = apply_preset(leads, "marketing")
    assert {lead.person_name for lead in out} == {"Ana", "Carla"}
    # matched_title was annotated with the canonical alias that matched, so
    # the UI can show the user why the lead was retained.
    annotated_titles = {lead.matched_title for lead in out}
    assert annotated_titles & {"marketing", "growth"}


def test_apply_preset_does_not_use_keyword_as_evidence() -> None:
    """Bug we are guarding against: if the user searched 'marketing', the
    keyword leaking into matched_title shouldn't make engineers match the
    'marketing' preset. Only the actual headline counts.
    """
    eng = _lead(title="Senior Backend Engineer", person="Eng")
    # Simulate a lead that came from the search with a stale matched_title.
    eng_with_stale_match = eng.model_copy(update={"matched_title": "marketing"})
    out = apply_preset([eng_with_stale_match], "marketing")
    assert out == []


def test_custom_preset_returns_all_leads_untouched() -> None:
    leads = [_lead(title="A"), _lead(title="B", person="b"), _lead(title=None, person="c")]
    out = apply_preset(leads, "custom")
    assert len(out) == 3
