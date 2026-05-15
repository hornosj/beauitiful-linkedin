from __future__ import annotations

import logging
from typing import Any

import httpx

from beautiful_linkedin.api_logging import (
    format_http_error_short,
    format_transport_error_short,
    log_http_error,
    log_transport_error,
)
from beautiful_linkedin.cache.sqlite_cache import SqliteJsonCache
from beautiful_linkedin.models import CompanyInput, Lead, ProviderDiagnostic
from beautiful_linkedin.processing.lead_extractor import match_target_title
from beautiful_linkedin.processing.scorer import score_lead
from beautiful_linkedin.providers.lead_provider import LeadProvider

logger = logging.getLogger(__name__)


class LushaProvider(LeadProvider):
    name = "lusha"
    endpoint = "https://api.lusha.com/person/search"

    def __init__(
        self,
        api_key: str,
        timeout_seconds: float = 20.0,
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
        diagnostic = self._new_diagnostic(company)
        if not company.company_domain:
            diagnostic.notes.append(
                "sem domain: campo `filters.company` recebe o nome — Lusha pode não casar"
            )
        payload = self._payload(company, max_results, offset)
        data = self._request(payload, company, diagnostic)

        records = data.get("data") if isinstance(data, dict) else []
        if not isinstance(records, list):
            records = []
        diagnostic.raw_records = len(records)
        if not records:
            logger.info(
                "Lusha: 0 registros para '%s' (offset=%d).",
                company.company_name, offset,
            )

        leads: list[Lead] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            lead = self._record_to_lead(company, record, include_uncertain, diagnostic)
            if lead:
                leads.append(lead)
        diagnostic.leads_returned = len(leads)
        return leads

    def _request(
        self,
        payload: dict[str, Any],
        company: CompanyInput,
        diagnostic: ProviderDiagnostic | None = None,
    ) -> dict[str, Any]:
        cache_payload = {"cache_version": 1, "request": payload}
        if self.cache:
            cached = self.cache.get_json(self.name, cache_payload)
            if isinstance(cached, dict):
                return cached

        headers = {
            "Content-Type": "application/json",
            "api_key": self.api_key,
        }
        context = f"empresa={company.company_name}"

        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self.transport,
                headers=headers,
            ) as client:
                response = client.post(self.endpoint, json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            log_http_error(logger, provider="Lusha", error=exc, context=context)
            if diagnostic is not None:
                diagnostic.last_error = format_http_error_short("search", exc)
            return {}
        except (httpx.TransportError, ValueError) as exc:
            log_transport_error(
                logger,
                provider="Lusha",
                error=exc,
                endpoint=self.endpoint,
                context=context,
            )
            if diagnostic is not None:
                diagnostic.last_error = format_transport_error_short("search", exc)
            return {}

        if self.cache:
            self.cache.set_json(self.name, cache_payload, data)
        return data

    def _payload(
        self,
        company: CompanyInput,
        max_results: int,
        offset: int = 0,
    ) -> dict[str, Any]:
        # Using pagination based on page parameters
        page = (offset // max_results) + 1 if max_results > 0 else 1
        
        # TODO: Adjust lusha search parameters if needed based on real API docs.
        # This is a safe assumption for the payload structure.
        return {
            "filters": {
                "company": company.company_domain or company.company_name,
                "jobTitle": company.titles,
            },
            "limit": min(max_results, 100),
            "page": page,
        }

    def _record_to_lead(
        self,
        company: CompanyInput,
        record: dict[str, Any],
        include_uncertain: bool,
        diagnostic: ProviderDiagnostic | None = None,
    ) -> Lead | None:
        first_name = record.get("firstName") or ""
        last_name = record.get("lastName") or ""
        person_name = f"{first_name} {last_name}".strip()

        title = record.get("jobTitle")
        linkedin_url = record.get("linkedinUrl")
        
        # Try to parse emails if available 
        # (Lusha might return an array or object depending on access level)
        email_data = record.get("email") or record.get("emails")
        email = None
        if isinstance(email_data, list) and email_data:
            # Assuming first element might be an object like {"email": "..."} or a string
            if isinstance(email_data[0], dict):
                email = email_data[0].get("emailAddress") or email_data[0].get("email")
            elif isinstance(email_data[0], str):
                email = email_data[0]
        elif isinstance(email_data, str):
            email = email_data

        record_company = record.get("company") or {}
        company_name = record_company.get("name") if isinstance(record_company, dict) else company.company_name
        if not company_name:
            company_name = company.company_name

        snippet = "Lusha structured result"
        matched_title = match_target_title(title or snippet, company.titles)

        if not include_uncertain and not matched_title:
            if diagnostic is not None:
                diagnostic.dropped_title_filter += 1
            return None

        # Determine source URL
        source_url = linkedin_url or f"lusha:{record.get('hashedPersonId', record.get('id', ''))}"
        
        lead = Lead(
            company_name=company.company_name,
            company_domain=company.company_domain,
            person_name=person_name,
            title=title,
            linkedin_url=linkedin_url,
            email=email,
            source_url=source_url,
            source_type="api_lusha",
            snippet=f"{title or ''} | {company_name} | {snippet}".strip(" |"),
            matched_title=matched_title,
            confidence_score=30,
        )
        return lead.model_copy(update={"confidence_score": score_lead(lead)})
