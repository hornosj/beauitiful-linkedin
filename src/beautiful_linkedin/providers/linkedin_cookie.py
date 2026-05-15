from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import unquote, urlparse

import httpx

from beautiful_linkedin.cookie_resolver import resolve_linkedin_li_at_cookie
from beautiful_linkedin.models import CompanyInput, Lead
from beautiful_linkedin.processing.lead_extractor import match_target_title
from beautiful_linkedin.processing.normalizer import normalize_text
from beautiful_linkedin.processing.scorer import score_lead
from beautiful_linkedin.providers.lead_provider import LeadProvider

logger = logging.getLogger(__name__)

LINKEDIN_BASE = "https://www.linkedin.com"
VOYAGER_BASE = f"{LINKEDIN_BASE}/voyager/api"
SEARCH_QUERY_ID = "voyagerSearchDashClusters.b0928897b71bd00a5a7291755dcd64f0"
RESULTS_PER_PAGE = 25
MAX_SEARCH_PAGES = 20
LOCAL_TITLE_MATCH_MAX_SEARCH_PAGES = 40


class LinkedInCookieEmployeesProvider(LeadProvider):
    name = "linkedin_cookie"

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
                "Cookie li_at não encontrado. Informe --linkedin-cookie ou configure LINKEDIN_LI_AT_COOKIE."
            )
            return []

        try:
            session = LinkedInCookieSession(
                li_at=li_at,
                timeout_seconds=self.timeout_seconds,
                transport=self.transport,
            )
            session.initialize()
            company_info = session.resolve_company(company)
            records = session.search_employees(
                company_info,
                company.titles,
                max_results,
                title_match_mode=self.title_match_mode,
            )
        except httpx.HTTPStatusError as exc:
            from beautiful_linkedin.api_logging import log_http_error

            log_http_error(
                logger,
                provider="LinkedIn (cookie)",
                error=exc,
                context=f"empresa={company.company_name}",
            )
            return []
        except Exception as exc:
            logger.warning(
                "Scraper com cookie falhou para %s. %s: %s",
                company.company_name, type(exc).__name__, exc,
            )
            return []

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


@dataclass(frozen=True)
class CompanyInfo:
    name: str
    universal_name: str
    company_id: str | None
    linkedin_url: str


