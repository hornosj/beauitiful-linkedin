from __future__ import annotations

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

_SENIORITY_PATTERNS: dict[Seniority, list[str]] = {
    Seniority(level): [normalize_text(alias) for alias in aliases if alias.strip()]
    for level, aliases in SENIORITY_ALIASES.items()
}


def classify_seniority(title: str | None) -> Seniority | None:
    """Return the highest seniority level matched by ``title``.

    Matches are evaluated in order from most senior to least senior so that a
    string like "VP of Engineering and former Senior Manager" is classified as
    VP, not Manager.
    """
    normalized = _padded(normalize_text(title))
    if not normalized.strip():
        return None
    for level in SENIORITY_ORDER:
        for pattern in _SENIORITY_PATTERNS[level]:
            if not pattern:
                continue
            needle = _padded(pattern)
            if needle in normalized:
                return level
    return None


def aliases_for(level: Seniority) -> list[str]:
    return list(SENIORITY_ALIASES.get(level.value, []))


def _padded(value: str) -> str:
    return f" {value} "
