"""TDD for the 'busca geral' (general search, no keywords) flow.

The user-visible flow: when the small-company dialog offers "Fazer busca
geral", or when the user explicitly toggles general mode in the form,
the system runs a people_search WITHOUT any ``?keywords=`` filter and
keeps every visible card.

Pure-logic tests here. UI integration is covered separately on the
Electron side.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from beautiful_linkedin.models import CompanyInput, Lead, ProspectingResult, ProspectingSummary
from beautiful_linkedin.providers.linkedin_people_search import (
    LinkedInPeopleSearchProvider,
    PeopleCard,
    PeopleListFetcher,
    PeopleSearchOptions,
    build_people_search_url,
)
from beautiful_linkedin.server.app import build_app


# ---------- model validation -----------------------------------------------


def test_company_input_accepts_empty_titles_for_general_search() -> None:
    company = CompanyInput(company_name="Acme", titles=[])
    assert company.titles == []
    assert company.company_name == "Acme"


def test_company_input_still_rejects_blank_company_name() -> None:
    with pytest.raises(ValueError):
        CompanyInput(company_name=" ", titles=[])


# ---------- URL builder --------------------------------------------------


def test_build_people_search_url_omits_keywords_for_empty_list() -> None:
    assert build_people_search_url("acme", []) == (
        "https://www.linkedin.com/company/acme/people/"
    )


def test_build_people_search_url_omits_keywords_for_none() -> None:
    assert build_people_search_url("acme", None) == (
        "https://www.linkedin.com/company/acme/people/"
    )


# ---------- provider behavior --------------------------------------------


class _RecordingFetcher(PeopleListFetcher):
    needs_li_at = False

    def __init__(self, html: str) -> None:
        self.html = html
        self.urls_fetched: list[str] = []

    def fetch_listing(self, url: str, *, li_at: str, scrolls: int) -> str:
        self.urls_fetched.append(url)
        return self.html


def _people_card_html() -> str:
    return """
    <ul>
      <li class="org-people-profile-card__profile-card-spacing">
        <a href="https://www.linkedin.com/in/ana-silva/" aria-hidden="true">Ana Silva</a>
        <div class="artdeco-entity-lockup__subtitle">Backend Engineer</div>
        <div class="artdeco-entity-lockup__caption">São Paulo</div>
      </li>
      <li class="org-people-profile-card__profile-card-spacing">
        <a href="https://www.linkedin.com/in/bruno-costa/" aria-hidden="true">Bruno Costa</a>
        <div class="artdeco-entity-lockup__subtitle">Head of Marketing</div>
        <div class="artdeco-entity-lockup__caption">Rio de Janeiro</div>
      </li>
    </ul>
    """


def test_provider_in_general_mode_fetches_url_without_keywords() -> None:
    fetcher = _RecordingFetcher(_people_card_html())
    provider = LinkedInPeopleSearchProvider(
        fetcher=fetcher,
        options=PeopleSearchOptions(scrolls=0),
    )
    leads = provider.find_leads(
        CompanyInput(company_name="Acme", linkedin_url="https://www.linkedin.com/company/acme/", titles=[]),
        max_results=10,
        include_uncertain=False,
    )
    assert len(fetcher.urls_fetched) == 1
    assert "?keywords=" not in fetcher.urls_fetched[0]
    # General mode keeps every visible card, even those whose headline
    # would not match any keyword filter.
    persons = {lead.person_name for lead in leads}
    assert persons == {"Ana Silva", "Bruno Costa"}


def test_provider_with_keywords_still_filters_for_match() -> None:
    fetcher = _RecordingFetcher(_people_card_html())
    provider = LinkedInPeopleSearchProvider(
        fetcher=fetcher,
        options=PeopleSearchOptions(scrolls=0),
    )
    leads = provider.find_leads(
        CompanyInput(
            company_name="Acme",
            linkedin_url="https://www.linkedin.com/company/acme/",
            titles=["marketing"],
        ),
        max_results=10,
        include_uncertain=False,
    )
    persons = {lead.person_name for lead in leads}
    # Only Bruno (Head of Marketing) survives the marketing filter.
    assert persons == {"Bruno Costa"}


# ---------- server contract ---------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    def fake_runner(**kwargs: Any) -> ProspectingResult:
        company = kwargs["companies"][0]
        return ProspectingResult(
            leads=[],
            summary=ProspectingSummary(
                total_companies_processed=1,
                total_raw_leads=0,
                total_deduplicated_leads=0,
                output_file=str(kwargs["output_path"]),
                top_sources={},
            ),
        )

    monkeypatch.setattr(
        "beautiful_linkedin.server.app.run_prospecting",
        fake_runner,
    )
    app = build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))
    return TestClient(app)


def test_search_endpoint_accepts_empty_titles(client: TestClient) -> None:
    response = client.post(
        "/search",
        json={
            "company_name": "Acme",
            "titles": [],
            "max_results": 25,
            "scrape_mode": "people_search",
            "output_path": "output/leads.csv",
            "general_search": True,
        },
    )
    assert response.status_code == 200, response.text


def test_search_endpoint_rejects_empty_titles_when_general_flag_is_false(
    client: TestClient,
) -> None:
    response = client.post(
        "/search",
        json={
            "company_name": "Acme",
            "titles": [],
            "max_results": 25,
            "scrape_mode": "people_search",
            "output_path": "output/leads.csv",
        },
    )
    assert response.status_code == 422
