from beautiful_linkedin.models import CompanyInput, SearchResult
from beautiful_linkedin.processing.lead_extractor import (
    extract_leads_from_company_site_text,
    extract_leads_from_search_results,
)


def test_company_site_extractor_does_not_create_generic_partial_lead_when_uncertain():
    company = CompanyInput(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        linkedin_url=None,
        titles=["rh"],
    )

    leads = extract_leads_from_company_site_text(
        company=company,
        source_url="https://safra.com.br/",
        text="Conheça os produtos e serviços de pessoa jurídica do Safra",
        include_uncertain=True,
    )

    assert leads == []


def test_search_extractor_handles_linkedin_locale_profile_urls():
    company = CompanyInput(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        linkedin_url=None,
        titles=["rh"],
    )
    results = [
        SearchResult(
            title="Fulana Silva - Gerente de Recursos Humanos - Banco Safra | LinkedIn",
            url="https://br.linkedin.com/in/fulana-silva",
            snippet="Fulana Silva - Gerente de Recursos Humanos - Banco Safra",
            source_type="search",
        )
    ]

    leads = extract_leads_from_search_results(company, results, include_uncertain=False)

    assert len(leads) == 1
    assert leads[0].person_name == "Fulana Silva"
    assert leads[0].matched_title == "rh"


def test_search_extractor_parses_view_profile_titles():
    company = CompanyInput(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        linkedin_url=None,
        titles=["rh"],
    )
    results = [
        SearchResult(
            title="Veja o perfil de Silvana Tinti no LinkedIn",
            url="https://br.linkedin.com/in/silvana-tinti",
            snippet="Silvana Tinti - Banco Safra",
            source_type="search_duckduckgo",
        )
    ]

    leads = extract_leads_from_search_results(company, results, include_uncertain=True)

    assert len(leads) == 1
    assert leads[0].person_name == "Silvana Tinti"


def test_search_extractor_keeps_title_when_result_starts_with_role():
    company = CompanyInput(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        linkedin_url=None,
        titles=["rh"],
    )
    results = [
        SearchResult(
            title="Superintendente Geral de RH | Banco Safra - LinkedIn",
            url="https://br.linkedin.com/in/juliana-martins",
            snippet="Public search result",
            source_type="search_duckduckgo",
        )
    ]

    leads = extract_leads_from_search_results(company, results, include_uncertain=True)

    assert len(leads) == 1
    assert leads[0].title == "Superintendente Geral de RH"


def test_search_extractor_uses_public_profile_url_slug_when_name_is_missing():
    company = CompanyInput(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        linkedin_url=None,
        titles=["rh"],
    )
    results = [
        SearchResult(
            title="Superintendente Geral de RH | Banco Safra - LinkedIn",
            url="https://br.linkedin.com/in/juliana-martins-0a141b44",
            snippet="Public search result",
            source_type="search_duckduckgo",
        )
    ]

    leads = extract_leads_from_search_results(company, results, include_uncertain=True)

    assert leads[0].person_name == "Juliana Martins"


def test_search_extractor_marks_sales_name_matches_as_maybe_incorrect():
    company = CompanyInput(
        company_name="XP Inc",
        company_domain="xpi.com.br",
        linkedin_url=None,
        titles=["sales"],
    )
    results = [
        SearchResult(
            title="Joao Sales - XP Inc | LinkedIn",
            url="https://br.linkedin.com/in/joao-sales",
            snippet="Joao Sales - XP Inc",
            source_type="search",
        ),
        SearchResult(
            title="Sales Barbosa - XP Inc | LinkedIn",
            url="https://br.linkedin.com/in/sales-barbosa",
            snippet="Sales Barbosa - XP Inc",
            source_type="search",
        ),
        SearchResult(
            title="joao sales - xp inc | LinkedIn",
            url="https://br.linkedin.com/in/joao-sales",
            snippet="joao sales - xp inc",
            source_type="search",
        ),
    ]

    leads = extract_leads_from_search_results(company, results, include_uncertain=False)

    assert len(leads) == 3
    assert all(lead.matched_title is None for lead in leads)
    assert all(lead.validation_status == "maybe_incorrect" for lead in leads)
    assert all("Talvez incorreto" in (lead.validation_note or "") for lead in leads)


