"""TDD for the async, step-by-step people-search probe.

Flow under test:
1. POST /people-search/probe/start  -> {probe_id, status: 'pending'}
2. Backend runs the resolver in a thread, appending events.
3. GET /people-search/probe/{id}    -> {status, events, classification}

Resolver is monkeypatched: we inject a function that emits a scripted
sequence of events plus a final dict of signals. No network. No Playwright.
"""
from __future__ import annotations

import time
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from beautiful_linkedin.server.app import build_app


@pytest.fixture
def client(tmp_path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    def fake_resolver(
        *,
        company_name: str,
        company_domain: str | None,
        linkedin_url: str | None,
        emit: Callable[[str, dict[str, Any]], None],
    ) -> dict[str, Any]:
        if company_name.lower() == "fail":
            emit("error", {"message": "boom"})
            return {}
        emit("recognizing", {"company_name": company_name})
        slug = company_name.lower().replace(" ", "-")
        emit("recognized", {"slug": slug})
        emit("fetching", {"url": f"https://www.linkedin.com/company/{slug}/people/"})
        if company_name.lower() == "tinyco":
            emit("employees_seen", {"count": 80, "raw_text": "80 associated members"})
            return {
                "employee_count": 80,
                "employee_count_text": "80 associated members",
                "visible_card_count": 80,
            }
        if company_name.lower() == "bigco":
            emit("employees_seen", {"count": 10001, "raw_text": "10,001+ employees"})
            return {
                "employee_count": None,
                "employee_count_text": "10,001+ employees",
                "visible_card_count": 200,
            }
        return {}

    monkeypatch.setattr(
        "beautiful_linkedin.server.app._resolve_people_company_size",
        fake_resolver,
    )
    app = build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))
    return TestClient(app)


def _wait_for_probe(client: TestClient, probe_id: str, deadline_seconds: float = 5.0) -> dict:
    deadline = time.time() + deadline_seconds
    while time.time() < deadline:
        state = client.get(f"/people-search/probe/{probe_id}").json()
        if state["status"] in {"completed", "failed"}:
            return state
        time.sleep(0.05)
    raise AssertionError(f"probe {probe_id} did not finish: {state}")


def test_probe_start_returns_id_and_pending_status(client: TestClient) -> None:
    response = client.post(
        "/people-search/probe/start",
        json={"company_name": "TinyCo"},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["probe_id"]
    assert body["status"] in {"pending", "running"}


def test_probe_emits_step_events_and_classifies_small(client: TestClient) -> None:
    probe_id = client.post(
        "/people-search/probe/start",
        json={"company_name": "TinyCo"},
    ).json()["probe_id"]

    final = _wait_for_probe(client, probe_id)
    assert final["status"] == "completed"

    event_names = [event["event"] for event in final["events"]]
    assert event_names[:3] == ["recognizing", "recognized", "fetching"]
    assert "employees_seen" in event_names
    assert "classified" in event_names

    classification = final["classification"]
    assert classification["is_small"] is True
    assert classification["employee_count"] == 80
    assert classification["should_offer_general_search"] is True


def test_probe_classifies_big_company_as_not_small(client: TestClient) -> None:
    probe_id = client.post(
        "/people-search/probe/start",
        json={"company_name": "BigCo"},
    ).json()["probe_id"]
    final = _wait_for_probe(client, probe_id)
    assert final["status"] == "completed"
    assert final["classification"]["is_small"] is False
    assert final["classification"]["should_offer_general_search"] is False


def test_probe_handles_resolver_errors_gracefully(client: TestClient) -> None:
    probe_id = client.post(
        "/people-search/probe/start",
        json={"company_name": "Fail"},
    ).json()["probe_id"]
    final = _wait_for_probe(client, probe_id)
    # Resolver errors must not crash the probe; the run completes with
    # whatever signals it managed to capture (here: none).
    assert final["status"] == "completed"
    assert final["classification"]["is_small"] is None
    assert any(event["event"] == "error" for event in final["events"])


def test_probe_404_for_missing_id(client: TestClient) -> None:
    response = client.get("/people-search/probe/does-not-exist")
    assert response.status_code == 404
