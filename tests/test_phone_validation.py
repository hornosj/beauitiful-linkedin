"""Offline tests for PhoneValidator.

The validator wraps ``phonenumbers`` plus our BR-aware heuristics. We
test the wrapper's *decisions*, not libphonenumber itself: we want
guarantees that
- "raw national number" → "+55..." for BR,
- mobile / fixed / toll-free are correctly classified,
- 0800 / 4004 / 3003 and 11-1s are dropped before the lib sees them,
- repetitive placeholders bounce.
"""

from __future__ import annotations

from beautiful_linkedin.storage.phone_validation import (
    PhoneType,
    PhoneValidationStatus,
    PhoneValidator,
)


def test_validator_promotes_br_mobile_to_e164() -> None:
    validator = PhoneValidator()
    result = validator.validate("(11) 99999-9999")
    assert result.status == PhoneValidationStatus.VALID
    assert result.e164 == "+5511999999999"
    assert result.country == "BR"
    assert result.type == PhoneType.MOBILE


def test_validator_keeps_leading_plus_format() -> None:
    validator = PhoneValidator()
    result = validator.validate("+1 415 555 0132")
    # phonenumbers may flag US directory test numbers as VALID or
    # PROBABLE depending on metadata; both shapes are acceptable here.
    assert result.status in {
        PhoneValidationStatus.VALID,
        PhoneValidationStatus.PROBABLE,
    }
    assert result.country == "US"


def test_validator_classifies_br_fixed_line() -> None:
    validator = PhoneValidator()
    result = validator.validate("+55 11 3030-4040")
    assert result.status == PhoneValidationStatus.VALID
    assert result.type == PhoneType.FIXED


def test_validator_rejects_call_center_prefixes() -> None:
    validator = PhoneValidator()
    for raw in ("0800 123 4567", "4004 2020", "3003 1234", "0300 555 0000"):
        result = validator.validate(raw)
        assert result.status == PhoneValidationStatus.RISKY, raw
        assert "call_center" in result.reason or result.reason == "toll_free"


def test_validator_drops_repetitive_placeholders() -> None:
    validator = PhoneValidator()
    result = validator.validate("(00) 00000-0000")
    assert result.status == PhoneValidationStatus.INVALID
    assert result.reason in {"repetitive_digits", "not_possible", "unparseable"}


def test_validator_drops_empty_and_garbage() -> None:
    validator = PhoneValidator()
    assert validator.validate("").status == PhoneValidationStatus.INVALID
    assert validator.validate("not a phone").status == PhoneValidationStatus.INVALID
    # Way too short — phonenumbers will say not_possible.
    short = validator.validate("123")
    assert short.status in {
        PhoneValidationStatus.INVALID,
        PhoneValidationStatus.RISKY,
    }


def test_validator_handles_messy_separators() -> None:
    """`11.99999-9999` with a dot used to break phonenumbers' strict
    parser; the fallback path should still produce E.164."""
    validator = PhoneValidator()
    result = validator.validate("11.99999-9999")
    assert result.status == PhoneValidationStatus.VALID
    assert result.e164 == "+5511999999999"


def test_validator_uses_default_region_override() -> None:
    validator = PhoneValidator(default_region="US")
    result = validator.validate("415-555-0132")
    # Should parse as US under the override, not as BR.
    assert result.country == "US"
