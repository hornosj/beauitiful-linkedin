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
            # Deprecated: endpoint ignores UI/client pricing and uses the
            # server-side canonical table.
            "credit_costs_brl": {"lusha": 99.99},
            "confirmed": False,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "estimated"
    assert body["estimate"]["selected_leads"] == 1
    assert body["estimate"]["total_estimated_brl"] == 3.0
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


def test_enrich_endpoint_logs_provider_cost_and_marks_no_data_attempt(
    tmp_path: Path, monkeypatch
) -> None:
    app = build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))
    client = TestClient(app)
    table_id = client.post(
        "/lead-tables",
        json={"name": "Enrich", "leads": [_lead().model_dump(mode="json")]},
    ).json()["table"]["id"]

    class EmptyApolloProvider:
        name = "apollo"
        errors: list[str] = []

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def enrich(self, selected: list[Lead], options: EnrichmentOptions) -> list[Any]:
            assert [lead.person_name for lead in selected] == ["Ana Silva"]
            return []

    monkeypatch.setattr(
        "beautiful_linkedin.server.app.ApolloEnrichmentProvider", EmptyApolloProvider
    )

    response = client.post(
        f"/lead-tables/{table_id}/enrich",
        json={
            "lead_refs": ["https://www.linkedin.com/in/ana-silva/"],
            "fields": "email",
            "providers": ["apollo"],
            "credit_costs_brl": {"apollo": 0.3},
            "confirmed": True,
            "api_keys": {"apollo_api_key": "test-apollo"},
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"]["updated_leads"] == 0
    assert body["summary"]["provider_logs"] == [
        {
            "provider": "apollo",
            "requested_leads": 1,
            "matched_leads": 0,
            "updated_leads": 0,
            "estimated_credits": 1,
            "estimated_brl": 0.3,
            "status": "no_data",
            "message": "Apollo consultou 1 lead(s), mas não retornou e-mail.",
        }
    ]

    detail = client.get(f"/lead-tables/{table_id}").json()
    ana = detail["leads"][0]
    assert ana["email"] is None
    assert ana["enrichment_source"] == "apollo"
    assert ana["enrichment_status"] == "api_consulted_no_data"
    assert "Consultado via API apollo; nenhum dado novo retornado." in ana["consultation_note"]


def test_enrich_endpoint_orders_paid_providers_by_cost(
    tmp_path: Path, monkeypatch
) -> None:
    app = build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))
    client = TestClient(app)
    table_id = client.post(
        "/lead-tables",
        json={"name": "Enrich", "leads": [_lead().model_dump(mode="json")]},
    ).json()["table"]["id"]
    calls: list[str] = []

    class FakeApolloProvider:
        name = "apollo"
        errors: list[str] = []

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def enrich(self, selected: list[Lead], options: EnrichmentOptions) -> list[Any]:
            calls.append(self.name)
            return []

    class FakeLushaProvider(FakeApolloProvider):
        name = "lusha"

    monkeypatch.setattr(
        "beautiful_linkedin.server.app.ApolloEnrichmentProvider", FakeApolloProvider
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app.LushaEnrichmentProvider", FakeLushaProvider
    )

    response = client.post(
        f"/lead-tables/{table_id}/enrich",
        json={
            "lead_refs": ["https://www.linkedin.com/in/ana-silva/"],
            "fields": "email",
            "providers": ["lusha", "apollo"],
            "credit_costs_brl": {"apollo": 0.3, "lusha": 3.0},
            "confirmed": True,
            "api_keys": {"apollo_api_key": "test-apollo", "lusha_api_key": "test-lusha"},
        },
    )

    assert response.status_code == 200, response.text
    assert calls == ["apollo", "lusha"]


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


