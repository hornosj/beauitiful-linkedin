"""Filter/ranking presets applied locally to a saved-leads table.

The presets exist so prospecting at small companies can run a broad
``people_search`` and the user still gets focused subsets ("Gerar tabela de
RH", "...de Marketing", etc) without re-running a network search.

Matching uses the lead's *real* ``title``/headline — never the keyword the
user originally searched with — so a 'marketing'-flavored search at a 200
person company doesn't accidentally tag every engineer as marketing.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from beautiful_linkedin.models import Lead


PRESET_ALIASES: dict[str, tuple[str, ...]] = {
    "rh": (
        "rh",
        "recursos humanos",
        "human resources",
        "people",
        "talent",
        "people operations",
        "talent acquisition",
    ),
    "people": (
        "people",
        "people operations",
        "talent",
        "talent acquisition",
        "human resources",
        "rh",
    ),
    "tech": (
        "software",
        "developer",
        "desenvolvedor",
        "engineer",
        "engenharia",
        "tech",
        "tecnologia",
        "backend",
        "frontend",
        "full stack",
        "fullstack",
        "devops",
        "sre",
        "data engineer",
        "data scientist",
        "machine learning",
        "platform",
    ),
    "sales": (
        "sales",
        "vendas",
        "comercial",
        "account executive",
        "sdr",
        "bdr",
        "business development",
    ),
    "marketing": (
        "marketing",
        "growth",
        "demand generation",
        "performance marketing",
        "social media",
        "content",
        "cmo",
        "brand",
    ),
    "custom": (),
}


@dataclass(frozen=True)
class Preset:
    value: str
    label: str
    aliases: tuple[str, ...]


_LABELS: dict[str, str] = {
    "rh": "Tabela de RH",
    "people": "Tabela de People",
    "tech": "Tabela de Tecnologia",
    "sales": "Tabela de Vendas",
    "marketing": "Tabela de Marketing",
    "custom": "Tabela customizada",
}


def list_presets() -> list[Preset]:
    return [
        Preset(value=key, label=_LABELS.get(key, key), aliases=PRESET_ALIASES[key])
        for key in PRESET_ALIASES
    ]


def matches_preset(lead: Lead, preset: str) -> bool:
    """True when the lead's *headline* (``title``) matches the preset.

    The user-provided search keyword is intentionally NOT consulted —
    matching has to come from what the person publicly calls themselves.
    """
    if preset == "custom":
        return True
    aliases = PRESET_ALIASES.get(preset)
    if not aliases:
        return False
    return _alias_in_text(aliases, lead.title) is not None


def apply_preset(leads: Iterable[Lead], preset: str) -> list[Lead]:
    """Return only leads matching ``preset``.

    For non-custom presets, ``matched_title`` on the returned copy is set
    to the canonical alias that triggered the match — so the UI can show
    *why* a row was retained without re-deriving the logic on the front end.
    Pre-existing ``matched_title`` from the search is intentionally
    overwritten because that field carried the search keyword, not the
    headline alias.
    """
    leads_list = list(leads)
    if preset == "custom":
        return leads_list
    aliases = PRESET_ALIASES.get(preset, ())
    if not aliases:
        return []
    out: list[Lead] = []
    for lead in leads_list:
        matched = _alias_in_text(aliases, lead.title)
        if matched is None:
            continue
        out.append(lead.model_copy(update={"matched_title": matched}))
    return out


def _alias_in_text(aliases: Iterable[str], text: str | None) -> str | None:
    if not text:
        return None
    haystack = _normalize(text)
    for alias in aliases:
        needle = _normalize(alias)
        if not needle:
            continue
        if re.search(rf"\b{re.escape(needle)}\b", haystack):
            return alias
    return None


def _normalize(value: str) -> str:
    no_accents = (
        unicodedata.normalize("NFD", value)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    return no_accents.lower().strip()
