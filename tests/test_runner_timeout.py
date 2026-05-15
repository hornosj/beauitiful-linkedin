import time

from beautiful_linkedin.models import CompanyInput, Lead
from beautiful_linkedin.runner import _run_company_providers


class FastProvider:
    name = "fast"

    def find_leads(self, company, max_results, include_uncertain, search_depth="standard"):
        return [
            Lead(
                company_name=company.company_name,
                company_domain=company.company_domain,
                person_name="Fulana Silva",
                title="Gerente de RH",
                linkedin_url="https://br.linkedin.com/in/fulana",
                source_url="https://br.linkedin.com/in/fulana",
                source_type="test",
                snippet="Gerente de RH",
                matched_title="rh",
                confidence_score=90,
            )
        ]


class SlowProvider:
    name = "slow"

    def find_leads(self, company, max_results, include_uncertain, search_depth="standard"):
        time.sleep(0.3)
        return []


class RecordingApiProvider:
    def __init__(self, name, batches):
        self.name = name
        self.batches = batches
        self.calls = []

    def find_leads(
        self,
        company,
        max_results,
        include_uncertain,
        search_depth="standard",
        offset=0,
    ):
        self.calls.append({"max_results": max_results, "offset": offset})
        batch_index = 0 if offset == 0 else 1
        return self.batches[min(batch_index, len(self.batches) - 1)]


def test_run_company_providers_returns_before_slow_provider_finishes():
    company = CompanyInput(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        linkedin_url=None,
        titles=["rh"],
    )

    started = time.perf_counter()
    results = _run_company_providers(
        company=company,
        providers=[FastProvider(), SlowProvider()],
        scraper=None,
        max_results=5,
        include_uncertain=True,
        search_depth="standard",
        official_sites=False,
        parallelism=2,
        provider_timeout_seconds=0.05,
    )

    elapsed = time.perf_counter() - started

    assert elapsed < 0.25
    assert any(batch and batch[0].person_name == "Fulana Silva" for batch in results)


def test_run_company_providers_splits_api_budget_evenly():
    company = CompanyInput(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        linkedin_url=None,
        titles=["rh"],
    )
    pdl = RecordingApiProvider("pdl", [[]])
    coresignal = RecordingApiProvider("coresignal", [[]])

    _run_company_providers(
        company=company,
        providers=[pdl, coresignal],
        scraper=None,
        max_results=10,
        include_uncertain=True,
        search_depth="standard",
        official_sites=False,
        parallelism=2,
        provider_timeout_seconds=1,
    )

    assert pdl.calls == [{"max_results": 5, "offset": 0}]
    assert coresignal.calls == [{"max_results": 5, "offset": 0}]


def test_run_company_providers_refills_until_target_when_round_has_new_leads():
    company = CompanyInput(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        linkedin_url=None,
        titles=["rh"],
    )
    duplicate = Lead(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        person_name="Fulana Silva",
        title="Gerente de RH",
        linkedin_url="https://br.linkedin.com/in/fulana",
        source_url="https://br.linkedin.com/in/fulana",
        source_type="api_pdl",
        snippet="Gerente de RH",
        matched_title="rh",
        confidence_score=90,
    )
    pdl_unique = duplicate.model_copy(
        update={
            "person_name": "Beltrana Souza",
            "linkedin_url": "https://br.linkedin.com/in/beltrana",
            "source_url": "https://br.linkedin.com/in/beltrana",
        }
    )
    core_unique = duplicate.model_copy(
        update={
            "person_name": "Ciclana Lima",
            "linkedin_url": "https://br.linkedin.com/in/ciclana",
            "source_url": "https://br.linkedin.com/in/ciclana",
            "source_type": "api_coresignal",
        }
    )
    pdl = RecordingApiProvider("pdl", [[duplicate], [pdl_unique]])
    coresignal = RecordingApiProvider("coresignal", [[duplicate], [core_unique]])

    results = _run_company_providers(
        company=company,
        providers=[pdl, coresignal],
        scraper=None,
        max_results=3,
        include_uncertain=True,
        search_depth="standard",
        official_sites=False,
        parallelism=2,
        provider_timeout_seconds=1,
    )

    flattened = [lead for batch in results for lead in batch]

    assert pdl.calls == [
        {"max_results": 2, "offset": 0},
        {"max_results": 2, "offset": 2},
    ]
    assert coresignal.calls == [
        {"max_results": 2, "offset": 0},
        {"max_results": 2, "offset": 2},
    ]
    assert {lead.person_name for lead in flattened} == {
        "Fulana Silva",
        "Beltrana Souza",
        "Ciclana Lima",
    }


