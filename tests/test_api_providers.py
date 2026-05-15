import json
import logging

import httpx

from beautiful_linkedin.models import CompanyInput
from beautiful_linkedin.providers.apify_linkedin import ApifyLinkedInEmployeesProvider
from beautiful_linkedin.providers.apollo import ApolloProvider
from beautiful_linkedin.providers.coresignal import CoresignalEmployeeProvider
from beautiful_linkedin.providers.people_data_labs import PeopleDataLabsProvider
from beautiful_linkedin.providers.public_search import PublicSearchLeadProvider


def test_people_data_labs_provider_normalizes_person_records():
    def handler(request):
        assert request.method == "GET"
        assert request.headers["X-api-key"] == "pdl-token"
        assert "from" not in request.url.params
        return httpx.Response(
            200,
            json={
                "status": 200,
                "data": [
                    {
                        "full_name": "Fulana Silva",
                        "job_title": "Gerente de RH",
                        "job_company_name": "Banco Safra",
                        "linkedin_url": "https://br.linkedin.com/in/fulana",
                        "summary": "Gerente de RH no Banco Safra",
                    }
                ],
                "total": 1,
            },
        )

    provider = PeopleDataLabsProvider(
        api_key="pdl-token",
        transport=httpx.MockTransport(handler),
    )
    company = CompanyInput(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        linkedin_url=None,
        titles=["rh"],
    )

    leads = provider.find_leads(company, max_results=5, include_uncertain=False)

    assert len(leads) == 1
    assert leads[0].person_name == "Fulana Silva"
    assert leads[0].source_type == "api_pdl"
    assert leads[0].matched_title == "rh"


def test_people_data_labs_cache_key_is_versioned_without_leaking_to_api():
    seen_cache_payloads = []

    class FakeCache:
        def get_json(self, namespace, payload):
            seen_cache_payloads.append((namespace, payload))
            return None

        def set_json(self, namespace, payload, response):
            seen_cache_payloads.append((namespace, payload))

    def handler(request):
        assert "_cache_version" not in request.url.params
        return httpx.Response(200, json={"data": []})

    provider = PeopleDataLabsProvider(
        api_key="pdl-token",
        transport=httpx.MockTransport(handler),
        cache=FakeCache(),
    )
    company = CompanyInput(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        linkedin_url=None,
        titles=["Software"],
    )

    provider.find_leads(company, max_results=5, include_uncertain=True)

    pdl_payloads = [payload for namespace, payload in seen_cache_payloads if namespace == "pdl"]
    assert pdl_payloads
    assert pdl_payloads[0]["cache_version"] == 2
    assert "_cache_version" not in pdl_payloads[0]["request"]


def test_coresignal_provider_normalizes_employee_preview_records():
    def handler(request):
        assert request.headers["apikey"] == "core-token"
        assert "from" not in request.content.decode("utf-8")
        assert "size" not in request.content.decode("utf-8")
        return httpx.Response(
            200,
            json=[
                {
                    "full_name": "Fulana Silva",
                    "active_experience_title": "Gerente de RH",
                    "company_name": "Banco Safra",
                    "professional_network_url": "https://br.linkedin.com/in/fulana",
                    "headline": "Gerente de RH no Banco Safra",
                }
            ],
        )

    provider = CoresignalEmployeeProvider(
        api_key="core-token",
        transport=httpx.MockTransport(handler),
    )
    company = CompanyInput(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        linkedin_url=None,
        titles=["rh"],
    )

    leads = provider.find_leads(company, max_results=5, include_uncertain=False)

    assert len(leads) == 1
    assert leads[0].person_name == "Fulana Silva"
    assert leads[0].source_type == "api_coresignal"
    assert leads[0].confidence_score == 100


