"""Validate and canonicalize phone candidates.

Wraps Google's ``phonenumbers`` (port of libphonenumber) and adds BR-aware
heuristics:

- Country code inference: a 10-11 digit Brazilian number missing ``+55``
  is rescued so harvesters can pass raw "(11) 99999-9999" through.
- Type classification: mobile / fixed_line / voip / unknown.
- Call-center drop: 0800/4004/3003 are dropped *again* here as a second
  gate even though the harvester already filters them — paid providers
  may return them and we don't want them slipping in via that path.
- Carrier and region come from ``phonenumbers``'s offline metadata
  (zero network calls).

HLR/MNP live-network probes are intentionally out of scope for now and
left for a follow-up; the contract here is offline-only validation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

import phonenumbers
from phonenumbers import (
    NumberParseException,
    PhoneNumberFormat,
    PhoneNumberType,
    carrier as pn_carrier,
    geocoder as pn_geocoder,
)


logger = logging.getLogger(__name__)


class PhoneValidationStatus(str, Enum):
    VALID = "valid"          # phonenumbers says is_valid_number → True
    PROBABLE = "probable"    # parses but is_possible only
    INVALID = "invalid"      # cannot be parsed or fails possibility
    RISKY = "risky"          # dropped by our own heuristics (call center, etc.)


class PhoneType(str, Enum):
    MOBILE = "mobile"
    FIXED = "fixed"
    FIXED_OR_MOBILE = "fixed_or_mobile"
    VOIP = "voip"
    TOLL_FREE = "toll_free"
    PAGER = "pager"
    UNKNOWN = "unknown"


_TYPE_MAP: dict[int, PhoneType] = {
    PhoneNumberType.MOBILE: PhoneType.MOBILE,
    PhoneNumberType.FIXED_LINE: PhoneType.FIXED,
    PhoneNumberType.FIXED_LINE_OR_MOBILE: PhoneType.FIXED_OR_MOBILE,
    PhoneNumberType.VOIP: PhoneType.VOIP,
    PhoneNumberType.TOLL_FREE: PhoneType.TOLL_FREE,
    PhoneNumberType.PAGER: PhoneType.PAGER,
}


_CALL_CENTER_PREFIXES: tuple[str, ...] = ("0800", "4004", "3003", "0300")


@dataclass(frozen=True)
class PhoneValidationResult:
    """Outcome of validating one candidate.

    ``e164`` is the canonical form we persist; ``national`` is the
    "pretty" form for display; ``status`` drives whether the value is
    accepted at all.
    """

    status: PhoneValidationStatus
    e164: str | None = None
    national: str | None = None
    country: str | None = None       # ISO-3166 alpha-2, e.g. "BR"
    type: PhoneType = PhoneType.UNKNOWN
    carrier: str | None = None
    region: str | None = None        # human-readable, e.g. "São Paulo"
    reason: str = ""


class PhoneValidator:
    """Offline validation + classification of phone candidates."""

    def __init__(
        self,
        *,
        default_region: str = "BR",
        carrier_locale: str = "pt",
        region_locale: str = "pt",
    ) -> None:
        self._default_region = default_region
        self._carrier_locale = carrier_locale
        self._region_locale = region_locale

    def validate(self, raw: str) -> PhoneValidationResult:
        digits = _digits_only(raw or "")
        if not digits:
            return PhoneValidationResult(
                status=PhoneValidationStatus.INVALID, reason="empty"
            )
        if _is_call_center(digits):
            return PhoneValidationResult(
                status=PhoneValidationStatus.RISKY,
                reason="call_center_prefix",
            )
        if _is_repetitive(digits):
            return PhoneValidationResult(
                status=PhoneValidationStatus.INVALID,
                reason="repetitive_digits",
            )

        parsed = self._parse(raw, digits)
        if parsed is None:
            return PhoneValidationResult(
                status=PhoneValidationStatus.INVALID, reason="unparseable"
            )

        if not phonenumbers.is_possible_number(parsed):
            return PhoneValidationResult(
                status=PhoneValidationStatus.INVALID, reason="not_possible"
            )

        is_valid = phonenumbers.is_valid_number(parsed)
        type_code = phonenumbers.number_type(parsed)
        phone_type = _TYPE_MAP.get(type_code, PhoneType.UNKNOWN)
        if phone_type == PhoneType.TOLL_FREE:
            return PhoneValidationResult(
                status=PhoneValidationStatus.RISKY,
                reason="toll_free",
                e164=_format(parsed, PhoneNumberFormat.E164),
                national=_format(parsed, PhoneNumberFormat.NATIONAL),
                type=phone_type,
                country=phonenumbers.region_code_for_number(parsed),
            )

        try:
            carrier_name = pn_carrier.name_for_number(parsed, self._carrier_locale) or None
        except Exception:
            carrier_name = None
        try:
            region_name = (
                pn_geocoder.description_for_number(parsed, self._region_locale) or None
            )
        except Exception:
            region_name = None

        return PhoneValidationResult(
            status=PhoneValidationStatus.VALID if is_valid else PhoneValidationStatus.PROBABLE,
            e164=_format(parsed, PhoneNumberFormat.E164),
            national=_format(parsed, PhoneNumberFormat.NATIONAL),
            country=phonenumbers.region_code_for_number(parsed) or self._default_region,
            type=phone_type,
            carrier=carrier_name,
            region=region_name,
            reason="phonenumbers_valid" if is_valid else "phonenumbers_possible",
        )

    def _parse(self, raw: str, digits: str) -> phonenumbers.PhoneNumber | None:
        # First attempt: respect a leading ``+`` if present.
        if raw.strip().startswith("+"):
            try:
                return phonenumbers.parse(raw, None)
            except NumberParseException:
                pass

        # Second attempt: parse with the default region. This is what
        # rescues "11999999999" → +55 11 99999-9999 when the harvester
        # found a raw national-format number on a BR site.
        try:
            return phonenumbers.parse(raw, self._default_region)
        except NumberParseException:
            pass

        # Third attempt: rebuild from digits only. Some pages have weird
        # formatting (`11.99999-9999` with a stray separator) that
        # phonenumbers refuses to parse directly.
        try:
            return phonenumbers.parse(digits, self._default_region)
        except NumberParseException:
            return None


def _format(parsed: phonenumbers.PhoneNumber, fmt: int) -> str:
    try:
        return phonenumbers.format_number(parsed, fmt)
    except Exception:
        return ""


def _digits_only(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _is_call_center(digits: str) -> bool:
    return digits.startswith(_CALL_CENTER_PREFIXES) or digits[2:].startswith(
        _CALL_CENTER_PREFIXES
    )


def _is_repetitive(digits: str) -> bool:
    return len(set(digits)) <= 1
