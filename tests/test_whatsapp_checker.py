"""Offline tests for WhatsAppNumberChecker (wa.me probe).

We never touch the live wa.me — the HTTP client is injected, so every
case asserts the classifier on a canned response. The contract:

- Redirect to ``api.whatsapp.com/send?phone=...`` → ACTIVE.
- HTML contains a positive marker ("/send?phone=", "iniciar conversa",
  etc.) and no negative marker → ACTIVE.
- HTML contains a negative marker ("número de telefone compartilhado",
  etc.) and no positive marker → INACTIVE.
- HTTP 404 → INACTIVE.
- HTTP 5xx → UNKNOWN (don't conclude during Meta blips).
- Ambiguous body → UNKNOWN.
- Bad length → INVALID_FORMAT.
"""

from __future__ import annotations

from beautiful_linkedin.storage.whatsapp_checker import (
    WhatsAppNumberChecker,
    WhatsAppStatus,
)


def _stub_client(response_by_url):
    def fetch(url):
        return response_by_url.get(url, (404, url, ""))

    return fetch


def _checker(response_by_url):
    return WhatsAppNumberChecker(
        http_client=_stub_client(response_by_url),
        sleep=lambda _s: None,
        request_interval_seconds=0,
    )


def test_active_via_redirect_to_send() -> None:
    checker = _checker(
        {
            "https://wa.me/5511999999999": (
                200,
                "https://api.whatsapp.com/send?phone=5511999999999",
                "<html>Chat ready</html>",
            )
        }
    )
    result = checker.check("+5511999999999")
    assert result.status == WhatsAppStatus.ACTIVE
    assert result.reason == "redirect_to_send"


def test_active_via_positive_marker_in_body() -> None:
    checker = _checker(
        {
            "https://wa.me/5511999999999": (
                200,
                "https://wa.me/5511999999999",
                '<a href="whatsapp://send?phone=5511999999999">Iniciar conversa</a>',
            )
        }
    )
    assert checker.check("+5511999999999").status == WhatsAppStatus.ACTIVE


def test_inactive_via_negative_marker() -> None:
    checker = _checker(
        {
            "https://wa.me/5511888888888": (
                200,
                "https://wa.me/5511888888888",
                "<p>Número de telefone compartilhado pelo URL inválido.</p>",
            )
        }
    )
    assert checker.check("+5511888888888").status == WhatsAppStatus.INACTIVE


def test_inactive_via_404() -> None:
    checker = _checker(
        {"https://wa.me/5511777777777": (404, "https://wa.me/5511777777777", "")}
    )
    assert checker.check("+5511777777777").status == WhatsAppStatus.INACTIVE


def test_unknown_on_server_error() -> None:
    checker = _checker(
        {"https://wa.me/5511666666666": (503, "", "")}
    )
    result = checker.check("+5511666666666")
    assert result.status == WhatsAppStatus.UNKNOWN
    assert "503" in result.reason


def test_unknown_on_ambiguous_body() -> None:
    checker = _checker(
        {"https://wa.me/5511555555555": (200, "https://wa.me/5511555555555", "<html>oi</html>")}
    )
    assert checker.check("+5511555555555").status == WhatsAppStatus.UNKNOWN


def test_invalid_format_short_circuits_without_http_call() -> None:
    calls: list[str] = []

    def tracking_client(url):
        calls.append(url)
        return (200, url, "")

    checker = WhatsAppNumberChecker(
        http_client=tracking_client,
        sleep=lambda _s: None,
        request_interval_seconds=0,
    )
    assert checker.check("12").status == WhatsAppStatus.INVALID_FORMAT
    assert calls == []


def test_cache_returns_same_result_without_second_http_call() -> None:
    calls: list[str] = []

    def tracking_client(url):
        calls.append(url)
        return (200, "https://api.whatsapp.com/send?phone=5511999999999", "")

    checker = WhatsAppNumberChecker(
        http_client=tracking_client,
        sleep=lambda _s: None,
        request_interval_seconds=0,
    )
    a = checker.check("+5511999999999")
    b = checker.check("+5511999999999")
    assert a == b
    assert len(calls) == 1


def test_failure_in_client_falls_back_to_unknown() -> None:
    def boom(_url):
        raise ConnectionError("boom")

    checker = WhatsAppNumberChecker(
        http_client=boom,
        sleep=lambda _s: None,
        request_interval_seconds=0,
    )
    result = checker.check("+5511999999999")
    assert result.status == WhatsAppStatus.UNKNOWN
    assert "ConnectionError" in result.reason