def test_coresignal_cache_key_is_versioned_without_leaking_to_api_body():
    seen_cache_payloads = []

    class FakeCache:
        def get_json(self, namespace, payload):
            seen_cache_payloads.append((namespace, payload))
            return None

        def set_json(self, namespace, payload, response):
            seen_cache_payloads.append((namespace, payload))

    def handler(request):
        assert "cache_version" not in request.content.decode("utf-8")
        return httpx.Response(200, json=[])

    provider = CoresignalEmployeeProvider(
        api_key="core-token",
        transport=httpx.MockTransport(handler),
        cache=FakeCache(),
    )
    company = CompanyInput(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        linkedin_url=None,
        titles=["Software"],
    )

    provider.find_leads(company, max_results=5, include_uncertain=True)

    coresignal_payloads = [
        payload for namespace, payload in seen_cache_payloads if namespace == "coresignal"
    ]
    assert coresignal_payloads
    assert coresignal_payloads[0]["cache_version"] == 2
    assert "cache_version" not in coresignal_payloads[0]["payload"]


def test_coresignal_provider_skips_records_without_company_evidence():
    def handler(request):
        return httpx.Response(
            200,
            json=[
                {
                    "full_name": "Pessoa Fora Do Alvo",
                    "active_experience_title": "Software Engineer",
                    "company_name": "Outra Empresa",
                    "company_website": "https://outra.example",
                    "headline": "Software Engineer",
                }
            ],
        )

    provider = CoresignalEmployeeProvider(
        api_key="core-token",
        transport=httpx.MockTransport(handler),
    )
    company = CompanyInput(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        linkedin_url=None,
        titles=["Software"],
    )

    assert provider.find_leads(company, max_results=5, include_uncertain=True) == []


def test_coresignal_provider_skips_records_with_missing_company_and_unrelated_headline():
    def handler(request):
        return httpx.Response(
            200,
            json=[
                {
                    "full_name": "Pessoa Sem Empresa Atual",
                    "active_experience_title": None,
                    "company_name": None,
                    "company_website": None,
                    "headline": "Desenvolvedor de Software Sênior na Starbucks",
                }
            ],
        )

    provider = CoresignalEmployeeProvider(
        api_key="core-token",
        transport=httpx.MockTransport(handler),
    )
    company = CompanyInput(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        linkedin_url=None,
        titles=["Software"],
    )

    assert provider.find_leads(company, max_results=5, include_uncertain=True) == []


def test_public_search_provider_runs_queries_in_parallel_and_extracts_leads():
    class FakeEngine:
        def search(self, query, max_results):
            from beautiful_linkedin.models import SearchResult

            return [
                SearchResult(
                    title="Fulana Silva - Gerente de RH - Banco Safra | LinkedIn",
                    url="https://br.linkedin.com/in/fulana",
                    snippet="Fulana Silva - Gerente de RH - Banco Safra",
                    source_type="search_fake",
                )
            ]

    provider = PublicSearchLeadProvider(FakeEngine(), parallelism=3)
    company = CompanyInput(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        linkedin_url=None,
        titles=["rh"],
    )

    leads = provider.find_leads(
        company,
        max_results=5,
        include_uncertain=False,
        search_depth="standard",
    )

    assert len(leads) == 1
    assert leads[0].person_name == "Fulana Silva"


def test_public_search_provider_respects_query_limit():
    class CountingEngine:
        def __init__(self):
            self.calls = 0

        def search(self, query, max_results):
            self.calls += 1
            return []

    engine = CountingEngine()
    provider = PublicSearchLeadProvider(engine, parallelism=3, query_limit=2)
    company = CompanyInput(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        linkedin_url="https://www.linkedin.com/company/safra-banco/",
        titles=["rh"],
    )

    provider.find_leads(
        company,
        max_results=5,
        include_uncertain=True,
        search_depth="deep",
    )

    assert engine.calls == 2


