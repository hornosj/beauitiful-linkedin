from __future__ import annotations

import pytest

from beautiful_linkedin.models import CompanyInput
from beautiful_linkedin.providers.linkedin_people_search import (
    LinkedInPeopleSearchProvider,
    PeopleCard,
    PeopleSearchOptions,
    build_people_search_url,
    extract_cards_from_html,
)


# ---------------------------------------------------------------------------
# extract_cards_from_html
# ---------------------------------------------------------------------------

LINKEDIN_PEOPLE_HTML_PRIMARY = """
<html><body>
<ul>
  <li class="org-people-profile-card__profile-card-spacing">
    <div class="artdeco-entity-lockup">
      <a class="app-aware-link" href="/in/ana-silva-marketing/?trk=foo">
        <div class="artdeco-entity-lockup__title">Ana Silva</div>
      </a>
      <div class="artdeco-entity-lockup__subtitle">Head of Marketing at Nubank</div>
      <div class="artdeco-entity-lockup__caption">São Paulo, Brasil</div>
    </div>
  </li>
  <li class="org-people-profile-card__profile-card-spacing">
    <div class="artdeco-entity-lockup">
      <a class="app-aware-link" href="https://www.linkedin.com/in/joao-growth/">
        <div class="artdeco-entity-lockup__title">João Growth</div>
      </a>
      <div class="artdeco-entity-lockup__subtitle">Growth Manager · Nubank</div>
      <div class="artdeco-entity-lockup__caption">Rio de Janeiro</div>
    </div>
  </li>
</ul>
</body></html>
"""


def test_extract_cards_finds_primary_layout():
    cards = extract_cards_from_html(LINKEDIN_PEOPLE_HTML_PRIMARY)

    assert len(cards) == 2
    ana, joao = cards
    assert ana.full_name == "Ana Silva"
    assert ana.headline == "Head of Marketing at Nubank"
    assert ana.location == "São Paulo, Brasil"
    assert ana.profile_url == "https://www.linkedin.com/in/ana-silva-marketing/"

    assert joao.full_name == "João Growth"
    assert joao.headline == "Growth Manager · Nubank"
    assert joao.profile_url == "https://www.linkedin.com/in/joao-growth/"


LINKEDIN_PEOPLE_HTML_FALLBACK = """
<html><body>
<div class="search-results-container">
  <div class="random-card-class">
    <a href="/in/maria-rh">
      <span class="entity-result__title-text">Maria RH</span>
    </a>
    <div class="entity-result__primary-subtitle">People Operations</div>
    <div class="entity-result__secondary-subtitle">São Paulo, Brasil</div>
  </div>
</div>
</body></html>
"""


def test_extract_cards_falls_back_when_primary_layout_missing():
    cards = extract_cards_from_html(LINKEDIN_PEOPLE_HTML_FALLBACK)

    assert len(cards) == 1
    card = cards[0]
    assert card.profile_url == "https://www.linkedin.com/in/maria-rh/"
    assert card.full_name == "Maria RH"
    assert card.headline == "People Operations"
    assert card.location == "São Paulo, Brasil"


def test_extract_cards_dedupes_by_profile_url():
    html = """
    <ul>
      <li class="org-people-profile-card__profile-card-spacing">
        <a href="/in/duplicada/?trk=a">
          <div class="artdeco-entity-lockup__title">Pessoa A</div>
        </a>
        <div class="artdeco-entity-lockup__subtitle">Engenheira</div>
      </li>
      <li class="org-people-profile-card__profile-card-spacing">
        <a href="https://www.linkedin.com/in/duplicada">
          <div class="artdeco-entity-lockup__title">Pessoa A again</div>
        </a>
        <div class="artdeco-entity-lockup__subtitle">Engenheira</div>
      </li>
    </ul>
    """
    cards = extract_cards_from_html(html)

    assert len(cards) == 1
    assert cards[0].profile_url == "https://www.linkedin.com/in/duplicada/"


