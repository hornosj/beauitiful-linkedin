"""Tests for the in-app Telegram credentials configuration.

The desktop app lets the end user paste their my.telegram.org ``api_id`` /
``api_hash`` instead of hand-editing a ``.env``. These exercise both the
persistence helpers in :mod:`beautiful_linkedin.config` and the FastAPI
endpoints that the renderer calls, all offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beautiful_linkedin import config
from beautiful_linkedin.server.app import build_app


@pytest.fixture
def isolated_creds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point credential storage at a temp file and neutralize any ``.env``.

    ``load_settings`` calls ``load_dotenv`` which would otherwise pull the
    developer's real values from the project ``.env``. Pre-seeding the env
    vars as empty blocks that (dotenv won't override an existing key), so the
    stored-file fallback is what these tests actually measure.
    """
    creds = tmp_path / "telegram_credentials.json"
    monkeypatch.setenv("BEAUTIFUL_LINKEDIN_TELEGRAM_CONFIG_PATH", str(creds))
    for var in (
        "BEAUTIFUL_LINKEDIN_TELEGRAM_API_ID",
        "TELEGRAM_API_ID",
        "BEAUTIFUL_LINKEDIN_TELEGRAM_API_HASH",
        "TELEGRAM_API_HASH",
    ):
        monkeypatch.setenv(var, "")
    return creds


def test_save_read_clear_roundtrip(isolated_creds: Path) -> None:
    assert config.read_stored_telegram_credentials() == (None, None)

    config.save_telegram_credentials("1234567", "deadbeefcafe")
    assert config.read_stored_telegram_credentials() == ("1234567", "deadbeefcafe")

    settings = config.load_settings()
    assert settings.telegram_api_id == "1234567"
    assert settings.telegram_api_hash == "deadbeefcafe"

    config.clear_telegram_credentials()
    assert config.read_stored_telegram_credentials() == (None, None)


def test_corrupted_file_is_tolerated(isolated_creds: Path) -> None:
    isolated_creds.write_text("{ not json", encoding="utf-8")
    assert config.read_stored_telegram_credentials() == (None, None)


def test_env_takes_precedence_over_stored_file(
    isolated_creds: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.save_telegram_credentials("1111111", "fromfile")
    monkeypatch.setenv("BEAUTIFUL_LINKEDIN_TELEGRAM_API_ID", "9999999")
    monkeypatch.setenv("BEAUTIFUL_LINKEDIN_TELEGRAM_API_HASH", "fromenv")

    settings = config.load_settings()
    assert settings.telegram_api_id == "9999999"
    assert settings.telegram_api_hash == "fromenv"


def test_config_endpoint_persists_credentials(isolated_creds: Path) -> None:
    client = TestClient(build_app())

    response = client.post(
        "/telegram/telethon/config",
        json={"api_id": "1234567", "api_hash": "deadbeefcafe"},
    )
    assert response.status_code == 200
    assert response.json()["configured"] is True
    assert config.read_stored_telegram_credentials() == ("1234567", "deadbeefcafe")


def test_config_endpoint_rejects_non_numeric_id(isolated_creds: Path) -> None:
    client = TestClient(build_app())

    response = client.post(
        "/telegram/telethon/config",
        json={"api_id": "abc123", "api_hash": "deadbeef"},
    )
    assert response.status_code == 400
    assert config.read_stored_telegram_credentials() == (None, None)


def test_config_delete_clears_stored_file(isolated_creds: Path) -> None:
    client = TestClient(build_app())
    config.save_telegram_credentials("1234567", "deadbeef")

    response = client.delete("/telegram/telethon/config")
    assert response.status_code == 200
    assert config.read_stored_telegram_credentials() == (None, None)
