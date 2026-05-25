"""Free active-presence probe via ``wa.me``.

Meta's ``https://wa.me/<digits>`` endpoint serves different content
depending on whether the digits map to an active WhatsApp account:

- **Active account:** the page renders a "Continue to chat" / "Iniciar
  conversa" CTA with the account's phone formatted, plus a deep link
  to ``whatsapp://send?phone=...``. The full URL is present in the
  HTML.
- **Inactive / non-existent number:** Meta serves a generic landing
  page ("Phone number shared via URL is invalid"). The CTA and the
  ``whatsapp://send`` deep link are absent.

This is **not** an official API and the page markup will change
eventually. The checker is therefore:

- Fail-soft (network/parse errors → ``UNKNOWN``, never raise).
- Cache-aggressive (per-number, per process) so re-checking the same
  number is free.
- Rate-limited via an injectable sleep so callers can throttle a batch
  without coupling the policy here.

Used by :class:`PhoneValidator` as an optional post-format probe and
by the orchestrator as a confidence booster: a candidate that
WhatsApp confirms gets a meaningful score bump.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable


logger = logging.getLogger(__name__)


HttpClient = Callable[[str], tuple[int, str, str]]
"""``http_client(url) -> (status_code, final_url, html)``.

``final_url`` is the URL after redirects — for wa.me this lets us
distinguish "redirected to api.whatsapp.com/send?phone=..." (the
positive signal) from "redirected to the generic landing page".
"""


class WhatsAppStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"          # network/parse failure
    INVALID_FORMAT = "invalid"   # digits didn't make a possible E.164


@dataclass(frozen=True)
class WhatsAppCheck:
    status: WhatsAppStatus
    reason: str = ""
    checked_url: str | None = None


# Positive-signal markers we look for in the response. Meta updates the
# wa.me landing page periodically; we look for a *combination* of
# markers so a single string rename doesn't flip every number from
# ACTIVE to INACTIVE silently.
_POSITIVE_MARKERS = (
    "whatsapp://send",
    "/send?phone=",
    "iniciar conversa",
    "continue to chat",
    "abrir no whatsapp",
    "open in whatsapp",
)

# Strong negative markers. When the page contains any of these AND no
# positive marker, we call it INACTIVE.
_NEGATIVE_MARKERS = (
    "número de telefone compartilhado",
    "phone number shared via url",
    "número de teléfono compartido",
)


class WhatsAppNumberChecker:
    """``wa.me``-based presence probe.

    Construction injects the HTTP client + sleep function so tests
    stay fully offline. The default client uses ``httpx`` with the
    same conservative User-Agent the other harvesters use.
    """

    def __init__(
        self,
        *,
        http_client: HttpClient | None = None,
        sleep: Callable[[float], None] = time.sleep,
        request_interval_seconds: float = 0.5,
        timeout_seconds: float = 6.0,
    ) -> None:
        self._http_client = http_client or _default_http_client(timeout_seconds)
        self._sleep = sleep
        self._interval = max(0.0, request_interval_seconds)
        self._cache: dict[str, WhatsAppCheck] = {}
        self._lock = threading.Lock()
        self._last_request_at = 0.0

    def check(self, e164: str) -> WhatsAppCheck:
        digits = _digits_only(e164 or "")
        if not (8 <= len(digits) <= 15):
            return WhatsAppCheck(
                status=WhatsAppStatus.INVALID_FORMAT,
                reason="bad_length",
            )

        with self._lock:
            cached = self._cache.get(digits)
        if cached is not None:
            return cached

        self._throttle()
        url = f"https://wa.me/{digits}"
        try:
            status_code, final_url, html = self._http_client(url)
        except Exception as exc:
            logger.debug("wa.me check %s falhou: %s", digits, exc)
            result = WhatsAppCheck(
                status=WhatsAppStatus.UNKNOWN,
                reason=f"http_error:{type(exc).__name__}",
                checked_url=url,
            )
            self._cache_result(digits, result)
            return result

        result = _classify(status_code, final_url, html, url)
        self._cache_result(digits, result)
        return result

    def __call__(self, e164: str) -> WhatsAppCheck:
        return self.check(e164)

    def _throttle(self) -> None:
        if self._interval <= 0:
            return
        with self._lock:
            elapsed = time.monotonic() - self._last_request_at
            wait = self._interval - elapsed
            self._last_request_at = time.monotonic() + max(0.0, wait)
        if wait > 0:
            self._sleep(wait)

    def _cache_result(self, digits: str, result: WhatsAppCheck) -> None:
        with self._lock:
            self._cache[digits] = result


def _classify(
    status_code: int,
    final_url: str | None,
    html: str | None,
    requested_url: str,
) -> WhatsAppCheck:
    """Map an HTTP response to a status.

    Decision tree, mirroring observed Meta behavior in May 2026:

    1. Status >= 500 → UNKNOWN (Meta blip; don't conclude either way).
    2. Status 4xx → INACTIVE if 404; UNKNOWN otherwise.
    3. Final URL contains "/send?phone=" → ACTIVE (the canonical
       positive redirect target).
    4. HTML contains any positive marker → ACTIVE.
    5. HTML contains any negative marker → INACTIVE.
    6. Otherwise → UNKNOWN. We refuse to conclude on ambiguous pages
       so we don't silently downgrade good numbers when Meta changes
       the markup.
    """
    if status_code >= 500:
        return WhatsAppCheck(
            status=WhatsAppStatus.UNKNOWN,
            reason=f"http_{status_code}",
            checked_url=requested_url,
        )
    if status_code == 404:
        return WhatsAppCheck(
            status=WhatsAppStatus.INACTIVE,
            reason="http_404",
            checked_url=requested_url,
        )
    if 400 <= status_code < 500:
        return WhatsAppCheck(
            status=WhatsAppStatus.UNKNOWN,
            reason=f"http_{status_code}",
            checked_url=requested_url,
        )

    final = (final_url or "").lower()
    if "send?phone=" in final or "wa.me/" not in final:
        # wa.me typically redirects to api.whatsapp.com/send?phone=...
        # for active numbers. Anything that *leaves* wa.me with phone
        # in the query string is a strong positive signal.
        if "phone=" in final:
            return WhatsAppCheck(
                status=WhatsAppStatus.ACTIVE,
                reason="redirect_to_send",
                checked_url=final,
            )

    if not html:
        return WhatsAppCheck(
            status=WhatsAppStatus.UNKNOWN,
            reason="empty_body",
            checked_url=final or requested_url,
        )
    html_lower = html.lower()
    has_positive = any(marker in html_lower for marker in _POSITIVE_MARKERS)
    has_negative = any(marker in html_lower for marker in _NEGATIVE_MARKERS)
    if has_positive and not has_negative:
        return WhatsAppCheck(
            status=WhatsAppStatus.ACTIVE,
            reason="positive_marker",
            checked_url=final or requested_url,
        )
    if has_negative and not has_positive:
        return WhatsAppCheck(
            status=WhatsAppStatus.INACTIVE,
            reason="negative_marker",
            checked_url=final or requested_url,
        )
    return WhatsAppCheck(
        status=WhatsAppStatus.UNKNOWN,
        reason="ambiguous_body",
        checked_url=final or requested_url,
    )


def _digits_only(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _default_http_client(timeout_seconds: float) -> HttpClient:
    """Build the real httpx-backed client. Isolated so tests skip it."""

    def fetch(url: str) -> tuple[int, str, str]:
        import httpx

        try:
            response = httpx.get(
                url,
                timeout=timeout_seconds,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0 Safari/537.36"
                    ),
                    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
                },
            )
        except Exception as exc:
            logger.debug("wa.me http error %s: %s", url, exc)
            return (0, "", "")
        final_url = str(response.url) if response.url is not None else ""
        return (response.status_code, final_url, response.text or "")

    return fetch
