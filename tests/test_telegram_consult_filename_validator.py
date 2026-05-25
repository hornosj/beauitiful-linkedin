"""Tests for the filename heuristic that tells the real Unix Mk text
download apart from the site's corrupted blob.

The provider clicks "Texto" and sometimes receives:
- ``nome_resultado_mk_unix_1779287290685.txt`` (real)
- ``bcd032cd-b0fc-42db-8185-5009be86dac5`` (corrupted, UUID, no ext)

Accepting the wrong one would silently persist garbage in
``consulta_telegram`` — these tests pin the rule.
"""

from __future__ import annotations

from beautiful_linkedin.storage.telegram_group_playwright_lookup import (
    TelegramGroupPlaywrightLookup,
)


def _lookup() -> TelegramGroupPlaywrightLookup:
    # Construct with a no-op fetcher so we never reach Playwright.
    return TelegramGroupPlaywrightLookup(fetcher=lambda _name: None)  # type: ignore[arg-type]


def test_accepts_real_unix_mk_filename() -> None:
    lookup = _lookup()
    assert lookup._looks_like_valid_text_download(
        "nome_resultado_mk_unix_1779287290685.txt"
    )


def test_accepts_real_filename_case_insensitive() -> None:
    lookup = _lookup()
    assert lookup._looks_like_valid_text_download(
        "Nome_Resultado_MK_Unix_999.TXT"
    )


def test_rejects_uuid_blob_without_extension() -> None:
    lookup = _lookup()
    assert not lookup._looks_like_valid_text_download(
        "bcd032cd-b0fc-42db-8185-5009be86dac5"
    )


def test_rejects_empty_filename() -> None:
    lookup = _lookup()
    assert not lookup._looks_like_valid_text_download("")
    assert not lookup._looks_like_valid_text_download("   ")


def test_rejects_other_extensions() -> None:
    lookup = _lookup()
    assert not lookup._looks_like_valid_text_download(
        "nome_resultado_mk_unix_1779287290685.pdf"
    )
    assert not lookup._looks_like_valid_text_download(
        "nome_resultado_mk_unix_1779287290685.html"
    )


def test_rejects_txt_with_wrong_prefix() -> None:
    """Even with the .txt extension, a different naming scheme should
    be rejected — likely a different bot / unrelated download."""
    lookup = _lookup()
    assert not lookup._looks_like_valid_text_download("relatorio_geral.txt")
    assert not lookup._looks_like_valid_text_download("dados.txt")
