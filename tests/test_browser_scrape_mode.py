import pytest

from beautiful_linkedin.cli import (
    is_risky_run,
    parse_functions,
    parse_lead_providers,
    parse_seniority,
)
from beautiful_linkedin.config import Settings
from beautiful_linkedin.processing.function_taxonomy import JobFunction
from beautiful_linkedin.processing.seniority_taxonomy import Seniority
from beautiful_linkedin.providers.factory import build_lead_providers
from beautiful_linkedin.providers.linkedin_playwright import (
    LinkedInPlaywrightProvider,
)
from beautiful_linkedin.scrape_modes import apply_scrape_mode, parse_scrape_mode


def test_parse_scrape_mode_accepts_browser_aliases():
    assert parse_scrape_mode("browser") == "browser"
    assert parse_scrape_mode("navegador") == "browser"
    assert parse_scrape_mode("playwright") == "browser"
    assert parse_scrape_mode("arriscado") == "browser"


def test_browser_mode_forces_playwright_provider_and_warns():
    options = apply_scrape_mode(
        scrape_mode="browser",
        lead_providers=["auto"],
        official_sites=True,
        web_query_limit=20,
        parallelism=8,
    )
    assert options.lead_providers == ["linkedin_playwright"]
    assert options.official_sites is False
    assert options.web_query_limit == 0
    assert options.parallelism == 1
    assert "ARRISCADO" in options.label


def test_parse_lead_providers_accepts_playwright_aliases():
    for alias in ("linkedin_playwright", "playwright", "browser", "navegador"):
        providers = parse_lead_providers(alias)
        assert providers == [alias.lower()]


def test_factory_creates_playwright_provider_with_cookie():
    settings = Settings(
        linkedin_li_at_cookie="AQED-cookie",
        linkedin_cookie_browser="none",
    )
    providers = build_lead_providers(
        settings, None, ["linkedin_playwright"], web_query_limit=0
    )
    assert len(providers) == 1
    assert isinstance(providers[0], LinkedInPlaywrightProvider)
    assert providers[0].cookie == "AQED-cookie"


def test_is_risky_run_true_for_browser_mode():
    assert is_risky_run("browser", ["auto"]) is True


def test_is_risky_run_true_when_provider_is_risky():
    assert is_risky_run("api", ["linkedin_playwright"]) is True
    assert is_risky_run("api", ["browser"]) is True


def test_is_risky_run_false_for_safe_setup():
    assert is_risky_run("api", ["pdl", "web"]) is False
    assert is_risky_run("serp", ["web"]) is False


def test_parse_seniority_aliases():
    assert parse_seniority("c_level,vp") == [Seniority.C_LEVEL, Seniority.VP]
    assert parse_seniority("diretor,gerente") == [Seniority.DIRECTOR, Seniority.MANAGER]
    assert parse_seniority(None) == []
    assert parse_seniority("") == []


def test_parse_seniority_unknown_value_raises():
    import typer

    with pytest.raises(typer.BadParameter):
        parse_seniority("ninja")


def test_parse_functions_aliases():
    assert parse_functions("marketing,vendas") == [
        JobFunction.MARKETING,
        JobFunction.SALES,
    ]
    assert parse_functions("rh,engenharia") == [
        JobFunction.HR,
        JobFunction.ENGINEERING,
    ]


def test_parse_functions_unknown_value_raises():
    import typer

    with pytest.raises(typer.BadParameter):
        parse_functions("alquimia")