def test_extract_cards_skips_company_links_and_fragments():
    html = """
    <ul>
      <li class="org-people-profile-card__profile-card-spacing">
        <a href="/company/nubank/">
          <div class="artdeco-entity-lockup__title">Nubank</div>
        </a>
      </li>
      <li class="org-people-profile-card__profile-card-spacing">
        <a href="/in/valida-pessoa/">
          <div class="artdeco-entity-lockup__title">Pessoa Válida</div>
        </a>
        <div class="artdeco-entity-lockup__subtitle">CTO</div>
      </li>
    </ul>
    """
    cards = extract_cards_from_html(html)
    assert [card.profile_url for card in cards] == [
        "https://www.linkedin.com/in/valida-pessoa/"
    ]


def test_extract_cards_returns_empty_for_empty_or_garbage_html():
    assert extract_cards_from_html("") == []
    assert extract_cards_from_html("<html></html>") == []
    assert extract_cards_from_html("<html><body><p>hello</p></body></html>") == []


# ---------------------------------------------------------------------------
# build_people_search_url
# ---------------------------------------------------------------------------


def test_build_people_search_url_with_title():
    assert build_people_search_url("nubank", "head of marketing") == (
        "https://www.linkedin.com/company/nubank/people/?keywords=head+of+marketing"
    )


def test_build_people_search_url_with_title_list_joins_with_commas():
    assert build_people_search_url("nubank", ["rh", "people", "talent"]) == (
        "https://www.linkedin.com/company/nubank/people/?keywords=rh%2Cpeople%2Ctalent"
    )


def test_build_people_search_url_filters_empty_titles_in_list():
    assert build_people_search_url("nubank", ["rh", "  ", ""]) == (
        "https://www.linkedin.com/company/nubank/people/?keywords=rh"
    )


def test_build_people_search_url_without_title():
    assert (
        build_people_search_url("nubank", None)
        == "https://www.linkedin.com/company/nubank/people/"
    )


# ---------------------------------------------------------------------------
# Provider behaviour with a fake fetcher
# ---------------------------------------------------------------------------


class FakeFetcher:
    """Stand-in for the Scrapling/Playwright fetcher used by the provider."""

    def __init__(self, html_by_url: dict[str, str]) -> None:
        self.html_by_url = html_by_url
        self.fetched_urls: list[str] = []

    def fetch_listing(self, url: str, *, li_at: str, scrolls: int) -> str:
        assert li_at, "fetcher must receive the li_at cookie"
        self.fetched_urls.append(url)
        return self.html_by_url.get(url, "")


def test_provider_returns_empty_without_cookie():
    provider = LinkedInPeopleSearchProvider(
        cookie=None,
        cookie_browser="none",
        fetcher=FakeFetcher({}),
    )
    company = CompanyInput(
        company_name="Nubank",
        linkedin_url="https://www.linkedin.com/company/nubank/",
        titles=["marketing"],
    )
    assert provider.find_leads(company, max_results=10, include_uncertain=True) == []


def test_provider_combines_titles_into_single_keywords_request():
    combined_url = (
        "https://www.linkedin.com/company/nubank/people/"
        "?keywords=marketing%2Cgrowth"
    )

    fetcher = FakeFetcher({combined_url: LINKEDIN_PEOPLE_HTML_PRIMARY})

    provider = LinkedInPeopleSearchProvider(
        cookie="AQED" + "x" * 180,
        cookie_browser="none",
        fetcher=fetcher,
        options=PeopleSearchOptions(scrolls=0),
    )
    company = CompanyInput(
        company_name="Nubank",
        linkedin_url="https://www.linkedin.com/company/nubank/",
        titles=["marketing", "growth"],
    )

    leads = provider.find_leads(company, max_results=10, include_uncertain=True)

    # One request only — LinkedIn renders each comma-separated keyword as its
    # own filter chip, so we no longer paginate per title.
    assert fetcher.fetched_urls == [combined_url]
    assert not any("/in/" in url for url in fetcher.fetched_urls)

    urls = [lead.linkedin_url for lead in leads]
    assert "https://www.linkedin.com/in/ana-silva-marketing/" in urls
    assert "https://www.linkedin.com/in/joao-growth/" in urls
    assert all(lead.source_type == "linkedin_people_search" for lead in leads)


