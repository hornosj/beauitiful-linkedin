"""Find phone numbers published on a company's own website.

Bucket A of the phone discovery proposal — same shape as
``CompanyEmailHarvester``: fetch the homepage plus a handful of public
pages on the same domain, pull out phone numbers in every common
encoding (E.164, BR pretty-print, ``tel:`` links, ``wa.me`` links,
schema.org JSON-LD), and return them paired with the URL they came from
so downstream code can show provenance.

External HTTP is injected as ``http_client(url) -> (status, html)`` so
tests can run fully offline. The harvester never validates the number —
that's :mod:`beautiful_linkedin.storage.phone_validation`'s job. We just
collect candidates and let the validator decide.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Callable, Iterable


logger = logging.getLogger(__name__)


HttpClient = Callable[[str], tuple[int, str]]


DEFAULT_PATHS: tuple[str, ...] = (
    "contato",
    "contact",
    "sobre",
    "about",
    "team",
    "equipe",
    "leadership",
    "fale-conosco",
    "quem-somos",
)


# Permissive phone regex. We deliberately accept dirty input ("Tel.: +55
# (11) 99999-9999 ramal 1234") and rely on the validator to canonicalize.
# Constraints:
# - Must start with an optional ``+`` followed by 7-15 digits/separators.
# - Separators allowed inside: spaces, dots, hyphens, parentheses.
# - At least 8 digits total (drops 4-digit ramal-only mentions).
_PHONE_REGEX = re.compile(
    r"""
    (?<![A-Za-z0-9])             # left boundary: not a letter/digit
    (
        \+?                       # optional leading +
        (?:\(?\d{1,4}\)?[\s.\-]?){1,4}
        \d{2,5}                   # mid block
        [\s.\-]?
        \d{3,5}                   # tail block
    )
    (?![A-Za-z])                 # right boundary: not a letter
    """,
    re.VERBOSE,
)


_TEL_HREF = re.compile(r"""tel:\s*([+0-9()\s.\-]{6,40})""", re.IGNORECASE)

# ``wa.me/<digits>`` and ``api.whatsapp.com/send?phone=<digits>``.
# The slash after ``wa.me`` is consumed inside a non-capturing group so
# only the digit run lands in group(1); the ``phone=`` form has no
# slash between the marker and the number.
_WHATSAPP_HREF = re.compile(
    r"""(?:wa\.me/|api\.whatsapp\.com/send\?[^"'\s]*?phone=)([+0-9()\s.\-]{6,40})""",
    re.IGNORECASE,
)

# ``"telephone": "+55..."`` inside JSON-LD blocks. We don't parse JSON-LD
# strictly — a regex over the raw HTML catches both ``"telephone"`` and
# the rarer ``"telephoneNumber"`` without us needing to find every
# ``<script type="application/ld+json">`` boundary.
_JSONLD_PHONE = re.compile(
    r'"telephone(?:Number)?"\s*:\s*"([^"]{6,40})"', re.IGNORECASE
)

# Numbers we never want to surface as a person's phone even if the page
# happens to mention them. The validator drops these too as a second
# gate, but skipping them at extraction time keeps noise low.
_CALL_CENTER_PREFIXES: tuple[str, ...] = ("0800", "4004", "3003", "0300")


@dataclass(frozen=True)
class HarvestedPhone:
    """One phone candidate plus where we found it.

    ``raw`` is the substring as it appeared in the page (useful for the
    UI to show "found 'Tel: (11) 99999-9999' on /contato"); ``digits``
    is the normalized digit-only form used for de-duplication during
    harvesting. Final E.164 normalization happens in the validator.
    """

    raw: str
    digits: str
    source_url: str
    context: str  # one of: "tel", "whatsapp", "jsonld", "text"


class PhoneNumberHarvester:
    """Fetch + regex-extract phone numbers from a company's own website."""

    def __init__(
        self,
        *,
        http_client: HttpClient | None = None,
        paths: list[str] | tuple[str, ...] | None = None,
        timeout_seconds: float = 5.0,
        max_per_page: int = 30,
    ) -> None:
        self._http_client = http_client or _default_http_client(timeout_seconds)
        self._paths = (
            tuple(DEFAULT_PATHS) if paths is None else tuple(paths)
        )
        self._max_per_page = max(1, max_per_page)

    def harvest(self, domain: str | None) -> list[HarvestedPhone]:
        cleaned = (domain or "").strip().lower()
        if not cleaned:
            return []

        base = f"https://{cleaned.lstrip('/')}"
        urls = [f"{base}/"] + [f"{base}/{path.lstrip('/')}" for path in self._paths]

        seen_digits: set[str] = set()
        out: list[HarvestedPhone] = []
        for url in urls:
            html = self._fetch(url)
            if not html:
                continue
            for candidate in _extract_phones(html, url):
                if candidate.digits in seen_digits:
                    continue
                seen_digits.add(candidate.digits)
                out.append(candidate)
                if len(out) >= self._max_per_page * len(urls):
                    return out
        return out

    def _fetch(self, url: str) -> str:
        try:
            status, html = self._http_client(url)
        except Exception as exc:
            logger.debug("PhoneNumberHarvester: %s falhou (%s)", url, exc)
            return ""
        if status != 200:
            return ""
        return html or ""


def _extract_phones(html: str, source_url: str) -> Iterable[HarvestedPhone]:
    """Pull phones out of one HTML page, tagging each by context."""
    seen: set[str] = set()

    # Order matters: structured signals (tel:, wa.me, JSON-LD) win over
    # the generic text regex because they're less prone to false
    # positives like CNPJ / IBGE numbers caught by raw digit hunting.
    for context, regex in (
        ("tel", _TEL_HREF),
        ("whatsapp", _WHATSAPP_HREF),
        ("jsonld", _JSONLD_PHONE),
    ):
        for match in regex.finditer(html):
            raw = match.group(1).strip()
            digits = _digits_only(raw)
            if not _is_plausible_phone(digits):
                continue
            if digits in seen:
                continue
            seen.add(digits)
            yield HarvestedPhone(
                raw=raw, digits=digits, source_url=source_url, context=context
            )

    # Plain-text regex last; skip anything we already saw via the
    # structured paths.
    for match in _PHONE_REGEX.finditer(_strip_scripts(html)):
        raw = match.group(1).strip()
        digits = _digits_only(raw)
        if not _is_plausible_phone(digits):
            continue
        if digits in seen:
            continue
        seen.add(digits)
        yield HarvestedPhone(
            raw=raw, digits=digits, source_url=source_url, context="text"
        )


def _strip_scripts(html: str) -> str:
    """Remove <script> and <style> blocks before running the loose regex.

    JSON-LD payloads inside scripts already get picked up by
    :data:`_JSONLD_PHONE`; running the permissive regex over them
    duplicates work and risks matching JS timestamps / cache buster
    integers as "phones".
    """
    no_scripts = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    no_styles = re.sub(r"<style[^>]*>.*?</style>", " ", no_scripts, flags=re.IGNORECASE | re.DOTALL)
    return no_styles


def _digits_only(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _is_plausible_phone(digits: str) -> bool:
    """First-pass filter so we don't flood the validator with garbage.

    Real BR mobile is 10-13 digits (with or without country code). Real
    international E.164 is at most 15 digits. Anything outside that
    range is either a CEP, CNPJ, IBGE code, or a tracking pixel id.
    """
    if not (8 <= len(digits) <= 15):
        return False
    if digits.startswith(_CALL_CENTER_PREFIXES):
        return False
    # Reject sequences of a single repeating digit (e.g. "00000000000",
    # "11111111" — almost always placeholder values).
    if len(set(digits)) <= 1:
        return False
    return True


def _default_http_client(timeout_seconds: float) -> HttpClient:
    """Build the real httpx-backed client. Isolated so tests skip it."""

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
            logger.debug("PhoneNumberHarvester http error %s: %s", url, exc)
            return (0, "")
        return (response.status_code, response.text or "")

    return fetch


# JSON-LD walker kept as a convenience for callers that already parsed
# a page and want richer extraction. The default regex pipeline above
# is enough for the harvester loop; this is exposed for tests and for
# future page-aware extractors (e.g. the PDF extractor).
def iter_jsonld_phones(html: str) -> list[str]:
    """Walk every JSON-LD payload and yield ``telephone`` values."""
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    out: list[str] = []
    for block in blocks:
        try:
            payload = json.loads(block)
        except Exception:
            continue
        out.extend(_walk_jsonld(payload))
    return out


def _walk_jsonld(node: object) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key.lower() in {"telephone", "telephonenumber"}:
                if isinstance(value, str):
                    found.append(value)
            elif isinstance(value, (dict, list)):
                found.extend(_walk_jsonld(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_jsonld(item))
    return found
