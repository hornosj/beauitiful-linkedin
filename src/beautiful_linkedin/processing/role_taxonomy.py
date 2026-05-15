from __future__ import annotations

AREAS_MAP = {
    "Marketing / Growth": [
        "marketing",
        "growth",
        "cmo",
        "head of marketing",
        "demand generation",
        "performance marketing",
        "brand manager",
    ],
    "Vendas / Comercial": [
        "sales",
        "vendas",
        "comercial",
        "business development",
        "sdr",
        "bdr",
        "account executive",
        "head of sales",
    ],
    "RH / People / Talent": [
        "rh",
        "people",
        "talent",
        "recruiter",
        "recrutamento",
        "human resources",
        "people partner",
    ],
    "Produto": [
        "product",
        "produto",
        "product manager",
        "head of product",
        "product owner",
    ],
    "Tecnologia": [
        "technology",
        "tecnologia",
        "engineering",
        "software engineer",
        "tech lead",
        "cto",
        "head of engineering",
    ],
    "C-Level": [
        "ceo",
        "founder",
        "co-founder",
        "cfo",
        "coo",
        "cmo",
        "cto",
    ],
}

AREA_SLUGS = {
    "Marketing / Growth": "marketing_growth",
    "Vendas / Comercial": "sales_commercial",
    "RH / People / Talent": "hr_people_talent",
    "Produto": "product",
    "Tecnologia": "technology",
    "C-Level": "c_level",
}


def get_terms_for_area(area: str) -> list[str]:
    """Returns a list of terms mapped to a specific area. Defaults to the area name if not found."""
    return AREAS_MAP.get(area, [area])


def get_area_slug(area: str) -> str:
    """Returns the stable API value for a predefined role area."""
    return AREA_SLUGS.get(area, area.lower().replace(" / ", "_").replace(" ", "_"))


def is_custom_area(area: str) -> bool:
    """Returns True if the given area is not in the predefined map."""
    return area not in AREAS_MAP

def get_available_areas() -> list[str]:
    """Returns the list of predefined areas plus 'Customizada'."""
    return list(AREAS_MAP.keys()) + ["Customizada"]
