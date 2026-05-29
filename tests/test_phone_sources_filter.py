"""Tests for the ``phone_sources`` request flag that scopes the internal
phone enrichment pipeline to a subset of lookup providers.

The "Buscar via Telegram" button in the UI passes
``phone_sources=["telegram_group"]`` and expects the backend to:

1. Run ONLY the Telegram-group provider (skip Receita CNPJ, PDFs, bot 1:1).
2. Disable the site-harvest bucket — that bucket is independent of the
   lookup providers, so without an explicit skip it would still fire.
"""

from __future__ import annotations

from beautiful_linkedin.server.app import (
    _build_phone_orchestrator,
    _resolve_phone_source_names,
)


class _StubLookup:
    """Minimal stand-in for a ``PhoneLookupProvider``."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.errors: list[str] = []

    def lookup(self, _query):  # pragma: no cover - never invoked here
        return []


def test_resolve_phone_source_names_translates_aliases() -> None:
    assert _resolve_phone_source_names(["telegram_group"]) == {
        "telegram_group_consultasgratis",
        "void_phone_consultasgratis",
    }
    assert _resolve_phone_source_names(["TELEGRAM_GROUP"]) == {
        "telegram_group_consultasgratis",
        "void_phone_consultasgratis",
    }
    assert _resolve_phone_source_names(
        ["telegram_group", "receita_cnpj"]
    ) == {
        "telegram_group_consultasgratis",
        "void_phone_consultasgratis",
        "receita_cnpj",
    }
    assert _resolve_phone_source_names(None) is None
    assert _resolve_phone_source_names([]) is None
    assert _resolve_phone_source_names(["  "]) is None


def test_build_phone_orchestrator_filters_to_telegram_group(monkeypatch) -> None:
    providers = [
        _StubLookup("receita_cnpj"),
        _StubLookup("pdf_serp"),
        _StubLookup("consultoria_gonzales_bot"),
        _StubLookup("telegram_group_consultasgratis"),
        _StubLookup("void_phone_consultasgratis"),
    ]
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_phone_lookup_providers",
        lambda _settings: providers,
    )
    # Defang harvester so a misconfigured filter would still be visible in
    # the orchestrator state rather than silently making a real HTTP call.
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_phone_harvester",
        lambda: object(),
    )

    orchestrator = _build_phone_orchestrator(
        phone_sources=["telegram_group"],
    )

    assert [p.name for p in orchestrator._lookup_providers] == [
        "telegram_group_consultasgratis",
        "void_phone_consultasgratis",
    ]
    # Site-harvest bucket is muted when the UI scoped to lookup channels.
    assert orchestrator._harvest_fn("nubank.com") == []


def test_build_phone_orchestrator_keeps_all_providers_without_filter(
    monkeypatch,
) -> None:
    providers = [
        _StubLookup("receita_cnpj"),
        _StubLookup("telegram_group_consultasgratis"),
    ]
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_phone_lookup_providers",
        lambda _settings: providers,
    )

    captured: list[str] = []

    class _StubHarvester:
        def harvest(self, domain: str):
            captured.append(domain)
            return []

    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_phone_harvester",
        lambda: _StubHarvester(),
    )

    orchestrator = _build_phone_orchestrator(phone_sources=None)

    assert {p.name for p in orchestrator._lookup_providers} == {
        "receita_cnpj",
        "telegram_group_consultasgratis",
    }
    # Harvester is wired (not the empty fallback) when no filter is set.
    orchestrator._harvest_fn("nubank.com")
    assert captured == ["nubank.com"]
