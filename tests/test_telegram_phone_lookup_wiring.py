from __future__ import annotations

from beautiful_linkedin.config import Settings
from beautiful_linkedin.server.app import (
    _default_phone_harvester,
    _default_phone_lookup_providers,
)


class _FakeEngine:
    def search(self, query: str, max_results: int = 10):  # noqa: ARG002
        return []


def test_default_phone_lookup_providers_adds_consultoria_gonzales_when_configured(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._resolve_free_engines",
        lambda _settings: {"fake": _FakeEngine()},
    )

    providers = _default_phone_lookup_providers(
        Settings(
            telegram_api_id="12345",
            telegram_api_hash="abc123",
            telegram_session_name="data/test-session",
        )
    )

    assert [p.name for p in providers] == [
        "receita_cnpj",
        "pdf_serp",
        "consultoria_gonzales_bot",
    ]


def test_default_phone_lookup_providers_skips_consultoria_gonzales_without_credentials(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._resolve_free_engines",
        lambda _settings: {"fake": _FakeEngine()},
    )

    providers = _default_phone_lookup_providers(Settings())

    assert [p.name for p in providers] == ["receita_cnpj", "pdf_serp"]


def test_default_phone_harvester_is_noop_while_telegram_is_the_only_source() -> None:
    harvester = _default_phone_harvester()

    assert harvester.harvest("empresa.com") == []