def test_public_search_provider_finds_mercado_livre_before_empty_abort():
    class MercadoLivreVariantEngine:
        def __init__(self):
            self.calls = []

        def search(self, query, max_results):
            from beautiful_linkedin.models import SearchResult

            self.calls.append(query)
            if "Mercado Livre Brasil" not in query:
                return []
            return [
                SearchResult(
                    title="Ana Silva - Head of Marketing - Mercado Livre Brasil | LinkedIn",
                    url="https://br.linkedin.com/in/ana-silva",
                    snippet="Ana Silva - Head of Marketing - Mercado Livre Brasil",
                    source_type="search_fake",
                )
            ]

    engine = MercadoLivreVariantEngine()
    provider = PublicSearchLeadProvider(
        engine,
        parallelism=1,
        sleep=lambda _seconds: None,
    )
    company = CompanyInput(
        company_name="Mercado Livre",
        company_domain="mercadolivre.com.br",
        linkedin_url="https://www.linkedin.com/company/mercadolivre-com/",
        titles=["marketing"],
    )

    leads = provider.find_leads(
        company,
        max_results=5,
        include_uncertain=False,
        search_depth="standard",
    )

    assert len(leads) == 1
    assert leads[0].person_name == "Ana Silva"
    assert "Mercado Livre Brasil" in " ".join(engine.calls[:8])


def test_apify_linkedin_provider_normalizes_actor_dataset_items():
    def handler(request):
        assert request.method == "POST"
        assert request.url.params["token"] == "apify-token"
        payload = request.read().decode("utf-8")
        assert "apify-token" not in payload
        assert '"maxItems":5' in payload
        assert '"profileScraperMode":"Short ($4 per 1k)"' in payload
        return httpx.Response(
            200,
            json=[
                {
                    "id": "abc123",
                    "firstName": "Fulana",
                    "lastName": "Silva",
                    "linkedinUrl": "https://www.linkedin.com/in/fulana",
                    "headline": "Head de Marketing no Banco Safra",
                    "location": {"linkedinText": "São Paulo, Brasil"},
                    "currentPosition": [{"companyName": "Banco Safra"}],
                    "experience": [
                        {
                            "position": "Head de Marketing",
                            "companyName": "Banco Safra",
                        }
                    ],
                }
            ],
        )

    provider = ApifyLinkedInEmployeesProvider(
        api_key="apify-token",
        transport=httpx.MockTransport(handler),
    )
    company = CompanyInput(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        linkedin_url="https://www.linkedin.com/company/safra-banco/",
        titles=["marketing"],
    )

    leads = provider.find_leads(company, max_results=5, include_uncertain=False)

    assert len(leads) == 1
    assert leads[0].person_name == "Fulana Silva"
    assert leads[0].title == "Head de Marketing"
    assert leads[0].linkedin_url == "https://www.linkedin.com/in/fulana"
    assert leads[0].source_type == "api_apify_linkedin"
    assert leads[0].matched_title == "marketing"


