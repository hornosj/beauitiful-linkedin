import logging

import httpx
import pytest

from beautiful_linkedin.api_logging import (
    log_http_error,
    log_transport_error,
    log_unexpected_error,
    truncate,
)


def _make_http_status_error(status: int, body: str, url: str = "https://api.example.com/v1") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    response = httpx.Response(status, text=body, request=request)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("Expected HTTPStatusError")


def test_log_http_error_includes_status_url_and_body_excerpt(caplog):
    exc = _make_http_status_error(401, '{"error":"invalid_api_key"}')
    logger = logging.getLogger("beautiful_linkedin.test")

    with caplog.at_level(logging.WARNING, logger="beautiful_linkedin.test"):
        log_http_error(logger, provider="People Data Labs", error=exc, context="empresa=CSD BR")

    assert any(
        "People Data Labs retornou HTTP 401" in record.getMessage()
        and "https://api.example.com/v1" in record.getMessage()
        and "invalid_api_key" in record.getMessage()
        and "[empresa=CSD BR]" in record.getMessage()
        for record in caplog.records
    )


def test_log_http_error_truncates_long_bodies(caplog):
    long_body = "x" * 1000
    exc = _make_http_status_error(500, long_body)
    logger = logging.getLogger("beautiful_linkedin.test_truncation")

    with caplog.at_level(logging.WARNING, logger="beautiful_linkedin.test_truncation"):
        log_http_error(logger, provider="Apollo", error=exc)

    message = caplog.records[-1].getMessage()
    assert "(+500 chars)" in message
    assert "x" * 500 in message


def test_log_http_error_handles_empty_body(caplog):
    exc = _make_http_status_error(429, "")
    logger = logging.getLogger("beautiful_linkedin.test_empty")

    with caplog.at_level(logging.WARNING, logger="beautiful_linkedin.test_empty"):
        log_http_error(logger, provider="Lusha", error=exc)

    assert "<corpo vazio>" in caplog.records[-1].getMessage()


def test_log_transport_error_includes_endpoint_and_exception_type(caplog):
    logger = logging.getLogger("beautiful_linkedin.test_transport")
    exc = httpx.ConnectError("connection refused")

    with caplog.at_level(logging.WARNING, logger="beautiful_linkedin.test_transport"):
        log_transport_error(
            logger,
            provider="Serper",
            error=exc,
            endpoint="https://google.serper.dev/search",
            context="query=site:linkedin.com",
        )

    message = caplog.records[-1].getMessage()
    assert "Requisição Serper falhou" in message
    assert "https://google.serper.dev/search" in message
    assert "ConnectError" in message
    assert "connection refused" in message
    assert "[query=site:linkedin.com]" in message


def test_log_transport_error_works_without_endpoint_or_context(caplog):
    logger = logging.getLogger("beautiful_linkedin.test_minimal")
    exc = ValueError("invalid json")

    with caplog.at_level(logging.WARNING, logger="beautiful_linkedin.test_minimal"):
        log_transport_error(logger, provider="Coresignal", error=exc)

    message = caplog.records[-1].getMessage()
    assert message.startswith("Requisição Coresignal falhou.")
    assert "ValueError" in message


def test_log_unexpected_error_includes_traceback(caplog):
    logger = logging.getLogger("beautiful_linkedin.test_unexpected")

    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        with caplog.at_level(logging.WARNING, logger="beautiful_linkedin.test_unexpected"):
            log_unexpected_error(
                logger,
                provider="Apify LinkedIn Actor",
                error=exc,
                endpoint="https://api.apify.com/...",
                context="empresa=CSD BR",
            )

    record = caplog.records[-1]
    assert "Erro inesperado em Apify LinkedIn Actor" in record.getMessage()
    assert record.exc_info is not None, "expected exc_info for traceback"


@pytest.mark.parametrize(
    "value,limit,expected",
    [
        ("hello world", 100, "hello world"),
        ("hello world", 5, "hello…"),
        ("multi\nline\ttext", 100, "multi line text"),
        ("", 10, ""),
    ],
)
def test_truncate_normalizes_whitespace_and_caps_length(value, limit, expected):
    assert truncate(value, limit) == expected
