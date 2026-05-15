from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from beautiful_linkedin.models import Lead
from beautiful_linkedin.server.app import build_app
from beautiful_linkedin.storage.enrichment import (
    EnrichmentOptions,
    estimate_enrichment_cost,
)


def _lead(
    *,
    person: str = "Ana Silva",
    company: str = "Nubank",
    domain: str = "nubank.com.br",
    title: str = "Head of Marketing",
    linkedin_url: str = "https://www.linkedin.com/in/ana-silva/",
    email: str | None = None,
    phone: str | None = None,
) -> Lead:
    return Lead(
        company_name=company,
        company_domain=domain,
        person_name=person,
        title=title,
        linkedin_url=linkedin_url,
        email=email,
        phone=phone,
        source_url=linkedin_url,
        source_type="linkedin_people_search",
        snippet=title,
        confidence_score=88,
    )


def test_estimate_enrichment_cost_skips_snovio_for_phone_only() -> None:
    leads = [_lead(person="Ana"), _lead(person="Bruno", linkedin_url="https://www.linkedin.com/in/bruno/")]
    options = EnrichmentOptions(
        fields="phone",
        providers=["snovio", "lusha"],
        credit_costs_brl={"snovio": 2.0, "lusha": 3.5},
    )

    estimate = estimate_enrichment_cost(leads, options)

    assert estimate.selected_leads == 2
    assert estimate.total_estimated_credits == 2
    assert estimate.total_estimated_brl == 7.0
    assert "snovio não enriquece telefone" in " ".join(estimate.warnings).lower()
    assert [item.provider for item in estimate.provider_estimates] == ["lusha"]


def test_enrich_endpoint_estimates_without_confirming(
    tmp_path: Path, monkeypatch
) -> None:
    app = build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))
    client = TestClient(app)
    table_id = client.post(
        "/lead-tables",
        json={"name": "Enrich", "leads": [_lead().model_dump(mode="json")]},
    ).json()["table"]["id"]

    called = False

    def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("providers must not run before confirmation")

    monkeypatch.setattr("beautiful_linkedin.server.app.run_saved_lead_enrichment", fail_if_called)

    response = client.post(
        f"/lead-tables/{table_id}/enrich",
        json={
            "lead_refs": ["https://www.linkedin.com/in/ana-silva/"],
            "fields": "email",
            "providers": ["lusha"],
            "credit_costs_brl": {"lusha": 4.25},
            "confirmed": False,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "estimated"
    assert body["estimate"]["selected_leads"] == 1
    assert body["estimate"]["total_estimated_brl"] == 4.25
    assert body["summary"]["enriched_leads"] == 0
    assert called is False


def test_enrich_endpoint_enriches_only_selected_leads_and_persists_contacts(
    tmp_path: Path, monkeypatch
) -> None:
    app = build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))
    client = TestClient(app)
    leads = [
        _lead(person="Ana Silva", linkedin_url="https://www.linkedin.com/in/ana-silva/"),
        _lead(person="Bruno Costa", linkedin_url="https://www.linkedin.com/in/bruno-costa/"),
    ]
    table_id = client.post(
        "/lead-tables",
        json={"name": "Enrich", "leads": [lead.model_dump(mode="json") for lead in leads]},
    ).json()["table"]["id"]

    class FakeLushaProvider:
        name = "lusha"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def enrich(self, selected: list[Lead], options: EnrichmentOptions) -> list[Any]:
            assert [lead.person_name for lead in selected] == ["Ana Silva"]
            from beautiful_linkedin.storage.enrichment import EnrichmentUpdate

            return [
                EnrichmentUpdate(
                    lead=selected[0],
                    provider="lusha",
                    email="ana@nubank.com.br",
                    phone="+55 11 99999-0000",
                    raw={"contact": "ok"},
                )
            ]

    monkeypatch.setattr(
        "beautiful_linkedin.server.app.LushaEnrichmentProvider", FakeLushaProvider
    )

    response = client.post(
        f"/lead-tables/{table_id}/enrich",
        json={
            "lead_refs": ["https://www.linkedin.com/in/ana-silva/"],
            "fields": "both",
            "providers": ["lusha"],
            "credit_costs_brl": {"lusha": 5.0},
            "confirmed": True,
            "api_keys": {"lusha_api_key": "test-lusha"},
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["summary"]["requested_leads"] == 1
    assert body["summary"]["enriched_leads"] == 1
    assert body["summary"]["updated_leads"] == 1

    detail = client.get(f"/lead-tables/{table_id}").json()
    ana = next(lead for lead in detail["leads"] if lead["person_name"] == "Ana Silva")
    bruno = next(lead for lead in detail["leads"] if lead["person_name"] == "Bruno Costa")
    assert ana["email"] == "ana@nubank.com.br"
    assert ana["phone"] == "+55 11 99999-0000"
    assert "Enriquecido via lusha" in ana["consultation_note"]
    assert bruno["email"] is None
    assert bruno["phone"] is None
    assert detail["table"]["enrichment_status"] == "enriched"


def test_apollo_phone_requires_webhook(tmp_path: Path) -> None:
    app = build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))
    client = TestClient(app)
    table_id = client.post(
        "/lead-tables",
        json={"name": "Enrich", "leads": [_lead().model_dump(mode="json")]},
    ).json()["table"]["id"]

    response = client.post(
        f"/lead-tables/{table_id}/enrich",
        json={
            "lead_refs": ["https://www.linkedin.com/in/ana-silva/"],
            "fields": "phone",
            "providers": ["apollo"],
            "confirmed": True,
            "api_keys": {"apollo_api_key": "test-apollo"},
        },
    )

    assert response.status_code == 422
    assert "webhook" in response.text.lower()


