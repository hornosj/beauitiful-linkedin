from __future__ import annotations

from beautiful_linkedin.processing.normalizer import normalize_text

TITLE_ALIASES: dict[str, list[str]] = {
    "rh": [
        "rh",
        "recursos humanos",
        "human resources",
        "people",
        "talent",
        "gente e gestao",
        "gente e gestão",
    ],
    "hr": [
        "hr",
        "human resources",
        "people",
        "talent",
        "recursos humanos",
    ],
    "software": [
        "software",
        "software engineer",
        "software developer",
        "software analyst",
        "developer",
        "desenvolvedor",
        "desenvolvedora",
        "engenheiro de software",
        "engenheira de software",
        "analista de sistemas",
        "analista de software",
        "analista desenvolvedor",
        "systems analyst",
        "system analyst",
        "analyst software",
        "it analyst",
        "technology analyst",
        "technical analyst",
        "developer analyst",
        "softweare analyst",
        "backend",
        "frontend",
        "full stack",
        "tech lead",
        "devops",
    ],
    "sales": [
        "sales",
        "vendas",
        "comercial",
        "business development",
        "account executive",
        "account manager",
        "sdr",
        "bdr",
        "inside sales",
        "field sales",
        "sales operations",
        "head of sales",
        "diretor comercial",
        "diretora comercial",
        "executivo de contas",
        "executiva de contas",
        "representante comercial",
    ],
    "vendas": [
        "vendas",
        "sales",
        "comercial",
        "account executive",
        "sdr",
        "bdr",
        "head of sales",
    ],
    "comercial": [
        "comercial",
        "sales",
        "vendas",
        "business development",
        "executivo de contas",
        "representante comercial",
    ],
}

SEARCH_TITLE_ALIASES: dict[str, list[str]] = {
    "rh": ["rh", "recursos humanos", "human resources"],
    "hr": ["hr", "human resources", "recursos humanos"],
    "software": [
        "software",
        "software engineer",
        "software analyst",
        "desenvolvedor",
        "analista de sistemas",
        "analista de software",
        "systems analyst",
    ],
    "sales": [
        "sales",
        "vendas",
        "comercial",
        "account executive",
        "business development",
        "sdr",
        "bdr",
    ],
    "vendas": ["vendas", "sales", "comercial", "account executive"],
    "comercial": ["comercial", "sales", "vendas", "business development"],
    "marketing": [
        "marketing",
        "growth",
        "demand generation",
        "performance marketing",
        "head of marketing",
    ],
}

DEEP_SEARCH_TITLE_ALIASES: dict[str, list[str]] = {
    "rh": [
        "rh",
        "recursos humanos",
        "human resources",
        "gerente de rh",
        "superintendente de rh",
        "hr business partner",
        "talent acquisition",
        "people",
    ],
    "hr": [
        "hr",
        "human resources",
        "recursos humanos",
        "hr business partner",
        "talent acquisition",
        "people",
        "people operations",
    ],
    "software": [
        "software",
        "software engineer",
        "software developer",
        "software analyst",
        "developer",
        "desenvolvedor",
        "desenvolvedora",
        "engenheiro de software",
        "engenheira de software",
        "analista de sistemas",
        "analista de software",
        "analista desenvolvedor",
        "systems analyst",
        "system analyst",
        "analyst software",
        "it analyst",
        "technology analyst",
        "technical analyst",
        "developer analyst",
        "softweare analyst",
        "analista",
        "backend developer",
        "frontend developer",
        "full stack",
        "tech lead",
        "devops",
        "cloud engineer",
        "data engineer",
        "technology",
        "tecnologia",
    ],
    "sales": [
        "sales",
        "vendas",
        "comercial",
        "business development",
        "bizdev",
        "account executive",
        "account manager",
        "sdr",
        "bdr",
        "inside sales",
        "field sales",
        "channel sales",
        "sales operations",
        "partnerships",
        "representante comercial",
        "executivo de contas",
        "executiva de contas",
        "head of sales",
        "diretor comercial",
        "diretora comercial",
        "vp sales",
    ],
    "vendas": [
        "vendas",
        "sales",
        "comercial",
        "business development",
        "account executive",
        "sdr",
        "bdr",
        "head of sales",
    ],
    "comercial": [
        "comercial",
        "sales",
        "vendas",
        "business development",
        "executivo de contas",
        "representante comercial",
        "diretor comercial",
    ],
    "marketing": [
        "marketing",
        "growth",
        "demand generation",
        "performance marketing",
        "brand manager",
        "content marketing",
        "social media",
        "digital marketing",
        "marketing digital",
        "head of marketing",
        "diretor de marketing",
        "diretora de marketing",
        "cmo",
    ],
}


def expand_title_terms(titles: list[str]) -> list[str]:
    return _expand_from_map(titles, TITLE_ALIASES)


def expand_search_title_terms(titles: list[str]) -> list[str]:
    return _expand_from_map(titles, SEARCH_TITLE_ALIASES)


def expand_deep_search_title_terms(titles: list[str]) -> list[str]:
    return _expand_from_map(titles, DEEP_SEARCH_TITLE_ALIASES)


def matches_title_alias(text: str, title: str) -> bool:
    normalized_text = normalize_text(text)
    return any(normalize_text(alias) in normalized_text for alias in _aliases_for_title(title))


def _expand_from_map(titles: list[str], alias_map: dict[str, list[str]]) -> list[str]:
    expanded: list[str] = []
    seen: set[str] = set()

    for title in titles:
        normalized_title = normalize_text(title)
        if normalized_title and normalized_title not in seen:
            expanded.append(title)
            seen.add(normalized_title)

    for title in titles:
        normalized_title = normalize_text(title)
        for term in alias_map.get(normalized_title, []):
            normalized = normalize_text(term)
            if normalized and normalized not in seen:
                expanded.append(term)
                seen.add(normalized)

    return expanded


def _aliases_for_title(title: str) -> list[str]:
    normalized = normalize_text(title)
    return TITLE_ALIASES.get(normalized, [title])
