from beautiful_linkedin.models import CompanyInput
from beautiful_linkedin.search.query_builder import (
    build_balanced_queries_for_company,
    build_queries_for_company,
)


def test_query_builder_generates_layered_queries():
    company = CompanyInput(
        company_name="Nubank",
        company_domain="nubank.com.br",
        linkedin_url="https://www.linkedin.com/company/nubank",
        titles=["marketing"],
    )

    queries = build_queries_for_company(company, depth="standard")

    assert queries[:5] == [
        'site:linkedin.com/in "Nubank" "marketing"',
        'inurl:linkedin.com/in "Nubank" "marketing"',
        '"Nubank" "marketing" linkedin.com/in',
        '"Nubank" "marketing" "LinkedIn"',
        '"Nubank" "marketing" site:linkedin.com',
    ]


def test_query_builder_skips_domain_queries_when_domain_missing():
    company = CompanyInput(
        company_name="Nubank",
        company_domain=None,
        linkedin_url=None,
        titles=["growth"],
    )

    queries = build_queries_for_company(company, depth="standard")

    assert queries[:5] == [
        'site:linkedin.com/in "Nubank" "growth"',
        'inurl:linkedin.com/in "Nubank" "growth"',
        '"Nubank" "growth" linkedin.com/in',
        '"Nubank" "growth" "LinkedIn"',
        '"Nubank" "growth" site:linkedin.com',
    ]


def test_query_builder_expands_common_short_portuguese_titles():
    company = CompanyInput(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        linkedin_url="https://www.linkedin.com/company/safra-banco/",
        titles=["rh"],
    )

    queries = build_queries_for_company(company, depth="standard")

    assert 'site:linkedin.com/in "Banco Safra" "rh"' in queries
    assert 'site:linkedin.com/in "Banco Safra" "recursos humanos"' in queries
    assert '"Banco Safra" "human resources" linkedin.com/in' in queries
    assert '"safra.com.br" "people" linkedin.com/in' not in queries


def test_query_builder_deep_mode_adds_more_linkedin_profile_variants():
    company = CompanyInput(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        linkedin_url="https://www.linkedin.com/company/safra-banco/",
        titles=["rh"],
    )

    standard_queries = build_queries_for_company(company, depth="standard")
    deep_queries = build_queries_for_company(company, depth="deep")

    assert len(deep_queries) > len(standard_queries)
    assert 'site:br.linkedin.com/in "Banco Safra" "rh"' in deep_queries
    assert '"Banco Safra" "rh" "Ver o perfil"' in deep_queries
    assert '"Banco Safra" "talent acquisition" "LinkedIn"' in deep_queries


def test_query_builder_expands_software_titles_for_deep_search():
    company = CompanyInput(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        linkedin_url="https://www.linkedin.com/company/safra-banco/",
        titles=["Software"],
    )

    queries = build_queries_for_company(company, depth="deep")

    assert '"Banco Safra" "software engineer" linkedin.com/in' in queries
    assert '"Banco Safra" "desenvolvedor" "LinkedIn"' in queries
    assert '"Banco Safra" "analista de sistemas" "Ver o perfil"' in queries


def test_balanced_query_builder_spreads_early_queries_across_titles():
    company = CompanyInput(
        company_name="Nubank",
        company_domain="nubank.com.br",
        linkedin_url="https://www.linkedin.com/company/nubank/",
        titles=[
            "account executive",
            "bdr",
            "business development",
            "comercial",
            "head of sales",
            "sales",
            "sdr",
            "vendas",
        ],
    )

    queries = build_balanced_queries_for_company(company, depth="deep")
    first_queries = queries[: len(company.titles)]

    for title in company.titles:
        assert any(f'"{title}"' in query for query in first_queries)


def test_query_builder_adds_mercado_livre_identity_variants():
    company = CompanyInput(
        company_name="Mercado Livre",
        company_domain="mercadolivre.com.br",
        linkedin_url="https://www.linkedin.com/company/mercadolivre-com/",
        titles=["marketing"],
    )

    queries = build_queries_for_company(company, depth="standard")

    assert 'site:linkedin.com/in "Mercado Livre Brasil" "marketing"' in queries
    assert 'site:linkedin.com/in "Mercado Libre" "marketing"' in queries
    assert 'site:linkedin.com/in "mercadolivre" "marketing"' in queries


def test_balanced_query_builder_reaches_mercado_livre_variants_before_empty_abort_window():
    company = CompanyInput(
        company_name="Mercado Livre",
        company_domain="mercadolivre.com.br",
        linkedin_url="https://www.linkedin.com/company/mercadolivre-com/",
        titles=["marketing"],
    )

    queries = build_balanced_queries_for_company(company, depth="standard")
    first_abort_window = queries[:8]

    assert any("Mercado Livre Brasil" in query for query in first_abort_window)
    assert any("Mercado Libre" in query for query in first_abort_window)
