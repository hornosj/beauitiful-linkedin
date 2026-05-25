"""Tests for ``POST /lead-tables/{id}/telegram-followup-phone``.

The endpoint glues the CPF-stage workflow to:

- the FastAPI surface (request validation, 404/422 handling),
- the persistent name-stage rows that drive candidate selection,
- the existing phone enrichment update path that keeps the
  ``phone_alternatives`` trail intact.

These tests stay fully offline by monkeypatching
``_default_gonzales_cpf_consult`` with a deterministic stub.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beautiful_linkedin.models import Lead
from beautiful_linkedin.server.app import build_app, get_saved_leads_store
from beautiful_linkedin.storage.telegram_group_playwright_lookup import (
    TelegramConsultResult,
)


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "beautiful_linkedin.server.app.probe_cdp_endpoint",
        lambda *args, **kwargs: False,
    )
    return build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))


class _FakeCpfDriver:
    """Stub matching :class:`GonzalesCpfConsult`'s ``consult(cpf)``
    contract. Returns a deterministic payload per CPF so the test can
    assert the persistence path end-to-end.
    """

    provider = "gon_cpf"

    def __init__(self, phone_by_cpf: dict[str, str]) -> None:
        self._phone_by_cpf = phone_by_cpf
        self.calls: list[str] = []

    def consult(self, cpf: str) -> TelegramConsultResult:
        self.calls.append(cpf)
        phone = self._phone_by_cpf.get(cpf)
        if phone is None:
            return TelegramConsultResult(
                provider=self.provider,
                lead_name="",
                query=f"/cpf {cpf}",
                raw_text=f"CPF: {cpf}\nNenhum telefone encontrado.",
                source_url=None,
                downloaded_at=None,
                error=None,
            )
        return TelegramConsultResult(
            provider=self.provider,
            lead_name="",
            query=f"/cpf {cpf}",
            raw_text=f"CPF: {cpf}\nTelefone: {phone}",
            source_url=f"https://exemplo.com/r/{cpf}",
            downloaded_at=None,
            error=None,
        )


def _lead(person: str = "Ana Silva", *, phone: str | None = None) -> Lead:
    url = f"https://linkedin.com/in/{person.lower().replace(' ', '-')}"
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
        linkedin_location="São Paulo, Brazil",
        phone=phone,
    )


def _create_table_with_leads(client: TestClient, leads: list[Lead]) -> str:
    return client.post(
        "/lead-tables",
        json={
            "name": "Telegram followup tests",
            "leads": [lead.model_dump(mode="json") for lead in leads],
        },
    ).json()["table"]["id"]


def _seed_cpf_candidate(
    store, table_id: str, lead_ref: str, *, cpf: str, score: int, lead_name: str
) -> None:
    store.save_telegram_consult(
        table_id=table_id,
        lead_ref=lead_ref,
        provider="gon",
        lead_name=lead_name,
        query=f"/nome {lead_name}",
        raw_text="…",
        source_url=None,
        downloaded_at=None,
        error=None,
        extracted_nome=lead_name,
        extracted_cpf=cpf,
        extracted_candidates=[
            {
                "cpf": cpf,
                "nome": lead_name,
                "data_nascimento": "10/01/1985",
                "endereco": "Rua X, São Paulo/SP",
                "match_score": score,
                "signals_used": ["name", "location"],
                "breakdown": {"confidence_label": "alta"},
            }
        ],
        match_score=score,
        run_id="seed-run",
        query_type="name",
    )


def test_followup_phone_persists_phone_via_alternatives_trail(
    monkeypatch, app
) -> None:
    client = TestClient(app)
    leads = [_lead("Ana Silva")]
    table_id = _create_table_with_leads(client, leads)
    store = get_saved_leads_store(app)
    _seed_cpf_candidate(
        store,
        table_id,
        lead_ref=leads[0].linkedin_url,
        cpf="111.222.333-44",
        score=90,
        lead_name="Ana Silva",
    )

    driver = _FakeCpfDriver({"111.222.333-44": "(11) 99999-0000"})
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_gonzales_cpf_consult",
        lambda settings=None: driver,
    )

    response = client.post(
        f"/lead-tables/{table_id}/telegram-followup-phone",
        json={"lead_refs": [leads[0].linkedin_url]},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert driver.calls == ["111.222.333-44"]
    assert body["summary"]["requested_leads"] == 1
    assert body["summary"]["leads_with_phone"] == 1
    candidates = body["leads"][0]["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["confidence"] == 90
    assert candidates[0]["cpf"] == "111.222.333-44"
    # Phone landed in the alternatives trail because the lead had no
    # primary phone yet — first non-null phone becomes the primary via
    # the COALESCE in apply_internal_phone_enrichment_updates.
    persisted = store.list_leads(table_id)[0]
    assert persisted.phone is not None
    assert persisted.phone.startswith("+55")


def test_followup_phone_ignores_target_titles(monkeypatch, app) -> None:
    # Title gate was removed: operator selection IS the decision to
    # spend Telegram quota. Even with a LinkedIn cargo that diverges
    # from the table's target_titles, the follow-up runs /cpf on the
    # persisted candidate.
    client = TestClient(app)
    lead = Lead(
        company_name="Empresa",
        company_domain="empresa.com",
        person_name="Ana Silva",
        title="Marketing Manager",
        linkedin_url="https://linkedin.com/in/ana",
        source_url="https://linkedin.com/in/ana",
        source_type="linkedin_people_search",
        snippet="",
        confidence_score=80,
        linkedin_location="São Paulo, Brazil",
        linkedin_experience_title="Engenheiro de Software",
    )
    table_id = _create_table_with_leads(client, [lead])
    store = get_saved_leads_store(app)
    _seed_cpf_candidate(
        store,
        table_id,
        lead_ref=lead.linkedin_url,
        cpf="111.222.333-44",
        score=90,
        lead_name="Ana Silva",
    )

    driver = _FakeCpfDriver({"111.222.333-44": "(11) 99999-0000"})
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_gonzales_cpf_consult",
        lambda settings=None: driver,
    )

    response = client.post(
        f"/lead-tables/{table_id}/telegram-followup-phone",
        json={
            "lead_refs": [lead.linkedin_url],
            "target_titles": ["marketing"],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert driver.calls == ["111.222.333-44"]
    assert body["summary"]["leads_blocked"] == 0
    assert body["leads"][0]["blocked_reason"] != "linkedin_cargo_divergente"


def test_followup_phone_404_when_table_missing(monkeypatch, app) -> None:
    client = TestClient(app)
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_gonzales_cpf_consult",
        lambda settings=None: _FakeCpfDriver({}),
    )

    response = client.post(
        "/lead-tables/does-not-exist/telegram-followup-phone",
        json={"lead_refs": ["ref-1"]},
    )
    assert response.status_code == 404


def test_followup_phone_does_not_overwrite_existing_lead_phone(
    monkeypatch, app
) -> None:
    client = TestClient(app)
    leads = [_lead("Ana Silva", phone="+5511988887777")]
    table_id = _create_table_with_leads(client, leads)
    store = get_saved_leads_store(app)
    _seed_cpf_candidate(
        store,
        table_id,
        lead_ref=leads[0].linkedin_url,
        cpf="111.222.333-44",
        score=85,
        lead_name="Ana Silva",
    )

    driver = _FakeCpfDriver({"111.222.333-44": "(21) 90000-0000"})
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_gonzales_cpf_consult",
        lambda settings=None: driver,
    )

    response = client.post(
        f"/lead-tables/{table_id}/telegram-followup-phone",
        json={"lead_refs": [leads[0].linkedin_url]},
    )
    assert response.status_code == 200, response.text

    persisted = store.list_leads(table_id)[0]
    assert persisted.phone == "+5511988887777"
    sources = [alt.get("source") for alt in persisted.phone_alternatives]
    assert any("telegram_consult_cpf" in (s or "") for s in sources)
    assert response.json()["summary"]["skipped_existing_phone"] >= 1
