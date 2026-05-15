from beautiful_linkedin.models import CompanyInput
from beautiful_linkedin.processing.lead_extractor import match_target_title
from beautiful_linkedin.processing.title_aliases import expand_deep_search_title_terms
from beautiful_linkedin.providers.people_data_labs import PeopleDataLabsProvider


def test_software_aliases_match_portuguese_and_english_titles():
    assert match_target_title("Desenvolvedor Backend Senior", ["Software"]) == "Software"
    assert match_target_title("Software Engineer", ["Software"]) == "Software"
    assert match_target_title("Analista de Sistemas", ["Software"]) == "Software"
    assert match_target_title("Software Analyst", ["Software"]) == "Software"
    assert match_target_title("Systems Analyst", ["Software"]) == "Software"
    assert match_target_title("Analista de Software", ["Software"]) == "Software"


def test_software_deep_search_includes_general_analyst_keywords():
    terms = expand_deep_search_title_terms(["Software"])

    assert "software analyst" in terms
    assert "analyst software" in terms
    assert "systems analyst" in terms
    assert "analista de software" in terms
    assert "analista" in terms


def test_pdl_payload_expands_software_title_terms():
    company = CompanyInput(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        linkedin_url=None,
        titles=["Software"],
    )

    sql = PeopleDataLabsProvider("token")._payload(company, max_results=25, offset=0)["sql"]
    payload = PeopleDataLabsProvider("token")._payload(company, max_results=25, offset=0)

    assert "job_title LIKE '%software engineer%'" in sql
    assert "job_title LIKE '%desenvolvedor%'" in sql
    assert "job_title LIKE '%analista de sistemas%'" in sql
    assert "from" not in payload