def test_provider_dedupes_within_single_response():
    url = (
        "https://www.linkedin.com/company/acme/people/?keywords=eng%2Ccto"
    )
    duplicated_html = """
    <ul>
      <li class="org-people-profile-card__profile-card-spacing">
        <a href="/in/repetida/"><div class="artdeco-entity-lockup__title">Repetida</div></a>
        <div class="artdeco-entity-lockup__subtitle">Engineer at Acme</div>
      </li>
      <li class="org-people-profile-card__profile-card-spacing">
        <a href="/in/repetida/"><div class="artdeco-entity-lockup__title">Repetida</div></a>
        <div class="artdeco-entity-lockup__subtitle">Engineer at Acme</div>
      </li>
    </ul>
    """
    fetcher = FakeFetcher({url: duplicated_html})

    provider = LinkedInPeopleSearchProvider(
        cookie="AQED" + "x" * 180,
        cookie_browser="none",
        fetcher=fetcher,
        options=PeopleSearchOptions(scrolls=0),
    )
    company = CompanyInput(
        company_name="Acme",
        linkedin_url="https://www.linkedin.com/company/acme/",
        titles=["eng", "cto"],
    )

    leads = provider.find_leads(company, max_results=5, include_uncertain=True)
    assert len(leads) == 1
    assert leads[0].linkedin_url == "https://www.linkedin.com/in/repetida/"


def test_provider_respects_max_results():
    url = "https://www.linkedin.com/company/acme/people/?keywords=eng"
    html_with_three = """
    <ul>
      <li class="org-people-profile-card__profile-card-spacing">
        <a href="/in/p1/"><div class="artdeco-entity-lockup__title">P1</div></a>
        <div class="artdeco-entity-lockup__subtitle">Eng</div>
      </li>
      <li class="org-people-profile-card__profile-card-spacing">
        <a href="/in/p2/"><div class="artdeco-entity-lockup__title">P2</div></a>
        <div class="artdeco-entity-lockup__subtitle">Eng</div>
      </li>
      <li class="org-people-profile-card__profile-card-spacing">
        <a href="/in/p3/"><div class="artdeco-entity-lockup__title">P3</div></a>
        <div class="artdeco-entity-lockup__subtitle">Eng</div>
      </li>
    </ul>
    """
    fetcher = FakeFetcher({url: html_with_three})

    provider = LinkedInPeopleSearchProvider(
        cookie="AQED" + "x" * 180,
        cookie_browser="none",
        fetcher=fetcher,
        options=PeopleSearchOptions(scrolls=0),
    )
    company = CompanyInput(
        company_name="Acme",
        linkedin_url="https://www.linkedin.com/company/acme/",
        titles=["eng"],
    )
    leads = provider.find_leads(company, max_results=2, include_uncertain=True)
    assert len(leads) == 2


def test_provider_swallows_fetcher_failure():
    class BoomFetcher:
        def fetch_listing(self, url, *, li_at, scrolls):
            raise RuntimeError("Scrapling/Playwright explodiu")

    provider = LinkedInPeopleSearchProvider(
        cookie="AQED" + "x" * 180,
        cookie_browser="none",
        fetcher=BoomFetcher(),
        options=PeopleSearchOptions(scrolls=0),
    )
    company = CompanyInput(
        company_name="Acme",
        linkedin_url="https://www.linkedin.com/company/acme/",
        titles=["eng"],
    )

    assert provider.find_leads(company, max_results=5, include_uncertain=True) == []