def test_apollo_uses_by_company_endpoint_when_resolution_succeeds():
    search_payloads = []

    def handler(request):
        if "organizations/enrich" in str(request.url):
            assert request.method == "GET"
            assert request.url.params["domain"] == "nubank.com.br"
            return httpx.Response(200, json={"organization": {"id": "org_123"}})
        if "people/search" in str(request.url):
            payload = json.loads(request.content.decode())
            search_payloads.append(payload)
            return httpx.Response(
                200,
                json={
                    "people": [
                        {
                            "id": "p1",
                            "name": "Fulana Silva",
                            "title": "Head of Marketing",
                            "linkedin_url": "https://www.linkedin.com/in/fulana",
                            "organization": {"name": "Nubank"},
                        }
                    ]
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    provider = ApolloProvider(
        api_key="apollo-token",
        transport=httpx.MockTransport(handler),
    )
    company = CompanyInput(
        company_name="Nubank",
        company_domain="nubank.com.br",
        linkedin_url=None,
        titles=["marketing"],
    )

    leads = provider.find_leads(company, max_results=5, include_uncertain=False)

    assert len(leads) == 1
    assert search_payloads[0]["organization_ids"] == ["org_123"]
    assert "q_organization_domains" not in search_payloads[0]


def test_apollo_falls_back_to_search_when_resolution_fails():
    search_payloads = []

    def handler(request):
        if "organizations/enrich" in str(request.url):
            return httpx.Response(404, json={"error": "not_found"})
        if "people/search" in str(request.url):
            payload = json.loads(request.content.decode())
            search_payloads.append(payload)
            return httpx.Response(
                200,
                json={
                    "people": [
                        {
                            "id": "p1",
                            "name": "Fulana Silva",
                            "title": "Head of Marketing",
                            "linkedin_url": "https://www.linkedin.com/in/fulana",
                            "organization": {"name": "Nubank"},
                        }
                    ]
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    provider = ApolloProvider(
        api_key="apollo-token",
        transport=httpx.MockTransport(handler),
    )
    company = CompanyInput(
        company_name="Nubank",
        company_domain="nubank.com.br",
        linkedin_url=None,
        titles=["marketing"],
    )

    leads = provider.find_leads(company, max_results=5, include_uncertain=False)

    assert len(leads) == 1
    assert search_payloads[0]["q_organization_domains"] == "nubank.com.br"
    assert "organization_ids" not in search_payloads[0]


def test_people_data_labs_uses_by_company_endpoint_when_resolution_succeeds():
    search_sql = []

    def handler(request):
        if "company/enrich" in str(request.url):
            assert request.url.params["website"] == "nubank.com.br"
            return httpx.Response(200, json={"id": "pdl_company_123"})
        if "person/search" in str(request.url):
            sql = request.url.params["sql"]
            search_sql.append(sql)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "p1",
                            "full_name": "Fulana Silva",
                            "job_title": "Head of Marketing",
                            "job_company_name": "Nubank",
                            "job_company_website": "nubank.com.br",
                            "linkedin_url": "https://www.linkedin.com/in/fulana",
                            "summary": "Head of Marketing at Nubank",
                        }
                    ]
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    provider = PeopleDataLabsProvider(
        api_key="pdl-token",
        transport=httpx.MockTransport(handler),
    )
    company = CompanyInput(
        company_name="Nubank",
        company_domain="nubank.com.br",
        linkedin_url=None,
        titles=["marketing"],
    )

    leads = provider.find_leads(company, max_results=5, include_uncertain=False)

    assert len(leads) == 1
    assert "job_company_id='pdl_company_123'" in search_sql[0]
    assert "job_company_name='Nubank'" not in search_sql[0]


def test_people_data_labs_falls_back_to_search_when_resolution_fails():
    search_sql = []

    def handler(request):
        if "company/enrich" in str(request.url):
            return httpx.Response(404, json={"error": "not_found"})
        if "person/search" in str(request.url):
            sql = request.url.params["sql"]
            search_sql.append(sql)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "p1",
                            "full_name": "Fulana Silva",
                            "job_title": "Head of Marketing",
                            "job_company_name": "Nubank",
                            "job_company_website": "nubank.com.br",
                            "linkedin_url": "https://www.linkedin.com/in/fulana",
                            "summary": "Head of Marketing at Nubank",
                        }
                    ]
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    provider = PeopleDataLabsProvider(
        api_key="pdl-token",
        transport=httpx.MockTransport(handler),
    )
    company = CompanyInput(
        company_name="Nubank",
        company_domain="nubank.com.br",
        linkedin_url=None,
        titles=["marketing"],
    )

    leads = provider.find_leads(company, max_results=5, include_uncertain=False)

    assert len(leads) == 1
    assert "job_company_name='Nubank'" in search_sql[0]
    assert "job_company_id=" not in search_sql[0]


