"""Endpoint tests for POST /lead-tables/{id}/experimental-search.

The endpoint must:
- accept an open table id;
- resolve free engines from the running settings (monkeypatched here);
- run dedupe against the table's saved leads;
- persist new leads with source_type=experimental_search;
- return a summary the UI can render (counts + engine names + note).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beautiful_linkedin.models import SearchResult
from beautiful_linkedin.search.search_engine import SearchEngine
from beautiful_linkedin.server.app import build_app


class FakeEngine(SearchEngine):
    def __init__(self, results: list[SearchResult]) -> None:
        self.results = results

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        return self.results


@pytest.fixture
def fake_engines(monkeypatch: pytest.MonkeyPatch) -> dict[str, SearchEngine]:
    engines: dict[str, SearchEngine] = {
        "fake": FakeEngine(
            [
                SearchResult(
                    title="Ana Silva - Head of Marketing - Acme | LinkedIn",
                    url="https://www.linkedin.com/in/ana-silva/",
                    snippet="Acme · Head of Marketing",
                    source_type="experimental_search",
                ),
                SearchResult(
                    title="Bruno Costa - Marketing - Acme | LinkedIn",
                    url="https://www.linkedin.com/in/bruno-costa/",
                    snippet="Acme · Marketing Manager",
                    source_type="experimental_search",
                ),
            ]
        )
    }
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._resolve_free_engines",
        lambda settings: engines,
    )
    return engines


@pytest.fixture
def client(tmp_path: Path, fake_engines) -> TestClient:
    app = build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))
    return TestClient(app)


def _saved_lead(person: str, slug: str, confidence: int = 88) -> dict:
    url = f"https://www.linkedin.com/in/{slug}/"
    return {
        "company_name": "Acme",
        "person_name": person,
        "title": "Head of Marketing",
        "linkedin_url": url,
        "source_url": url,
        "source_type": "linkedin_people_search",
        "snippet": "",
        "confidence_score": confidence,
    }


def test_experimental_search_adds_only_new_leads(client: TestClient) -> None:
    created = client.post(
        "/lead-tables",
        json={
            "name": "Acme MKT",
            "leads": [_saved_lead("Ana Silva", "ana-silva")],
            "keywords": ["marketing"],
            "search_request": {
                "company_name": "Acme",
                "company_domain": "acme.com",
                "titles": ["marketing"],
            },
        },
    ).json()
    table_id = created["table"]["id"]

    response = client.post(f"/lead-tables/{table_id}/experimental-search")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["candidates_total"] >= 2
    assert body["duplicates_skipped"] == 1
    assert len(body["new_leads"]) == 1
    assert body["new_leads"][0]["person_name"] == "Bruno Costa"
    assert body["new_leads"][0]["source_type"] == "experimental_search"
    assert "experimental" in body["new_leads"][0]["consultation_note"].lower()
    assert body["engines_used"] == ["fake"]

    # Persisted: the table now has both Ana and Bruno.
    detail = client.get(f"/lead-tables/{table_id}").json()
    persons = {lead["person_name"] for lead in detail["leads"]}
    assert persons == {"Ana Silva", "Bruno Costa"}


def test_experimental_search_reports_no_new_when_all_overlap(
    client: TestClient,
) -> None:
    created = client.post(
        "/lead-tables",
        json={
            "name": "Acme Saturated",
            "leads": [
                _saved_lead("Ana Silva", "ana-silva"),
                _saved_lead("Bruno Costa", "bruno-costa"),
            ],
            "keywords": ["marketing"],
            "search_request": {
                "company_name": "Acme",
                "company_domain": "acme.com",
                "titles": ["marketing"],
            },
        },
    ).json()
    table_id = created["table"]["id"]

    body = client.post(f"/lead-tables/{table_id}/experimental-search").json()
    assert body["new_leads"] == []
    assert body["duplicates_skipped"] >= 2


def test_experimental_search_returns_note_when_no_engines(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._resolve_free_engines",
        lambda settings: {},
    )
    table_id = client.post(
        "/lead-tables",
        json={
            "name": "Sem Buscadores",
            "leads": [_saved_lead("Ana", "a")],
            "keywords": ["marketing"],
            "search_request": {"company_name": "Acme"},
        },
    ).json()["table"]["id"]

    body = client.post(f"/lead-tables/{table_id}/experimental-search").json()
    assert body["new_leads"] == []
    assert body["engines_used"] == []
    assert body["note"]


def test_experimental_search_404_for_missing_table(client: TestClient) -> None:
    response = client.post("/lead-tables/does-not-exist/experimental-search")
    assert response.status_code == 404
