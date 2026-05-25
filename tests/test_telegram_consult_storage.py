"""Tests for the ``tabela_telegram`` CRUD on ``SavedLeadsStore``.

The table holds one row per (table_id, lead_ref, provider). Both
providers ("gon" and "unix") can coexist for the same lead and a
re-run for the same (lead, provider) replaces the prior row.
"""

from __future__ import annotations

from pathlib import Path

from beautiful_linkedin.storage.saved_leads import SavedLeadsStore


def _store(tmp_path: Path) -> SavedLeadsStore:
    return SavedLeadsStore(tmp_path / "saved.sqlite")


def test_save_and_list_telegram_consult(tmp_path: Path) -> None:
    store = _store(tmp_path)
    table = store.create_table(name="Teste")

    saved = store.save_telegram_consult(
        table_id=table.id,
        lead_ref="https://linkedin.com/in/john",
        provider="unix",
        lead_name="John Doe",
        query="/nome John Doe",
        raw_text="resultado xyz",
        source_url="https://exemplo.com/r/abc",
        downloaded_at="2026-05-20T12:00:00Z",
        error=None,
    )

    assert saved.id > 0
    assert saved.provider == "unix"
    assert saved.lead_name == "John Doe"
    assert saved.raw_text == "resultado xyz"
    assert saved.error is None

    listed = store.list_telegram_consults(table.id)
    assert len(listed) == 1
    assert listed[0].lead_ref == "https://linkedin.com/in/john"


def test_save_telegram_consult_upserts_same_provider(tmp_path: Path) -> None:
    store = _store(tmp_path)
    table = store.create_table(name="Teste")

    store.save_telegram_consult(
        table_id=table.id,
        lead_ref="ref-1",
        provider="unix",
        lead_name="John Doe",
        query="/nome John Doe",
        raw_text="primeiro",
        source_url=None,
        downloaded_at=None,
        error=None,
    )
    updated = store.save_telegram_consult(
        table_id=table.id,
        lead_ref="ref-1",
        provider="unix",
        lead_name="John Doe",
        query="/nome John Doe",
        raw_text="segundo",
        source_url=None,
        downloaded_at=None,
        error=None,
    )

    assert updated.raw_text == "segundo"
    listed = store.list_telegram_consults(table.id)
    assert len(listed) == 1
    assert listed[0].raw_text == "segundo"


def test_gon_and_unix_coexist_per_lead(tmp_path: Path) -> None:
    """The (table_id, lead_ref, provider) unique constraint must let
    both providers persist independently for the same lead."""
    store = _store(tmp_path)
    table = store.create_table(name="Teste")

    store.save_telegram_consult(
        table_id=table.id,
        lead_ref="ref-1",
        provider="gon",
        lead_name="Ana",
        query="/nome Ana",
        raw_text="gon body",
        source_url=None,
        downloaded_at=None,
        error=None,
        extracted_cpf="111.222.333-44",
        match_score=80,
    )
    store.save_telegram_consult(
        table_id=table.id,
        lead_ref="ref-1",
        provider="unix",
        lead_name="Ana",
        query="/nome Ana",
        raw_text="unix body",
        source_url=None,
        downloaded_at=None,
        error=None,
        extracted_cpf="999.888.777-66",
        match_score=40,
    )

    listed = store.list_telegram_consults(table.id)
    # list_telegram_consults orders gon first then unix.
    assert [c.provider for c in listed] == ["gon", "unix"]
    assert listed[0].match_score == 80
    assert listed[1].extracted_cpf == "999.888.777-66"


def test_save_telegram_consult_persists_candidates_and_match_details(tmp_path: Path) -> None:
    store = _store(tmp_path)
    table = store.create_table(name="Teste")

    saved = store.save_telegram_consult(
        table_id=table.id,
        lead_ref="ref-x",
        provider="gon",
        lead_name="Ana",
        query="/nome Ana",
        raw_text="...",
        source_url=None,
        downloaded_at=None,
        error=None,
        extracted_candidates=[
            {"cpf": "111.222.333-44", "nome": "Ana Silva", "match_score": 80},
            {"cpf": "999.888.777-66", "nome": "Ana Costa", "match_score": 30},
        ],
        match_score=80,
        match_details={"location": {"score": 100}},
    )

    assert len(saved.extracted_candidates) == 2
    assert saved.extracted_candidates[0]["cpf"] == "111.222.333-44"
    assert saved.match_details["location"]["score"] == 100


def test_save_telegram_consult_records_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    table = store.create_table(name="Teste")

    saved = store.save_telegram_consult(
        table_id=table.id,
        lead_ref="ref-err",
        provider="gon",
        lead_name="Erro Doe",
        query="/nome Erro Doe",
        raw_text=None,
        source_url=None,
        downloaded_at=None,
        error="timeout_bot",
    )

    assert saved.error == "timeout_bot"
    assert saved.raw_text is None


def test_list_telegram_consults_is_table_scoped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    table_a = store.create_table(name="A")
    table_b = store.create_table(name="B")

    store.save_telegram_consult(
        table_id=table_a.id,
        lead_ref="a-1",
        provider="unix",
        lead_name="A",
        query="/nome A",
        raw_text="A texto",
        source_url=None,
        downloaded_at=None,
        error=None,
    )
    store.save_telegram_consult(
        table_id=table_b.id,
        lead_ref="b-1",
        provider="unix",
        lead_name="B",
        query="/nome B",
        raw_text="B texto",
        source_url=None,
        downloaded_at=None,
        error=None,
    )

    assert [c.lead_ref for c in store.list_telegram_consults(table_a.id)] == ["a-1"]
    assert [c.lead_ref for c in store.list_telegram_consults(table_b.id)] == ["b-1"]
