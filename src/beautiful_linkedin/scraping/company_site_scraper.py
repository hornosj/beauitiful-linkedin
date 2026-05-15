from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from beautiful_linkedin.config import USER_AGENT
from beautiful_linkedin.models import CompanyInput, Lead
from beautiful_linkedin.processing.lead_extractor import extract_leads_from_company_site_text
from beautiful_linkedin.processing.normalizer import normalize_domain
from beautiful_linkedin.scraping.html_parser import extract_visible_text

logger = logging.getLogger(__name__)

LIKELY_PATHS = [
    "/",
    "/about",
    "/about-us",
    "/sobre",
    "/sobre-nos",
    "/quem-somos",
    "/team",
    "/equipe",
    "/leadership",
    "/lideranca",
    "/governance",
    "/governanca",
    "/trabalhe-conosco",
    "/trabalhe-conosco.htm",
    "/carreiras",
    "/careers",
    "/pessoas",
    "/people",
    "/executivos",
    "/diretoria",
    "/diretoria-executiva",
    "/governanca-corporativa",
    "/relacoes-com-investidores",
    "/investidores",
    "/imprensa",
]

LIKELY_LINK_TERMS = [
    "about",
    "sobre",
    "quem-somos",
    "team",
    "equipe",
    "leadership",
    "lideranca",
    "governanca",
    "governance",
    "diretoria",
    "executivos",
    "carreiras",
    "trabalhe",
    "people",
    "pessoas",
    "imprensa",
]


@dataclass(frozen=True)
class _PageCandidate:
    url: str
    preview: str
    source_order: int


