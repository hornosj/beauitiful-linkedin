"""Unit tests for the CDP-backed company-size resolver.

The resolver is the bridge between the FastAPI probe registry and the
people_search provider. It builds the slug, fetches the People page via
the injected page factory, parses signals, and emits step events along
the way. No network: the page factory is a fake that returns canned HTML.
"""

from __future__ import annotations

from typing import Any

from beautiful_linkedin.providers.linkedin_people_search import (
    resolve_company_size_via_page,
)


class FakePage:
    def __init__(self, html: str, *, final_url: str | None = None) -> None:
        self._html = html
        # When ``final_url`` is set, simulate a redirect: goto leaves url at
        # the redirect target instead of echoing the requested URL.
        self._forced_final_url = final_url
        self.url = final_url or "https://www.linkedin.com/company/acme/people/"

    def goto(self, url: str, wait_until: str | None = None) -> None:
        self.url = self._forced_final_url or url

    def content(self) -> str:
        return self._html

    def close(self) -> None:
        pass


def _collect_events() -> tuple[list[dict[str, Any]], Any]:
    events: list[dict[str, Any]] = []

    def emit(event: str, data: dict[str, Any] | None = None) -> None:
        events.append({"event": event, "data": dict(data or {})})

    return events, emit


def test_resolver_extracts_count_from_associated_members() -> None:
    page = FakePage(
        '<header><p>128 funcionários associados</p></header>'
        '<li class="org-people-profile-card__profile-card-spacing">a</li>' * 3
    )
    events, emit = _collect_events()

    signals = resolve_company_size_via_page(
        company_name="Acme",
        linkedin_url=None,
        company_domain=None,
        emit=emit,
        page_factory=lambda: page,
    )

    assert signals["employee_count"] == 128
    names = [event["event"] for event in events]
    assert names[:3] == ["recognizing", "recognized", "fetching"]
    assert "employees_seen" in names
    seen = next(event for event in events if event["event"] == "employees_seen")
    assert seen["data"]["count"] == 128


def test_resolver_falls_back_to_card_count_heuristic() -> None:
    page = FakePage(
        '<li class="org-people-profile-card__profile-card-spacing">a</li>' * 15
    )
    events, emit = _collect_events()

    signals = resolve_company_size_via_page(
        company_name="Acme",
        linkedin_url=None,
        company_domain=None,
        emit=emit,
        page_factory=lambda: page,
    )

    assert signals["employee_count"] is None
    assert signals["visible_card_count"] == 15


def test_resolver_emits_login_event_when_redirected_to_login() -> None:
    page = FakePage(
        "<html><body>login</body></html>",
        final_url="https://www.linkedin.com/login",
    )
    events, emit = _collect_events()

    signals = resolve_company_size_via_page(
        company_name="Acme",
        linkedin_url=None,
        company_domain=None,
        emit=emit,
        page_factory=lambda: page,
    )

    assert signals == {}
    assert any(event["event"] == "login_required" for event in events)


def test_resolver_uses_linkedin_url_slug_when_provided() -> None:
    page = FakePage('<header><p>57 funcionários associados</p></header>')
    events, emit = _collect_events()

    resolve_company_size_via_page(
        company_name="Acme S.A.",
        linkedin_url="https://www.linkedin.com/company/acme-sa/about/",
        company_domain=None,
        emit=emit,
        page_factory=lambda: page,
    )

    recognized = next(event for event in events if event["event"] == "recognized")
    assert recognized["data"]["slug"] == "acme-sa"
