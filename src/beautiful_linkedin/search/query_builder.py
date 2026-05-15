from __future__ import annotations

from beautiful_linkedin.models import CompanyInput
from urllib.parse import urlparse

from beautiful_linkedin.processing.normalizer import normalize_text
from beautiful_linkedin.processing.title_aliases import (
    expand_deep_search_title_terms,
    expand_search_title_terms,
)


def build_queries_for_company(company: CompanyInput, depth: str = "standard") -> list[str]:
    queries = [
        query
        for group in _query_groups_for_company(company, depth=depth)
        for query in group
    ]
    return _dedupe_preserving_order(queries)


def build_balanced_queries_for_company(company: CompanyInput, depth: str = "standard") -> list[str]:
    groups = _query_groups_for_company(company, depth=depth)
    if not groups:
        return []

    queries: list[str] = []
    max_group_size = max(len(group) for group in groups)
    for query_index in range(max_group_size):
        for group in groups:
            if query_index < len(group):
                queries.append(group[query_index])

    return _dedupe_preserving_order(queries)


def _query_groups_for_company(company: CompanyInput, depth: str) -> list[list[str]]:
    title_terms = (
        expand_deep_search_title_terms(company.titles)
        if depth == "deep"
        else expand_search_title_terms(company.titles)
    )
    company_terms = _company_terms(company, include_expanded=depth == "deep")

    return [
        _queries_for_company_term(company, company_term, title, depth)
        for title in title_terms
        for company_term in company_terms
    ]


def _queries_for_company_term(
    company: CompanyInput,
    company_term: str,
    title: str,
    depth: str,
) -> list[str]:
    queries: list[str] = []
    # ----- Tier 1: site: operator — highest precision -----
    # Google is excellent with site:linkedin.com/in
    queries.append(f'site:linkedin.com/in "{company_term}" "{title}"')

    # ----- Tier 2: inurl: operator — Google / Bing understand this -----
    queries.append(f'inurl:linkedin.com/in "{company_term}" "{title}"')

    # ----- Tier 3: explicit URL mention — works on all engines -----
    queries.append(f'"{company_term}" "{title}" linkedin.com/in')

    # ----- Tier 4: "LinkedIn" keyword — catches snippets -----
    queries.append(f'"{company_term}" "{title}" "LinkedIn"')

    # ----- Tier 5: alternative patterns -----
    queries.append(f'"{company_term}" "{title}" site:linkedin.com')

    if company.company_domain:
        queries.append(f'"{company.company_domain}" "{title}" linkedin.com/in')

    if depth == "deep":
        queries.extend(
            [
                f'site:br.linkedin.com/in "{company_term}" "{title}"',
                # "Ver o perfil" is the Portuguese LinkedIn profile CTA
                f'"{company_term}" "{title}" "Ver o perfil"',
                f'"{company_term}" "{title}" "View profile"',
                # Broader company page reference
                f'"linkedin.com/company/{_slugify(company_term)}" "{title}"',
                # Try with title OR common variations
                f'site:linkedin.com/in "{company_term}" ({title} OR {_title_singular(title)})',
            ]
        )

    return queries


def _company_terms(company: CompanyInput, include_expanded: bool) -> list[str]:
    terms = [company.company_name]
    terms.extend(_known_company_aliases(company.company_name))
    if not include_expanded:
        return _dedupe_preserving_order(terms)

    simplified = _simplify_company_name(company.company_name)
    if simplified and simplified.lower() != company.company_name.lower():
        terms.append(simplified)

    slug = _linkedin_company_slug(company.linkedin_url)
    if slug:
        terms.extend(_terms_from_linkedin_slug(slug))

    return _dedupe_preserving_order(terms)


def _known_company_aliases(company_name: str) -> list[str]:
    normalized = normalize_text(company_name)
    if normalized in {"mercado livre", "mercado libre", "mercadolivre"}:
        return ["Mercado Livre Brasil", "Mercado Libre", "mercadolivre"]
    return []


def _simplify_company_name(company_name: str) -> str:
    removable_terms = [" banco ", " bank ", " inc ", " s.a. ", " sa ", " ltda ", " ltd "]
    simplified = f" {company_name.lower()} "
    for term in removable_terms:
        simplified = simplified.replace(term, " ")
    cleaned = " ".join(simplified.split())
    return cleaned.title()


def _linkedin_company_slug(linkedin_url: str | None) -> str | None:
    if not linkedin_url:
        return None
    parsed = urlparse(linkedin_url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "company":
        return parts[1]
    return None


def _terms_from_linkedin_slug(slug: str) -> list[str]:
    terms = [slug.replace("-", " ")]
    if slug.endswith("-com"):
        base = slug[: -len("-com")]
        terms.extend([f"{base}.com", base])
    return terms


def _slugify(text: str) -> str:
    """Convert a company name to a LinkedIn-like slug."""
    return text.lower().replace(" ", "-").replace(".", "").replace(",", "")


def _title_singular(title: str) -> str:
    """Naive attempt to get a singular form (remove trailing s)."""
    t = title.strip()
    if t.lower().endswith("s") and len(t) > 3:
        return t[:-1]
    return t


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        normalized = value.lower()
        if normalized not in seen:
            deduped.append(value)
            seen.add(normalized)
    return deduped
