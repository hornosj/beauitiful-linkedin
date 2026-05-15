from __future__ import annotations

import logging
import re
from typing import Any, Literal
from urllib.parse import quote

import httpx

from beautiful_linkedin.cookie_resolver import resolve_linkedin_li_at_cookie
from beautiful_linkedin.models import CompanyInput, Lead
from beautiful_linkedin.processing.lead_extractor import match_target_title
from beautiful_linkedin.processing.scorer import score_lead
from beautiful_linkedin.providers.lead_provider import LeadProvider
from beautiful_linkedin.providers.linkedin_cookie import (
    LINKEDIN_BASE,
    CompanyInfo,
    LinkedInCookieSession,
    _browser_user_agent,
    _first_string,
)

logger = logging.getLogger(__name__)

SALES_NAV_BASE = f"{LINKEDIN_BASE}/sales-api"
SALES_PEOPLE_SEARCH = f"{SALES_NAV_BASE}/salesApiPeopleSearch"
SALES_RESULTS_PER_PAGE = 25
SALES_MAX_PAGES = 40
SALES_LOCAL_TITLE_MATCH_MAX_PAGES = 100


class LinkedInSalesNavigatorProvider(LeadProvider):
    """Search company employees using LinkedIn Sales Navigator endpoints.

    Requires the cookie of an account with an active Sales Navigator subscription.
    If Sales Navigator endpoints are unavailable (account does not have access,
    schema changed, rate limit), falls back to the regular voyager search via
    LinkedInCookieEmployeesProvider with deeper pagination.
    """

    name = "linkedin_sales_navigator"

    def __init__(
        self,
        cookie: str | None = None,
        cookie_browser: str = "auto",
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        title_match_mode: Literal["api", "local"] = "local",
    ) -> None:
        self.cookie = cookie
        self.cookie_browser = cookie_browser
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.title_match_mode = title_match_mode

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

        li_at = resolve_linkedin_li_at_cookie(self.cookie, browser=self.cookie_browser)
        if not li_at:
            logger.warning(
                "Cookie li_at não encontrado. Faça login no LinkedIn (com Sales Navigator) "
                "no navegador ou configure LINKEDIN_LI_AT_COOKIE."
            )
            return []

        try:
            session = SalesNavSession(
                li_at=li_at,
                timeout_seconds=self.timeout_seconds,
                transport=self.transport,
            )
            session.initialize()
            company_info = session.resolve_company(company)
        except Exception as exc:
            logger.warning(
                "Sales Navigator: não foi possível inicializar sessão para %s. %s",
                company.company_name, exc,
            )
            return _fallback_voyager_search(
                company=company,
                max_results=max_results,
                include_uncertain=include_uncertain,
                cookie=self.cookie,
                cookie_browser=self.cookie_browser,
                timeout_seconds=self.timeout_seconds,
                transport=self.transport,
                reason=f"erro de inicialização: {exc}",
            )

        try:
            records = session.search_employees_via_sales_nav(
                company_info,
                company.titles,
                max_results,
                title_match_mode=self.title_match_mode,
            )
        except SalesNavUnavailableError as exc:
            logger.warning(
                "Sales Navigator não disponível para esta conta (%s). "
                "Caindo para busca regular do LinkedIn com paginação ampliada.",
                exc,
            )
            return _fallback_voyager_search(
                company=company,
                max_results=max_results,
                include_uncertain=include_uncertain,
                cookie=self.cookie,
                cookie_browser=self.cookie_browser,
                timeout_seconds=self.timeout_seconds,
                transport=self.transport,
                reason=str(exc),
            )
        except Exception as exc:
            logger.warning(
                "Sales Navigator falhou para %s. Caindo para busca regular. %s",
                company.company_name, exc,
            )
            return _fallback_voyager_search(
                company=company,
                max_results=max_results,
                include_uncertain=include_uncertain,
                cookie=self.cookie,
                cookie_browser=self.cookie_browser,
                timeout_seconds=self.timeout_seconds,
                transport=self.transport,
                reason=str(exc),
            )

        if not records:
            logger.info(
                "Sales Navigator não retornou resultados para %s. Tentando busca regular.",
                company.company_name,
            )
            return _fallback_voyager_search(
                company=company,
                max_results=max_results,
                include_uncertain=include_uncertain,
                cookie=self.cookie,
                cookie_browser=self.cookie_browser,
                timeout_seconds=self.timeout_seconds,
                transport=self.transport,
                reason="resposta vazia",
            )

        leads: list[Lead] = []
        for record in records:
            lead = _record_to_lead(company, record, include_uncertain)
            if lead:
                leads.append(lead)
        logger.info(
            "LinkedIn cookie: pulled %d funcionários totais para %s, %d passaram filtro local de cargo",
            len(records),
            company.company_name,
            len(leads),
        )
        return leads[:max_results]


