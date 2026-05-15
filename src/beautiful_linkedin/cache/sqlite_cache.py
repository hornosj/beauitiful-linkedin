from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator
from typing import Any


class SqliteJsonCache:
    def __init__(self, path: str | Path, ttl_seconds: int | None = None) -> None:
        self.path = Path(path)
        self.ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def get_json(self, namespace: str, payload: dict[str, Any]) -> Any | None:
        key = self._cache_key(namespace, payload)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT response_json, created_at FROM api_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None

            response_json, created_at = row
            if self.ttl_seconds is not None and time.time() - float(created_at) > self.ttl_seconds:
                connection.execute("DELETE FROM api_cache WHERE cache_key = ?", (key,))
                return None

            return json.loads(response_json)

    def set_json(self, namespace: str, payload: dict[str, Any], response: Any) -> None:
        key = self._cache_key(namespace, payload)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO api_cache(cache_key, namespace, request_json, response_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    response_json = excluded.response_json,
                    created_at = excluded.created_at
                """,
                (
                    key,
                    namespace,
                    json.dumps(payload, sort_keys=True, ensure_ascii=False),
                    json.dumps(response, sort_keys=True, ensure_ascii=False),
                    time.time(),
                ),
            )

    def _ensure_schema(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS api_cache (
                    cache_key TEXT PRIMARY KEY,
                    namespace TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at REAL NOT NULL
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

    def _cache_key(self, namespace: str, payload: dict[str, Any]) -> str:
        canonical = json.dumps(
            {"namespace": namespace, "payload": payload},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
