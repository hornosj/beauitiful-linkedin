from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd
import pytest

from beautiful_linkedin.export.csv_exporter import OUTPUT_COLUMNS
from beautiful_linkedin.models import Lead
from beautiful_linkedin.storage.saved_leads import (
    ENRICHMENT_NOT_ENRICHED,
    ENRICHMENT_NOT_IMPLEMENTED,
    ImportColumnError,
    SOURCE_TYPE_IMPORTED,
    SOURCE_TYPE_MERGED,
    SavedLeadsStore,
)

SAVED_LEADS_EXPORT_COLUMNS = [
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


def _lead(
    *,
    person: str = "Ana Silva",
    company: str = "Nubank",
    title: str = "Head of Marketing",
    linkedin_url: str | None = "https://www.linkedin.com/in/ana-silva/",
    confidence: int = 80,
    source_url: str | None = None,
    email: str | None = None,
    source_type: str = "linkedin_cookie",
) -> Lead:
    return Lead(
        company_name=company,
        person_name=person,
        title=title,
        linkedin_url=linkedin_url,
        email=email,
        source_url=source_url or linkedin_url or "https://example.com/profile",
        source_type=source_type,
        snippet=f"{title} | Sao Paulo",
        matched_title="marketing",
        confidence_score=confidence,
    )


@pytest.fixture
def store(tmp_path: Path) -> SavedLeadsStore:
    return SavedLeadsStore(tmp_path / "saved_leads.sqlite")


def test_create_and_list_tables_round_trip(store: SavedLeadsStore) -> None:
    table = store.create_table(
        name="Marketing Nubank",
        keywords=["marketing", "growth", "marketing"],
        search_queries=["site:linkedin.com/in marketing Nubank"],
        search_request={"company_name": "Nubank", "titles": ["marketing"]},
    )

    assert table.id
    assert table.name == "Marketing Nubank"
    assert table.keywords == ["marketing", "growth"]
    assert table.enrichment_status == ENRICHMENT_NOT_ENRICHED

    tables = store.list_tables()
    assert [t.id for t in tables] == [table.id]
    assert tables[0].lead_count == 0


def test_create_table_strips_secrets_from_search_request(
    store: SavedLeadsStore,
) -> None:
    table = store.create_table(
        name="Sample",
        search_request={
            "company_name": "Nubank",
            "linkedin_cookie": "li_at=AAA",
            "api_keys": {"apollo_api_key": "secret"},
            "linkedin_li_at_cookie": "AAA",
            "titles": ["marketing"],
        },
    )

    persisted = store.get_table(table.id).search_request
    assert "linkedin_cookie" not in persisted
    assert "linkedin_li_at_cookie" not in persisted
    assert "api_keys" not in persisted
    assert persisted["company_name"] == "Nubank"
    assert persisted["titles"] == ["marketing"]


def test_add_leads_deduplicates_within_table(store: SavedLeadsStore) -> None:
    table = store.create_table(name="Dedupe", keywords=["marketing"])
    inserted = store.add_leads(
        table.id,
        [
            _lead(person="Ana", confidence=60),
            _lead(person="Ana", confidence=85),
        ],
    )

    assert inserted == 2  # second update upgrades confidence
    leads = store.list_leads(table.id)
    assert len(leads) == 1
    assert leads[0].confidence_score == 85

    # Re-adding with lower confidence is ignored
    again = store.add_leads(table.id, [_lead(person="Ana", confidence=10)])
    assert again == 0
    assert store.list_leads(table.id)[0].confidence_score == 85


def test_export_csv_matches_official_schema(
    store: SavedLeadsStore, tmp_path: Path
) -> None:
    table = store.create_table(name="Export Test")
    store.add_leads(
        table.id,
        [
            _lead(person="Ana", confidence=88),
            _lead(
                person="Bruno",
                linkedin_url="https://www.linkedin.com/in/bruno/",
                confidence=70,
            ),
        ],
    )

    output = tmp_path / "out.csv"
    result = store.export_csv(table.id, output)
    assert result == output
    assert result.exists()

    with result.open(encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows = list(reader)

    assert header == SAVED_LEADS_EXPORT_COLUMNS
    assert len(rows) == 2
    assert rows[0] == [
        "Nubank",
        "Ana",
        "Ana",
        "https://www.linkedin.com/in/ana-silva/",
        "Head of Marketing",
        "",
        "",
        "",
        "",
        "",
        "",
    ]


def test_export_csv_official_format_uses_output_columns(
    store: SavedLeadsStore, tmp_path: Path
) -> None:
    table = store.create_table(name="Official Export")
    store.add_leads(table.id, [_lead(person="Ana", confidence=88)])
    output = tmp_path / "official.csv"

    result = store.export_csv(table.id, output, format="oficial")

    with result.open(encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
    assert header == OUTPUT_COLUMNS


def test_export_csv_rejects_unknown_format(
    store: SavedLeadsStore, tmp_path: Path
) -> None:
    table = store.create_table(name="Bad Format")
    store.add_leads(table.id, [_lead(person="Ana")])
    with pytest.raises(ValueError):
        store.export_csv(table.id, tmp_path / "x.csv", format="planilha-magica")


def test_export_csv_default_path_uses_slug(
    store: SavedLeadsStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    table = store.create_table(name="Marketing — Nubank ✨")
    store.add_leads(table.id, [_lead(person="Ana")])
    path = store.export_csv(table.id)
    assert path.parent.name == "saved-leads"
    assert path.name.endswith(".csv")
    assert "marketing" in path.name.lower()


def test_import_csv_recognizes_aliases(
    store: SavedLeadsStore, tmp_path: Path
) -> None:
    src = tmp_path / "external.csv"
    df = pd.DataFrame(
        [
            {
                "Nome Completo": "Carla Lima",
                "Empresa": "Acme",
                "Cargo": "Head of Growth",
                "LinkedIn": "https://www.linkedin.com/in/carla/",
            },
            {
                "Full Name": "Diego Souza",
                "Organization": "Beta",
                "Headline": "CMO",
                "LinkedIn": "",
            },
        ]
    )
    df.to_csv(src, index=False)

    table = store.import_table(name="External", file_path=src)
    assert table.source_type == SOURCE_TYPE_IMPORTED
    assert table.lead_count == 2

    leads = store.list_leads(table.id)
    persons = {lead.person_name for lead in leads}
    assert persons == {"Carla Lima", "Diego Souza"}
    diego = next(lead for lead in leads if lead.person_name == "Diego Souza")
    assert diego.source_type == "imported_table"
    assert diego.source_url.startswith("import:external:")
    assert diego.confidence_score == 70
    assert diego.validation_status == "valid"


def test_import_csv_missing_required_columns_raises(
    store: SavedLeadsStore, tmp_path: Path
) -> None:
    src = tmp_path / "broken.csv"
    pd.DataFrame([{"Some Other": "X"}]).to_csv(src, index=False)
    with pytest.raises(ImportColumnError):
        store.import_table(name="Broken", file_path=src)


def test_merge_tables_dedupes_and_combines_keywords(
    store: SavedLeadsStore,
) -> None:
    a = store.create_table(name="A", keywords=["marketing", "growth"])
    b = store.create_table(name="B", keywords=["growth", "cmo"])
    store.add_leads(
        a.id,
        [
            _lead(person="Ana", confidence=60),
            _lead(
                person="Bruno",
                linkedin_url="https://www.linkedin.com/in/bruno/",
                confidence=50,
            ),
        ],
    )
    store.add_leads(
        b.id,
        [
            _lead(person="Ana", confidence=95),
            _lead(
                person="Clara",
                linkedin_url="https://www.linkedin.com/in/clara/",
                confidence=70,
            ),
        ],
    )

    merged = store.merge_tables(name="Merged", table_ids=[a.id, b.id])
    assert merged.source_type == SOURCE_TYPE_MERGED
    assert merged.keywords == ["marketing", "growth", "cmo"]

    leads = store.list_leads(merged.id)
    assert len(leads) == 3  # Ana deduped between sources
    ana = next(lead for lead in leads if lead.person_name == "Ana")
    assert ana.confidence_score == 95


def test_delete_table_removes_leads(store: SavedLeadsStore) -> None:
    table = store.create_table(name="DropMe")
    store.add_leads(table.id, [_lead()])
    store.delete_table(table.id)

    with pytest.raises(KeyError):
        store.get_table(table.id)
    assert store.list_tables() == []


def test_mark_enrichment_updates_status(store: SavedLeadsStore) -> None:
    table = store.create_table(name="Enrich")
    store.add_leads(table.id, [_lead()])
    updated = store.mark_enrichment(table.id, status=ENRICHMENT_NOT_IMPLEMENTED)
    assert updated.enrichment_status == ENRICHMENT_NOT_IMPLEMENTED
