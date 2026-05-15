from __future__ import annotations

import gzip
import json
import logging
import re
from typing import Any

import httpx
from bs4 import BeautifulSoup

from beautiful_linkedin.api_logging import log_http_error, log_transport_error
from beautiful_linkedin.cache.sqlite_cache import SqliteJsonCache
from beautiful_linkedin.models import CompanyInput, Lead
from beautiful_linkedin.processing.lead_extractor import match_target_title
from beautiful_linkedin.processing.normalizer import normalize_text
from beautiful_linkedin.providers.lead_provider import LeadProvider

logger = logging.getLogger(__name__)

COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"
CDX_BASE = "https://index.commoncrawl.org"
WARC_BASE = "https://data.commoncrawl.org"


class CommonCrawlProvider(LeadProvider):
    name = "common_crawl"

    def __init__(
        self,
        indexes: list[str] | None = None,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        cache: SqliteJsonCache | None = None,
        max_pages: int = 5,
    ) -> None:
        self.indexes = indexes
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.cache = cache
        self.max_pages = max_pages

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

        indexes = self.indexes or self._latest_indexes()
        if not indexes:
            return []

        cache_payload = {
            "cache_version": 1,
            "request": {
                "company_name": company.company_name,
                "company_domain": company.company_domain,
                "indexes": indexes,
            },
        }
        if self.cache:
            cached = self.cache.get_json(self.name, cache_payload)
            if isinstance(cached, list):
                return [Lead.model_validate(item) for item in cached if isinstance(item, dict)]

        candidates = self._candidate_captures(indexes)
        max_captures = min(max(max_results * 2, 1), 200)
        leads: list[Lead] = []
        checked = 0
        for capture in candidates[:max_captures]:
            checked += 1
            html = self._fetch_capture_html(capture, company)
            if not html or not _mentions_company(html, company):
                continue
            lead = _lead_from_capture(company, capture, html, include_uncertain)
            if not lead:
                continue
            leads.append(lead)
            if len(leads) >= max_results:
                break

        logger.info(
            "Common Crawl: %d captures candidatos para '%s', %d matches",
            checked,
            company.company_name,
            len(leads),
        )
        if self.cache:
            self.cache.set_json(self.name, cache_payload, [lead.model_dump() for lead in leads])
        return leads

    def _latest_indexes(self) -> list[str]:
        cache_payload = {"cache_version": 1, "request": {"url": COLLINFO_URL}}
        if self.cache:
            cached = self.cache.get_json("common_crawl_indexes", cache_payload)
            if isinstance(cached, list):
                return [str(item) for item in cached[:3] if item]

        data = self._get_json(COLLINFO_URL, "Common Crawl", "indexes")
        if not isinstance(data, list):
            return []
        indexes = [
            str(item.get("id"))
            for item in data
            if isinstance(item, dict) and item.get("id")
        ][:3]
        if indexes and self.cache:
            self.cache.set_json("common_crawl_indexes", cache_payload, indexes)
        return indexes

    def _candidate_captures(self, indexes: list[str]) -> list[dict[str, Any]]:
        captures: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index_id in indexes[: self.max_pages]:
            endpoint = f"{CDX_BASE}/{index_id}-index"
            params = {
                "url": "*.linkedin.com/in/*",
                "output": "json",
                "limit": "200",
                "filter": r"~url:.*linkedin\.com/in/.*",
            }
            lines = self._get_text_lines(
                endpoint,
                "Common Crawl",
                "url=*.linkedin.com/in/*",
                params=params,
            )
            for line in lines:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip()
                if not url or url in seen:
                    continue
                seen.add(url)
                captures.append(item)
        return captures

    def _fetch_capture_html(
        self, capture: dict[str, Any], company: CompanyInput
    ) -> str:
        filename = str(capture.get("filename") or "").strip()
        offset = _int_value(capture.get("offset"))
        length = _int_value(capture.get("length"))
        if not filename or offset is None or length is None:
            return ""
        endpoint = f"{WARC_BASE}/{filename}"
        headers = {"Range": f"bytes={offset}-{offset + length - 1}"}
        context = f"empresa={company.company_name}"
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self.transport,
                headers=headers,
            ) as client:
                response = client.get(endpoint)
                response.raise_for_status()
                content = response.content
        except httpx.HTTPStatusError as exc:
            log_http_error(logger, provider="Common Crawl", error=exc, context=context)
            return ""
        except (httpx.TransportError, ValueError) as exc:
            log_transport_error(
                logger,
                provider="Common Crawl",
                error=exc,
                endpoint=endpoint,
                context=context,
            )
            return ""

        try:
            content = gzip.decompress(content)
        except OSError:
            pass
        return _extract_html_from_warc(content)

    def _get_json(self, endpoint: str, provider: str, context: str) -> Any:
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self.transport,
                headers={"Accept": "application/json"},
            ) as client:
                response = client.get(endpoint)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            log_http_error(logger, provider=provider, error=exc, context=context)
            return None
        except (httpx.TransportError, ValueError) as exc:
            log_transport_error(
                logger,
                provider=provider,
                error=exc,
                endpoint=endpoint,
                context=context,
            )
            return None

    def _get_text_lines(
        self,
        endpoint: str,
        provider: str,
        context: str,
        params: dict[str, str],
    ) -> list[str]:
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self.transport,
                headers={"Accept": "text/plain,application/json"},
            ) as client:
                response = client.get(endpoint, params=params)
                response.raise_for_status()
                return [line for line in response.text.splitlines() if line.strip()]
        except httpx.HTTPStatusError as exc:
            log_http_error(logger, provider=provider, error=exc, context=context)
            return []
        except (httpx.TransportError, ValueError) as exc:
            log_transport_error(
                logger,
                provider=provider,
                error=exc,
                endpoint=endpoint,
                context=context,
            )
            return []


