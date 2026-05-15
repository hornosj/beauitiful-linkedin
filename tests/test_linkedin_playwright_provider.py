from beautiful_linkedin.models import CompanyInput
from beautiful_linkedin.providers.linkedin_playwright import (
    LinkedInPlaywrightProvider,
    PlaywrightProfileRecord,
    build_people_url,
    company_universal_name,
    record_to_lead,
)


def test_build_people_url_no_keyword():
    assert (
        build_people_url("nubank", None)
        == "https://www.linkedin.com/company/nubank/people/"
    )


def test_build_people_url_with_keyword_is_url_encoded():
    url = build_people_url("nubank", "head of marketing")
    assert url.startswith("https://www.linkedin.com/company/nubank/people/?keywords=")
    assert "head+of+marketing" in url


def test_company_universal_name_prefers_linkedin_url():
    company = CompanyInput(
        company_name="Nu Pagamentos",
        linkedin_url="https://www.linkedin.com/company/nubank/",
        titles=["marketing"],
    )
    assert company_universal_name(company) == "nubank"


def test_company_universal_name_falls_back_to_slug_from_name():
    company = CompanyInput(
        company_name="Stone Pagamentos",
        titles=["marketing"],
    )
    assert company_universal_name(company) == "stone-pagamentos"


def test_record_to_lead_keeps_matched_titles():
    company = CompanyInput(
        company_name="Nubank",
        titles=["marketing"],
    )
    record = PlaywrightProfileRecord(
        full_name="Ana Silva",
        headline="Head of Marketing at Nubank",
        location="Sao Paulo, Brasil",
        profile_url="https://www.linkedin.com/in/ana-silva/",
    )
    lead = record_to_lead(company, record, include_uncertain=False)
    assert lead is not None
    assert lead.person_name == "Ana Silva"
    assert lead.matched_title == "marketing"
    assert lead.source_type == "linkedin_playwright"


def test_record_to_lead_drops_unmatched_when_strict():
    company = CompanyInput(
        company_name="Nubank",
        titles=["marketing"],
    )
    record = PlaywrightProfileRecord(
        full_name="Caio Finance",
        headline="Finance Analyst at Nubank",
        location="Sao Paulo, Brasil",
        profile_url="https://www.linkedin.com/in/caio-finance/",
    )
    assert record_to_lead(company, record, include_uncertain=False) is None


def test_record_to_lead_keeps_unmatched_when_uncertain_allowed():
    company = CompanyInput(
        company_name="Nubank",
        titles=["marketing"],
    )
    record = PlaywrightProfileRecord(
        full_name="Caio Finance",
        headline="Finance Analyst at Nubank",
        location="Sao Paulo, Brasil",
        profile_url="https://www.linkedin.com/in/caio-finance/",
    )
    lead = record_to_lead(company, record, include_uncertain=True)
    assert lead is not None
    assert lead.matched_title is None


def test_provider_returns_empty_without_cookie():
    provider = LinkedInPlaywrightProvider(cookie=None, cookie_browser="none")
    company = CompanyInput(
        company_name="Nubank",
        linkedin_url="https://www.linkedin.com/company/nubank/",
        titles=["marketing"],
    )
    assert provider.find_leads(company, max_results=5, include_uncertain=True) == []


def test_provider_uses_collect_records_when_cookie_present(monkeypatch):
    provider = LinkedInPlaywrightProvider(cookie="AQED-cookie", cookie_browser="none")

    def fake_collect(self, li_at, company_slug, titles, max_results):
        assert li_at == "AQED-cookie"
        assert company_slug == "nubank"
        return [
            PlaywrightProfileRecord(
                full_name="Ana Silva",
                headline="Head of Marketing at Nubank",
                location="Sao Paulo, Brasil",
                profile_url="https://www.linkedin.com/in/ana-silva/",
            ),
            PlaywrightProfileRecord(
                full_name="Caio Finance",
                headline="Finance Analyst at Nubank",
                location="Sao Paulo, Brasil",
                profile_url="https://www.linkedin.com/in/caio-finance/",
            ),
        ]

    monkeypatch.setattr(
        LinkedInPlaywrightProvider, "collect_profile_records", fake_collect
    )

    company = CompanyInput(
        company_name="Nubank",
        linkedin_url="https://www.linkedin.com/company/nubank/",
        titles=["marketing"],
    )
    leads = provider.find_leads(company, max_results=5, include_uncertain=False)
    assert len(leads) == 1
    assert leads[0].person_name == "Ana Silva"
    assert leads[0].source_type == "linkedin_playwright"


def test_provider_swallows_collect_failure_and_returns_empty(monkeypatch):
    provider = LinkedInPlaywrightProvider(cookie="AQED-cookie", cookie_browser="none")

    def boom(self, li_at, company_slug, titles, max_results):
        raise RuntimeError("Playwright nao instalado")

    monkeypatch.setattr(
        LinkedInPlaywrightProvider, "collect_profile_records", boom
    )

    company = CompanyInput(
        company_name="Nubank",
        linkedin_url="https://www.linkedin.com/company/nubank/",
        titles=["marketing"],
    )
    assert provider.find_leads(company, max_results=5, include_uncertain=True) == []
