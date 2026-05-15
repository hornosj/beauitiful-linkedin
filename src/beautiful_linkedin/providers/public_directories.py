from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable
from typing import Any

import httpx
from bs4 import BeautifulSoup

from beautiful_linkedin.api_logging import log_http_error, log_transport_error
from beautiful_linkedin.cache.sqlite_cache import SqliteJsonCache
from beautiful_linkedin.models import CompanyInput, Lead
from beautiful_linkedin.processing.lead_extractor import match_target_title
from beautiful_linkedin.providers.lead_provider import LeadProvider

logger = logging.getLogger(__name__)

DIRECTORY_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


class PublicDirectoriesProvider(LeadProvider):
    name = "public_directories"

    def __init__(
        self,
        sources: list[str] | None = None,
        timeout_seconds: float = 12.0,
        transport: httpx.BaseTransport | None = None,
        cache: SqliteJsonCache | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.sources = sources or ["theorg", "rocketreach"]
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.cache = cache
        self.sleep = sleep

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

        leads: list[Lead] = []
        for source in self.sources:
            source_leads = self._cached_source_leads(source, company)
            if source_leads is None:
                if source == "theorg":
                    source_leads = self._scrape_theorg(company)
                elif source == "rocketreach":
                    source_leads = self._scrape_rocketreach(company)
                else:
                    logger.warning("Diretório público desconhecido '%s'. Ignorando.", source)
                    source_leads = []
                self._store_source_leads(source, company, source_leads)

            for lead in source_leads:
                matched_title = match_target_title(
                    f"{lead.title or ''} {lead.snippet}", company.titles
                )
                if not include_uncertain and not matched_title:
                    continue
                leads.append(lead.model_copy(update={"matched_title": matched_title}))
                if len(leads) >= max_results:
                    return leads
            self.sleep(random.uniform(0.5, 1.5))
        return leads

    def _scrape_theorg(self, company: CompanyInput) -> list[Lead]:
        slug = _company_slug(company.company_name)
        url = f"https://theorg.com/org/{slug}"
        html = self._get_html(url, "Public Dir theorg", company)
        if not html:
            return []
        soup = BeautifulSoup(html, "lxml")
        cards = _select_cards(soup, ["team-member", "person-card"])
        return [
            lead
            for card in cards
            if (
                lead := _lead_from_card(
                    company=company,
                    card=card,
                    source_url=url,
                    source_type="public_dir_theorg",
                )
            )
        ]

    def _scrape_rocketreach(self, company: CompanyInput) -> list[Lead]:
        slug = _company_slug(company.company_name)
        url = f"https://rocketreach.co/company/{slug}"
        html = self._get_html(url, "Public Dir rocketreach", company)
        if not html:
            return []
        soup = BeautifulSoup(html, "lxml")
        cards = _select_cards(soup, ["profile-card"])
        return [
            lead
            for card in cards
            if (
                lead := _lead_from_card(
                    company=company,
                    card=card,
                    source_url=url,
                    source_type="public_dir_rocketreach",
                )
            )
        ]

    def _get_html(
        self, url: str, provider: str, company: CompanyInput
    ) -> str:
        headers = {
            "User-Agent": DIRECTORY_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        context = f"empresa={company.company_name}"
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self.transport,
                headers=headers,
                follow_redirects=True,
            ) as client:
                response = client.get(url)
                response.raise_for_status()
                return response.text
        except httpx.HTTPStatusError as exc:
            log_http_error(logger, provider=provider, error=exc, context=context)
            return ""
        except (httpx.TransportError, ValueError) as exc:
            log_transport_error(
                logger,
                provider=provider,
                error=exc,
                endpoint=url,
                context=context,
            )
            return ""

    def _cached_source_leads(
        self, source: str, company: CompanyInput
    ) -> list[Lead] | None:
        if not self.cache:
            return None
        cached = self.cache.get_json(self.name, _cache_payload(source, company))
        if not isinstance(cached, list):
            return None
        return [Lead.model_validate(item) for item in cached if isinstance(item, dict)]

    def _store_source_leads(
        self, source: str, company: CompanyInput, leads: list[Lead]
    ) -> None:
        if self.cache:
            self.cache.set_json(
                self.name,
                _cache_payload(source, company),
                [lead.model_dump() for lead in leads],
            )


def _select_cards(soup: BeautifulSoup, tokens: list[str]) -> list[Any]:
    cards = []
    for tag in soup.find_all(True):
        attrs = " ".join(
            str(value)
            for value in [
                tag.get("data-testid"),
                tag.get("class"),
                tag.get("data-test"),
            ]
            if value
        ).lower()
        if any(token in attrs for token in tokens):
            cards.append(tag)
    return cards


def _lead_from_card(
    company: CompanyInput,
    card: Any,
    source_url: str,
    source_type: str,
) -> Lead | None:
    person_name = _extract_name(card)
    title = _extract_title(card, person_name)
    linkedin_url = _extract_linkedin_url(card)
    if not person_name and not title:
        return None
    snippet = " | ".join(part for part in [person_name, title, company.company_name] if part)
    return Lead(
        company_name=company.company_name,
        company_domain=company.company_domain,
        person_name=person_name,
        title=title,
        linkedin_url=linkedin_url,
        source_url=linkedin_url or source_url,
        source_type=source_type,
        snippet=snippet,
        matched_title=None,
        confidence_score=25,
    )


def _extract_name(card: Any) -> str | None:
    for selector in [
        '[class*="name"]',
        '[data-testid*="name"]',
        "h1",
        "h2",
        "h3",
        "h4",
        "a",
    ]:
        element = card.select_one(selector)
        text = _clean_text(element.get_text(" ", strip=True)) if element else None
        if text and not _looks_like_role(text):
            return text
    return None


def _extract_title(card: Any, person_name: str | None) -> str | None:
    for selector in [
        '[class*="title"]',
        '[class*="role"]',
        '[class*="position"]',
        '[data-testid*="title"]',
        '[data-testid*="role"]',
        "p",
        "span",
    ]:
        for element in card.select(selector):
            text = _clean_text(element.get_text(" ", strip=True))
            if not text or text == person_name:
                continue
            if _looks_like_role(text):
                return text
    return None


def _extract_linkedin_url(card: Any) -> str | None:
    element = card.select_one('a[href*="linkedin.com/in/"]')
    if not element:
        return None
    href = str(element.get("href") or "").strip()
    return href or None


def _looks_like_role(text: str) -> bool:
    lowered = text.lower()
    role_terms = [
        "chief",
        "ceo",
        "cfo",
        "cto",
        "founder",
        "head",
        "director",
        "manager",
        "marketing",
        "sales",
        "growth",
        "product",
        "engineer",
        "analyst",
        "lead",
        "president",
        "diretor",
        "gerente",
        "coordenador",
        "analista",
    ]
    return any(term in lowered for term in role_terms)


def _company_slug(company_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", company_name.lower()).strip("-")
    return re.sub(r"-+", "-", slug)


def _clean_text(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _cache_payload(source: str, company: CompanyInput) -> dict[str, Any]:
    return {
        "cache_version": 1,
        "request": {"source": source, "company_slug": _company_slug(company.company_name)},
    }
