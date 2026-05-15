"""Google HTML SERP scraper with consent handling and JS-redirect bypass.

This engine handles Google's consent page flow and uses the Google
Basic Version (gbv=1) parameter to request simpler HTML that doesn't
require JavaScript rendering.

When Google serves a redirect/consent page, we:
1. Submit the consent form programmatically
2. Accept cookies from the consent flow
3. Retry the search with the established session
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from beautiful_linkedin.models import SearchResult
from beautiful_linkedin.processing.normalizer import (
    contains_linkedin_profile_url,
    normalize_url,
)
from beautiful_linkedin.search.search_engine import SearchEngine
from beautiful_linkedin.search.user_agents import build_headers, random_profile

logger = logging.getLogger(__name__)

GOOGLE_SEARCH_URL = "https://www.google.com/search"

# Pre-set consent cookie to bypass EU/BR consent page
_CONSENT_COOKIES = {
    "CONSENT": "PENDING+987",
    "SOCS": "CAISHAgCEhJnd3NfMjAyNDAyMjEtMF9SQzIaAmVuIAEaBgiA_LyuBg",
}


class GoogleHtmlSearchEngine(SearchEngine):
    """Scrape Google Search HTML results without API keys.

    Handles Google's consent/redirect page by pre-setting cookies and
    using the Basic Version (gbv=1) parameter.
    """

    def __init__(
        self,
        sleep_range: tuple[float, float] = (3.0, 7.0),
        timeout_seconds: float = 20.0,
        max_pages: int = 2,
        results_per_page: int = 10,
        geo: str = "BR",
        lang: str = "pt-BR",
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.sleep_range = sleep_range
        self.timeout_seconds = timeout_seconds
        self.max_pages = max_pages
        self.results_per_page = results_per_page
        self.geo = geo
        self.lang = lang
        self.transport = transport
        self.sleep = sleep
        self._profile = random_profile()
        self._client: httpx.Client | None = None
        self._query_count = 0
        self._max_queries_per_session = random.randint(6, 12)
        self._consent_handled = False

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        all_results: list[SearchResult] = []
        seen_urls: set[str] = set()

        pages = min(
            self.max_pages,
            max(1, (max_results + self.results_per_page - 1) // self.results_per_page),
        )

        for page in range(pages):
            start = page * self.results_per_page
            try:
                html = self._fetch_html(query, start=start)
                if not html:
                    break

                # Check if we got a consent/redirect page
                if _is_consent_or_redirect(html):
                    logger.info("Google: consent/redirect page detectada, tentando resolver...")
                    html = self._handle_consent_and_retry(query, start=start)
                    if not html or _is_consent_or_redirect(html):
                        logger.warning("Google: não conseguiu ultrapassar consent page")
                        break

                page_results = self._parse_html(html)
                new_count = 0
                for r in page_results:
                    nurl = normalize_url(r.url)
                    if nurl not in seen_urls:
                        all_results.append(r)
                        seen_urls.add(nurl)
                        new_count += 1
                if new_count == 0:
                    break
                if len(all_results) >= max_results:
                    break
                self._sleep_jittered(1.5, 3.5)
            except _GoogleBlockedError as exc:
                logger.warning("Google bloqueou a busca para '%s'. %s", query, exc)
                break
            except httpx.HTTPError as exc:
                logger.warning("Google falhou na consulta '%s'. %s", query, exc)
                break
            except Exception as exc:
                logger.warning("Erro inesperado no Google para '%s'. %s", query, exc)
                break

        self._sleep_between_queries()
        return all_results[:max_results]

    def _get_client(self) -> httpx.Client:
        if (
            self._client is None
            or self._client.is_closed
            or self._query_count >= self._max_queries_per_session
        ):
            if self._client is not None and not self._client.is_closed:
                self._client.close()
            self._rotate_session()
        return self._client  # type: ignore[return-value]

    def _rotate_session(self) -> None:
        self._profile = random_profile()
        self._query_count = 0
        self._max_queries_per_session = random.randint(6, 12)
        self._consent_handled = False
        headers = build_headers(self._profile)
        # Don't use brotli - httpx may not decompress it properly
        headers["Accept-Encoding"] = "gzip, deflate"

        self._client = httpx.Client(
            headers=headers,
            timeout=self.timeout_seconds,
            follow_redirects=True,
            transport=self.transport,
            cookies=dict(_CONSENT_COOKIES),  # Pre-set consent cookies
        )
        # Pre-warm
        try:
            self._client.get(
                "https://www.google.com/",
                params={"hl": self.lang[:2]},
            )
            self._sleep_jittered(0.5, 1.5)
        except Exception:
            pass

    def _fetch_html(self, query: str, start: int = 0) -> str:
        client = self._get_client()
        self._query_count += 1

        params = {
            "q": query,
            "num": str(self.results_per_page),
            "hl": self.lang[:2],
            "gl": self.geo.lower(),
            "start": str(start),
            "gbv": "1",  # Google Basic Version: simpler HTML, less JS
            "filter": "0",
        }

        response = client.get(GOOGLE_SEARCH_URL, params=params)

        if response.status_code in {429, 503}:
            raise _GoogleBlockedError(f"HTTP {response.status_code}")

        if _looks_like_captcha(response.text):
            self._rotate_session()
            raise _GoogleBlockedError("CAPTCHA detectado no Google")

        response.raise_for_status()
        return response.text

    def _handle_consent_and_retry(self, query: str, start: int = 0) -> str:
        """Try to handle the consent page by submitting the form."""
        client = self._get_client()

        # Method 1: Set SOCS cookie directly (newer consent mechanism)
        client.cookies.set(
            "SOCS",
            "CAISHAgCEhJnd3NfMjAyNDAyMjEtMF9SQzIaAmVuIAEaBgiA_LyuBg",
            domain=".google.com",
        )
        client.cookies.set("CONSENT", "YES+cb.20240101-00-p0.pt+FX+111", domain=".google.com")

        self._sleep_jittered(0.5, 1.0)
        self._consent_handled = True

        # Retry the search
        params = {
            "q": query,
            "num": str(self.results_per_page),
            "hl": self.lang[:2],
            "gl": self.geo.lower(),
            "start": str(start),
            "gbv": "1",
            "filter": "0",
        }

        try:
            response = client.get(GOOGLE_SEARCH_URL, params=params)
            if response.status_code == 200:
                return response.text
        except Exception:
            pass

        return ""

    def _parse_html(self, html: str) -> list[SearchResult]:
        soup = BeautifulSoup(html, "lxml")
        results: list[SearchResult] = []
        seen: set[str] = set()

        # Strategy 1: Standard result blocks
        for div in soup.select("div.g, div.tF2Cxc, div[data-sokoban-container]"):
            result = self._parse_result_div(div)
            if result:
                nurl = normalize_url(result.url)
                if nurl not in seen:
                    results.append(result)
                    seen.add(nurl)

        # Strategy 2: All anchor tags pointing to linkedin.com/in
        for link in soup.select('a[href*="linkedin.com/in/"]'):
            url = self._extract_url_from_link(link)
            if not url or not contains_linkedin_profile_url(url):
                continue
            nurl = normalize_url(url)
            if nurl in seen:
                continue
            title = _text_of(link)
            snippet = _snippet_near(link)
            if title or snippet:
                results.append(
                    SearchResult(
                        title=title, url=url, snippet=snippet,
                        source_type="search_google_html",
                    )
                )
                seen.add(nurl)

        # Strategy 3: Parse /url?q= redirect links
        for link in soup.select("a[href^='/url?']"):
            url = self._extract_url_from_link(link)
            if not url or not contains_linkedin_profile_url(url):
                continue
            nurl = normalize_url(url)
            if nurl in seen:
                continue
            title = _text_of(link)
            results.append(
                SearchResult(
                    title=title or url, url=url, snippet="",
                    source_type="search_google_html",
                )
            )
            seen.add(nurl)

        # Strategy 4: Find cite/text references
        for cite in soup.select("cite"):
            text = cite.get_text(" ", strip=True)
            if "linkedin.com/in/" in text:
                url = _extract_url_from_text(text)
                if url:
                    nurl = normalize_url(url)
                    if nurl not in seen:
                        parent = cite.find_parent("div")
                        title = ""
                        snippet = ""
                        if parent:
                            h3 = parent.select_one("h3")
                            title = h3.get_text(" ", strip=True) if h3 else ""
                        results.append(
                            SearchResult(
                                title=title or url, url=url, snippet=snippet,
                                source_type="search_google_html",
                            )
                        )
                        seen.add(nurl)

        return results

    def _parse_result_div(self, div: Tag) -> SearchResult | None:
        link = div.select_one("a[href]")
        if link is None:
            return None
        url = self._extract_url_from_link(link)
        if not url or not contains_linkedin_profile_url(url):
            return None
        h3 = div.select_one("h3")
        title = h3.get_text(" ", strip=True) if h3 else _text_of(link)
        snippet = ""
        for selector in ["div.VwiC3b", "span.aCOpRe", "div[data-sncf]", "div.IsZvec"]:
            node = div.select_one(selector)
            if node:
                snippet = node.get_text(" ", strip=True)[:500]
                break
        if not snippet:
            snippet = div.get_text(" ", strip=True)[:300]
        return SearchResult(
            title=_clean_google_title(title), url=url, snippet=snippet,
            source_type="search_google_html",
        )

    def _extract_url_from_link(self, link: Tag) -> str | None:
        href = str(link.get("href") or "")
        if not href:
            return None
        if href.startswith("/url?"):
            parsed = urlparse(href)
            q = parse_qs(parsed.query).get("q", [""])[0]
            return unquote(q).strip() if q else None
        if href.startswith("http"):
            return href.strip()
        return None

    def _sleep_between_queries(self) -> None:
        low, high = self.sleep_range
        if high <= 0:
            return
        self._sleep_jittered(low, high)

    def _sleep_jittered(self, low: float, high: float) -> None:
        self.sleep(random.uniform(max(0, low), max(low, high)))


class _GoogleBlockedError(Exception):
    pass


def _is_consent_or_redirect(html: str) -> bool:
    """Detect Google's consent/redirect/interstitial pages."""
    lowered = html.lower()
    indicators = [
        "clique aqui se o redirecionamento",
        "click here if the redirect",
        "before you continue to google",
        "consent.google.com",
        "sg_rel",  # Google's redirect marker
    ]
    return any(indicator in lowered for indicator in indicators)


