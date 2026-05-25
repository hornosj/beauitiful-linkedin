from __future__ import annotations

from beautiful_linkedin.storage.phone_lookup import LookupQuery
from beautiful_linkedin.storage.telegram_phone_lookup import (
    TelegramBotPhoneLookupProvider,
)


def test_consultoria_gonzales_provider_sends_nome_command_and_parses_phones() -> None:
    seen_messages: list[str] = []

    def requester(message: str) -> str:
        seen_messages.append(message)
        return "Resultado para Ana Silva\nTelefone: +55 11 99999-0000\nOutro: (11) 4002-8922"

    provider = TelegramBotPhoneLookupProvider(requester=requester)

    candidates = provider.lookup(
        LookupQuery(
            full_name="Ana Silva",
            company_name="Marlabs",
            company_domain="marlabs.com",
        )
    )

    assert seen_messages == ["/nome Ana Silva"]
    assert [c.raw for c in candidates] == ["+55 11 99999-0000", "(11) 4002-8922"]
    assert all(c.source == "consultoria_gonzales_bot" for c in candidates)
    assert all(c.context == "telegram_bot_name_match" for c in candidates)
    assert candidates[0].extra["bot"] == "@ConsultoriaGonzalesbot"


def test_consultoria_gonzales_provider_is_fail_soft() -> None:
    def requester(_message: str) -> str:
        raise RuntimeError("telegram timeout")

    provider = TelegramBotPhoneLookupProvider(requester=requester)

    assert provider.lookup(LookupQuery(full_name="Ana Silva", company_name="Marlabs")) == []
    assert provider.errors


def test_consultoria_gonzales_provider_skips_missing_full_name() -> None:
    provider = TelegramBotPhoneLookupProvider(requester=lambda _message: "+55 11 99999-0000")

    assert provider.lookup(LookupQuery(full_name="", company_name="Marlabs")) == []


def test_consultoria_gonzales_provider_invalid_api_id_is_fail_soft() -> None:
    provider = TelegramBotPhoneLookupProvider(api_id="not-a-number", api_hash="hash")

    assert provider.lookup(LookupQuery(full_name="Ana Silva", company_name="Marlabs")) == []
    assert "consultoria_gonzales_bot:invalid_api_id" in provider.errors
