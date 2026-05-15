from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table

from beautiful_linkedin.config import load_settings
from beautiful_linkedin.models import CompanyInput
from beautiful_linkedin.processing.function_taxonomy import JobFunction
from beautiful_linkedin.processing.lead_filter import LeadFilter
from beautiful_linkedin.processing.seniority_taxonomy import Seniority
from beautiful_linkedin.runner import run_prospecting
from beautiful_linkedin.scrape_modes import apply_scrape_mode
from beautiful_linkedin.ui.banner import print_banner
from beautiful_linkedin.ui.prompts import (
    INTERACTIVE_UNAVAILABLE_MESSAGE,
    ask_csv_run,
    ask_linkedin_scraper_run,
    ask_main_menu,
    ask_new_search,
    is_interactive_available,
)
from beautiful_linkedin.ui.tables import (
    print_final_summary,
    print_leads_preview,
    print_run_summary,
)

console = Console()

logging.basicConfig(
    level=logging.WARNING,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, show_path=False, markup=True)],
)

app = typer.Typer(
    name="beautiful-linkedin",
    help="CLI bonita para descoberta pública de leads B2B.",
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback()
def main_callback(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    run_interactive()


@app.command("search")
def search_command(
    company_name: str = typer.Option(..., "--company-name", help="Nome da empresa."),
    company_domain: str | None = typer.Option(None, "--company-domain", help="Domínio oficial da empresa."),
    linkedin_url: str | None = typer.Option(None, "--linkedin-url", help="URL pública da empresa no LinkedIn, apenas como contexto."),
    titles: str = typer.Option(..., "--titles", help="Cargos ou termos-alvo separados por vírgula."),
    max_results: int = typer.Option(20, "--max-results", min=1, help="Quantidade de leads que a ferramenta deve tentar puxar."),
    official_sites: str = typer.Option("true", "--official-sites", help="true/false: buscar também em sites oficiais."),
    include_uncertain: str = typer.Option("false", "--include-uncertain", help="true/false: manter leads sem match claro de cargo."),
    search_engines: str = typer.Option("auto", "--search-engines", help="Motores separados por vírgula: auto, searxng, google_html, bing_html, brave_html, duckduckgo_html, duckduckgo, brave, google, serper, crawl4ai."),
    search_depth: str = typer.Option("standard", "--search-depth", help="standard ou deep para expandir consultas."),
    lead_providers: str = typer.Option("auto", "--lead-providers", help="Providers separados por vírgula: auto, pdl, coresignal, apollo, lusha, apify_linkedin, public_directories, common_crawl, linkedin_cookie, web."),
    scrape_mode: str = typer.Option("api", "--scrape-mode", help="api, serp ou cookie. serp ignora APIs/cookies; cookie usa li_at."),
    linkedin_cookie: str | None = typer.Option(None, "--linkedin-cookie", help="Valor do li_at ou header contendo li_at. Use 'auto' ou omita para tentar detectar."),
    linkedin_cookie_browser: str = typer.Option("auto", "--linkedin-cookie-browser", help="auto, chrome, edge, brave, firefox ou none."),
    parallelism: int = typer.Option(6, "--parallelism", min=1, max=32, help="Workers paralelos para providers."),
    web_query_limit: int = typer.Option(48, "--web-query-limit", min=0, help="Máximo de consultas públicas por empresa. No SERP, 0 usa o padrão; nos demais modos, 0 desativa web."),
    provider_timeout: float = typer.Option(120.0, "--provider-timeout", min=1.0, help="Tempo máximo de espera por rodada de providers por empresa."),
    use_cache: str = typer.Option("true", "--use-cache", help="true/false: salvar respostas de API em cache local."),
    output: str = typer.Option(..., "--output", help="Caminho de saída CSV ou XLSX."),
    output_format: str | None = typer.Option(None, "--output-format", help="csv ou xlsx. Usa a extensão do output por padrão."),
    seniority: str | None = typer.Option(None, "--seniority", help="Filtro pós-coleta: c_level,vp,director,head,manager,senior,mid,junior,intern (separados por vírgula)."),
    functions: str | None = typer.Option(None, "--functions", help="Filtro pós-coleta por área: marketing,sales,engineering,product,design,data,finance,hr,operations,legal,customer_success,executive."),
    locations: str | None = typer.Option(None, "--locations", help="Filtro pós-coleta por localização (substring no snippet, separados por vírgula)."),
    exclude_titles: str | None = typer.Option(None, "--exclude-titles", help="Excluir leads cujo título contenha esses termos (separados por vírgula)."),
    min_confidence: int | None = typer.Option(None, "--min-confidence", min=0, max=100, help="Manter apenas leads com confidence_score >= valor."),
    drop_unclassified: str = typer.Option("false", "--drop-unclassified", help="true/false: descartar leads sem classificação detectada quando filtros são aplicados."),
) -> None:
    company = CompanyInput(
        company_name=company_name,
        company_domain=company_domain,
        linkedin_url=linkedin_url,
        titles=parse_titles(titles),
    )
    lead_filter = build_lead_filter(
        seniority=seniority,
        functions=functions,
        locations=locations,
        exclude_titles=exclude_titles,
        min_confidence=min_confidence,
        drop_unclassified=parse_bool(drop_unclassified),
    )
    official = parse_bool(official_sites)
    uncertain = parse_bool(include_uncertain)
    parsed_engines = parse_search_engines(search_engines)
    parsed_depth = parse_search_depth(search_depth)
    parsed_providers = parse_lead_providers(lead_providers)
    source_options = parse_scrape_mode_options(
        scrape_mode=scrape_mode,
        lead_providers=parsed_providers,
        official_sites=official,
        web_query_limit=web_query_limit,
        parallelism=parallelism,
    )
    parsed_engines = search_engines_for_scrape_mode(source_options.scrape_mode, parsed_engines)
    parsed_cache = parse_bool(use_cache)
    print_banner(console)
    print_run_summary(
        console,
        [company],
        max_results,
        source_options.official_sites,
        uncertain,
        output,
        parsed_engines,
        parsed_depth,
        source_options.lead_providers,
        source_options.parallelism,
        source_options.label,
    )
    confirm_risky_mode_or_abort(source_options.scrape_mode, source_options.lead_providers)
    result = run_prospecting(
        companies=[company],
        max_results=max_results,
        official_sites=source_options.official_sites,
        include_uncertain=uncertain,
        output_path=output,
        output_format=output_format,
        console=console,
        search_engines=parsed_engines,
        search_depth=parsed_depth,
        lead_providers=source_options.lead_providers,
        parallelism=source_options.parallelism,
        web_query_limit=source_options.web_query_limit,
        provider_timeout_seconds=provider_timeout,
        use_cache=parsed_cache,
        linkedin_cookie=linkedin_cookie,
        linkedin_cookie_browser=linkedin_cookie_browser,
        lead_filter=lead_filter,
    )
    print_final_summary(console, result.summary, max_results=max_results)
    print_leads_preview(console, result.leads)


@app.command("from-csv")
def from_csv_command(
    input: str = typer.Option(..., "--input", help="Caminho do CSV de entrada."),
    output: str = typer.Option(..., "--output", help="Caminho de saída CSV ou XLSX."),
    max_results: int = typer.Option(20, "--max-results", min=1, help="Quantidade de leads que a ferramenta deve tentar puxar por empresa."),
    official_sites: str = typer.Option("true", "--official-sites", help="true/false: buscar também em sites oficiais."),
    include_uncertain: str = typer.Option("false", "--include-uncertain", help="true/false: manter leads sem match claro de cargo."),
    search_engines: str = typer.Option("auto", "--search-engines", help="Motores separados por vírgula: auto, searxng, google_html, bing_html, brave_html, duckduckgo_html, duckduckgo, brave, google, serper, crawl4ai."),
    search_depth: str = typer.Option("standard", "--search-depth", help="standard ou deep para expandir consultas."),
    lead_providers: str = typer.Option("auto", "--lead-providers", help="Providers separados por vírgula: auto, pdl, coresignal, apollo, lusha, apify_linkedin, public_directories, common_crawl, linkedin_cookie, web."),
    scrape_mode: str = typer.Option("api", "--scrape-mode", help="api, serp ou cookie. serp ignora APIs/cookies; cookie usa li_at."),
    linkedin_cookie: str | None = typer.Option(None, "--linkedin-cookie", help="Valor do li_at ou header contendo li_at. Use 'auto' ou omita para tentar detectar."),
    linkedin_cookie_browser: str = typer.Option("auto", "--linkedin-cookie-browser", help="auto, chrome, edge, brave, firefox ou none."),
    parallelism: int = typer.Option(6, "--parallelism", min=1, max=32, help="Workers paralelos para providers."),
    web_query_limit: int = typer.Option(48, "--web-query-limit", min=0, help="Máximo de consultas públicas por empresa. No SERP, 0 usa o padrão; nos demais modos, 0 desativa web."),
    provider_timeout: float = typer.Option(120.0, "--provider-timeout", min=1.0, help="Tempo máximo de espera por rodada de providers por empresa."),
    use_cache: str = typer.Option("true", "--use-cache", help="true/false: salvar respostas de API em cache local."),
    output_format: str | None = typer.Option(None, "--output-format", help="csv ou xlsx. Usa a extensão do output por padrão."),
    seniority: str | None = typer.Option(None, "--seniority", help="Filtro pós-coleta: c_level,vp,director,head,manager,senior,mid,junior,intern."),
    functions: str | None = typer.Option(None, "--functions", help="Filtro pós-coleta por área: marketing,sales,engineering,product,design,data,finance,hr,operations,legal,customer_success,executive."),
    locations: str | None = typer.Option(None, "--locations", help="Filtro pós-coleta por localização (substring no snippet)."),
    exclude_titles: str | None = typer.Option(None, "--exclude-titles", help="Excluir leads cujo título contenha esses termos."),
    min_confidence: int | None = typer.Option(None, "--min-confidence", min=0, max=100, help="Manter apenas leads com confidence_score >= valor."),
    drop_unclassified: str = typer.Option("false", "--drop-unclassified", help="true/false: descartar leads sem classificação detectada quando filtros são aplicados."),
) -> None:
    companies = load_companies_from_csv(input)
    lead_filter = build_lead_filter(
        seniority=seniority,
        functions=functions,
        locations=locations,
        exclude_titles=exclude_titles,
        min_confidence=min_confidence,
        drop_unclassified=parse_bool(drop_unclassified),
    )
    official = parse_bool(official_sites)
    uncertain = parse_bool(include_uncertain)
    parsed_engines = parse_search_engines(search_engines)
    parsed_depth = parse_search_depth(search_depth)
    parsed_providers = parse_lead_providers(lead_providers)
    source_options = parse_scrape_mode_options(
        scrape_mode=scrape_mode,
        lead_providers=parsed_providers,
        official_sites=official,
        web_query_limit=web_query_limit,
        parallelism=parallelism,
    )
    parsed_engines = search_engines_for_scrape_mode(source_options.scrape_mode, parsed_engines)
    parsed_cache = parse_bool(use_cache)
    print_banner(console)
    print_run_summary(
        console,
        companies,
        max_results,
        source_options.official_sites,
        uncertain,
        output,
        parsed_engines,
        parsed_depth,
        source_options.lead_providers,
        source_options.parallelism,
        source_options.label,
    )
    confirm_risky_mode_or_abort(source_options.scrape_mode, source_options.lead_providers)
    result = run_prospecting(
        companies=companies,
        max_results=max_results,
        official_sites=source_options.official_sites,
        include_uncertain=uncertain,
        output_path=output,
        output_format=output_format,
        console=console,
        search_engines=parsed_engines,
        search_depth=parsed_depth,
        lead_providers=source_options.lead_providers,
        parallelism=source_options.parallelism,
        web_query_limit=source_options.web_query_limit,
        provider_timeout_seconds=provider_timeout,
        use_cache=parsed_cache,
        linkedin_cookie=linkedin_cookie,
        linkedin_cookie_browser=linkedin_cookie_browser,
        lead_filter=lead_filter,
    )
    print_final_summary(console, result.summary, max_results=max_results)
    print_leads_preview(console, result.leads)


@app.command("example-csv")
def example_csv_command() -> None:
    table = Table(title="Formato do CSV de entrada", header_style="bold cyan")
    table.add_column("company_name")
    table.add_column("company_domain")
    table.add_column("linkedin_url")
    table.add_column("titles")
    table.add_row(
        "Nubank",
        "nubank.com.br",
        "https://www.linkedin.com/company/nubank",
        "marketing,growth,cmo,head of marketing",
    )
    table.add_row(
        "XP Inc",
        "xpinc.com",
        "https://www.linkedin.com/company/xp-inc",
        "sales,marketing,partnerships",
    )
    console.print(table)


@app.command("about")
def about_command() -> None:
    console.print(about_panel())


@app.command("linkedin-scraper")
def linkedin_scraper_command(
    company_name: str | None = typer.Option(None, "--company-name", help="Nome da empresa alvo."),
    linkedin_url: str | None = typer.Option(None, "--linkedin-url", help="URL da empresa no LinkedIn."),
    titles: str | None = typer.Option(None, "--titles", help="Cargos ou termos separados por vírgula."),
    max_results: int = typer.Option(100, "--max-results", min=1, help="Quantidade de perfis que serão solicitados ao Actor."),
    include_uncertain: str = typer.Option("true", "--include-uncertain", help="true/false: manter leads sem match claro de cargo."),
    output: str = typer.Option("output/linkedin_scraper_leads.csv", "--output", help="Caminho de saída CSV ou XLSX."),
    output_format: str | None = typer.Option(None, "--output-format", help="csv ou xlsx. Usa a extensão do output por padrão."),
    provider_timeout: float = typer.Option(240.0, "--provider-timeout", min=1.0, help="Tempo máximo de espera pelo Actor da Apify."),
    use_cache: str = typer.Option("true", "--use-cache", help="true/false: salvar respostas da Apify em cache local."),
) -> None:
    print_banner(console)
    if not company_name or not linkedin_url or not titles:
        console.print(linkedin_scraper_panel())
        console.print(
            "[yellow]Para executar, informe --company-name, "
            "--linkedin-url e --titles.[/yellow]"
        )
        return

    run_linkedin_scraper_with_apify(
        company_name=company_name,
        linkedin_url=linkedin_url,
        titles=parse_titles(titles),
        max_results=max_results,
        include_uncertain=parse_bool(include_uncertain),
        output_path=output,
        output_format=output_format,
        provider_timeout=provider_timeout,
        use_cache=parse_bool(use_cache),
    )


@app.command("linkedin-sales-navigator")
def linkedin_sales_navigator_command(
    company_name: str = typer.Option(..., "--company-name", help="Nome da empresa alvo."),
    linkedin_url: str = typer.Option(..., "--linkedin-url", help="URL da empresa no LinkedIn."),
    titles: str = typer.Option(..., "--titles", help="Cargos ou termos separados por vírgula."),
    max_results: int = typer.Option(100, "--max-results", min=1, help="Quantidade de perfis que serão buscados."),
    include_uncertain: str = typer.Option("true", "--include-uncertain", help="true/false: manter leads sem match claro de cargo."),
    linkedin_cookie: str | None = typer.Option(None, "--linkedin-cookie", help="Valor do li_at ou header contendo li_at. Omitir para detectar automaticamente."),
    linkedin_cookie_browser: str = typer.Option("auto", "--linkedin-cookie-browser", help="auto, chrome, edge, brave, firefox ou none."),
    output: str = typer.Option("output/linkedin_sales_nav_leads.csv", "--output", help="Caminho de saída CSV ou XLSX."),
    output_format: str | None = typer.Option(None, "--output-format", help="csv ou xlsx. Usa a extensão do output por padrão."),
    provider_timeout: float = typer.Option(180.0, "--provider-timeout", min=1.0, help="Tempo máximo de espera pelo Sales Navigator."),
) -> None:
    print_banner(console)
    run_linkedin_sales_navigator_scraper(
        company_name=company_name,
        linkedin_url=linkedin_url,
        titles=parse_titles(titles),
        max_results=max_results,
        include_uncertain=parse_bool(include_uncertain),
        linkedin_cookie=linkedin_cookie,
        linkedin_cookie_browser=linkedin_cookie_browser,
        output_path=output,
        output_format=output_format,
        provider_timeout=provider_timeout,
    )


@app.command("linkedin-browser")
def linkedin_browser_command(
    company_name: str = typer.Option(..., "--company-name", help="Nome da empresa alvo."),
    linkedin_url: str = typer.Option(..., "--linkedin-url", help="URL da empresa no LinkedIn."),
    titles: str = typer.Option(..., "--titles", help="Cargos ou termos separados por vírgula."),
    max_results: int = typer.Option(50, "--max-results", min=1, help="Quantidade de perfis que serão buscados."),
    include_uncertain: str = typer.Option("true", "--include-uncertain", help="true/false: manter leads sem match claro de cargo."),
    linkedin_cookie: str | None = typer.Option(None, "--linkedin-cookie", help="Valor do li_at ou header contendo li_at. Omitir para detectar automaticamente."),
    linkedin_cookie_browser: str = typer.Option("auto", "--linkedin-cookie-browser", help="auto, chrome, edge, brave, firefox ou none."),
    headless: str = typer.Option("true", "--headless", help="true/false: rodar Chromium sem interface."),
    output: str = typer.Option("output/linkedin_browser_leads.csv", "--output", help="Caminho de saída CSV ou XLSX."),
    output_format: str | None = typer.Option(None, "--output-format", help="csv ou xlsx. Usa a extensão do output por padrão."),
    provider_timeout: float = typer.Option(300.0, "--provider-timeout", min=1.0, help="Tempo máximo de espera pelo navegador."),
    seniority: str | None = typer.Option(None, "--seniority", help="Filtro pós-coleta de senioridade."),
    functions: str | None = typer.Option(None, "--functions", help="Filtro pós-coleta de função."),
    locations: str | None = typer.Option(None, "--locations", help="Filtro pós-coleta de localização."),
    exclude_titles: str | None = typer.Option(None, "--exclude-titles", help="Excluir títulos contendo esses termos."),
    min_confidence: int | None = typer.Option(None, "--min-confidence", min=0, max=100, help="Filtro por confidence_score mínimo."),
    drop_unclassified: str = typer.Option("false", "--drop-unclassified", help="true/false: descartar leads sem classificação."),
    yes: bool = typer.Option(False, "--yes", help="Pular o prompt de confirmação do risco (uso por sua conta e risco)."),
) -> None:
    print_banner(console)
    if not yes:
        confirm_risky_mode_or_abort("browser", ["linkedin_playwright"])
    run_linkedin_browser_scraper(
        company_name=company_name,
        linkedin_url=linkedin_url,
        titles=parse_titles(titles),
        max_results=max_results,
        include_uncertain=parse_bool(include_uncertain),
        linkedin_cookie=linkedin_cookie,
        linkedin_cookie_browser=linkedin_cookie_browser,
        headless=parse_bool(headless),
        output_path=output,
        output_format=output_format,
        provider_timeout=provider_timeout,
        lead_filter=build_lead_filter(
            seniority=seniority,
            functions=functions,
            locations=locations,
            exclude_titles=exclude_titles,
            min_confidence=min_confidence,
            drop_unclassified=parse_bool(drop_unclassified),
        ),
    )


@app.command("linkedin-cookie")
def linkedin_cookie_command(
    company_name: str = typer.Option(..., "--company-name", help="Nome da empresa alvo."),
    linkedin_url: str = typer.Option(..., "--linkedin-url", help="URL da empresa no LinkedIn."),
    titles: str = typer.Option(..., "--titles", help="Cargos ou termos separados por vírgula."),
    max_results: int = typer.Option(100, "--max-results", min=1, help="Quantidade de perfis que serão buscados."),
    include_uncertain: str = typer.Option("true", "--include-uncertain", help="true/false: manter leads sem match claro de cargo."),
    linkedin_cookie: str | None = typer.Option(None, "--linkedin-cookie", help="Valor do li_at ou header contendo li_at. Omitir para detectar automaticamente."),
    linkedin_cookie_browser: str = typer.Option("auto", "--linkedin-cookie-browser", help="auto, chrome, edge, brave, firefox ou none."),
    output: str = typer.Option("output/linkedin_cookie_leads.csv", "--output", help="Caminho de saída CSV ou XLSX."),
    output_format: str | None = typer.Option(None, "--output-format", help="csv ou xlsx. Usa a extensão do output por padrão."),
    provider_timeout: float = typer.Option(120.0, "--provider-timeout", min=1.0, help="Tempo máximo de espera pelo LinkedIn."),
) -> None:
    print_banner(console)
    run_linkedin_cookie_scraper(
        company_name=company_name,
        linkedin_url=linkedin_url,
        titles=parse_titles(titles),
        max_results=max_results,
        include_uncertain=parse_bool(include_uncertain),
        linkedin_cookie=linkedin_cookie,
        linkedin_cookie_browser=linkedin_cookie_browser,
        output_path=output,
        output_format=output_format,
        provider_timeout=provider_timeout,
    )


def run_interactive() -> None:
    if not is_interactive_available():
        console.print(f"[yellow]{INTERACTIVE_UNAVAILABLE_MESSAGE}[/yellow]")
        raise typer.Exit(code=2)

    settings = load_settings()
    try:
        print_banner(console)
        while True:
            choice = ask_main_menu()
            if choice in (None, "Sair"):
                raise typer.Exit(code=0)
            if choice == "Nova busca guiada":
                answers = ask_new_search(
                    default_max_results=settings.default_max_results,
                    settings=settings,
                )
                company = CompanyInput(
                    company_name=answers.company_name,
                    company_domain=answers.company_domain,
                    linkedin_url=answers.linkedin_url,
                    titles=parse_titles(answers.titles),
                )
                output = normalize_output_path(answers.output_path, answers.output_format)
                source_options = parse_scrape_mode_options(
                    scrape_mode=answers.scrape_mode,
                    lead_providers=parse_lead_providers(answers.lead_providers),
                    official_sites=answers.official_sites,
                    web_query_limit=settings.web_query_limit,
                    parallelism=answers.parallelism,
                )
                print_run_summary(
                    console,
                    [company],
                    answers.max_results,
                    source_options.official_sites,
                    answers.include_uncertain,
                    str(output),
                    parse_search_engines(answers.search_engines),
                    parse_search_depth(answers.search_depth),
                    source_options.lead_providers,
                    source_options.parallelism,
                    source_options.label,
                )
                
                import questionary
                if not questionary.confirm("Deseja iniciar a busca agora?", default=True).ask():
                    continue

                result = run_prospecting(
                    companies=[company],
                    max_results=answers.max_results,
                    official_sites=source_options.official_sites,
                    include_uncertain=answers.include_uncertain,
                    output_path=output,
                    output_format=answers.output_format,
                    console=console,
                    search_engines=parse_search_engines(answers.search_engines),
                    search_depth=parse_search_depth(answers.search_depth),
                    lead_providers=source_options.lead_providers,
                    parallelism=source_options.parallelism,
                    web_query_limit=source_options.web_query_limit,
                    linkedin_cookie=answers.linkedin_cookie,
                    linkedin_cookie_browser=answers.linkedin_cookie_browser,
                )
                print_final_summary(console, result.summary, max_results=answers.max_results)
                print_leads_preview(console, result.leads)
            elif choice == "Rodar a partir de CSV":
                answers = ask_csv_run(
                    default_max_results=settings.default_max_results,
                    settings=settings,
                )
                companies = load_companies_from_csv(answers.input_path)
                output = normalize_output_path(answers.output_path, answers.output_format)
                source_options = parse_scrape_mode_options(
                    scrape_mode=answers.scrape_mode,
                    lead_providers=parse_lead_providers(answers.lead_providers),
                    official_sites=answers.official_sites,
                    web_query_limit=settings.web_query_limit,
                    parallelism=answers.parallelism,
                )
                print_run_summary(
                    console,
                    companies,
                    answers.max_results,
                    source_options.official_sites,
                    answers.include_uncertain,
                    str(output),
                    parse_search_engines(answers.search_engines),
                    parse_search_depth(answers.search_depth),
                    source_options.lead_providers,
                    source_options.parallelism,
                    source_options.label,
                )
                result = run_prospecting(
                    companies=companies,
                    max_results=answers.max_results,
                    official_sites=source_options.official_sites,
                    include_uncertain=answers.include_uncertain,
                    output_path=output,
                    output_format=answers.output_format,
                    console=console,
                    search_engines=parse_search_engines(answers.search_engines),
                    search_depth=parse_search_depth(answers.search_depth),
                    lead_providers=source_options.lead_providers,
                    parallelism=source_options.parallelism,
                    web_query_limit=source_options.web_query_limit,
                    linkedin_cookie=answers.linkedin_cookie,
                    linkedin_cookie_browser=answers.linkedin_cookie_browser,
                )
                print_final_summary(console, result.summary, max_results=answers.max_results)
                print_leads_preview(console, result.leads)
            elif choice == "Ver exemplo de CSV":
                example_csv_command()
            elif choice == "Scraper LinkedIn com cookie":
                answers = ask_linkedin_scraper_run(
                    default_max_results=settings.default_max_results
                )
                runner_fn = (
                    run_linkedin_sales_navigator_scraper
                    if answers.use_sales_navigator
                    else run_linkedin_cookie_scraper
                )
                runner_fn(
                    company_name=answers.company_name,
                    linkedin_url=answers.linkedin_url,
                    titles=parse_titles(answers.titles),
                    max_results=answers.max_results,
                    include_uncertain=answers.include_uncertain,
                    linkedin_cookie=answers.linkedin_cookie,
                    linkedin_cookie_browser=answers.linkedin_cookie_browser,
                    output_path=normalize_output_path(
                        answers.output_path,
                        answers.output_format,
                    ),
                    output_format=answers.output_format,
                    provider_timeout=settings.provider_timeout_seconds,
                )
            elif choice == "Sobre / notas de segurança":
                about_command()
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Interrompido.[/yellow]")
        raise typer.Exit(code=130) from None
    except Exception as exc:
        console.print(f"[yellow]{INTERACTIVE_UNAVAILABLE_MESSAGE}[/yellow]")
        console.print(f"[dim]{exc}[/dim]")
        raise typer.Exit(code=2) from None


def load_companies_from_csv(input_path: str | Path) -> list[CompanyInput]:
    path = Path(input_path)
    if not path.exists():
        raise typer.BadParameter(f"CSV de entrada não encontrado: {path}")

    dataframe = pd.read_csv(path).fillna("")
    required = {"company_name", "company_domain", "linkedin_url", "titles"}
    missing = required.difference(dataframe.columns)
    if missing:
        raise typer.BadParameter(f"CSV de entrada sem colunas obrigatórias: {', '.join(sorted(missing))}")

    companies = [
        CompanyInput(
            company_name=str(row["company_name"]),
            company_domain=str(row["company_domain"]) or None,
            linkedin_url=str(row["linkedin_url"]) or None,
            titles=parse_titles(str(row["titles"])),
        )
        for _, row in dataframe.iterrows()
    ]
    if not companies:
        raise typer.BadParameter("O CSV de entrada não tem empresas.")
    return companies


def parse_titles(value: str) -> list[str]:
    titles = [" ".join(title.strip().split()) for title in value.split(",") if title.strip()]
    if not titles:
        raise typer.BadParameter("Informe pelo menos um cargo ou termo.")
    return titles


def parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "y", "sim", "s"}:
        return True
    if normalized in {"false", "0", "no", "n", "nao", "não"}:
        return False
    raise typer.BadParameter("Valor esperado: true ou false.")


RISKY_PROVIDERS = {"linkedin_playwright", "playwright", "browser", "navegador", "linkedin_browser"}


def is_risky_run(scrape_mode: str | None, lead_providers: list[str] | None) -> bool:
    if (scrape_mode or "").strip().lower() == "browser":
        return True
    if not lead_providers:
        return False
    normalized = {name.strip().lower() for name in lead_providers if name.strip()}
    return bool(normalized.intersection(RISKY_PROVIDERS))


def confirm_risky_mode_or_abort(scrape_mode: str | None, lead_providers: list[str] | None) -> None:
    """Show a hard-to-miss warning and require explicit confirmation for risky runs.

    Tied to the browser/Playwright mode, where LinkedIn can restrict or ban the
    account being used. Skipped when stdin/stdout are not a TTY (CI, pipelines)
    so non-interactive callers can opt in via flags only — they already passed
    the mode explicitly.
    """
    if not is_risky_run(scrape_mode, lead_providers):
        return
    console.print(
        Panel(
            "[bold red]Modo ARRISCADO selecionado (Playwright logado).[/bold red]\n"
            "Este modo navega no LinkedIn com a sua conta. Você corre risco real "
            "de receber restrição temporária ou banimento permanente da conta.\n\n"
            "Recomendações: use uma conta secundária; não rode em paralelo; respeite "
            "o ritmo conservador (sleeps entre ações); pare se aparecer captcha.",
            title="ATENÇÃO - risco de bloqueio da conta",
            border_style="red",
        )
    )
    if not is_interactive_available():
        return
    import questionary

    answer = questionary.confirm(
        "Você confirma que entende o risco e quer continuar com o modo navegador?",
        default=False,
    ).ask()
    if not answer:
        console.print("[yellow]Execução cancelada.[/yellow]")
        raise typer.Exit(code=0)


def parse_seniority(value: str | None) -> list[Seniority]:
    if not value:
        return []
    items = [item.strip().lower() for item in value.split(",") if item.strip()]
    aliases = {
        "c_level": Seniority.C_LEVEL,
        "c-level": Seniority.C_LEVEL,
        "clevel": Seniority.C_LEVEL,
        "executive": Seniority.C_LEVEL,
        "vp": Seniority.VP,
        "vice_president": Seniority.VP,
        "director": Seniority.DIRECTOR,
        "diretor": Seniority.DIRECTOR,
        "head": Seniority.HEAD,
        "manager": Seniority.MANAGER,
        "gerente": Seniority.MANAGER,
        "senior": Seniority.SENIOR,
        "sr": Seniority.SENIOR,
        "mid": Seniority.MID,
        "pleno": Seniority.MID,
        "junior": Seniority.JUNIOR,
        "jr": Seniority.JUNIOR,
        "intern": Seniority.INTERN,
        "estagiario": Seniority.INTERN,
        "estagiário": Seniority.INTERN,
    }
    parsed: list[Seniority] = []
    for item in items:
        try:
            parsed.append(aliases[item])
        except KeyError as exc:
            raise typer.BadParameter(
                f"Senioridade desconhecida: {item}. Use c_level, vp, director, head, manager, senior, mid, junior, intern."
            ) from exc
    return parsed


def parse_functions(value: str | None) -> list[JobFunction]:
    if not value:
        return []
    items = [item.strip().lower() for item in value.split(",") if item.strip()]
    aliases = {
        "marketing": JobFunction.MARKETING,
        "growth": JobFunction.MARKETING,
        "sales": JobFunction.SALES,
        "vendas": JobFunction.SALES,
        "comercial": JobFunction.SALES,
        "engineering": JobFunction.ENGINEERING,
        "engenharia": JobFunction.ENGINEERING,
        "tech": JobFunction.ENGINEERING,
        "product": JobFunction.PRODUCT,
        "produto": JobFunction.PRODUCT,
        "design": JobFunction.DESIGN,
        "data": JobFunction.DATA,
        "analytics": JobFunction.DATA,
        "finance": JobFunction.FINANCE,
        "financeiro": JobFunction.FINANCE,
        "hr": JobFunction.HR,
        "rh": JobFunction.HR,
        "people": JobFunction.HR,
        "operations": JobFunction.OPERATIONS,
        "ops": JobFunction.OPERATIONS,
        "operacoes": JobFunction.OPERATIONS,
        "operações": JobFunction.OPERATIONS,
        "legal": JobFunction.LEGAL,
        "juridico": JobFunction.LEGAL,
        "jurídico": JobFunction.LEGAL,
        "customer_success": JobFunction.CUSTOMER_SUCCESS,
        "cs": JobFunction.CUSTOMER_SUCCESS,
        "executive": JobFunction.EXECUTIVE,
        "executivo": JobFunction.EXECUTIVE,
    }
    parsed: list[JobFunction] = []
    for item in items:
        try:
            parsed.append(aliases[item])
        except KeyError as exc:
            raise typer.BadParameter(
                f"Função desconhecida: {item}. Use marketing, sales, engineering, product, design, data, finance, hr, operations, legal, customer_success ou executive."
            ) from exc
    return parsed


def build_lead_filter(
    seniority: str | None,
    functions: str | None,
    locations: str | None,
    exclude_titles: str | None,
    min_confidence: int | None,
    drop_unclassified: bool,
) -> LeadFilter:
    return LeadFilter(
        seniority_in=parse_seniority(seniority),
        functions_in=parse_functions(functions),
        locations_in=[loc.strip() for loc in (locations or "").split(",") if loc.strip()],
        exclude_titles=[term.strip() for term in (exclude_titles or "").split(",") if term.strip()],
        min_confidence_score=min_confidence,
        drop_unclassified=drop_unclassified,
    )


def parse_search_engines(value: str) -> list[str]:
    engines = [engine.strip().lower() for engine in value.split(",") if engine.strip()]
    if not engines:
        raise typer.BadParameter("Informe pelo menos um motor de busca.")
    allowed = {
        "auto",
        "google_html",
        "google-html",
        "bing_html",
        "bing-html",
        "bing",
        "brave_html",
        "brave-html",
        "duckduckgo_html",
        "ddg_html",
        "duckduckgo-html",
        "duckduckgo",
        "brave",
        "google",
        "google_cse",
        "serper",
        "searxng",
        "searx",
        "crawl4ai",
        "crawl_4_ai",
        "crawl-4-ai",
    }
    invalid = sorted(set(engines).difference(allowed))
    if invalid:
        raise typer.BadParameter(f"Motor(es) de busca desconhecido(s): {', '.join(invalid)}")
    return engines


def parse_search_depth(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"standard", "deep"}:
        raise typer.BadParameter("A profundidade da busca deve ser standard ou deep.")
    return normalized


def parse_lead_providers(value: str) -> list[str]:
    providers = [provider.strip().lower() for provider in value.split(",") if provider.strip()]
    if not providers:
        raise typer.BadParameter("Informe pelo menos um provider de leads.")
    allowed = {
        "auto",
        "pdl",
        "people_data_labs",
        "peopledatalabs",
        "coresignal",
        "apollo",
        "lusha",
        "apify",
        "apify_linkedin",
        "linkedin_apify",
        "public_directories",
        "public_dirs",
        "theorg",
        "rocketreach_public",
        "common_crawl",
        "commoncrawl",
        "cc",
        "linkedin_cookie",
        "cookie",
        "li_at",
        "linkedin_people_search",
        "people_search",
        "people",
        "linkedin_listing",
        "linkedin_sales_navigator",
        "sales_navigator",
        "sales_nav",
        "salesnav",
        "linkedin_playwright",
        "playwright",
        "browser",
        "navegador",
        "linkedin_browser",
        "web",
        "search",
        "public_search",
    }
    invalid = sorted(set(providers).difference(allowed))
    if invalid:
        raise typer.BadParameter(f"Provider(s) de leads desconhecido(s): {', '.join(invalid)}")
    return providers


def parse_scrape_mode_options(
    scrape_mode: str,
    lead_providers: list[str],
    official_sites: bool,
    web_query_limit: int,
    parallelism: int,
):
    try:
        return apply_scrape_mode(
            scrape_mode=scrape_mode,
            lead_providers=lead_providers,
            official_sites=official_sites,
            web_query_limit=web_query_limit,
            parallelism=parallelism,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def search_engines_for_scrape_mode(scrape_mode: str, search_engines: list[str]) -> list[str]:
    if scrape_mode == "serp" and search_engines == ["auto"]:
        return ["auto"]  # Let factory.py _build_auto_engine_list decide
    return search_engines


def normalize_output_path(output_path: str, output_format: str) -> Path:
    path = Path(output_path)
    suffix = ".xlsx" if output_format.lower() == "xlsx" else ".csv"
    if path.suffix.lower() != suffix:
        path = path.with_suffix(suffix)
    return path


def about_panel() -> Panel:
    text = (
        "O Beautiful LinkedIn trabalha com APIs configuradas, resultados de "
        "busca pública e sites oficiais das empresas.\n\n"
        "A integração Apify LinkedIn Actor é opcional, não entra no modo auto "
        "e delega a coleta a um serviço terceiro. Use apenas quando houver "
        "base legal, permissão operacional e aceite dos termos aplicáveis.\n\n"
        "O app não implementa login no LinkedIn, automação de navegador local, "
        "proxies, captcha solver, mascaramento de IP ou técnicas de evasão.\n\n"
        "Os resultados podem ser incompletos ou desatualizados. O usuário é "
        "responsável por usar os dados conforme LGPD, opt-out, regras das "
        "plataformas e demais normas aplicáveis. A ferramenta ajuda a descobrir "
        "possíveis leads, mas não garante cobertura completa nem dados atuais."
    )
    return Panel(text, title="Sobre / notas de segurança", border_style="cyan")


def linkedin_scraper_panel() -> Panel:
    text = (
        "Esta seção executa exclusivamente o provider Apify LinkedIn Actor "
        "(`apify_linkedin`).\n\n"
        "Ela não usa PDL, Coresignal, Apollo, Lusha, busca pública ou scraping "
        "local. Para funcionar, configure APIFY_API_KEY no .env.\n\n"
        "O Beautiful LinkedIn apenas chama a API da Apify e normaliza o dataset "
        "retornado. O app não implementa login no LinkedIn, automação de "
        "navegador local, proxy, captcha solver, mascaramento de IP ou evasão."
    )
    return Panel(text, title="Scraper via LinkedIn", border_style="yellow")


def run_linkedin_scraper_with_apify(
    company_name: str,
    linkedin_url: str,
    titles: list[str],
    max_results: int,
    include_uncertain: bool,
    output_path: str | Path,
    output_format: str | None = None,
    provider_timeout: float = 240.0,
    use_cache: bool = True,
):
    company = CompanyInput(
        company_name=company_name,
        company_domain=None,
        linkedin_url=linkedin_url,
        titles=titles,
    )
    print_run_summary(
        console,
        [company],
        max_results,
        False,
        include_uncertain,
        str(output_path),
        ["none"],
        "standard",
        ["apify_linkedin"],
        1,
    )
    result = run_prospecting(
        companies=[company],
        max_results=max_results,
        official_sites=False,
        include_uncertain=include_uncertain,
        output_path=output_path,
        output_format=output_format,
        console=console,
        search_engines=["auto"],
        search_depth="standard",
        lead_providers=["apify_linkedin"],
        parallelism=1,
        web_query_limit=0,
        provider_timeout_seconds=provider_timeout,
        use_cache=use_cache,
    )
    print_final_summary(console, result.summary, max_results=max_results)
    print_leads_preview(console, result.leads)
    return result


def run_linkedin_sales_navigator_scraper(
    company_name: str,
    linkedin_url: str,
    titles: list[str],
    max_results: int,
    include_uncertain: bool,
    linkedin_cookie: str | None,
    linkedin_cookie_browser: str,
    output_path: str | Path,
    output_format: str | None = None,
    provider_timeout: float = 180.0,
):
    company = CompanyInput(
        company_name=company_name,
        company_domain=None,
        linkedin_url=linkedin_url,
        titles=titles,
    )
    print_run_summary(
        console,
        [company],
        max_results,
        False,
        include_uncertain,
        str(output_path),
        ["none"],
        "standard",
        ["linkedin_sales_navigator"],
        1,
        "LinkedIn com Sales Navigator - usa li_at + endpoints sales-api",
    )
    result = run_prospecting(
        companies=[company],
        max_results=max_results,
        official_sites=False,
        include_uncertain=include_uncertain,
        output_path=output_path,
        output_format=output_format,
        console=console,
        search_engines=["auto"],
        search_depth="standard",
        lead_providers=["linkedin_sales_navigator"],
        parallelism=1,
        web_query_limit=0,
        provider_timeout_seconds=provider_timeout,
        use_cache=False,
        linkedin_cookie=linkedin_cookie,
        linkedin_cookie_browser=linkedin_cookie_browser,
    )
    print_final_summary(console, result.summary, max_results=max_results)
    print_leads_preview(console, result.leads)
    return result


def run_linkedin_browser_scraper(
    company_name: str,
    linkedin_url: str,
    titles: list[str],
    max_results: int,
    include_uncertain: bool,
    linkedin_cookie: str | None,
    linkedin_cookie_browser: str,
    headless: bool,
    output_path: str | Path,
    output_format: str | None = None,
    provider_timeout: float = 300.0,
    lead_filter: LeadFilter | None = None,
):
    company = CompanyInput(
        company_name=company_name,
        company_domain=None,
        linkedin_url=linkedin_url,
        titles=titles,
    )
    print_run_summary(
        console,
        [company],
        max_results,
        False,
        include_uncertain,
        str(output_path),
        ["none"],
        "standard",
        ["linkedin_playwright"],
        1,
        "ARRISCADO - navegador logado via Playwright. Pode bloquear sua conta.",
    )
    result = run_prospecting(
        companies=[company],
        max_results=max_results,
        official_sites=False,
        include_uncertain=include_uncertain,
        output_path=output_path,
        output_format=output_format,
        console=console,
        search_engines=["auto"],
        search_depth="standard",
        lead_providers=["linkedin_playwright"],
        parallelism=1,
        web_query_limit=0,
        provider_timeout_seconds=provider_timeout,
        use_cache=False,
        linkedin_cookie=linkedin_cookie,
        linkedin_cookie_browser=linkedin_cookie_browser,
        playwright_headless=headless,
        lead_filter=lead_filter,
    )
    print_final_summary(console, result.summary, max_results=max_results)
    print_leads_preview(console, result.leads)
    return result


def run_linkedin_cookie_scraper(
    company_name: str,
    linkedin_url: str,
    titles: list[str],
    max_results: int,
    include_uncertain: bool,
    linkedin_cookie: str | None,
    linkedin_cookie_browser: str,
    output_path: str | Path,
    output_format: str | None = None,
    provider_timeout: float = 120.0,
):
    company = CompanyInput(
        company_name=company_name,
        company_domain=None,
        linkedin_url=linkedin_url,
        titles=titles,
    )
    print_run_summary(
        console,
        [company],
        max_results,
        False,
        include_uncertain,
        str(output_path),
        ["none"],
        "standard",
        ["linkedin_cookie"],
        1,
        "LinkedIn com cookie - usa li_at atual quando disponível",
    )
    result = run_prospecting(
        companies=[company],
        max_results=max_results,
        official_sites=False,
        include_uncertain=include_uncertain,
        output_path=output_path,
        output_format=output_format,
        console=console,
        search_engines=["auto"],
        search_depth="standard",
        lead_providers=["linkedin_cookie"],
        parallelism=1,
        web_query_limit=0,
        provider_timeout_seconds=provider_timeout,
        use_cache=False,
        linkedin_cookie=linkedin_cookie,
        linkedin_cookie_browser=linkedin_cookie_browser,
    )
    print_final_summary(console, result.summary, max_results=max_results)
    print_leads_preview(console, result.leads)
    return result
