from beautiful_linkedin.scrape_modes import apply_scrape_mode, parse_scrape_mode


def test_parse_scrape_mode_accepts_aliases():
    assert parse_scrape_mode("api") == "api"
    assert parse_scrape_mode("apis") == "api"
    assert parse_scrape_mode("serp-only") == "serp"
    assert parse_scrape_mode("sem-cookies") == "serp"
    assert parse_scrape_mode("cookies") == "cookie"


def test_serp_mode_forces_public_search_without_official_sites_or_apis():
    options = apply_scrape_mode(
        scrape_mode="serp",
        lead_providers=["apollo", "web"],
        official_sites=True,
        web_query_limit=0,
        parallelism=6,
    )

    assert options.lead_providers == ["web"]
    assert options.official_sites is False
    assert options.web_query_limit == 48
    assert options.parallelism == 2
    assert "Sem APIs" in options.label


def test_cookie_mode_forces_cookie_provider_and_disables_web_search():
    options = apply_scrape_mode(
        scrape_mode="cookie",
        lead_providers=["auto"],
        official_sites=True,
        web_query_limit=12,
        parallelism=8,
    )

    assert options.lead_providers == ["linkedin_cookie"]
    assert options.official_sites is False
    assert options.web_query_limit == 0
    assert options.parallelism == 1
    assert "cookie" in options.label.lower()


def test_api_mode_preserves_selected_providers():
    options = apply_scrape_mode(
        scrape_mode="api",
        lead_providers=["apollo", "web"],
        official_sites=True,
        web_query_limit=4,
        parallelism=7,
    )

    assert options.lead_providers == ["apollo", "web"]
    assert options.official_sites is True
    assert options.web_query_limit == 4
    assert options.parallelism == 7
