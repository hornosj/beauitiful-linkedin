"""Pure-logic tests for company-size classification.

The classifier consumes raw signals (employee count, visible card count,
range strings like '51-200 employees') and decides whether to treat a
company as 'small' (~50-300 funcionários). Drives the small-company
dialog on the people_search flow.
"""
from __future__ import annotations

import pytest

from beautiful_linkedin.processing.company_size import (
    SMALL_COMPANY_MAX,
    SMALL_COMPANY_MIN,
    CompanySizeClassification,
    classify_company_size,
    parse_company_size_from_html,
    parse_employee_count_text,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("11-50 employees", 50),
        ("51-200 funcionários", 200),
        ("201-500 employees", 500),
        ("1,001-5,000 employees", 5000),
        ("10,001+ employees", 10001),
        ("57 funcionários", 57),
        ("", None),
        ("about us", None),
        (None, None),
    ],
)
def test_parse_employee_count_text(raw: str | None, expected: int | None) -> None:
    assert parse_employee_count_text(raw) == expected


def test_classify_uses_exact_employee_count_when_known() -> None:
    result = classify_company_size(employee_count=120)
    assert result.is_small is True
    assert result.employee_count == 120
    assert result.source == "exact"
    assert SMALL_COMPANY_MIN <= result.employee_count <= SMALL_COMPANY_MAX


def test_classify_flags_below_range_as_small_with_note() -> None:
    result = classify_company_size(employee_count=20)
    assert result.is_small is True
    assert result.source == "exact"
    assert result.note  # explains why


def test_classify_flags_large_company_as_not_small() -> None:
    result = classify_company_size(employee_count=4_500)
    assert result.is_small is False
    assert result.employee_count == 4_500


def test_classify_uses_range_text_when_exact_missing() -> None:
    result = classify_company_size(employee_count_text="51-200 employees")
    assert result.is_small is True
    assert result.source == "range"
    assert result.employee_count == 200  # upper bound


def test_classify_heuristic_falls_back_to_visible_card_count() -> None:
    result = classify_company_size(visible_card_count=80)
    assert result.is_small is True
    assert result.source == "heuristic"


def test_classify_heuristic_skips_when_signals_insufficient() -> None:
    result = classify_company_size(visible_card_count=2)
    # Two cards alone is not enough evidence in either direction.
    assert result.is_small is None
    assert result.source == "unknown"


def test_classify_handles_no_signals() -> None:
    result = classify_company_size()
    assert isinstance(result, CompanySizeClassification)
    assert result.is_small is None
    assert result.source == "unknown"


def test_parse_company_size_from_html_extracts_associated_members_pt() -> None:
    html = """
    <header class="org-people__header-spacing-content">
      <h2>Funcionários</h2>
      <p>128 funcionários associados</p>
    </header>
    """
    out = parse_company_size_from_html(html)
    assert out["employee_count"] == 128
    assert "128" in (out["employee_count_text"] or "")


def test_parse_company_size_from_html_extracts_range_about_page() -> None:
    html = """
    <dl>
      <dt>Tamanho da empresa</dt>
      <dd>51-200 funcionários</dd>
      <dt>Sede</dt>
      <dd>São Paulo</dd>
    </dl>
    """
    out = parse_company_size_from_html(html)
    assert out["employee_count_text"] == "51-200 funcionários"
    assert out["employee_count"] == 200


def test_parse_company_size_from_html_extracts_english_range() -> None:
    html = """
    <section><h2>About</h2>
      <p>Company size: 1,001-5,000 employees</p>
      <p>2,300 associated members</p>
    </section>
    """
    out = parse_company_size_from_html(html)
    # Exact 'associated members' beats the range.
    assert out["employee_count"] == 2300


def test_parse_company_size_counts_visible_cards() -> None:
    html = '<ul><li class="org-people-profile-card__profile-card-spacing">a</li>' * 5 + (
        '<li class="org-people-profile-card__profile-card-spacing">b</li>'
    ) + "</ul>"
    out = parse_company_size_from_html(html)
    assert out["visible_card_count"] == 6


def test_parse_company_size_returns_empty_when_no_signal() -> None:
    assert parse_company_size_from_html("<html><body>hello</body></html>") == {
        "employee_count": None,
        "employee_count_text": None,
        "visible_card_count": 0,
    }


def test_classify_prefers_exact_over_range_and_heuristic() -> None:
    result = classify_company_size(
        employee_count=180,
        employee_count_text="1,001-5,000 employees",
        visible_card_count=900,
    )
    assert result.is_small is True
    assert result.source == "exact"
    assert result.employee_count == 180
