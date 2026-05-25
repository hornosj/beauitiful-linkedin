"""Bucket B+: discover individual phones from public PDFs.

Search engines index a surprising amount of PDF content where
professionals leak their direct contact: pitch decks, conference
attendee lists, awarded papers, public proposals, training-program
participant lists, resumes published as PDF on personal sites.

The provider runs a small set of ``filetype:pdf`` queries per lead,
downloads each candidate (size-capped), extracts text with
``pdfminer.six``, and applies the same phone regex the website
harvester uses. Phones with the lead's name in the *same page* of the
PDF count as ``pdf_name_proximity`` (the strongest individual signal);
phones elsewhere in the document are tagged ``pdf`` (weaker — could be
the conference organizer's contact, not the lead's).

Fail-soft everywhere: failed downloads, parse errors, oversized PDFs,
and engine failures land in ``self.errors`` and the lookup keeps
going.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from typing import Callable

from beautiful_linkedin.models import SearchResult
from beautiful_linkedin.search.search_engine import SearchEngine
from beautiful_linkedin.storage.phone_harvester import (
    _PHONE_REGEX,
    _digits_only,
    _is_plausible_phone,
)
from beautiful_linkedin.storage.phone_lookup import (
    LookupQuery,
    PhoneCandidate,
)


logger = logging.getLogger(__name__)


HttpBinaryClient = Callable[[str], tuple[int, bytes, str]]
"""``http_client(url) -> (status_code, body_bytes, content_type)``."""


_DEFAULT_QUERY_TEMPLATES: tuple[str, ...] = (
    '"{name}" "{company}" filetype:pdf',
    '"{name}" filetype:pdf',
)


_DEFAULT_MAX_PDF_BYTES = 4 * 1024 * 1024  # 4 MB
_DEFAULT_RESULTS_PER_QUERY = 5
_DEFAULT_MAX_PDFS_PER_LEAD = 4


@dataclass(frozen=True)
class _PdfPhoneMatch:
    raw: str
    digits: str
    source_url: str
    engine: str
    has_proximity: bool


class PdfPhoneExtractor:
    """Bucket B PDF-based phone lookup."""

    name = "pdf_serp"

    def __init__(
        self,
        *,
        engines: list[SearchEngine],
        http_client: HttpBinaryClient | None = None,
        max_results_per_query: int = _DEFAULT_RESULTS_PER_QUERY,
        max_pdfs_per_lead: int = _DEFAULT_MAX_PDFS_PER_LEAD,
        max_bytes: int = _DEFAULT_MAX_PDF_BYTES,
        query_templates: tuple[str, ...] | None = None,
        engine_labels: list[str] | None = None,
        timeout_seconds: float = 12.0,
    ) -> None:
        if not engines:
            raise ValueError("PdfPhoneExtractor requires at least one engine")
        self._engines = engines
        self._engine_labels = engine_labels or [type(e).__name__ for e in engines]
        self._http_client = http_client or _default_binary_client(timeout_seconds)
        self._max_results = max(1, max_results_per_query)
        self._max_pdfs = max(1, max_pdfs_per_lead)
        self._max_bytes = max(1024, max_bytes)
        self._templates = tuple(query_templates or _DEFAULT_QUERY_TEMPLATES)
        self.errors: list[str] = []

    def lookup(self, query: LookupQuery) -> list[PhoneCandidate]:
        name = (query.full_name or "").strip()
        company = (query.company_name or "").strip()
        if not name:
            return []

        queries = []
        for tmpl in self._templates:
            try:
                queries.append(tmpl.format(name=name, company=company))
            except (KeyError, IndexError):
                continue
        queries = list(dict.fromkeys(queries))

        candidate_urls = self._collect_pdf_urls(queries)
        if not candidate_urls:
            return []

        seen_digits: set[str] = set()
        out: list[PhoneCandidate] = []
        for url, engine_label in candidate_urls[: self._max_pdfs]:
            text = self._download_and_extract(url)
            if not text:
                continue
            for match in self._phones_in(text, url, engine_label, name):
                if match.digits in seen_digits:
                    continue
                seen_digits.add(match.digits)
                out.append(
                    PhoneCandidate(
                        raw=match.raw,
                        source=self.name,
                        source_url=match.source_url,
                        context=(
                            "pdf_name_proximity" if match.has_proximity else "pdf"
                        ),
                        extra={"engine": match.engine},
                    )
                )
        return out

    def _collect_pdf_urls(self, queries: list[str]) -> list[tuple[str, str]]:
        urls: list[tuple[str, str]] = []
        seen: set[str] = set()
        for q in queries:
            for engine, label in zip(self._engines, self._engine_labels):
                try:
                    results = engine.search(q, max_results=self._max_results) or []
                except Exception as exc:
                    self.errors.append(f"{label}:{type(exc).__name__}:{exc}")
                    continue
                for r in results:
                    url = (r.url or "").strip()
                    if not _looks_like_pdf(url, r):
                        continue
                    if url in seen:
                        continue
                    seen.add(url)
                    urls.append((url, label))
        return urls

    def _download_and_extract(self, url: str) -> str:
        try:
            status, body, content_type = self._http_client(url)
        except Exception as exc:
            self.errors.append(f"{self.name}:download:{type(exc).__name__}:{exc}")
            return ""
        if status != 200 or not body:
            return ""
        if len(body) > self._max_bytes:
            return ""
        if "pdf" not in (content_type or "").lower() and not url.lower().endswith(".pdf"):
            # Some servers serve PDFs as ``application/octet-stream``. Try
            # the magic bytes as a last check before bailing.
            if not body.startswith(b"%PDF"):
                return ""

        try:
            from pdfminer.high_level import extract_text

            text = extract_text(io.BytesIO(body))
        except Exception as exc:
            self.errors.append(f"{self.name}:pdfminer:{type(exc).__name__}:{exc}")
            return ""
        return text or ""

    def _phones_in(
        self,
        text: str,
        source_url: str,
        engine_label: str,
        full_name: str,
    ) -> list[_PdfPhoneMatch]:
        if not text:
            return []
        # Split into "windows" of ~1500 chars so we can test name
        # proximity per chunk rather than over the entire document.
        # Documents like a 60-page event guide may have the lead's
        # name on page 4 and unrelated phones on page 30 — without
        # windowing every phone in the doc would be tagged proximity.
        windows = _chunk(text, 1500)
        name_lower = full_name.lower()
        first_last = _first_last(full_name)
        out: list[_PdfPhoneMatch] = []
        for window in windows:
            window_lower = window.lower()
            window_has_name = (
                name_lower in window_lower
                or (
                    first_last is not None
                    and first_last[0] in window_lower
                    and first_last[1] in window_lower
                )
            )
            for match in _PHONE_REGEX.finditer(window):
                raw = match.group(1).strip()
                digits = _digits_only(raw)
                if not _is_plausible_phone(digits):
                    continue
                out.append(
                    _PdfPhoneMatch(
                        raw=raw,
                        digits=digits,
                        source_url=source_url,
                        engine=engine_label,
                        has_proximity=window_has_name,
                    )
                )
        return out


def _looks_like_pdf(url: str, result: SearchResult) -> bool:
    """Best-effort filter for PDF-ish SERP hits.

    The filetype:pdf operator generally restricts to PDFs but some
    engines (especially the HTML scrapers) leak non-PDF results.
    """
    if not url:
        return False
    lower = url.lower()
    if lower.endswith(".pdf") or ".pdf?" in lower or ".pdf#" in lower:
        return True
    snippet = (result.snippet or "").lower()
    title = (result.title or "").lower()
    return ".pdf" in snippet or ".pdf" in title


def _chunk(text: str, window: int) -> list[str]:
    if not text:
        return []
    if window <= 0:
        return [text]
    return [text[i : i + window] for i in range(0, len(text), max(1, window // 2))]


def _first_last(name: str) -> tuple[str, str] | None:
    tokens = [t for t in (name or "").lower().split() if t]
    if len(tokens) < 2:
        return None
    return tokens[0], tokens[-1]


def _default_binary_client(timeout_seconds: float) -> HttpBinaryClient:
    def fetch(url: str) -> tuple[int, bytes, str]:
        import httpx

        try:
            response = httpx.get(
                url,
                timeout=timeout_seconds,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (compatible; BeautifulLinkedIn/1.0; "
                        "+https://github.com/beautiful-linkedin)"
                    ),
                    "Accept": "application/pdf,application/octet-stream,*/*",
                },
            )
        except Exception as exc:
            logger.debug("PdfPhoneExtractor http error %s: %s", url, exc)
            return (0, b"", "")
        return (
            response.status_code,
            response.content or b"",
            response.headers.get("content-type", ""),
        )

    return fetch