def _lead_from_capture(
    company: CompanyInput,
    capture: dict[str, Any],
    html: str,
    include_uncertain: bool,
) -> Lead | None:
    title_text = _html_title(html)
    person_name, title = _parse_linkedin_title(title_text, company)
    snippet = title_text or "Common Crawl LinkedIn capture"
    matched_title = match_target_title(f"{title or ''} {snippet}", company.titles)
    if not include_uncertain and not matched_title:
        return None
    linkedin_url = str(capture.get("url") or "").strip()
    return Lead(
        company_name=company.company_name,
        company_domain=company.company_domain,
        person_name=person_name,
        title=title,
        linkedin_url=linkedin_url,
        source_url=linkedin_url,
        source_type="common_crawl",
        snippet=snippet,
        matched_title=matched_title,
        confidence_score=55 if matched_title else 35,
    )


def _parse_linkedin_title(
    title_text: str, company: CompanyInput
) -> tuple[str | None, str | None]:
    cleaned = re.sub(r"\s+", " ", title_text or "").strip()
    if not cleaned:
        return None, None
    pieces = [
        piece.strip()
        for piece in re.split(r"\s+\|\s+|\s+-\s+|\s+–\s+|\s+—\s+", cleaned)
        if piece.strip()
    ]
    pieces = [
        piece
        for piece in pieces
        if normalize_text(piece) not in {"linkedin", normalize_text(company.company_name)}
    ]
    if len(pieces) >= 2:
        return pieces[0], pieces[1]
    return pieces[0] if pieces else None, None


def _html_title(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    title = soup.find("title")
    return title.get_text(" ", strip=True) if title else ""


def _mentions_company(html: str, company: CompanyInput) -> bool:
    normalized = normalize_text(BeautifulSoup(html, "lxml").get_text(" ", strip=True))
    company_name = normalize_text(company.company_name)
    if company_name and company_name in normalized:
        return True
    if company.company_domain and normalize_text(company.company_domain) in normalized:
        return True
    return False


def _extract_html_from_warc(content: bytes) -> str:
    text = content.decode("utf-8", errors="ignore")
    index = text.lower().find("<html")
    if index >= 0:
        return text[index:]
    parts = re.split(r"\r?\n\r?\n", text, maxsplit=2)
    return parts[-1] if parts else text


def _int_value(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
