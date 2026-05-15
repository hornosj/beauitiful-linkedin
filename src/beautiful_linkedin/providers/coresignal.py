from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote, urlparse

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
from beautiful_linkedin.processing.normalizer import normalize_domain, normalize_text
from beautiful_linkedin.processing.scorer import score_lead
from beautiful_linkedin.processing.title_aliases import expand_deep_search_title_terms
from beautiful_linkedin.providers.lead_provider import LeadProvider

logger = logging.getLogger(__name__)


class CoresignalEmployeeProvider(LeadProvider):
    name = "coresignal"
    endpoint = "https://api.coresignal.com/cdapi/v2/employee_multi_source/search/es_dsl/preview"
    company_collect_base = "https://api.coresignal.com/cdapi/v2/company_multi_source/collect"
    employee_by_company_base = "https://api.coresignal.com/cdapi/v2/employee_multi_source/by_company"

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
        company_id = (
            self._resolve_coresignal_company_id(company, diagnostic)
            if self.prefer_by_company
            else None
        )
        if company_id:
            logger.info(
                "Coresignal: usando by_company/%s (resolvido via collect)",
                company_id,
            )
            records = self._request_by_company(
                company_id, max_results, offset, company, diagnostic
            )
        else:
            if self.prefer_by_company:
                diagnostic.notes.append("collect não resolveu company_id; usando search preview (DSL)")
            logger.info("Coresignal: usando search preview (resolução falhou)")
            payload = self._payload(company, max_results, offset)
            records = self._request(payload, max_results, offset, company, diagnostic)
        diagnostic.raw_records = len(records)
        if not records:
            logger.info(
                "Coresignal: 0 registros para '%s' (offset=%d).",
                company.company_name, offset,
            )
        leads: list[Lead] = []
        for record in records[:max_results]:
            lead = self._record_to_lead(company, record, include_uncertain, diagnostic)
            if lead:
                leads.append(lead)
        diagnostic.leads_returned = len(leads)
        return leads

    def _request(
        self,
        payload: dict[str, Any],
        max_results: int,
        offset: int,
        company: CompanyInput,
        diagnostic: ProviderDiagnostic | None = None,
    ) -> list[dict[str, Any]]:
        page = max(1, offset // max(max_results, 1) + 1)
        cache_payload = {
            "cache_version": 2,
            "payload": payload,
            "max_results": max_results,
            "page": page,
        }
        if self.cache:
            cached = self.cache.get_json(self.name, cache_payload)
            if isinstance(cached, list):
                return cached

        headers = {
            "accept": "application/json",
            "apikey": self.api_key,
            "Content-Type": "application/json",
        }
        context = f"empresa={company.company_name}, page={page}"
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self.transport,
                headers=headers,
            ) as client:
                response = client.post(self.endpoint, params={"page": page}, json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            log_http_error(logger, provider="Coresignal", error=exc, context=context)
            if diagnostic is not None:
                diagnostic.last_error = format_http_error_short("search", exc)
            return []
        except (httpx.TransportError, ValueError) as exc:
            log_transport_error(
                logger,
                provider="Coresignal",
                error=exc,
                endpoint=self.endpoint,
                context=context,
            )
            if diagnostic is not None:
                diagnostic.last_error = format_transport_error_short("search", exc)
            return []

        records = _extract_records(data)
        if self.cache:
            self.cache.set_json(self.name, cache_payload, records)
        return records

    def _resolve_coresignal_company_id(
        self,
        company: CompanyInput,
        diagnostic: ProviderDiagnostic | None = None,
    ) -> str | None:
        identifier = _coresignal_company_identifier(company)
        cache_payload = {"cache_version": 1, "request": {"identifier": identifier}}
        if self.cache:
            cached = self.cache.get_json("coresignal_company", cache_payload)
            if isinstance(cached, str):
                return cached

        headers = {"accept": "application/json", "apikey": self.api_key}
        endpoint = f"{self.company_collect_base}/{quote(identifier, safe='')}"
        context = f"empresa={company.company_name}"
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self.transport,
                headers=headers,
            ) as client:
                response = client.get(endpoint)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            log_http_error(logger, provider="Coresignal", error=exc, context=context)
            if diagnostic is not None:
                diagnostic.last_error = format_http_error_short("collect", exc)
            return None
        except (httpx.TransportError, ValueError) as exc:
            log_transport_error(
                logger,
                provider="Coresignal",
                error=exc,
                endpoint=endpoint,
                context=context,
            )
            if diagnostic is not None:
                diagnostic.last_error = format_transport_error_short("collect", exc)
            return None

        company_id = _coresignal_company_id(data)
        if company_id and self.cache:
            self.cache.set_json("coresignal_company", cache_payload, company_id)
        return company_id

    def _request_by_company(
        self,
        company_id: str,
        max_results: int,
        offset: int,
        company: CompanyInput,
        diagnostic: ProviderDiagnostic | None = None,
    ) -> list[dict[str, Any]]:
        page = max(1, offset // max(max_results, 1) + 1)
        endpoint = f"{self.employee_by_company_base}/{quote(company_id, safe='')}"
        cache_payload = {
            "cache_version": 1,
            "company_id": company_id,
            "max_results": max_results,
            "page": page,
        }
        if self.cache:
            cached = self.cache.get_json(self.name, cache_payload)
            if isinstance(cached, list):
                return cached

        headers = {"accept": "application/json", "apikey": self.api_key}
        context = f"empresa={company.company_name}, page={page}"
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self.transport,
                headers=headers,
            ) as client:
                response = client.get(endpoint, params={"page": page})
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            log_http_error(logger, provider="Coresignal", error=exc, context=context)
            if diagnostic is not None:
                diagnostic.last_error = format_http_error_short("by_company", exc)
            return []
        except (httpx.TransportError, ValueError) as exc:
            log_transport_error(
                logger,
                provider="Coresignal",
                error=exc,
                endpoint=endpoint,
                context=context,
            )
            if diagnostic is not None:
                diagnostic.last_error = format_transport_error_short("by_company", exc)
            return []

        records = _extract_records(data)
        if self.cache:
            self.cache.set_json(self.name, cache_payload, records)
        return records

    def _payload(
        self,
        company: CompanyInput,
        max_results: int,
        offset: int = 0,
    ) -> dict[str, Any]:
        title_query = " OR ".join(f'"{term}"' for term in expand_deep_search_title_terms(company.titles)[:10])
        company_query = f'"{company.company_name}"'
        if company.company_domain:
            company_query = f'{company_query} OR "{company.company_domain}"'

        must = [
            {
                "query_string": {
                    "query": company_query,
                    "fields": ["company_name", "company_website", "headline", "summary"],
                    "default_operator": "and",
                }
            },
            {
                "query_string": {
                    "query": title_query,
                    "fields": ["active_experience_title", "headline", "summary"],
                    "default_operator": "or",
                }
            },
        ]
        return {"query": {"bool": {"must": must}}}

    def _record_to_lead(
        self,
        company: CompanyInput,
        record: dict[str, Any],
        include_uncertain: bool,
        diagnostic: ProviderDiagnostic | None = None,
    ) -> Lead | None:
        person_name = _first_string(record, ["full_name"])
        title = _first_string(record, ["active_experience_title", "job_title", "headline"])
        record_company_name = _first_string(record, ["company_name"])
        company_name = record_company_name or company.company_name
        linkedin_url = _first_string(
            record,
            ["professional_network_url", "websites_professional_network", "profile_url"],
        )
        snippet = _first_string(record, ["headline"]) or "Coresignal structured employee result"
        matched_title = match_target_title(title or snippet, company.titles)
        if not _has_company_evidence(
            company,
            record_company_name,
            _first_string(record, ["company_website"]),
            snippet,
        ):
            if diagnostic is not None:
                diagnostic.dropped_company_evidence += 1
            return None
        if not include_uncertain and not matched_title:
            if diagnostic is not None:
                diagnostic.dropped_title_filter += 1
            return None

        lead = Lead(
            company_name=company.company_name,
            company_domain=company.company_domain,
            person_name=person_name,
            title=title,
            linkedin_url=linkedin_url,
            source_url=linkedin_url or f"coresignal:{record.get('id', '')}",
            source_type="api_coresignal",
            snippet=f"{title or ''} | {company_name} | {snippet}".strip(" |"),
            matched_title=matched_title,
            confidence_score=30,
        )
        return lead.model_copy(update={"confidence_score": score_lead(lead)})


