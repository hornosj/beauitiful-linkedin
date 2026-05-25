"""Bucket A+: discover company-line phones from Receita Federal public data.

The Receita Federal publishes the full CNPJ registry as monthly open
data. The two practical free wrappers are:

- ``https://brasilapi.com.br/api/cnpj/v1/{cnpj}`` — light rate limit,
  no auth, returns ``ddd_telefone_1`` / ``ddd_telefone_2`` /
  ``correio_eletronico`` and the partner (sócio) list.
- ``https://minhareceita.org/{cnpj}`` — same shape, no rate limit.

Resolving the lead's company → CNPJ is the non-trivial bit. We don't
ship a private mapping table; instead:

1. If the lead already carries a ``company_cnpj`` (some providers fill
   it), use it directly.
2. Otherwise, ask any configured :class:`SearchEngine` for ``"{empresa}"
   CNPJ`` and look for a 14-digit string in the first results.
3. Cache resolved (company_key → cnpj) for the rest of the run so the
   384 leads at Nubank cost one resolution, not 384.

This is the cleanest possible source: every byte returned was already
published by Receita Federal. The trade-off is that we get **company
phones**, not direct individual mobiles — but it covers the
"institutional fallback" with near-100% match for active CNPJs and
zero gray-zone exposure.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Callable

from beautiful_linkedin.search.search_engine import SearchEngine
from beautiful_linkedin.storage.phone_lookup import (
    LookupQuery,
    PhoneCandidate,
)


logger = logging.getLogger(__name__)


HttpClient = Callable[[str], tuple[int, str]]
"""``http_client(url) -> (status_code, body_text)``."""


# Permissive CNPJ matcher: accepts ``00.000.000/0000-00`` formatted and
# ``00000000000000`` raw. No ``\b`` boundary because search snippets
# often surround the CNPJ with punctuation or HTML entities that
# break ``\b`` (e.g. ``CNPJ:14.380.200/0001-21,``).
_CNPJ_DIGITS = re.compile(
    r"(?<![\d])(\d{2}[\.\-/]?\d{3}[\.\-/]?\d{3}[\.\-/]?\d{4}[\.\-/]?\d{2})(?![\d])"
)


@dataclass(frozen=True)
class _ResolvedCompany:
    """Outcome of resolving a company name to its Receita Federal CNPJ.

    ``cnpj`` is the 14-digit canonical form (no punctuation); ``found_via``
    records which path landed it for diagnostics.
    """

    cnpj: str
    found_via: str   # "lead" | "search" | "cache"


class ReceitaCnpjLookupProvider:
    """Phone lookup via Receita Federal CNPJ open data.

    For each lead, resolve the employer's CNPJ (using the lead's
    explicit value when present, falling back to a SERP-mediated
    search) and then fetch the public registry. Returns one or two
    candidates per company — the registered telephone fields.

    Two backends supported, in order: ``brasilapi`` (default) and
    ``minhareceita``. If the first 4xx/5xx, the second is tried. The
    HTTP client is injectable for tests.
    """

    name = "receita_cnpj"

    def __init__(
        self,
        *,
        http_client: HttpClient | None = None,
        search_engines: list[SearchEngine] | None = None,
        backends: tuple[str, ...] = ("brasilapi", "minhareceita"),
        timeout_seconds: float = 8.0,
    ) -> None:
        self._http_client = http_client or _default_http_client(timeout_seconds)
        self._engines = list(search_engines or [])
        self._backends = tuple(backends)
        self._cnpj_cache: dict[str, _ResolvedCompany | None] = {}
        self._cnpj_response_cache: dict[str, dict | None] = {}
        self._lock = threading.Lock()
        self.errors: list[str] = []

    def lookup(self, query: LookupQuery) -> list[PhoneCandidate]:
        company = (query.company_name or "").strip()
        if not company:
            return []

        resolved = self._resolve_cnpj(company)
        if resolved is None:
            return []

        data = self._fetch_cnpj_data(resolved.cnpj)
        if not data:
            return []

        out: list[PhoneCandidate] = []
        for field in ("ddd_telefone_1", "ddd_telefone_2"):
            raw = (data.get(field) or "").strip()
            if not raw:
                continue
            out.append(
                PhoneCandidate(
                    raw=raw,
                    source=self.name,
                    source_url=f"https://brasilapi.com.br/api/cnpj/v1/{resolved.cnpj}",
                    context="receita_cnpj",
                    extra={
                        "cnpj": resolved.cnpj,
                        "found_via": resolved.found_via,
                        "razao_social": str(data.get("razao_social") or ""),
                        "nome_fantasia": str(data.get("nome_fantasia") or ""),
                    },
                )
            )
        return out

    # ----- CNPJ resolution -------------------------------------------------

    def _resolve_cnpj(self, company: str) -> _ResolvedCompany | None:
        key = _normalize_key(company)
        with self._lock:
            if key in self._cnpj_cache:
                cached = self._cnpj_cache[key]
                if cached is None:
                    return None
                return _ResolvedCompany(cnpj=cached.cnpj, found_via="cache")

        resolved = self._search_for_cnpj(company)
        with self._lock:
            self._cnpj_cache[key] = resolved
        return resolved

    def _search_for_cnpj(self, company: str) -> _ResolvedCompany | None:
        if not self._engines:
            return None
        queries = [
            f'"{company}" CNPJ',
            f'"{company}" "CNPJ"',
        ]
        for q in queries:
            for engine in self._engines:
                try:
                    results = engine.search(q, max_results=8) or []
                except Exception as exc:
                    self.errors.append(
                        f"{self.name}:search:{type(engine).__name__}:{exc}"
                    )
                    continue
                for r in results:
                    text = " ".join(
                        part for part in (r.title or "", r.snippet or "", r.url or "")
                    )
                    cnpj = _first_cnpj_in(text)
                    if cnpj:
                        return _ResolvedCompany(cnpj=cnpj, found_via="search")
        return None

    # ----- Receita fetch ---------------------------------------------------

    def _fetch_cnpj_data(self, cnpj: str) -> dict | None:
        with self._lock:
            cached = self._cnpj_response_cache.get(cnpj)
        if cached is not None:
            return cached
        for backend in self._backends:
            url = _backend_url(backend, cnpj)
            if url is None:
                continue
            data = self._fetch_safe(url)
            if data:
                with self._lock:
                    self._cnpj_response_cache[cnpj] = data
                return data
        with self._lock:
            self._cnpj_response_cache[cnpj] = None
        return None

    def _fetch_safe(self, url: str) -> dict | None:
        try:
            status, body = self._http_client(url)
        except Exception as exc:
            self.errors.append(f"{self.name}:fetch:{type(exc).__name__}:{exc}")
            return None
        if status != 200 or not body:
            return None
        try:
            import json

            data = json.loads(body)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        return data


def _backend_url(backend: str, cnpj: str) -> str | None:
    if backend == "brasilapi":
        return f"https://brasilapi.com.br/api/cnpj/v1/{cnpj}"
    if backend == "minhareceita":
        return f"https://minhareceita.org/{cnpj}"
    return None


def _first_cnpj_in(text: str) -> str | None:
    """Pull the first CNPJ-shaped substring out of free text.

    Match accepts the formatted and unformatted forms. Validation of
    the check digits is intentionally skipped — Receita's endpoints
    will return 404 on a fake CNPJ, which is a cheaper filter than
    re-implementing the check-digit math here.
    """
    for match in _CNPJ_DIGITS.finditer(text or ""):
        digits = "".join(ch for ch in match.group(1) if ch.isdigit())
        if len(digits) == 14:
            return digits
    return None


def _normalize_key(name: str) -> str:
    return " ".join((name or "").lower().split())


def _default_http_client(timeout_seconds: float) -> HttpClient:
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
                    ),
                    "Accept": "application/json",
                },
            )
        except Exception as exc:
            logger.debug("ReceitaCnpjLookupProvider http error %s: %s", url, exc)
            return (0, "")
        return (response.status_code, response.text or "")

    return fetch
