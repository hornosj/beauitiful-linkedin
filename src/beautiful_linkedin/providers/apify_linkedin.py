from __future__ import annotations

import logging
from typing import Any

import httpx

from beautiful_linkedin.api_logging import log_http_error, log_transport_error
from beautiful_linkedin.cache.sqlite_cache import SqliteJsonCache
from beautiful_linkedin.models import CompanyInput, Lead
from beautiful_linkedin.processing.lead_extractor import match_target_title
from beautiful_linkedin.processing.normalizer import normalize_text
from beautiful_linkedin.processing.scorer import score_lead
from beautiful_linkedin.providers.lead_provider import LeadProvider

logger = logging.getLogger(__name__)


class ApifyLinkedInEmployeesProvider(LeadProvider):
    name = "apify_linkedin"
    endpoint = (
        "https://api.apify.com/v2/acts/"
        "harvestapi~linkedin-company-employees/run-sync-get-dataset-items"
    )
    profile_mode = "Short ($4 per 1k)"

    def __init__(
        self,
        api_key: str,
        timeout_seconds: float = 180.0,
        transport: httpx.BaseTransport | None = None,
        cache: SqliteJsonCache | None = None,
    ) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.cache = cache

    def find_leads(
        self,
        company: CompanyInput,
        max_results: int,
        include_uncertain: bool,
        search_depth: str = "standard",
        offset: int = 0,
    ) -> list[Lead]:
        if offset > 0:
            return []

        payload = self._payload(company, max_results)
        records = self._request(payload, company)
        if not records:
            logger.info(
                "Apify LinkedIn Actor: 0 registros para '%s'.", company.company_name
            )
        leads: list[Lead] = []
        for record in records:
            lead = self._record_to_lead(company, record, include_uncertain)
            if lead:
                leads.append(lead)
        return leads[:max_results]

    def _payload(self, company: CompanyInput, max_results: int) -> dict[str, Any]:
        company_identifier = company.linkedin_url or company.company_name
        return {
            "companies": [company_identifier],
            "profileScraperMode": self.profile_mode,
            "maxItems": max_results,
            "jobTitles": company.titles,
        }

    def _request(
        self, payload: dict[str, Any], company: CompanyInput
    ) -> list[dict[str, Any]]:
        cache_payload = {"cache_version": 1, "request": payload}
        if self.cache:
            cached = self.cache.get_json(self.name, cache_payload)
            if isinstance(cached, list):
                return cached

        context = f"empresa={company.company_name}"
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = client.post(
                    self.endpoint,
                    params={"token": self.api_key},
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            log_http_error(
                logger, provider="Apify LinkedIn Actor", error=exc, context=context
            )
            return []
        except (httpx.TransportError, ValueError) as exc:
            log_transport_error(
                logger,
                provider="Apify LinkedIn Actor",
                error=exc,
                endpoint=self.endpoint,
                context=context,
            )
            return []

        records = _extract_records(data)
        if self.cache:
            self.cache.set_json(self.name, cache_payload, records)
        return records

    def _record_to_lead(
        self,
        company: CompanyInput,
        record: dict[str, Any],
        include_uncertain: bool,
    ) -> Lead | None:
        linkedin_url = _first_string(record, ["linkedinUrl", "linkedin_url", "profileUrl"])
        person_name = _person_name(record)
        title = _current_title(record)
        company_name = _current_company_name(record) or company.company_name
        location = _location_text(record)
        headline = _first_string(record, ["headline", "summary", "about"])
        snippet = " | ".join(
            part
            for part in [title, company_name, location, headline]
            if isinstance(part, str) and part.strip()
        )
        matched_title = match_target_title(f"{title or ''} {headline or ''}", company.titles)
        if not include_uncertain and not matched_title:
            return None

        if not _has_company_evidence(company, company_name, record):
            return None

        lead = Lead(
            company_name=company.company_name,
            company_domain=company.company_domain,
            person_name=person_name,
            title=title or headline,
            linkedin_url=linkedin_url,
            email=_email(record),
            source_url=linkedin_url or f"apify_linkedin:{record.get('id', '')}",
            source_type="api_apify_linkedin",
            snippet=snippet or "Apify LinkedIn Company Employees Actor result",
            matched_title=matched_title,
            confidence_score=30,
        )
        return lead.model_copy(update={"confidence_score": score_lead(lead)})


def _extract_records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ["items", "data", "results"]:
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _first_string(record: dict[str, Any], keys: list[str]) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _person_name(record: dict[str, Any]) -> str | None:
    full_name = _first_string(record, ["fullName", "full_name", "name"])
    if full_name:
        return full_name
    first_name = _first_string(record, ["firstName", "first_name"]) or ""
    last_name = _first_string(record, ["lastName", "last_name"]) or ""
    name = f"{first_name} {last_name}".strip()
    return name or None


def _current_title(record: dict[str, Any]) -> str | None:
    for item in _list_values(record, ["currentPosition", "currentPositions", "experience"]):
        title = _dict_first_string(item, ["position", "title", "jobTitle"])
        if title:
            return title
    return _first_string(record, ["title", "jobTitle", "headline"])


def _current_company_name(record: dict[str, Any]) -> str | None:
    for item in _list_values(record, ["currentPosition", "currentPositions", "experience"]):
        company_name = _dict_first_string(item, ["companyName", "company", "organizationName"])
        if company_name:
            return company_name
    return _first_string(record, ["companyName", "currentCompany", "organizationName"])


def _location_text(record: dict[str, Any]) -> str | None:
    location = record.get("location")
    if isinstance(location, dict):
        for key in ["linkedinText", "text", "name"]:
            value = location.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(location, str) and location.strip():
        return location.strip()
    return None


def _email(record: dict[str, Any]) -> str | None:
    value = record.get("email")
    if isinstance(value, str) and value.strip():
        return value.strip()
    emails = record.get("emails")
    if isinstance(emails, list):
        for item in emails:
            if isinstance(item, str) and item.strip():
                return item.strip()
            if isinstance(item, dict):
                email = _dict_first_string(item, ["email", "emailAddress", "value"])
                if email:
                    return email
    return None


def _list_values(record: dict[str, Any], keys: list[str]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for key in keys:
        raw = record.get(key)
        if isinstance(raw, list):
            values.extend(item for item in raw if isinstance(item, dict))
        elif isinstance(raw, dict):
            values.append(raw)
    return values


def _dict_first_string(record: dict[str, Any], keys: list[str]) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _has_company_evidence(
    company: CompanyInput,
    record_company_name: str | None,
    record: dict[str, Any],
) -> bool:
    target_name = normalize_text(company.company_name)
    record_company = normalize_text(record_company_name)
    if target_name and target_name in record_company:
        return True

    linkedin_url = normalize_text(company.linkedin_url)
    for item in _list_values(record, ["currentPosition", "currentPositions", "experience"]):
        company_url = normalize_text(_dict_first_string(item, ["companyLinkedinUrl"]))
        if linkedin_url and company_url and linkedin_url.rstrip("/") == company_url.rstrip("/"):
            return True

    return False
