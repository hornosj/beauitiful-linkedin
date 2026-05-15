import httpx

from beautiful_linkedin.models import CompanyInput
from beautiful_linkedin.providers.public_directories import PublicDirectoriesProvider


def test_theorg_scraper_extracts_name_role_and_linkedin_url():
    html = """
    <html>
      <body>
        <div data-testid="team-member-card">
          <h3>Ana Silva</h3>
          <p>Chief Marketing Officer</p>
          <a href="https://www.linkedin.com/in/ana-silva/">LinkedIn</a>
        </div>
      </body>
    </html>
    """

    def handler(request):
        assert str(request.url) == "https://theorg.com/org/nubank"
        return httpx.Response(200, text=html)

    provider = PublicDirectoriesProvider(
        sources=["theorg"],
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    company = CompanyInput(
        company_name="Nubank",
        company_domain="nubank.com.br",
        linkedin_url=None,
        titles=["marketing"],
    )

    leads = provider.find_leads(company, max_results=5, include_uncertain=False)

    assert len(leads) == 1
    assert leads[0].person_name == "Ana Silva"
    assert leads[0].title == "Chief Marketing Officer"
    assert leads[0].linkedin_url == "https://www.linkedin.com/in/ana-silva/"
    assert leads[0].source_type == "public_dir_theorg"
    assert leads[0].confidence_score == 25


def test_rocketreach_scraper_extracts_profiles():
    html = """
    <html>
      <body>
        <section class="profile-card">
          <h4>Bruno Costa</h4>
          <span class="profile-title">Sales Director</span>
        </section>
      </body>
    </html>
    """

    def handler(request):
        assert str(request.url) == "https://rocketreach.co/company/nubank"
        return httpx.Response(200, text=html)

    provider = PublicDirectoriesProvider(
        sources=["rocketreach"],
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    company = CompanyInput(
        company_name="Nubank",
        company_domain="nubank.com.br",
        linkedin_url=None,
        titles=["sales"],
    )

    leads = provider.find_leads(company, max_results=5, include_uncertain=False)

    assert len(leads) == 1
    assert leads[0].person_name == "Bruno Costa"
    assert leads[0].title == "Sales Director"
    assert leads[0].source_type == "public_dir_rocketreach"


def test_public_directories_skips_source_on_404(caplog):
    html = """
    <html>
      <body>
        <section class="profile-card">
          <h4>Carla Souza</h4>
          <span class="profile-title">Product Manager</span>
        </section>
      </body>
    </html>
    """

    def handler(request):
        url = str(request.url)
        if "theorg.com" in url:
            return httpx.Response(404, text="not found")
        if "rocketreach.co" in url:
            return httpx.Response(200, text=html)
        raise AssertionError(f"Unexpected request: {request.method} {url}")

    provider = PublicDirectoriesProvider(
        sources=["theorg", "rocketreach"],
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    company = CompanyInput(
        company_name="Nubank",
        company_domain="nubank.com.br",
        linkedin_url=None,
        titles=["product"],
    )

    leads = provider.find_leads(company, max_results=5, include_uncertain=False)

    assert len(leads) == 1
    assert leads[0].person_name == "Carla Souza"
    assert "Public Dir theorg retornou HTTP 404" in caplog.text


def test_public_directories_uses_cache_on_second_call():
    calls = 0

    class FakeCache:
        def __init__(self):
            self.value = None

        def get_json(self, namespace, payload):
            return self.value

        def set_json(self, namespace, payload, response):
            self.value = response

    html = """
    <html>
      <body>
        <div class="person-card">
          <h3>Daniel Lima</h3>
          <p>Marketing Manager</p>
        </div>
      </body>
    </html>
    """

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=html)

    provider = PublicDirectoriesProvider(
        sources=["theorg"],
        transport=httpx.MockTransport(handler),
        cache=FakeCache(),
        sleep=lambda _: None,
    )
    company = CompanyInput(
        company_name="Nubank",
        company_domain="nubank.com.br",
        linkedin_url=None,
        titles=["marketing"],
    )

    first = provider.find_leads(company, max_results=5, include_uncertain=False)
    second = provider.find_leads(company, max_results=5, include_uncertain=False)

    assert len(first) == 1
    assert len(second) == 1
    assert calls == 1
