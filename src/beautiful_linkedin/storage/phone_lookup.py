"""Phone lookup provider interface.

Defines the plug-in shape that every external phone source must
implement so the orchestrator can mix Bucket B (SERP snippets),
Bucket C (Telegram bots like UsersBox / Eye of God), and any future
paid source without changing the core pipeline.

The contract is intentionally narrow:

- One method ``lookup(query)`` that returns ``list[PhoneCandidate]``.
- ``errors: list[str]`` mirrored from the paid providers so the
  orchestrator can surface "Bucket B failed because SearxNG returned
  429" without breaking the run.
- ``name`` for UI labelling and analytics.

Providers are expected to be fail-soft: any network/parse exception
should land in ``errors`` and the method should return ``[]`` instead
of raising. The orchestrator wraps every call in ``_safe_lookup`` as a
second line of defence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class LookupQuery:
    """What we ask an external provider to find."""

    full_name: str
    company_name: str
    company_domain: str | None = None
    linkedin_url: str | None = None
    email: str | None = None


@dataclass(frozen=True)
class PhoneCandidate:
    """One candidate phone returned by an external provider.

    ``raw`` is the string as the provider returned it; the orchestrator
    will run our :class:`PhoneValidator` to canonicalize it to E.164
    before scoring or persisting. ``context`` is a short tag that the
    score function uses to weight different sources — e.g. a SERP
    snippet that contains the lead's full name is stronger than one
    that just contains the company name.
    """

    raw: str
    source: str            # "serp" | "whatsapp" | "usersbox" | ...
    source_url: str | None  # where the candidate came from, when available
    context: str = "lookup"
    confidence_hint: int | None = None  # provider's own confidence, when available
    extra: dict[str, str] = field(default_factory=dict)


class PhoneLookupProvider(Protocol):
    """Minimal protocol every external phone source implements."""

    name: str
    errors: list[str]

    def lookup(self, query: LookupQuery) -> list[PhoneCandidate]:
        ...


# ---------------------------------------------------------------------------
# Generic NOOP / disabled provider — used as a placeholder when a
# paid bot adapter (UsersBox, Eye of God) is referenced but its
# credentials are missing. Keeps wiring in the orchestrator branch-free.
# ---------------------------------------------------------------------------


class NoopLookupProvider:
    """Disabled provider stub.

    Used by the server when a paid lookup provider (UsersBox, etc.) is
    *configured in code* but credentials are missing in ``.env``. We
    return an empty list and record a single "disabled" line in
    ``errors`` so the diagnostics endpoint can show "UsersBox: not
    configured" instead of silently dropping the bucket.
    """

    def __init__(self, name: str, reason: str = "not_configured") -> None:
        self.name = name
        self.errors: list[str] = [f"{name}:{reason}"]

    def lookup(self, query: LookupQuery) -> list[PhoneCandidate]:  # noqa: ARG002
        return []
