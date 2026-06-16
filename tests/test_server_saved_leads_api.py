from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from beautiful_linkedin.server.app import build_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = build_app(saved_leads_path=str(tmp_path / "saved_leads.sqlite"))
    return TestClient(app)


def _lead_payload(
    *,
    person: str = "Ana Silva",
    linkedin_url: str = "https://www.linkedin.com/in/ana-silva/",
    confidence: int = 88,
) -> dict[str, Any]:
    return {
        "company_name": "Nubank",
        "person_name": person,
        "title": "Head of Marketing",
        "linkedin_url": linkedin_url,
        "source_url": linkedin_url,
        "source_type": "linkedin_cookie",
        "snippet": "",
        "confidence_score": confidence,
    }


def test_create_list_and_get_lead_table(client: TestClient) -> None:
    create = client.post(
        "/lead-tables",
        json={
            "name": "Marketing Nubank",
            "leads": [_lead_payload()],
            "keywords": ["marketing", "growth"],
            "search_request": {
                "company_name": "Nubank",
                "titles": ["marketing"],
                "linkedin_cookie": "li_at=AAA",
            },
            "generate_queries": True,
        },
    )
    assert create.status_code == 201, create.text
    body = create.json()
    table_id = body["table"]["id"]
    assert body["table"]["lead_count"] == 1
    assert body["table"]["keywords"] == ["marketing", "growth"]
    assert "linkedin_cookie" not in body["table"]["search_request"]
    assert body["table"]["search_queries"], "queries should be generated"

    listing = client.get("/lead-tables").json()
    assert any(item["id"] == table_id for item in listing)

    detail = client.get(f"/lead-tables/{table_id}").json()
    assert detail["table"]["id"] == table_id
    assert len(detail["leads"]) == 1
    assert detail["leads"][0]["person_name"] == "Ana Silva"


def test_export_endpoint_writes_csv(client: TestClient, tmp_path: Path) -> None:
    created = client.post(
        "/lead-tables",
        json={"name": "Export", "leads": [_lead_payload()], "keywords": []},
    ).json()
    table_id = created["table"]["id"]
    target = tmp_path / "exported.csv"

    response = client.post(
        f"/lead-tables/{table_id}/export",
        json={"output_path": str(target)},
    )
    assert response.status_code == 200
    assert response.json()["output_path"] == str(target)
    assert target.exists()
    content = target.read_text(encoding="utf-8").splitlines()
    header = content[0].split(",")
    assert header == [
        "Company Name",
        "First Name",
        "Full Name",
        "LinkedIn",
        "Cargo",
        "E-mail",
        "Name S",
        "Full Name S",
        "Cargo",
        "LinkedIn",
        "Telefone",
    ]


def test_export_endpoint_supports_official_format(
    client: TestClient, tmp_path: Path
) -> None:
    created = client.post(
        "/lead-tables",
        json={"name": "Export Off", "leads": [_lead_payload()], "keywords": []},
    ).json()
    table_id = created["table"]["id"]
    target = tmp_path / "official.csv"

    response = client.post(
        f"/lead-tables/{table_id}/export",
        json={"output_path": str(target), "format": "oficial"},
    )
    assert response.status_code == 200
    header = target.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert header[0] == "company_name"
    assert "confidence_score" in header


def test_export_endpoint_rejects_unknown_format(client: TestClient) -> None:
    table_id = client.post(
        "/lead-tables", json={"name": "Bad Fmt", "leads": [_lead_payload()]}
    ).json()["table"]["id"]
    response = client.post(
        f"/lead-tables/{table_id}/export",
        json={"format": "planilha-magica"},
    )
    assert response.status_code == 422