def test_lusha_provider_uses_v2_batch_shape_with_filterby() -> None:
    """Lusha /v2/person é um endpoint de batch: requer `contacts: []` e
    `metadata.filterBy` ∈ {emailAddresses, phoneNumbers}. Não aceita
    `contact` (singular) nem `revealEmails`/`revealPhones`."""
    from beautiful_linkedin.storage.enrichment import LushaEnrichmentProvider

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "requestId": "req-1",
                "contacts": {
                    "0": {
                        "data": {
                            "name": {"full": "Ana Silva"},
                            "emailAddresses": [
                                {"email": "ana@nubank.com.br", "type": "work"}
                            ],
                            "phoneNumbers": [
                                {"number": "+55 11 99999-0000", "type": "mobile"}
                            ],
                        }
                    }
                },
            },
        )

    provider = LushaEnrichmentProvider(
        "lusha-key", transport=httpx.MockTransport(handler)
    )
    updates = provider.enrich(
        [_lead()],
        EnrichmentOptions(fields="email", providers=["lusha"]),
    )

    assert len(updates) == 1
    assert updates[0].email == "ana@nubank.com.br"
    assert str(requests[0].url) == "https://api.lusha.com/v2/person"
    payload = json_from_request(requests[0])
    assert payload["metadata"]["filterBy"] == "emailAddresses"
    assert isinstance(payload["contacts"], list)
    assert len(payload["contacts"]) == 1
    contact = payload["contacts"][0]
    assert contact["contactId"] == "0"
    assert contact["linkedinUrl"] == "https://www.linkedin.com/in/ana-silva/"
    assert contact["fullName"] == "Ana Silva"
    # Properties Lusha v2 rejeita explicitamente:
    assert "contact" not in payload
    assert "revealEmails" not in payload
    assert "revealPhones" not in payload


def test_lusha_provider_both_fields_makes_two_calls_one_per_filter() -> None:
    """Quando fields='both', Lusha v2 não aceita filtrar pelos dois ao
    mesmo tempo. O provider deve fazer duas chamadas (uma por filtro)."""
    from beautiful_linkedin.storage.enrichment import LushaEnrichmentProvider

    filter_bys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json_from_request(request)
        filter_bys.append(body["metadata"]["filterBy"])
        if body["metadata"]["filterBy"] == "emailAddresses":
            payload = {
                "contacts": {
                    "0": {
                        "data": {
                            "emailAddresses": [{"email": "ana@nubank.com.br"}]
                        }
                    }
                }
            }
        else:
            payload = {
                "contacts": {
                    "0": {
                        "data": {
                            "phoneNumbers": [{"number": "+55 11 99999-0000"}]
                        }
                    }
                }
            }
        return httpx.Response(200, json=payload)

    provider = LushaEnrichmentProvider(
        "lusha-key", transport=httpx.MockTransport(handler)
    )
    updates = provider.enrich(
        [_lead()],
        EnrichmentOptions(fields="both", providers=["lusha"]),
    )

    assert sorted(filter_bys) == ["emailAddresses", "phoneNumbers"]
    assert len(updates) == 1
    assert updates[0].email == "ana@nubank.com.br"
    assert updates[0].phone == "+55 11 99999-0000"


def test_pdl_provider_uses_v5_person_enrich_with_xapikey_header() -> None:
    """PDL Person Enrichment v5: GET /v5/person/enrich com X-Api-Key.
    Resposta status=200 e data.{work_email,emails,mobile_phone,phone_numbers}.
    """
    from beautiful_linkedin.storage.enrichment import PdlEnrichmentProvider

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": 200,
                "likelihood": 9,
                "data": {
                    "full_name": "ana silva",
                    "work_email": "ana@nubank.com.br",
                    "emails": [
                        {"address": "ana.personal@gmail.com", "type": "personal"}
                    ],
                    "mobile_phone": "+55 11 99999-0000",
                    "phone_numbers": ["+55 11 4002-8922"],
                },
            },
        )

    provider = PdlEnrichmentProvider(
        "pdl-key", transport=httpx.MockTransport(handler)
    )
    updates = provider.enrich(
        [_lead()],
        EnrichmentOptions(fields="both", providers=["pdl"]),
    )

    assert len(updates) == 1
    assert updates[0].email == "ana@nubank.com.br"
    assert updates[0].phone == "+55 11 99999-0000"
    assert requests[0].method == "GET"
    assert "/v5/person/enrich" in str(requests[0].url)
    assert requests[0].headers.get("x-api-key") == "pdl-key"
    # PDL prioriza casamento via linkedin_url (profile param).
    assert "profile=" in str(requests[0].url) or "linkedin" in str(requests[0].url).lower()