def test_search_extractor_keeps_sales_when_it_is_in_the_title_context():
    company = CompanyInput(
        company_name="XP Inc",
        company_domain="xpi.com.br",
        linkedin_url=None,
        titles=["sales"],
    )
    results = [
        SearchResult(
            title="Maria Silva - Head of Sales - XP Inc | LinkedIn",
            url="https://br.linkedin.com/in/maria-silva",
            snippet="Maria Silva - Head of Sales - XP Inc",
            source_type="search",
        )
    ]

    leads = extract_leads_from_search_results(company, results, include_uncertain=False)

    assert len(leads) == 1
    assert leads[0].person_name == "Maria Silva"
    assert leads[0].title == "Head of Sales"
    assert leads[0].matched_title == "sales"
    assert leads[0].validation_status == "valid"
    assert leads[0].validation_note is None


def test_search_extractor_marks_sales_name_with_unrelated_title_as_maybe_incorrect():
    company = CompanyInput(
        company_name="XP Inc",
        company_domain="xpi.com.br",
        linkedin_url=None,
        titles=["sales"],
    )
    results = [
        SearchResult(
            title="Joao Sales - Marketing Analyst - XP Inc | LinkedIn",
            url="https://br.linkedin.com/in/joao-sales",
            snippet="Joao Sales - Marketing Analyst - XP Inc",
            source_type="search",
        )
    ]

    leads = extract_leads_from_search_results(company, results, include_uncertain=False)

    assert len(leads) == 1
    assert leads[0].title == "Marketing Analyst"
    assert leads[0].matched_title is None
    assert leads[0].validation_status == "maybe_incorrect"
    assert "Talvez incorreto" in (leads[0].validation_note or "")


def test_search_extractor_marks_function_keywords_in_names_or_company_segments_as_maybe_incorrect():
    cases = [
        ("product", "Ana Product - XP Inc | LinkedIn", "Ana Product - XP Inc"),
        ("people", "People Costa - XP Inc | LinkedIn", "People Costa - XP Inc"),
        ("data", "Data Silva - XP Inc | LinkedIn", "Data Silva - XP Inc"),
        ("legal", "Legal Santos - XP Inc | LinkedIn", "Legal Santos - XP Inc"),
        ("growth", "Rafael Growth - XP Inc | LinkedIn", "Rafael Growth - XP Inc"),
        ("people", "Ana Silva - People | LinkedIn", "Ana Silva - People"),
        ("product", "Ana Silva - Product | LinkedIn", "Ana Silva - Product"),
        ("data", "Ana Silva - Data | LinkedIn", "Ana Silva - Data"),
    ]

    for title, result_title, snippet in cases:
        company = CompanyInput(
            company_name="XP Inc",
            company_domain="xpi.com.br",
            linkedin_url=None,
            titles=[title],
        )
        results = [
            SearchResult(
                title=result_title,
                url=f"https://br.linkedin.com/in/{title}-case",
                snippet=snippet,
                source_type="search",
            )
        ]

        leads = extract_leads_from_search_results(company, results, include_uncertain=False)

        assert len(leads) == 1, title
        assert leads[0].matched_title is None, title
        assert leads[0].validation_status == "maybe_incorrect", title
        assert "Talvez incorreto" in (leads[0].validation_note or ""), title


def test_search_extractor_keeps_function_keywords_in_real_title_segments():
    cases = [
        ("people", "Ana Silva - Head of People - XP Inc | LinkedIn", "Head of People"),
        ("product", "Bruno Lima - Product Manager - XP Inc | LinkedIn", "Product Manager"),
        ("data", "Carla Souza - Data Analyst - XP Inc | LinkedIn", "Data Analyst"),
        ("legal", "Daniel Costa - Legal Counsel - XP Inc | LinkedIn", "Legal Counsel"),
        ("growth", "Eva Moraes - Growth Marketing Manager - XP Inc | LinkedIn", "Growth Marketing Manager"),
        ("design", "Fabio Rocha - Product Designer - XP Inc | LinkedIn", "Product Designer"),
    ]

    for title, result_title, expected_title in cases:
        company = CompanyInput(
            company_name="XP Inc",
            company_domain="xpi.com.br",
            linkedin_url=None,
            titles=[title],
        )
        results = [
            SearchResult(
                title=result_title,
                url=f"https://br.linkedin.com/in/{title}-real-role",
                snippet=result_title,
                source_type="search",
            )
        ]

        leads = extract_leads_from_search_results(company, results, include_uncertain=False)

        assert len(leads) == 1, title
        assert leads[0].title == expected_title
        assert leads[0].matched_title == title
        assert leads[0].validation_status == "valid"