class CompanySiteScraper:
    def __init__(
        self,
        timeout_seconds: float = 8.0,
        max_pages: int = 3,
        paths: list[str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_pages = max_pages
        self.paths = paths or LIKELY_PATHS
        self.transport = transport

    def scrape_company(
        self,
        company: CompanyInput,
        include_uncertain: bool = False,
    ) -> list[Lead]:
        domain = normalize_domain(company.company_domain)
        if not domain:
            return []

        if "linkedin.com" in domain:
            logger.warning("Skipping company-site scrape for LinkedIn domain: %s", domain)
            return []

        leads: list[Lead] = []
        headers = {"User-Agent": USER_AGENT}

        with httpx.Client(
            timeout=self.timeout_seconds,
            headers=headers,
            follow_redirects=True,
            transport=self.transport,
        ) as client:
            homepage_url = f"https://{domain}/"
            homepage_html = self._fetch(client, homepage_url, domain)
            crawled_urls: set[str] = set()

            if homepage_html:
                crawled_urls.add(_normalize_candidate_key(homepage_url))
                text = extract_visible_text(homepage_html)
                leads.extend(
                    extract_leads_from_company_site_text(
                        company=company,
                        source_url=homepage_url,
                        text=text,
                        include_uncertain=include_uncertain,
                    )
                )

            candidates = self._rank_candidates(
                self._candidate_pages(domain, homepage_html, company),
                company,
                crawled_urls,
            )

            remaining_pages = max(0, self.max_pages - len(crawled_urls))
            for candidate in candidates[:remaining_pages]:
                url = candidate.url
                key = _normalize_candidate_key(url)
                if key in crawled_urls:
                    continue
                html = self._fetch(client, url, domain)
                crawled_urls.add(key)
                if not html:
                    continue
                text = extract_visible_text(html)
                leads.extend(
                    extract_leads_from_company_site_text(
                        company=company,
                        source_url=url,
                        text=text,
                        include_uncertain=include_uncertain,
                    )
                )

        return leads

    def _candidate_pages(
        self,
        domain: str,
        homepage_html: str | None,
        company: CompanyInput,
    ) -> list[_PageCandidate]:
        candidates = [
            _PageCandidate(
                url=f"https://{domain}{path}",
                preview=path,
                source_order=index,
            )
            for index, path in enumerate(self.paths)
        ]
        if homepage_html:
            candidates.extend(
                self._discover_likely_links(
                    homepage_html,
                    domain,
                    company,
                    source_offset=len(candidates),
                )
            )
        return _dedupe_candidates(candidates)

    def _rank_candidates(
        self,
        candidates: list[_PageCandidate],
        company: CompanyInput,
        crawled_urls: set[str],
    ) -> list[_PageCandidate]:
        scored = [
            (candidate, self._expected_information_gain(candidate, company))
            for candidate in candidates
            if _normalize_candidate_key(candidate.url) not in crawled_urls
        ]
        scored.sort(key=lambda item: (-item[1], item[0].source_order))
        return [candidate for candidate, _score in scored]

    def _expected_information_gain(
        self,
        candidate: _PageCandidate,
        company: CompanyInput,
    ) -> float:
        relevance = _term_overlap_score(
            _tokens(candidate.preview),
            _company_site_query_terms(company),
        )
        authority = _authority_score(candidate.url)
        novelty = _novelty_score(candidate.preview, candidate.url)
        return 0.55 * relevance + 0.30 * authority + 0.15 * novelty

    def _fetch(self, client: httpx.Client, url: str, allowed_domain: str) -> str | None:
        try:
            response = client.get(url)
        except httpx.TimeoutException as exc:
            logger.warning("Timeout fetching official page %s. %s", url, exc)
            return None
        except httpx.TransportError as exc:
            logger.warning("Connection error fetching official page %s. %s", url, exc)
            return None

        if response.status_code < 200 or response.status_code >= 300:
            logger.warning(
                "Skipping official page %s because it returned HTTP %s.",
                url,
                response.status_code,
            )
            return None

        if not self._is_allowed_final_url(str(response.url), allowed_domain):
            logger.warning(
                "Skipping redirected page outside the official domain: %s",
                response.url,
            )
            return None

        return response.text

    def _is_allowed_final_url(self, url: str, allowed_domain: str) -> bool:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host == allowed_domain or host.endswith(f".{allowed_domain}")

    def _discover_likely_links(
        self,
        html: str,
        domain: str,
        company: CompanyInput,
        source_offset: int = 0,
    ) -> list[_PageCandidate]:
        soup = BeautifulSoup(html, "lxml")
        links: list[_PageCandidate] = []
        target_terms = _tokens(" ".join(company.titles))
        for index, anchor in enumerate(soup.find_all("a", href=True)):
            href = str(anchor.get("href") or "").strip()
            if not href or href.startswith("#") or href.startswith("mailto:"):
                continue
            candidate = urljoin(f"https://{domain}/", href).split("#", 1)[0]
            if not self._is_allowed_final_url(candidate, domain):
                continue

            preview = " ".join(
                part
                for part in [
                    anchor.get_text(" ", strip=True),
                    urlparse(candidate).path.replace("/", " "),
                ]
                if part
            )
            normalized_preview = preview.lower()
            preview_terms = _tokens(preview)
            if (
                any(term in normalized_preview for term in LIKELY_LINK_TERMS)
                or bool(preview_terms & target_terms)
            ):
                links.append(
                    _PageCandidate(
                        url=candidate,
                        preview=preview,
                        source_order=source_offset + index,
                    )
                )
        return links


def _company_site_query_terms(company: CompanyInput) -> set[str]:
    query_parts = [
        company.company_name,
        " ".join(company.titles),
        "team equipe leadership lideranca liderança diretoria executivos pessoas people",
    ]
    return _tokens(" ".join(query_parts))


def _term_overlap_score(candidate_terms: set[str], query_terms: set[str]) -> float:
    if not candidate_terms or not query_terms:
        return 0.0
    return len(candidate_terms & query_terms) / len(query_terms)


def _authority_score(url: str) -> float:
    path = urlparse(url).path.lower()
    score = 0.35
    if any(term in path for term in LIKELY_LINK_TERMS):
        score += 0.35
    if any(term in path for term in ["team", "equipe", "leadership", "lideranca", "diretoria", "people"]):
        score += 0.2
    if path.count("/") <= 2:
        score += 0.1
    if re.search(r"\.(jpg|jpeg|png|gif|webp|svg|pdf)$", path):
        score -= 0.4
    return max(0.0, min(score, 1.0))


def _novelty_score(preview: str, url: str) -> float:
    terms = _tokens(f"{preview} {urlparse(url).path}")
    if not terms:
        return 0.4
    generic = {"sobre", "about", "home", "inicio", "index", "html", "htm"}
    return max(0.2, len(terms - generic) / len(terms))


def _tokens(text: str) -> set[str]:
    normalized = text.lower()
    normalized = re.sub(r"[^a-z0-9áàâãéêíóôõúçñü]+", " ", normalized)
    return {token for token in normalized.split() if len(token) > 2}


def _dedupe_candidates(values: list[_PageCandidate]) -> list[_PageCandidate]:
    seen: set[str] = set()
    deduped: list[_PageCandidate] = []
    for value in values:
        normalized = _normalize_candidate_key(value.url)
        if normalized not in seen:
            deduped.append(value)
            seen.add(normalized)
    return deduped


def _normalize_candidate_key(url: str) -> str:
    return url.lower().rstrip("/")