class LinkedInCookieSession:
    def __init__(
        self,
        li_at: str,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.li_at = li_at
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.cookies = f"li_at={li_at}; li_gc=1; lang=en_US"
        self.csrf_token = ""

    def initialize(self) -> None:
        with self._client() as client:
            response = client.get(
                f"{LINKEDIN_BASE}/feed/",
                headers={
                    "user-agent": _browser_user_agent(),
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "accept-language": "en-US,en;q=0.9",
                    "cookie": self.cookies,
                },
            )
        self.csrf_token = _extract_csrf_token(response.headers.get_list("set-cookie"), response.text)
        if not self.csrf_token:
            raise RuntimeError("não foi possível obter JSESSIONID/CSRF com o cookie atual")
        self.cookies = f'{self.cookies}; JSESSIONID="{self.csrf_token}"'

    def resolve_company(self, company: CompanyInput) -> CompanyInfo:
        universal_name = _company_universal_name(company)
        data = self._voyager_get(
            "organization/companies",
            params={
                "decorationId": "com.linkedin.voyager.deco.organization.web.WebFullCompanyMain-40",
                "q": "universalName",
                "universalName": universal_name,
            },
        )
        element = _first_dict(data.get("elements") if isinstance(data, dict) else None)
        company_id = _company_id_from_record(element) if element else None
        name = _first_string(element or {}, ["name"]) or company.company_name
        if not company_id:
            company_id = self._resolve_company_id_from_html(universal_name)
        return CompanyInfo(
            name=name,
            universal_name=universal_name,
            company_id=company_id,
            linkedin_url=f"{LINKEDIN_BASE}/company/{universal_name}/",
        )

    def search_employees(
        self,
        company: CompanyInfo,
        titles: list[str],
        max_results: int,
        title_match_mode: Literal["api", "local"] = "local",
    ) -> list[dict[str, Any]]:
        if title_match_mode == "local":
            return self._search_company_employees(company)
        return self._search_employees_by_api_title(company, titles, max_results)

    def _search_company_employees(self, company: CompanyInfo) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        page = 0
        while page < LOCAL_TITLE_MATCH_MAX_SEARCH_PAGES:
            data = self._voyager_get(
                "graphql",
                params={
                    "variables": _search_variables(
                        company_id=company.company_id,
                        company_name=company.name,
                        title="",
                        start=page * RESULTS_PER_PAGE,
                    ),
                    "queryId": SEARCH_QUERY_ID,
                },
            )
            page_records = _extract_profiles_from_search_response(data)
            if not page_records:
                break
            added = 0
            for record in page_records:
                url = _first_string(record, ["linkedinUrl"]) or ""
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
            while len(records) < max_results:
                data = self._voyager_get(
                    "graphql",
                    params={
                        "variables": _search_variables(
                            company_id=company.company_id,
                            company_name=company.name,
                            title=title,
                            start=page * RESULTS_PER_PAGE,
                        ),
                        "queryId": SEARCH_QUERY_ID,
                    },
                )
                page_records = _extract_profiles_from_search_response(data)
                if not page_records:
                    break
                for record in page_records:
                    url = _first_string(record, ["linkedinUrl"]) or ""
                    if url and url in seen_urls:
                        continue
                    record["companyName"] = record.get("companyName") or company.name
                    records.append(record)
                    if url:
                        seen_urls.add(url)
                    if len(records) >= max_results:
                        break
                page += 1
                if page >= MAX_SEARCH_PAGES:
                    break
        return records

    def _resolve_company_id_from_html(self, universal_name: str) -> str | None:
        with self._client() as client:
            response = client.get(
                f"{LINKEDIN_BASE}/company/{universal_name}/",
                headers=self._html_headers(),
            )
        match = re.search(r"urn:li:fsd_company:(\d+)", response.text)
        return match.group(1) if match else None

    def _voyager_get(self, path: str, params: dict[str, str]) -> Any:
        with self._client() as client:
            response = client.get(
                f"{VOYAGER_BASE}/{path}",
                params=params,
                headers=self._voyager_headers(),
            )
        if response.status_code in {401, 403}:
            raise RuntimeError("cookie expirado, inválido ou sem permissão")
        if response.status_code == 429:
            raise RuntimeError("LinkedIn retornou rate limit para o cookie atual")
        response.raise_for_status()
        return response.json()

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout_seconds, transport=self.transport)

    def _voyager_headers(self) -> dict[str, str]:
        return {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "csrf-token": self.csrf_token,
            "cookie": self.cookies,
            "user-agent": _browser_user_agent(),
            "x-li-lang": "en_US",
            "x-restli-protocol-version": "2.0.0",
        }

    def _html_headers(self) -> dict[str, str]:
        return {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "cookie": self.cookies,
            "user-agent": _browser_user_agent(),
        }


def _record_to_lead(
    company: CompanyInput,
    record: dict[str, Any],
    include_uncertain: bool,
) -> Lead | None:
    person_name = _first_string(record, ["name", "fullName"])
    title = _current_title(record)
    linkedin_url = _first_string(record, ["linkedinUrl", "linkedin_url"])
    company_name = _first_string(record, ["companyName"]) or company.company_name
    headline = _first_string(record, ["headline"]) or ""
    matched_title = match_target_title(f"{title or ''} {headline}", company.titles)
    if not include_uncertain and not matched_title:
        return None

    lead = Lead(
        company_name=company.company_name,
        company_domain=company.company_domain,
        person_name=person_name,
        title=title or headline or None,
        linkedin_url=linkedin_url,
        source_url=linkedin_url or "linkedin_cookie:search",
        source_type="linkedin_cookie",
        snippet=" | ".join(part for part in [title, company_name, headline] if part),
        matched_title=matched_title,
        confidence_score=30,
    )
    return lead.model_copy(update={"confidence_score": score_lead(lead)})


