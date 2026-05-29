"""Tests for the Telegram-consult endpoints.

The route goes through ``_default_telegram_consult_lookup`` which now
builds a :class:`TelegramConsultOrchestrator`. We monkeypatch it with a
fake orchestrator that returns ``list[TelegramConsultResult]`` so the
suite stays offline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beautiful_linkedin.models import Lead
from beautiful_linkedin.server.app import build_app, get_saved_leads_store
from beautiful_linkedin.storage.telegram_group_playwright_lookup import (
    TelegramConsultResult,
)


def _lead(person: str, *, linkedin_url: str | None = None) -> Lead:
    url = (
        linkedin_url
        or f"https://www.linkedin.com/in/{person.lower().replace(' ', '-')}/"
    )
    return Lead(
        company_name="Empresa",
        company_domain="empresa.com",
        person_name=person,
        title="Marketing Manager",
        linkedin_url=url,
        source_url=url,
        source_type="linkedin_people_search",
        snippet="",
        confidence_score=80,
    )


def _create_table(client: TestClient, leads: list[Lead]) -> str:
    return client.post(
        "/lead-tables",
        json={
            "name": "Telegram",
            "leads": [lead.model_dump(mode="json") for lead in leads],
        },
    ).json()["table"]["id"]


class _FakeOrchestrator:
    """Stub matching :class:`TelegramConsultOrchestrator`'s ``consult``
    contract. Each call returns one ``TelegramConsultResult`` per
    provider so the endpoint test exercises the multi-row persistence
    path."""

    def __init__(
        self,
        *,
        gon_by_name: dict[str, TelegramConsultResult],
        unix_by_name: dict[str, TelegramConsultResult],
    ) -> None:
        self._gon = gon_by_name
        self._unix = unix_by_name
        self.calls: list[str] = []

    def consult(self, lead_name: str) -> list[TelegramConsultResult]:
        self.calls.append(lead_name)

        def fallback(provider: str) -> TelegramConsultResult:
            return TelegramConsultResult(
                provider=provider,
                lead_name=lead_name,
                query=f"/nome {lead_name}",
                raw_text=None,
                source_url=None,
                downloaded_at=None,
                error="no_fake_for_lead",
            )

        return [
            self._gon.get(lead_name, fallback("gon")),
            self._unix.get(lead_name, fallback("unix")),
        ]


class _FakeExperimentalOrchestrator:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def consult(self, lead_name: str) -> list[TelegramConsultResult]:
        self.calls.append(lead_name)
        now = datetime.now(timezone.utc).isoformat()
        return [
            TelegramConsultResult(
                provider="finder",
                lead_name=lead_name,
                query=f"/nome {lead_name}",
                raw_text="Nome: Ana Silva\nCPF: 111.222.333-44",
                source_url="https://finder.example/result.json",
                downloaded_at=now,
                error=None,
            ),
            TelegramConsultResult(
                provider="gon",
                lead_name=lead_name,
                query=f"/nome {lead_name}",
                raw_text="Nome: Ana Silva\nCPF: 111.222.333-44",
                source_url="https://gon.example/result",
                downloaded_at=now,
                error=None,
            ),
            TelegramConsultResult(
                provider="unix",
                lead_name=lead_name,
                query=f"/nome {lead_name}",
                raw_text="Nome: Ana Silva\nCPF: 999.888.777-66",
                source_url="https://unix.example/result.txt",
                downloaded_at=now,
                error=None,
            ),
        ]


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "beautiful_linkedin.server.app.probe_cdp_endpoint",
        lambda *args, **kwargs: False,
    )
    return build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))


def test_telegram_consult_runs_gon_then_unix_and_persists_both(monkeypatch, app) -> None:
    client = TestClient(app)
    leads = [_lead("Ana Silva")]
    table_id = _create_table(client, leads)

    now = datetime.now(timezone.utc).isoformat()
    fake = _FakeOrchestrator(
        gon_by_name={
            "Ana Silva": TelegramConsultResult(
                provider="gon",
                lead_name="Ana Silva",
                query="/nome Ana Silva",
                raw_text="Nome: Ana Silva\nCPF: 111.222.333-44\nNascimento: 10/01/1985\nEndereço: Rua X, São Paulo/SP",
                source_url="https://exemplo.com/r/ana-gon",
                downloaded_at=now,
                error=None,
            )
        },
        unix_by_name={
            "Ana Silva": TelegramConsultResult(
                provider="unix",
                lead_name="Ana Silva",
                query="/nome Ana Silva",
                raw_text="Nome: Ana Silva\nCPF: 999.888.777-66\nNascimento: 02/02/1970\nEndereço: Rua Y, Belém/PA",
                source_url="https://exemplo.com/r/ana-unix",
                downloaded_at=now,
                error=None,
            )
        },
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_telegram_consult_lookup",
        lambda settings=None: fake,
    )

    response = client.post(
        f"/lead-tables/{table_id}/telegram-consult",
        json={"lead_refs": [leads[0].linkedin_url]},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert fake.calls == ["Ana Silva"]
    # One lead × two providers = two rows persisted.
    by_provider = {c["provider"]: c for c in body["consults"]}
    assert set(by_provider) == {"gon", "unix"}
    assert by_provider["gon"]["extracted_cpf"] == "111.222.333-44"
    assert by_provider["gon"]["extracted_birth_date"] == "10/01/1985"
    assert by_provider["unix"]["extracted_cpf"] == "999.888.777-66"


def test_telegram_consult_multiple_experimental_is_separate_from_default(
    monkeypatch, app
) -> None:
    client = TestClient(app)
    leads = [_lead("Ana Silva")]
    table_id = _create_table(client, leads)

    default_called = False

    def fail_if_default_used(settings=None):  # noqa: ANN001, ARG001
        nonlocal default_called
        default_called = True
        raise AssertionError("default lookup should not be used")

    fake = _FakeExperimentalOrchestrator()
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_telegram_consult_lookup",
        fail_if_default_used,
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_telegram_multi_experimental_lookup",
        lambda settings=None: fake,
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_findex_cpf_consult",
        lambda settings=None: type(
            "NoopFinderCpf",
            (),
            {
                "consult": lambda self, cpf: TelegramConsultResult(
                    provider="finder_cpf",
                    lead_name=cpf,
                    query=f"/cpf {cpf}",
                    raw_text=None,
                    source_url=None,
                    downloaded_at=None,
                    error="disabled_in_test",
                )
            },
        )(),
    )

    response = client.post(
        f"/lead-tables/{table_id}/telegram-consult/multiple-experimental",
        json={"lead_refs": [leads[0].linkedin_url]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert default_called is False
    assert fake.calls == ["Ana Silva"]
    assert [c["provider"] for c in body["consults"][:3]] == [
        "finder",
        "gon",
        "unix",
    ]


def test_telegram_consult_telethon_experimental_is_separate_from_cdp_lookup(
    monkeypatch, app
) -> None:
    client = TestClient(app)
    leads = [_lead("Ana Silva")]
    table_id = _create_table(client, leads)

    default_called = False
    cdp_multi_called = False

    def fail_if_default_used(settings=None):  # noqa: ANN001, ARG001
        nonlocal default_called
        default_called = True
        raise AssertionError("default CDP lookup should not be used")

    def fail_if_cdp_multi_used(settings=None):  # noqa: ANN001, ARG001
        nonlocal cdp_multi_called
        cdp_multi_called = True
        raise AssertionError("CDP multiple lookup should not be used")

    fake = _FakeExperimentalOrchestrator()
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_telegram_consult_lookup",
        fail_if_default_used,
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_telegram_multi_experimental_lookup",
        fail_if_cdp_multi_used,
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_telegram_telethon_multi_experimental_lookup",
        lambda settings=None: fake,
    )

    response = client.post(
        f"/lead-tables/{table_id}/telegram-consult/telethon-experimental",
        json={"lead_refs": [leads[0].linkedin_url]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert default_called is False
    assert cdp_multi_called is False
    assert fake.calls == ["Ana Silva"]
    assert [c["provider"] for c in body["consults"]] == ["finder", "gon", "unix"]


def test_telethon_cpf_stage_uses_telethon_driver_not_playwright(monkeypatch, app) -> None:
    client = TestClient(app)
    leads = [_lead("Ana Silva")]
    table_id = _create_table(client, leads)
    store = get_saved_leads_store(app)
    store.save_telegram_consult(
        table_id=table_id,
        lead_ref=leads[0].linkedin_url,
        provider="finder",
        lead_name="Ana Silva",
        query="/nome Ana Silva",
        raw_text="Nome: Ana Silva\nCPF: 111.222.333-44",
        source_url=None,
        downloaded_at=None,
        error=None,
        extracted_candidates=[
            {
                "cpf": "111.222.333-44",
                "nome": "Ana Silva",
                "match_score": 90,
            }
        ],
        query_type="name",
    )

    playwright_called = False

    def fail_if_playwright_used(settings=None):  # noqa: ANN001, ARG001
        nonlocal playwright_called
        playwright_called = True
        raise AssertionError("Playwright CPF driver should not be used")

    class _FakeTelethonCpf:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def consult(self, cpf: str) -> TelegramConsultResult:
            self.calls.append(cpf)
            return TelegramConsultResult(
                provider="gon_cpf",
                lead_name="Ana Silva",
                query=f"/cpf {cpf}",
                raw_text=f"CPF: {cpf}\nTelefone: (11) 99999-0000",
                source_url="https://telethon.example/cpf",
                downloaded_at=datetime.now(timezone.utc).isoformat(),
                error=None,
            )

    telethon = _FakeTelethonCpf()
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_gonzales_cpf_consult",
        fail_if_playwright_used,
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_telethon_serasa_cpf_consult",
        lambda settings=None: telethon,
    )

    response = client.post(
        f"/lead-tables/{table_id}/telegram-phone/telethon-cpf-stage",
        json={"lead_ref": leads[0].linkedin_url, "cpf": "111.222.333-44"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert playwright_called is False
    assert telethon.calls == ["111.222.333-44"]
    assert body["summary"]["cpf_consults"] == 1
    assert body["summary"]["leads_with_phone"] == 1


def test_telethon_cpf_stage_accepts_persisted_primary_cpf(monkeypatch, app) -> None:
    client = TestClient(app)
    leads = [_lead("Ana Silva")]
    table_id = _create_table(client, leads)
    store = get_saved_leads_store(app)
    store.save_telegram_consult(
        table_id=table_id,
        lead_ref=leads[0].linkedin_url,
        provider="gon",
        lead_name="Ana Silva",
        query="/nome Ana Silva",
        raw_text="CPF Extraído: 111.222.333-44",
        source_url=None,
        downloaded_at=None,
        error=None,
        extracted_nome="Ana Silva",
        extracted_cpf="111.222.333-44",
        match_score=88,
        extracted_candidates=[],
        query_type="name",
    )

    class _FakeTelethonCpf:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def consult(self, cpf: str) -> TelegramConsultResult:
            self.calls.append(cpf)
            return TelegramConsultResult(
                provider="gon_cpf",
                lead_name="Ana Silva",
                query=f"/cpf {cpf}",
                raw_text=f"CPF: {cpf}\nTelefone: (11) 99999-0000",
                source_url="https://telethon.example/cpf",
                downloaded_at=datetime.now(timezone.utc).isoformat(),
                error=None,
            )

    telethon = _FakeTelethonCpf()
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_telethon_serasa_cpf_consult",
        lambda settings=None: telethon,
    )

    response = client.post(
        f"/lead-tables/{table_id}/telegram-phone/telethon-cpf-stage",
        json={"lead_ref": leads[0].linkedin_url, "cpf": "111.222.333-44"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert telethon.calls == ["111.222.333-44"]
    assert body["summary"]["cpf_consults"] == 1
    assert body["leads"][0]["blocked_reason"] is None
    assert body["leads"][0]["candidates"][0]["confidence"] == 88


def test_telegram_consult_multiple_experimental_runs_finder_cpf_on_consensus(
    monkeypatch, app
) -> None:
    client = TestClient(app)
    leads = [_lead("Ana Silva")]
    table_id = _create_table(client, leads)

    fake = _FakeExperimentalOrchestrator()

    class _FakeFinderCpf:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def consult(self, cpf: str) -> TelegramConsultResult:
            self.calls.append(cpf)
            return TelegramConsultResult(
                provider="finder_cpf",
                lead_name=cpf,
                query=f"/cpf {cpf}",
                raw_text="Telefone: (11) 99999-0000\n=== finder_fotos_salvas ===\ndata/telegram_artifacts/findex/foto.jpg",
                source_url="https://finder.example/cpf",
                downloaded_at=datetime.now(timezone.utc).isoformat(),
                error=None,
            )

    finder_cpf = _FakeFinderCpf()
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_telegram_multi_experimental_lookup",
        lambda settings=None: fake,
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_findex_cpf_consult",
        lambda settings=None: finder_cpf,
    )

    response = client.post(
        f"/lead-tables/{table_id}/telegram-consult/multiple-experimental",
        json={"lead_refs": [leads[0].linkedin_url]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    # Both CPFs carry the lead's exact name ("Ana Silva"), so under the
    # per-piece name scoring both clear the 65 follow-up threshold and become
    # targets. The consensus CPF (seen by finder+gon) still ranks FIRST; the
    # single-provider homonym follows. The per-lead cap keeps this bounded.
    assert finder_cpf.calls == ["111.222.333-44", "999.888.777-66"]
    finder_cpf_payload = next(
        c for c in body["consults"] if c["provider"] == "finder_cpf"
    )
    assert finder_cpf_payload["query_type"] == "cpf"
    assert "finder_fotos_salvas" in finder_cpf_payload["raw_text"]


def test_telegram_consult_multiple_experimental_runs_finder_cpf_for_finder_only_cpf(
    monkeypatch, app
) -> None:
    client = TestClient(app)
    leads = [_lead("Ana Silva")]
    table_id = _create_table(client, leads)

    class _FinderOnlyOrchestrator:
        def consult(self, lead_name: str) -> list[TelegramConsultResult]:
            now = datetime.now(timezone.utc).isoformat()
            return [
                TelegramConsultResult(
                    provider="finder",
                    lead_name=lead_name,
                    query=f"/nome {lead_name}",
                    raw_text='{"nome":"Ana Silva","cpf":"22233344455"}',
                    source_url="https://finder.example/result.json",
                    downloaded_at=now,
                    error=None,
                ),
                TelegramConsultResult(
                    provider="gon",
                    lead_name=lead_name,
                    query=f"/nome {lead_name}",
                    raw_text=None,
                    source_url=None,
                    downloaded_at=now,
                    error="no_result",
                ),
                TelegramConsultResult(
                    provider="unix",
                    lead_name=lead_name,
                    query=f"/nome {lead_name}",
                    raw_text=None,
                    source_url=None,
                    downloaded_at=now,
                    error="no_result",
                ),
            ]

    class _FakeFinderCpf:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def consult(self, cpf: str) -> TelegramConsultResult:
            self.calls.append(cpf)
            return TelegramConsultResult(
                provider="finder_cpf",
                lead_name=cpf,
                query=f"/cpf {cpf}",
                raw_text="Telefone: (11) 98888-7777",
                source_url="https://finder.example/cpf",
                downloaded_at=datetime.now(timezone.utc).isoformat(),
                error=None,
            )

    finder_cpf = _FakeFinderCpf()
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_telegram_multi_experimental_lookup",
        lambda settings=None: _FinderOnlyOrchestrator(),
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_findex_cpf_consult",
        lambda settings=None: finder_cpf,
    )

    response = client.post(
        f"/lead-tables/{table_id}/telegram-consult/multiple-experimental",
        json={"lead_refs": [leads[0].linkedin_url]},
    )

    assert response.status_code == 200, response.text
    assert finder_cpf.calls == ["222.333.444-55"]


def test_telegram_consult_lists_persisted_rows_with_provider_order(monkeypatch, app) -> None:
    client = TestClient(app)
    leads = [_lead("Ana Silva")]
    table_id = _create_table(client, leads)

    fake = _FakeOrchestrator(
        gon_by_name={
            "Ana Silva": TelegramConsultResult(
                provider="gon",
                lead_name="Ana Silva",
                query="/nome Ana Silva",
                raw_text="Nome: Ana\nCPF: 111.222.333-44",
                source_url="https://exemplo.com/r/g",
                downloaded_at="2026-05-20T00:00:00Z",
                error=None,
            )
        },
        unix_by_name={
            "Ana Silva": TelegramConsultResult(
                provider="unix",
                lead_name="Ana Silva",
                query="/nome Ana Silva",
                raw_text="Nome: Ana\nCPF: 999.888.777-66",
                source_url="https://exemplo.com/r/u",
                downloaded_at="2026-05-20T00:00:00Z",
                error=None,
            )
        },
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_telegram_consult_lookup",
        lambda settings=None: fake,
    )

    client.post(
        f"/lead-tables/{table_id}/telegram-consult",
        json={"lead_refs": [leads[0].linkedin_url]},
    )

    listed = client.get(f"/lead-tables/{table_id}/telegram-consults").json()
    assert [c["provider"] for c in listed["consults"]] == ["gon", "unix"]


def test_telegram_consult_list_reparses_legacy_raw_text(monkeypatch, app) -> None:
    """Rows saved before the parser learned the Unix dot-leader format
    have raw_text + CPF only. Listing should hydrate structured fields
    from the saved text so the UI does not require a new Telegram run.
    """
    import beautiful_linkedin.server.app as app_module

    client = TestClient(app)
    leads = [
        Lead(
            company_name="Empresa",
            company_domain="empresa.com",
            person_name="Ana Silva",
            title="Marketing",
            linkedin_url="https://linkedin.com/in/ana",
            source_url="https://linkedin.com/in/ana",
            source_type="linkedin_people_search",
            snippet="",
            confidence_score=80,
            linkedin_location="São Paulo, Brazil",
            linkedin_education=[{"institution": "USP", "end_year": 2007}],
        )
    ]
    table_id = _create_table(client, leads)
    store = app_module.get_saved_leads_store(app)
    store.save_telegram_consult(
        table_id=table_id,
        lead_ref="https://linkedin.com/in/ana",
        provider="unix",
        lead_name="Ana Silva",
        query="/nome Ana Silva",
        raw_text=(
            "NOME....................: ANA SILVA\n"
            "CPF.....................: 111.222.333-44\n"
            "DATA DE NASCIMENTO......: 15/03/1985\n"
            "ENDEREÇO COMPLETO.......: RUA X, SAO PAULO, SP\n"
        ),
        source_url=None,
        downloaded_at=None,
        error=None,
    )

    listed = client.get(f"/lead-tables/{table_id}/telegram-consults").json()
    row = listed["consults"][0]

    assert row["extracted_nome"] == "ANA SILVA"
    assert row["extracted_cpf"] == "111.222.333-44"
    assert row["extracted_birth_date"] == "15/03/1985"
    assert "SAO PAULO" in row["extracted_address"]
    assert row["extracted_candidates"][0]["match_score"] == 100


def test_telegram_consult_404_when_table_missing(monkeypatch, app) -> None:
    client = TestClient(app)
    fake = _FakeOrchestrator(gon_by_name={}, unix_by_name={})
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_telegram_consult_lookup",
        lambda settings=None: fake,
    )
    response = client.post(
        "/lead-tables/does-not-exist/telegram-consult",
        json={"lead_refs": ["ref-1"]},
    )
    assert response.status_code == 404


def test_telegram_consult_skips_lead_without_name(monkeypatch, app) -> None:
    client = TestClient(app)
    leads = [
        Lead(
            company_name="Empresa",
            company_domain="empresa.com",
            person_name=None,
            title="Marketing Manager",
            linkedin_url="https://linkedin.com/in/sem-nome",
            source_url="https://linkedin.com/in/sem-nome",
            source_type="linkedin_people_search",
            snippet="",
            confidence_score=80,
        )
    ]
    table_id = _create_table(client, leads)

    fake = _FakeOrchestrator(gon_by_name={}, unix_by_name={})
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_telegram_consult_lookup",
        lambda settings=None: fake,
    )

    response = client.post(
        f"/lead-tables/{table_id}/telegram-consult",
        json={"lead_refs": [leads[0].linkedin_url]},
    )
    assert response.status_code == 200
    body = response.json()
    assert fake.calls == []
    assert len(body["consults"]) == 1
    assert body["consults"][0]["error"] == "lead_sem_nome_ou_ref"


def test_telegram_consult_uses_lead_signals_to_rank_candidates(monkeypatch, app) -> None:
    """End-to-end: parser + matcher pipe through the endpoint so the
    persisted row carries ranked candidates with location signals."""
    client = TestClient(app)
    leads = [
        Lead(
            company_name="Empresa",
            company_domain="empresa.com",
            person_name="Ana Silva",
            title="Marketing",
            linkedin_url="https://linkedin.com/in/ana",
            source_url="https://linkedin.com/in/ana",
            source_type="linkedin_people_search",
            snippet="",
            confidence_score=80,
            linkedin_location="São Paulo, Brazil",
            linkedin_education=[{"institution": "USP", "end_year": 2007}],
        )
    ]
    table_id = _create_table(client, leads)

    raw = (
        "Nome: Ana Silva\nCPF: 111.222.333-44\nNascimento: 10/01/1985\n"
        "Endereço: Rua X, São Paulo/SP\n\n"
        "Nome: Ana Silva\nCPF: 999.888.777-66\nNascimento: 02/02/1965\n"
        "Endereço: Rua Y, Belém/PA"
    )
    fake = _FakeOrchestrator(
        gon_by_name={
            "Ana Silva": TelegramConsultResult(
                provider="gon",
                lead_name="Ana Silva",
                query="/nome Ana Silva",
                raw_text=raw,
                source_url=None,
                downloaded_at=None,
                error=None,
            )
        },
        unix_by_name={},
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_telegram_consult_lookup",
        lambda settings=None: fake,
    )

    response = client.post(
        f"/lead-tables/{table_id}/telegram-consult",
        json={"lead_refs": ["https://linkedin.com/in/ana"]},
    )
    body = response.json()
    gon = next(c for c in body["consults"] if c["provider"] == "gon")
    # SP candidate wins over Belém.
    assert gon["extracted_candidates"][0]["cpf"] == "111.222.333-44"
    assert gon["extracted_candidates"][0]["match_score"] >= 70
    assert gon["match_score"] == gon["extracted_candidates"][0]["match_score"]


def test_telegram_consult_refreshes_linkedin_signals_before_ranking(
    monkeypatch, app
) -> None:
    """The Telegram flow owns the comparison step: if the selected lead
    does not already carry location/education signals, it must open the
    LinkedIn profile validation pipeline before scoring CPF candidates.
    """
    import beautiful_linkedin.server.app as app_module
    from beautiful_linkedin.storage.linkedin_profile_validation import (
        LinkedInProfileValidationUpdate,
    )

    client = TestClient(app)
    leads = [
        Lead(
            company_name="Empresa",
            company_domain="empresa.com",
            person_name="Ana Silva",
            title="Marketing",
            linkedin_url="https://linkedin.com/in/ana",
            source_url="https://linkedin.com/in/ana",
            source_type="linkedin_people_search",
            snippet="",
            confidence_score=80,
        )
    ]
    table_id = _create_table(client, leads)
    validation_calls: list[list[str | None]] = []

    def fake_validation(**kwargs):
        selected = kwargs["leads"]
        validation_calls.append([lead.linkedin_url for lead in selected])
        return [
            (
                selected[0],
                LinkedInProfileValidationUpdate(
                    status="validated",
                    location="São Paulo, São Paulo, Brasil",
                    education=[
                        {
                            "institution": "USP",
                            "degree": "Administração",
                            "end_year": 2007,
                            "period": "2003 - 2007",
                        }
                    ],
                ),
            )
        ]

    monkeypatch.setattr(app_module, "_run_linkedin_profile_validation", fake_validation)
    monkeypatch.setattr(app_module, "probe_cdp_endpoint", lambda *args, **kwargs: True)

    raw = (
        "▸ PESSOA 1\n"
        "NOME....................: ANA SILVA\n"
        "CPF.....................: 111.222.333-44\n"
        "DATA DE NASCIMENTO......: 15/03/1985\n"
        "ENDEREÇO COMPLETO.......: RUA X, SAO PAULO, SP\n"
        "\n"
        "▸ PESSOA 2\n"
        "NOME....................: ANA SILVA\n"
        "CPF.....................: 999.888.777-66\n"
        "DATA DE NASCIMENTO......: 02/02/1965\n"
        "ENDEREÇO COMPLETO.......: RUA Y, SALVADOR, BA\n"
    )
    fake = _FakeOrchestrator(
        gon_by_name={
            "Ana Silva": TelegramConsultResult(
                provider="gon",
                lead_name="Ana Silva",
                query="/nome Ana Silva",
                raw_text=raw,
                source_url=None,
                downloaded_at=None,
                error=None,
            )
        },
        unix_by_name={},
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_telegram_consult_lookup",
        lambda settings=None: fake,
    )

    response = client.post(
        f"/lead-tables/{table_id}/telegram-consult",
        json={"lead_refs": ["https://linkedin.com/in/ana"]},
    )

    assert response.status_code == 200, response.text
    assert validation_calls == [["https://linkedin.com/in/ana"]]
    gon = next(c for c in response.json()["consults"] if c["provider"] == "gon")
    assert gon["extracted_candidates"][0]["cpf"] == "111.222.333-44"
    assert gon["extracted_candidates"][0]["match_score"] == 100
    assert set(gon["extracted_candidates"][0]["signals_used"]) == {
        "name",
        "location",
        "education_age",
        "data_quality",
    }


def test_telegram_consult_refreshes_when_only_location_is_missing(
    monkeypatch, app
) -> None:
    """Lead já tem education salvo (de uma validação anterior) mas a
    location ficou em branco — talvez o parser não conseguiu extrair
    naquela rodada. Mesmo assim, o refresh deve disparar para tentar
    pegar a location agora, porque ela é o sinal mais forte contra
    homônimos. Antes do fix, o gate ``not (loc or edu)`` considerava o
    lead "ok" e nunca completava o sinal mais valioso.
    """
    import beautiful_linkedin.server.app as app_module
    from beautiful_linkedin.storage.linkedin_profile_validation import (
        LinkedInProfileValidationUpdate,
    )

    client = TestClient(app)
    leads = [
        Lead(
            company_name="Empresa",
            company_domain="empresa.com",
            person_name="Gustavo Acacio",
            title="Marketing",
            linkedin_url="https://linkedin.com/in/gustavo",
            source_url="https://linkedin.com/in/gustavo",
            source_type="linkedin_people_search",
            snippet="",
            confidence_score=80,
            # education JÁ existe — mas sem location o matcher cai
            # para "nome + age + data_quality" e não diferencia
            # homônimos.
            linkedin_education=[
                {"institution": "USP", "end_year": 2010},
            ],
        )
    ]
    table_id = _create_table(client, leads)
    validation_calls: list[list[str | None]] = []

    def fake_validation(**kwargs):
        selected = kwargs["leads"]
        validation_calls.append([lead.linkedin_url for lead in selected])
        return [
            (
                selected[0],
                LinkedInProfileValidationUpdate(
                    status="validated",
                    location="São Paulo, São Paulo, Brasil",
                    education=selected[0].linkedin_education,
                ),
            )
        ]

    monkeypatch.setattr(app_module, "_run_linkedin_profile_validation", fake_validation)
    monkeypatch.setattr(app_module, "probe_cdp_endpoint", lambda *args, **kwargs: True)

    fake = _FakeOrchestrator(
        gon_by_name={
            "Gustavo Acacio": TelegramConsultResult(
                provider="gon",
                lead_name="Gustavo Acacio",
                query="/nome Gustavo Acacio",
                raw_text=(
                    "NOME: Gustavo Acacio\nCPF: 220.440.418-71\n"
                    "DATA DE NASCIMENTO: 10/03/1980\n"
                    "ENDEREÇO: RUA X, SAO PAULO, SP\n"
                ),
                source_url=None,
                downloaded_at=None,
                error=None,
            )
        },
        unix_by_name={},
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_telegram_consult_lookup",
        lambda settings=None: fake,
    )

    response = client.post(
        f"/lead-tables/{table_id}/telegram-consult",
        json={"lead_refs": ["https://linkedin.com/in/gustavo"]},
    )

    assert response.status_code == 200, response.text
    # The refresh DID fire even though education was already present.
    assert validation_calls == [["https://linkedin.com/in/gustavo"]]
    gon = next(c for c in response.json()["consults"] if c["provider"] == "gon")
    assert "location" in gon["extracted_candidates"][0]["signals_used"]


def test_telegram_consult_uses_linkedin_career_age_to_reject_old_homonym(
    monkeypatch, app
) -> None:
    client = TestClient(app)
    leads = [
        Lead(
            company_name="Empresa",
            company_domain="empresa.com",
            person_name="Ana Silva",
            title="Marketing",
            linkedin_url="https://linkedin.com/in/ana",
            source_url="https://linkedin.com/in/ana",
            source_type="linkedin_people_search",
            snippet="",
            confidence_score=80,
            linkedin_location="São Paulo, Brazil",
            linkedin_experience_title="Analista de Marketing Junior",
            linkedin_experience_start_year=2022,
        )
    ]
    table_id = _create_table(client, leads)

    raw = (
        "NOME....................: ANA SILVA\n"
        "CPF.....................: 111.222.333-44\n"
        "DATA DE NASCIMENTO......: 10/05/1947\n"
        "ENDEREÇO COMPLETO.......: RUA X, SAO PAULO, SP\n"
        "\n"
        "NOME....................: ANA SILVA\n"
        "CPF.....................: 999.888.777-66\n"
        "DATA DE NASCIMENTO......: 15/03/1998\n"
        "ENDEREÇO COMPLETO.......: RUA Y, SAO PAULO, SP\n"
    )
    fake = _FakeOrchestrator(
        gon_by_name={
            "Ana Silva": TelegramConsultResult(
                provider="gon",
                lead_name="Ana Silva",
                query="/nome Ana Silva",
                raw_text=raw,
                source_url=None,
                downloaded_at=None,
                error=None,
            )
        },
        unix_by_name={},
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_telegram_consult_lookup",
        lambda settings=None: fake,
    )

    response = client.post(
        f"/lead-tables/{table_id}/telegram-consult",
        json={"lead_refs": ["https://linkedin.com/in/ana"]},
    )

    assert response.status_code == 200, response.text
    gon = next(c for c in response.json()["consults"] if c["provider"] == "gon")
    assert gon["extracted_candidates"][0]["cpf"] == "999.888.777-66"
    assert gon["extracted_candidates"][0]["match_score"] >= 85
    old = next(c for c in gon["extracted_candidates"] if c["cpf"] == "111.222.333-44")
    assert old["match_score"] <= 20
    assert old["breakdown"]["penalties"][0]["code"] == "career_age_implausible"


def test_telethon_pipeline_phone_stage_is_limited_to_best_cpf(
    monkeypatch, app
) -> None:
    client = TestClient(app)
    leads = [_lead("Ana Silva")]
    table_id = _create_table(client, leads)
    seen_max_candidates: list[int] = []

    class _FakeCpfDriver:
        def consult(self, cpf: str) -> TelegramConsultResult:  # pragma: no cover
            raise AssertionError("phone follow-up is monkeypatched")

    monkeypatch.setattr(
        "beautiful_linkedin.server.app._refresh_linkedin_signals_for_telegram",
        lambda *, selected, **kwargs: selected,
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_telegram_telethon_multi_experimental_lookup",
        lambda settings=None: object(),
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_telethon_serasa_cpf_consult",
        lambda settings=None: _FakeCpfDriver(),
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._run_telegram_consult",
        lambda **kwargs: [],
    )

    def fake_phone_followup(**kwargs):
        seen_max_candidates.append(kwargs["max_candidates"])
        return []

    monkeypatch.setattr(
        "beautiful_linkedin.server.app._pipeline_run_phone_followup",
        fake_phone_followup,
    )

    response = client.post(
        f"/lead-tables/{table_id}/telegram-consult/telethon-pipeline",
        json={
            "lead_refs": [leads[0].linkedin_url],
            "max_leads": 10,
            "max_cpf_candidates": 5,
        },
    )

    assert response.status_code == 200, response.text
    assert seen_max_candidates == [1]
