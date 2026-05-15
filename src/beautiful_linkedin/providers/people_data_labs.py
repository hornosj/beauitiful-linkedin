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
from beautiful_linkedin.processing.normalizer import normalize_domain, normalize_text
from beautiful_linkedin.processing.scorer import score_lead
from beautiful_linkedin.processing.title_aliases import expand_deep_search_title_terms
from beautiful_linkedin.providers.lead_provider import LeadProvider

logger = logging.getLogger(__name__)


class PeopleDataLabsProvider(LeadProvider):
    name = "pdl"
    endpoint = "https://api.peopledatalabs.com/v5/person/search"
    company_enrich_endpoint = "https://api.peopledatalabs.com/v5/company/enrich"

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
        self._scroll_tokens: dict[str, str] = {}

    def find_leads(
        self,
        company: CompanyInput,
        max_results: int,
        include_uncertain: bool,
        search_depth: str = "standard",
        offset: int = 0,
    ) -> list[Lead]:
        diagnostic = self._new_diagnostic(company)
        pdl_company_id = (
            self._resolve_pdl_company_id(company, diagnostic)
            if self.prefer_by_company
            else None
        )
        if pdl_company_id:
            logger.info(
                "People Data Labs: usando job_company_id='%s' (resolvido via enrich)",
                pdl_company_id,
            )
        elif self.prefer_by_company:
            diagnostic.notes.append("enrich não resolveu pdl_company_id; usando job_company_name")
            logger.info("People Data Labs: usando job_company_name (resolução falhou)")
        payload = self._payload(company, max_results, offset, pdl_company_id)
        data = self._request(payload, company, diagnostic)
        scroll_token = data.get("scroll_token") if isinstance(data, dict) else None
        if offset == 0 and isinstance(scroll_token, str) and scroll_token:
            self._scroll_tokens[self._scroll_key(company)] = scroll_token
        records = data.get("data") if isinstance(data, dict) else []
        if not isinstance(records, list):
            records = []
        diagnostic.raw_records = len(records)
        if not records:
            logger.info(
                "People Data Labs: 0 registros para '%s' (offset=%d). "
                "Verifique cargo/empresa ou limite da API.",
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
        cache_payload = {"cache_version": 2, "request": payload}
        if self.cache:
            cached = self.cache.get_json(self.name, cache_payload)
            if isinstance(cached, dict):
                return cached

        headers = {"Content-Type": "application/json", "X-api-key": self.api_key}
        context = f"empresa={company.company_name}"
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self.transport,
                headers=headers,
            ) as client:
                response = client.get(self.endpoint, params=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            log_http_error(
                logger, provider="People Data Labs", error=exc, context=context
            )
            if diagnostic is not None:
                diagnostic.last_error = format_http_error_short("search", exc)
            return {}
        except (httpx.TransportError, ValueError) as exc:
            log_transport_error(
                logger,
                provider="People Data Labs",
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

    def _resolve_pdl_company_id(
        self,
        company: CompanyInput,
        diagnostic: ProviderDiagnostic | None = None,
    ) -> str | None:
        request = (
            {"website": company.company_domain}
            if company.company_domain
            else {"name": company.company_name}
        )
        cache_payload = {"cache_version": 1, "request": request}
        if self.cache:
            cached = self.cache.get_json("pdl_company", cache_payload)
            if isinstance(cached, str):
                return cached

        headers = {"Content-Type": "application/json", "X-api-key": self.api_key}
        context = f"empresa={company.company_name}"
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self.transport,
                headers=headers,
            ) as client:
                response = client.get(self.company_enrich_endpoint, params=request)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            log_http_error(
                logger, provider="People Data Labs", error=exc, context=context
            )
            if diagnostic is not None:
                diagnostic.last_error = format_http_error_short("enrich", exc)
            return None
        except (httpx.TransportError, ValueError) as exc:
            log_transport_error(
                logger,
                provider="People Data Labs",
                error=exc,
                endpoint=self.company_enrich_endpoint,
                context=context,
            )
            if diagnostic is not None:
                diagnostic.last_error = format_transport_error_short("enrich", exc)
            return None

        pdl_company_id = _pdl_company_id(data)
        if pdl_company_id and self.cache:
            self.cache.set_json("pdl_company", cache_payload, pdl_company_id)
        return pdl_company_id

    def _payload(
        self,
        company: CompanyInput,
        max_results: int,
        offset: int = 0,
        pdl_company_id: str | None = None,
    ) -> dict[str, Any]:
        title_terms = expand_deep_search_title_terms(company.titles)[:12]
        title_clause = " OR ".join(
            f"job_title LIKE '%{_sql_escape(term)}%'" for term in title_terms
        )
        if pdl_company_id:
            company_filter = f"job_company_id='{_sql_escape(pdl_company_id)}'"
        else:
            company_clause = f"job_company_name='{_sql_escape(company.company_name)}'"
            domain_clause = (
                f" OR job_company_website='{_sql_escape(company.company_domain)}'"
                if company.company_domain
                else ""
            )
            company_filter = f"({company_clause}{domain_clause})"
        sql = (
            "SELECT * FROM person WHERE "
            f"{company_filter}"
            f" AND ({title_clause})"
        )
        payload = {
            "sql": sql,
            "size": min(max_results, 100),
            "dataset": "all",
            "titlecase": True,
            "data_include": (
                "id,full_name,job_title,job_company_name,job_company_website,"
                "linkedin_url,profiles,summary,location_country"
            ),
        }
        if offset > 0:
            scroll_token = self._scroll_tokens.get(self._scroll_key(company))
            if scroll_token:
                payload["scroll_token"] = scroll_token
        return payload

    def _scroll_key(self, company: CompanyInput) -> str:
        return f"{company.company_name}|{company.company_domain or ''}|{','.join(company.titles)}"

    def _record_to_lead(
        self,
        company: CompanyInput,
        record: dict[str, Any],
        include_uncertain: bool,
        diagnostic: ProviderDiagnostic | None = None,
    ) -> Lead | None:
        person_name = _first_string(record, ["full_name", "name"])
        title = _first_string(record, ["job_title", "title"])
        record_company_name = _first_string(record, ["job_company_name"])
        company_name = record_company_name or company.company_name
        linkedin_url = _extract_linkedin_url(record)
        snippet = _first_string(record, ["summary"]) or "People Data Labs structured result"
        matched_title = match_target_title(title or snippet, company.titles)
        if not _has_company_evidence(
            company,
            record_company_name,
            _first_string(record, ["job_company_website"]),
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
            source_url=linkedin_url or f"pdl:{record.get('id', '')}",
            source_type="api_pdl",
            snippet=f"{title or ''} | {company_name} | {snippet}".strip(" |"),
            matched_title=matched_title,
            confidence_score=30,
        )
        return lead.model_copy(update={"confidence_score": score_lead(lead)})


def _first_string(record: dict[str, Any], keys: list[str]) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_linkedin_url(record: dict[str, Any]) -> str | None:
    direct = _first_string(record, ["linkedin_url"])
    if direct:
        return direct
    profiles = record.get("profiles")
    if isinstance(profiles, list):
        for profile in profiles:
            if isinstance(profile, dict):
                url = str(profile.get("url") or "").strip()
                network = str(profile.get("network") or "").lower()
                if "linkedin" in network or "linkedin.com/in" in url:
                    return url
            elif isinstance(profile, str) and "linkedin.com/in" in profile:
                return profile
    return None


def _sql_escape(value: str) -> str:
    return value.replace("'", "''")


def _pdl_company_id(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    source = data.get("data") if isinstance(data.get("data"), dict) else data
    value = source.get("id") if isinstance(source, dict) else None
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
