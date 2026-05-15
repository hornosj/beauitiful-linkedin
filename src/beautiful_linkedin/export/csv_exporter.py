from __future__ import annotations

from pathlib import Path

import pandas as pd

from beautiful_linkedin.models import Lead

OUTPUT_COLUMNS = [
    "company_name",
    "company_domain",
    "person_name",
    "title",
    "linkedin_url",
    "email",
    "phone",
    "source_url",
    "source_type",
    "snippet",
    "matched_title",
    "validation_status",
    "validation_note",
    "confidence_score",
    "previously_consulted_at",
    "consultation_note",
]


def leads_to_dataframe(leads: list[Lead]) -> pd.DataFrame:
    records = [lead.model_dump() for lead in leads]
    return pd.DataFrame(records, columns=OUTPUT_COLUMNS)


def export_csv(leads: list[Lead], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    leads_to_dataframe(leads).to_csv(path, index=False)
    return path
