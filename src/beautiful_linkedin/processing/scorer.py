from __future__ import annotations

from beautiful_linkedin.models import Lead
from beautiful_linkedin.processing.normalizer import contains_linkedin_profile_url, normalize_text


def score_lead(lead: Lead) -> int:
    score = 30

    if contains_linkedin_profile_url(lead.linkedin_url or lead.source_url):
        score += 25

    if lead.matched_title:
        score += 20

    company = normalize_text(lead.company_name)
    searchable_text = normalize_text(f"{lead.title or ''} {lead.snippet or ''}")
    if company and company in searchable_text:
        score += 15

    if lead.person_name:
        score += 10

    if lead.source_type == "company_site":
        score += 10

    if lead.source_type.startswith("api_"):
        score += 15

    if lead.validation_status == "maybe_incorrect":
        score -= 25

    return max(0, min(score, 100))
