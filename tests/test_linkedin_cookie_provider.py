import httpx

from beautiful_linkedin.models import CompanyInput
from beautiful_linkedin.providers.linkedin_cookie import LinkedInCookieEmployeesProvider


def test_linkedin_cookie_provider_normalizes_voyager_people_results():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/feed/" in url:
            return httpx.Response(
                200,
                headers={"set-cookie": 'JSESSIONID="ajax:123"; Path=/;'},
                text="<html></html>",
            )
        if "organization/companies" in url:
            return httpx.Response(
                200,
                json={
                    "elements": [
                        {
                            "entityUrn": "urn:li:fsd_company:12345",
                            "name": "Nubank",
                            "universalName": "nubank",
                        }
                    ]
                },
            )
        if "graphql" in url:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "searchDashClustersByAll": {
                            "paging": {"total": 1},
                            "elements": [
                                {
                                    "items": [
                                        {
                                            "item": {
                                                "entityResult": {
                                                    "navigationUrl": "https://www.linkedin.com/in/ana-silva/",
                                                    "title": {"text": "Ana Silva"},
                                                    "primarySubtitle": {
                                                        "text": "Head of Marketing at Nubank"
                                                    },
                                                    "secondarySubtitle": {
                                                        "text": "Sao Paulo, Brazil"
                                                    },
                                                }
                                            }
                                        }
                                    ]
                                }
                            ],
                        }
                    }
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {url}")

    provider = LinkedInCookieEmployeesProvider(
        cookie="AQED-cookie",
        transport=httpx.MockTransport(handler),
    )
    company = CompanyInput(
        company_name="Nubank",
        company_domain="nubank.com.br",
        linkedin_url="https://www.linkedin.com/company/nubank/",
        titles=["marketing"],
    )

    leads = provider.find_leads(company, max_results=5, include_uncertain=False)

    assert len(leads) == 1
    assert leads[0].person_name == "Ana Silva"
    assert leads[0].title == "Head of Marketing"
    assert leads[0].linkedin_url == "https://www.linkedin.com/in/ana-silva/"
    assert leads[0].matched_title == "marketing"
    assert leads[0].source_type == "linkedin_cookie"


def test_linkedin_cookie_provider_returns_empty_without_cookie():
    provider = LinkedInCookieEmployeesProvider(cookie=None, cookie_browser="none")
    company = CompanyInput(
        company_name="Nubank",
        company_domain=None,
        linkedin_url="https://www.linkedin.com/company/nubank/",
        titles=["marketing"],
    )

    assert provider.find_leads(company, max_results=5, include_uncertain=True) == []


def test_linkedin_cookie_provider_local_title_match_pulls_all_then_filters():
    graphql_variables: list[str] = []

    def profile(public_id: str, name: str, headline: str) -> dict:
        return {
            "item": {
                "entityResult": {
                    "navigationUrl": f"https://www.linkedin.com/in/{public_id}/",
                    "title": {"text": name},
                    "primarySubtitle": {"text": headline},
                    "secondarySubtitle": {"text": "Sao Paulo, Brazil"},
                }
            }
        }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/feed/" in url:
            return httpx.Response(
                200,
                headers={"set-cookie": 'JSESSIONID="ajax:123"; Path=/;'},
                text="<html></html>",
            )
        if "organization/companies" in url:
            return httpx.Response(
                200,
                json={
                    "elements": [
                        {
                            "entityUrn": "urn:li:fsd_company:12345",
                            "name": "Nubank",
                            "universalName": "nubank",
                        }
                    ]
                },
            )
        if "graphql" in url:
            variables = request.url.params["variables"]
            graphql_variables.append(variables)
            if "start:0" not in variables:
                return httpx.Response(200, json={"data": {}})
            return httpx.Response(
                200,
                json={
                    "data": {
                        "searchDashClustersByAll": {
                            "elements": [
                                {
                                    "items": [
                                        profile(
                                            "ana-marketing",
                                            "Ana Marketing",
                                            "Marketing Manager at Nubank",
                                        ),
                                        profile(
                                            "bia-growth",
                                            "Bia Growth",
                                            "Head of Marketing at Nubank",
                                        ),
                                        profile(
                                            "caio-finance",
                                            "Caio Finance",
                                            "Finance Analyst at Nubank",
                                        ),
                                        profile(
                                            "duda-legal",
                                            "Duda Legal",
                                            "Legal Counsel at Nubank",
                                        ),
                                        profile(
                                            "edu-product",
                                            "Edu Product",
                                            "Product Manager at Nubank",
                                        ),
                                    ]
                                }
                            ],
                        }
                    }
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {url}")

    provider = LinkedInCookieEmployeesProvider(
        cookie="AQED-cookie",
        cookie_browser="none",
        transport=httpx.MockTransport(handler),
    )
    company = CompanyInput(
        company_name="Nubank",
        company_domain="nubank.com.br",
        linkedin_url="https://www.linkedin.com/company/nubank/",
        titles=["marketing"],
    )

    leads = provider.find_leads(company, max_results=5, include_uncertain=False)

    assert len(leads) == 2
    assert {lead.person_name for lead in leads} == {"Ana Marketing", "Bia Growth"}
    assert graphql_variables
    assert all("key:title" not in variables for variables in graphql_variables)
