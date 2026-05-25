"""End-to-end offline tests for the phone enrichment branch of
``/lead-tables/{id}/internal-enrich``.

Covers:
- blocking endpoint with ``fields="phone"`` populates Lead.phone +
  trail columns from a fake harvester,
- ``fields="both"`` runs e-mail and phone in the same call,
- existing phone is never overwritten (skipped counter),
- harvester returning nothing yields ``failed_no_phone_candidate``,
- the full pipeline composes the merge helper (phone_verified_by gets
  seeded with the source label).

All HTTP / DNS / SMTP is stubbed; ``phonenumbers`` is real because it's
pure-Python and offline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from beautiful_linkedin.models import Lead
from beautiful_linkedin.server.app import build_app
from beautiful_linkedin.storage.phone_harvester import HarvestedPhone


@pytest.fixture(autouse=True)
def _disable_real_network(monkeypatch):
    """Block every network-touching default in the phone pipeline.

    The phone orchestrator wires SERP providers, a wa.me checker and
    the e-mail-side domain discoverer by default. Without these
    stubs the test process would hit DuckDuckGo / wa.me / crt.sh
    every run — slow, flaky, and a courtesy violation. Tests that
    *want* to exercise a real source override individual factories
    explicitly.
    """
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_domain_discoverer",
        lambda: None,
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_phone_lookup_providers",
        lambda _settings: [],
    )
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_whatsapp_checker",
        lambda: None,
    )


def _lead(
    *,
    person: str,
    domain: str | None = "empresa.com",
    phone: str | None = None,
    company_name: str = "Empresa",
) -> Lead:
    url = f"https://www.linkedin.com/in/{person.lower().replace(' ', '-')}/"
    return Lead(
        company_name=company_name,
        company_domain=domain,
        person_name=person,
        title="Marketing Manager",
        linkedin_url=url,
        email=None,
        phone=phone,
        source_url=url,
        source_type="linkedin_people_search",
        snippet="",
        confidence_score=80,
    )


def _create_table(client: TestClient, leads: list[Lead]) -> str:
    return client.post(
        "/lead-tables",
        json={
            "name": "Phone Enrich",
            "leads": [lead.model_dump(mode="json") for lead in leads],
        },
    ).json()["table"]["id"]


def _stub_harvester(phones_by_domain: dict[str, list[HarvestedPhone]]):
    class FakeHarvester:
        def harvest(self, domain: str) -> list[HarvestedPhone]:
            return list(phones_by_domain.get(domain or "", []))

    return FakeHarvester()


def test_phone_endpoint_enriches_from_harvested_candidates(
    tmp_path: Path, monkeypatch
) -> None:
    """A fake harvester returns one mobile and one fixed-line for the
    company; the orchestrator should pick the mobile (higher type
    score) and persist it E.164-normalized with provenance."""
    app = build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))
    client = TestClient(app)

    table_id = _create_table(client, [_lead(person="Ana Silva")])

    phones = {
        "empresa.com": [
            HarvestedPhone(
                raw="(11) 3030-4040",
                digits="1130304040",
                source_url="https://empresa.com/contato",
                context="tel",
            ),
            HarvestedPhone(
                raw="(11) 99999-9999",
                digits="11999999999",
                source_url="https://empresa.com/team",
                context="tel",
            ),
        ]
    }
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_phone_harvester",
        lambda: _stub_harvester(phones),
    )

    response = client.post(
        f"/lead-tables/{table_id}/internal-enrich",
        json={"fields": "phone", "confirmed": True},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    summary = body["summary"]
    assert summary["enriched_phone_leads"] == 1
    assert summary["skipped_existing_phone"] == 0

    enriched = body["leads"][0]
    assert enriched["phone"] == "+5511999999999"
    assert enriched["phone_type"] == "mobile"
    assert enriched["phone_country"] == "BR"
    assert enriched["phone_validation_status"] == "valid"
    assert enriched["phone_source"] == "internal"
    assert "internal" in enriched["phone_verified_by"]


def test_phone_endpoint_skips_lead_with_existing_phone(
    tmp_path: Path, monkeypatch
) -> None:
    app = build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))
    client = TestClient(app)
    table_id = _create_table(
        client,
        [_lead(person="Bruno", phone="+5511777776666")],
    )

    phones = {
        "empresa.com": [
            HarvestedPhone(
                raw="(11) 99999-9999",
                digits="11999999999",
                source_url="https://empresa.com/",
                context="tel",
            )
        ]
    }
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_phone_harvester",
        lambda: _stub_harvester(phones),
    )

    response = client.post(
        f"/lead-tables/{table_id}/internal-enrich",
        json={"fields": "phone", "confirmed": True},
    )
    body = response.json()
    summary = body["summary"]
    assert summary["enriched_phone_leads"] == 0
    assert summary["skipped_existing_phone"] == 1
    # Primary phone untouched.
    assert body["leads"][0]["phone"] == "+5511777776666"


def test_phone_endpoint_failed_when_harvester_returns_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    app = build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))
    client = TestClient(app)
    table_id = _create_table(client, [_lead(person="Ana")])

    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_phone_harvester",
        lambda: _stub_harvester({}),
    )

    response = client.post(
        f"/lead-tables/{table_id}/internal-enrich",
        json={"fields": "phone", "confirmed": True},
    )
    body = response.json()
    assert body["summary"]["failed_no_phone_candidate"] == 1
    assert body["leads"][0]["phone"] is None


def test_phone_endpoint_with_fields_both_runs_email_and_phone(
    tmp_path: Path, monkeypatch
) -> None:
    """``fields="both"`` triggers both pipelines in the same call so the
    UI can offer one button. Email pipeline gets a no-op harvester and
    a permissive MX resolver; phone pipeline gets a fake harvester
    with a valid mobile."""
    app = build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))
    client = TestClient(app)
    table_id = _create_table(client, [_lead(person="Ana")])

    # ---- e-mail wiring -----------------------------------------------------
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
    # ---- phone wiring ------------------------------------------------------
    phones = {
        "empresa.com": [
            HarvestedPhone(
                raw="(11) 99999-9999",
                digits="11999999999",
                source_url="https://empresa.com/contato",
                context="tel",
            )
        ]
    }
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_phone_harvester",
        lambda: _stub_harvester(phones),
    )

    response = client.post(
        f"/lead-tables/{table_id}/internal-enrich",
        json={"fields": "both", "confirmed": True},
    )
    body = response.json()
    summary = body["summary"]
    # Email path will run (and likely yield a candidate via the basic
    # pattern generator); regardless of the exact count, the *phone*
    # counter must reflect that the phone branch executed.
    assert summary["enriched_phone_leads"] == 1
    assert body["leads"][0]["phone"] == "+5511999999999"


def test_phone_endpoint_emits_stream_events_for_phone_channel(
    tmp_path: Path, monkeypatch
) -> None:
    """SSE consumer needs ``channel="phone"`` on phone-side events when
    ``fields="both"`` so the UI can demultiplex e-mail vs phone progress."""
    import json

    app = build_app(saved_leads_path=str(tmp_path / "saved.sqlite"))
    client = TestClient(app)
    table_id = _create_table(client, [_lead(person="Ana")])

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
    phones = {
        "empresa.com": [
            HarvestedPhone(
                raw="(11) 99999-9999",
                digits="11999999999",
                source_url="https://empresa.com/",
                context="tel",
            )
        ]
    }
    monkeypatch.setattr(
        "beautiful_linkedin.server.app._default_phone_harvester",
        lambda: _stub_harvester(phones),
    )

    with client.stream(
        "POST",
        f"/lead-tables/{table_id}/internal-enrich/stream",
        json={"fields": "phone", "confirmed": True},
    ) as response:
        events: list[dict[str, Any]] = []
        for line in response.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))

    types = [e["type"] for e in events]
    assert "start" in types
    assert "done" in types
    # Phone-side phase events must be tagged with channel="phone" so the
    # UI can render them in the phone column when ``fields="both"``.
    phone_phase_events = [
        e for e in events if e["type"] == "phase" and e.get("channel") == "phone"
    ]
    assert phone_phase_events, "expected channel-tagged phase events on the phone stream"
    done = next(e for e in events if e["type"] == "done")
    assert done["summary"]["enriched_phone_leads"] == 1