def test_run_company_providers_stops_when_first_round_reaches_target():
    company = CompanyInput(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        linkedin_url=None,
        titles=["rh"],
    )
    first = Lead(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        person_name="Fulana Silva",
        title="Gerente de RH",
        linkedin_url="https://br.linkedin.com/in/fulana",
        source_url="https://br.linkedin.com/in/fulana",
        source_type="api_pdl",
        snippet="Gerente de RH",
        matched_title="rh",
        confidence_score=90,
    )
    second = first.model_copy(
        update={
            "person_name": "Beltrana Souza",
            "linkedin_url": "https://br.linkedin.com/in/beltrana",
            "source_url": "https://br.linkedin.com/in/beltrana",
            "source_type": "api_coresignal",
        }
    )
    third = first.model_copy(
        update={
            "person_name": "Ciclana Lima",
            "linkedin_url": "https://br.linkedin.com/in/ciclana",
            "source_url": "https://br.linkedin.com/in/ciclana",
        }
    )
    fourth = first.model_copy(
        update={
            "person_name": "Diana Ramos",
            "linkedin_url": "https://br.linkedin.com/in/diana",
            "source_url": "https://br.linkedin.com/in/diana",
            "source_type": "api_coresignal",
        }
    )
    pdl = RecordingApiProvider("pdl", [[first, third], [first]])
    coresignal = RecordingApiProvider("coresignal", [[second, fourth], [second]])

    _run_company_providers(
        company=company,
        providers=[pdl, coresignal],
        scraper=None,
        max_results=4,
        include_uncertain=True,
        search_depth="standard",
        official_sites=False,
        parallelism=2,
        provider_timeout_seconds=1,
    )

    assert pdl.calls == [{"max_results": 2, "offset": 0}]
    assert coresignal.calls == [{"max_results": 2, "offset": 0}]


def test_run_company_providers_interleaves_api_leads_before_capping_target():
    company = CompanyInput(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        linkedin_url=None,
        titles=["rh"],
    )

    def make_lead(index, source_type):
        return Lead(
            company_name="Banco Safra",
            company_domain="safra.com.br",
            person_name=f"Pessoa {source_type} {index}",
            title="Gerente de RH",
            linkedin_url=f"https://br.linkedin.com/in/{source_type}-{index}",
            source_url=f"https://br.linkedin.com/in/{source_type}-{index}",
            source_type=source_type,
            snippet="Gerente de RH",
            matched_title="rh",
            confidence_score=90,
        )

    pdl = RecordingApiProvider("pdl", [[make_lead(index, "api_pdl") for index in range(4)]])
    coresignal = RecordingApiProvider(
        "coresignal",
        [[make_lead(index, "api_coresignal") for index in range(4)]],
    )

    results = _run_company_providers(
        company=company,
        providers=[pdl, coresignal],
        scraper=None,
        max_results=4,
        include_uncertain=True,
        search_depth="standard",
        official_sites=False,
        parallelism=1,
        provider_timeout_seconds=1,
    )

    flattened = [lead for batch in results for lead in batch]

    assert len(flattened) == 4
    assert {lead.source_type for lead in flattened} == {"api_pdl", "api_coresignal"}


def test_run_company_providers_stops_when_refill_round_has_no_new_leads():
    company = CompanyInput(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        linkedin_url=None,
        titles=["rh"],
    )
    first = Lead(
        company_name="Banco Safra",
        company_domain="safra.com.br",
        person_name="Fulana Silva",
        title="Gerente de RH",
        linkedin_url="https://br.linkedin.com/in/fulana",
        source_url="https://br.linkedin.com/in/fulana",
        source_type="api_pdl",
        snippet="Gerente de RH",
        matched_title="rh",
        confidence_score=90,
    )
    second = first.model_copy(
        update={
            "person_name": "Beltrana Souza",
            "linkedin_url": "https://br.linkedin.com/in/beltrana",
            "source_url": "https://br.linkedin.com/in/beltrana",
            "source_type": "api_coresignal",
        }
    )
    pdl = RecordingApiProvider("pdl", [[first], [first]])
    coresignal = RecordingApiProvider("coresignal", [[second], [second]])

    _run_company_providers(
        company=company,
        providers=[pdl, coresignal],
        scraper=None,
        max_results=10,
        include_uncertain=True,
        search_depth="standard",
        official_sites=False,
        parallelism=2,
        provider_timeout_seconds=1,
    )

    assert pdl.calls == [
        {"max_results": 5, "offset": 0},
        {"max_results": 5, "offset": 5},
    ]
    assert coresignal.calls == [
        {"max_results": 5, "offset": 0},
        {"max_results": 5, "offset": 5},
    ]