def test_provider_aborts_all_titles_when_session_invalid():
    """If the first fetch raises LinkedInAuthError, skip remaining titles."""
    from beautiful_linkedin.providers.linkedin_people_search import LinkedInAuthError

    calls: list[str] = []

    class AuthErrorFetcher:
        def fetch_listing(self, url, *, li_at, scrolls):
            calls.append(url)
            raise LinkedInAuthError("li_at expirado ou inválido")

    provider = LinkedInPeopleSearchProvider(
        cookie="AQED" + "x" * 180,
        cookie_browser="none",
        fetcher=AuthErrorFetcher(),
        options=PeopleSearchOptions(scrolls=0),
    )
    company = CompanyInput(
        company_name="Acme",
        linkedin_url="https://www.linkedin.com/company/acme/",
        titles=["eng", "cto", "marketing"],
    )

    leads = provider.find_leads(company, max_results=5, include_uncertain=True)
    assert leads == []
    # Only one call — once we detect auth failure, we don't loop through remaining titles.
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Cookie validation
# ---------------------------------------------------------------------------


def test_validate_li_at_accepts_real_looking_cookie():
    from beautiful_linkedin.providers.linkedin_people_search import looks_like_valid_li_at

    # A real li_at is ~150-220 ASCII chars (printable), often starts with AQE.
    cookie = "AQEDAU" + "x" * 180
    assert looks_like_valid_li_at(cookie) is True


def test_validate_li_at_rejects_short_strings():
    from beautiful_linkedin.providers.linkedin_people_search import looks_like_valid_li_at

    assert looks_like_valid_li_at("") is False
    assert looks_like_valid_li_at(None) is False
    assert looks_like_valid_li_at("short") is False


def test_validate_li_at_rejects_non_printable_garbage():
    """browser_cookie3 on Chrome 127+/Windows can return raw encrypted bytes
    masquerading as a string. Those will contain control chars or non-ASCII."""
    from beautiful_linkedin.providers.linkedin_people_search import looks_like_valid_li_at

    garbage = "AQED" + "\x00\x01\x02\x03\xff\xfe" + "x" * 100
    assert looks_like_valid_li_at(garbage) is False


# ---------------------------------------------------------------------------
# Playwright error mapping
# ---------------------------------------------------------------------------


def test_classify_playwright_error_maps_redirect_loop_to_auth_error():
    from beautiful_linkedin.providers.linkedin_people_search import (
        LinkedInAuthError,
        classify_playwright_error,
    )

    err = classify_playwright_error(
        Exception(
            "Page.goto: net::ERR_TOO_MANY_REDIRECTS at "
            "https://www.linkedin.com/company/x/people/"
        )
    )
    assert isinstance(err, LinkedInAuthError)
    assert "li_at" in str(err).lower()


def test_company_slug_extracts_from_people_url():
    """User pastes the full People URL and we still get the slug."""
    from beautiful_linkedin.providers.linkedin_people_search import _company_slug

    company = CompanyInput(
        company_name="Mercado Livre",
        linkedin_url="https://www.linkedin.com/company/mercadolivre-com/people/",
        titles=["growth"],
    )
    assert _company_slug(company) == "mercadolivre-com"


# ---------------------------------------------------------------------------
# CDP support
# ---------------------------------------------------------------------------


def test_probe_cdp_endpoint_returns_true_when_reachable(monkeypatch):
    from beautiful_linkedin.providers import linkedin_people_search as mod

    def fake_get(url, timeout):
        class R:
            status_code = 200

        return R()

    monkeypatch.setattr(mod, "_http_get", fake_get)

    assert mod.probe_cdp_endpoint("http://127.0.0.1:9222") is True