def test_pdl_provider_skips_when_status_not_200() -> None:
    """PDL retorna status 404 quando não encontra match — não deve gerar update."""
    from beautiful_linkedin.storage.enrichment import PdlEnrichmentProvider

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": 404, "error": {"message": "no matches"}},
        )

    provider = PdlEnrichmentProvider(
        "pdl-key", transport=httpx.MockTransport(handler)
    )
    updates = provider.enrich(
        [_lead()],
        EnrichmentOptions(fields="email", providers=["pdl"]),
    )

    assert updates == []


def test_estimate_enrichment_cost_includes_pdl() -> None:
    """PDL cobra crédito por match bem-sucedido — entra na estimativa
    junto com os outros providers."""
    leads = [_lead(person="Ana"), _lead(person="Bruno", linkedin_url="https://www.linkedin.com/in/bruno/")]
    options = EnrichmentOptions(
        fields="email",
        providers=["pdl", "lusha"],
        credit_costs_brl={"pdl": 0.5, "lusha": 3.0},
    )

    estimate = estimate_enrichment_cost(leads, options)

    providers_in_estimate = {item.provider for item in estimate.provider_estimates}
    assert "pdl" in providers_in_estimate
    pdl_item = next(item for item in estimate.provider_estimates if item.provider == "pdl")
    # 2 leads × 1 field (email) = 2 credits × R$0,50 = R$1,00
    assert pdl_item.estimated_credits == 2
    assert pdl_item.estimated_brl == 1.0


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


# ---------------------------------------------------------------------------
# Cross-provider email verification trail
# ---------------------------------------------------------------------------


def test_merge_email_verification_marks_badge_when_sources_agree() -> None:
    """Two providers landing on the same email must promote the lead to
    verified — the badge in the UI hangs off ``email_verified_by`` having
    >= 2 entries. The primary email never changes."""
    from beautiful_linkedin.storage.saved_leads import _merge_email_verification

    verified_by, alternatives = _merge_email_verification(
        existing_email="ana.silva@acme.com",
        existing_verified_by=["internal"],
        existing_alternatives=[],
        incoming_email="ana.silva@acme.com",  # same address Apollo found
        incoming_source="apollo",
        incoming_confidence=95,
        now="2026-05-18T12:00:00Z",
    )

    assert verified_by == ["internal", "apollo"]
    assert alternatives == []


def test_merge_email_verification_records_disagreeing_email_as_alternative() -> None:
    """When paid returns a different email, keep the primary intact but
    log Apollo's suggestion so the UI can show 'Apollo sugeriu X'."""
    from beautiful_linkedin.storage.saved_leads import _merge_email_verification

    verified_by, alternatives = _merge_email_verification(
        existing_email="ana.silva@acme.com",
        existing_verified_by=["internal"],
        existing_alternatives=[],
        incoming_email="ana@acme.com.br",
        incoming_source="apollo",
        incoming_confidence=92,
        now="2026-05-18T12:00:00Z",
    )

    assert verified_by == ["internal"]  # no badge
    assert len(alternatives) == 1
    assert alternatives[0]["email"] == "ana@acme.com.br"
    assert alternatives[0]["source"] == "apollo"
    assert alternatives[0]["confidence"] == 92


def test_merge_email_verification_dedupes_repeated_alternatives() -> None:
    """The same provider proposing the same alternative twice (e.g. a
    re-run of paid enrichment) must not duplicate the alternative."""
    from beautiful_linkedin.storage.saved_leads import _merge_email_verification

    existing_alt = {
        "email": "ana@acme.io",
        "source": "apollo",
        "confidence": 90,
        "found_at": "2026-05-18T10:00:00Z",
    }
    verified_by, alternatives = _merge_email_verification(
        existing_email="ana.silva@acme.com",
        existing_verified_by=["internal"],
        existing_alternatives=[existing_alt],
        incoming_email="ana@acme.io",
        incoming_source="apollo",
        incoming_confidence=90,
        now="2026-05-18T12:00:00Z",
    )
    assert len(alternatives) == 1


