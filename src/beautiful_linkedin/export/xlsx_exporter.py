from __future__ import annotations

from pathlib import Path

from beautiful_linkedin.export.csv_exporter import leads_to_dataframe
from beautiful_linkedin.models import Lead


def export_xlsx(leads: list[Lead], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    leads_to_dataframe(leads).to_excel(path, index=False)
    return path