def test_lusha_provider_payload_uses_v2_person_and_reveal_flags() -> None:
    from beautiful_linkedin.storage.enrichment import LushaEnrichmentProvider

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "email": {"email": "ana@nubank.com.br"},
                    "phoneNumbers": [{"number": "+55 11 99999-0000"}],
                }
            },
        )

    provider = LushaEnrichmentProvider(
        "lusha-key", transport=httpx.MockTransport(handler)
    )
    updates = provider.enrich(
        [_lead()],
        EnrichmentOptions(fields="both", providers=["lusha"]),
    )

    assert len(updates) == 1
    assert updates[0].email == "ana@nubank.com.br"
    assert updates[0].phone == "+55 11 99999-0000"
    assert str(requests[0].url) == "https://api.lusha.com/v2/person"
    payload = json_from_request(requests[0])
    assert payload["metadata"]["filterBy"] == "linkedinUrl"
    assert payload["contact"]["linkedinUrl"] == "https://www.linkedin.com/in/ana-silva/"
    assert payload["revealEmails"] is True
    assert payload["revealPhones"] is True


def test_snovio_provider_uses_name_domain_start_and_result_flow() -> None:
    from beautiful_linkedin.storage.enrichment import SnovioEnrichmentProvider

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url).endswith("/v1/oauth/access_token"):
            return httpx.Response(200, json={"access_token": "token"})
        if "/v2/emails-by-domain-by-name/start" in str(request.url):
            return httpx.Response(200, json={"data": {"task_hash": "hash-1"}})
        if "/v2/emails-by-domain-by-name/result" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "data": [
                        {
                            "result": {
                                "emails": [
                                    {"email": "ana@nubank.com.br", "status": "valid"}
                                ]
                            }
                        }
                    ],
                },
            )
        raise AssertionError(str(request.url))

    provider = SnovioEnrichmentProvider(
        client_id="id",
        client_secret="secret",
        transport=httpx.MockTransport(handler),
        poll_sleep=lambda _: None,
    )
    updates = provider.enrich(
        [_lead()],
        EnrichmentOptions(fields="email", providers=["snovio"]),
    )

    assert updates[0].email == "ana@nubank.com.br"
    assert any("/v2/emails-by-domain-by-name/start" in url for url in seen)
    assert any("/v2/emails-by-domain-by-name/result" in url for url in seen)


def json_from_request(request: httpx.Request) -> dict[str, Any]:
    import json

    return json.loads(request.content.decode("utf-8"))
