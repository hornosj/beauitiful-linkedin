"""Tests for the unified ``/nome`` → ``/cpf`` → phone flow.

Covers the contract for ``run_extract_phone_via_cpf`` (the in-process
workflow) and ``POST /lead-tables/{id}/telegram-phone`` (the FastAPI
endpoint the UI calls).

All scenarios run offline via injected fakes — no Playwright, no DNS,
no real Telegram.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beautiful_linkedin.models import Lead
from beautiful_linkedin.server.app import build_app, get_saved_leads_store
from beautiful_linkedin.storage.saved_leads import SavedLeadsStore
from beautiful_linkedin.storage.telegram_group_playwright_lookup import (
    TelegramConsultResult,
)
from beautiful_linkedin.storage.telegram_pipeline import (
    TelegramPhoneFlowResult,
    run_extract_phone_via_cpf,
)


# ---------------------------------------------------------------------------
# Workflow-level tests (run_extract_phone_via_cpf)
# ---------------------------------------------------------------------------


def _lead(
    person: str = "Ana Silva",
    *,
    email: str | None = None,
    phone: str | None = None,
    linkedin_experience_title: str | None = None,
) -> Lead:
    url = f"https://linkedin.com/in/{person.lower().replace(' ', '-')}"
    return Lead(
        company_name="Empresa",
        company_domain="empresa.com",
        person_name=person,
        title="Marketing Manager",
        linkedin_url=url,
        email=email,
        source_url=url,
        source_type="linkedin_people_search",
        snippet="",
        confidence_score=80,
        linkedin_location="São Paulo, Brazil",
        # Education anchors the age signal so the matcher can score this
        # candidate above the follow-up threshold without needing
        # synthetic experience years on every test.
        linkedin_education=[
            {"institution": "USP", "degree": "Administração", "end_year": 2007}
        ],
        linkedin_experience_title=linkedin_experience_title,
        phone=phone,
    )


def _store(tmp_path: Path) -> SavedLeadsStore:
    return SavedLeadsStore(str(tmp_path / "saved.sqlite"))


def _table(store: SavedLeadsStore, leads: list[Lead]) -> str:
    table = store.create_table(name="Unified flow tests")
    store.add_leads(table.id, leads)
    return table.id


def _name_result(lead_name: str, raw: str) -> TelegramConsultResult:
    return TelegramConsultResult(
        provider="gon",
        lead_name=lead_name,
        query=f"/nome {lead_name}",
        raw_text=raw,
        source_url="https://exemplo.com/r/nome",
        downloaded_at=None,
        error=None,
    )


def _cpf_result(cpf: str, phone: str) -> TelegramConsultResult:
    return TelegramConsultResult(
        provider="gon_cpf",
        lead_name="",
        query=f"/cpf {cpf}",
        raw_text=f"CPF: {cpf}\nTelefone: {phone}",
        source_url=f"https://exemplo.com/r/cpf/{cpf}",
        downloaded_at=None,
        error=None,
    )


def test_unified_workflow_blocks_when_lead_has_no_linkedin_signals(
    tmp_path: Path,
) -> None:
    """Lead sem âncora de localização/educação/experiência → o gate de
    sinais bloqueia antes de qualquer tráfego Telegram. Sem âncora o
    matcher só compara nome + data_quality, e homônimos do CPF alheio
    passam o min_score — o gate evita queimar a quota nessa situação.
    """
    lead = Lead(
        company_name="Empresa",
        company_domain="empresa.com",
        person_name="Ana Silva",
        title="Marketing Manager",
        linkedin_url="https://linkedin.com/in/ana-silva-skinny",
        source_url="https://linkedin.com/in/ana-silva-skinny",
        source_type="linkedin_people_search",
        snippet="",
        confidence_score=80,
        # Nenhum sinal LinkedIn populado.
    )
    store = _store(tmp_path)
    table_id = _table(store, [lead])

    name_calls: list[str] = []
    cpf_calls: list[str] = []

    def name_fn(lead_name: str) -> TelegramConsultResult:
        name_calls.append(lead_name)
        raise AssertionError("name stage must not run without LinkedIn signals")

    def cpf_fn(cpf: str) -> TelegramConsultResult:
        cpf_calls.append(cpf)
        raise AssertionError("cpf stage must not run without LinkedIn signals")

    result = run_extract_phone_via_cpf(
        lead=lead,
        table_id=table_id,
        store=store,
        name_consult_fn=name_fn,
        cpf_consult_fn=cpf_fn,
        target_titles=None,
        run_id="run-signals-gate",
    )

    assert name_calls == []
    assert cpf_calls == []
    assert result.blocked_reason == "missing_linkedin_signals"
    rows = store.list_telegram_consults(table_id)
    assert len(rows) == 1
    assert rows[0].blocked_reason == "missing_linkedin_signals"


def test_unified_workflow_rejects_cpf_with_completely_different_name(
    tmp_path: Path,
) -> None:
    """Bug Totvs: lead 'Nadia Ramos' recebia CPFs de 'Guilherme Ferreira'.
    O gate de nome agora rejeita o candidato antes do /cpf, mesmo com
    age/location razoáveis. Repro do bug + asserção do fix.
    """
    lead = _lead(person="Nadia Ramos")
    store = _store(tmp_path)
    table_id = _table(store, [lead])

    cpf_calls: list[str] = []

    def name_fn(lead_name: str) -> TelegramConsultResult:
        # Bot devolve um homônimo com mesmo endereço/idade do lead, mas
        # nome completamente diferente — clássico vazamento do Telegram.
        return _name_result(
            lead_name,
            "Nome: Guilherme Ferreira\nCPF: 999.888.777-66\n"
            "Endereço: Rua X, São Paulo/SP\nNascimento: 10/01/1985",
        )

    def cpf_fn(cpf: str) -> TelegramConsultResult:
        cpf_calls.append(cpf)
        raise AssertionError("cpf must NOT run for name-mismatched candidate")

    result = run_extract_phone_via_cpf(
        lead=lead,
        table_id=table_id,
        store=store,
        name_consult_fn=name_fn,
        cpf_consult_fn=cpf_fn,
        target_titles=None,
        run_id="run-name-gate",
    )

    assert cpf_calls == []
    assert result.blocked_reason in {"no_eligible_cpf", "no_cpf_from_name_stage"}
    name_rows = [
        row for row in store.list_telegram_consults(table_id)
        if row.query_type == "name"
    ]
    assert name_rows, "name stage row must be persisted for audit"
    payload = name_rows[0].extracted_candidates[0]
    assert payload["match_score"] == 0
    assert payload["breakdown"].get("rejected") is True


def test_unified_workflow_runs_name_then_cpf_atomically(tmp_path: Path) -> None:
    lead = _lead()
    store = _store(tmp_path)
    table_id = _table(store, [lead])

    name_calls: list[str] = []
    cpf_calls: list[str] = []

    def name_fn(lead_name: str) -> TelegramConsultResult:
        name_calls.append(lead_name)
        return _name_result(
            lead_name,
            "Nome: Ana Silva\nCPF: 111.222.333-44\n"
            "Nascimento: 10/01/1985\nEndereço: Rua X, São Paulo/SP",
        )

    def cpf_fn(cpf: str) -> TelegramConsultResult:
        cpf_calls.append(cpf)
        return _cpf_result(cpf, "(11) 99999-0000")

    result = run_extract_phone_via_cpf(
        lead=lead,
        table_id=table_id,
        store=store,
        name_consult_fn=name_fn,
        cpf_consult_fn=cpf_fn,
        target_titles=None,
        run_id="run-unified-1",
    )

    assert name_calls == ["Ana Silva"]
    assert cpf_calls == ["111.222.333-44"]
    assert result.blocked_reason is None
    assert len(result.phone_candidates) == 1
    phone = result.phone_candidates[0]
    assert phone.cpf == "111.222.333-44"
    assert phone.confidence == phone.provenance["cpf_match_score"]
    assert phone.phone_digits == "11999990000"
    # Both stages persisted.
    rows = store.list_telegram_consults(table_id)
    stages = {row.query_type for row in rows}
    assert stages == {"name", "cpf"}


def test_unified_workflow_logs_each_observable_stage(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    lead = _lead()
    store = _store(tmp_path)
    table_id = _table(store, [lead])

    def name_fn(lead_name: str) -> TelegramConsultResult:
        return _name_result(
            lead_name,
            "Nome: Ana Silva\nCPF: 111.222.333-44\n"
            "Nascimento: 10/01/1985\nEndereço: Rua X, São Paulo/SP",
        )

    def cpf_fn(cpf: str) -> TelegramConsultResult:
        return _cpf_result(cpf, "(11) 99999-0000")

    with caplog.at_level(
        logging.INFO, logger="beautiful_linkedin.storage.telegram_pipeline"
    ):
        run_extract_phone_via_cpf(
            lead=lead,
            table_id=table_id,
            store=store,
            name_consult_fn=name_fn,
            cpf_consult_fn=cpf_fn,
            target_titles=None,
            run_id="run-log-1",
        )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "telegram_phone_stage stage=run_started" in messages
    assert "telegram_phone_stage stage=signals_gate_passed" in messages
    assert "telegram_phone_stage stage=gonzales_name_sent" in messages
    assert "telegram_phone_stage stage=gonzales_name_parsed" in messages
    assert "telegram_phone_stage stage=gonzales_cpf_sent" in messages
    assert "telegram_phone_stage stage=gonzales_cpf_parsed" in messages
    assert "telegram_phone_stage stage=completed" in messages
    assert "run_id=run-log-1" in messages
    assert f"table_id={table_id}" in messages


def test_unified_workflow_uses_persisted_verified_cpf_before_name(
    tmp_path: Path,
) -> None:
    lead = _lead()
    store = _store(tmp_path)
    table_id = _table(store, [lead])
    lead_ref = lead.linkedin_url or ""
    store.save_telegram_consult(
        table_id=table_id,
        lead_ref=lead_ref,
        provider="gon",
        lead_name=lead.person_name or "",
        query="/nome Ana Silva",
        raw_text="Nome: Ana Silva\nCPF: 111.222.333-44",
        source_url=None,
        downloaded_at=None,
        error=None,
        extracted_nome="Ana Silva",
        extracted_cpf="111.222.333-44",
        extracted_candidates=[
            {
                "cpf": "111.222.333-44",
                "nome": "Ana Silva",
                "match_score": 90,
                "signals_used": ["name", "location"],
                "breakdown": {"confidence_label": "alta"},
            }
        ],
        match_score=90,
        query_type="name",
        run_id="previous-run",
    )

    name_calls: list[str] = []
    cpf_calls: list[str] = []

    def name_fn(value: str) -> TelegramConsultResult:
        name_calls.append(value)
        raise AssertionError("persisted verified CPF must skip /nome")

    def cpf_fn(cpf: str) -> TelegramConsultResult:
        cpf_calls.append(cpf)
        return _cpf_result(cpf, "(11) 99999-0000")

    result = run_extract_phone_via_cpf(
        lead=lead,
        table_id=table_id,
        store=store,
        name_consult_fn=name_fn,
        cpf_consult_fn=cpf_fn,
        target_titles=None,
        run_id="run-from-persisted-cpf",
    )

    assert name_calls == []
    assert cpf_calls == ["111.222.333-44"]
    assert result.name_consult is not None
    assert result.name_consult.run_id == "previous-run"
    assert len(result.phone_candidates) == 1


def test_unified_workflow_falls_back_to_findex_email_when_name_has_no_cpf(
    tmp_path: Path,
) -> None:
    lead = _lead(email="ana.silva@empresa.com")
    store = _store(tmp_path)
    table_id = _table(store, [lead])

    cpf_calls: list[str] = []
    email_calls: list[str] = []

    def name_fn(lead_name: str) -> TelegramConsultResult:
        return _name_result(lead_name, "Nenhum CPF encontrado para este nome.")

    def cpf_fn(value: str) -> TelegramConsultResult:
        cpf_calls.append(value)
        raise AssertionError("cpf stage must not run without a CPF")

    def email_fn(email: str) -> TelegramConsultResult:
        email_calls.append(email)
        return TelegramConsultResult(
            provider="findex",
            lead_name=lead.person_name or "",
            query=f"/email {email}",
            raw_text=f"E-mail: {email}\nTelefone: (11) 97777-0000",
            source_url="https://exemplo.com/findex/email",
            downloaded_at=None,
            error=None,
        )

    result = run_extract_phone_via_cpf(
        lead=lead,
        table_id=table_id,
        store=store,
        name_consult_fn=name_fn,
        cpf_consult_fn=cpf_fn,
        email_consult_fn=email_fn,
        target_titles=None,
        run_id="run-email-fallback",
    )

    assert cpf_calls == []
    assert email_calls == ["ana.silva@empresa.com"]
    assert result.blocked_reason is None
    assert result.cpf_consult is not None
    assert result.cpf_consult.query_type == "email"
    assert result.phone_candidates[0].source_provider == "findex"
    assert result.phone_candidates[0].phone_digits == "11977770000"


def test_unified_workflow_falls_back_to_findex_linkedin_contact_email(
    tmp_path: Path,
) -> None:
    lead = _lead(email=None)
    lead.linkedin_contact_email = "ana.linkedin@gmail.com"
    store = _store(tmp_path)
    table_id = _table(store, [lead])

    email_calls: list[str] = []

    def name_fn(lead_name: str) -> TelegramConsultResult:
        return _name_result(lead_name, "Nenhum CPF encontrado para este nome.")

    def cpf_fn(value: str) -> TelegramConsultResult:
        raise AssertionError("cpf stage must not run without a CPF")

    def email_fn(email: str) -> TelegramConsultResult:
        email_calls.append(email)
        return TelegramConsultResult(
            provider="findex",
            lead_name=lead.person_name or "",
            query=f"/email {email}",
            raw_text=f"E-mail: {email}\nTelefone: (11) 96666-0000",
            source_url="https://exemplo.com/findex/email",
            downloaded_at=None,
            error=None,
        )

    result = run_extract_phone_via_cpf(
        lead=lead,
        table_id=table_id,
        store=store,
        name_consult_fn=name_fn,
        cpf_consult_fn=cpf_fn,
        email_consult_fn=email_fn,
        target_titles=None,
        run_id="run-linkedin-email-fallback",
    )

    assert email_calls == ["ana.linkedin@gmail.com"]
    assert result.cpf_consult is not None
    assert result.cpf_consult.query_type == "email"
    assert result.phone_candidates[0].phone_digits == "11966660000"
    assert result.phone_candidates[0].provenance.get("email_source") == "linkedin_contact"


def test_unified_workflow_routes_signals_block_to_findex_when_email_exists(
    tmp_path: Path,
) -> None:
    # Lead has NO LinkedIn signals (no location, no education, no career
    # anchor) — signals_gate would otherwise block silently. With an e-mail
    # available, the pipeline must skip /nome (homonym risk) and dispatch
    # /usa <email> directly. The marker row keeps audit trail with reason
    # ``missing_linkedin_signals_routed_to_findex``.
    lead = Lead(
        company_name="Empresa",
        company_domain="empresa.com",
        person_name="Ana Silva",
        title="Marketing Manager",
        linkedin_url="https://linkedin.com/in/ana-silva",
        email="ana@empresa.com",
        source_url="https://linkedin.com/in/ana-silva",
        source_type="linkedin_people_search",
        snippet="",
        confidence_score=80,
    )
    store = _store(tmp_path)
    table_id = _table(store, [lead])

    name_calls: list[str] = []
    email_calls: list[str] = []

    def name_fn(lead_name: str) -> TelegramConsultResult:
        name_calls.append(lead_name)
        raise AssertionError("name stage must be skipped when signals_gate routes to findex")

    def cpf_fn(value: str) -> TelegramConsultResult:
        raise AssertionError("cpf stage must not run")

    def email_fn(email: str) -> TelegramConsultResult:
        email_calls.append(email)
        return TelegramConsultResult(
            provider="findex",
            lead_name=lead.person_name or "",
            query=f"/email {email}",
            raw_text=f"E-mail: {email}\nTelefone: (11) 94444-0000",
            source_url="https://exemplo.com/findex",
            downloaded_at=None,
            error=None,
        )

    result = run_extract_phone_via_cpf(
        lead=lead,
        table_id=table_id,
        store=store,
        name_consult_fn=name_fn,
        cpf_consult_fn=cpf_fn,
        email_consult_fn=email_fn,
        target_titles=None,
        run_id="run-signals-to-findex",
    )

    assert name_calls == []
    assert email_calls == ["ana@empresa.com"]
    assert result.phone_candidates[0].phone_digits == "11944440000"
    assert result.phone_candidates[0].provenance.get("email_source") == "primary"
    assert result.name_consult is not None
    assert "routed_to_findex" in (result.name_consult.blocked_reason or "")


def test_unified_workflow_still_blocks_when_signals_and_email_missing(
    tmp_path: Path,
) -> None:
    lead = Lead(
        company_name="Empresa",
        company_domain="empresa.com",
        person_name="Ana Silva",
        title="Marketing Manager",
        linkedin_url="https://linkedin.com/in/ana-silva",
        email=None,
        source_url="https://linkedin.com/in/ana-silva",
        source_type="linkedin_people_search",
        snippet="",
        confidence_score=80,
    )
    store = _store(tmp_path)
    table_id = _table(store, [lead])

    def name_fn(lead_name: str) -> TelegramConsultResult:
        raise AssertionError("name stage must not run when signals missing")

    def cpf_fn(value: str) -> TelegramConsultResult:
        raise AssertionError("cpf stage must not run")

    def email_fn(email: str) -> TelegramConsultResult:
        raise AssertionError("email stage must not run without an email")

    result = run_extract_phone_via_cpf(
        lead=lead,
        table_id=table_id,
        store=store,
        name_consult_fn=name_fn,
        cpf_consult_fn=cpf_fn,
        email_consult_fn=email_fn,
        target_titles=None,
        run_id="run-signals-hard-block",
    )

    assert result.blocked_reason == "missing_linkedin_signals"
    assert result.phone_candidates == []


def test_unified_workflow_emits_stage_events_on_happy_path(
    tmp_path: Path,
) -> None:
    lead = _lead()
    store = _store(tmp_path)
    table_id = _table(store, [lead])

    def name_fn(lead_name: str) -> TelegramConsultResult:
        return _name_result(
            lead_name,
            "Nome: Ana Silva\nCPF: 111.222.333-44\n"
            "Nascimento: 10/01/1985\nEndereço: Rua X, São Paulo/SP",
        )

    def cpf_fn(value: str) -> TelegramConsultResult:
        return _cpf_result(value, "(11) 91111-0000")

    def email_fn(email: str) -> TelegramConsultResult:
        raise AssertionError("email stage must not run on happy path")

    result = run_extract_phone_via_cpf(
        lead=lead,
        table_id=table_id,
        store=store,
        name_consult_fn=name_fn,
        cpf_consult_fn=cpf_fn,
        email_consult_fn=email_fn,
        target_titles=None,
        run_id="run-stages-happy",
    )

    stage_names = [event.stage for event in result.stages]
    assert stage_names[0] == "run_started"
    # title_gate was removed from the pipeline — operator selection IS
    # the decision to spend quota, no second-guess needed.
    assert "title_gate_passed" not in stage_names
    assert "signals_gate_passed" in stage_names
    assert "gonzales_name_sent" in stage_names
    assert "gonzales_name_parsed" in stage_names
    assert "gonzales_cpf_sent" in stage_names
    assert "gonzales_cpf_parsed" in stage_names
    assert stage_names[-1] == "completed"
    assert result.last_stage == "completed"


def test_unified_workflow_emits_signals_routed_to_findex_stage(
    tmp_path: Path,
) -> None:
    lead = Lead(
        company_name="Empresa",
        company_domain="empresa.com",
        person_name="Ana Silva",
        title="Marketing Manager",
        linkedin_url="https://linkedin.com/in/ana-silva",
        email="ana@empresa.com",
        source_url="https://linkedin.com/in/ana-silva",
        source_type="linkedin_people_search",
        snippet="",
        confidence_score=80,
    )
    store = _store(tmp_path)
    table_id = _table(store, [lead])

    def email_fn(email: str) -> TelegramConsultResult:
        return TelegramConsultResult(
            provider="findex",
            lead_name=lead.person_name or "",
            query=f"/email {email}",
            raw_text=f"Telefone: (11) 92222-0000",
            source_url=None,
            downloaded_at=None,
            error=None,
        )

    result = run_extract_phone_via_cpf(
        lead=lead,
        table_id=table_id,
        store=store,
        name_consult_fn=lambda n: (_ for _ in ()).throw(AssertionError("name must skip")),
        cpf_consult_fn=lambda v: (_ for _ in ()).throw(AssertionError("cpf must skip")),
        email_consult_fn=email_fn,
        target_titles=None,
        run_id="run-stage-signals-routed",
    )

    stage_names = [event.stage for event in result.stages]
    assert "signals_gate_routed_to_findex" in stage_names
    assert "findex_email_sent" in stage_names
    assert "findex_email_parsed" in stage_names
    assert stage_names[-1] == "completed"


def test_unified_workflow_falls_back_to_findex_email_alternatives(
    tmp_path: Path,
) -> None:
    # Primary and linkedin_contact are both empty but a previous provider left
    # a guess in email_alternatives — Findex should still get a shot at it
    # and stamp ``email_source=alternatives`` on the resulting phone.
    lead = _lead(email=None)
    lead.email_alternatives = [
        {
            "email": "ana.alt@gmail.com",
            "source": "apollo",
            "confidence": 70,
            "found_at": "2026-05-01T00:00:00Z",
        }
    ]
    store = _store(tmp_path)
    table_id = _table(store, [lead])

    email_calls: list[str] = []

    def name_fn(lead_name: str) -> TelegramConsultResult:
        return _name_result(lead_name, "Nenhum CPF encontrado para este nome.")

    def cpf_fn(value: str) -> TelegramConsultResult:
        raise AssertionError("cpf stage must not run without a CPF")

    def email_fn(email: str) -> TelegramConsultResult:
        email_calls.append(email)
        return TelegramConsultResult(
            provider="findex",
            lead_name=lead.person_name or "",
            query=f"/email {email}",
            raw_text=f"E-mail: {email}\nTelefone: (11) 95555-0000",
            source_url="https://exemplo.com/findex/email",
            downloaded_at=None,
            error=None,
        )

    result = run_extract_phone_via_cpf(
        lead=lead,
        table_id=table_id,
        store=store,
        name_consult_fn=name_fn,
        cpf_consult_fn=cpf_fn,
        email_consult_fn=email_fn,
        target_titles=None,
        run_id="run-alt-email",
    )

    assert email_calls == ["ana.alt@gmail.com"]
    assert result.phone_candidates[0].phone_digits == "11955550000"
    assert result.phone_candidates[0].provenance.get("email_source") == "alternatives"


def test_unified_workflow_ignores_linkedin_skip_title_and_starts_telegram(
    tmp_path: Path,
) -> None:
    lead = _lead()
    lead.title = "Head of Marketing"
    lead.linkedin_experience_title = "Pular para conteúdo principal"
    store = _store(tmp_path)
    table_id = _table(store, [lead])

    name_calls: list[str] = []
    cpf_calls: list[str] = []

    def name_fn(lead_name: str) -> TelegramConsultResult:
        name_calls.append(lead_name)
        return _name_result(
            lead_name,
            "Nome: Ana Silva\nCPF: 111.222.333-44\n"
            "Nascimento: 10/01/1985\nEndereço: Rua X, São Paulo/SP",
        )

    def cpf_fn(cpf: str) -> TelegramConsultResult:
        cpf_calls.append(cpf)
        return _cpf_result(cpf, "(11) 99999-0000")

    result = run_extract_phone_via_cpf(
        lead=lead,
        table_id=table_id,
        store=store,
        name_consult_fn=name_fn,
        cpf_consult_fn=cpf_fn,
        target_titles=["marketing"],
        run_id="run-ignore-skip-title",
    )

    assert name_calls == ["Ana Silva"]
    assert cpf_calls == ["111.222.333-44"]
    assert result.blocked_reason is None
    assert result.phone_candidates


def test_unified_workflow_ignores_target_titles_and_runs_telegram(
    tmp_path: Path,
) -> None:
    # Title gate was removed: the operator's checkbox selection IS the
    # decision to spend Telegram quota on the lead. Even when the
    # LinkedIn cargo doesn't match the table's ``target_titles``, the
    # flow proceeds — no more silent "linkedin_cargo_divergente" blocks
    # that confused operators into thinking the automation never tried.
    lead = _lead(linkedin_experience_title="Engenheiro de Software")
    store = _store(tmp_path)
    table_id = _table(store, [lead])

    name_calls: list[str] = []
    cpf_calls: list[str] = []

    def name_fn(value: str) -> TelegramConsultResult:
        name_calls.append(value)
        return _name_result(
            value,
            "Nome: Ana Silva\nCPF: 111.222.333-44\n"
            "Nascimento: 10/01/1985\nEndereço: Rua X, São Paulo/SP",
        )

    def cpf_fn(cpf: str) -> TelegramConsultResult:
        cpf_calls.append(cpf)
        return _cpf_result(cpf, "(11) 99999-0000")

    result = run_extract_phone_via_cpf(
        lead=lead,
        table_id=table_id,
        store=store,
        name_consult_fn=name_fn,
        cpf_consult_fn=cpf_fn,
        target_titles=["marketing"],
        run_id="run-no-gate",
    )

    assert name_calls == ["Ana Silva"]
    assert cpf_calls == ["111.222.333-44"]
    assert result.blocked_reason is None
    assert len(result.phone_candidates) == 1
    stage_names = [event.stage for event in result.stages]
    assert "title_gate_blocked" not in stage_names
    assert "title_gate_routed_to_findex" not in stage_names


def test_unified_workflow_skips_cpf_when_no_eligible(tmp_path: Path) -> None:
    """Name stage finds a CPF but its match score sits below the
    follow-up threshold. The cpf stage must not run; a marker row is
    persisted explaining why.
    """
    lead = Lead(
        company_name="Empresa",
        company_domain="empresa.com",
        person_name="Ana Silva",
        title="Marketing Manager",
        linkedin_url="https://linkedin.com/in/ana-silva",
        source_url="https://linkedin.com/in/ana-silva",
        source_type="linkedin_people_search",
        snippet="",
        confidence_score=80,
        # Lead com âncora de localização (passa o signals gate), mas o
        # candidato devolvido pelo bot está em outra cidade e o nome
        # bate o suficiente pra evitar reject — só score baixo.
        linkedin_location="São Paulo, Brazil",
    )
    store = _store(tmp_path)
    table_id = _table(store, [lead])

    cpf_calls: list[str] = []

    def name_fn(lead_name: str) -> TelegramConsultResult:
        # Mesmo nome do lead (passa o gate) mas localização divergente
        # e sem data de nascimento → score abaixo do min_score=65.
        return _name_result(
            lead_name,
            "Nome: Ana Silva\nCPF: 999.888.777-66\n"
            "Endereço: Rua Y, Manaus/AM",
        )

    def cpf_fn(value: str) -> TelegramConsultResult:
        cpf_calls.append(value)
        raise AssertionError("cpf stage must not run below threshold")

    result = run_extract_phone_via_cpf(
        lead=lead,
        table_id=table_id,
        store=store,
        name_consult_fn=name_fn,
        cpf_consult_fn=cpf_fn,
        target_titles=None,
        run_id="run-no-eligible",
    )

    assert cpf_calls == []
    assert result.blocked_reason == "no_eligible_cpf"
    assert result.phone_candidates == []


def test_unified_workflow_aggregates_multiple_cpfs_in_one_row(
    tmp_path: Path,
) -> None:
    """Two strong CPFs from the same name; the cpf stage runs twice
    but persists a single aggregate row carrying both attempts."""
    lead = _lead()
    store = _store(tmp_path)
    table_id = _table(store, [lead])

    cpf_phones = {
        "111.222.333-44": "(11) 99111-1111",
        "555.666.777-88": "(21) 98222-2222",
    }

    def name_fn(lead_name: str) -> TelegramConsultResult:
        return _name_result(
            lead_name,
            "Nome: Ana Silva\nCPF: 111.222.333-44\n"
            "Endereço: Rua A, São Paulo/SP\n"
            "Nascimento: 10/01/1985\n\n"
            "Nome: Ana Silva\nCPF: 555.666.777-88\n"
            "Endereço: Rua B, São Paulo/SP\n"
            "Nascimento: 12/02/1987",
        )

    def cpf_fn(cpf: str) -> TelegramConsultResult:
        return _cpf_result(cpf, cpf_phones[cpf])

    result = run_extract_phone_via_cpf(
        lead=lead,
        table_id=table_id,
        store=store,
        name_consult_fn=name_fn,
        cpf_consult_fn=cpf_fn,
        target_titles=None,
        run_id="run-multi-cpf",
    )

    assert len(result.phone_candidates) == 2
    cpf_rows = [
        row for row in store.list_telegram_consults(table_id)
        if row.query_type == "cpf"
    ]
    # Aggregate row — one storage entry carrying both CPF attempts.
    assert len(cpf_rows) == 1
    cpfs_in_row = {c.get("cpf") for c in cpf_rows[0].extracted_candidates}
    assert cpfs_in_row == {"111.222.333-44", "555.666.777-88"}


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "beautiful_linkedin.server.app.probe_cdp_endpoint",
        lambda *args, **kwargs: False,
    )
    return build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))


class _FakeNameDriver:
    provider = "gon"

    def __init__(self, raw_by_name: dict[str, str]) -> None:
        self._raw = raw_by_name
        self.calls: list[str] = []

    def consult(self, lead_name: str) -> TelegramConsultResult:
        self.calls.append(lead_name)
        raw = self._raw.get(lead_name)
        return TelegramConsultResult(
            provider=self.provider,
            lead_name=lead_name,
            query=f"/nome {lead_name}",
            raw_text=raw,
            source_url=None,
            downloaded_at=None,
            error=None if raw else "no_fake",
        )


class _FakeCpfDriver:
    provider = "gon_cpf"

    def __init__(self, phone_by_cpf: dict[str, str]) -> None:
        self._phone = phone_by_cpf
        self.calls: list[str] = []

    def consult(self, cpf: str) -> TelegramConsultResult:
        self.calls.append(cpf)
        phone = self._phone.get(cpf)
        if phone is None:
            return TelegramConsultResult(
                provider=self.provider,
                lead_name="",
                query=f"/cpf {cpf}",
                raw_text=None,
                source_url=None,
                downloaded_at=None,
                error="no_fake",
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


def _create_table_with_leads(client: TestClient, leads: list[Lead]) -> str:
    return client.post(
        "/lead-tables",
        json={
            "name": "Unified endpoint",
            "leads": [lead.model_dump(mode="json") for lead in leads],
        },
    ).json()["table"]["id"]


def test_endpoint_unified_flow_runs_both_stages_and_persists_phone(
    monkeypatch, app
) -> None:
    client = TestClient(app)
    leads = [_lead("Ana Silva")]
    table_id = _create_table_with_leads(client, leads)

    name_driver = _FakeNameDriver(
        {
            "Ana Silva": (
                "Nome: Ana Silva\nCPF: 111.222.333-44\n"
                "Endereço: Rua X, São Paulo/SP\n"
                "Nascimento: 10/01/1985"
            )
        }
    )
    cpf_driver = _FakeCpfDriver({"111.222.333-44": "(11) 99999-0000"})

    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_gonzales_consult",
        lambda settings=None: name_driver,
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_gonzales_cpf_consult",
        lambda settings=None: cpf_driver,
    )

    response = client.post(
        f"/lead-tables/{table_id}/telegram-phone",
        json={"lead_refs": [leads[0].linkedin_url]},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert name_driver.calls == ["Ana Silva"]
    assert cpf_driver.calls == ["111.222.333-44"]
    assert body["summary"]["leads_with_phone"] == 1
    lead_payload = body["leads"][0]
    assert lead_payload["blocked_reason"] is None
    assert lead_payload["candidates"][0]["confidence"] >= 60
    assert lead_payload["name_consult"]["query_type"] == "name"
    assert lead_payload["cpf_consult"]["query_type"] == "cpf"

    store = get_saved_leads_store(app)
    persisted = store.list_leads(table_id)[0]
    assert persisted.phone is not None and persisted.phone.startswith("+55")


def test_endpoint_unified_flow_persists_all_contact_phones_and_personal_email(
    monkeypatch, app
) -> None:
    client = TestClient(app)
    leads = [_lead("Ana Silva", email="ana.silva@empresa.com")]
    table_id = _create_table_with_leads(client, leads)

    name_driver = _FakeNameDriver(
        {
            "Ana Silva": (
                "Nome: Ana Silva\nCPF: 111.222.333-44\n"
                "Endereço: Rua X, São Paulo/SP\n"
                "Nascimento: 10/01/1985"
            )
        }
    )
    cpf_driver = _FakeCpfDriver(
        {
            "111.222.333-44": (
                "\nContatos\n"
                "TELEFONE 1\n"
                "[OUTRO] ((21)) 2105-0000\n"
                "TELEFONE 2\n"
                "[RESIDENCIAL] ((61)) 98142-2886\n"
                "E-MAIL 1\n"
                "danisg.dani@gmail.com"
            )
        }
    )

    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_gonzales_consult",
        lambda settings=None: name_driver,
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_gonzales_cpf_consult",
        lambda settings=None: cpf_driver,
    )

    response = client.post(
        f"/lead-tables/{table_id}/telegram-phone",
        json={"lead_refs": [leads[0].linkedin_url]},
    )
    assert response.status_code == 200, response.text

    store = get_saved_leads_store(app)
    persisted = store.list_leads(table_id)[0]
    persisted_phone_digits = {
        "".join(ch for ch in (persisted.phone or "") if ch.isdigit()),
        *[
            "".join(ch for ch in str(alt.get("phone") or "") if ch.isdigit())
            for alt in persisted.phone_alternatives
        ],
    }
    assert {"552121050000", "5561981422886"}.issubset(persisted_phone_digits)
    assert persisted.email == "ana.silva@empresa.com"
    assert any(
        alt.get("email") == "danisg.dani@gmail.com"
        and "telegram_consult_cpf" in (alt.get("source") or "")
        for alt in persisted.email_alternatives
    )


def test_endpoint_unified_flow_does_not_overwrite_existing_phone(
    monkeypatch, app
) -> None:
    existing = "+5511988887777"
    leads = [_lead("Ana Silva", phone=existing)]
    client = TestClient(app)
    table_id = _create_table_with_leads(client, leads)

    name_driver = _FakeNameDriver(
        {
            "Ana Silva": (
                "Nome: Ana Silva\nCPF: 111.222.333-44\n"
                "Endereço: Rua X, São Paulo/SP\n"
                "Nascimento: 10/01/1985"
            )
        }
    )
    cpf_driver = _FakeCpfDriver({"111.222.333-44": "(21) 90000-0000"})

    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_gonzales_consult",
        lambda settings=None: name_driver,
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_gonzales_cpf_consult",
        lambda settings=None: cpf_driver,
    )

    response = client.post(
        f"/lead-tables/{table_id}/telegram-phone",
        json={"lead_refs": [leads[0].linkedin_url]},
    )
    assert response.status_code == 200, response.text

    store = get_saved_leads_store(app)
    persisted = store.list_leads(table_id)[0]
    assert persisted.phone == existing
    sources = [alt.get("source") for alt in persisted.phone_alternatives]
    assert any("telegram_consult_cpf" in (s or "") for s in sources)


def test_endpoint_unified_flow_404_on_missing_table(monkeypatch, app) -> None:
    client = TestClient(app)
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_gonzales_consult",
        lambda settings=None: _FakeNameDriver({}),
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_gonzales_cpf_consult",
        lambda settings=None: _FakeCpfDriver({}),
    )
    response = client.post(
        "/lead-tables/does-not-exist/telegram-phone",
        json={"lead_refs": ["ref"]},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Resumable run endpoints (/start, /next, /cancel) — anti-ban pause UX
# ---------------------------------------------------------------------------


def _patch_drivers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    name_driver: _FakeNameDriver,
    cpf_driver: _FakeCpfDriver,
) -> None:
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_gonzales_consult",
        lambda settings=None: name_driver,
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_gonzales_cpf_consult",
        lambda settings=None: cpf_driver,
    )


def test_resumable_start_creates_run_without_running_anything(
    monkeypatch, app
) -> None:
    """``/start`` deve criar um run com cursor zerado e total_leads
    correto, mas NÃO disparar nenhuma consulta — a quota dos bots só é
    consumida quando o cliente confirma cada `/next`."""
    leads = [_lead("Ana Silva"), _lead("Bruno Costa")]
    client = TestClient(app)
    table_id = _create_table_with_leads(client, leads)

    name_driver = _FakeNameDriver({})
    cpf_driver = _FakeCpfDriver({})
    _patch_drivers(monkeypatch, name_driver=name_driver, cpf_driver=cpf_driver)

    response = client.post(
        f"/lead-tables/{table_id}/telegram-phone/start",
        json={"lead_refs": [leads[0].linkedin_url, leads[1].linkedin_url]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_leads"] == 2
    assert body["next_index"] == 0
    assert body["status"] == "pending"
    assert name_driver.calls == []
    assert cpf_driver.calls == []


def test_resumable_next_processes_one_lead_at_a_time(monkeypatch, app) -> None:
    """Cada chamada a ``/next`` processa exatamente UM lead, avança o
    cursor em 1 e devolve o resultado para o cliente decidir continuar."""
    leads = [_lead("Ana Silva"), _lead("Bruno Costa")]
    client = TestClient(app)
    table_id = _create_table_with_leads(client, leads)

    name_driver = _FakeNameDriver(
        {
            "Ana Silva": (
                "Nome: Ana Silva\nCPF: 111.222.333-44\n"
                "Endereço: Rua X, São Paulo/SP\nNascimento: 10/01/1985"
            ),
            "Bruno Costa": (
                "Nome: Bruno Costa\nCPF: 555.666.777-88\n"
                "Endereço: Rua Y, São Paulo/SP\nNascimento: 12/02/1985"
            ),
        }
    )
    cpf_driver = _FakeCpfDriver(
        {"111.222.333-44": "(11) 99111-1111", "555.666.777-88": "(11) 99222-2222"}
    )
    _patch_drivers(monkeypatch, name_driver=name_driver, cpf_driver=cpf_driver)

    start = client.post(
        f"/lead-tables/{table_id}/telegram-phone/start",
        json={"lead_refs": [leads[0].linkedin_url, leads[1].linkedin_url]},
    ).json()
    run_id = start["run_id"]

    # 1º /next: processa Ana Silva, avança para index 1.
    first = client.post(
        f"/lead-tables/{table_id}/telegram-phone/next",
        json={"run_id": run_id},
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["status"] == "in_progress"
    assert first_body["next_index"] == 1
    assert name_driver.calls == ["Ana Silva"]
    assert cpf_driver.calls == ["111.222.333-44"]
    assert first_body["last_lead"]["lead_name"] == "Ana Silva"

    # 2º /next: processa Bruno e marca completed.
    second = client.post(
        f"/lead-tables/{table_id}/telegram-phone/next",
        json={"run_id": run_id},
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["status"] == "completed"
    assert second_body["next_index"] == 2
    assert name_driver.calls == ["Ana Silva", "Bruno Costa"]

    # 3º /next idempotente: já completed, devolve estado final sem rodar.
    third = client.post(
        f"/lead-tables/{table_id}/telegram-phone/next",
        json={"run_id": run_id},
    ).json()
    assert third["status"] == "completed"
    assert name_driver.calls == ["Ana Silva", "Bruno Costa"]


def test_resumable_cancel_stops_subsequent_next(monkeypatch, app) -> None:
    """``/cancel`` marca o run como cancelado; ``/next`` retorna 409 para
    o cliente saber que precisa criar um run novo (não silenciar para
    evitar disparos acidentais após o usuário pedir para parar)."""
    leads = [_lead("Ana Silva"), _lead("Bruno Costa")]
    client = TestClient(app)
    table_id = _create_table_with_leads(client, leads)

    name_driver = _FakeNameDriver(
        {
            "Ana Silva": (
                "Nome: Ana Silva\nCPF: 111.222.333-44\n"
                "Endereço: Rua X, São Paulo/SP\nNascimento: 10/01/1985"
            ),
        }
    )
    cpf_driver = _FakeCpfDriver({"111.222.333-44": "(11) 99111-1111"})
    _patch_drivers(monkeypatch, name_driver=name_driver, cpf_driver=cpf_driver)

    start = client.post(
        f"/lead-tables/{table_id}/telegram-phone/start",
        json={"lead_refs": [leads[0].linkedin_url, leads[1].linkedin_url]},
    ).json()
    run_id = start["run_id"]

    client.post(
        f"/lead-tables/{table_id}/telegram-phone/next",
        json={"run_id": run_id},
    )
    assert name_driver.calls == ["Ana Silva"]

    cancel = client.post(
        f"/lead-tables/{table_id}/telegram-phone/cancel",
        json={"run_id": run_id},
    )
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "cancelled"

    next_after_cancel = client.post(
        f"/lead-tables/{table_id}/telegram-phone/next",
        json={"run_id": run_id},
    )
    assert next_after_cancel.status_code == 409
    # Nenhuma consulta nova depois do cancel.
    assert name_driver.calls == ["Ana Silva"]


def test_resumable_next_404_when_run_missing(monkeypatch, app) -> None:
    leads = [_lead("Ana Silva")]
    client = TestClient(app)
    table_id = _create_table_with_leads(client, leads)
    _patch_drivers(
        monkeypatch,
        name_driver=_FakeNameDriver({}),
        cpf_driver=_FakeCpfDriver({}),
    )
    response = client.post(
        f"/lead-tables/{table_id}/telegram-phone/next",
        json={"run_id": "ghost"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Two-step interactive flow (/extract-cpfs + /run-cpf-stage + /skip-current-lead)
# ---------------------------------------------------------------------------


def test_two_step_extract_cpfs_returns_candidates_without_running_cpf(
    monkeypatch, app
) -> None:
    """``/extract-cpfs`` roda /nome + matcher, devolve candidatos, e
    PARA. Nenhum /cpf é disparado até o cliente confirmar via
    ``/run-cpf-stage``."""
    leads = [_lead("Ana Silva")]
    client = TestClient(app)
    table_id = _create_table_with_leads(client, leads)

    name_driver = _FakeNameDriver(
        {
            "Ana Silva": (
                "Nome: Ana Silva\nCPF: 111.222.333-44\n"
                "Endereço: Rua X, São Paulo/SP\nNascimento: 10/01/1985"
            )
        }
    )
    cpf_driver = _FakeCpfDriver({})  # vazio — não deve ser chamado
    _patch_drivers(monkeypatch, name_driver=name_driver, cpf_driver=cpf_driver)

    start = client.post(
        f"/lead-tables/{table_id}/telegram-phone/start",
        json={"lead_refs": [leads[0].linkedin_url]},
    ).json()
    run_id = start["run_id"]

    extract = client.post(
        f"/lead-tables/{table_id}/telegram-phone/extract-cpfs",
        json={"run_id": run_id},
    )
    assert extract.status_code == 200, extract.text
    body = extract.json()
    assert body["status"] == "awaiting_cpf_confirmation"
    assert body["next_index"] == 0
    assert body["lead_name"] == "Ana Silva"
    assert body["eligible_cpfs"] == ["111.222.333-44"]
    assert len(body["candidates"]) >= 1
    assert body["candidates"][0]["eligible"] is True
    assert cpf_driver.calls == []


def test_two_step_extract_cpfs_omits_candidates_over_75_from_review(
    monkeypatch, app
) -> None:
    leads = [_lead("Ana Silva")]
    client = TestClient(app)
    table_id = _create_table_with_leads(client, leads)

    name_driver = _FakeNameDriver(
        {
            "Ana Silva": (
                "Nome: Ana Silva\nCPF: 111.222.333-44\n"
                "Endereço: Rua X, São Paulo/SP\nNascimento: 10/01/1985\n\n"
                "Nome: Ana Silva\nCPF: 999.888.777-66\n"
                "Endereço: Rua Y, São Paulo/SP\nNascimento: 10/01/1940"
            )
        }
    )
    cpf_driver = _FakeCpfDriver({})
    _patch_drivers(monkeypatch, name_driver=name_driver, cpf_driver=cpf_driver)

    start = client.post(
        f"/lead-tables/{table_id}/telegram-phone/start",
        json={"lead_refs": [leads[0].linkedin_url]},
    ).json()

    extract = client.post(
        f"/lead-tables/{table_id}/telegram-phone/extract-cpfs",
        json={"run_id": start["run_id"]},
    )

    assert extract.status_code == 200, extract.text
    body = extract.json()
    assert body["status"] == "awaiting_cpf_confirmation"
    assert body["eligible_cpfs"] == ["111.222.333-44"]
    assert [c["cpf"] for c in body["candidates"]] == ["111.222.333-44"]
    assert cpf_driver.calls == []


def test_two_step_extract_cpfs_reuses_matching_persisted_cpfs_and_prunes_mismatches(
    monkeypatch, app
) -> None:
    lead = Lead(
        company_name="Empresa",
        company_domain="empresa.com",
        person_name="Julia Santos",
        title="Marketing",
        linkedin_url="https://linkedin.com/in/julia-santos",
        source_url="https://linkedin.com/in/julia-santos",
        source_type="linkedin_people_search",
        snippet="",
        confidence_score=80,
        # Sem sinais LinkedIn: se não reutilizar o CPF persistido antes
        # do gate, o endpoint bloquearia com missing_linkedin_signals.
    )
    client = TestClient(app)
    table_id = _create_table_with_leads(client, [lead])
    store = get_saved_leads_store(app)
    store.save_telegram_consult(
        table_id=table_id,
        lead_ref=lead.linkedin_url,
        provider="finder",
        lead_name="Julia Santos",
        query="/nome Julia Santos",
        raw_text="resultado bruto",
        source_url=None,
        downloaded_at=None,
        error=None,
        extracted_candidates=[
            {
                "cpf": "111.222.333-44",
                "nome": "Julia Santos",
                "match_score": 20,
            },
            {
                "cpf": "999.888.777-66",
                "nome": "Gustavo Lima",
                "match_score": 99,
            },
        ],
        match_score=99,
        query_type="name",
    )

    name_driver = _FakeNameDriver({})
    cpf_driver = _FakeCpfDriver({})
    _patch_drivers(monkeypatch, name_driver=name_driver, cpf_driver=cpf_driver)

    start = client.post(
        f"/lead-tables/{table_id}/telegram-phone/start",
        json={"lead_refs": [lead.linkedin_url]},
    ).json()
    extract = client.post(
        f"/lead-tables/{table_id}/telegram-phone/extract-cpfs",
        json={"run_id": start["run_id"]},
    )

    assert extract.status_code == 200, extract.text
    body = extract.json()
    assert body["status"] == "awaiting_cpf_confirmation"
    assert body["eligible_cpfs"] == ["111.222.333-44"]
    assert [c["cpf"] for c in body["candidates"]] == ["111.222.333-44"]
    assert name_driver.calls == []

    rows = store.list_telegram_consults_for_lead(
        table_id, lead.linkedin_url, query_type="name"
    )
    assert rows[0].extracted_candidates == [
        {
            "cpf": "111.222.333-44",
            "nome": "Julia Santos",
            "match_score": 20,
        }
    ]


def test_two_step_flow_logs_start_extract_and_cpf_stage(
    monkeypatch, app, caplog: pytest.LogCaptureFixture
) -> None:
    leads = [_lead("Ana Silva")]
    client = TestClient(app)
    table_id = _create_table_with_leads(client, leads)

    name_driver = _FakeNameDriver(
        {
            "Ana Silva": (
                "Nome: Ana Silva\nCPF: 111.222.333-44\n"
                "Endereço: Rua X, São Paulo/SP\nNascimento: 10/01/1985"
            )
        }
    )
    cpf_driver = _FakeCpfDriver({"111.222.333-44": "(11) 99111-1111"})
    _patch_drivers(monkeypatch, name_driver=name_driver, cpf_driver=cpf_driver)

    with caplog.at_level(logging.INFO, logger="beautiful_linkedin.server.app"):
        start = client.post(
            f"/lead-tables/{table_id}/telegram-phone/start",
            json={"lead_refs": [leads[0].linkedin_url]},
        ).json()
        run_id = start["run_id"]
        client.post(
            f"/lead-tables/{table_id}/telegram-phone/extract-cpfs",
            json={"run_id": run_id},
        )
        client.post(
            f"/lead-tables/{table_id}/telegram-phone/run-cpf-stage",
            json={"run_id": run_id, "cpfs": ["111.222.333-44"]},
        )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "telegram_phone_endpoint event=start_created" in messages
    assert "telegram_phone_endpoint event=extract_cpfs_started" in messages
    assert "telegram_phone_endpoint event=extract_cpfs_awaiting_confirmation" in messages
    assert "telegram_phone_endpoint event=run_cpf_stage_started" in messages
    assert "telegram_phone_endpoint event=run_cpf_stage_completed" in messages
    assert f"run_id={run_id}" in messages
    assert f"table_id={table_id}" in messages


def test_two_step_run_cpf_stage_runs_only_user_selected(monkeypatch, app) -> None:
    """``/run-cpf-stage`` dispara /cpf SÓ pros CPFs que o cliente
    selecionou. Outros CPFs candidatos NÃO são consultados."""
    leads = [_lead("Ana Silva")]
    client = TestClient(app)
    table_id = _create_table_with_leads(client, leads)

    name_driver = _FakeNameDriver(
        {
            "Ana Silva": (
                "Nome: Ana Silva\nCPF: 111.222.333-44\n"
                "Endereço: Rua X, São Paulo/SP\nNascimento: 10/01/1985\n\n"
                "Nome: Ana Silva\nCPF: 555.666.777-88\n"
                "Endereço: Rua Y, São Paulo/SP\nNascimento: 12/02/1987"
            )
        }
    )
    cpf_driver = _FakeCpfDriver(
        {
            "111.222.333-44": "(11) 99111-1111",
            "555.666.777-88": "(11) 99222-2222",
        }
    )
    _patch_drivers(monkeypatch, name_driver=name_driver, cpf_driver=cpf_driver)

    start = client.post(
        f"/lead-tables/{table_id}/telegram-phone/start",
        json={"lead_refs": [leads[0].linkedin_url]},
    ).json()
    run_id = start["run_id"]

    extract = client.post(
        f"/lead-tables/{table_id}/telegram-phone/extract-cpfs",
        json={"run_id": run_id},
    ).json()
    assert set(extract["eligible_cpfs"]) == {"111.222.333-44", "555.666.777-88"}

    # Operador escolheu APENAS o primeiro CPF.
    run_cpf = client.post(
        f"/lead-tables/{table_id}/telegram-phone/run-cpf-stage",
        json={"run_id": run_id, "cpfs": ["111.222.333-44"]},
    )
    assert run_cpf.status_code == 200, run_cpf.text
    body = run_cpf.json()
    assert body["status"] == "completed"
    assert body["next_index"] == 1
    assert cpf_driver.calls == ["111.222.333-44"]


def test_two_step_run_cpf_stage_rejects_unknown_cpf(monkeypatch, app) -> None:
    """CPF que NÃO veio do /nome desse lead é rejeitado com 422 — defesa
    contra payload manipulado pelo cliente."""
    leads = [_lead("Ana Silva")]
    client = TestClient(app)
    table_id = _create_table_with_leads(client, leads)

    name_driver = _FakeNameDriver(
        {
            "Ana Silva": (
                "Nome: Ana Silva\nCPF: 111.222.333-44\n"
                "Endereço: Rua X, São Paulo/SP\nNascimento: 10/01/1985"
            )
        }
    )
    cpf_driver = _FakeCpfDriver({})
    _patch_drivers(monkeypatch, name_driver=name_driver, cpf_driver=cpf_driver)

    start = client.post(
        f"/lead-tables/{table_id}/telegram-phone/start",
        json={"lead_refs": [leads[0].linkedin_url]},
    ).json()
    client.post(
        f"/lead-tables/{table_id}/telegram-phone/extract-cpfs",
        json={"run_id": start["run_id"]},
    )

    response = client.post(
        f"/lead-tables/{table_id}/telegram-phone/run-cpf-stage",
        json={"run_id": start["run_id"], "cpfs": ["999.999.999-99"]},
    )
    assert response.status_code == 422
    assert cpf_driver.calls == []


def test_two_step_skip_current_lead_advances_without_cpf(monkeypatch, app) -> None:
    """``/skip-current-lead`` avança o cursor sem disparar /cpf — usado
    quando o operador olha os CPFs e decide que nenhum vale."""
    leads = [_lead("Ana Silva"), _lead("Bruno Costa")]
    client = TestClient(app)
    table_id = _create_table_with_leads(client, leads)

    name_driver = _FakeNameDriver(
        {
            "Ana Silva": (
                "Nome: Ana Silva\nCPF: 111.222.333-44\n"
                "Endereço: Rua X, São Paulo/SP\nNascimento: 10/01/1985"
            ),
            "Bruno Costa": (
                "Nome: Bruno Costa\nCPF: 555.666.777-88\n"
                "Endereço: Rua Y, São Paulo/SP\nNascimento: 12/02/1985"
            ),
        }
    )
    cpf_driver = _FakeCpfDriver({})
    _patch_drivers(monkeypatch, name_driver=name_driver, cpf_driver=cpf_driver)

    start = client.post(
        f"/lead-tables/{table_id}/telegram-phone/start",
        json={"lead_refs": [leads[0].linkedin_url, leads[1].linkedin_url]},
    ).json()
    run_id = start["run_id"]

    client.post(
        f"/lead-tables/{table_id}/telegram-phone/extract-cpfs",
        json={"run_id": run_id},
    )
    skip = client.post(
        f"/lead-tables/{table_id}/telegram-phone/skip-current-lead",
        json={"run_id": run_id},
    )
    assert skip.status_code == 200, skip.text
    body = skip.json()
    assert body["status"] == "in_progress"
    assert body["next_index"] == 1
    assert body["last_lead"]["blocked_reason"] == "skipped_by_operator"
    assert cpf_driver.calls == []


def test_two_step_run_cpf_stage_requires_extract_first(monkeypatch, app) -> None:
    """``/run-cpf-stage`` só funciona depois de ``/extract-cpfs`` ter
    devolvido candidatos. Chamar direto retorna 409."""
    leads = [_lead("Ana Silva")]
    client = TestClient(app)
    table_id = _create_table_with_leads(client, leads)
    _patch_drivers(
        monkeypatch,
        name_driver=_FakeNameDriver({}),
        cpf_driver=_FakeCpfDriver({}),
    )

    start = client.post(
        f"/lead-tables/{table_id}/telegram-phone/start",
        json={"lead_refs": [leads[0].linkedin_url]},
    ).json()
    response = client.post(
        f"/lead-tables/{table_id}/telegram-phone/run-cpf-stage",
        json={"run_id": start["run_id"], "cpfs": ["111.222.333-44"]},
    )
    assert response.status_code == 409


def test_two_step_extract_cpfs_auto_advances_when_blocked(monkeypatch, app) -> None:
    """Gate de sinais LinkedIn deve bloquear o lead direto no /extract-cpfs,
    sem disparar /nome nem ficar em awaiting_cpf_confirmation."""
    lead = Lead(
        company_name="Empresa",
        company_domain="empresa.com",
        person_name="Sem Sinais",
        title="Marketing",
        linkedin_url="https://linkedin.com/in/sem-sinais",
        source_url="https://linkedin.com/in/sem-sinais",
        source_type="linkedin_people_search",
        snippet="",
        confidence_score=80,
    )
    client = TestClient(app)
    table_id = _create_table_with_leads(client, [lead])

    name_driver = _FakeNameDriver({})
    cpf_driver = _FakeCpfDriver({})
    _patch_drivers(monkeypatch, name_driver=name_driver, cpf_driver=cpf_driver)

    start = client.post(
        f"/lead-tables/{table_id}/telegram-phone/start",
        json={"lead_refs": [lead.linkedin_url]},
    ).json()
    extract = client.post(
        f"/lead-tables/{table_id}/telegram-phone/extract-cpfs",
        json={"run_id": start["run_id"]},
    ).json()
    assert extract["status"] == "completed"
    assert extract["blocked_reason"] == "missing_linkedin_signals"
    assert name_driver.calls == []
    assert cpf_driver.calls == []
