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
                        linkedin_url, email, phone, source_url, source_type, snippet,
                        matched_title, validation_status, validation_note,
                        confidence_score, previously_consulted_at, consultation_note,
                        raw_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(table_id, lead_key) DO UPDATE SET
                        company_name = excluded.company_name,
                        company_domain = excluded.company_domain,
                        person_name = excluded.person_name,
                        title = excluded.title,
                        linkedin_url = excluded.linkedin_url,
                        email = excluded.email,
                        phone = excluded.phone,
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
        with self._lock, self._connect() as connection:
            self._assert_table_exists(connection, table_id)
            rows = connection.execute(
                f"""
                SELECT {", ".join(OUTPUT_COLUMNS)}
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
                    SELECT email, phone, consultation_note
                    FROM saved_leads
                    WHERE table_id = ? AND lead_key = ?
                    """,
                    (table_id, lead_key),
                ).fetchone()
                if existing is None:
                    continue
                email = existing["email"] or update.email
                phone = existing["phone"] or update.phone
                note = _append_note(
                    existing["consultation_note"],
                    update.note or f"Enriquecido via {update.provider}.",
                )
                cursor = connection.execute(
                    """
                    UPDATE saved_leads
                    SET email = ?, phone = ?, consultation_note = ?,
                        enrichment_payload_json = ?, enriched_at = ?
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


def _append_note(existing: str | None, note: str) -> str:
    if not existing:
        return note
    if note in existing:
        return existing
    return f"{existing} {note}".strip()


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
    )


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