def test_coresignal_uses_by_company_endpoint_when_resolution_succeeds():
    called_by_company = []

    def handler(request):
        url = str(request.url)
        if "company_multi_source/collect" in url:
            assert request.method == "GET"
            return httpx.Response(200, json={"id": 12345})
        if "employee_multi_source/by_company/12345" in url:
            called_by_company.append(url)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "e1",
                            "full_name": "Fulana Silva",
                            "active_experience_title": "Head of Marketing",
                            "company_name": "Nubank",
                            "company_website": "nubank.com.br",
                            "professional_network_url": "https://www.linkedin.com/in/fulana",
                            "headline": "Head of Marketing at Nubank",
                        }
                    ]
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {url}")

    provider = CoresignalEmployeeProvider(
        api_key="core-token",
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
    assert called_by_company


def test_coresignal_falls_back_to_search_when_resolution_fails():
    search_payloads = []

    def handler(request):
        url = str(request.url)
        if "company_multi_source/collect" in url:
            return httpx.Response(404, json={"error": "not_found"})
        if "employee_multi_source/search/es_dsl/preview" in url:
            search_payloads.append(json.loads(request.content.decode()))
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "e1",
                        "full_name": "Fulana Silva",
                        "active_experience_title": "Head of Marketing",
                        "company_name": "Nubank",
                        "company_website": "nubank.com.br",
                        "professional_network_url": "https://www.linkedin.com/in/fulana",
                        "headline": "Head of Marketing at Nubank",
                    }
                ],
            )
        raise AssertionError(f"Unexpected request: {request.method} {url}")

    provider = CoresignalEmployeeProvider(
        api_key="core-token",
        transport=httpx.MockTransport(handler),
    )
    company = CompanyInput(
        company_name="Nubank",
        company_domain="nubank.com.br",
        linkedin_url=None,
        titles=["marketing"],
    )

    leads = provider.find_leads(company, max_results=5, include_uncertain=False)

    assert len(leads) == 1
    assert search_payloads


def test_people_data_labs_logs_detailed_error_on_http_failure(caplog):
    def handler(request):
        return httpx.Response(401, json={"error": "invalid_api_key"})

    provider = PeopleDataLabsProvider(
        api_key="bad-token",
        transport=httpx.MockTransport(handler),
    )
    company = CompanyInput(
        company_name="CSD BR",
        company_domain="csdbr.com.br",
        linkedin_url=None,
        titles=["marketing"],
    )

    with caplog.at_level(logging.WARNING, logger="beautiful_linkedin.providers.people_data_labs"):
        leads = provider.find_leads(company, max_results=5, include_uncertain=False)

    assert leads == []
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "People Data Labs retornou HTTP 401" in messages
    assert "invalid_api_key" in messages
    assert "[empresa=CSD BR]" in messages


def test_apollo_logs_detailed_error_on_http_failure(caplog):
    from beautiful_linkedin.providers.apollo import ApolloProvider

    def handler(request):
        return httpx.Response(422, text='{"error":"missing_field:person_titles"}')

    provider = ApolloProvider(api_key="bad-token", transport=httpx.MockTransport(handler))
    company = CompanyInput(
        company_name="CSD BR",
        company_domain=None,
        linkedin_url=None,
        titles=["marketing"],
    )

    with caplog.at_level(logging.WARNING, logger="beautiful_linkedin.providers.apollo"):
        leads = provider.find_leads(company, max_results=5, include_uncertain=False)

    assert leads == []
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "Apollo retornou HTTP 422" in messages
    assert "missing_field:person_titles" in messages


def test_serper_logs_detailed_error_on_http_failure(caplog):
    from beautiful_linkedin.search.serper_search import SerperSearchEngine

    def handler(request):
        return httpx.Response(403, json={"message": "forbidden — quota exceeded"})

    engine = SerperSearchEngine(
        api_key="bad-token",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )

    with caplog.at_level(logging.WARNING, logger="beautiful_linkedin.search.serper_search"):
        results = engine.search("site:linkedin.com 'CSD BR'", max_results=10)

    assert results == []
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "Serper retornou HTTP 403" in messages
    assert "quota exceeded" in messages
    assert "[query=" in messages