def _search_variables(
    company_id: str | None,
    company_name: str,
    title: str,
    start: int,
) -> str:
    filters = ["(key:resultType,value:List(PEOPLE))"]
    if company_id:
        filters.append(f"(key:currentCompany,value:List({company_id}))")
    if title:
        filters.append(f"(key:title,value:List({_escape_linkedin_query_value(title)}))")
    keywords = title or company_name
    return (
        f"(start:{start},origin:GLOBAL_SEARCH_HEADER,"
        f"query:(keywords:{_escape_linkedin_query_value(keywords)},"
        "flagshipSearchIntent:SEARCH_SRP,"
        f"queryParameters:List({','.join(filters)}),"
        "includeFiltersInResponse:false))"
    )


def _extract_profiles_from_search_response(data: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for entity in _walk_dicts(data):
        profile = _profile_from_entity(entity)
        if profile:
            records.append(profile)
    return _dedupe_records(records)


def _profile_from_entity(entity: dict[str, Any]) -> dict[str, Any] | None:
    url = _profile_url_from_entity(entity)
    public_identifier = _public_identifier_from_url(url) if url else _first_string(entity, ["publicIdentifier"])
    if not public_identifier:
        return None

    name = _text_value(entity.get("title")) or _name_from_entity(entity)
    headline = (
        _text_value(entity.get("primarySubtitle"))
        or _first_string(entity, ["headline", "occupation"])
        or ""
    )
    location = _text_value(entity.get("secondarySubtitle")) or ""
    return {
        "publicIdentifier": public_identifier,
        "linkedinUrl": url or f"{LINKEDIN_BASE}/in/{public_identifier}/",
        "name": name,
        "headline": headline,
        "location": location,
        "title": _title_from_headline(headline),
    }


def _walk_dicts(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        found.append(value)
        for child in value.values():
            found.extend(_walk_dicts(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_dicts(child))
    return found


def _dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for record in records:
        key = _first_string(record, ["linkedinUrl"]) or _first_string(record, ["publicIdentifier"])
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return deduped


def _extract_csrf_token(set_cookies: list[str], body: str) -> str:
    for cookie in set_cookies:
        match = re.search(r'JSESSIONID="?([^";]+)', cookie)
        if match:
            return match.group(1).replace('"', "")
    match = re.search(r'JSESSIONID.*?["\']([^"\']+)["\']', body)
    return match.group(1).replace('"', "") if match else ""


def _company_universal_name(company: CompanyInput) -> str:
    if company.linkedin_url:
        parsed = urlparse(company.linkedin_url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] == "company":
            return parts[1].lower()
    return re.sub(r"[^a-z0-9-]+", "-", normalize_text(company.company_name)).strip("-")


def _company_id_from_record(record: dict[str, Any]) -> str | None:
    for key in ["entityUrn", "objectUrn"]:
        value = _first_string(record, [key])
        if value:
            match = re.search(r":(\d+)$", value)
            if match:
                return match.group(1)
    return None


def _current_title(record: dict[str, Any]) -> str | None:
    title = _first_string(record, ["title"])
    if title:
        return title
    return _title_from_headline(_first_string(record, ["headline"]) or "")


def _title_from_headline(headline: str) -> str | None:
    cleaned = headline.strip()
    lowered = cleaned.lower()
    for separator in [" at ", " na ", " no ", " em "]:
        index = lowered.find(separator)
        if index > 0:
            return cleaned[:index].strip()
    return cleaned or None


def _profile_url_from_entity(entity: dict[str, Any]) -> str | None:
    url = _first_string(entity, ["navigationUrl", "linkedinUrl", "url"])
    if url and "/in/" in url:
        return url.split("?")[0]
    public_identifier = _first_string(entity, ["publicIdentifier"])
    if public_identifier:
        return f"{LINKEDIN_BASE}/in/{public_identifier}/"
    return None


def _public_identifier_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "in":
        return unquote(parts[1])
    return None


def _name_from_entity(entity: dict[str, Any]) -> str | None:
    first = _first_string(entity, ["firstName"]) or ""
    last = _first_string(entity, ["lastName"]) or ""
    return f"{first} {last}".strip() or None


def _text_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        direct = _first_string(value, ["text"])
        if direct:
            return direct
    return None


def _first_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
    return value if isinstance(value, dict) else None


def _first_string(record: dict[str, Any], keys: list[str]) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _escape_linkedin_query_value(value: str) -> str:
    return value.replace(",", " ").replace(")", " ").replace("(", " ").strip()


def _browser_user_agent() -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