def test_paid_after_internal_with_same_email_persists_verified_badge(
    tmp_path: Path,
) -> None:
    """End-to-end through SQLite: internal finds ``ana@acme.com`` →
    paid Apollo confirms it → reading the lead back must show both
    providers on ``email_verified_by`` so the UI badge renders."""
    from beautiful_linkedin.storage.internal_enrichment import (
        EnrichmentStatus,
        EnrichmentUpdate,
    )
    from beautiful_linkedin.storage.saved_leads import SavedLeadsStore

    store = SavedLeadsStore(str(tmp_path / "saved.sqlite"))
    table = store.create_table(name="Cross-source")
    store.add_leads(table.id, [_lead(person="Ana Silva", email=None)])
    saved = store.list_leads(table.id)
    lead = saved[0]

    # Step 1: internal enrichment finds an email.
    store.apply_internal_enrichment_updates(
        table.id,
        [
            (
                lead,
                EnrichmentUpdate(
                    email="ana.silva@nubank.com.br",
                    enrichment_source="internal",
                    enrichment_status=EnrichmentStatus.ENRICHED,
                    enrichment_confidence=75,
                    email_type="work",
                ),
            )
        ],
    )

    after_internal = store.list_leads(table.id)[0]
    assert after_internal.email == "ana.silva@nubank.com.br"
    assert after_internal.email_verified_by == ["internal"]
    assert after_internal.email_alternatives == []

    # Step 2: paid Apollo returns the SAME email — should NOT overwrite,
    # but must append "apollo" to verified_by so the UI shows the badge.
    class _PaidUpdate:
        def __init__(self, lead: Lead, email: str) -> None:
            self.lead = lead
            self.email = email
            self.phone = None
            self.provider = "apollo"
            self.raw: dict[str, Any] = {}
            self.note = None

    store.apply_enrichment_updates(
        table.id,
        [_PaidUpdate(after_internal, "ana.silva@nubank.com.br")],
    )

    after_paid = store.list_leads(table.id)[0]
    assert after_paid.email == "ana.silva@nubank.com.br"  # primary intact
    assert after_paid.email_verified_by == ["internal", "apollo"]
    assert after_paid.email_alternatives == []


def test_paid_after_internal_with_different_email_records_alternative(
    tmp_path: Path,
) -> None:
    """Internal finds ``ana@acme.com``, paid Apollo returns
    ``ana@acme.io`` — primary stays, Apollo's value goes to alternatives,
    no badge is shown (verified_by stays at 1 entry)."""
    from beautiful_linkedin.storage.internal_enrichment import (
        EnrichmentStatus,
        EnrichmentUpdate,
    )
    from beautiful_linkedin.storage.saved_leads import SavedLeadsStore

    store = SavedLeadsStore(str(tmp_path / "saved.sqlite"))
    table = store.create_table(name="Cross-source")
    store.add_leads(table.id, [_lead(person="Ana Silva", email=None)])
    lead = store.list_leads(table.id)[0]

    store.apply_internal_enrichment_updates(
        table.id,
        [
            (
                lead,
                EnrichmentUpdate(
                    email="ana@acme.com",
                    enrichment_source="internal",
                    enrichment_status=EnrichmentStatus.ENRICHED,
                    enrichment_confidence=70,
                    email_type="work",
                ),
            )
        ],
    )

    class _PaidUpdate:
        def __init__(self, lead: Lead) -> None:
            self.lead = lead
            self.email = "ana@acme.io"
            self.phone = None
            self.provider = "apollo"
            self.raw: dict[str, Any] = {}
            self.note = None

    saved = store.list_leads(table.id)[0]
    store.apply_enrichment_updates(table.id, [_PaidUpdate(saved)])

    final = store.list_leads(table.id)[0]
    assert final.email == "ana@acme.com"
    assert final.email_verified_by == ["internal"]
    assert len(final.email_alternatives) == 1
    assert final.email_alternatives[0]["email"] == "ana@acme.io"
    assert final.email_alternatives[0]["source"] == "apollo"


def test_merge_email_verification_seeds_trail_when_no_existing_primary() -> None:
    """First write: the incoming email becomes the primary, the source
    is the only entry in the verification trail. No alternatives yet."""
    from beautiful_linkedin.storage.saved_leads import _merge_email_verification

    verified_by, alternatives = _merge_email_verification(
        existing_email=None,
        existing_verified_by=[],
        existing_alternatives=[],
        incoming_email="ana.silva@acme.com",
        incoming_source="apollo",
        incoming_confidence=95,
        now="2026-05-18T12:00:00Z",
    )
    assert verified_by == ["apollo"]
    assert alternatives == []
