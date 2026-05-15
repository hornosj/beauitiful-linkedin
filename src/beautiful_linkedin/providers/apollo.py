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


class ApolloProvider(LeadProvider):
    name = "apollo"
    endpoint = "https://api.apollo.io/api/v1/people/search"
    organization_enrich_endpoint = "https://api.apollo.io/api/v1/organizations/enrich"

    def __init__(
        self,
        api_key: str,
        timeout_seconds: float = 20.0,
        transport: httpx.BaseTransport | None = None,
        cache: SqliteJsonCache | None = None,
        prefer_by_company: bool = True,
    ) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.cache = cache
        self.prefer_by_company = prefer_by_company

    def find_leads(
        self,
        company: CompanyInput,
        max_results: int,
        include_uncertain: bool,
        search_depth: str = "standard",
        offset: int = 0,
    ) -> list[Lead]:
        diagnostic = self._new_diagnostic(company)
        organization_id = (
            self._resolve_organization_id(company, diagnostic)
            if self.prefer_by_company
            else None
        )
        if organization_id:
            logger.info(
                "Apollo: usando organization_ids=[%s] (resolvido via enrich)",
                organization_id,
            )
        elif self.prefer_by_company:
            note = (
                "enrich não resolveu organization_id; sem domain, busca por nome "
                "tende a retornar 0 (Apollo espera domínio em q_organization_domains)"
                if not company.company_domain
                else "enrich não resolveu organization_id; usando q_organization_domains com o domain"
            )
            diagnostic.notes.append(note)
            logger.info("Apollo: %s", note)
        payload = self._payload(company, max_results, offset, organization_id)
        if "q_organization_domains" in payload and not company.company_domain:
            diagnostic.notes.append(
                "fallback inseguro: q_organization_domains preenchido com o nome da empresa"
            )
        data = self._request(payload, company, diagnostic)

        records = data.get("people") if isinstance(data, dict) else []
        if not isinstance(records, list):
            records = []
        diagnostic.raw_records = len(records)
        if not records:
            logger.info(
                "Apollo: 0 registros para '%s' (offset=%d).",
                company.company_name, offset,
            )

        leads: list[Lead] = []
        for record in records:
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
            "Cache-Control": "no-cache",
        }

        req_payload = dict(payload)
        req_payload["api_key"] = self.api_key
        context = f"empresa={company.company_name}"

        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self.transport,
                headers=headers,
            ) as client:
                response = client.post(self.endpoint, json=req_payload)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            log_http_error(logger, provider="Apollo", error=exc, context=context)
            if diagnostic is not None:
                diagnostic.last_error = format_http_error_short("search", exc)
            return {}
        except (httpx.TransportError, ValueError) as exc:
            log_transport_error(
                logger,
                provider="Apollo",
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

    def _resolve_organization_id(
        self,
        company: CompanyInput,
        diagnostic: ProviderDiagnostic | None = None,
    ) -> str | None:
        request = (
            {"domain": company.company_domain}
            if company.company_domain
            else {"name": company.company_name}
        )
        cache_payload = {"cache_version": 1, "request": request}
        if self.cache:
            cached = self.cache.get_json("apollo_organization", cache_payload)
            if isinstance(cached, str):
                return cached
            if cached is None:
                pass

        headers = {"Cache-Control": "no-cache", "x-api-key": self.api_key}
        context = f"empresa={company.company_name}"
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self.transport,
                headers=headers,
            ) as client:
                response = client.get(self.organization_enrich_endpoint, params=request)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            log_http_error(logger, provider="Apollo", error=exc, context=context)
            if diagnostic is not None:
                diagnostic.last_error = format_http_error_short("enrich", exc)
            return None
        except (httpx.TransportError, ValueError) as exc:
            log_transport_error(
                logger,
                provider="Apollo",
                error=exc,
                endpoint=self.organization_enrich_endpoint,
                context=context,
            )
            if diagnostic is not None:
                diagnostic.last_error = format_transport_error_short("enrich", exc)
            return None

        organization_id = _apollo_organization_id(data)
        if organization_id and self.cache:
            self.cache.set_json("apollo_organization", cache_payload, organization_id)
        return organization_id

    def _payload(
        self,
        company: CompanyInput,
        max_results: int,
        offset: int = 0,
        organization_id: str | None = None,
    ) -> dict[str, Any]:
        page = (offset // max_results) + 1 if max_results > 0 else 1
        payload: dict[str, Any] = {
            "person_titles": company.titles,
            "page": page,
            "per_page": min(max_results, 100),
        }
        if organization_id:
            payload["organization_ids"] = [organization_id]
        else:
            payload["q_organization_domains"] = company.company_domain or company.company_name
        return payload

    def _record_to_lead(
        self,
        company: CompanyInput,
        record: dict[str, Any],
        include_uncertain: bool,
        diagnostic: ProviderDiagnostic | None = None,
    ) -> Lead | None:
        person_name = record.get("name")
        title = record.get("title")
        linkedin_url = record.get("linkedin_url")
        email = record.get("email")

        organization = record.get("organization") or {}
        record_company_name = organization.get("name")
        company_name = record_company_name or company.company_name

        snippet = "Apollo.io structured result"
        matched_title = match_target_title(title or snippet, company.titles)

        if not include_uncertain and not matched_title:
            if diagnostic is not None:
                diagnostic.dropped_title_filter += 1
            return None

        # Determine source URL
        source_url = linkedin_url or f"apollo:{record.get('id', '')}"
        
        lead = Lead(
            company_name=company.company_name,
            company_domain=company.company_domain,
            person_name=person_name,
            title=title,
            linkedin_url=linkedin_url,
            email=email,
            source_url=source_url,
            source_type="api_apollo",
            snippet=f"{title or ''} | {company_name} | {snippet}".strip(" |"),
            matched_title=matched_title,
            confidence_score=30,
        )
        return lead.model_copy(update={"confidence_score": score_lead(lead)})


def _apollo_organization_id(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    organization = data.get("organization")
    if isinstance(organization, dict):
        value = organization.get("id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    value = data.get("id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