class SalesNavUnavailableError(RuntimeError):
    pass


class SalesNavSession(LinkedInCookieSession):
    """Extends voyager session with Sales Navigator API calls."""

    def search_employees_via_sales_nav(
        self,
        company: CompanyInfo,
        titles: list[str],
        max_results: int,
        title_match_mode: Literal["api", "local"] = "local",
    ) -> list[dict[str, Any]]:
        if not company.company_id:
            logger.warning(
                "Sales Navigator: company_id não resolvido para %s.", company.name
            )
            raise SalesNavUnavailableError("company_id ausente")

        if title_match_mode == "local":
            return self._search_company_employees_via_sales_nav(company)
        return self._search_employees_by_api_title(company, titles, max_results)

    def _search_company_employees_via_sales_nav(
        self, company: CompanyInfo
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        page = 0
        while page < SALES_LOCAL_TITLE_MATCH_MAX_PAGES:
            start = page * SALES_RESULTS_PER_PAGE
            page_records = self._sales_search_page(company.company_id or "", "", start)
            if not page_records:
                break

            added = 0
            for record in page_records:
                url = record.get("linkedinUrl") or ""
                if url and url in seen_urls:
                    continue
                record["companyName"] = record.get("companyName") or company.name
                records.append(record)
                if url:
                    seen_urls.add(url)
                added += 1

            if added == 0:
                break
            page += 1

        return records

    def _search_employees_by_api_title(
        self,
        company: CompanyInfo,
        titles: list[str],
        max_results: int,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        title_terms = titles or [""]

        for title in title_terms:
            page = 0
            while len(records) < max_results and page < SALES_MAX_PAGES:
                start = page * SALES_RESULTS_PER_PAGE
                page_records = self._sales_search_page(
                    company.company_id, title, start
                )
                if not page_records:
                    break

                added = 0
                for record in page_records:
                    url = record.get("linkedinUrl") or ""
                    if url and url in seen_urls:
                        continue
                    record["companyName"] = (
                        record.get("companyName") or company.name
                    )
                    records.append(record)
                    if url:
                        seen_urls.add(url)
                    added += 1
                    if len(records) >= max_results:
                        break

                if added == 0:
                    break
                page += 1

        return records

    def _sales_search_page(
        self, company_id: str, title: str, start: int
    ) -> list[dict[str, Any]]:
        company_urn = f"urn:li:organization:{company_id}"
        encoded_company_urn = quote(company_urn, safe="")
        filters = [
            "(type:CURRENT_COMPANY,values:List("
            f"(id:{encoded_company_urn},selectionType:INCLUDED)))"
        ]
        if title and title.strip():
            escaped_title = _escape_sales_nav_value(title.strip())
            filters.append(
                f"(type:CURRENT_TITLE,values:List((text:{escaped_title},selectionType:INCLUDED)))"
            )

        query_param = f"(filters:List({','.join(filters)}))"
        params = {
            "q": "peopleSearchQuery",
            "start": str(start),
            "count": str(SALES_RESULTS_PER_PAGE),
            "query": query_param,
        }

        with self._client() as client:
            response = client.get(
                SALES_PEOPLE_SEARCH,
                params=params,
                headers=self._sales_headers(),
            )

        if response.status_code in {401, 403}:
            raise SalesNavUnavailableError(
                "cookie sem permissão para Sales Navigator (HTTP "
                f"{response.status_code})"
            )
        if response.status_code == 404:
            raise SalesNavUnavailableError(
                "endpoint Sales Navigator não disponível (HTTP 404)"
            )
        if response.status_code == 429:
            raise SalesNavUnavailableError(
                "rate limit no Sales Navigator (HTTP 429)"
            )
        response.raise_for_status()

        try:
            data = response.json()
        except ValueError as exc:
            raise SalesNavUnavailableError(
                f"resposta inválida do Sales Navigator: {exc}"
            ) from exc

        return _profiles_from_sales_response(data)

    def _sales_headers(self) -> dict[str, str]:
        return {
            "accept": "application/vnd.linkedin.normalized+json+2.1",
            "accept-language": "en-US,en;q=0.9",
            "csrf-token": self.csrf_token,
            "cookie": self.cookies,
            "user-agent": _browser_user_agent(),
            "x-li-lang": "en_US",
            "x-restli-protocol-version": "2.0.0",
            "referer": f"{LINKEDIN_BASE}/sales/search/people",
        }


def _profiles_from_sales_response(data: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []

    candidates: list[dict[str, Any]] = []
    elements = data.get("elements")
    if isinstance(elements, list):
        for element in elements:
            if isinstance(element, dict):
                candidates.append(element)

    included = data.get("included")
    if isinstance(included, list):
        for element in included:
            if isinstance(element, dict):
                candidates.append(element)

    profiles: list[dict[str, Any]] = []
    for element in candidates:
        profile = _profile_from_sales_element(element)
        if profile:
            profiles.append(profile)

    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for profile in profiles:
        key = profile.get("linkedinUrl") or profile.get("publicIdentifier")
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(profile)
    return deduped


def _profile_from_sales_element(element: dict[str, Any]) -> dict[str, Any] | None:
    entity_urn = _first_string(element, ["entityUrn", "objectUrn", "urn"])
    public_id = _public_id_from_sales_urn(entity_urn) if entity_urn else None
    if not public_id:
        public_id = _first_string(element, ["publicIdentifier", "memberIdentity"])
    if not public_id:
        return None

    name = _name_from_element(element)
    if not name:
        return None

    headline = _first_string(element, ["headline", "summary"]) or ""
    title = (
        _first_string(
            element,
            [
                "currentPositionTitle",
                "currentPositionRoleTitle",
                "title",
                "jobTitle",
            ],
        )
        or _title_from_headline(headline)
    )
    company_name = _first_string(
        element,
        ["companyName", "currentPositionCompanyName", "currentCompanyName"],
    ) or ""
    location = _first_string(element, ["geoRegion", "location", "locationName"]) or ""

    return {
        "publicIdentifier": public_id,
        "linkedinUrl": f"{LINKEDIN_BASE}/in/{public_id}/",
        "name": name,
        "headline": headline,
        "title": title,
        "companyName": company_name,
        "location": location,
    }


def _public_id_from_sales_urn(urn: str) -> str | None:
    match = re.search(r"salesProfile:\(([^,)]+)", urn)
    if match:
        return match.group(1).strip()
    match = re.search(r"member:\(([^,)]+)", urn)
    if match:
        return match.group(1).strip()
    return None


def _name_from_element(element: dict[str, Any]) -> str | None:
    full_name = _first_string(element, ["fullName", "name"])
    if full_name:
        return full_name
    first = _first_string(element, ["firstName"]) or ""
    last = _first_string(element, ["lastName"]) or ""
    combined = f"{first} {last}".strip()
    return combined or None


def _title_from_headline(headline: str) -> str | None:
    cleaned = headline.strip()
    lowered = cleaned.lower()
    for separator in [" at ", " na ", " no ", " em "]:
        index = lowered.find(separator)
        if index > 0:
            return cleaned[:index].strip()
    return cleaned or None


def _escape_sales_nav_value(value: str) -> str:
    return value.replace(",", " ").replace("(", " ").replace(")", " ").strip()


def _record_to_lead(
    company: CompanyInput,
    record: dict[str, Any],
    include_uncertain: bool,
) -> Lead | None:
    person_name = record.get("name")
    title = record.get("title") or None
    headline = record.get("headline") or ""
    linkedin_url = record.get("linkedinUrl")
    company_name = record.get("companyName") or company.company_name
    matched_title = match_target_title(
        f"{title or ''} {headline}", company.titles
    )
    if not include_uncertain and not matched_title:
        return None

    lead = Lead(
        company_name=company.company_name,
        company_domain=company.company_domain,
        person_name=person_name,
        title=title or headline or None,
        linkedin_url=linkedin_url,
        source_url=linkedin_url or "linkedin_sales_navigator:search",
        source_type="linkedin_sales_navigator",
        snippet=" | ".join(
            part for part in [title, company_name, headline] if part
        ),
        matched_title=matched_title,
        confidence_score=35,
    )
    return lead.model_copy(update={"confidence_score": score_lead(lead)})


def _fallback_voyager_search(
    company: CompanyInput,
    max_results: int,
    include_uncertain: bool,
    cookie: str | None,
    cookie_browser: str,
    timeout_seconds: float,
    transport: httpx.BaseTransport | None,
    reason: str,
) -> list[Lead]:
    """Fall back to the regular voyager-based provider with deeper pagination."""
    from beautiful_linkedin.providers.linkedin_cookie import (
        LinkedInCookieEmployeesProvider,
    )

    logger.info(
        "Sales Navigator indisponível (%s). Usando busca padrão do LinkedIn (até 20 páginas).",
        reason,
    )
    fallback = LinkedInCookieEmployeesProvider(
        cookie=cookie,
        cookie_browser=cookie_browser,
        timeout_seconds=timeout_seconds,
        transport=transport,
    )
    return fallback.find_leads(
        company=company,
        max_results=max_results,
        include_uncertain=include_uncertain,
        search_depth="deep",
    )
