"""Durable SQLite-backed store of saved lead tables.

This is intentionally separate from ``cache/`` because saved leads are
durable product data, not API response caches. The store reuses the
locking/context-manager pattern from ``cache/lead_history.py``.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import unicodedata
import uuid
import csv
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from beautiful_linkedin.export.csv_exporter import (
    OUTPUT_COLUMNS,
    export_csv as export_csv_official,
)
from beautiful_linkedin.models import Lead
from beautiful_linkedin.processing.deduplicator import (
    compute_global_key,
    deduplicate_leads,
    lead_dedupe_key,
)


SOURCE_TYPE_SEARCH = "search"
SOURCE_TYPE_IMPORTED = "imported"
SOURCE_TYPE_MERGED = "merged"

ENRICHMENT_NOT_ENRICHED = "not_enriched"
ENRICHMENT_NOT_IMPLEMENTED = "not_implemented"
ENRICHMENT_ENRICHED = "enriched"

SAVED_LEADS_EXPORT_COLUMNS = [
    "Company Name",
    "First Name",
    "Full Name",
    "LinkedIn",
    "Cargo",
    "E-mail",
    "Name S",
    "Full Name S",
    "Cargo",
    "LinkedIn",
    "Telefone",
]


# Secret-bearing fields that must never be persisted in search_request_json.
_FORBIDDEN_REQUEST_FIELDS = {
    "linkedin_cookie",
    "linkedin_li_at_cookie",
    "api_keys",
}


_PERSON_ALIASES = {
    "person_name",
    "name",
    "full_name",
    "fullname",
    "nome",
    "nome_completo",
    "lead_name",
    "contact_name",
    "profile_name",
}
_COMPANY_ALIASES = {
    "company_name",
    "company",
    "organization",
    "organization_name",
    "empresa",
    "current_company",
    "employer",
    "account_name",
}
_TITLE_ALIASES = {
    "title",
    "job_title",
    "current_title",
    "position",
    "job_position",
    "role",
    "cargo",
    "funcao",
    "função",
    "headline",
    "linkedin_headline",
    "occupation",
    "current_position",
}
_LINKEDIN_ALIASES = {"linkedin_url", "linkedin", "profile_url", "linkedin_profile"}
_EMAIL_ALIASES = {"email", "e-mail", "mail", "work_email"}
_PHONE_ALIASES = {"phone", "telefone", "mobile", "celular", "work_phone"}
_DOMAIN_ALIASES = {"company_domain", "domain", "website", "company_website"}


class SavedLeadTable(BaseModel):
    id: str
    name: str
    created_at: str
    updated_at: str
    source_type: str
    keywords: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)
    search_request: dict[str, Any] = Field(default_factory=dict)
    enrichment_status: str = ENRICHMENT_NOT_ENRICHED
    lead_count: int = 0


class TelegramConsult(BaseModel):
    """One Telegram-group consult result tied to a saved lead.

    A single (table_id, lead_ref) can have multiple rows — one per
    provider ("gon" + "unix") — so each provider's output is preserved
    independently and the UI can show them side by side.

    ``query_type`` distinguishes the name-stage consult (``"name"``,
    default) from later sensitive follow-ups (``"cpf"``, ``"phone"``,
    ``"email"``) so the row also serves as the durable event store for
    the in-process pipeline. ``run_id`` groups rows that came out of the
    same workflow invocation. ``blocked_reason`` records why a row was
    persisted without consulting the provider (e.g. cargo divergente).
    """

    id: int
    table_id: str
    lead_ref: str
    provider: str = "unix"
    lead_name: str
    query: str
    raw_text: str | None = None
    source_url: str | None = None
    downloaded_at: str | None = None
    error: str | None = None
    extracted_nome: str | None = None
    extracted_cpf: str | None = None
    extracted_birth_date: str | None = None
    extracted_address: str | None = None
    extracted_candidates: list[dict[str, Any]] = Field(default_factory=list)
    match_score: int | None = None
    match_details: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    run_id: str | None = None
    query_type: str = "name"
    query_value: str | None = None
    blocked_reason: str | None = None


class TelegramProviderStateRow(BaseModel):
    """In-DB shape of one provider's runtime state.

    Mirrors :class:`telegram_pipeline.TelegramProviderState`; kept in
    storage so the cooldown survives a server restart.
    """

    provider: str
    cooldown_until: str | None = None
    last_error: str | None = None
    updated_at: str | None = None


class ImportColumnError(ValueError):
    """Raised when an imported file lacks the minimum required columns."""


class SavedLeadsStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # ---- table CRUD --------------------------------------------------------

    def create_table(
        self,
        *,
        name: str,
        source_type: str = SOURCE_TYPE_SEARCH,
        keywords: Iterable[str] | None = None,
        search_queries: Iterable[str] | None = None,
        search_request: dict[str, Any] | None = None,
        enrichment_status: str = ENRICHMENT_NOT_ENRICHED,
    ) -> SavedLeadTable:
        cleaned_name = _clean_name(name)
        now = _now_iso()
        table_id = uuid.uuid4().hex
        sanitized_request = _sanitize_search_request(search_request or {})
        clean_keywords = _dedupe_preserving_order(keywords or [])
        clean_queries = _dedupe_preserving_order(search_queries or [])

        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO saved_lead_tables(
                    id, name, created_at, updated_at, source_type,
                    keywords_json, search_queries_json, search_request_json,
                    enrichment_status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    table_id,
                    cleaned_name,
                    now,
                    now,
                    source_type,
                    json.dumps(clean_keywords, ensure_ascii=False),
                    json.dumps(clean_queries, ensure_ascii=False),
                    json.dumps(sanitized_request, ensure_ascii=False, sort_keys=True),
                    enrichment_status,
                ),
            )

        return SavedLeadTable(
            id=table_id,
            name=cleaned_name,
            created_at=now,
            updated_at=now,
            source_type=source_type,
            keywords=clean_keywords,
            search_queries=clean_queries,
            search_request=sanitized_request,
            enrichment_status=enrichment_status,
            lead_count=0,
        )

    def list_tables(self) -> list[SavedLeadTable]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT t.id, t.name, t.created_at, t.updated_at, t.source_type,
                       t.keywords_json, t.search_queries_json, t.search_request_json,
                       t.enrichment_status,
                       COALESCE(c.cnt, 0) AS lead_count
                FROM saved_lead_tables t
                LEFT JOIN (
                    SELECT table_id, COUNT(*) AS cnt FROM saved_leads GROUP BY table_id
                ) c ON c.table_id = t.id
                ORDER BY t.updated_at DESC
                """
            ).fetchall()
        return [_row_to_table(row) for row in rows]

    def get_table(self, table_id: str) -> SavedLeadTable:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT t.id, t.name, t.created_at, t.updated_at, t.source_type,
                       t.keywords_json, t.search_queries_json, t.search_request_json,
                       t.enrichment_status,
                       (SELECT COUNT(*) FROM saved_leads WHERE table_id = t.id) AS lead_count
                FROM saved_lead_tables t
                WHERE t.id = ?
                """,
                (table_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"saved lead table not found: {table_id}")
        return _row_to_table(row)

    def delete_table(self, table_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM saved_leads WHERE table_id = ?", (table_id,)
            )
            cursor = connection.execute(
                "DELETE FROM saved_lead_tables WHERE id = ?", (table_id,)
            )
            if cursor.rowcount == 0:
                raise KeyError(f"saved lead table not found: {table_id}")

    # ---- global dedup ------------------------------------------------------

    def global_dedupe_keys(self, *, exclude_table_id: str | None = None) -> set[str]:
        """Return the identity keys of every lead already saved, any table.

        Used by the search pipeline to skip leads that were consulted before
        (cross-table dedup). Keys follow :func:`compute_global_key`: LinkedIn
        URL primeiro, depois nome+empresa+cargo. ``exclude_table_id`` lets a
        re-enrichment of an existing table ignore its own rows.
        """
        keys: set[str] = set()
        with self._lock, self._connect() as connection:
            if exclude_table_id is None:
                rows = connection.execute(
                    """
                    SELECT linkedin_url, person_name, company_name, title, source_url
                    FROM saved_leads
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT linkedin_url, person_name, company_name, title, source_url
                    FROM saved_leads
                    WHERE table_id != ?
                    """,
                    (exclude_table_id,),
                ).fetchall()
        for row in rows:
            key = compute_global_key(
                linkedin_url=row["linkedin_url"],
                person_name=row["person_name"],
                company_name=row["company_name"],
                title=row["title"],
                source_url=row["source_url"],
            )
            if key is not None:
                keys.add(key)
        return keys

    # ---- leads -------------------------------------------------------------

    def add_leads(self, table_id: str, leads: list[Lead]) -> int:
        if not leads:
            self._touch_table(table_id)
            return 0

        inserted = 0
        with self._lock, self._connect() as connection:
            self._assert_table_exists(connection, table_id)
            for index, lead in enumerate(leads):
                lead_key = _lead_key_for(lead, fallback_index=index)
                values = _lead_row_values(table_id, lead_key, lead)
                cursor = connection.execute(
                    """
                    INSERT INTO saved_leads(
                        table_id, lead_key,
                        company_name, company_domain, person_name, title,
                        linkedin_url, email, phone,
                        linkedin_profile_validation_status,
                        linkedin_experience_title,
                        linkedin_experience_company,
                        linkedin_experience_start_year,
                        linkedin_experience_end_year,
                        linkedin_experience_checked_at,
                        linkedin_contact_email,
                        linkedin_contact_website,
                        linkedin_contact_phone,
                        linkedin_location,
                        linkedin_education_json,
                        linkedin_birthday,
                        source_url, source_type, snippet,
                        matched_title, validation_status, validation_note,
                        confidence_score, previously_consulted_at, consultation_note,
                        raw_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(table_id, lead_key) DO UPDATE SET
                        company_name = excluded.company_name,
                        company_domain = excluded.company_domain,
                        person_name = excluded.person_name,
                        title = excluded.title,
                        linkedin_url = excluded.linkedin_url,
                        email = excluded.email,
                        phone = excluded.phone,
                        linkedin_profile_validation_status = excluded.linkedin_profile_validation_status,
                        linkedin_experience_title = excluded.linkedin_experience_title,
                        linkedin_experience_company = excluded.linkedin_experience_company,
                        linkedin_experience_start_year = excluded.linkedin_experience_start_year,
                        linkedin_experience_end_year = excluded.linkedin_experience_end_year,
                        linkedin_experience_checked_at = excluded.linkedin_experience_checked_at,
                        linkedin_contact_email = excluded.linkedin_contact_email,
                        linkedin_contact_website = excluded.linkedin_contact_website,
                        linkedin_contact_phone = excluded.linkedin_contact_phone,
                        linkedin_location = excluded.linkedin_location,
                        linkedin_education_json = excluded.linkedin_education_json,
                        linkedin_birthday = excluded.linkedin_birthday,
                        source_url = excluded.source_url,
                        source_type = excluded.source_type,
                        snippet = excluded.snippet,
                        matched_title = excluded.matched_title,
                        validation_status = excluded.validation_status,
                        validation_note = excluded.validation_note,
                        confidence_score = excluded.confidence_score,
                        previously_consulted_at = excluded.previously_consulted_at,
                        consultation_note = excluded.consultation_note,
                        raw_json = excluded.raw_json
                    WHERE excluded.confidence_score > saved_leads.confidence_score
                    """,
                    values,
                )
                if cursor.rowcount > 0:
                    inserted += 1
            connection.execute(
                "UPDATE saved_lead_tables SET updated_at = ? WHERE id = ?",
                (_now_iso(), table_id),
            )
        return inserted

    def list_leads(self, table_id: str) -> list[Lead]:
        # SELECT * so the enrichment columns flow through to the Lead
        # without each new field needing an extra rename.
        with self._lock, self._connect() as connection:
            self._assert_table_exists(connection, table_id)
            rows = connection.execute(
                """
                SELECT *
                FROM saved_leads
                WHERE table_id = ?
                ORDER BY confidence_score DESC, id ASC
                """,
                (table_id,),
            ).fetchall()
        return [_row_to_lead(row) for row in rows]

    # ---- export / import / merge / enrich ---------------------------------

    def export_csv(
        self,
        table_id: str,
        output_path: str | Path | None = None,
        *,
        format: str = "tabela",
    ) -> Path:
        table = self.get_table(table_id)
        leads = self.list_leads(table_id)
        path = (
            Path(output_path)
            if output_path is not None
            else Path("output/saved-leads") / f"{_slugify(table.name)}.csv"
        )
        if format == "oficial":
            return export_csv_official(leads, path)
        if format == "tabela":
            return export_saved_leads_csv(leads, path)
        raise ValueError(f"unknown export format: {format!r}")

    def import_table(self, *, name: str, file_path: str | Path) -> SavedLeadTable:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"import file not found: {path}")

        import pandas as pd  # local import — heavy and only needed here

        suffix = path.suffix.lower()
        if suffix in {".xlsx", ".xls"}:
            df = pd.read_excel(path)
        else:
            df = pd.read_csv(path)

        leads = _dataframe_to_leads(df, file_stem=path.stem)
        table = self.create_table(
            name=name,
            source_type=SOURCE_TYPE_IMPORTED,
            keywords=[],
            search_queries=[],
            search_request={"imported_from": path.name, "row_count": len(leads)},
        )
        self.add_leads(table.id, leads)
        return self.get_table(table.id)

    def merge_tables(
        self,
        *,
        name: str,
        table_ids: list[str],
        keywords: Iterable[str] | None = None,
    ) -> SavedLeadTable:
        if not table_ids:
            raise ValueError("merge_tables requires at least one source table id")

        merged_leads: list[Lead] = []
        merged_keywords: list[str] = list(keywords or [])
        merged_queries: list[str] = []
        for table_id in table_ids:
            source = self.get_table(table_id)
            merged_keywords.extend(source.keywords)
            merged_queries.extend(source.search_queries)
            merged_leads.extend(self.list_leads(table_id))

        unique_leads = deduplicate_leads(merged_leads)
        table = self.create_table(
            name=name,
            source_type=SOURCE_TYPE_MERGED,
            keywords=merged_keywords,
            search_queries=merged_queries,
            search_request={"merged_from": list(table_ids)},
        )
        self.add_leads(table.id, unique_leads)
        return self.get_table(table.id)

    def mark_enrichment(
        self,
        table_id: str,
        *,
        status: str = ENRICHMENT_NOT_IMPLEMENTED,
        payload: dict[str, Any] | None = None,
    ) -> SavedLeadTable:
        now = _now_iso()
        with self._lock, self._connect() as connection:
            self._assert_table_exists(connection, table_id)
            connection.execute(
                """
                UPDATE saved_lead_tables
                SET enrichment_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, now, table_id),
            )
            if payload is not None:
                connection.execute(
                    """
                    UPDATE saved_leads
                    SET enrichment_payload_json = ?, enriched_at = ?
                    WHERE table_id = ?
                    """,
                    (
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        now,
                        table_id,
                    ),
                )
        return self.get_table(table_id)

    def apply_enrichment_updates(
        self,
        table_id: str,
        updates: list[Any],
    ) -> int:
        """Persist contact enrichments for existing saved leads.

        ``updates`` are intentionally duck-typed to avoid coupling storage to a
        provider implementation. Each update must expose ``lead``, ``email``,
        ``phone``, ``provider`` and ``raw`` attributes.
        """
        if not updates:
            return 0

        now = _now_iso()
        updated = 0
        with self._lock, self._connect() as connection:
            self._assert_table_exists(connection, table_id)
            for update in updates:
                lead = update.lead
                lead_key = _lead_key_for(lead, fallback_index=0)
                existing = connection.execute(
                    """
                    SELECT email, phone, consultation_note,
                           email_verified_by_json, email_alternatives_json
                    FROM saved_leads
                    WHERE table_id = ? AND lead_key = ?
                    """,
                    (table_id, lead_key),
                ).fetchone()
                if existing is None:
                    continue
                email = existing["email"] or update.email
                phone = existing["phone"] or update.phone
                # Cross-provider verification trail. Same email coming
                # from a paid provider that internal already found turns
                # into a badge; a different email goes to the alts list.
                verified_by, alternatives = _merge_email_verification(
                    existing_email=existing["email"],
                    existing_verified_by=_json_list(
                        existing["email_verified_by_json"]
                        if "email_verified_by_json" in existing.keys()
                        else None
                    ),
                    existing_alternatives=_json_dict_list(
                        existing["email_alternatives_json"]
                        if "email_alternatives_json" in existing.keys()
                        else None
                    ),
                    incoming_email=update.email,
                    incoming_source=update.provider,
                    incoming_confidence=None,
                    now=now,
                )
                note = _append_note(
                    existing["consultation_note"],
                    update.note or f"Enriquecido via {update.provider}.",
                )
                cursor = connection.execute(
                    """
                    UPDATE saved_leads
                    SET email = ?, phone = ?, consultation_note = ?,
                        enrichment_payload_json = ?, enriched_at = ?,
                        enrichment_source = ?,
                        enrichment_status = ?,
                        email_verified_by_json = ?,
                        email_alternatives_json = ?
                    WHERE table_id = ? AND lead_key = ?
                    """,
                    (
                        email,
                        phone,
                        note,
                        json.dumps(
                            {
                                "provider": update.provider,
                                "raw": update.raw,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                        update.provider,
                        "api_consulted",
                        json.dumps(verified_by, ensure_ascii=False),
                        json.dumps(alternatives, ensure_ascii=False),
                        table_id,
                        lead_key,
                    ),
                )
                if cursor.rowcount > 0 and (
                    (update.email and not existing["email"])
                    or (update.phone and not existing["phone"])
                ):
                    updated += 1
            if updated:
                connection.execute(
                    """
                    UPDATE saved_lead_tables
                    SET enrichment_status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (ENRICHMENT_ENRICHED, now, table_id),
                )
        return updated

    def mark_api_enrichment_attempt(
        self,
        table_id: str,
        leads: list[Lead],
        *,
        provider: str,
        raw: dict[str, Any] | None = None,
    ) -> int:
        """Mark leads as consulted by a paid API even when no data changed."""
        if not leads:
            return 0
        now = _now_iso()
        updated = 0
        note = f"Consultado via API {provider}; nenhum dado novo retornado."
        payload = {
            "provider": provider,
            "status": "no_data",
            "raw": raw or {},
        }
        with self._lock, self._connect() as connection:
            self._assert_table_exists(connection, table_id)
            for lead in leads:
                lead_key = _lead_key_for(lead, fallback_index=0)
                existing = connection.execute(
                    """
                    SELECT consultation_note
                    FROM saved_leads
                    WHERE table_id = ? AND lead_key = ?
                    """,
                    (table_id, lead_key),
                ).fetchone()
                if existing is None:
                    continue
                cursor = connection.execute(
                    """
                    UPDATE saved_leads
                    SET consultation_note = ?,
                        enrichment_payload_json = ?,
                        enriched_at = ?,
                        enrichment_source = ?,
                        enrichment_status = ?
                    WHERE table_id = ? AND lead_key = ?
                    """,
                    (
                        _append_note(existing["consultation_note"], note),
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        now,
                        provider,
                        "api_consulted_no_data",
                        table_id,
                        lead_key,
                    ),
                )
                if cursor.rowcount > 0:
                    updated += 1
            if updated:
                connection.execute(
                    """
                    UPDATE saved_lead_tables
                    SET updated_at = ?
                    WHERE id = ?
                    """,
                    (now, table_id),
                )
        return updated

    def apply_internal_enrichment_updates(
        self,
        table_id: str,
        updates: list[tuple[Lead, Any]],
    ) -> dict[str, int]:
        """Persist the outcome of :class:`InternalLeadEnrichmentService` runs.

        Each tuple is ``(lead, EnrichmentUpdate)``. The lead is matched by
        the same ``lead_key`` we already use for upserts. We never overwrite
        an existing e-mail — that's the service's contract — and we always
        persist the enrichment metadata, even on failures, so the UI can
        show "tried, no domain" / "tried, mismatched pattern".

        Returns counters: ``{"enriched": N, "skipped_existing_email": M,
        "failed_missing_domain": K, "no_change": L}``.
        """
        counters = {
            "enriched": 0,
            "skipped_existing_email": 0,
            "failed_missing_domain": 0,
            "no_change": 0,
        }
        if not updates:
            return counters
        now = _now_iso()
        with self._lock, self._connect() as connection:
            self._assert_table_exists(connection, table_id)
            for lead, update in updates:
                lead_key = _lead_key_for(lead, fallback_index=0)
                existing = connection.execute(
                    """
                    SELECT email, email_verified_by_json, email_alternatives_json
                    FROM saved_leads
                    WHERE table_id = ? AND lead_key = ?
                    """,
                    (table_id, lead_key),
                ).fetchone()
                if existing is None:
                    counters["no_change"] += 1
                    continue

                # Bucket each outcome for the response summary.
                status_value = getattr(update.enrichment_status, "value", str(update.enrichment_status))
                if update.skipped_existing_email or existing["email"]:
                    counters["skipped_existing_email"] += 1
                elif update.failure_reason == "missing_domain":
                    counters["failed_missing_domain"] += 1
                elif update.email:
                    counters["enriched"] += 1
                else:
                    counters["no_change"] += 1

                # Same cross-provider trail logic as the paid path: if
                # internal independently arrived at the same email a
                # paid provider already stored, mark it verified; if
                # internal proposes a different address, save it as an
                # alternative the UI can surface but never overwrite.
                verified_by, alternatives = _merge_email_verification(
                    existing_email=existing["email"],
                    existing_verified_by=_json_list(
                        existing["email_verified_by_json"]
                        if "email_verified_by_json" in existing.keys()
                        else None
                    ),
                    existing_alternatives=_json_dict_list(
                        existing["email_alternatives_json"]
                        if "email_alternatives_json" in existing.keys()
                        else None
                    ),
                    incoming_email=update.email,
                    incoming_source=update.enrichment_source or "internal",
                    incoming_confidence=update.enrichment_confidence,
                    now=now,
                )

                connection.execute(
                    """
                    UPDATE saved_leads
                    SET email = COALESCE(email, ?),
                        enrichment_source = ?,
                        enrichment_status = ?,
                        enrichment_confidence = ?,
                        email_type = ?,
                        email_validation_status = ?,
                        enriched_at = ?,
                        email_verified_by_json = ?,
                        email_alternatives_json = ?
                    WHERE table_id = ? AND lead_key = ?
                    """,
                    (
                        update.email,
                        update.enrichment_source,
                        status_value,
                        update.enrichment_confidence,
                        update.email_type,
                        getattr(update.email_validation_status, "value", str(update.email_validation_status)) if update.email_validation_status else None,
                        now,
                        json.dumps(verified_by, ensure_ascii=False),
                        json.dumps(alternatives, ensure_ascii=False),
                        table_id,
                        lead_key,
                    ),
                )
            if counters["enriched"] > 0:
                connection.execute(
                    """
                    UPDATE saved_lead_tables
                    SET enrichment_status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (ENRICHMENT_ENRICHED, now, table_id),
                )
        return counters

    def apply_contact_email_updates(
        self,
        table_id: str,
        updates: list[tuple[Lead, Any]],
    ) -> dict[str, int]:
        """Persist contact e-mails found by non-corporate discovery steps.

        Used by the Telegram phone stage when a CPF/SISREG/Findex result
        includes ``E-MAIL 1``. The corporate ``email`` column remains the
        primary address: if it already exists, the incoming personal
        address is only merged into ``email_alternatives_json``.
        """
        counters = {
            "enriched": 0,
            "skipped_existing_email": 0,
            "no_change": 0,
        }
        if not updates:
            return counters
        now = _now_iso()
        with self._lock, self._connect() as connection:
            self._assert_table_exists(connection, table_id)
            for lead, update in updates:
                incoming_email = _clean_optional(getattr(update, "email", None))
                if not incoming_email or "@" not in incoming_email:
                    counters["no_change"] += 1
                    continue
                lead_key = _lead_key_for(lead, fallback_index=0)
                existing = connection.execute(
                    """
                    SELECT email, email_type, email_validation_status,
                           enrichment_source, enrichment_confidence,
                           email_verified_by_json, email_alternatives_json
                    FROM saved_leads
                    WHERE table_id = ? AND lead_key = ?
                    """,
                    (table_id, lead_key),
                ).fetchone()
                if existing is None:
                    counters["no_change"] += 1
                    continue

                existing_email = existing["email"]
                if existing_email:
                    counters["skipped_existing_email"] += 1
                else:
                    counters["enriched"] += 1

                verified_by, alternatives = _merge_email_verification(
                    existing_email=existing_email,
                    existing_verified_by=_json_list(
                        existing["email_verified_by_json"]
                        if "email_verified_by_json" in existing.keys()
                        else None
                    ),
                    existing_alternatives=_json_dict_list(
                        existing["email_alternatives_json"]
                        if "email_alternatives_json" in existing.keys()
                        else None
                    ),
                    incoming_email=incoming_email,
                    incoming_source=getattr(update, "source", None)
                    or "telegram_contact",
                    incoming_confidence=getattr(update, "confidence", None),
                    now=now,
                )

                connection.execute(
                    """
                    UPDATE saved_leads
                    SET email = COALESCE(email, ?),
                        enrichment_source = CASE
                            WHEN email IS NULL THEN ?
                            ELSE enrichment_source
                        END,
                        enrichment_confidence = CASE
                            WHEN email IS NULL THEN ?
                            ELSE enrichment_confidence
                        END,
                        email_type = CASE
                            WHEN email IS NULL THEN ?
                            ELSE email_type
                        END,
                        email_validation_status = CASE
                            WHEN email IS NULL THEN ?
                            ELSE email_validation_status
                        END,
                        enriched_at = ?,
                        email_verified_by_json = ?,
                        email_alternatives_json = ?
                    WHERE table_id = ? AND lead_key = ?
                    """,
                    (
                        incoming_email,
                        getattr(update, "source", None) or "telegram_contact",
                        getattr(update, "confidence", None),
                        getattr(update, "email_type", None) or "personal",
                        getattr(update, "email_validation_status", None)
                        or "unknown",
                        now,
                        json.dumps(verified_by, ensure_ascii=False),
                        json.dumps(alternatives, ensure_ascii=False),
                        table_id,
                        lead_key,
                    ),
                )
            if counters["enriched"] > 0:
                connection.execute(
                    """
                    UPDATE saved_lead_tables
                    SET enrichment_status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (ENRICHMENT_ENRICHED, now, table_id),
                )
        return counters

    def apply_contact_address_updates(
        self,
        table_id: str,
        updates: list[tuple[Lead, str]],
    ) -> dict[str, int]:
        """Persist a residential address found by the Telegram CPF stage.

        The address comes from the SISREG-III ``/cpf`` report (the same
        lookup that harvests the phone). It is informational — it does
        NOT touch ``confidence`` or the table's enrichment status — and is
        never overwritten once a lead already has one (first write wins).
        """
        counters = {
            "enriched": 0,
            "skipped_existing_address": 0,
            "no_change": 0,
        }
        if not updates:
            return counters
        now = _now_iso()
        with self._lock, self._connect() as connection:
            self._assert_table_exists(connection, table_id)
            changed = False
            for lead, address in updates:
                value = _clean_optional(address)
                if not value:
                    counters["no_change"] += 1
                    continue
                lead_key = _lead_key_for(lead, fallback_index=0)
                existing = connection.execute(
                    """
                    SELECT endereco FROM saved_leads
                    WHERE table_id = ? AND lead_key = ?
                    """,
                    (table_id, lead_key),
                ).fetchone()
                if existing is None:
                    counters["no_change"] += 1
                    continue
                if existing["endereco"]:
                    counters["skipped_existing_address"] += 1
                    continue
                connection.execute(
                    """
                    UPDATE saved_leads
                    SET endereco = ?
                    WHERE table_id = ? AND lead_key = ? AND endereco IS NULL
                    """,
                    (value, table_id, lead_key),
                )
                counters["enriched"] += 1
                changed = True
            if changed:
                connection.execute(
                    "UPDATE saved_lead_tables SET updated_at = ? WHERE id = ?",
                    (now, table_id),
                )
        return counters

    def apply_internal_phone_enrichment_updates(
        self,
        table_id: str,
        updates: list[tuple[Lead, Any]],
    ) -> dict[str, int]:
        """Persist the outcome of internal phone enrichment runs.

        Mirrors :meth:`apply_internal_enrichment_updates` but for phones.
        Each tuple is ``(lead, PhoneEnrichmentUpdate)``. The lead is
        matched by ``lead_key``. The primary ``phone`` is never
        overwritten — that's the service's contract — and metadata
        columns are always written so the UI can show "tried, no
        candidate" / "tried, candidate was call-center".

        Returns counters: ``{"enriched", "skipped_existing_phone",
        "failed_no_candidate", "no_change"}``.
        """
        counters = {
            "enriched": 0,
            "skipped_existing_phone": 0,
            "failed_no_candidate": 0,
            "no_change": 0,
        }
        if not updates:
            return counters
        now = _now_iso()
        with self._lock, self._connect() as connection:
            self._assert_table_exists(connection, table_id)
            for lead, update in updates:
                lead_key = _lead_key_for(lead, fallback_index=0)
                existing = connection.execute(
                    """
                    SELECT phone, phone_verified_by_json, phone_alternatives_json
                    FROM saved_leads
                    WHERE table_id = ? AND lead_key = ?
                    """,
                    (table_id, lead_key),
                ).fetchone()
                if existing is None:
                    counters["no_change"] += 1
                    continue

                incoming_phone = getattr(update, "phone", None)
                existing_phone = existing["phone"]
                existing_phone_blocks_update = bool(
                    existing_phone and not _is_document_like_phone(existing_phone)
                )
                if (
                    getattr(update, "skipped_existing_phone", False)
                    or existing_phone_blocks_update
                ):
                    counters["skipped_existing_phone"] += 1
                elif getattr(update, "failure_reason", None) == "no_candidate":
                    counters["failed_no_candidate"] += 1
                elif incoming_phone:
                    counters["enriched"] += 1
                else:
                    counters["no_change"] += 1

                verified_by, alternatives = _merge_phone_verification(
                    existing_phone=(
                        existing_phone if existing_phone_blocks_update else None
                    ),
                    existing_verified_by=_json_list(
                        existing["phone_verified_by_json"]
                        if "phone_verified_by_json" in existing.keys()
                        else None
                    ),
                    existing_alternatives=_json_dict_list(
                        existing["phone_alternatives_json"]
                        if "phone_alternatives_json" in existing.keys()
                        else None
                    ),
                    incoming_phone=incoming_phone,
                    incoming_source=getattr(update, "source", None) or "internal",
                    incoming_confidence=getattr(update, "confidence", None),
                    now=now,
                )
                phone_to_store = (
                    existing_phone if existing_phone_blocks_update else incoming_phone
                )

                connection.execute(
                    """
                    UPDATE saved_leads
                    SET phone = ?,
                        phone_type = ?,
                        phone_country = ?,
                        phone_carrier = ?,
                        phone_region = ?,
                        phone_validation_status = ?,
                        phone_confidence = ?,
                        phone_source = ?,
                        phone_source_url = ?,
                        phone_verified_by_json = ?,
                        phone_alternatives_json = ?,
                        enriched_at = ?
                    WHERE table_id = ? AND lead_key = ?
                    """,
                    (
                        phone_to_store,
                        getattr(update, "phone_type", None),
                        getattr(update, "country", None),
                        getattr(update, "carrier", None),
                        getattr(update, "region", None),
                        getattr(update, "validation_status", None),
                        getattr(update, "confidence", None),
                        getattr(update, "source", None),
                        getattr(update, "source_url", None),
                        json.dumps(verified_by, ensure_ascii=False),
                        json.dumps(alternatives, ensure_ascii=False),
                        now,
                        table_id,
                        lead_key,
                    ),
                )
            if counters["enriched"] > 0:
                connection.execute(
                    """
                    UPDATE saved_lead_tables
                    SET enrichment_status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (ENRICHMENT_ENRICHED, now, table_id),
                )
        return counters

    def apply_linkedin_profile_validation_updates(
        self,
        table_id: str,
        updates: list[tuple[Lead, Any]],
    ) -> dict[str, int]:
        """Persist LinkedIn profile validation results for saved leads.

        The profile validator writes to LinkedIn-specific columns and uses the
        current Experience title as the lead's canonical title. It still never
        overwrites the original company, API/internal e-mail, or existing phone.
        Contact e-mails and phones are also mirrored into the existing
        alternatives trails so the UI can show every source that disagrees with
        the current primary value.
        """
        counters = {
            "validated": 0,
            "failed": 0,
            "no_linkedin_url": 0,
            "no_change": 0,
        }
        if not updates:
            return counters

        now = _now_iso()
        updated = 0
        with self._lock, self._connect() as connection:
            self._assert_table_exists(connection, table_id)
            for lead, update in updates:
                lead_key = _lead_key_for(lead, fallback_index=0)
                existing = connection.execute(
                    """
                    SELECT email, phone, consultation_note,
                           title, linkedin_experience_title,
                           email_verified_by_json, email_alternatives_json,
                           phone_verified_by_json, phone_alternatives_json
                    FROM saved_leads
                    WHERE table_id = ? AND lead_key = ?
                    """,
                    (table_id, lead_key),
                ).fetchone()
                if existing is None:
                    counters["no_change"] += 1
                    continue

                status_value = str(getattr(update, "status", None) or "no_change")
                if status_value == "validated":
                    counters["validated"] += 1
                elif status_value == "failed":
                    counters["failed"] += 1
                elif status_value == "no_linkedin_url":
                    counters["no_linkedin_url"] += 1
                else:
                    counters["no_change"] += 1

                contact_email = _clean_optional(getattr(update, "contact_email", None))
                contact_phone = _clean_optional(getattr(update, "contact_phone", None))
                experience_title = _clean_optional(getattr(update, "experience_title", None))
                if _is_linkedin_navigation_title(experience_title):
                    experience_title = None
                existing_title = _clean_optional(existing["title"])
                existing_linkedin_title = _clean_optional(
                    existing["linkedin_experience_title"]
                )
                title_to_store = experience_title or (
                    None
                    if _is_linkedin_navigation_title(existing_title)
                    else existing_title
                )
                linkedin_title_to_store = experience_title or (
                    None
                    if _is_linkedin_navigation_title(existing_linkedin_title)
                    else existing_linkedin_title
                )
                experience_company = _clean_optional(getattr(update, "experience_company", None))
                experience_start_year = _clean_optional_int(
                    getattr(update, "experience_start_year", None)
                )
                experience_end_year = _clean_optional_int(
                    getattr(update, "experience_end_year", None)
                )
                linkedin_location = _clean_optional(getattr(update, "location", None))
                linkedin_education = getattr(update, "education", None) or []
                linkedin_education_json = (
                    json.dumps(linkedin_education, ensure_ascii=False)
                    if linkedin_education
                    else None
                )
                linkedin_birthday = _clean_optional(getattr(update, "birthday", None))
                verified_by, email_alternatives = _merge_linkedin_contact_email(
                    existing_email=existing["email"],
                    existing_verified_by=_json_list(
                        existing["email_verified_by_json"]
                        if "email_verified_by_json" in existing.keys()
                        else None
                    ),
                    existing_alternatives=_json_dict_list(
                        existing["email_alternatives_json"]
                        if "email_alternatives_json" in existing.keys()
                        else None
                    ),
                    incoming_email=contact_email,
                    now=now,
                )
                phone_verified_by, phone_alternatives = _merge_phone_verification(
                    existing_phone=existing["phone"],
                    existing_verified_by=_json_list(
                        existing["phone_verified_by_json"]
                        if "phone_verified_by_json" in existing.keys()
                        else None
                    ),
                    existing_alternatives=_json_dict_list(
                        existing["phone_alternatives_json"]
                        if "phone_alternatives_json" in existing.keys()
                        else None
                    ),
                    incoming_phone=contact_phone,
                    incoming_source="linkedin_contact",
                    incoming_confidence=90 if contact_phone else None,
                    now=now,
                )
                note = _append_note(
                    existing["consultation_note"],
                    _linkedin_validation_note(update),
                )
                cursor = connection.execute(
                    """
                    UPDATE saved_leads
                    SET linkedin_profile_validation_status = ?,
                        title = ?,
                        linkedin_experience_title = ?,
                        linkedin_experience_company = ?,
                        linkedin_experience_start_year = ?,
                        linkedin_experience_end_year = ?,
                        linkedin_experience_checked_at = ?,
                        linkedin_contact_email = ?,
                        linkedin_contact_website = ?,
                        linkedin_contact_phone = ?,
                        linkedin_location = COALESCE(?, linkedin_location),
                        linkedin_education_json = COALESCE(?, linkedin_education_json),
                        linkedin_birthday = COALESCE(?, linkedin_birthday),
                        phone = COALESCE(phone, ?),
                        consultation_note = ?,
                        email_verified_by_json = ?,
                        email_alternatives_json = ?,
                        phone_verified_by_json = ?,
                        phone_alternatives_json = ?,
                        linkedin_profile_validation_payload_json = ?
                    WHERE table_id = ? AND lead_key = ?
                    """,
                    (
                        status_value,
                        title_to_store,
                        linkedin_title_to_store,
                        experience_company,
                        experience_start_year,
                        experience_end_year,
                        now,
                        contact_email,
                        _clean_optional(getattr(update, "contact_website", None)),
                        contact_phone,
                        linkedin_location,
                        linkedin_education_json,
                        linkedin_birthday,
                        contact_phone,
                        note,
                        json.dumps(verified_by, ensure_ascii=False),
                        json.dumps(email_alternatives, ensure_ascii=False),
                        json.dumps(phone_verified_by, ensure_ascii=False),
                        json.dumps(phone_alternatives, ensure_ascii=False),
                        json.dumps(
                            {
                                "source": "linkedin_profile_validation",
                                "status": status_value,
                                "experience_start_year": experience_start_year,
                                "experience_end_year": experience_end_year,
                                "location": linkedin_location,
                                "education": linkedin_education,
                                "birthday": linkedin_birthday,
                                "raw": getattr(update, "raw", None) or {},
                                "error": getattr(update, "error", None),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        table_id,
                        lead_key,
                    ),
                )
                if cursor.rowcount > 0:
                    updated += 1
            if updated:
                connection.execute(
                    """
                    UPDATE saved_lead_tables
                    SET enrichment_status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (ENRICHMENT_ENRICHED, now, table_id),
                )
        return counters

    # ---- telegram consult --------------------------------------------------

    def save_telegram_consult(
        self,
        *,
        table_id: str,
        lead_ref: str,
        provider: str,
        lead_name: str,
        query: str,
        raw_text: str | None,
        source_url: str | None,
        downloaded_at: str | None,
        error: str | None,
        extracted_nome: str | None = None,
        extracted_cpf: str | None = None,
        extracted_birth_date: str | None = None,
        extracted_address: str | None = None,
        extracted_candidates: list[dict[str, Any]] | None = None,
        match_score: int | None = None,
        match_details: dict[str, Any] | None = None,
        run_id: str | None = None,
        query_type: str = "name",
        query_value: str | None = None,
        blocked_reason: str | None = None,
    ) -> TelegramConsult:
        """Upsert one Telegram-group consult row scoped by provider.

        UNIQUE(table_id, lead_ref, provider, query_type) means the same
        lead can carry both a Gon row and a Unix row, while a re-run for
        the same (lead, provider, query_type) replaces the prior result
        instead of accumulating duplicates. The default ``query_type``
        is ``"name"`` so legacy callers (the existing /telegram-consult
        endpoint) keep their previous semantics unchanged.
        """
        now = datetime.now(timezone.utc).isoformat()
        candidates_json = (
            json.dumps(extracted_candidates, ensure_ascii=False)
            if extracted_candidates
            else None
        )
        details_json = (
            json.dumps(match_details, ensure_ascii=False) if match_details else None
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tabela_telegram (
                    table_id, lead_ref, provider, lead_name, query, raw_text,
                    source_url, downloaded_at, error,
                    extracted_nome, extracted_cpf, extracted_birth_date,
                    extracted_address, extracted_candidates_json,
                    match_score, match_details_json,
                    run_id, query_type, query_value, blocked_reason,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(table_id, lead_ref, provider, query_type) DO UPDATE SET
                    lead_name = excluded.lead_name,
                    query = excluded.query,
                    raw_text = excluded.raw_text,
                    source_url = excluded.source_url,
                    downloaded_at = excluded.downloaded_at,
                    error = excluded.error,
                    extracted_nome = excluded.extracted_nome,
                    extracted_cpf = excluded.extracted_cpf,
                    extracted_birth_date = excluded.extracted_birth_date,
                    extracted_address = excluded.extracted_address,
                    extracted_candidates_json = excluded.extracted_candidates_json,
                    match_score = excluded.match_score,
                    match_details_json = excluded.match_details_json,
                    run_id = excluded.run_id,
                    query_value = excluded.query_value,
                    blocked_reason = excluded.blocked_reason,
                    created_at = excluded.created_at
                """,
                (
                    table_id,
                    lead_ref,
                    provider,
                    lead_name,
                    query,
                    raw_text,
                    source_url,
                    downloaded_at,
                    error,
                    extracted_nome,
                    extracted_cpf,
                    extracted_birth_date,
                    extracted_address,
                    candidates_json,
                    match_score,
                    details_json,
                    run_id,
                    query_type,
                    query_value,
                    blocked_reason,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM tabela_telegram"
                " WHERE table_id = ? AND lead_ref = ? AND provider = ?"
                "   AND query_type = ?",
                (table_id, lead_ref, provider, query_type),
            ).fetchone()
        return _row_to_telegram_consult(row)

    # ---- telegram pipeline state ------------------------------------------

    def list_telegram_consults_for_lead(
        self,
        table_id: str,
        lead_ref: str,
        *,
        query_type: str | None = None,
    ) -> list[TelegramConsult]:
        """Fetch persisted consult rows for one (table, lead) pair.

        Used by the phone follow-up step in
        :mod:`telegram_pipeline` to pick CPFs that were ranked above the
        follow-up threshold by an earlier name-stage run.
        """
        sql = (
            "SELECT * FROM tabela_telegram"
            " WHERE table_id = ? AND lead_ref = ?"
        )
        params: list[Any] = [table_id, lead_ref]
        if query_type is not None:
            sql += " AND query_type = ?"
            params.append(query_type)
        sql += " ORDER BY created_at DESC, id DESC"
        with self._lock, self._connect() as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
        return [_row_to_telegram_consult(r) for r in rows]

    def get_telegram_provider_state(
        self, provider: str
    ) -> TelegramProviderStateRow | None:
        """Return the persisted runtime state for one provider, or
        ``None`` when the provider has never been observed."""
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT provider, cooldown_until, last_error, updated_at"
                " FROM telegram_provider_state WHERE provider = ?",
                (provider,),
            ).fetchone()
        if row is None:
            return None
        return TelegramProviderStateRow(
            provider=row["provider"],
            cooldown_until=row["cooldown_until"],
            last_error=row["last_error"],
            updated_at=row["updated_at"],
        )

    def set_telegram_provider_cooldown(
        self,
        provider: str,
        *,
        cooldown_until: str,
        last_error: str,
    ) -> TelegramProviderStateRow:
        """Upsert the cooldown window for one provider. Called when the
        workflow detects a rate-limit signal (e.g. Gon's "uso excessivo")
        so subsequent runs skip the provider until ``cooldown_until``."""
        now = _now_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO telegram_provider_state (
                    provider, cooldown_until, last_error, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    cooldown_until = excluded.cooldown_until,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (provider, cooldown_until, last_error, now),
            )
        return TelegramProviderStateRow(
            provider=provider,
            cooldown_until=cooldown_until,
            last_error=last_error,
            updated_at=now,
        )

    def clear_telegram_provider_cooldown(self, provider: str) -> None:
        """Remove the cooldown window for one provider, e.g. after the
        operator's manual reset or when the workflow proves the bot
        recovered (first successful consult clears the window).
        """
        now = _now_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO telegram_provider_state (
                    provider, cooldown_until, last_error, updated_at
                ) VALUES (?, NULL, NULL, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    cooldown_until = NULL,
                    last_error = NULL,
                    updated_at = excluded.updated_at
                """,
                (provider, now),
            )

    def list_telegram_consults(self, table_id: str) -> list[TelegramConsult]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tabela_telegram WHERE table_id = ?"
                # Gon first, then Unix — matches the UI's expected order.
                " ORDER BY"
                "   CASE provider WHEN 'gon' THEN 0 WHEN 'unix' THEN 1 ELSE 2 END,"
                "   created_at DESC, id DESC",
                (table_id,),
            ).fetchall()
        return [_row_to_telegram_consult(r) for r in rows]

    # ---- internals ---------------------------------------------------------

    def _ensure_schema(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS saved_lead_tables (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    keywords_json TEXT NOT NULL,
                    search_queries_json TEXT NOT NULL,
                    search_request_json TEXT NOT NULL,
                    enrichment_status TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS saved_leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    table_id TEXT NOT NULL REFERENCES saved_lead_tables(id) ON DELETE CASCADE,
                    lead_key TEXT NOT NULL,
                    company_name TEXT NOT NULL,
                    company_domain TEXT,
                    person_name TEXT,
                    title TEXT,
                    linkedin_url TEXT,
                    email TEXT,
                    phone TEXT,
                    source_url TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    snippet TEXT NOT NULL DEFAULT '',
                    matched_title TEXT,
                    validation_status TEXT NOT NULL DEFAULT 'valid',
                    validation_note TEXT,
                    confidence_score INTEGER NOT NULL,
                    previously_consulted_at TEXT,
                    consultation_note TEXT,
                    raw_json TEXT,
                    enrichment_payload_json TEXT,
                    enriched_at TEXT,
                    UNIQUE(table_id, lead_key)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_saved_leads_table ON saved_leads(table_id)"
            )
            _ensure_column(connection, "saved_leads", "phone", "TEXT")
            # Per-lead enrichment metadata — additive columns so existing
            # databases keep working after the schema bump.
            _ensure_column(connection, "saved_leads", "enrichment_source", "TEXT")
            _ensure_column(connection, "saved_leads", "enrichment_status", "TEXT")
            _ensure_column(
                connection, "saved_leads", "enrichment_confidence", "INTEGER"
            )
            _ensure_column(connection, "saved_leads", "email_type", "TEXT")
            _ensure_column(
                connection, "saved_leads", "email_validation_status", "TEXT"
            )
            # Cross-provider verification trail. Stored as JSON because
            # both fields are unbounded lists of small dicts — keeping
            # them in a single column avoids a third table and keeps the
            # serialization symmetric with the model.
            _ensure_column(
                connection, "saved_leads", "email_verified_by_json", "TEXT"
            )
            _ensure_column(
                connection, "saved_leads", "email_alternatives_json", "TEXT"
            )
            # Phone enrichment metadata — mirrors the email trail. All
            # additive so existing rows keep working; defaults are NULL.
            _ensure_column(connection, "saved_leads", "phone_type", "TEXT")
            _ensure_column(connection, "saved_leads", "phone_country", "TEXT")
            _ensure_column(connection, "saved_leads", "phone_carrier", "TEXT")
            _ensure_column(connection, "saved_leads", "phone_region", "TEXT")
            _ensure_column(
                connection, "saved_leads", "phone_validation_status", "TEXT"
            )
            _ensure_column(
                connection, "saved_leads", "phone_confidence", "INTEGER"
            )
            _ensure_column(connection, "saved_leads", "phone_source", "TEXT")
            _ensure_column(connection, "saved_leads", "phone_source_url", "TEXT")
            _ensure_column(
                connection, "saved_leads", "phone_verified_by_json", "TEXT"
            )
            _ensure_column(
                connection, "saved_leads", "phone_alternatives_json", "TEXT"
            )
            # LinkedIn profile validation metadata. These are separate from
            # the primary title/company/contact columns so the app can show
            # differences without destroying data collected by APIs/search.
            _ensure_column(
                connection,
                "saved_leads",
                "linkedin_profile_validation_status",
                "TEXT",
            )
            _ensure_column(connection, "saved_leads", "linkedin_experience_title", "TEXT")
            _ensure_column(
                connection, "saved_leads", "linkedin_experience_company", "TEXT"
            )
            _ensure_column(
                connection, "saved_leads", "linkedin_experience_start_year", "INTEGER"
            )
            _ensure_column(
                connection, "saved_leads", "linkedin_experience_end_year", "INTEGER"
            )
            _ensure_column(
                connection, "saved_leads", "linkedin_experience_checked_at", "TEXT"
            )
            _ensure_column(connection, "saved_leads", "linkedin_contact_email", "TEXT")
            _ensure_column(connection, "saved_leads", "linkedin_contact_website", "TEXT")
            _ensure_column(connection, "saved_leads", "linkedin_contact_phone", "TEXT")
            # LinkedIn signals used by the Telegram-consult matcher.
            # ``linkedin_education_json`` carries a list of education
            # entries (institution, degree, start_year, end_year, ...).
            _ensure_column(connection, "saved_leads", "linkedin_location", "TEXT")
            _ensure_column(
                connection, "saved_leads", "linkedin_education_json", "TEXT"
            )
            _ensure_column(connection, "saved_leads", "linkedin_birthday", "TEXT")
            _ensure_column(
                connection,
                "saved_leads",
                "linkedin_profile_validation_payload_json",
                "TEXT",
            )
            # Residential address from a Telegram CPF (SISREG-III) consult.
            # Written only by the phone stage, never overwritten — see
            # ``apply_contact_address_updates``.
            _ensure_column(connection, "saved_leads", "endereco", "TEXT")
            # Telegram-group consult results, one row per
            # (table_id, lead_ref, provider). Two providers exist today:
            # "gon" (ConsultoriaGonzalesbot, replies in-group with
            # "ver resultado completo" → HTML scrape) and "unix"
            # (Unix Mk via @UnixGruposRobot DM → RESULTADO Web →
            # "Texto" file download). The UNIQUE constraint lets each
            # lead carry both, and a re-run UPDATES instead of stacking.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tabela_telegram (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    table_id TEXT NOT NULL REFERENCES saved_lead_tables(id) ON DELETE CASCADE,
                    lead_ref TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT 'unix',
                    lead_name TEXT NOT NULL,
                    query TEXT NOT NULL,
                    raw_text TEXT,
                    source_url TEXT,
                    downloaded_at TEXT,
                    error TEXT,
                    extracted_nome TEXT,
                    extracted_cpf TEXT,
                    extracted_birth_date TEXT,
                    extracted_address TEXT,
                    extracted_candidates_json TEXT,
                    match_score INTEGER,
                    match_details_json TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(table_id, lead_ref, provider)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tabela_telegram_table "
                "ON tabela_telegram(table_id)"
            )
            # Best-effort migration from the v1 schema (consulta_telegram,
            # single row per lead). Old rows get provider='unix' so the
            # operator's prior Unix runs stay visible after upgrade.
            if _table_exists(connection, "consulta_telegram"):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO tabela_telegram (
                        table_id, lead_ref, provider, lead_name, query,
                        raw_text, source_url, downloaded_at, error, created_at
                    )
                    SELECT
                        table_id, lead_ref, 'unix' AS provider, lead_name, query,
                        raw_text, source_url, downloaded_at, error, created_at
                    FROM consulta_telegram
                    """
                )
                connection.execute("DROP TABLE consulta_telegram")
            # v2 schema bump for the in-process Telegram pipeline. The
            # name-stage row is no longer the only consult shape — the
            # follow-up CPF/phone queries persist alongside it, so the
            # UNIQUE constraint must include ``query_type``. Old
            # databases were created with the narrower UNIQUE, so we
            # rebuild the table when ``query_type`` is missing.
            _migrate_tabela_telegram_v2(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_provider_state (
                    provider TEXT PRIMARY KEY,
                    cooldown_until TEXT,
                    last_error TEXT,
                    updated_at TEXT
                )
                """
            )

    def _touch_table(self, table_id: str) -> None:
        with self._lock, self._connect() as connection:
            self._assert_table_exists(connection, table_id)
            connection.execute(
                "UPDATE saved_lead_tables SET updated_at = ? WHERE id = ?",
                (_now_iso(), table_id),
            )

    @staticmethod
    def _assert_table_exists(connection: sqlite3.Connection, table_id: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM saved_lead_tables WHERE id = ?", (table_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"saved lead table not found: {table_id}")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()


# ---- helpers ---------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_column(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
    definition: str,
) -> None:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    existing = {str(row["name"]) for row in rows}
    if column_name not in existing:
        connection.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
        )


def _migrate_tabela_telegram_v2(connection: sqlite3.Connection) -> None:
    """Bump ``tabela_telegram`` to the pipeline v2 schema.

    Adds ``run_id``/``query_type``/``query_value``/``blocked_reason``
    and widens the UNIQUE constraint to include ``query_type`` so the
    same (lead, provider) can carry a name-stage row and follow-up CPF
    or phone rows side by side. SQLite cannot widen a UNIQUE in place,
    so the migration rebuilds the table when the new column is missing
    — a no-op on fresh installs and on already-migrated databases.

    Existing rows are preserved verbatim and inherit ``query_type='name'``
    (the historical semantics of the table) so the legacy UI keeps
    rendering them exactly as before.
    """
    rows = connection.execute("PRAGMA table_info(tabela_telegram)").fetchall()
    if not rows:
        return
    existing = {str(r["name"]) for r in rows}
    if "query_type" in existing:
        return
    connection.execute(
        """
        CREATE TABLE tabela_telegram_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_id TEXT NOT NULL REFERENCES saved_lead_tables(id) ON DELETE CASCADE,
            lead_ref TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT 'unix',
            lead_name TEXT NOT NULL,
            query TEXT NOT NULL,
            raw_text TEXT,
            source_url TEXT,
            downloaded_at TEXT,
            error TEXT,
            extracted_nome TEXT,
            extracted_cpf TEXT,
            extracted_birth_date TEXT,
            extracted_address TEXT,
            extracted_candidates_json TEXT,
            match_score INTEGER,
            match_details_json TEXT,
            run_id TEXT,
            query_type TEXT NOT NULL DEFAULT 'name',
            query_value TEXT,
            blocked_reason TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(table_id, lead_ref, provider, query_type)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO tabela_telegram_v2 (
            id, table_id, lead_ref, provider, lead_name, query, raw_text,
            source_url, downloaded_at, error,
            extracted_nome, extracted_cpf, extracted_birth_date, extracted_address,
            extracted_candidates_json, match_score, match_details_json,
            query_type, created_at
        )
        SELECT id, table_id, lead_ref, provider, lead_name, query, raw_text,
               source_url, downloaded_at, error,
               extracted_nome, extracted_cpf, extracted_birth_date, extracted_address,
               extracted_candidates_json, match_score, match_details_json,
               'name', created_at
        FROM tabela_telegram
        """
    )
    connection.execute("DROP TABLE tabela_telegram")
    connection.execute("ALTER TABLE tabela_telegram_v2 RENAME TO tabela_telegram")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_tabela_telegram_table "
        "ON tabela_telegram(table_id)"
    )


def _append_note(existing: str | None, note: str) -> str:
    if not existing:
        return note
    if note in existing:
        return existing
    return f"{existing} {note}".strip()


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_optional_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if 1900 <= number <= 2100:
        return number
    return None


def _is_linkedin_navigation_title(value: Any) -> bool:
    cleaned = _clean_optional(value)
    if not cleaned:
        return False
    normalized = unicodedata.normalize("NFD", cleaned)
    ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    words = re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).strip()
    return words == "skip to main content" or (
        words.startswith("pular para conte") and words.endswith("principal")
    )


def _is_document_like_phone(value: Any) -> bool:
    """Detect document numbers previously mis-persisted as phone values.

    CNS is 15 bare digits. A real international number can also have 15
    digits, but in this app values from phone enrichment are stored with
    ``+`` when they are E.164-like; a bare 15-digit value from Gonzales is
    overwhelmingly likely to be CNS/document data.
    """
    text = _clean_optional(value) or ""
    digits = _digits_only(text)
    return len(digits) == 15 and not text.startswith("+")


def _linkedin_validation_note(update: Any) -> str:
    status = str(getattr(update, "status", None) or "no_change")
    if status == "validated":
        title = _clean_optional(getattr(update, "experience_title", None))
        company = _clean_optional(getattr(update, "experience_company", None))
        if title and company:
            return f"Validação LinkedIn: experiência atual '{title}' em '{company}'."
        return "Validação LinkedIn: perfil consultado."
    if status == "no_linkedin_url":
        return "Validação LinkedIn: lead sem URL de perfil."
    if status == "failed":
        error = _clean_optional(getattr(update, "error", None))
        return f"Validação LinkedIn falhou{': ' + error if error else ''}."
    return "Validação LinkedIn: nenhum dado novo encontrado."


def _clean_name(value: str) -> str:
    cleaned = " ".join((value or "").strip().split())
    if not cleaned:
        raise ValueError("table name is required")
    return cleaned


def _slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()
    return slug or "saved-leads"


def _sanitize_search_request(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    sanitized: dict[str, Any] = {}
    for key, value in payload.items():
        if key in _FORBIDDEN_REQUEST_FIELDS:
            continue
        if isinstance(value, dict):
            sanitized[key] = _sanitize_search_request(value)
        else:
            sanitized[key] = value
    return sanitized


def _dedupe_preserving_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in items:
        if raw is None:
            continue
        item = str(raw).strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _lead_key_for(lead: Lead, *, fallback_index: int) -> str:
    key = lead_dedupe_key(lead)
    if key is not None:
        key_type, key_value = key
        return f"{key_type}:{key_value}"
    return f"row:{fallback_index}:{lead.source_url}"


def _lead_row_values(table_id: str, lead_key: str, lead: Lead) -> tuple[Any, ...]:
    return (
        table_id,
        lead_key,
        lead.company_name,
        lead.company_domain,
        lead.person_name,
        lead.title,
        lead.linkedin_url,
        lead.email,
        lead.phone,
        lead.linkedin_profile_validation_status,
        lead.linkedin_experience_title,
        lead.linkedin_experience_company,
        lead.linkedin_experience_start_year,
        lead.linkedin_experience_end_year,
        lead.linkedin_experience_checked_at,
        lead.linkedin_contact_email,
        lead.linkedin_contact_website,
        lead.linkedin_contact_phone,
        lead.linkedin_location,
        json.dumps(lead.linkedin_education, ensure_ascii=False)
        if lead.linkedin_education
        else None,
        lead.linkedin_birthday,
        lead.source_url,
        lead.source_type,
        lead.snippet or "",
        lead.matched_title,
        lead.validation_status,
        lead.validation_note,
        int(lead.confidence_score),
        lead.previously_consulted_at,
        lead.consultation_note,
        json.dumps(lead.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
    )


def export_saved_leads_csv(leads: list[Lead], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(SAVED_LEADS_EXPORT_COLUMNS)
        for lead in leads:
            writer.writerow(_saved_lead_export_row(lead))
    return path


def _saved_lead_export_row(lead: Lead) -> list[str]:
    full_name = lead.person_name or ""
    return [
        lead.company_name,
        _first_name(full_name),
        full_name,
        lead.linkedin_url or "",
        lead.title or "",
        lead.email or "",
        "",
        "",
        "",
        "",
        lead.phone or "",
    ]


def _first_name(full_name: str) -> str:
    parts = [part for part in full_name.strip().split() if part]
    return parts[0] if parts else ""


def _row_to_table(row: sqlite3.Row) -> SavedLeadTable:
    return SavedLeadTable(
        id=row["id"],
        name=row["name"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        source_type=row["source_type"],
        keywords=_json_list(row["keywords_json"]),
        search_queries=_json_list(row["search_queries_json"]),
        search_request=_json_dict(row["search_request_json"]),
        enrichment_status=row["enrichment_status"],
        lead_count=int(row["lead_count"]) if "lead_count" in row.keys() else 0,
    )


def _row_to_telegram_consult(row: sqlite3.Row) -> TelegramConsult:
    keys = set(row.keys())
    candidates_raw = row["extracted_candidates_json"] if "extracted_candidates_json" in keys else None
    details_raw = row["match_details_json"] if "match_details_json" in keys else None
    try:
        candidates = json.loads(candidates_raw) if candidates_raw else []
    except Exception:
        candidates = []
    try:
        details = json.loads(details_raw) if details_raw else {}
    except Exception:
        details = {}
    return TelegramConsult(
        id=int(row["id"]),
        table_id=row["table_id"],
        lead_ref=row["lead_ref"],
        provider=row["provider"] if "provider" in keys else "unix",
        lead_name=row["lead_name"],
        query=row["query"],
        raw_text=row["raw_text"],
        source_url=row["source_url"],
        downloaded_at=row["downloaded_at"],
        error=row["error"],
        extracted_nome=row["extracted_nome"] if "extracted_nome" in keys else None,
        extracted_cpf=row["extracted_cpf"] if "extracted_cpf" in keys else None,
        extracted_birth_date=(
            row["extracted_birth_date"] if "extracted_birth_date" in keys else None
        ),
        extracted_address=row["extracted_address"] if "extracted_address" in keys else None,
        extracted_candidates=candidates if isinstance(candidates, list) else [],
        match_score=row["match_score"] if "match_score" in keys else None,
        match_details=details if isinstance(details, dict) else {},
        created_at=row["created_at"],
        run_id=row["run_id"] if "run_id" in keys else None,
        query_type=row["query_type"] if "query_type" in keys else "name",
        query_value=row["query_value"] if "query_value" in keys else None,
        blocked_reason=row["blocked_reason"] if "blocked_reason" in keys else None,
    )


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    """Lightweight check used by the schema migration to detect the v1
    ``consulta_telegram`` table and decide whether to copy its rows
    into ``tabela_telegram`` before dropping it."""
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _row_to_lead(row: sqlite3.Row) -> Lead:
    return Lead(
        company_name=row["company_name"],
        company_domain=row["company_domain"],
        person_name=row["person_name"],
        title=row["title"],
        linkedin_url=row["linkedin_url"],
        email=row["email"],
        phone=row["phone"] if "phone" in row.keys() else None,
        source_url=row["source_url"],
        source_type=row["source_type"],
        snippet=row["snippet"] or "",
        matched_title=row["matched_title"],
        validation_status=row["validation_status"] or "valid",
        validation_note=row["validation_note"],
        confidence_score=int(row["confidence_score"]),
        previously_consulted_at=row["previously_consulted_at"],
        consultation_note=row["consultation_note"],
        enrichment_source=_row_get(row, "enrichment_source"),
        enrichment_status=_row_get(row, "enrichment_status"),
        enrichment_confidence=_row_get_int(row, "enrichment_confidence"),
        email_type=_row_get(row, "email_type"),
        email_validation_status=_row_get(row, "email_validation_status"),
        enriched_at=_row_get(row, "enriched_at"),
        email_verified_by=_json_list(_row_get(row, "email_verified_by_json")),
        email_alternatives=_json_dict_list(_row_get(row, "email_alternatives_json")),
        phone_type=_row_get(row, "phone_type"),
        phone_country=_row_get(row, "phone_country"),
        phone_carrier=_row_get(row, "phone_carrier"),
        phone_region=_row_get(row, "phone_region"),
        phone_validation_status=_row_get(row, "phone_validation_status"),
        phone_confidence=_row_get_int(row, "phone_confidence"),
        phone_source=_row_get(row, "phone_source"),
        phone_source_url=_row_get(row, "phone_source_url"),
        phone_verified_by=_json_list(_row_get(row, "phone_verified_by_json")),
        phone_alternatives=_json_dict_list(_row_get(row, "phone_alternatives_json")),
        linkedin_profile_validation_status=_row_get(
            row, "linkedin_profile_validation_status"
        ),
        linkedin_experience_title=_row_get(row, "linkedin_experience_title"),
        linkedin_experience_company=_row_get(row, "linkedin_experience_company"),
        linkedin_experience_start_year=_row_get_int(
            row, "linkedin_experience_start_year"
        ),
        linkedin_experience_end_year=_row_get_int(
            row, "linkedin_experience_end_year"
        ),
        linkedin_experience_checked_at=_row_get(row, "linkedin_experience_checked_at"),
        linkedin_contact_email=_row_get(row, "linkedin_contact_email"),
        linkedin_contact_website=_row_get(row, "linkedin_contact_website"),
        linkedin_contact_phone=_row_get(row, "linkedin_contact_phone"),
        linkedin_location=_row_get(row, "linkedin_location"),
        linkedin_education=_json_dict_list(_row_get(row, "linkedin_education_json")),
        linkedin_birthday=_row_get(row, "linkedin_birthday"),
        endereco=_row_get(row, "endereco"),
    )


def _merge_email_verification(
    *,
    existing_email: str | None,
    existing_verified_by: list[str],
    existing_alternatives: list[dict[str, Any]],
    incoming_email: str | None,
    incoming_source: str,
    incoming_confidence: int | None,
    now: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Cross-provider merge for the primary email + its trail.

    The contract the user described:
    - Same incoming email as the one already saved → add the incoming
      source to ``email_verified_by`` (drives the "Verificado" badge).
    - Different incoming email → push it onto ``email_alternatives``
      so the UI can show "Apollo sugeriu X" without losing the trail.
    - The primary ``email`` is never overwritten here — caller keeps
      doing ``COALESCE(email, incoming)``. This helper only manages
      the verification trail.

    Returns ``(new_verified_by, new_alternatives)``.
    """
    verified_by = list(existing_verified_by)
    alternatives = list(existing_alternatives)

    incoming_norm = (incoming_email or "").strip().lower()
    existing_norm = (existing_email or "").strip().lower()
    source = (incoming_source or "").strip().lower() or "unknown"

    if not incoming_norm:
        return verified_by, alternatives

    if not existing_norm:
        # First write: incoming becomes primary; seed the trail.
        if source not in verified_by:
            verified_by.append(source)
        return verified_by, alternatives

    if incoming_norm == existing_norm:
        if source not in verified_by:
            verified_by.append(source)
        return verified_by, alternatives

    # Diverges from primary — record as alternative, deduped by (email,
    # source). Same source proposing the same alternative twice is a
    # no-op; different sources land separately so the UI can show "two
    # paid APIs disagree with the primary".
    already_recorded = any(
        (alt.get("email") or "").strip().lower() == incoming_norm
        and (alt.get("source") or "").strip().lower() == source
        for alt in alternatives
    )
    if not already_recorded:
        alternatives.append(
            {
                "email": incoming_email.strip(),
                "source": source,
                "confidence": incoming_confidence,
                "found_at": now,
            }
        )
    return verified_by, alternatives


def _merge_linkedin_contact_email(
    *,
    existing_email: str | None,
    existing_verified_by: list[str],
    existing_alternatives: list[dict[str, Any]],
    incoming_email: str | None,
    now: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Merge LinkedIn Contact Info e-mail without making it primary.

    Contact Info can contain personal e-mails. We keep the explicit
    ``linkedin_contact_email`` column and mirror it into the trail, but we do
    not overwrite ``email``. If it matches the primary, it verifies the
    primary; otherwise it becomes an alternative suggestion.
    """
    verified_by = list(existing_verified_by)
    alternatives = list(existing_alternatives)
    incoming_norm = (incoming_email or "").strip().lower()
    existing_norm = (existing_email or "").strip().lower()
    source = "linkedin_contact"
    if not incoming_norm:
        return verified_by, alternatives
    if existing_norm and incoming_norm == existing_norm:
        if source not in verified_by:
            verified_by.append(source)
        return verified_by, alternatives
    already_recorded = any(
        (alt.get("email") or "").strip().lower() == incoming_norm
        and (alt.get("source") or "").strip().lower() == source
        for alt in alternatives
    )
    if not already_recorded:
        alternatives.append(
            {
                "email": incoming_email.strip(),
                "source": source,
                "confidence": 90,
                "found_at": now,
            }
        )
    return verified_by, alternatives


def _merge_phone_verification(
    *,
    existing_phone: str | None,
    existing_verified_by: list[str],
    existing_alternatives: list[dict[str, Any]],
    incoming_phone: str | None,
    incoming_source: str,
    incoming_confidence: int | None,
    now: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Cross-provider merge for the primary phone + its trail.

    Mirrors :func:`_merge_email_verification`. Comparison is done on
    the **canonical E.164** form when possible (via :mod:`phonenumbers`),
    falling back to digit-only matching so ``+55 11 99999-9999`` and
    ``11999999999`` collide as the same number even when sources format
    differently.

    The primary ``phone`` is never overwritten here; callers keep doing
    ``COALESCE(phone, incoming)``. This helper only manages the
    verification trail (``phone_verified_by``) and the divergence list
    (``phone_alternatives``).
    """
    verified_by = list(existing_verified_by)
    alternatives = list(existing_alternatives)

    incoming_key = _phone_compare_key(incoming_phone)
    existing_key = _phone_compare_key(existing_phone)
    source = (incoming_source or "").strip().lower() or "unknown"

    if not incoming_key:
        return verified_by, alternatives

    if not existing_key:
        if source not in verified_by:
            verified_by.append(source)
        return verified_by, alternatives

    if incoming_key == existing_key:
        if source not in verified_by:
            verified_by.append(source)
        return verified_by, alternatives

    already_recorded = any(
        _phone_compare_key(str(alt.get("phone") or "")) == incoming_key
        and (alt.get("source") or "").strip().lower() == source
        for alt in alternatives
    )
    if not already_recorded:
        alternatives.append(
            {
                "phone": (incoming_phone or "").strip(),
                "source": source,
                "confidence": incoming_confidence,
                "found_at": now,
            }
        )
    return verified_by, alternatives


def _phone_compare_key(value: str | None) -> str:
    """Stable key for comparing two phones across formatting.

    Tries E.164 via ``phonenumbers`` with BR as the default region (the
    primary user base). If parsing fails for any reason, falls back to
    a digit-only comparison so the helper never raises on bad input.
    """
    cleaned = (value or "").strip()
    if not cleaned:
        return ""
    try:
        import phonenumbers

        try:
            parsed = phonenumbers.parse(cleaned, "BR")
        except phonenumbers.NumberParseException:
            parsed = None
        if parsed is not None and phonenumbers.is_possible_number(parsed):
            return phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.E164
            )
    except Exception:
        pass
    return _digits_only(cleaned)


def _digits_only(value: str) -> str:
    return "".join(ch for ch in value or "" if ch.isdigit())


def _json_dict_list(raw: str | None) -> list[dict[str, Any]]:
    """Parse a JSON column expected to be a list of dicts. Defensive
    against legacy ``null`` / malformed payloads so an old row never
    explodes on read."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _row_get(row: sqlite3.Row, column: str) -> str | None:
    if column not in row.keys():
        return None
    value = row[column]
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _row_get_int(row: sqlite3.Row, column: str) -> int | None:
    value = _row_get(row, column)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _json_dict(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalize_column(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    return re.sub(r"\s+", "_", text)


def _build_column_map(columns: Iterable[Any]) -> dict[str, list[str]]:
    """Map canonical Lead field -> ordered list of source column names that match."""
    mapping: dict[str, list[str]] = {}
    for original in columns:
        normalized = _normalize_column(original)
        if not normalized:
            continue
        canonical: str | None = None
        if normalized in _PERSON_ALIASES:
            canonical = "person_name"
        elif normalized in _COMPANY_ALIASES:
            canonical = "company_name"
        elif normalized in _TITLE_ALIASES:
            canonical = "title"
        elif normalized in _LINKEDIN_ALIASES:
            canonical = "linkedin_url"
        elif normalized in _EMAIL_ALIASES:
            canonical = "email"
        elif normalized in _PHONE_ALIASES:
            canonical = "phone"
        elif normalized in _DOMAIN_ALIASES:
            canonical = "company_domain"
        if canonical is not None:
            mapping.setdefault(canonical, []).append(str(original))
    return mapping


def _cell(row: Any, column: str | None) -> str | None:
    if column is None:
        return None
    try:
        value = row[column]
    except (KeyError, IndexError):
        return None
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    return text


def _first_cell(row: Any, columns: list[str]) -> str | None:
    for column in columns:
        value = _cell(row, column)
        if value:
            return value
    return None


def _dataframe_to_leads(df: Any, *, file_stem: str) -> list[Lead]:
    canonical_to_sources = _build_column_map(df.columns)

    missing = [
        required
        for required in ("person_name", "company_name", "title")
        if not canonical_to_sources.get(required)
    ]
    if missing:
        raise ImportColumnError(
            "imported file is missing required columns: " + ", ".join(missing)
        )

    leads: list[Lead] = []
    for index, row in df.iterrows():
        person = _first_cell(row, canonical_to_sources.get("person_name", []))
        company = _first_cell(row, canonical_to_sources.get("company_name", []))
        title = _first_cell(row, canonical_to_sources.get("title", []))
        if not (person and company and title):
            continue
        linkedin_url = _first_cell(row, canonical_to_sources.get("linkedin_url", []))
        email = _first_cell(row, canonical_to_sources.get("email", []))
        phone = _first_cell(row, canonical_to_sources.get("phone", []))
        domain = _first_cell(row, canonical_to_sources.get("company_domain", []))
        source_url = linkedin_url or f"import:{file_stem}:{index}"
        leads.append(
            Lead(
                company_name=company,
                company_domain=domain,
                person_name=person,
                title=title,
                linkedin_url=linkedin_url,
                email=email,
                phone=phone,
                source_url=source_url,
                source_type="imported_table",
                snippet="",
                matched_title=None,
                validation_status="valid",
                validation_note=None,
                confidence_score=70,
            )
        )
    return leads
