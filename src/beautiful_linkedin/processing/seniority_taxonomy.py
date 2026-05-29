from __future__ import annotations

import re
from enum import Enum

from beautiful_linkedin.processing.normalizer import normalize_text


class Seniority(str, Enum):
    C_LEVEL = "c_level"
    VP = "vp"
    DIRECTOR = "director"
    HEAD = "head"
    MANAGER = "manager"
    SENIOR = "senior"
    MID = "mid"
    JUNIOR = "junior"
    INTERN = "intern"


SENIORITY_ORDER: list[Seniority] = [
    Seniority.C_LEVEL,
    Seniority.VP,
    Seniority.DIRECTOR,
    Seniority.HEAD,
    Seniority.MANAGER,
    Seniority.SENIOR,
    Seniority.MID,
    Seniority.JUNIOR,
    Seniority.INTERN,
]


SENIORITY_ALIASES: dict[str, list[str]] = {
    Seniority.C_LEVEL.value: [
        "ceo", "cfo", "cto", "cmo", "coo", "cpo", "cio", "ciso", "chro", "cdo", "cro",
        "chief executive", "chief financial", "chief technology", "chief marketing",
        "chief operating", "chief product", "chief information", "chief security",
        "chief people", "chief data", "chief revenue", "chief growth", "chief of staff",
        "founder", "co-founder", "cofounder", "fundador", "fundadora", "co-fundador",
        "co-fundadora", "cofundador", "cofundadora", "presidente", "president",
        "owner", "proprietario", "proprietaria",
    ],
    Seniority.VP.value: [
        "vp", "v.p.", "vice president", "vice-president", "vice presidente",
        "vice-presidente", "evp", "svp", "executive vice president",
        "senior vice president",
    ],
    Seniority.DIRECTOR.value: [
        "director", "directora", "diretor", "diretora", "diretoria",
        "executive director", "managing director", "diretor executivo",
        "diretora executiva", "diretor geral", "diretora geral", "country director",
        "regional director",
    ],
    Seniority.HEAD.value: [
        "head", "head of", "global head", "regional head", "responsavel por",
        "responsavel pela", "responsável por", "responsável pela",
    ],
    Seniority.MANAGER.value: [
        "manager", "gerente", "gerenta", "coordenador", "coordenadora", "coordinator",
        "supervisor", "supervisora", "team lead", "team leader", "tech lead",
        "lider de", "líder de", "lider tecnico", "líder técnico", "engineering manager",
        "product manager", "people manager", "principal",
    ],
    Seniority.SENIOR.value: [
        "senior", "sênior", "sr.", "sr ", "specialist", "especialista senior",
        "especialista sênior", "consultor senior", "consultora senior",
        "consultor sênior", "staff", "staff engineer",
    ],
    Seniority.MID.value: [
        "pleno", "plena", "mid level", "mid-level", "mid ", "intermediario",
        "intermediário", "analista pleno", "analista plena",
    ],
    Seniority.JUNIOR.value: [
        "junior", "júnior", "jr.", "jr ", "trainee", "assistente", "assistant",
        "associate", "analista junior", "analista júnior",
    ],
    Seniority.INTERN.value: [
        "intern", "estagiario", "estagiária", "estagiaria", "estágio", "estagio",
        "internship", "aprendiz",
    ],
}

# Rank by descending seniority (0 = most senior). Used both for "this
# level or above" filtering and to keep the highest match when several
# levels would otherwise apply.
SENIORITY_RANK: dict[Seniority, int] = {
    level: index for index, level in enumerate(SENIORITY_ORDER)
}


# Any run of non-alphanumeric characters becomes a single space, so a token
# glued to punctuation ("VP," / "Sr." / "co-fundador") still matches as a
# whole word. The previous space-padding approach silently missed those.
_TOKENIZE_RE = re.compile(r"[^a-z0-9]+")


def _tokenized(value: str | None) -> str:
    normalized = normalize_text(value)
    if not normalized:
        return ""
    return _TOKENIZE_RE.sub(" ", normalized).strip()


def _alias_pattern(alias: str) -> re.Pattern[str] | None:
    """Compile a word-boundary regex for an alias after the same
    punctuation→space normalization applied to titles, so both sides use
    the same token shape (``\\b`` is safe because no edge punctuation
    survives)."""
    token = _TOKENIZE_RE.sub(" ", normalize_text(alias)).strip()
    if not token:
        return None
    return re.compile(rf"\b{re.escape(token)}\b")


_SENIORITY_PATTERNS: dict[Seniority, list[re.Pattern[str]]] = {
    Seniority(level): [
        pattern
        for alias in aliases
        if (pattern := _alias_pattern(alias)) is not None
    ]
    for level, aliases in SENIORITY_ALIASES.items()
}


def classify_seniority(title: str | None) -> Seniority | None:
    """Return the highest seniority level matched by ``title``.

    Matches are evaluated in order from most senior to least senior so that a
    string like "VP of Engineering and former Senior Manager" is classified as
    VP, not Manager. Matching is word-bounded over a punctuation-normalized
    form of the title, so "VP, Marketing" and "CMO, Head of Growth" classify
    correctly (vs. the old substring/space-padding approach).
    """
    text = _tokenized(title)
    if not text:
        return None
    for level in SENIORITY_ORDER:
        for pattern in _SENIORITY_PATTERNS[level]:
            if pattern.search(text):
                return level
    return None


def levels_at_or_above(level: Seniority) -> list[Seniority]:
    """Return ``level`` and every more-senior level, most senior first."""
    cutoff = SENIORITY_RANK[level]
    return [lvl for lvl in SENIORITY_ORDER if SENIORITY_RANK[lvl] <= cutoff]


def aliases_for(level: Seniority) -> list[str]:
    return list(SENIORITY_ALIASES.get(level.value, []))