def _extract_records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ["data", "results", "items"]:
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


def _has_company_evidence(
    company: CompanyInput,
    record_company_name: str | None,
    record_company_website: str | None,
    snippet: str | None,
) -> bool:
    target_name = normalize_text(company.company_name)
    simplified_target = normalize_text(company.company_name.replace("Banco", "").strip())
    record_name = normalize_text(record_company_name)
    if target_name and target_name in record_name:
        return True
    if simplified_target and simplified_target in record_name:
        return True

    target_domain = normalize_domain(company.company_domain)
    record_domain = normalize_domain(record_company_website)
    if target_domain and target_domain == record_domain:
        return True

    normalized_snippet = normalize_text(snippet)
    return bool(
        (target_name and target_name in normalized_snippet)
        or (simplified_target and simplified_target in normalized_snippet)
    )


def _coresignal_company_identifier(company: CompanyInput) -> str:
    if company.linkedin_url:
        parsed = urlparse(company.linkedin_url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] == "company":
            return parts[1]
    if company.company_domain:
        return company.company_domain
    return re.sub(r"[^a-z0-9-]+", "-", normalize_text(company.company_name)).strip("-")


def _coresignal_company_id(data: Any) -> str | None:
    if isinstance(data, list):
        for item in data:
            value = _coresignal_company_id(item)
            if value:
                return value
        return None
    if not isinstance(data, dict):
        return None
    source = data.get("data") if isinstance(data.get("data"), dict) else data
    value = source.get("id") if isinstance(source, dict) else None
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value).strip()
    return None
