"""Tests for the FastAPI sidecar that powers the Electron desktop app.

Written TDD-first: these describe the contract the renderer relies on,
without booting Chromium or hitting LinkedIn. The runner is monkeypatched
so endpoints exercise serialization, validation, and the run registry only.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from beautiful_linkedin.models import Lead, ProspectingResult, ProspectingSummary
from beautiful_linkedin.server.app import RunStatus, build_app, get_run_registry


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    captured: dict[str, Any] = {}

    def fake_run_prospecting(**kwargs: Any) -> ProspectingResult:
        captured.update(kwargs)
        leads = [
            Lead(
                company_name=kwargs["companies"][0].company_name,
                person_name="Ana Silva",
                title="Head of Marketing",
                linkedin_url="https://www.linkedin.com/in/ana-silva/",
                source_url="https://www.linkedin.com/in/ana-silva/",
                source_type="linkedin_cookie",
                snippet="Head of Marketing | Sao Paulo",
                matched_title="marketing",
                confidence_score=88,
            )
        ]
        summary = ProspectingSummary(
            total_companies_processed=len(kwargs["companies"]),
            total_raw_leads=1,
            total_deduplicated_leads=1,
            output_file=str(kwargs["output_path"]),
            top_sources={"linkedin_cookie": 1},
        )
        return ProspectingResult(leads=leads, summary=summary)

    monkeypatch.setattr(
        "beautiful_linkedin.server.app.run_prospecting", fake_run_prospecting
    )
    app = build_app()
    app.state.captured = captured
    get_run_registry(app).clear()
    return TestClient(app)


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert "version" in payload


def test_taxonomies_endpoint_lists_seniority_and_functions(client: TestClient) -> None:
    response = client.get("/taxonomies")
    assert response.status_code == 200
    payload = response.json()
    seniority = {item["value"] for item in payload["seniority"]}
    functions = {item["value"] for item in payload["functions"]}
    scrape_modes = {item["value"] for item in payload["scrape_modes"]}
    role_presets = {item["value"]: item for item in payload["role_presets"]}

    assert "c_level" in seniority
    assert "intern" in seniority
    assert "marketing" in functions
    assert "engineering" in functions
    assert {"api", "serp", "cookie", "browser", "people_search"}.issubset(scrape_modes)
    assert "marketing_growth" in role_presets
    assert "growth" in role_presets["marketing_growth"]["aliases"]


def test_search_returns_leads_and_summary(client: TestClient) -> None:
    body = {
        "company_name": "Nubank",
        "company_domain": "nubank.com.br",
        "linkedin_url": "https://www.linkedin.com/company/nubank/",
        "titles": ["marketing"],
        "max_results": 10,
        "scrape_mode": "serp",
        "output_path": "output/test.csv",
    }
    response = client.post("/search", json=body)
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["total_deduplicated_leads"] == 1
    assert payload["leads"][0]["person_name"] == "Ana Silva"
    assert payload["summary"]["output_file"] == "output/test.csv"


def test_search_propagates_filter_payload(client: TestClient) -> None:
    body = {
        "company_name": "Nubank",
        "titles": ["marketing"],
        "max_results": 5,
        "scrape_mode": "serp",
        "output_path": "output/test.csv",
        "filters": {
            "seniority_in": ["c_level", "vp"],
            "functions_in": ["marketing"],
            "exclude_titles": ["recruiter"],
            "min_confidence_score": 50,
            "drop_unclassified": True,
        },
    }
    response = client.post("/search", json=body)
    assert response.status_code == 200
    captured = client.app.state.captured
    lead_filter = captured["lead_filter"]
    assert [s.value for s in lead_filter.seniority_in] == ["c_level", "vp"]
    assert [f.value for f in lead_filter.functions_in] == ["marketing"]
    assert lead_filter.exclude_titles == ["recruiter"]
    assert lead_filter.min_confidence_score == 50
    assert lead_filter.drop_unclassified is True


def test_search_applies_api_key_overrides(client: TestClient) -> None:
    body = {
        "company_name": "Nubank",
        "titles": ["marketing"],
        "max_results": 5,
        "scrape_mode": "api",
        "output_path": "output/test.csv",
        "api_keys": {
            "apollo_api_key": "apollo-test-key",
            "serper_api_key": "serper-test-key",
            "linkedin_li_at_cookie": "li_at=test-cookie",
            "linkedin_cookie_browser": "chrome",
        },
    }
    response = client.post("/search", json=body)
    assert response.status_code == 200

    settings = client.app.state.captured["settings"]
    assert settings.apollo_api_key == "apollo-test-key"
    assert settings.serper_api_key == "serper-test-key"
    assert settings.linkedin_li_at_cookie == "li_at=test-cookie"
    assert settings.linkedin_cookie_browser == "chrome"


def test_search_rejects_browser_mode_without_explicit_consent(client: TestClient) -> None:
    body = {
        "company_name": "Nubank",
        "linkedin_url": "https://www.linkedin.com/company/nubank/",
        "titles": ["marketing"],
        "max_results": 5,
        "scrape_mode": "browser",
        "output_path": "output/test.csv",
    }
    response = client.post("/search", json=body)
    assert response.status_code == 412
    payload = response.json()
    assert "ARRISCADO" in payload["detail"] or "arriscado" in payload["detail"]


def test_search_accepts_browser_mode_with_consent(client: TestClient) -> None:
    body = {
        "company_name": "Nubank",
        "linkedin_url": "https://www.linkedin.com/company/nubank/",
        "titles": ["marketing"],
        "max_results": 5,
        "scrape_mode": "browser",
        "output_path": "output/test.csv",
        "accept_risk": True,
    }
    response = client.post("/search", json=body)
    assert response.status_code == 200


def test_search_validates_required_fields(client: TestClient) -> None:
    response = client.post("/search", json={"titles": ["marketing"]})
    assert response.status_code == 422


def test_cookie_diagnostic_returns_explicit_when_provided(client: TestClient) -> None:
    response = client.post(
        "/diagnostics/cookie",
        json={"cookie": "li_at=AQED-test-explicit", "browser": "auto"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["found"] is True
    assert payload["source"] == "explicit"
    # The preview is masked, never the raw value.
    assert "AQED-test-explicit" not in (payload.get("preview") or "")


def test_cookie_diagnostic_returns_missing_with_hints_when_browser_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LINKEDIN_LI_AT_COOKIE", raising=False)
    monkeypatch.delenv("LINKEDIN_COOKIE", raising=False)
    monkeypatch.delenv("LI_AT", raising=False)

    response = client.post(
        "/diagnostics/cookie",
        json={"cookie": None, "browser": "none"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["found"] is False
    assert payload["source"] == "missing"
    assert isinstance(payload["hints"], list) and payload["hints"]


def test_run_lifecycle_via_async_endpoint(client: TestClient) -> None:
    body = {
        "company_name": "Nubank",
        "titles": ["marketing"],
        "max_results": 5,
        "scrape_mode": "serp",
        "output_path": "output/test.csv",
    }
    response = client.post("/search/start", json=body)
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    assert run_id

    deadline = time.time() + 5.0
    while time.time() < deadline:
        status_response = client.get(f"/runs/{run_id}")
        assert status_response.status_code == 200
        status_payload = status_response.json()
        if status_payload["status"] in {RunStatus.COMPLETED.value, RunStatus.FAILED.value}:
            break
        time.sleep(0.05)

    assert status_payload["status"] == RunStatus.COMPLETED.value
    assert status_payload["result"]["leads"][0]["person_name"] == "Ana Silva"


def test_run_status_404_for_unknown_run(client: TestClient) -> None:
    response = client.get("/runs/does-not-exist")
    assert response.status_code == 404


def test_cancel_run_marks_it_cancelled(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    started = threading.Event()
    proceed = threading.Event()

    def slow_run_prospecting(**kwargs: Any) -> ProspectingResult:
        started.set()
        proceed.wait(timeout=2.0)
        return ProspectingResult(
            leads=[],
            summary=ProspectingSummary(
                total_companies_processed=0,
                total_raw_leads=0,
                total_deduplicated_leads=0,
                output_file=str(kwargs["output_path"]),
            ),
        )

    monkeypatch.setattr(
        "beautiful_linkedin.server.app.run_prospecting", slow_run_prospecting
    )

    body = {
        "company_name": "Nubank",
        "titles": ["marketing"],
        "max_results": 5,
        "scrape_mode": "serp",
        "output_path": "output/test.csv",
    }
    start = client.post("/search/start", json=body)
    run_id = start.json()["run_id"]
    assert started.wait(timeout=2.0)

    cancel = client.delete(f"/runs/{run_id}")
    assert cancel.status_code == 200
    proceed.set()

    deadline = time.time() + 2.0
    while time.time() < deadline:
        status = client.get(f"/runs/{run_id}").json()
        if status["status"] in {RunStatus.CANCELLED.value, RunStatus.COMPLETED.value}:
            break
        time.sleep(0.05)
    assert status["status"] == RunStatus.CANCELLED.value