def _looks_like_captcha(html: str) -> bool:
    lowered = html.lower()
    indicators = [
        "unusual traffic",
        "our systems have detected unusual traffic",
        "sorry/image",
        "/recaptcha/",
        "captcha",
        "to continue, please prove",
    ]
    return any(indicator in lowered for indicator in indicators)


def _text_of(tag: Tag) -> str:
    return re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()


def _snippet_near(tag: Tag) -> str:
    parent = tag.find_parent(["div", "li", "article"])
    if parent:
        return parent.get_text(" ", strip=True)[:500]
    return ""


def _clean_google_title(title: str) -> str:
    title = re.sub(r"\s*[-–—|]\s*LinkedIn\s*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^LinkedIn\s*[-–—|]\s*", "", title, flags=re.IGNORECASE)
    return title.strip()


def _extract_url_from_text(text: str) -> str | None:
    match = re.search(
        r"(?:https?://)?(?:www\.|br\.)?linkedin\.com/in/[\w-]+",
        text, re.IGNORECASE,
    )
    if match:
        url = match.group(0)
        if not url.startswith("http"):
            url = f"https://{url}"
        return url
    breadcrumb = re.search(
        r"(?:www\.|br\.)?linkedin\.com\s*›\s*in\s*›\s*([\w-]+)",
        text, re.IGNORECASE,
    )
    if breadcrumb:
        return f"https://www.linkedin.com/in/{breadcrumb.group(1)}"
    return None