def test_probe_cdp_endpoint_returns_false_when_unreachable(monkeypatch):
    from beautiful_linkedin.providers import linkedin_people_search as mod

    def fake_get(url, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(mod, "_http_get", fake_get)

    assert mod.probe_cdp_endpoint("http://127.0.0.1:9222") is False


def test_cdp_fetcher_uses_existing_browser_session_without_li_at():
    """The CDP fetcher must NEVER re-inject li_at — that's what causes logout.

    It connects to the user's running Chrome, opens a new tab in an existing
    context (already logged in), reads the People URL, closes the tab.
    """
    from beautiful_linkedin.providers.linkedin_people_search import (
        CDPPeopleFetcher,
    )

    actions: list[tuple[str, str]] = []

    class FakePage:
        def __init__(self):
            self.url = "https://www.linkedin.com/feed/"

        def goto(self, url, wait_until="domcontentloaded"):
            actions.append(("goto", url))
            self.url = url

        def content(self):
            actions.append(("content", self.url))
            return "<html><body>cards aqui</body></html>"

        def mouse(self):
            return self

        def wheel(self, x, y):
            actions.append(("wheel", f"{x},{y}"))

        def close(self):
            actions.append(("page_close", ""))

    class FakeContext:
        def __init__(self):
            self.add_cookies_called = False

        def add_cookies(self, *args, **kwargs):
            self.add_cookies_called = True

        def new_page(self):
            page = FakePage()
            page.mouse = page  # FakePage is its own mouse for wheel()
            return page

    class FakeBrowser:
        def __init__(self):
            self.contexts = [FakeContext()]
            self.closed = False

        def close(self):
            actions.append(("browser_close", ""))
            self.closed = True

    fake_browser = FakeBrowser()

    def fake_connect(endpoint: str):
        actions.append(("connect", endpoint))
        return fake_browser

    fetcher = CDPPeopleFetcher(
        endpoint="http://127.0.0.1:9222",
        options=PeopleSearchOptions(scrolls=0, min_delay_seconds=0, max_delay_seconds=0),
        connect=fake_connect,
    )
    assert fetcher.needs_li_at is False

    html = fetcher.fetch_listing(
        "https://www.linkedin.com/company/mercadolivre-com/people/?keywords=growth",
        li_at="ignored-by-cdp",
        scrolls=0,
    )
    assert "cards aqui" in html

    # Crucial assertions:
    # 1) We never injected cookies (would cause logout if we touched user's session).
    assert fake_browser.contexts[0].add_cookies_called is False
    # 2) We used connect_over_cdp, not launch.
    assert ("connect", "http://127.0.0.1:9222") in actions
    # 3) We closed only the page we opened, not the user's browser.
    assert ("page_close", "") in actions
    assert ("browser_close", "") not in actions
    assert fake_browser.closed is False


def test_cdp_fetcher_raises_auth_error_when_not_logged_in():
    from beautiful_linkedin.providers.linkedin_people_search import (
        CDPPeopleFetcher,
        LinkedInAuthError,
    )

    class FakePage:
        def __init__(self):
            self.url = "https://www.linkedin.com/feed/"

        def goto(self, url, wait_until="domcontentloaded"):
            self.url = "https://www.linkedin.com/login?redirect=" + url

        def content(self):
            return "<html>login</html>"

        def close(self):
            pass

    class FakeContext:
        def add_cookies(self, *a, **k):
            pass

        def new_page(self):
            return FakePage()

    class FakeBrowser:
        def __init__(self):
            self.contexts = [FakeContext()]

        def close(self):
            pass

    fetcher = CDPPeopleFetcher(
        endpoint="http://127.0.0.1:9222",
        options=PeopleSearchOptions(scrolls=0, min_delay_seconds=0, max_delay_seconds=0),
        connect=lambda endpoint: FakeBrowser(),
    )

    import pytest

    with pytest.raises(LinkedInAuthError):
        fetcher.fetch_listing(
            "https://www.linkedin.com/company/x/people/", li_at="", scrolls=0
        )


def test_default_fetcher_prefers_cdp_when_endpoint_alive(monkeypatch):
    """When Chrome with --remote-debugging-port is running, use it."""
    from beautiful_linkedin.providers import linkedin_people_search as mod

    monkeypatch.setattr(mod, "probe_cdp_endpoint", lambda endpoint: True)

    options = PeopleSearchOptions(scrolls=0)
    options.cdp_endpoint = "http://127.0.0.1:9222"
    options.cdp_enabled = True

    fetcher = mod._default_fetcher(options)
    assert isinstance(fetcher, mod.CDPPeopleFetcher)


def test_default_fetcher_skips_cdp_when_disabled(monkeypatch):
    from beautiful_linkedin.providers import linkedin_people_search as mod

    # Even if the endpoint is alive, disabled flag wins.
    monkeypatch.setattr(mod, "probe_cdp_endpoint", lambda endpoint: True)
    monkeypatch.setattr(mod, "_try_build_scrapling_fetcher", lambda options: "scrap")
    monkeypatch.setattr(mod, "_try_build_playwright_fetcher", lambda options: "pw")

    options = PeopleSearchOptions()
    options.cdp_enabled = False
    options.cdp_endpoint = "http://127.0.0.1:9222"

    fetcher = mod._default_fetcher(options)
    assert fetcher == "scrap"


def test_provider_with_cdp_fetcher_skips_li_at_resolution(monkeypatch):
    """When the fetcher declares needs_li_at=False, the provider must NOT
    look up the cookie at all (no env, no browser store, no warnings)."""
    from beautiful_linkedin.providers import linkedin_people_search as mod

    # If anything tries to resolve the cookie, blow up.
    def boom(*args, **kwargs):
        raise AssertionError("CDP path must not call resolve_linkedin_li_at_cookie")

    monkeypatch.setattr(mod, "resolve_linkedin_li_at_cookie", boom)

    class CDPLikeFetcher:
        needs_li_at = False

        def fetch_listing(self, url, *, li_at, scrolls):
            return LINKEDIN_PEOPLE_HTML_PRIMARY

    provider = LinkedInPeopleSearchProvider(
        cookie=None,
        cookie_browser="auto",
        fetcher=CDPLikeFetcher(),
        options=PeopleSearchOptions(scrolls=0),
    )
    company = CompanyInput(
        company_name="Mercado Livre",
        linkedin_url="https://www.linkedin.com/company/mercadolivre-com/people/",
        titles=["growth"],
    )
    leads = provider.find_leads(company, max_results=10, include_uncertain=True)
    # Two cards in the primary HTML
    assert len(leads) == 2


def test_classify_playwright_error_passes_through_other_errors():
    from beautiful_linkedin.providers.linkedin_people_search import (
        LinkedInAuthError,
        classify_playwright_error,
    )

    original = TimeoutError("boom")
    result = classify_playwright_error(original)
    assert not isinstance(result, LinkedInAuthError)
    assert result is original


# ---------------------------------------------------------------------------
# CLI / factory wiring
# ---------------------------------------------------------------------------


def test_cli_accepts_linkedin_people_search_alias():
    from beautiful_linkedin.cli import parse_lead_providers

    assert parse_lead_providers("linkedin_people_search") == ["linkedin_people_search"]
    assert parse_lead_providers("people_search") == ["people_search"]


def test_factory_builds_linkedin_people_search_provider():
    from beautiful_linkedin.config import Settings
    from beautiful_linkedin.providers.factory import build_lead_providers

    providers = build_lead_providers(
        settings=Settings(linkedin_li_at_cookie="AQED-fake"),
        search_engine=None,
        provider_names=["linkedin_people_search"],
    )

    names = [p.name for p in providers]
    assert "linkedin_people_search" in names


def test_scrape_mode_people_search_maps_to_provider():
    from beautiful_linkedin.scrape_modes import apply_scrape_mode

    options = apply_scrape_mode(
        scrape_mode="people_search",
        lead_providers=["auto"],
        official_sites=True,
        web_query_limit=20,
        parallelism=4,
    )
    assert options.scrape_mode == "people_search"
    assert options.lead_providers == ["linkedin_people_search"]
    assert options.official_sites is False
    assert options.web_query_limit == 0
