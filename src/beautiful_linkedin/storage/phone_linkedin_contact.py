"""LinkedIn contact-info phone lookup (opt-in, cookie-based).

LinkedIn exposes a per-profile "Contact Info" endpoint that returns
whatever the user *chose* to publish — phone numbers, emails,
websites, IM handles. The provider only fires when:

1. A valid ``li_at`` cookie + CSRF token are configured for the
   project (same credentials the existing :mod:`linkedin_cookie`
   provider uses); and
2. The lead row has a ``linkedin_url`` we can map to a profile
   public identifier.

The data is consent-based by design: LinkedIn users explicitly
toggle which fields are public. Pulling these does not bypass any
privacy control. Risk profile is identical to the existing cookie
provider (cookie can be rate-limited or invalidated). Defaults to
*disabled*; the server's DI factory returns ``None`` unless cookie
material is available.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from beautiful_linkedin.storage.phone_lookup import (
    LookupQuery,
    PhoneCandidate,
)


logger = logging.getLogger(__name__)


VoyagerFetcher = Callable[[str], dict[str, Any] | None]
"""``fetch(public_id) -> contact_info_json | None``.

Injected so tests can run without LinkedIn. The default implementation
constructs an :class:`httpx.Client` with the project's existing
``li_at`` + ``csrf`` headers and calls
``/voyager/api/identity/profiles/{public_id}/profileContactInfo``.
"""


_PUBLIC_ID_REGEX = re.compile(r"linkedin\.com/in/([^/?#]+)", re.IGNORECASE)


@dataclass(frozen=True)
class _ContactInfo:
    phones: list[str]
    websites: list[str]


class LinkedInContactInfoLookupProvider:
    """Pull self-published phone numbers from a lead's LinkedIn profile.

    Construction takes a ``voyager_fetcher`` callable so the provider
    has no transitive cookie/HTTP imports — the server wires the real
    one when credentials are present, tests inject a fake.
    """

    name = "linkedin_contact_info"

    def __init__(self, *, voyager_fetcher: VoyagerFetcher) -> None:
        self._fetch = voyager_fetcher
        self.errors: list[str] = []

    def lookup(self, query: LookupQuery) -> list[PhoneCandidate]:
        public_id = _extract_public_id(query.linkedin_url)
        if not public_id:
            return []
        try:
            info = self._fetch(public_id)
        except Exception as exc:
            self.errors.append(
                f"{self.name}:{public_id}:{type(exc).__name__}:{exc}"
            )
            return []
        if not info:
            return []
        contact = _parse_contact_info(info)
        if not contact.phones:
            return []
        url = f"https://www.linkedin.com/in/{public_id}/overlay/contact-info/"
        return [
            PhoneCandidate(
                raw=phone,
                source=self.name,
                source_url=url,
                # Highest possible individual signal: the lead themself
                # chose to publish this on their own profile.
                context="linkedin_self_published",
                extra={"public_id": public_id},
            )
            for phone in contact.phones
        ]


def _extract_public_id(linkedin_url: str | None) -> str | None:
    if not linkedin_url:
        return None
    match = _PUBLIC_ID_REGEX.search(linkedin_url)
    if not match:
        return None
    return match.group(1).rstrip("/")


def _parse_contact_info(payload: dict[str, Any]) -> _ContactInfo:
    """Best-effort extraction of phones + websites from the Voyager
    ``profileContactInfo`` response shape.

    LinkedIn has changed this schema multiple times. We pull from the
    two shapes we've seen in the wild and ignore anything else.
    """
    phones: list[str] = []
    websites: list[str] = []

    raw_phones = payload.get("phoneNumbers") or payload.get("phone_numbers") or []
    if isinstance(raw_phones, list):
        for item in raw_phones:
            if isinstance(item, dict):
                number = item.get("number") or item.get("rawNumber")
                if isinstance(number, str) and number.strip():
                    phones.append(number.strip())
            elif isinstance(item, str) and item.strip():
                phones.append(item.strip())

    raw_websites = payload.get("websites") or []
    if isinstance(raw_websites, list):
        for item in raw_websites:
            if isinstance(item, dict):
                url = item.get("url")
                if isinstance(url, str) and url.strip():
                    websites.append(url.strip())

    return _ContactInfo(phones=phones, websites=websites)
