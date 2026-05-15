from __future__ import annotations

from collections import Counter
from pathlib import Path

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table

from beautiful_linkedin.models import CompanyInput, Lead, ProspectingSummary


def print_run_summary(
    console: Console,
    companies: list[CompanyInput],
    max_results: int,
    official_sites: bool,
    include_uncertain: bool,
    output_path: str,
    search_engines: list[str] | None = None,
    search_depth: str = "standard",
    lead_providers: list[str] | None = None,
    parallelism: int = 6,
    source_mode_label: str | None = None,
) -> None:
    titles = sorted({title for company in companies for title in company.titles})
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan")
    table.add_column(style="white")
    table.add_row("Empresas", str(len(companies)))
    table.add_row("Cargos/termos", ", ".join(titles))
    table.add_row(
        "Modo de busca",
        source_mode_label
        or ("APIs/busca pública + sites oficiais" if official_sites else "APIs/busca pública"),
    )
    table.add_row("Meta de leads", str(max_results))
    table.add_row("Motores de busca", ", ".join(search_engines or ["auto"]))
    table.add_row("Profundidade", "profunda" if search_depth == "deep" else "padrão")
    table.add_row("Fontes", ", ".join(lead_providers or ["auto"]))
    table.add_row("Workers paralelos", str(parallelism))
    table.add_row("Incluir incertos", "sim" if include_uncertain else "não")
    table.add_row("Arquivo de saída", output_path)
    table.add_row(
        "Nota de segurança",
        _security_note(source_mode_label),
    )
    console.print(Panel(table, title="Resumo da execução", border_style="cyan"))


def print_final_summary(console: Console, summary: ProspectingSummary, max_results: int = 30) -> None:
    table = Table(title="Resumo final", header_style="bold cyan")
    table.add_column("Métrica")
    table.add_column("Valor", justify="right")
    table.add_row("Empresas processadas", str(summary.total_companies_processed))
    table.add_row("Leads brutos", str(summary.total_raw_leads))
    table.add_row("Leads deduplicados", str(summary.total_deduplicated_leads))
    table.add_row("Talvez incorretos", str(summary.total_maybe_incorrect_leads))
    
    target = max_results * max(1, summary.total_companies_processed)
    found = summary.total_deduplicated_leads
    status = "nenhum"
    if found >= target:
        status = "meta atingida"
    elif found >= 10:
        status = "parcial"
    elif found > 0:
        status = "baixo"
        
    table.add_row("Meta de cobertura", f"{found}/{target}")
    table.add_row("Status da cobertura", status)
    
    table.add_row("Arquivo gerado", summary.output_file)
    table.add_row("Principais fontes", _format_top_sources(summary.top_sources))
    console.print(table)
    print_source_contribution(console, summary.top_sources)


def print_source_contribution(console: Console, top_sources: dict[str, int]) -> None:
    table = Table(title="Contribuição por fonte", header_style="bold cyan")
    table.add_column("Fonte")
    table.add_column("Leads aceitos", justify="right")
    table.add_column("Status")

    for source, count in _ordered_sources(top_sources):
        status = "gerou leads" if count > 0 else "sem leads aceitos"
        table.add_row(source, str(count), status)

    if not top_sources:
        table.add_row("-", "0", "sem fontes ativas")

    console.print(table)


def print_leads_preview(console: Console, leads: list[Lead], limit: int = 10) -> None:
    table = Table(title=f"Prévia: primeiros {min(limit, len(leads))} leads", header_style="bold cyan")
    table.add_column("Nome", overflow="fold")
    table.add_column("Cargo", overflow="fold")
    table.add_column("Empresa", overflow="fold")
    table.add_column("LinkedIn URL", overflow="fold")
    table.add_column("Fonte", overflow="fold")
    table.add_column("Confiança", justify="right")
    table.add_column("Validação", overflow="fold")

    for lead in leads[:limit]:
        table.add_row(
            lead.person_name or "-",
            lead.title or "-",
            lead.company_name,
            lead.linkedin_url or "-",
            lead.source_type,
            str(lead.confidence_score),
            "talvez incorreto" if lead.validation_status == "maybe_incorrect" else "ok",
        )

    if not leads:
        table.add_row("-", "-", "-", "-", "-", "0", "-")

    console.print(table)


def source_counts(leads: list[Lead]) -> dict[str, int]:
    return dict(Counter(lead.source_type for lead in leads))


def _format_top_sources(top_sources: dict[str, int]) -> str:
    if not top_sources:
        return "-"
    nonzero = {source: count for source, count in top_sources.items() if count > 0}
    if not nonzero:
        return "-"
    return ", ".join(f"{source}: {count}" for source, count in nonzero.items())


def _ordered_sources(top_sources: dict[str, int]) -> list[tuple[str, int]]:
    return sorted(top_sources.items(), key=lambda item: (-item[1], item[0]))


def _security_note(source_mode_label: str | None) -> str:
    label = (source_mode_label or "").lower()
    if "serp" in label:
        return "Sem APIs externas, sem cookies e sem login. Cobertura depende dos buscadores."
    if "cookie" in label:
        return "Usa sessão LinkedIn existente via li_at. Respeite limites e termos aplicáveis."
    return "Usa APIs configuradas, busca pública e sites oficiais."
