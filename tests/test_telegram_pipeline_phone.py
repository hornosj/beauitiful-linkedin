"""Tests for the Telegram phone follow-up (Phase 7).

These pin down the most safety-critical contract in the pipeline:

- A phone harvested from a ``/cpf`` follow-up inherits its CPF's match
  score 1:1 — the workflow refuses to invent a new confidence number.
- ``lead.phone`` is never overwritten when applying the follow-up's
  result; an existing phone wins.
- A diverging phone gets recorded in ``phone_alternatives`` so the
  cross-provider verification trail keeps both values visible.

Everything is offline: the consult function is a synchronous fake.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from beautiful_linkedin.models import Lead
from beautiful_linkedin.storage.internal_phone_enrichment import (
    PhoneEnrichmentUpdate,
)
from beautiful_linkedin.storage.saved_leads import SavedLeadsStore
from beautiful_linkedin.storage.telegram_group_playwright_lookup import (
    FINDEX_BOT_URL,
    GONZALES_BOT_URL,
    FindexEmailConsult,
    GonzalesBotConsult,
    GonzalesCpfConsult,
    TelegramConsultResult,
)
from beautiful_linkedin.storage.telegram_pipeline import (
    TelegramPhoneCandidate,
    extract_address_from_text,
    extract_emails_from_text,
    extract_phones_from_text,
    run_phone_followup,
)


def _lead(
    *,
    phone: str | None = None,
    title: str = "Marketing Manager",
    linkedin_url: str = "https://linkedin.com/in/ana-silva",
) -> Lead:
    return Lead(
        company_name="Empresa",
        company_domain="empresa.com",
        person_name="Ana Silva",
        title=title,
        linkedin_url=linkedin_url,
        source_url=linkedin_url,
        source_type="linkedin_people_search",
        snippet="",
        confidence_score=80,
        linkedin_location="São Paulo, Brazil",
        phone=phone,
    )


def _seed_cpf_candidate(
    store: SavedLeadsStore,
    table_id: str,
    lead: Lead,
    *,
    cpf: str,
    score: int,
    run_id: str = "run-name",
) -> None:
    store.save_telegram_consult(
        table_id=table_id,
        lead_ref=lead.linkedin_url,
        provider="gon",
        lead_name=lead.person_name,
        query=f"/nome {lead.person_name}",
        raw_text="…",
        source_url=None,
        downloaded_at=None,
        error=None,
        extracted_nome=lead.person_name,
        extracted_cpf=cpf,
        extracted_candidates=[
            {
                "cpf": cpf,
                "nome": lead.person_name,
                "data_nascimento": "10/01/1985",
                "endereco": "Rua X, São Paulo/SP",
                "match_score": score,
                "signals_used": ["name", "location", "education_age"],
                "breakdown": {"confidence_label": "alta"},
            }
        ],
        match_score=score,
        run_id=run_id,
        query_type="name",
    )


def _store_with_lead(tmp_path: Path, lead: Lead) -> tuple[SavedLeadsStore, str]:
    store = SavedLeadsStore(str(tmp_path / "saved.sqlite"))
    table = store.create_table(name="Phone follow-up tests")
    store.add_leads(table.id, [lead])
    return store, table.id


# ---------------------------------------------------------------------------
# Confidence policy: score is inherited 1:1, never recomputed.
# ---------------------------------------------------------------------------


def test_gonzales_cpf_consult_uses_cpf_verb_and_distinct_provider_name() -> None:
    """Smoke test: the /cpf driver is wired to the right verb and
    provider tag without touching Playwright. We inject the fetcher
    seam so the test stays offline.
    """
    captured_queries: list[str] = []

    def fake_fetcher(value: str) -> TelegramConsultResult:
        # The driver builds the query before invoking the fetcher; this
        # records what was actually sent to Telegram.
        captured_queries.append(value)
        return TelegramConsultResult(
            provider="gon_cpf",
            lead_name=value,
            query=f"/cpf {value}",
            raw_text="raw",
            source_url=None,
            downloaded_at=None,
            error=None,
        )

    driver = GonzalesCpfConsult(fetcher=fake_fetcher)
    result = driver.consult("111.222.333-44")

    assert driver.provider == "gon_cpf"
    assert driver._build_query("111.222.333-44") == "/cpf 111.222.333-44"
    assert result.provider == "gon_cpf"
    assert captured_queries == ["111.222.333-44"]


def test_gonzales_name_and_cpf_default_to_private_bot_chat() -> None:
    def fake_fetcher(value: str) -> TelegramConsultResult:
        return TelegramConsultResult(
            provider="gon",
            lead_name=value,
            query=f"/nome {value}",
            raw_text="Nome: Ana",
            source_url=None,
            downloaded_at=None,
            error=None,
        )

    name_driver = GonzalesBotConsult(fetcher=fake_fetcher)
    cpf_driver = GonzalesCpfConsult(fetcher=fake_fetcher)

    assert name_driver._group_url == GONZALES_BOT_URL
    assert cpf_driver._group_url == GONZALES_BOT_URL


def test_findex_email_consult_uses_email_verb_and_private_bot_chat() -> None:
    def fake_fetcher(value: str) -> TelegramConsultResult:
        return TelegramConsultResult(
            provider="findex",
            lead_name=value,
            query=f"/email {value}",
            raw_text="Telefone: (11) 97777-0000",
            source_url=None,
            downloaded_at=None,
            error=None,
        )

    driver = FindexEmailConsult(fetcher=fake_fetcher)
    result = driver.consult("ana@empresa.com")

    assert driver.provider == "findex"
    assert driver._group_url == FINDEX_BOT_URL
    assert driver._build_query("ana@empresa.com") == "/email ana@empresa.com"
    assert result.query == "/email ana@empresa.com"


def test_phone_inherits_match_score_one_to_one(tmp_path: Path) -> None:
    lead = _lead()
    store, table_id = _store_with_lead(tmp_path, lead)
    _seed_cpf_candidate(store, table_id, lead, cpf="111.222.333-44", score=85)

    def consult_fn(cpf: str) -> TelegramConsultResult:
        return TelegramConsultResult(
            provider="gon_cpf",
            lead_name=lead.person_name,
            query=f"/cpf {cpf}",
            raw_text=f"CPF: {cpf}\nNome: Ana Silva\nTelefone: (11) 99999-0000",
            source_url="https://exemplo.com/r/cpf",
            downloaded_at=datetime.now(timezone.utc).isoformat(),
            error=None,
        )

    out = run_phone_followup(
        lead=lead,
        table_id=table_id,
        store=store,
        consult_fn=consult_fn,
        target_titles=None,
        run_id="run-phone-1",
    )

    assert len(out) == 1
    candidate = out[0]
    assert isinstance(candidate, TelegramPhoneCandidate)
    assert candidate.confidence == 85
    assert candidate.cpf == "111.222.333-44"
    assert candidate.phone_digits == "11999990000"
    assert candidate.provenance["score_source"] == "telegram_match_score"
    assert candidate.provenance["cpf_match_score"] == 85
    assert "cpf_breakdown" in candidate.provenance


def test_gonzales_sisreg_phone_extraction_ignores_cns_document() -> None:
    raw = """
    Dados pessoais
    CPF
    353.559.228-30
    CNS
    700003672681800
    NOME
    JULIA GARCIA FONSECA

    Contatos
    TELEFONE 1
    [RESIDENCIAL] ((12)) 3949-2060
    TELEFONE 2
    [RESIDENCIAL] ((12)) 98198-4989

    Documentos
    Sem informação
    """

    phones = extract_phones_from_text(raw)

    assert phones == [
        ("(12) 3949-2060", "1239492060"),
        ("(12) 98198-4989", "12981984989"),
    ]
    assert all(digits != "700003672681800" for _raw, digits in phones)


def test_gonzales_sisreg_contact_extraction_keeps_personal_email() -> None:
    raw = """
    Contatos
    TELEFONE 1
    [OUTRO] ((21)) 2105-0000
    TELEFONE 2
    [RESIDENCIAL] ((61)) 98142-2886
    E-MAIL 1
    danisg.dani@gmail.com
    """

    assert extract_phones_from_text(raw) == [
        ("(21) 2105-0000", "2121050000"),
        ("(61) 98142-2886", "61981422886"),
    ]
    assert extract_emails_from_text(raw) == ["danisg.dani@gmail.com"]


def test_phone_confidence_matches_the_cpf_that_carried_it(tmp_path: Path) -> None:
    """Two CPFs above threshold, each producing its own phone. The
    workflow must NOT average, multiply, or otherwise blend the
    scores — phone A's confidence equals CPF A's score and phone B's
    confidence equals CPF B's score.
    """
    lead = _lead()
    store, table_id = _store_with_lead(tmp_path, lead)
    # A single name-stage row holds both candidates, mirroring the way
    # the real workflow persists multiple matches from one /nome consult.
    store.save_telegram_consult(
        table_id=table_id,
        lead_ref=lead.linkedin_url,
        provider="gon",
        lead_name=lead.person_name,
        query=f"/nome {lead.person_name}",
        raw_text="…",
        source_url=None,
        downloaded_at=None,
        error=None,
        extracted_nome=lead.person_name,
        extracted_cpf="111.111.111-11",
        extracted_candidates=[
            {
                "cpf": "111.111.111-11",
                "nome": lead.person_name,
                "data_nascimento": "10/01/1985",
                "endereco": "Rua X, São Paulo/SP",
                "match_score": 92,
                "signals_used": ["name", "location"],
                "breakdown": {"confidence_label": "alta"},
            },
            {
                "cpf": "222.222.222-22",
                "nome": lead.person_name,
                "data_nascimento": "10/01/1985",
                "endereco": "Rua X, São Paulo/SP",
                "match_score": 70,
                "signals_used": ["name", "location"],
                "breakdown": {"confidence_label": "media"},
            },
        ],
        match_score=92,
        run_id="run-name",
        query_type="name",
    )

    phone_by_cpf = {
        "111.111.111-11": "(11) 99111-0000",
        "222.222.222-22": "(21) 98222-1111",
    }

    def consult_fn(cpf: str) -> TelegramConsultResult:
        return TelegramConsultResult(
            provider="gon_cpf",
            lead_name=lead.person_name,
            query=f"/cpf {cpf}",
            raw_text=f"CPF: {cpf}\nTelefone: {phone_by_cpf[cpf]}",
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
        run_id="run-multi",
    )

    by_cpf = {candidate.cpf: candidate for candidate in out}
    assert by_cpf["111.111.111-11"].confidence == 92
    assert by_cpf["222.222.222-22"].confidence == 70
    # Order is the eligibility-ranked order: 92 first, then 70.
    assert [c.cpf for c in out] == ["111.111.111-11", "222.222.222-22"]


# ---------------------------------------------------------------------------
# Persistence: applying the follow-up via the existing phone enrichment
# update path must never overwrite an existing phone, and must record
# divergent phones as alternatives.
# ---------------------------------------------------------------------------


def _update_from_candidate(candidate: TelegramPhoneCandidate) -> PhoneEnrichmentUpdate:
    """Adapter mirroring what the future server endpoint will do.

    Keeping this in the test rather than the production code keeps the
    refactor scope tight: the workflow returns ``TelegramPhoneCandidate``
    instances and any caller is responsible for projecting them onto
    ``PhoneEnrichmentUpdate``. The mapping below is what the
    /telegram-followup endpoint should do once Phase 9 adds it.
    """
    digits_only = candidate.phone_digits
    e164 = digits_only if digits_only.startswith("55") else f"55{digits_only}"
    return PhoneEnrichmentUpdate(
        phone=f"+{e164}",
        national=candidate.phone_raw,
        confidence=candidate.confidence,
        source=f"telegram_consult_cpf:{candidate.source_provider}",
        source_url=candidate.provenance.get("raw_source_url"),
    )


def test_phone_followup_does_not_overwrite_existing_lead_phone(
    tmp_path: Path,
) -> None:
    existing_phone = "+5511988887777"
    lead = _lead(phone=existing_phone)
    store, table_id = _store_with_lead(tmp_path, lead)
    _seed_cpf_candidate(store, table_id, lead, cpf="111.222.333-44", score=90)

    def consult_fn(cpf: str) -> TelegramConsultResult:
        return TelegramConsultResult(
            provider="gon_cpf",
            lead_name=lead.person_name,
            query=f"/cpf {cpf}",
            raw_text=f"CPF: {cpf}\nTelefone: (11) 90000-0001",
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
        run_id="run-no-overwrite",
    )
    assert len(out) == 1

    store.apply_internal_phone_enrichment_updates(
        table_id, [(lead, _update_from_candidate(out[0]))]
    )

    persisted = store.list_leads(table_id)[0]
    assert persisted.phone == existing_phone
    # The diverging phone lands in the alternatives trail.
    alternatives_digits = {alt.get("phone") for alt in persisted.phone_alternatives}
    assert any("11900000001" in (alt or "") for alt in alternatives_digits)


def test_phone_followup_records_alternative_when_phone_diverges(
    tmp_path: Path,
) -> None:
    """Same lead, existing phone differs from the Telegram follow-up's
    phone. Apply the update via ``apply_internal_phone_enrichment_updates``
    and verify the primary stays put while the new value joins the
    alternatives list. The internal enrichment update path is the same
    one the Telegram follow-up will reuse end-to-end.
    """
    lead = _lead(phone="+5511777776666")
    store, table_id = _store_with_lead(tmp_path, lead)
    _seed_cpf_candidate(store, table_id, lead, cpf="111.222.333-44", score=75)

    def consult_fn(cpf: str) -> TelegramConsultResult:
        return TelegramConsultResult(
            provider="gon_cpf",
            lead_name=lead.person_name,
            query=f"/cpf {cpf}",
            raw_text=f"CPF: {cpf}\nFone: (21) 98888-1111",
            source_url="https://exemplo.com/r/cpf",
            downloaded_at=None,
            error=None,
        )

    out = run_phone_followup(
        lead=lead,
        table_id=table_id,
        store=store,
        consult_fn=consult_fn,
        target_titles=None,
        run_id="run-alt",
    )
    assert len(out) == 1
    candidate = out[0]
    assert candidate.confidence == 75

    counters = store.apply_internal_phone_enrichment_updates(
        table_id, [(lead, _update_from_candidate(candidate))]
    )
    assert counters["skipped_existing_phone"] >= 1

    persisted = store.list_leads(table_id)[0]
    # Primary phone untouched.
    assert persisted.phone == "+5511777776666"
    # New phone present in alternatives with the Telegram source label.
    sources = [alt.get("source") for alt in persisted.phone_alternatives]
    assert any("telegram_consult_cpf" in (s or "") for s in sources)


def test_phone_followup_replaces_document_like_existing_phone(
    tmp_path: Path,
) -> None:
    lead = _lead(phone="700003672681800")
    store, table_id = _store_with_lead(tmp_path, lead)
    _seed_cpf_candidate(store, table_id, lead, cpf="111.222.333-44", score=90)

    def consult_fn(cpf: str) -> TelegramConsultResult:
        return TelegramConsultResult(
            provider="gon_cpf",
            lead_name=lead.person_name,
            query=f"/cpf {cpf}",
            raw_text=(
                "Dados pessoais\nCNS\n700003672681800\n"
                "Contatos\nTELEFONE 1\n[RESIDENCIAL] ((12)) 3949-2060"
            ),
            source_url="https://exemplo.com/r/cpf",
            downloaded_at=None,
            error=None,
        )

    out = run_phone_followup(
        lead=lead,
        table_id=table_id,
        store=store,
        consult_fn=consult_fn,
        target_titles=None,
        run_id="run-replace-cns",
    )
    assert len(out) == 1

    counters = store.apply_internal_phone_enrichment_updates(
        table_id, [(lead, _update_from_candidate(out[0]))]
    )

    persisted = store.list_leads(table_id)[0]
    assert counters["enriched"] == 1
    assert counters["skipped_existing_phone"] == 0
    assert persisted.phone == "+551239492060"


# ---------------------------------------------------------------------------
# Address: the SISREG-III ``/cpf`` report carries the residential address
# (the name query does not). We parse it from both render variants and
# persist it onto the lead alongside the phone — first write wins.
# ---------------------------------------------------------------------------


_SISREG_ADDRESS_MULTILINE = """
Endereço
TIPO DE LOGRADOURO
QUADRA
LOGRADOURO
QR 406 CONJUNTO 14
COMPLEMENTO
CASA
NÚMERO
06
BAIRRO
SAMAMBAIA NORTE (SAMAMBAIA)
MUNICÍPIO DE RESIDÊNCIA
BRASILIA - DF
CEP
72318-215
PAÍS
BRASIL
Informações adicionais
"""

_SISREG_ADDRESS_INLINE = (
    "Endereço Tipo de logradouro RUA Logradouro GEORGE GUYNEMER "
    "Complemento Sem informação Número 100 Bairro PARQUE EDU CHAVES "
    "Município de residência SAO PAULO - SP CEP 02233-100 País BRASIL "
    "Informações adicionais Info 1 Cartoes agregados do usuario."
)


def test_extract_address_from_text_multiline_sisreg_block() -> None:
    assert extract_address_from_text(_SISREG_ADDRESS_MULTILINE) == (
        "QR 406 CONJUNTO 14, Nº 06, CASA, "
        "Bairro: SAMAMBAIA NORTE (SAMAMBAIA), BRASILIA - DF, CEP: 72318-215"
    )


def test_extract_address_from_text_inline_sisreg_block() -> None:
    # "Complemento Sem informação" must be dropped, not stitched in.
    assert extract_address_from_text(_SISREG_ADDRESS_INLINE) == (
        "GEORGE GUYNEMER, Nº 100, Bairro: PARQUE EDU CHAVES, "
        "SAO PAULO - SP, CEP: 02233-100"
    )


def test_extract_address_from_text_none_when_sem_informacao() -> None:
    raw = "Endereço\nENDEREÇO\nSem informação\nInformações adicionais\nInfo 1\n"
    assert extract_address_from_text(raw) is None


def test_extract_address_from_text_none_without_section() -> None:
    assert extract_address_from_text("Nome\nAna Silva\nCPF\n111.222.333-44") is None


def test_run_phone_followup_carries_contact_address(tmp_path: Path) -> None:
    lead = _lead()
    store, table_id = _store_with_lead(tmp_path, lead)
    _seed_cpf_candidate(store, table_id, lead, cpf="111.222.333-44", score=85)

    def consult_fn(cpf: str) -> TelegramConsultResult:
        return TelegramConsultResult(
            provider="gon_cpf",
            lead_name=lead.person_name,
            query=f"/cpf {cpf}",
            raw_text=(
                f"Relatório de CPF (SISREG-III)\nCPF\n{cpf}\n"
                "Contatos\nTELEFONE 1\n[RESIDENCIAL] ((11)) 99999-0000\n"
                + _SISREG_ADDRESS_MULTILINE
            ),
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
        run_id="run-address",
    )

    assert len(out) == 1
    assert out[0].provenance["contact_address"] == (
        "QR 406 CONJUNTO 14, Nº 06, CASA, "
        "Bairro: SAMAMBAIA NORTE (SAMAMBAIA), BRASILIA - DF, CEP: 72318-215"
    )


def test_apply_contact_address_persists_and_never_overwrites(tmp_path: Path) -> None:
    lead = _lead()
    store, table_id = _store_with_lead(tmp_path, lead)

    first = store.apply_contact_address_updates(
        table_id, [(lead, "QR 406 CONJUNTO 14, Nº 06, BRASILIA - DF")]
    )
    assert first["enriched"] == 1
    assert store.list_leads(table_id)[0].endereco == (
        "QR 406 CONJUNTO 14, Nº 06, BRASILIA - DF"
    )

    # A second consult for the same lead must NOT clobber the address.
    second = store.apply_contact_address_updates(
        table_id, [(lead, "OUTRA RUA, Nº 99, SAO PAULO - SP")]
    )
    assert second["enriched"] == 0
    assert second["skipped_existing_address"] == 1
    assert store.list_leads(table_id)[0].endereco == (
        "QR 406 CONJUNTO 14, Nº 06, BRASILIA - DF"
    )


def test_apply_telegram_contact_details_persists_address(tmp_path: Path) -> None:
    from beautiful_linkedin.server.app import _apply_telegram_contact_details

    lead = _lead()
    store, table_id = _store_with_lead(tmp_path, lead)
    candidate = TelegramPhoneCandidate(
        phone_raw="(11) 99999-0000",
        phone_digits="11999990000",
        cpf="111.222.333-44",
        confidence=85,
        source_provider="gon_cpf",
        run_id="run-details",
        lead_ref=lead.linkedin_url,
        nome=lead.person_name,
        provenance={
            "contact_address": "QR 406 CONJUNTO 14, Nº 06, BRASILIA - DF",
            "contact_emails": ["ana.pessoal@gmail.com"],
        },
    )

    _apply_telegram_contact_details(
        store=store, table_id=table_id, lead=lead, candidates=[candidate]
    )

    persisted = store.list_leads(table_id)[0]
    assert persisted.endereco == "QR 406 CONJUNTO 14, Nº 06, BRASILIA - DF"
