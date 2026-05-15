import httpx

from beautiful_linkedin.models import CompanyInput
from beautiful_linkedin.providers.linkedin_sales_navigator import (
    LinkedInSalesNavigatorProvider,
)


def _voyager_init_responses(request: httpx.Request) -> httpx.Response | None:
    url = str(request.url)
    if "/feed/" in url:
        return httpx.Response(
            200,
            headers={"set-cookie": 'JSESSIONID="ajax:abc"; Path=/;'},
            text="<html></html>",
        )
    if "organization/companies" in url:
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "entityUrn": "urn:li:fsd_company:9999",
                        "name": "CSD BR",
                        "universalName": "csd-br",
                    }
                ]
            },
        )
    return None


def test_sales_navigator_provider_returns_leads_when_endpoint_succeeds():
    def handler(request: httpx.Request) -> httpx.Response:
        early = _voyager_init_responses(request)
        if early is not None:
            return early
        url = str(request.url)
        if "salesApiPeopleSearch" in url:
            return httpx.Response(
                200,
                json={
                    "elements": [
                        {
                            "entityUrn": "urn:li:fs_salesProfile:(maria-souza,NAME_SEARCH,ABCD)",
                            "fullName": "Maria Souza",
                            "currentPositionTitle": "Head of Marketing",
                            "currentPositionCompanyName": "CSD BR",
                            "headline": "Head of Marketing at CSD BR",
                            "geoRegion": "Sao Paulo, Brazil",
                        }
                    ]
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {url}")

    provider = LinkedInSalesNavigatorProvider(
        cookie="AQED-cookie",
        cookie_browser="none",
        transport=httpx.MockTransport(handler),
    )
    company = CompanyInput(
        company_name="CSD BR",
        company_domain="csdbr.com.br",
        linkedin_url="https://www.linkedin.com/company/csd-br/",
        titles=["marketing"],
    )

    leads = provider.find_leads(company, max_results=5, include_uncertain=False)

    assert len(leads) == 1
    lead = leads[0]
    assert lead.person_name == "Maria Souza"
    assert lead.title == "Head of Marketing"
    assert lead.linkedin_url == "https://www.linkedin.com/in/maria-souza/"
    assert lead.source_type == "linkedin_sales_navigator"
    assert lead.matched_title == "marketing"


def test_sales_navigator_falls_back_to_voyager_when_endpoint_returns_403():
    """When the cookie has no Sales Navigator entitlement, the provider must
    fall back to the regular voyager people search."""

    def handler(request: httpx.Request) -> httpx.Response:
        early = _voyager_init_responses(request)
        if early is not None:
            return early
        url = str(request.url)
        if "salesApiPeopleSearch" in url:
            return httpx.Response(403, json={"status": 403})
        if "graphql" in url:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "searchDashClustersByAll": {
                            "elements": [
                                {
                                    "items": [
                                        {
                                            "item": {
                                                "entityResult": {
                                                    "navigationUrl": "https://www.linkedin.com/in/joao-fallback/",
                                                    "title": {"text": "Joao Fallback"},
                                                    "primarySubtitle": {
                                                        "text": "Head of Marketing at CSD BR"
                                                    },
                                                    "secondarySubtitle": {
                                                        "text": "Sao Paulo, Brazil"
                                                    },
                                                }
                                            }
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {url}")

    provider = LinkedInSalesNavigatorProvider(
        cookie="AQED-cookie",
        cookie_browser="none",
        transport=httpx.MockTransport(handler),
    )
    company = CompanyInput(
        company_name="CSD BR",
        company_domain="csdbr.com.br",
        linkedin_url="https://www.linkedin.com/company/csd-br/",
        titles=["marketing"],
    )

    leads = provider.find_leads(company, max_results=5, include_uncertain=False)

    assert len(leads) == 1
    fallback_lead = leads[0]
    assert fallback_lead.person_name == "Joao Fallback"
    assert fallback_lead.source_type == "linkedin_cookie"


def test_sales_navigator_returns_empty_without_cookie():
    provider = LinkedInSalesNavigatorProvider(cookie=None, cookie_browser="none")
    company = CompanyInput(
        company_name="CSD BR",
        company_domain=None,
        linkedin_url="https://www.linkedin.com/company/csd-br/",
        titles=["marketing"],
    )

    assert (
        provider.find_leads(company, max_results=5, include_uncertain=True) == []
    )


def test_sales_navigator_local_title_match_skips_api_title_filter():
    sales_queries: list[str] = []

    def sales_profile(public_id: str, name: str, title: str) -> dict:
        return {
            "entityUrn": f"urn:li:fs_salesProfile:({public_id},NAME_SEARCH,ABCD)",
            "fullName": name,
            "currentPositionTitle": title,
            "currentPositionCompanyName": "CSD BR",
            "headline": f"{title} at CSD BR",
            "geoRegion": "Sao Paulo, Brazil",
        }

    def handler(request: httpx.Request) -> httpx.Response:
        early = _voyager_init_responses(request)
        if early is not None:
            return early
        url = str(request.url)
        if "salesApiPeopleSearch" in url:
            query = request.url.params["query"]
            sales_queries.append(query)
            if request.url.params.get("start") != "0":
                return httpx.Response(200, json={"elements": []})
            return httpx.Response(
                200,
                json={
                    "elements": [
                        sales_profile(
                            "maria-marketing",
                            "Maria Marketing",
                            "Marketing Manager",
                        ),
                        sales_profile(
                            "joana-growth",
                            "Joana Growth",
                            "Head of Marketing",
                        ),
                        sales_profile("paulo-finance", "Paulo Finance", "CFO"),
                        sales_profile("carla-legal", "Carla Legal", "Legal Counsel"),
                        sales_profile("enzo-product", "Enzo Product", "Product Manager"),
                    ]
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {url}")

    provider = LinkedInSalesNavigatorProvider(
        cookie="AQED-cookie",
        cookie_browser="none",
        transport=httpx.MockTransport(handler),
    )
    company = CompanyInput(
        company_name="CSD BR",
        company_domain="csdbr.com.br",
        linkedin_url="https://www.linkedin.com/company/csd-br/",
        titles=["marketing"],
    )

    leads = provider.find_leads(company, max_results=5, include_uncertain=False)

    assert len(leads) == 2
    assert {lead.person_name for lead in leads} == {"Maria Marketing", "Joana Growth"}
    assert sales_queries
    assert all("CURRENT_TITLE" not in query for query in sales_queries)
