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
