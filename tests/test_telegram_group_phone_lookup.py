from __future__ import annotations

import time

import beautiful_linkedin.storage.telegram_group_phone_lookup as group_module
from beautiful_linkedin.storage.phone_lookup import LookupQuery
from beautiful_linkedin.storage.telegram_group_phone_lookup import (
    TelegramGroupPhoneLookupProvider,
    TelegramVoidPhoneLookupProvider,
    _normalize_group_username,
)


def _reset_throttle() -> None:
    group_module._last_send_ts = 0.0


def test_group_provider_sends_nome_and_parses_phones_from_multiple_replies() -> None:
    _reset_throttle()
    seen_messages: list[str] = []

    def requester(message: str) -> list[str]:
        seen_messages.append(message)
        return [
            "Resultado para Ana Silva",
            "Telefone: +55 11 99999-0000",
            "Outro reply: (11) 4002-8922 e tambem 5511988887777",
        ]

    provider = TelegramGroupPhoneLookupProvider(
        group_username="@CONSULTASGRATIS4NV",
        throttle_seconds=0,
        requester=requester,
    )

    candidates = provider.lookup(
        LookupQuery(full_name="Ana Silva", company_name="Marlabs")
    )

    assert seen_messages == ["/nome Ana Silva"]
    raws = [c.raw for c in candidates]
    assert "+55 11 99999-0000" in raws
    assert "(11) 4002-8922" in raws
    assert "5511988887777" in raws
    assert all(c.source == "telegram_group_consultasgratis" for c in candidates)
    assert all(c.context == "telegram_group_name_match" for c in candidates)
    assert candidates[0].extra["group"] == "@CONSULTASGRATIS4NV"
    assert candidates[0].source_url == "https://t.me/CONSULTASGRATIS4NV"


def test_void_provider_sends_telefone_and_parses_phones_from_replies() -> None:
    _reset_throttle()
    seen_messages: list[str] = []

    def requester(message: str) -> list[str]:
        seen_messages.append(message)
        return [
            "VOID SEARCH - TELEFONE",
            "Telefone encontrado: +55 11 98888-7777",
        ]

    provider = TelegramVoidPhoneLookupProvider(
        group_username="@CONSULTASGRATIS4NV",
        throttle_seconds=0,
        requester=requester,
    )

    candidates = provider.lookup(
        LookupQuery(full_name="Ana Silva", company_name="Marlabs")
    )

    assert seen_messages == ["/telefone Ana Silva"]
    assert [c.raw for c in candidates] == ["+55 11 98888-7777"]
    assert candidates[0].source == "void_phone_consultasgratis"
    assert candidates[0].context == "telegram_bot_name_match"
    assert candidates[0].extra["group"] == "@CONSULTASGRATIS4NV"
    assert candidates[0].extra["query"] == "/telefone Ana Silva"


def test_group_provider_dedups_same_phone_across_replies() -> None:
    _reset_throttle()

    def requester(_message: str) -> list[str]:
        return [
            "+55 11 99999-0000",
            "mesmo numero: 5511999990000",
            "outra mencao +55 11 99999-0000",
        ]

    provider = TelegramGroupPhoneLookupProvider(
        throttle_seconds=0, requester=requester
    )

    candidates = provider.lookup(
        LookupQuery(full_name="Ana Silva", company_name="Marlabs")
    )

    assert len(candidates) == 1


def test_group_provider_is_fail_soft_on_requester_error() -> None:
    _reset_throttle()

    def requester(_message: str) -> list[str]:
        raise RuntimeError("flood wait")

    provider = TelegramGroupPhoneLookupProvider(
        throttle_seconds=0, requester=requester
    )

    assert provider.lookup(
        LookupQuery(full_name="Ana Silva", company_name="Marlabs")
    ) == []
    assert provider.errors
    assert "RuntimeError" in provider.errors[0]


def test_group_provider_skips_missing_full_name() -> None:
    _reset_throttle()
    calls: list[str] = []

    provider = TelegramGroupPhoneLookupProvider(
        throttle_seconds=0,
        requester=lambda message: (calls.append(message) or ["+55 11 99999-0000"]),
    )

    assert provider.lookup(LookupQuery(full_name="", company_name="Marlabs")) == []
    assert calls == []


def test_group_provider_invalid_api_id_is_fail_soft() -> None:
    _reset_throttle()
    provider = TelegramGroupPhoneLookupProvider(
        api_id="not-a-number", api_hash="hash", throttle_seconds=0
    )

    assert (
        provider.lookup(LookupQuery(full_name="Ana Silva", company_name="Marlabs"))
        == []
    )
    assert "telegram_group_consultasgratis:invalid_api_id" in provider.errors


def test_group_provider_not_configured_is_fail_soft() -> None:
    _reset_throttle()
    provider = TelegramGroupPhoneLookupProvider(throttle_seconds=0)

    assert (
        provider.lookup(LookupQuery(full_name="Ana Silva", company_name="Marlabs"))
        == []
    )
    assert any("telegram_not_configured" in e for e in provider.errors)


def test_group_provider_throttle_blocks_consecutive_sends() -> None:
    _reset_throttle()

    def requester(_message: str) -> list[str]:
        return ["+55 11 99999-0000"]

    provider = TelegramGroupPhoneLookupProvider(
        throttle_seconds=0.3, requester=requester
    )

    start = time.monotonic()
    provider.lookup(LookupQuery(full_name="Ana Silva", company_name="Marlabs"))
    provider.lookup(LookupQuery(full_name="Bruno Costa", company_name="Marlabs"))
    elapsed = time.monotonic() - start

    assert elapsed >= 0.3


def test_normalize_group_username_accepts_multiple_forms() -> None:
    assert _normalize_group_username("CONSULTASGRATIS4NV") == "@CONSULTASGRATIS4NV"
    assert _normalize_group_username("@CONSULTASGRATIS4NV") == "@CONSULTASGRATIS4NV"
    assert (
        _normalize_group_username("https://t.me/CONSULTASGRATIS4NV")
        == "@CONSULTASGRATIS4NV"
    )
    assert (
        _normalize_group_username("t.me/CONSULTASGRATIS4NV") == "@CONSULTASGRATIS4NV"
    )
    assert _normalize_group_username("") == "@CONSULTASGRATIS4NV"
