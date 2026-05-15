from typer.testing import CliRunner

from beautiful_linkedin.cli import app


runner = CliRunner()


def _patch_output(monkeypatch):
    monkeypatch.setattr("beautiful_linkedin.cli.print_banner", lambda *args, **kwargs: None)
    monkeypatch.setattr("beautiful_linkedin.cli.print_run_summary", lambda *args, **kwargs: None)
    monkeypatch.setattr("beautiful_linkedin.cli.print_final_summary", lambda *args, **kwargs: None)
    monkeypatch.setattr("beautiful_linkedin.cli.print_leads_preview", lambda *args, **kwargs: None)


def test_search_command_applies_serp_only_mode(monkeypatch):
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
                top_sources = {"web_search": 0}

        return Result()

    _patch_output(monkeypatch)
    monkeypatch.setattr("beautiful_linkedin.cli.run_prospecting", fake_run_prospecting)

    result = runner.invoke(
        app,
        [
            "search",
            "--company-name",
            "Nubank",
            "--titles",
            "marketing",
            "--scrape-mode",
            "serp",
            "--lead-providers",
            "apollo",
            "--official-sites",
            "true",
            "--web-query-limit",
            "0",
            "--output",
            "output/test.csv",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["lead_providers"] == ["web"]
    assert calls["search_engines"] == ["auto"]
    assert calls["official_sites"] is False
    assert calls["web_query_limit"] == 48


def test_search_command_applies_cookie_mode_with_explicit_cookie(monkeypatch):
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
                top_sources = {"linkedin_cookie": 0}

        return Result()

    _patch_output(monkeypatch)
    monkeypatch.setattr("beautiful_linkedin.cli.run_prospecting", fake_run_prospecting)

    result = runner.invoke(
        app,
        [
            "search",
            "--company-name",
            "Nubank",
            "--linkedin-url",
            "https://www.linkedin.com/company/nubank/",
            "--titles",
            "marketing",
            "--scrape-mode",
            "cookie",
            "--linkedin-cookie",
            "AQED-cookie",
            "--linkedin-cookie-browser",
            "none",
            "--output",
            "output/test.csv",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["lead_providers"] == ["linkedin_cookie"]
    assert calls["official_sites"] is False
    assert calls["web_query_limit"] == 0
    assert calls["parallelism"] == 1
    assert calls["linkedin_cookie"] == "AQED-cookie"
    assert calls["linkedin_cookie_browser"] == "none"
