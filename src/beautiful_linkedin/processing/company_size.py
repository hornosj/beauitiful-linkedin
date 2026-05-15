"""Classify a company as 'small' (~50-300 funcionários) from available signals.

The classifier accepts the strongest signal it has (exact count > range > visible
cards heuristic) and is intentionally conservative: when in doubt it returns
``is_small=None`` so the UI can keep the keyword flow without scaring the user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

SMALL_COMPANY_MIN = 50
SMALL_COMPANY_MAX = 300

ClassificationSource = Literal["exact", "range", "heuristic", "unknown"]


@dataclass(frozen=True)
class CompanySizeClassification:
    is_small: bool | None
    employee_count: int | None
    source: ClassificationSource
    note: str | None = None


_RANGE_RE = re.compile(r"(\d[\d,]*)\s*[-–]\s*(\d[\d,]*)")
_PLUS_RE = re.compile(r"(\d[\d,]*)\s*\+")
_SINGLE_RE = re.compile(r"(\d[\d,]*)")


def parse_employee_count_text(raw: str | None) -> int | None:
    """Extract the upper bound (or single value) from strings like ``'51-200 employees'``.

    Returns ``None`` when the text contains no parseable count.
    """
    if not raw:
        return None
    text = raw.strip().lower()
    if not text:
        return None
    if not any(ch.isdigit() for ch in text):
        return None

    match = _RANGE_RE.search(text)
    if match:
        return _to_int(match.group(2))

    match = _PLUS_RE.search(text)
    if match:
        return _to_int(match.group(1))

    match = _SINGLE_RE.search(text)
    if match:
        return _to_int(match.group(1))
    return None


def classify_company_size(
    *,
    employee_count: int | None = None,
    employee_count_text: str | None = None,
    visible_card_count: int | None = None,
) -> CompanySizeClassification:
    if employee_count is not None:
        return _classify_from_count(employee_count, "exact")

    parsed = parse_employee_count_text(employee_count_text)
    if parsed is not None:
        return _classify_from_count(parsed, "range")

    if visible_card_count is not None and visible_card_count >= 10:
        # A modest crowd of visible cards is a signal that the company is at
        # least medium-sized but not gigantic.
        is_small = visible_card_count <= SMALL_COMPANY_MAX
        return CompanySizeClassification(
            is_small=is_small,
            employee_count=visible_card_count,
            source="heuristic",
            note=(
                "Estimativa baseada na contagem de cards visíveis na busca."
                if is_small
                else None
            ),
        )

    return CompanySizeClassification(
        is_small=None, employee_count=None, source="unknown"
    )


def _classify_from_count(
    count: int, source: ClassificationSource
) -> CompanySizeClassification:
    if count < SMALL_COMPANY_MIN:
        return CompanySizeClassification(
            is_small=True,
            employee_count=count,
            source=source,
            note="Empresa abaixo da faixa típica de empresa pequena (50+).",
        )
    if count <= SMALL_COMPANY_MAX:
        return CompanySizeClassification(
            is_small=True,
            employee_count=count,
            source=source,
        )
    return CompanySizeClassification(
        is_small=False,
        employee_count=count,
        source=source,
    )


def _to_int(raw: str) -> int:
    return int(raw.replace(",", "").replace(".", ""))


# ---------- HTML extraction ------------------------------------------------

# Catches the LinkedIn People page header ("128 funcionários associados", "12
# associated members"). The "associated members" phrasing is more precise than
# the company-page "Company size" range because LinkedIn only shows it once
# the listing has actually loaded.
_ASSOCIATED_RE = re.compile(
    r"(\d[\d.,]*)\s*(?:funcion[áa]rios?\s+associados|associated\s+members|"
    r"associated\s+employees|membros?\s+associados)",
    re.IGNORECASE,
)

# Catches the About-page "Tamanho da empresa: 51-200 funcionários" or
# "Company size: 1,001-5,000 employees" line.
_RANGE_RE_HTML = re.compile(
    r"(\d[\d.,]*\s*[-–]\s*\d[\d.,]*\s*(?:funcion[áa]rios?|employees))",
    re.IGNORECASE,
)
_RANGE_PLUS_HTML = re.compile(
    r"(\d[\d.,]*\+\s*(?:funcion[áa]rios?|employees))",
    re.IGNORECASE,
)

# Cards rendered on the People listing — used as a heuristic fallback.
_CARD_CLASS_RE = re.compile(
    r'class="[^"]*org-people-profile-card__profile-card-spacing[^"]*"',
)


def parse_company_size_from_html(html: str) -> dict[str, Any]:
    """Extract employee-count signals from a rendered LinkedIn page.

    Looks first for the high-confidence "associated members" header, then
    for a company-size range, then for the visible-card count. Returns an
    empty-valued dict when nothing matches — callers should pass the
    result straight into :func:`classify_company_size`.
    """
    text = _strip_html(html)

    employee_count: int | None = None
    employee_count_text: str | None = None

    match = _ASSOCIATED_RE.search(text)
    if match:
        employee_count = _to_int(match.group(1))
        employee_count_text = match.group(0).strip()
    else:
        range_match = _RANGE_RE_HTML.search(text) or _RANGE_PLUS_HTML.search(text)
        if range_match:
            employee_count_text = range_match.group(0).strip()
            employee_count = parse_employee_count_text(employee_count_text)

    return {
        "employee_count": employee_count,
        "employee_count_text": employee_count_text,
        "visible_card_count": _count_visible_cards(html),
    }


def _count_visible_cards(html: str) -> int:
    return len(_CARD_CLASS_RE.findall(html or ""))


_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(html: str) -> str:
    """Cheap text extraction — keeps tag whitespace as a single space.

    We deliberately avoid pulling in BeautifulSoup just for this; the
    regexes only need readable text and we already have lxml/bs4 elsewhere
    if a richer parse becomes necessary.
    """
    without_tags = re.sub(r"<[^>]+>", " ", html or "")
    return _WHITESPACE_RE.sub(" ", without_tags).strip()
