"""TDD for the /lead-tables/{id}/internal-enrich endpoint.

The endpoint orchestrates the in-process enrichment service over a table
of saved leads. It accepts ``lead_refs`` to scope the work, falls back to
all leads when omitted, and returns a categorical summary (enriched,
skipped_existing_email, failed_missing_domain).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from beautiful_linkedin.models import Lead
from beautiful_linkedin.server.app import build_app


@pytest.fixture(autouse=True)
def _disable_real_domain_discovery(monkeypatch):
    """The production discoverer fans out to crt.sh + DNS TXT. Stub it
    out across every endpoint test so they stay fully offline. Tests
    that want to exercise discovery override this fixture explicitly."""
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_domain_discoverer",
        lambda: None,
    )


def _lead(
    *,
    person: str,
    domain: str | None = "empresa.com",
    email: str | None = None,
    linkedin_url: str | None = None,
    company_name: str = "Empresa",
) -> Lead:
    url = linkedin_url or f"https://www.linkedin.com/in/{person.lower().replace(' ', '-')}/"
    return Lead(
        company_name=company_name,
        company_domain=domain,
        person_name=person,
        title="Marketing Manager",
        linkedin_url=url,
        email=email,
        source_url=url,
        source_type="linkedin_people_search",
        snippet="",
        confidence_score=80,
    )


def _create_table(client: TestClient, leads: list[Lead]) -> str:
    return client.post(
        "/lead-tables",
        json={
            "name": "Internal Enrich",
            "leads": [lead.model_dump(mode="json") for lead in leads],
        },
    ).json()["table"]["id"]


def _stub_mx(_: object) -> Any:
    """Force MX resolver to always succeed inside the FastAPI app context."""

    def factory(domain: str) -> bool:
        return True

    return factory


def test_internal_enrich_endpoint_enriches_selected_leads_and_skips_existing_email(
    tmp_path: Path, monkeypatch
) -> None:
    app = build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))
    client = TestClient(app)

    leads = [
        _lead(person="Ana Silva"),
        _lead(person="Bruno Costa", email="bruno.precadastrado@empresa.com"),
        # Different company, no domain anywhere on the table → genuinely
        # cannot be enriched. Keeps this test focused on the "missing
        # domain" path; cross-company inheritance is covered separately.
        _lead(person="Sem Domínio", domain=None, company_name="Outra Empresa"),
    ]
    table_id = _create_table(client, leads)

    # Replace the live DNS resolver with a fake that says every domain has MX.
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_mx_resolver",
        lambda: lambda _: True,
    )
    # Force harvester offline: return a no-op harvester so it never hits
    # the network during tests.
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_company_email_harvester",
        lambda: type("Noop", (), {"harvest": lambda self, d: []})(),
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_mailbox_verifier",
        lambda: None,
        raising=False,
    )

    response = client.post(
        f"/lead-tables/{table_id}/internal-enrich",
        json={"fields": "email", "confirmed": True},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    summary = body["summary"]
    assert summary["requested_leads"] == 3
    assert summary["enriched_leads"] == 1
    assert summary["skipped_existing_email"] == 1
    assert summary["failed_missing_domain"] == 1

    # Read leads back — Ana now has an e-mail, Bruno's original survives,
    # 'Sem Domínio' is unchanged but has enrichment metadata recorded.
    detail = client.get(f"/lead-tables/{table_id}").json()
    ana = next(l for l in detail["leads"] if l["person_name"] == "Ana Silva")
    bruno = next(l for l in detail["leads"] if l["person_name"] == "Bruno Costa")
    no_domain = next(l for l in detail["leads"] if l["person_name"] == "Sem Domínio")

    assert ana["email"] is not None and ana["email"].endswith("@empresa.com")
    assert ana["enrichment_source"] == "internal"
    assert ana["enrichment_status"] in {"enriched", "estimated"}
    assert ana["email_type"] == "work"

    assert bruno["email"] == "bruno.precadastrado@empresa.com"
    assert bruno["enrichment_source"] == "internal"

    assert no_domain["email"] is None
    assert no_domain["enrichment_status"] == "failed"


def test_internal_enrich_endpoint_scopes_work_to_lead_refs(
    tmp_path: Path, monkeypatch
) -> None:
    app = build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))
    client = TestClient(app)

    leads = [
        _lead(person="Ana Silva"),
        _lead(
            person="Carla Lima",
            linkedin_url="https://www.linkedin.com/in/carla/",
        ),
    ]
    table_id = _create_table(client, leads)

    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_mx_resolver",
        lambda: lambda _: True,
    )
    # Force harvester offline: return a no-op harvester so it never hits
    # the network during tests.
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_company_email_harvester",
        lambda: type("Noop", (), {"harvest": lambda self, d: []})(),
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_mailbox_verifier",
        lambda: None,
        raising=False,
    )

    response = client.post(
        f"/lead-tables/{table_id}/internal-enrich",
        json={
            "lead_refs": ["https://www.linkedin.com/in/carla/"],
            "fields": "email",
            "confirmed": True,
        },
    )

    body = response.json()
    assert body["summary"]["requested_leads"] == 1
    detail = client.get(f"/lead-tables/{table_id}").json()
    ana = next(l for l in detail["leads"] if l["person_name"] == "Ana Silva")
    carla = next(l for l in detail["leads"] if l["person_name"] == "Carla Lima")
    # Carla got enriched, Ana did NOT (she was not in lead_refs).
    assert carla["email"] is not None
    assert ana["email"] is None


def test_internal_enrich_endpoint_accepts_domain_override_for_missing_domains(
    tmp_path: Path, monkeypatch
) -> None:
    app = build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))
    client = TestClient(app)
    table_id = _create_table(client, [_lead(person="Ana Silva", domain=None)])

    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_mx_resolver",
        lambda: lambda _: True,
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_company_email_harvester",
        lambda: type("Noop", (), {"harvest": lambda self, d: []})(),
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_mailbox_verifier",
        lambda: None,
        raising=False,
    )

    response = client.post(
        f"/lead-tables/{table_id}/internal-enrich",
        json={
            "fields": "email",
            "confirmed": True,
            "company_domain": "empresa.com",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"]["enriched_leads"] == 1
    assert body["summary"]["failed_missing_domain"] == 0
    ana = next(l for l in body["leads"] if l["person_name"] == "Ana Silva")
    assert ana["email"] is not None and ana["email"].endswith("@empresa.com")


def test_internal_enrich_endpoint_uses_harvested_locals_to_pick_pattern(
    tmp_path: Path, monkeypatch
) -> None:
    """When the company website exposes ``first.last`` emails, the endpoint
    should detect that pattern and persist ``ana.silva@empresa.com`` —
    not the alphabetical-first candidate ``ana@empresa.com``."""
    app = build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))
    client = TestClient(app)
    table_id = _create_table(client, [_lead(person="Ana Silva")])

    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_mx_resolver",
        lambda: lambda _: True,
    )

    class StubHarvester:
        def harvest(self, domain: str):
            from beautiful_linkedin.storage.company_email_harvester import (
                HarvestedEmail,
            )
            return [
                HarvestedEmail(
                    email=f"{local}@empresa.com",
                    source_url="https://empresa.com/",
                )
                for local in ("bruno.costa", "carla.lima", "danilo.santos")
            ]

    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_company_email_harvester",
        lambda: StubHarvester(),
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_mailbox_verifier",
        lambda: None,
        raising=False,
    )

    response = client.post(
        f"/lead-tables/{table_id}/internal-enrich",
        json={"fields": "email", "confirmed": True},
    )
    assert response.status_code == 200
    detail = client.get(f"/lead-tables/{table_id}").json()
    ana = next(l for l in detail["leads"] if l["person_name"] == "Ana Silva")
    assert ana["email"] == "ana.silva@empresa.com"
    assert ana["enrichment_status"] == "enriched"


def test_internal_enrich_endpoint_marks_exact_published_email_as_valid(
    tmp_path: Path, monkeypatch
) -> None:
    app = build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))
    client = TestClient(app)
    table_id = _create_table(client, [_lead(person="Ana Silva")])

    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_mx_resolver",
        lambda: lambda _: True,
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_mailbox_verifier",
        lambda: None,
        raising=False,
    )

    class StubHarvester:
        def harvest(self, domain: str):
            from beautiful_linkedin.storage.company_email_harvester import (
                HarvestedEmail,
            )
            return [
                HarvestedEmail(
                    email="ana.silva@empresa.com",
                    source_url="https://empresa.com/equipe",
                )
            ]

    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_company_email_harvester",
        lambda: StubHarvester(),
    )

    response = client.post(
        f"/lead-tables/{table_id}/internal-enrich",
        json={"fields": "email", "confirmed": True},
    )

    assert response.status_code == 200
    detail = client.get(f"/lead-tables/{table_id}").json()
    ana = next(l for l in detail["leads"] if l["person_name"] == "Ana Silva")
    assert ana["email"] == "ana.silva@empresa.com"
    assert ana["email_validation_status"] == "valid"


def test_internal_enrich_inherits_domain_from_colleagues_email(
    tmp_path: Path, monkeypatch
) -> None:
    """A lead with no ``company_domain`` should still get enriched when
    another lead at the same company already has an email — the domain
    from that email is reused. Mirrors the real flow where Apollo found
    ``bruno@nubank.com.br`` for one lead and we want every other Nubank
    lead in the table to inherit that domain."""
    app = build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))
    client = TestClient(app)

    leads = [
        _lead(
            person="Bruno Costa",
            email="bruno.costa@nubank.com.br",
            domain=None,
            company_name="Nubank",
        ),
        _lead(
            person="Ana Silva",
            domain=None,
            company_name="Nubank",
            linkedin_url="https://www.linkedin.com/in/ana-silva-nu/",
        ),
    ]
    table_id = _create_table(client, leads)

    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_mx_resolver",
        lambda: lambda _: True,
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_company_email_harvester",
        lambda: type("Noop", (), {"harvest": lambda self, d: []})(),
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_mailbox_verifier",
        lambda: None,
        raising=False,
    )

    response = client.post(
        f"/lead-tables/{table_id}/internal-enrich",
        json={"fields": "email", "confirmed": True},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"]["enriched_leads"] == 1  # Bruno already had email
    assert body["summary"]["failed_missing_domain"] == 0

    ana = next(l for l in body["leads"] if l["person_name"] == "Ana Silva")
    # The pattern detected from Bruno's email is first.last, so Ana's
    # inferred candidate at nubank.com.br is ana.silva@nubank.com.br.
    assert ana["email"] == "ana.silva@nubank.com.br"
    assert ana["enrichment_source"] == "internal"


def test_internal_enrich_runs_discovery_phase_and_uses_discovered_domain(
    tmp_path: Path, monkeypatch
) -> None:
    """When the lead's column has only ``acme.com`` but discovery finds
    ``acme.com.br`` (via crt.sh / SPF / ccTLD), the orchestrator should
    test it too. Validates that the discovery phase is wired into the
    SSE pipeline end-to-end and produces ``discovery`` events."""
    import json

    from beautiful_linkedin.storage.domain_discovery import DiscoveryResult

    app = build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))
    client = TestClient(app)

    leads = [
        _lead(person="Ana Silva", domain="acme.com", company_name="Acme"),
    ]
    table_id = _create_table(client, leads)

    class FakeDiscoverer:
        def discover(self, seed: str) -> DiscoveryResult:
            assert seed == "acme.com"
            return DiscoveryResult(
                seed=seed,
                domains=["acme.com.br"],
                sources={"crt_sh": ["acme.com.br"], "cctld": [], "spf_dmarc": []},
            )

    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_domain_discoverer",
        lambda: FakeDiscoverer(),
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_mx_resolver",
        lambda: lambda _d: True,
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_company_email_harvester",
        lambda: type("Noop", (), {"harvest": lambda self, d: []})(),
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_mailbox_verifier",
        lambda: None,
        raising=False,
    )

    with client.stream(
        "POST",
        f"/lead-tables/{table_id}/internal-enrich/stream",
        json={"fields": "email", "confirmed": True},
    ) as response:
        assert response.status_code == 200
        events = [
            json.loads(line[len("data:") :].strip())
            for line in response.iter_lines()
            if line and line.startswith("data:")
        ]

    discovery_events = [e for e in events if e["type"] == "discovery"]
    assert len(discovery_events) == 1
    assert discovery_events[0]["seed"] == "acme.com"
    assert "acme.com.br" in discovery_events[0]["domains"]
    assert "crt_sh" in discovery_events[0]["sources"]

    # The discovered domain shows up among the harvested domains and
    # the final lead row records both attempts.
    lead_events = [e for e in events if e["type"] == "lead"]
    assert lead_events
    assert "acme.com.br" in (lead_events[0].get("tested_domains") or [])


def test_internal_enrich_endpoint_validates_field(tmp_path: Path) -> None:
    app = build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))
    client = TestClient(app)
    table_id = _create_table(client, [_lead(person="Ana")])

    # Unknown field values are rejected. ``email``, ``phone`` and
    # ``both`` are all valid now — the schema test covers acceptance;
    # this one only checks that gibberish still 422s.
    response = client.post(
        f"/lead-tables/{table_id}/internal-enrich",
        json={"fields": "smoke-signal", "confirmed": True},
    )
    assert response.status_code == 422


def test_internal_enrich_stream_emits_progress_and_final_summary(
    tmp_path: Path, monkeypatch
) -> None:
    """The SSE endpoint must stream lifecycle events (start, phase,
    progress, lead, done) and persist the same results as the blocking
    endpoint. The UI relies on the per-lead events to render its progress
    list — break that contract and the modal goes silent."""
    import json

    app = build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))
    client = TestClient(app)
    leads = [
        _lead(person="Ana Silva"),
        _lead(
            person="Bruno Costa",
            linkedin_url="https://www.linkedin.com/in/bruno/",
        ),
    ]
    table_id = _create_table(client, leads)

    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_mx_resolver",
        lambda: lambda _: True,
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_company_email_harvester",
        lambda: type("Noop", (), {"harvest": lambda self, d: []})(),
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_mailbox_verifier",
        lambda: None,
        raising=False,
    )

    with client.stream(
        "POST",
        f"/lead-tables/{table_id}/internal-enrich/stream",
        json={"fields": "email", "confirmed": True},
    ) as response:
        assert response.status_code == 200
        events: list[dict] = []
        for line in response.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            events.append(json.loads(payload))

    types = [e["type"] for e in events]
    assert types[0] == "start"
    assert "phase" in types
    assert types.count("lead") == 2
    assert types[-1] == "done"

    done = events[-1]
    assert done["summary"]["enriched_leads"] == 2
    assert len(done["leads"]) == 2
