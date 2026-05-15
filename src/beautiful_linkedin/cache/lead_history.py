from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from collections.abc import Iterator

from beautiful_linkedin.models import Lead
from beautiful_linkedin.processing.deduplicator import lead_dedupe_key


class LeadConsultationHistory:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def annotate_and_record(
        self,
        leads: list[Lead],
        *,
        now: datetime | None = None,
    ) -> list[Lead]:
        seen_at = _coerce_datetime(now)
        seen_at_iso = seen_at.isoformat(timespec="seconds")
        annotated: list[Lead] = []

        with self._lock, self._connect() as connection:
            for lead in leads:
                key = lead_dedupe_key(lead)
                if key is None:
                    annotated.append(lead)
                    continue

                key_type, key_value = key
                lead_key = _lead_history_key(key_type, key_value)
                row = connection.execute(
                    """
                    SELECT first_seen_at, seen_count
                    FROM lead_consultations
                    WHERE lead_key = ?
                    """,
                    (lead_key,),
                ).fetchone()

                stored_lead_json = json.dumps(
                    lead.model_dump(mode="json"),
                    sort_keys=True,
                    ensure_ascii=False,
                )
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO lead_consultations(
                            lead_key,
                            key_type,
                            key_value,
                            first_seen_at,
                            last_seen_at,
                            seen_count,
                            last_lead_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            lead_key,
                            key_type,
                            key_value,
                            seen_at_iso,
                            seen_at_iso,
                            1,
                            stored_lead_json,
                        ),
                    )
                    annotated.append(lead)
                    continue

                first_seen_at, seen_count = row
                annotated_lead = lead.model_copy(
                    update={
                        "previously_consulted_at": first_seen_at,
                        "consultation_note": _consultation_note(first_seen_at),
                    }
                )
                connection.execute(
                    """
                    UPDATE lead_consultations
                    SET last_seen_at = ?,
                        seen_count = ?,
                        last_lead_json = ?
                    WHERE lead_key = ?
                    """,
                    (
                        seen_at_iso,
                        int(seen_count) + 1,
                        stored_lead_json,
                        lead_key,
                    ),
                )
                annotated.append(annotated_lead)

        return annotated

    def _ensure_schema(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS lead_consultations (
                    lead_key TEXT PRIMARY KEY,
                    key_type TEXT NOT NULL,
                    key_value TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    seen_count INTEGER NOT NULL,
                    last_lead_json TEXT NOT NULL
                )
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()


def _coerce_datetime(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now().astimezone()
    if value.tzinfo is None:
        return value.astimezone()
    return value


def _consultation_note(first_seen_at: str) -> str:
    parsed = datetime.fromisoformat(first_seen_at)
    return f"Já consultado antes em {parsed:%d/%m/%Y %H:%M}"


def _lead_history_key(key_type: str, key_value: str) -> str:
    canonical = json.dumps([key_type, key_value], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
