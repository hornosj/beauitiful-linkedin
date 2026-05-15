"""TDD for POST /people-search/probe.

Probes a company before the people_search run to decide whether to offer
the 'busca geral' dialog. Backend is pluggable; the test uses a fake
resolver to avoid network.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from beautiful_linkedin.server.app import build_app


@pytest.fixture
def client(tmp_path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    def fake_probe(*, company_name: str, **kwargs: Any) -> dict[str, Any]:
        if company_name.lower() == "nubank":
            return {
                "employee_count_text": "10,001+ employees",
                "visible_card_count": None,
            }
        if company_name.lower() == "tiny startup":
            return {"employee_count": 42, "visible_card_count": None}
        if company_name.lower() == "midsize":
            return {"employee_count_text": "51-200 employees"}
        return {}

    monkeypatch.setattr(
        "beautiful_linkedin.server.app._probe_people_company",
        fake_probe,
    )
    app = build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))
    return TestClient(app)


def test_probe_flags_large_company_as_not_small(client: TestClient) -> None:
    response = client.post(
        "/people-search/probe",
        json={"company_name": "Nubank"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["is_small"] is False
    assert body["employee_count"] == 10001
    assert body["source"] == "range"


def test_probe_flags_small_company(client: TestClient) -> None:
    response = client.post(
        "/people-search/probe",
        json={"company_name": "Midsize"},
    )
    body = response.json()
    assert body["is_small"] is True
    assert body["source"] == "range"
    assert body["should_offer_general_search"] is True


def test_probe_below_range_also_offers_general_search(client: TestClient) -> None:
    response = client.post(
        "/people-search/probe",
        json={"company_name": "Tiny Startup"},
    )
    body = response.json()
    assert body["is_small"] is True
    assert body["should_offer_general_search"] is True


def test_probe_returns_unknown_when_no_signal(client: TestClient) -> None:
    response = client.post(
        "/people-search/probe",
        json={"company_name": "Unheard Of"},
    )
    body = response.json()
    assert body["is_small"] is None
    assert body["source"] == "unknown"
    assert body["should_offer_general_search"] is False
