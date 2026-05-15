from beautiful_linkedin.main import app
from beautiful_linkedin.models import CompanyInput, Lead, SearchResult


def test_import_app():
    assert app is not None


def test_models_can_be_instantiated():
    company = CompanyInput(
        company_name="Nubank",
        company_domain="nubank.com.br",
        linkedin_url="https://www.linkedin.com/company/nubank",
        titles=["marketing", "growth"],
    )
    result = SearchResult(
        title="Fulano Silva - Head of Marketing - Nubank | LinkedIn",
        url="https://www.linkedin.com/in/fulano",
        snippet="Public search result snippet",
        source_type="search",
    )
    lead = Lead(
        company_name=company.company_name,
        company_domain=company.company_domain,
        person_name="Fulano Silva",
        title="Head of Marketing",
        linkedin_url=result.url,
        source_url=result.url,
        source_type=result.source_type,
        snippet=result.snippet,
        matched_title="marketing",
        confidence_score=90,
    )

    assert lead.company_name == "Nubank"
