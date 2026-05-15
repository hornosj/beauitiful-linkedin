import httpx

from beautiful_linkedin.models import CompanyInput
from beautiful_linkedin.scraping.company_site_scraper import CompanySiteScraper


def test_company_site_scraper_prioritizes_relevant_discovered_links():
    requested_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        pages = {
            "/": """
                <html>
                  <body>
                    <a href="/about">Sobre</a>
                    <a href="/time-marketing">Conheça a equipe de marketing</a>
                  </body>
                </html>
            """,
            "/about": "<html><body>Empresa focada em tecnologia.</body></html>",
            "/time-marketing": """
                <html>
                  <body>Ana Silva - Head of Marketing - Acme</body>
                </html>
            """,
        }
        return httpx.Response(200, html=pages.get(request.url.path, ""))

    scraper = CompanySiteScraper(
        max_pages=2,
        paths=["/", "/about"],
        transport=httpx.MockTransport(handler),
    )
    company = CompanyInput(
        company_name="Acme",
        company_domain="acme.com",
        linkedin_url=None,
        titles=["marketing"],
    )

    leads = scraper.scrape_company(company)

    assert [lead.person_name for lead in leads] == ["Ana Silva"]
    assert "/time-marketing" in requested_paths
    assert "/about" not in requested_paths


def test_company_site_scraper_discovers_links_relevant_to_target_title():
    requested_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        pages = {
            "/": """
                <html>
                  <body>
                    <a href="/about">Sobre</a>
                    <a href="/marketing">Marketing</a>
                  </body>
                </html>
            """,
            "/about": "<html><body>Empresa focada em tecnologia.</body></html>",
            "/marketing": """
                <html>
                  <body>Bruno Lima - Marketing Manager - Acme</body>
                </html>
            """,
        }
        return httpx.Response(200, html=pages.get(request.url.path, ""))

    scraper = CompanySiteScraper(
        max_pages=2,
        paths=["/", "/about"],
        transport=httpx.MockTransport(handler),
    )
    company = CompanyInput(
        company_name="Acme",
        company_domain="acme.com",
        linkedin_url=None,
        titles=["marketing"],
    )

    leads = scraper.scrape_company(company)

    assert [lead.person_name for lead in leads] == ["Bruno Lima"]
    assert "/marketing" in requested_paths
    assert "/about" not in requested_paths
