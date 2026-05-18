"""Find e-mails published on a company's own website.

This is the Hunter.io-style technique adapted to the in-process enricher:
fetch the homepage plus a handful of likely-public pages on the same
domain, regex out e-mail addresses, and keep only the ones on the
company's own domain. The result feeds :func:`infer_pattern_from_locals`
so the pattern detector has primary evidence (a real e-mail published by
the company) instead of guessing from a single matching colleague.

External HTTP is injected as ``http_client(url) -> (status, html)`` so
tests can run completely offline. In production the default uses
``httpx`` with conservative timeouts and a custom user-agent.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable


logger = logging.getLogger(__name__)


HttpClient = Callable[[str], tuple[int, str]]


DEFAULT_PATHS: tuple[str, ...] = (
    "contato",
    "sobre",
    "team",
    "about",
    "contact",
    "equipe",
    "quem-somos",
)


# RFC-5322-lite: deliberately permissive so we catch the most common
# real-world forms in marketing pages. The strict validator runs later.
_EMAIL_REGEX = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", re.IGNORECASE
)


@dataclass(frozen=True)
class HarvestedEmail:
    email: str
    source_url: str


class CompanyEmailHarvester:
    """Fetch + regex-extract e-mails from a company's own website."""

    def __init__(
        self,
        *,
        http_client: HttpClient | None = None,
        paths: list[str] | tuple[str, ...] | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._http_client = http_client or _default_http_client(timeout_seconds)
        # ``None`` -> defaults; ``[]`` -> homepage only (handy for tests).
        self._paths = (
            tuple(DEFAULT_PATHS) if paths is None else tuple(paths)
        )

    def harvest(self, domain: str | None) -> list[HarvestedEmail]:
        cleaned = (domain or "").strip().lower()
        if not cleaned:
            return []

        scheme = "https://"
        base = f"{scheme}{cleaned.lstrip('/')}"
        urls = [f"{base}/"] + [f"{base}/{path.lstrip('/')}" for path in self._paths]

        domain_variants = _domain_variants(cleaned)
        seen_emails: set[str] = set()
        out: list[HarvestedEmail] = []
        for url in urls:
            html = self._fetch(url)
            if not html:
                continue
            for email in _extract_emails(html, domain_variants):
                if email in seen_emails:
                    continue
                seen_emails.add(email)
                out.append(HarvestedEmail(email=email, source_url=url))
        return out

    def _fetch(self, url: str) -> str:
        try:
            status, html = self._http_client(url)
        except Exception as exc:
            logger.debug("CompanyEmailHarvester: %s falhou (%s)", url, exc)
            return ""
        if status != 200:
            return ""
        return html or ""


def _domain_variants(domain: str) -> set[str]:
    """Return the set of host strings that count as 'the same company'.

    A site at ``empresa.com`` often uses both ``empresa.com`` and
    ``www.empresa.com`` interchangeably; we want emails on either to
    count, but third-party SaaS domains in their footer should be dropped.
    """
    bare = domain.lstrip("www.")
    return {domain, bare, f"www.{bare}"}


def _extract_emails(html: str, allowed_domains: set[str]) -> list[str]:
    """Pull e-mails out of HTML and keep only those on allowed domains."""
    emails: list[str] = []
    seen: set[str] = set()
    for match in _EMAIL_REGEX.finditer(html):
        candidate = match.group(0).lower()
        if candidate in seen:
            continue
        seen.add(candidate)
        local, _, host = candidate.rpartition("@")
        if not local or not host:
            continue
        # Strip a trailing dot/comma that the regex sometimes captures
        # when the e-mail is followed by punctuation in prose.
        host = host.rstrip(".,;:)")
        if host in allowed_domains:
            emails.append(f"{local}@{host}")
    return emails


def _default_http_client(timeout_seconds: float) -> HttpClient:
    """Build a real httpx-backed client; isolated so tests skip it entirely."""

    def fetch(url: str) -> tuple[int, str]:
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
                    )
                },
            )
        except Exception as exc:
            logger.debug("CompanyEmailHarvester http error %s: %s", url, exc)
            return (0, "")
        return (response.status_code, response.text or "")

    return fetch
