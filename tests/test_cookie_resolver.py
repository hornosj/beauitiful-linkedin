from beautiful_linkedin.cookie_resolver import (
    extract_li_at_cookie_value,
    resolve_linkedin_li_at_cookie,
)


def test_extract_li_at_cookie_value_accepts_raw_cookie_value():
    assert extract_li_at_cookie_value("AQED-raw-value") == "AQED-raw-value"


def test_extract_li_at_cookie_value_accepts_full_cookie_header():
    assert (
        extract_li_at_cookie_value("bcookie=abc; li_at=AQED-session; JSESSIONID=ajax")
        == "AQED-session"
    )


def test_resolve_linkedin_cookie_prefers_explicit_value(monkeypatch):
    monkeypatch.setenv("LINKEDIN_LI_AT_COOKIE", "AQED-from-env")

    assert resolve_linkedin_li_at_cookie("li_at=AQED-explicit;") == "AQED-explicit"


def test_resolve_linkedin_cookie_uses_environment(monkeypatch):
    monkeypatch.setenv("LINKEDIN_LI_AT_COOKIE", "AQED-from-env")

    assert resolve_linkedin_li_at_cookie(None) == "AQED-from-env"


def test_resolve_linkedin_cookie_returns_none_when_auto_lookup_fails(monkeypatch):
    monkeypatch.delenv("LINKEDIN_LI_AT_COOKIE", raising=False)
    monkeypatch.delenv("LINKEDIN_COOKIE", raising=False)
    monkeypatch.delenv("LI_AT", raising=False)

    assert resolve_linkedin_li_at_cookie(None, browser="none") is None


def test_browser_priority_puts_explicit_choice_first():
    from beautiful_linkedin.cookie_resolver import resolve_browser_priority

    order = resolve_browser_priority(requested="chrome", default_detector=lambda: "edge")
    assert order[0] == "chrome"


def test_browser_priority_uses_default_when_auto():
    from beautiful_linkedin.cookie_resolver import resolve_browser_priority

    order = resolve_browser_priority(requested="auto", default_detector=lambda: "edge")
    assert order[0] == "edge"
    # Other common browsers must be tried as fallbacks, no duplicates.
    assert order.count("edge") == 1
    assert {"chrome", "firefox"}.issubset(set(order))


def test_browser_priority_handles_unknown_default():
    from beautiful_linkedin.cookie_resolver import resolve_browser_priority

    order = resolve_browser_priority(requested="auto", default_detector=lambda: None)
    # Falls back to a sensible cross-browser order without crashing.
    assert "chrome" in order
    assert "edge" in order


def test_detect_windows_default_browser_maps_progid_to_known_name():
    from beautiful_linkedin.cookie_resolver import (
        _windows_default_browser_from_progid,
    )

    assert _windows_default_browser_from_progid("ChromeHTML") == "chrome"
    assert _windows_default_browser_from_progid("MSEdgeHTM") == "edge"
    assert _windows_default_browser_from_progid("BraveHTML") == "brave"
    assert _windows_default_browser_from_progid("FirefoxURL-308046B0AF4A39CB") == "firefox"
    assert _windows_default_browser_from_progid("OperaStable") == "opera"
    assert _windows_default_browser_from_progid("Unknown-9z") is None


def test_resolve_uses_rookiepy_when_available(monkeypatch):
    from beautiful_linkedin import cookie_resolver

    monkeypatch.delenv("LINKEDIN_LI_AT_COOKIE", raising=False)
    monkeypatch.delenv("LINKEDIN_COOKIE", raising=False)
    monkeypatch.delenv("LI_AT", raising=False)

    captured: list[str] = []

    def fake_rookiepy_reader(browser_name, domains):
        captured.append(browser_name)
        if browser_name == "chrome":
            return [
                {"name": "bcookie", "value": "abc", "domain": ".linkedin.com"},
                {"name": "li_at", "value": "AQED-from-rookie", "domain": ".linkedin.com"},
            ]
        return []

    def fake_browser_cookie3_reader(browser_name):
        raise AssertionError("browser_cookie3 should not be invoked when rookiepy succeeds")

    monkeypatch.setattr(cookie_resolver, "_read_with_rookiepy", fake_rookiepy_reader)
    monkeypatch.setattr(cookie_resolver, "_read_with_browser_cookie3", fake_browser_cookie3_reader)
    monkeypatch.setattr(
        cookie_resolver,
        "resolve_browser_priority",
        lambda requested, default_detector=None: ["chrome", "edge", "firefox"],
    )

    assert resolve_linkedin_li_at_cookie(None, browser="auto") == "AQED-from-rookie"
    assert captured[0] == "chrome"


def test_resolve_falls_back_to_browser_cookie3_when_rookiepy_empty(monkeypatch):
    from beautiful_linkedin import cookie_resolver

    monkeypatch.delenv("LINKEDIN_LI_AT_COOKIE", raising=False)
    monkeypatch.delenv("LINKEDIN_COOKIE", raising=False)
    monkeypatch.delenv("LI_AT", raising=False)

    monkeypatch.setattr(
        cookie_resolver, "_read_with_rookiepy", lambda browser_name, domains: []
    )

    bc3_calls: list[str] = []

    def fake_bc3(browser_name):
        bc3_calls.append(browser_name)
        if browser_name == "edge":
            return "AQED-from-bc3"
        return None

    monkeypatch.setattr(cookie_resolver, "_read_with_browser_cookie3", fake_bc3)
    monkeypatch.setattr(
        cookie_resolver,
        "resolve_browser_priority",
        lambda requested, default_detector=None: ["chrome", "edge"],
    )

    assert resolve_linkedin_li_at_cookie(None, browser="auto") == "AQED-from-bc3"
    assert bc3_calls == ["chrome", "edge"]


def test_resolve_returns_none_with_diagnostic_when_all_backends_fail(monkeypatch, caplog):
    import logging

    from beautiful_linkedin import cookie_resolver

    monkeypatch.delenv("LINKEDIN_LI_AT_COOKIE", raising=False)
    monkeypatch.delenv("LINKEDIN_COOKIE", raising=False)
    monkeypatch.delenv("LI_AT", raising=False)

    monkeypatch.setattr(
        cookie_resolver, "_read_with_rookiepy", lambda browser_name, domains: []
    )
    monkeypatch.setattr(
        cookie_resolver, "_read_with_browser_cookie3", lambda browser_name: None
    )
    monkeypatch.setattr(
        cookie_resolver,
        "resolve_browser_priority",
        lambda requested, default_detector=None: ["chrome", "edge"],
    )

    caplog.set_level(logging.WARNING, logger="beautiful_linkedin.cookie_resolver")
    result = resolve_linkedin_li_at_cookie(None, browser="auto")
    assert result is None
    full_log = " ".join(record.getMessage() for record in caplog.records)
    # Helpful hints surface in the warning
    assert "li_at" in full_log.lower()
    assert "linkedin" in full_log.lower()
