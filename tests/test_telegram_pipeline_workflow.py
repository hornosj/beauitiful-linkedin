"""Tests for the in-process Telegram enrichment workflow.

Covers the contracts that the saved-leads pipeline introduces on top of
the existing ``/lead-tables/{id}/telegram-consult`` endpoint:

- A cargo divergente at the LinkedIn gate must block the CPF follow-up
  and persist the reason so the UI can explain it.
- The rate-limit signal from ``GonzalesBotConsult`` should set a
  cooldown on the provider state row.
- An active cooldown must short-circuit further ``consult_provider``
  calls without burning Playwright.
- Empty raw payloads still produce a persisted row (no silent drops).
- The follow-up threshold (default 65) gates which CPFs are queried.
- Re-running the same workflow does not duplicate rows.

All tests rely on injected fakes — no Playwright, no network, no DNS.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from beautiful_linkedin.models import Lead
from beautiful_linkedin.storage.saved_leads import SavedLeadsStore
from beautiful_linkedin.storage.telegram_group_playwright_lookup import (
    TelegramConsultResult,
)
from beautiful_linkedin.storage.telegram_pipeline import (
    TELEGRAM_FOLLOWUP_MIN_SCORE,
    TELEGRAM_RATE_LIMIT_COOLDOWN_MINUTES,
    is_provider_in_cooldown,
    is_rate_limit_error,
    run_phone_followup,
    run_telegram_consult,
)


def _lead(
    person: str = "Ana Silva",
    *,
    title: str = "Marketing Manager",
    linkedin_url: str | None = None,
    linkedin_experience_title: str | None = None,
    linkedin_location: str | None = "São Paulo, Brazil",
) -> Lead:
    url = linkedin_url or f"https://linkedin.com/in/{person.lower().replace(' ', '-')}"
    return Lead(
        company_name="Empresa",
        company_domain="empresa.com",
        person_name=person,
        title=title,
        linkedin_url=url,
        source_url=url,
        source_type="linkedin_people_search",
        snippet="",
        confidence_score=80,
        linkedin_location=linkedin_location,
        linkedin_experience_title=linkedin_experience_title,
    )


def _store(tmp_path: Path) -> SavedLeadsStore:
    store = SavedLeadsStore(str(tmp_path / "saved.sqlite"))
    return store


def _create_table_with_leads(
    store: SavedLeadsStore, leads: list[Lead]
) -> str:
    table = store.create_table(name="Telegram pipeline tests")
    store.add_leads(table.id, leads)
    return table.id


# ---------------------------------------------------------------------------
# Rate-limit detection
# ---------------------------------------------------------------------------


class _ProviderTrackingLookup:
    """Minimal lookup with the per-provider dispatch contract.

    Records each ``consult_provider`` call so tests can assert that the
    cooldown short-circuit really prevents the consult.
    """

    def __init__(
        self,
        results_by_provider: dict[str, list[TelegramConsultResult]],
    ) -> None:
        # Pop one result per call so we can verify call counts deterministically.
        self._results = {k: list(v) for k, v in results_by_provider.items()}
        self.calls: list[tuple[str, str]] = []

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(self._results.keys())

    def consult_provider(self, provider: str, lead_name: str) -> TelegramConsultResult:
        self.calls.append((provider, lead_name))
        pool = self._results.get(provider, [])
        if pool:
            return pool.pop(0)
        return TelegramConsultResult(
            provider=provider,
            lead_name=lead_name,
            query=f"/nome {lead_name}",
            raw_text=None,
            source_url=None,
            downloaded_at=None,
            error="no_more_fakes",
        )

    def consult(self, lead_name: str) -> list[TelegramConsultResult]:
        return [
            self.consult_provider(p, lead_name) for p in self.provider_names
        ]


def test_rate_limit_error_detection_recognizes_known_tokens() -> None:
    assert is_rate_limit_error("RuntimeError: gon_rate_limit: uso excessivo")
    assert is_rate_limit_error("Gon disse: uso excessivo, tente novamente")
    assert is_rate_limit_error("muitas requisicoes na ultima hora")
    assert not is_rate_limit_error(None)
    assert not is_rate_limit_error("")
    assert not is_rate_limit_error("RuntimeError: gon_timeout")


def test_run_telegram_consult_sets_cooldown_on_rate_limit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    leads = [_lead("Ana Silva")]
    table_id = _create_table_with_leads(store, leads)

    lookup = _ProviderTrackingLookup(
        {
            "gon": [
                TelegramConsultResult(
                    provider="gon",
                    lead_name="Ana Silva",
                    query="/nome Ana Silva",
                    raw_text=None,
                    source_url=None,
                    downloaded_at=None,
                    error="RuntimeError: gon_rate_limit: uso excessivo",
                )
            ],
            "unix": [
                TelegramConsultResult(
                    provider="unix",
                    lead_name="Ana Silva",
                    query="/nome Ana Silva",
                    raw_text="Nome: Ana Silva\nCPF: 111.222.333-44",
                    source_url=None,
                    downloaded_at=None,
                    error=None,
                )
            ],
        }
    )
    fixed_now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)

    run_telegram_consult(
        selected=leads,
        table_id=table_id,
        store=store,
        lookup=lookup,
        now=lambda: fixed_now,
        run_id="run-1",
    )

    state = store.get_telegram_provider_state("gon")
    assert state is not None
    expected_until = (
        fixed_now + timedelta(minutes=TELEGRAM_RATE_LIMIT_COOLDOWN_MINUTES)
    ).isoformat()
    assert state.cooldown_until == expected_until
    assert "gon_rate_limit" in (state.last_error or "")
    # Unix is healthy, so no cooldown there.
    assert store.get_telegram_provider_state("unix") is None


def test_active_cooldown_skips_consult_provider_call(tmp_path: Path) -> None:
    store = _store(tmp_path)
    leads = [_lead("Ana Silva")]
    table_id = _create_table_with_leads(store, leads)

    fixed_now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    cooldown_until = (fixed_now + timedelta(minutes=15)).isoformat()
    store.set_telegram_provider_cooldown(
        "gon", cooldown_until=cooldown_until, last_error="gon_rate_limit: previous"
    )

    lookup = _ProviderTrackingLookup(
        {
            "gon": [],  # must NOT be called
            "unix": [
                TelegramConsultResult(
                    provider="unix",
                    lead_name="Ana Silva",
                    query="/nome Ana Silva",
                    raw_text="Nome: Ana Silva\nCPF: 111.222.333-44",
                    source_url=None,
                    downloaded_at=None,
                    error=None,
                )
            ],
        }
    )

    rows = run_telegram_consult(
        selected=leads,
        table_id=table_id,
        store=store,
        lookup=lookup,
        now=lambda: fixed_now,
    )

    # Gon was skipped; only Unix was actually consulted.
    assert [provider for provider, _ in lookup.calls] == ["unix"]
    persisted_by_provider = {row.provider: row for row in rows}
    assert (
        persisted_by_provider["gon"].error
        == f"rate_limited:cooldown_until={cooldown_until}"
    )
    assert persisted_by_provider["unix"].error is None


def test_is_provider_in_cooldown_handles_past_and_future() -> None:
    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    past = (now - timedelta(minutes=5)).isoformat()
    future = (now + timedelta(minutes=5)).isoformat()

    class _State:
        def __init__(self, until: str | None) -> None:
            self.cooldown_until = until

    assert is_provider_in_cooldown(_State(future), now=now)
    assert not is_provider_in_cooldown(_State(past), now=now)
    assert not is_provider_in_cooldown(_State(None), now=now)
    assert not is_provider_in_cooldown(None, now=now)


# ---------------------------------------------------------------------------
# Persistence of empty payloads
# ---------------------------------------------------------------------------


def test_empty_raw_text_still_persists_a_row(tmp_path: Path) -> None:
    store = _store(tmp_path)
    leads = [_lead("Ana Silva")]
    table_id = _create_table_with_leads(store, leads)

    lookup = _ProviderTrackingLookup(
        {
            "gon": [
                TelegramConsultResult(
                    provider="gon",
                    lead_name="Ana Silva",
                    query="/nome Ana Silva",
                    raw_text=None,
                    source_url=None,
                    downloaded_at=None,
                    error=None,
                )
            ],
            "unix": [
                TelegramConsultResult(
                    provider="unix",
                    lead_name="Ana Silva",
                    query="/nome Ana Silva",
                    raw_text="",
                    source_url=None,
                    downloaded_at=None,
                    error=None,
                )
            ],
        }
    )

    rows = run_telegram_consult(
        selected=leads,
        table_id=table_id,
        store=store,
        lookup=lookup,
        run_id="run-empty",
    )
    by_provider = {row.provider: row for row in rows}
    assert set(by_provider) == {"gon", "unix"}
    assert by_provider["gon"].extracted_candidates == []
    assert by_provider["unix"].extracted_candidates == []
    assert by_provider["gon"].run_id == "run-empty"


# ---------------------------------------------------------------------------
# Rerun idempotency
# ---------------------------------------------------------------------------


def test_rerun_same_lead_provider_does_not_duplicate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    leads = [_lead("Ana Silva")]
    table_id = _create_table_with_leads(store, leads)

    def make_lookup() -> _ProviderTrackingLookup:
        return _ProviderTrackingLookup(
            {
                "gon": [
                    TelegramConsultResult(
                        provider="gon",
                        lead_name="Ana Silva",
                        query="/nome Ana Silva",
                        raw_text="Nome: Ana Silva\nCPF: 111.222.333-44",
                        source_url=None,
                        downloaded_at=None,
                        error=None,
                    )
                ],
                "unix": [
                    TelegramConsultResult(
                        provider="unix",
                        lead_name="Ana Silva",
                        query="/nome Ana Silva",
                        raw_text="Nome: Ana Silva\nCPF: 999.888.777-66",
                        source_url=None,
                        downloaded_at=None,
                        error=None,
                    )
                ],
            }
        )

    run_telegram_consult(
        selected=leads, table_id=table_id, store=store, lookup=make_lookup()
    )
    run_telegram_consult(
        selected=leads, table_id=table_id, store=store, lookup=make_lookup()
    )

    rows = store.list_telegram_consults(table_id)
    # Two providers × one lead, regardless of how many reruns we did.
    assert len(rows) == 2
    assert {row.provider for row in rows} == {"gon", "unix"}


# ---------------------------------------------------------------------------
# CPF follow-up: title gate, threshold, max-3 cap.
# ---------------------------------------------------------------------------


def _seed_name_stage_candidates(
    store: SavedLeadsStore,
    table_id: str,
    lead: Lead,
    candidates: list[dict],
    *,
    provider: str = "gon",
    run_id: str = "run-name",
) -> None:
    """Helper to fake the output of an earlier name-stage run."""
    store.save_telegram_consult(
        table_id=table_id,
        lead_ref=lead.linkedin_url,
        provider=provider,
        lead_name=lead.person_name,
        query=f"/nome {lead.person_name}",
        raw_text="…",
        source_url=None,
        downloaded_at=None,
        error=None,
        extracted_nome=lead.person_name,
        extracted_cpf=candidates[0]["cpf"] if candidates else None,
        extracted_candidates=candidates,
        match_score=candidates[0]["match_score"] if candidates else None,
        run_id=run_id,
        query_type="name",
    )


def _cpf_payload(cpf: str, score: int, **extras) -> dict:
    base = {
        "cpf": cpf,
        "nome": "Ana Silva",
        "data_nascimento": "10/01/1985",
        "endereco": "Rua X, São Paulo/SP",
        "match_score": score,
        "signals_used": ["name", "location"],
        "breakdown": {"confidence_label": "alta"},
    }
    base.update(extras)
    return base


def test_phone_followup_ignores_target_titles(tmp_path: Path) -> None:
    # Title gate was removed: even when the LinkedIn cargo doesn't
    # match ``target_titles``, the follow-up consults the persisted CPF
    # candidates. Selection of the lead by the operator IS the decision
    # to spend Telegram quota — no second-guess on cargo here.
    store = _store(tmp_path)
    lead = _lead(
        title="Marketing Manager",
        linkedin_experience_title="Engenheiro de Software",
    )
    table_id = _create_table_with_leads(store, [lead])
    _seed_name_stage_candidates(
        store, table_id, lead, [_cpf_payload("111.222.333-44", 90)]
    )

    consult_calls: list[str] = []

    def consult_fn(cpf: str) -> TelegramConsultResult:
        consult_calls.append(cpf)
        return TelegramConsultResult(
            provider="gon_cpf",
            lead_name=lead.person_name or "",
            query=f"/cpf {cpf}",
            raw_text=f"CPF: {cpf}\nTelefone: (11) 99999-0000",
            source_url=None,
            downloaded_at=None,
            error=None,
        )

    out = run_phone_followup(
        lead=lead,
        table_id=table_id,
        store=store,
        consult_fn=consult_fn,
        target_titles=["marketing"],
        run_id="run-phone-1",
    )
    assert consult_calls == ["111.222.333-44"]
    assert len(out) == 1
    rows = store.list_telegram_consults_for_lead(
        table_id, lead.linkedin_url, query_type="cpf"
    )
    assert all(
        row.blocked_reason != "linkedin_cargo_divergente" for row in rows
    )


def test_phone_followup_skipped_when_no_candidate_above_threshold(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    lead = _lead()
    table_id = _create_table_with_leads(store, [lead])
    # Both candidates below the 65 threshold → no follow-up.
    _seed_name_stage_candidates(
        store,
        table_id,
        lead,
        [
            _cpf_payload("111.222.333-44", 55),
            _cpf_payload("222.333.444-55", 50),
        ],
    )

    consult_calls: list[str] = []

    def consult_fn(cpf: str) -> TelegramConsultResult:  # pragma: no cover - asserted
        consult_calls.append(cpf)
        raise AssertionError("consult should not be called below threshold")

    out = run_phone_followup(
        lead=lead,
        table_id=table_id,
        store=store,
        consult_fn=consult_fn,
        target_titles=None,
        run_id="run-no-eligible",
    )
    assert out == []
    assert consult_calls == []
    rows = store.list_telegram_consults_for_lead(
        table_id, lead.linkedin_url, query_type="cpf"
    )
    assert len(rows) == 1
    assert rows[0].blocked_reason == "no_eligible_cpf"
    assert f"no_eligible_cpf_above_{TELEGRAM_FOLLOWUP_MIN_SCORE}" in (rows[0].error or "")


def test_phone_followup_caps_at_three_candidates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    lead = _lead()
    table_id = _create_table_with_leads(store, [lead])
    _seed_name_stage_candidates(
        store,
        table_id,
        lead,
        [
            _cpf_payload("111.111.111-11", 95),
            _cpf_payload("222.222.222-22", 88),
            _cpf_payload("333.333.333-33", 80),
            _cpf_payload("444.444.444-44", 72),
            _cpf_payload("555.555.555-55", 70),
        ],
    )
    consulted: list[str] = []

    def consult_fn(cpf: str) -> TelegramConsultResult:
        consulted.append(cpf)
        return TelegramConsultResult(
            provider="gon_cpf",
            lead_name=lead.person_name,
            query=f"/cpf {cpf}",
            raw_text=f"CPF: {cpf}\nTelefone: (11) 99999-0000",
            source_url=None,
            downloaded_at=None,
            error=None,
        )

    out = run_phone_followup(
        lead=lead,
        table_id=table_id,
        store=store,
        consult_fn=consult_fn,
        target_titles=None,
        run_id="run-cap",
    )

    # Cap is 3; the lowest-scoring CPFs above the threshold get dropped.
    assert consulted == [
        "111.111.111-11",
        "222.222.222-22",
        "333.333.333-33",
    ]
    assert len(out) == 3
    # All three phones inherit their CPF's score 1:1.
    assert {c.confidence for c in out} == {95, 88, 80}


def test_phone_followup_cooldown_persists_blocked_row_without_calling_consult(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    lead = _lead()
    table_id = _create_table_with_leads(store, [lead])
    _seed_name_stage_candidates(
        store, table_id, lead, [_cpf_payload("111.222.333-44", 90)]
    )
    fixed_now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    cooldown_until = (fixed_now + timedelta(minutes=20)).isoformat()
    store.set_telegram_provider_cooldown(
        "gon_cpf",
        cooldown_until=cooldown_until,
        last_error="gon_rate_limit: uso excessivo",
    )

    def consult_fn(cpf: str) -> TelegramConsultResult:  # pragma: no cover
        raise AssertionError("must not consult under cooldown")

    out = run_phone_followup(
        lead=lead,
        table_id=table_id,
        store=store,
        consult_fn=consult_fn,
        target_titles=None,
        run_id="run-cooldown",
        now=lambda: fixed_now,
    )
    assert out == []
    rows = store.list_telegram_consults_for_lead(
        table_id, lead.linkedin_url, query_type="cpf"
    )
    assert len(rows) == 1
    assert rows[0].blocked_reason == "rate_limited"
    assert "rate_limited:cooldown_until=" in (rows[0].error or "")