def test_enrich_endpoint_estimates_saved_lead_enrichment(client: TestClient) -> None:
    table_id = client.post(
        "/lead-tables", json={"name": "Enrich", "leads": [_lead_payload()]}
    ).json()["table"]["id"]
    response = client.post(
        f"/lead-tables/{table_id}/enrich",
        json={
            "lead_refs": ["https://www.linkedin.com/in/ana-silva/"],
            "fields": "email",
            "providers": ["lusha"],
            # Pricing vem da tabela canônica do servidor; payload é ignorado.
            "credit_costs_brl": {"lusha": 99.99},
            "confirmed": False,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "estimated"
    # Default tabelado no servidor para lusha = R$3,00.
    assert body["estimate"]["total_estimated_brl"] == 3.0


def test_enrichment_pricing_endpoint_returns_default_table(client: TestClient) -> None:
    response = client.get("/enrichment/pricing")
    assert response.status_code == 200
    body = response.json()
    providers = {item["provider"]: item for item in body["items"]}
    assert set(providers.keys()) == {"apollo", "lusha", "snovio", "pdl"}
    assert providers["apollo"]["brl_per_credit"] == 0.30
    assert providers["lusha"]["brl_per_credit"] == 3.00
    assert providers["apollo"]["source"] == "default"
    assert providers["apollo"]["env_var"] == "ENRICHMENT_COST_BRL_APOLLO"


def test_enrichment_pricing_endpoint_honours_env_override(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv("ENRICHMENT_COST_BRL_APOLLO", "0.18")
    response = client.get("/enrichment/pricing")
    assert response.status_code == 200
    apollo = next(item for item in response.json()["items"] if item["provider"] == "apollo")
    assert apollo["brl_per_credit"] == 0.18
    assert apollo["source"] == "env_override"


def test_merge_endpoint_dedupes_across_tables(client: TestClient) -> None:
    a = client.post(
        "/lead-tables",
        json={
            "name": "A",
            "leads": [_lead_payload(confidence=60)],
            "keywords": ["marketing"],
        },
    ).json()["table"]["id"]
    b = client.post(
        "/lead-tables",
        json={
            "name": "B",
            "leads": [
                _lead_payload(confidence=95),
                _lead_payload(
                    person="Bruno",
                    linkedin_url="https://www.linkedin.com/in/bruno/",
                    confidence=70,
                ),
            ],
            "keywords": ["growth"],
        },
    ).json()["table"]["id"]

    merged = client.post(
        "/lead-tables/merge",
        json={"name": "Merged", "table_ids": [a, b]},
    )
    assert merged.status_code == 201, merged.text
    payload = merged.json()
    assert payload["table"]["source_type"] == "merged"
    persons = {lead["person_name"] for lead in payload["leads"]}
    assert persons == {"Ana Silva", "Bruno"}
    ana = next(lead for lead in payload["leads"] if lead["person_name"] == "Ana Silva")
    assert ana["confidence_score"] == 95
    assert payload["table"]["keywords"] == ["marketing", "growth"]


def test_import_endpoint_reads_csv(client: TestClient, tmp_path: Path) -> None:
    src = tmp_path / "external.csv"
    pd.DataFrame(
        [
            {
                "Nome Completo": "Carla Lima",
                "Empresa": "Acme",
                "Cargo": "Head of Growth",
                "LinkedIn": "https://www.linkedin.com/in/carla/",
            }
        ]
    ).to_csv(src, index=False)

    response = client.post(
        "/lead-tables/import",
        json={"name": "External", "file_path": str(src)},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["table"]["source_type"] == "imported"
    assert body["leads"][0]["person_name"] == "Carla Lima"


def test_import_endpoint_reports_missing_columns(
    client: TestClient, tmp_path: Path
) -> None:
    src = tmp_path / "bad.csv"
    pd.DataFrame([{"unrelated": 1}]).to_csv(src, index=False)
    response = client.post(
        "/lead-tables/import",
        json={"name": "Bad", "file_path": str(src)},
    )
    assert response.status_code == 422


def test_delete_endpoint(client: TestClient) -> None:
    table_id = client.post(
        "/lead-tables", json={"name": "Drop", "leads": [_lead_payload()]}
    ).json()["table"]["id"]
    response = client.delete(f"/lead-tables/{table_id}")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert client.get(f"/lead-tables/{table_id}").status_code == 404


def test_select_email_endpoint_overrides_primary(client: TestClient) -> None:
    payload = {**_lead_payload(), "email": "ana@nubank.com.br"}
    table_id = client.post(
        "/lead-tables", json={"name": "Pick", "leads": [payload]}
    ).json()["table"]["id"]
    lead_key = client.get(f"/lead-tables/{table_id}").json()["leads"][0]["lead_key"]
    assert lead_key

    resp = client.post(
        f"/lead-tables/{table_id}/select-email",
        json={"lead_key": lead_key, "email": "ana.silva@nubank.com.br"},
    )
    assert resp.status_code == 200, resp.text
    lead = resp.json()["leads"][0]
    assert lead["email"] == "ana.silva@nubank.com.br"
    assert lead["email_selected_by_user"] is True
    assert "ana@nubank.com.br" in {a["email"] for a in lead["email_alternatives"]}


def test_select_email_endpoint_rejects_bad_email(client: TestClient) -> None:
    payload = {**_lead_payload(), "email": "ana@nubank.com.br"}
    table_id = client.post(
        "/lead-tables", json={"name": "Pick", "leads": [payload]}
    ).json()["table"]["id"]
    lead_key = client.get(f"/lead-tables/{table_id}").json()["leads"][0]["lead_key"]

    resp = client.post(
        f"/lead-tables/{table_id}/select-email",
        json={"lead_key": lead_key, "email": "nope"},
    )
    assert resp.status_code == 422
