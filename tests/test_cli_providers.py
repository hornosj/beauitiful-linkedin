from beautiful_linkedin.cli import (
    linkedin_scraper_panel,
    parse_lead_providers,
    run_linkedin_scraper_with_apify,
)
from beautiful_linkedin.providers.factory import build_lead_providers
from beautiful_linkedin.config import Settings
from beautiful_linkedin.providers.apify_linkedin import ApifyLinkedInEmployeesProvider
from beautiful_linkedin.providers.apollo import ApolloProvider
from beautiful_linkedin.providers.common_crawl import CommonCrawlProvider
from beautiful_linkedin.providers.linkedin_cookie import LinkedInCookieEmployeesProvider
from beautiful_linkedin.providers.linkedin_sales_navigator import (
    LinkedInSalesNavigatorProvider,
)
from beautiful_linkedin.providers.lusha import LushaProvider
from beautiful_linkedin.providers.public_directories import PublicDirectoriesProvider

def test_parse_lead_providers_accepts_apollo_and_lusha():
    providers = parse_lead_providers("apollo, lusha")
    assert "apollo" in providers
    assert "lusha" in providers

def test_parse_lead_providers_accepts_apify_linkedin():
    providers = parse_lead_providers("apify_linkedin")
    assert providers == ["apify_linkedin"]

def test_parse_lead_providers_accepts_linkedin_cookie():
    providers = parse_lead_providers("linkedin_cookie")
    assert providers == ["linkedin_cookie"]

def test_parse_lead_providers_accepts_linkedin_sales_navigator_aliases():
    for alias in ("linkedin_sales_navigator", "sales_navigator", "sales_nav", "salesnav"):
        providers = parse_lead_providers(alias)
        assert providers == [alias.lower()]

def test_parse_lead_providers_accepts_public_directories_aliases():
    for alias in ("public_directories", "public_dirs", "theorg", "rocketreach_public"):
        providers = parse_lead_providers(alias)
        assert providers == [alias.lower()]

def test_parse_lead_providers_accepts_common_crawl_aliases():
    for alias in ("common_crawl", "commoncrawl", "cc"):
        providers = parse_lead_providers(alias)
        assert providers == [alias.lower()]

def test_factory_creates_sales_navigator_provider():
    settings = Settings(linkedin_li_at_cookie="AQED-cookie", linkedin_cookie_browser="none")

    providers = build_lead_providers(
        settings, None, ["linkedin_sales_navigator"], web_query_limit=0
    )

    assert len(providers) == 1
    assert isinstance(providers[0], LinkedInSalesNavigatorProvider)
    assert providers[0].cookie == "AQED-cookie"

def test_parse_lead_providers_accepts_all_auto():
    providers = parse_lead_providers("pdl,coresignal,apollo,lusha,web")
    assert "apollo" in providers
    assert "lusha" in providers

def test_factory_ignores_missing_keys():
    settings = Settings(apollo_api_key=None, lusha_api_key=None)
    providers = build_lead_providers(settings, None, ["apollo", "lusha"], web_query_limit=0)
    assert len(providers) == 0

def test_factory_creates_providers_when_keys_present():
    settings = Settings(apollo_api_key="key1", lusha_api_key="key2")
    providers = build_lead_providers(settings, None, ["apollo", "lusha"])
    assert len(providers) == 2
    assert any(isinstance(p, ApolloProvider) for p in providers)
    assert any(isinstance(p, LushaProvider) for p in providers)

def test_factory_creates_apify_linkedin_when_key_present():
    settings = Settings(apify_api_key="apify")

    providers = build_lead_providers(settings, None, ["apify_linkedin"])

    assert len(providers) == 1
    assert isinstance(providers[0], ApifyLinkedInEmployeesProvider)

def test_factory_creates_linkedin_cookie_provider_without_external_api_key():
    settings = Settings(linkedin_li_at_cookie="AQED-cookie", linkedin_cookie_browser="none")

    providers = build_lead_providers(settings, None, ["linkedin_cookie"], web_query_limit=0)

    assert len(providers) == 1
    assert isinstance(providers[0], LinkedInCookieEmployeesProvider)
    assert providers[0].cookie == "AQED-cookie"

def test_factory_creates_public_directories_provider():
    settings = Settings()

    providers = build_lead_providers(settings, None, ["public_directories"], web_query_limit=0)

    assert len(providers) == 1
    assert isinstance(providers[0], PublicDirectoriesProvider)

def test_factory_creates_common_crawl_provider():
    settings = Settings()

    providers = build_lead_providers(settings, None, ["common_crawl"], web_query_limit=0)

    assert len(providers) == 1
    assert isinstance(providers[0], CommonCrawlProvider)

def test_factory_auto_includes_all_configured_structured_apis():
    settings = Settings(
        people_data_labs_api_key="pdl",
        coresignal_api_key="core",
        apollo_api_key="apollo",
        lusha_api_key="lusha",
    )

    providers = build_lead_providers(settings, None, ["auto"], web_query_limit=0)

    assert [provider.name for provider in providers] == [
        "pdl",
        "coresignal",
        "apollo",
        "lusha",
    ]

def test_linkedin_scraper_panel_points_to_apify_provider():
    panel = linkedin_scraper_panel()
    rendered = str(panel.renderable)

    assert "apify_linkedin" in rendered
    assert "APIFY_API_KEY" in rendered
    assert "não usa PDL" in rendered

def test_linkedin_scraper_runner_uses_only_apify_provider(monkeypatch):
    calls = {}

    def fake_run_prospecting(**kwargs):
        calls.update(kwargs)

        class Result:
            leads = []

            class summary:
                total_companies_processed = 1
                total_raw_leads = 0
                total_deduplicated_leads = 0
                output_file = "output/test.csv"
                top_sources = {"api_apify_linkedin": 0}

        return Result()

    monkeypatch.setattr("beautiful_linkedin.cli.run_prospecting", fake_run_prospecting)
    monkeypatch.setattr("beautiful_linkedin.cli.print_final_summary", lambda *args, **kwargs: None)
    monkeypatch.setattr("beautiful_linkedin.cli.print_leads_preview", lambda *args, **kwargs: None)
    monkeypatch.setattr("beautiful_linkedin.cli.print_run_summary", lambda *args, **kwargs: None)

    run_linkedin_scraper_with_apify(
        company_name="BTG Pactual",
        linkedin_url="https://www.linkedin.com/company/btg-pactual/",
        titles=["marketing", "growth"],
        max_results=25,
        include_uncertain=True,
        output_path="output/test.csv",
        output_format="csv",
        provider_timeout=240,
        use_cache=True,
    )

    assert calls["lead_providers"] == ["apify_linkedin"]
    assert calls["official_sites"] is False
    assert calls["web_query_limit"] == 0
    assert calls["parallelism"] == 1
