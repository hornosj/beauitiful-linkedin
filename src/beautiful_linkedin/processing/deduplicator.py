from __future__ import annotations

from beautiful_linkedin.models import Lead
from beautiful_linkedin.processing.normalizer import normalize_source_url, normalize_text, normalize_url


def deduplicate_leads(leads: list[Lead]) -> list[Lead]:
    deduped: dict[tuple[str, str], Lead] = {}

    for lead in leads:
        key = lead_dedupe_key(lead)
        if key is None:
            synthetic_key = ("row", f"{len(deduped)}:{lead.source_url}")
            deduped[synthetic_key] = lead
            continue

        current = deduped.get(key)
        if current is None or lead.confidence_score > current.confidence_score:
            deduped[key] = lead

    return sorted(deduped.values(), key=lambda item: item.confidence_score, reverse=True)


def lead_dedupe_key(lead: Lead) -> tuple[str, str] | None:
    linkedin_url = normalize_url(lead.linkedin_url)
    if linkedin_url:
        return ("linkedin_url", linkedin_url)

    if lead.email:
        email = normalize_text(lead.email)
        if email:
            return ("email", email)

    if lead.person_name:
        person = normalize_text(lead.person_name)
        company = normalize_text(lead.company_name)
        if person and company:
            return ("person_company", f"{person}:{company}")

    source_url = normalize_source_url(lead.source_url)
    if source_url:
        return ("source_url", source_url)

    return None


def global_dedupe_key(lead: Lead) -> str | None:
    """Identity key used for *global* dedup across every saved table.

    Distinct from :func:`lead_dedupe_key` (which powers within-run / within-
    table dedup) because the product spec for cross-table dedup is explicit:

    1. Primary  → LinkedIn URL normalizada.
    2. Fallback → nome + empresa + cargo normalizados.

    The fallback intentionally includes ``title``: two people at the same
    company with different cargos are *different* leads and must not collide.
    """
    return compute_global_key(
        linkedin_url=lead.linkedin_url,
        person_name=lead.person_name,
        company_name=lead.company_name,
        title=lead.title,
        source_url=lead.source_url,
    )


def compute_global_key(
    *,
    linkedin_url: str | None,
    person_name: str | None,
    company_name: str | None,
    title: str | None,
    source_url: str | None = None,
) -> str | None:
    """Field-level variant of :func:`global_dedupe_key`.

    Lets storage compute the key straight from DB columns without building a
    full :class:`Lead`. Returns a stringified key so the result can live in a
    plain ``set[str]`` shared across the search pipeline.
    """
    url = normalize_url(linkedin_url)
    if url:
        return f"li:{url}"

    person = normalize_text(person_name or "")
    company = normalize_text(company_name or "")
    if person and company:
        cargo = normalize_text(title or "")
        return f"nct:{person}|{company}|{cargo}"

    src = normalize_source_url(source_url)
    if src:
        return f"src:{src}"

    return None
