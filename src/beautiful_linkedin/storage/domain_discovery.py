"""Free domain-discovery sources for the internal enrichment pipeline.

We start each run with a seed domain per company (from existing emails or
the ``company_domain`` column). Many companies have several mail-bearing
domains the seed alone never hits: ``acme.io`` for the product, regional
``.com.br``/``.co.uk`` sites, sister brands. This module finds those
without any paid API.

Three sources, each independent and cheap:

1. ``CrtShClient`` — queries https://crt.sh, the public Certificate
   Transparency mirror. Every TLS cert ever issued lives there; the
   SubjectAltName field on a single cert often lists every sibling
   hostname the company has ever served.

2. ``SpfDmarcDiscoverer`` — pulls ``TXT seed`` and ``TXT _dmarc.seed``,
   parses ``include:``/``redirect=`` from SPF and ``rua=mailto:`` from
   DMARC. Both reveal mail-related sister domains explicitly authorized
   by the company.

3. ``CctldVariantGenerator`` — purely local: from ``acme.com`` it
   emits ``acme.com.br``, ``acme.io``, etc. Static permutations gated by
   an MX check downstream so we never test garbage.

The three are composed by :class:`DomainDiscoveryService`. Each
discoverer accepts an injectable client/resolver so tests run offline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol


HttpFetcher = Callable[[str], tuple[int, str]]
TxtResolver = Callable[[str], list[str]]
MxChecker = Callable[[str], bool]


# Common second-level TLDs used by ccTLDs that take "{name}.{sld}.{cc}".
# When we strip the TLD off a seed we have to consider both single-label
# TLDs (``.com``) and these compound ones.
_COMPOUND_TLDS: tuple[str, ...] = (
    "com.br",
    "co.uk",
    "co.jp",
    "com.au",
    "co.za",
    "com.mx",
    "com.ar",
    "co.nz",
    "com.cn",
    "com.tr",
    "co.in",
    "com.pt",
    "com.es",
)


# Default TLD set the variant generator probes. Tight by design — adding
# more here multiplies DNS queries per company without adding much real
# coverage. Skewed toward the markets the user actually prospects in.
DEFAULT_CCTLD_VARIANTS: tuple[str, ...] = (
    "com",
    "com.br",
    "io",
    "co",
    "net",
    "app",
    "dev",
    "ai",
    "tech",
    "co.uk",
)


@dataclass
class DiscoveryResult:
    """What a single discovery run produced for one seed domain.

    Kept granular (``sources`` is a per-source counter) so the UI can
    show "found 7 new domains: 4 via CT, 2 via SPF, 1 via ccTLD".
    """

    seed: str
    domains: list[str] = field(default_factory=list)
    sources: dict[str, list[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# crt.sh
# ---------------------------------------------------------------------------


# crt.sh's ``name_value`` field can carry multiple newline-separated hosts
# per cert row, plus wildcards like ``*.acme.com``. We strip the wildcard
# prefix and keep apex+subdomain alike — the orchestrator will dedupe.
_NAMEVALUE_SPLIT = re.compile(r"[\s,]+")


class CrtShClient:
    """Single-call Certificate Transparency lookup via crt.sh.

    The endpoint returns a JSON array of certificate rows; each row has
    a ``name_value`` field listing every DNS name on the cert. SANs are
    the goldmine — a company that ever issued one TLS cert covering
    ``acme.com`` + ``acme.io`` has handed us the sister domain for free.
    """

    BASE_URL = "https://crt.sh"

    def __init__(
        self,
        *,
        http_fetcher: HttpFetcher,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._fetcher = http_fetcher
        self._timeout = timeout_seconds

    def query(self, seed: str) -> list[str]:
        """Return distinct hostnames seen across every cert that included
        ``seed``. Wildcards are flattened to their parent (``*.acme.com``
        → ``acme.com``). Empty list on any error — discovery is best-effort.
        """
        import json

        seed = (seed or "").strip().lower()
        if not seed or "." not in seed:
            return []

        url = f"{self.BASE_URL}/?q={seed}&output=json"
        try:
            status, body = self._fetcher(url)
        except Exception:
            return []
        if status != 200 or not body:
            return []
        try:
            rows = json.loads(body)
        except Exception:
            return []
        if not isinstance(rows, list):
            return []

        out: list[str] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw = row.get("name_value")
            if not isinstance(raw, str):
                continue
            for token in _NAMEVALUE_SPLIT.split(raw):
                cleaned = _normalize_hostname(token)
                if cleaned and cleaned not in seen:
                    seen.add(cleaned)
                    out.append(cleaned)
        return out


def _normalize_hostname(token: str) -> str:
    token = (token or "").strip().lower().lstrip("*.")
    if not token or " " in token or "@" in token:
        return ""
    # Strip stray trailing dots and ports.
    token = token.rstrip(".")
    if ":" in token:
        token = token.split(":", 1)[0]
    # Must look like a domain — at least one dot, allowed charset.
    if "." not in token:
        return ""
    # Underscore is legal in DNS labels (SPF helpers, DKIM selectors).
    # The MX gate downstream filters anything not actually receiving mail.
    if not re.fullmatch(r"[a-z0-9._\-]+", token):
        return ""
    return token


# ---------------------------------------------------------------------------
# SPF + DMARC TXT records
# ---------------------------------------------------------------------------


_SPF_TOKEN = re.compile(r"(?:include|redirect|exists)[=:]([A-Za-z0-9.\-_%{}]+)")
_DMARC_MAILTO = re.compile(r"mailto:([^,;\s]+)", re.IGNORECASE)


class SpfDmarcDiscoverer:
    """Pull mail-related sister domains from DNS policy records.

    SPF ``include:`` and ``redirect=`` list domains the company allows
    to send mail on its behalf — almost always sibling brands or owned
    infrastructure. DMARC ``rua=`` / ``ruf=`` reporting addresses very
    often live on a sister domain too.
    """

    def __init__(self, *, txt_resolver: TxtResolver) -> None:
        self._txt = txt_resolver

    def discover(self, seed: str) -> list[str]:
        seed = (seed or "").strip().lower()
        if not seed or "." not in seed:
            return []
        out: list[str] = []
        seen: set[str] = set()

        for record in self._safe_txt(seed):
            if not record.lower().startswith("v=spf1"):
                continue
            for match in _SPF_TOKEN.finditer(record):
                domain = _normalize_hostname(match.group(1))
                if domain and domain != seed and domain not in seen:
                    seen.add(domain)
                    out.append(domain)

        for record in self._safe_txt(f"_dmarc.{seed}"):
            if not record.lower().startswith("v=dmarc1"):
                continue
            for match in _DMARC_MAILTO.finditer(record):
                addr = match.group(1).strip()
                if "@" not in addr:
                    continue
                domain = _normalize_hostname(addr.split("@", 1)[1])
                if domain and domain != seed and domain not in seen:
                    seen.add(domain)
                    out.append(domain)
        return out

    def _safe_txt(self, name: str) -> list[str]:
        try:
            return [r for r in self._txt(name) if isinstance(r, str)]
        except Exception:
            return []


# ---------------------------------------------------------------------------
# ccTLD variant generator
# ---------------------------------------------------------------------------


class CctldVariantGenerator:
    """Permute a seed domain across a curated TLD list.

    Given ``acme.com`` and the default TLD set, emits
    ``acme.com.br``, ``acme.io``, ``acme.co.uk``, etc. The output is
    intentionally noisy — every variant is later gated by an MX check
    inside :class:`DomainDiscoveryService`, so we only keep the ones
    that actually receive mail.
    """

    def __init__(self, *, tlds: Iterable[str] = DEFAULT_CCTLD_VARIANTS) -> None:
        self._tlds = tuple(dict.fromkeys(t.strip().lower() for t in tlds if t))

    def variants(self, seed: str) -> list[str]:
        core = _extract_core(seed)
        if not core:
            return []
        out: list[str] = []
        seen: set[str] = {seed.strip().lower()}
        for tld in self._tlds:
            candidate = f"{core}.{tld}"
            if candidate not in seen:
                seen.add(candidate)
                out.append(candidate)
        return out


def _extract_core(seed: str) -> str:
    """Return the labels of ``seed`` before its TLD.

    ``acme.com``     → ``"acme"``
    ``acme.com.br``  → ``"acme"`` (compound TLD recognized)
    ``app.acme.io``  → ``"app.acme"`` (preserves subdomains by design —
        a company that lives under a sub may use it as a brand name)
    """
    seed = (seed or "").strip().lower().rstrip(".")
    if not seed or "." not in seed:
        return ""
    for compound in _COMPOUND_TLDS:
        suffix = f".{compound}"
        if seed.endswith(suffix):
            return seed[: -len(suffix)]
    return seed.rsplit(".", 1)[0]


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


class _DiscovererProto(Protocol):
    def query(self, seed: str) -> list[str]: ...


class DomainDiscoveryService:
    """Run every free discoverer and gate the result by MX existence.

    The MX filter is the deciding step — discovery surfaces dozens of
    plausible-looking hostnames per company, but only the ones that
    actually receive mail are worth handing to the validator. We cache
    MX answers per call so repeats across discoverers don't multiply
    DNS roundtrips.
    """

    def __init__(
        self,
        *,
        crt_sh_client: CrtShClient | None = None,
        spf_dmarc: SpfDmarcDiscoverer | None = None,
        cctld_generator: CctldVariantGenerator | None = None,
        mx_checker: MxChecker | None = None,
        max_per_source: int = 25,
    ) -> None:
        self._crt = crt_sh_client
        self._spf = spf_dmarc
        self._cctld = cctld_generator
        self._mx = mx_checker
        self._max_per_source = max(1, max_per_source)

    def discover(self, seed: str) -> DiscoveryResult:
        seed = (seed or "").strip().lower()
        result = DiscoveryResult(seed=seed)
        if not seed or "." not in seed:
            return result

        # ---- collect raw candidates per source --------------------------
        raw_by_source: dict[str, list[str]] = {}
        if self._crt is not None:
            raw_by_source["crt_sh"] = self._safe(self._crt.query, seed)[
                : self._max_per_source
            ]
        if self._spf is not None:
            raw_by_source["spf_dmarc"] = self._safe(self._spf.discover, seed)[
                : self._max_per_source
            ]
        if self._cctld is not None:
            raw_by_source["cctld"] = self._safe(self._cctld.variants, seed)[
                : self._max_per_source
            ]

        # ---- MX gate (shared cache so repeats across sources are free) --
        mx_cache: dict[str, bool] = {seed: True}  # trust the seed itself
        accepted: list[str] = []
        accepted_set: set[str] = set()

        for source, candidates in raw_by_source.items():
            kept: list[str] = []
            for domain in candidates:
                if domain == seed or domain in accepted_set:
                    continue
                has_mx = mx_cache.get(domain)
                if has_mx is None:
                    has_mx = self._has_mx(domain)
                    mx_cache[domain] = has_mx
                if has_mx:
                    accepted.append(domain)
                    accepted_set.add(domain)
                    kept.append(domain)
            result.sources[source] = kept

        result.domains = accepted
        return result

    def _has_mx(self, domain: str) -> bool:
        if self._mx is None:
            # No gate configured → trust the discoverer. Safe in tests
            # that already inject a no-MX scenario.
            return True
        try:
            return bool(self._mx(domain))
        except Exception:
            return False

    @staticmethod
    def _safe(fn: Callable[[str], list[str]], seed: str) -> list[str]:
        try:
            return list(fn(seed) or [])
        except Exception:
            return []
