from __future__ import annotations

import os
import sys
from dataclasses import dataclass

import questionary
from questionary import Choice

INTERACTIVE_UNAVAILABLE_MESSAGE = (
    "Modo interativo indisponível. Use: beautiful-linkedin search --help"
)


@dataclass(frozen=True)
class NewSearchAnswers:
    scrape_mode: str
    company_name: str
    company_domain: str | None
    linkedin_url: str | None
    titles: str
    max_results: int
    official_sites: bool
    include_uncertain: bool
    search_engines: str
    search_depth: str
    lead_providers: str
    linkedin_cookie: str | None
    linkedin_cookie_browser: str
    parallelism: int
    output_format: str
    output_path: str


@dataclass(frozen=True)
class CsvRunAnswers:
    scrape_mode: str
    input_path: str
    max_results: int
    official_sites: bool
    include_uncertain: bool
    search_engines: str
    search_depth: str
    lead_providers: str
    linkedin_cookie: str | None
    linkedin_cookie_browser: str
    parallelism: int
    output_format: str
    output_path: str


@dataclass(frozen=True)
class LinkedinScraperAnswers:
    company_name: str
    linkedin_url: str
    titles: str
    max_results: int
    include_uncertain: bool
    linkedin_cookie: str | None
    linkedin_cookie_browser: str
    output_format: str
    output_path: str
    use_sales_navigator: bool = False


def is_interactive_available() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty() and os.getenv("CI") is None


def ask_main_menu() -> str | None:
    return questionary.select(
        "O que você quer fazer?",
        choices=[
            "Nova busca guiada",
            "Rodar a partir de CSV",
            "Scraper LinkedIn com cookie",
            "Ver exemplo de CSV",
            "Sobre / notas de segurança",
            "Sair",
        ],
    ).ask()


from beautiful_linkedin.processing.role_taxonomy import get_available_areas, get_terms_for_area, is_custom_area
from beautiful_linkedin.config import Settings

def ask_new_search(
    default_max_results: int = 20,
    settings: Settings | None = None,
) -> NewSearchAnswers:
    settings = settings or Settings()
    company_name = _required_text("Qual empresa você quer prospectar?")
    company_domain = _optional_text("Qual o domínio da empresa? (Opcional)")
    scrape_mode = _ask_scrape_mode()
    linkedin_url = _ask_linkedin_url(scrape_mode)

    area_choice = str(
        questionary.select(
            "Qual área você deseja buscar?",
            choices=get_available_areas(),
        ).ask()
    )

    if is_custom_area(area_choice):
        titles_str = _required_text("Digite os cargos ou termos separados por vírgula")
    else:
        titles_str = ",".join(get_terms_for_area(area_choice))

    lead_providers = _lead_providers_for_mode(scrape_mode, settings)
    search_engines = _search_engines_for_mode(scrape_mode, lead_providers, settings)
    linkedin_cookie = _ask_linkedin_cookie(scrape_mode)
    linkedin_cookie_browser = _ask_cookie_browser(scrape_mode)
    official_sites = scrape_mode == "api" and bool(
        questionary.confirm("Buscar também no site oficial da empresa?", default=True).ask()
    )
    parallelism = 1 if scrape_mode == "cookie" else _positive_int("Quantos workers paralelos usar?", 8)

    return NewSearchAnswers(
        scrape_mode=scrape_mode,
        company_name=company_name,
        company_domain=company_domain,
        linkedin_url=linkedin_url,
        titles=titles_str,
        max_results=_positive_int("Quantos leads você quer tentar puxar?", default_max_results),
        official_sites=official_sites,
        include_uncertain=bool(
            questionary.confirm("Incluir resultados incertos?", default=False).ask()
        ),
        search_engines=search_engines,
        search_depth=_search_depth_for_mode(scrape_mode),
        lead_providers=lead_providers,
        linkedin_cookie=linkedin_cookie,
        linkedin_cookie_browser=linkedin_cookie_browser,
        parallelism=parallelism,
        output_format=str(
            questionary.select("Formato de saída", choices=["CSV", "XLSX"], default="CSV").ask()
        ),
        output_path=_required_text("Caminho do arquivo de saída", default="output/leads.csv"),
    )


def ask_csv_run(
    default_max_results: int = 20,
    settings: Settings | None = None,
) -> CsvRunAnswers:
    settings = settings or Settings()
    scrape_mode = _ask_scrape_mode()
    lead_providers = _lead_providers_for_mode(scrape_mode, settings)
    search_engines = _search_engines_for_mode(scrape_mode, lead_providers, settings)
    linkedin_cookie = _ask_linkedin_cookie(scrape_mode)
    linkedin_cookie_browser = _ask_cookie_browser(scrape_mode)
    official_sites = scrape_mode == "api" and bool(
        questionary.confirm("Buscar também nos sites oficiais das empresas?", default=True).ask()
    )
    parallelism = 1 if scrape_mode == "cookie" else _positive_int("Quantos workers paralelos usar?", 8)

    return CsvRunAnswers(
        scrape_mode=scrape_mode,
        input_path=_required_text("Caminho do CSV", default="data/companies.example.csv"),
        max_results=_positive_int("Quantos leads você quer tentar puxar por empresa?", default_max_results),
        official_sites=official_sites,
        include_uncertain=bool(
            questionary.confirm("Incluir resultados incertos?", default=False).ask()
        ),
        search_engines=search_engines,
        search_depth=_search_depth_for_mode(scrape_mode),
        lead_providers=lead_providers,
        linkedin_cookie=linkedin_cookie,
        linkedin_cookie_browser=linkedin_cookie_browser,
        parallelism=parallelism,
        output_format=str(questionary.select("Formato de saída", choices=["CSV", "XLSX"], default="CSV").ask()),
        output_path=_required_text("Caminho do arquivo de saída", default="output/leads.csv"),
    )


def ask_linkedin_scraper_run(default_max_results: int = 100) -> LinkedinScraperAnswers:
    use_sales_nav = bool(
        questionary.confirm(
            "Sua conta tem Sales Navigator ativo? "
            "(usa endpoints sales-api e cai para busca padrão se falhar)",
            default=False,
        ).ask()
    )
    label_suffix = " (Sales Navigator)" if use_sales_nav else ""
    default_output = (
        "output/linkedin_sales_nav_leads.csv"
        if use_sales_nav
        else "output/linkedin_cookie_leads.csv"
    )
    return LinkedinScraperAnswers(
        company_name=_required_text(
            f"Qual empresa buscar no LinkedIn com cookie{label_suffix}?"
        ),
        linkedin_url=_required_text("Qual a URL da empresa no LinkedIn?"),
        titles=_required_text("Quais cargos/termos filtrar? Separe por vírgula"),
        max_results=_positive_int("Quantos perfis tentar puxar?", default_max_results),
        include_uncertain=bool(
            questionary.confirm("Incluir resultados incertos?", default=True).ask()
        ),
        linkedin_cookie=_optional_text("Cookie li_at atual (opcional; Enter para detectar automaticamente)"),
        linkedin_cookie_browser=_ask_cookie_browser("cookie"),
        output_format=str(
            questionary.select("Formato de saída", choices=["CSV", "XLSX"], default="CSV").ask()
        ),
        output_path=_required_text(
            "Caminho do arquivo de saída", default=default_output
        ),
        use_sales_navigator=use_sales_nav,
    )


def _ask_scrape_mode() -> str:
    return str(
        questionary.select(
            "Primeiro, escolha a fonte de dados",
            choices=[
                Choice("APIs externas + busca pública configurada", value="api"),
                Choice("SERP-only: sem APIs, sem cookies e sem login", value="serp"),
                Choice("LinkedIn com cookie atual (li_at)", value="cookie"),
            ],
            default="serp",
        ).ask()
    )


def _ask_linkedin_url(scrape_mode: str) -> str | None:
    if scrape_mode == "cookie":
        return _required_text("URL da empresa no LinkedIn (necessária no modo cookie)")
    return _optional_text("URL pública da empresa no LinkedIn? (Opcional)")


def _lead_providers_for_mode(scrape_mode: str, settings: Settings | None = None) -> str:
    if scrape_mode == "serp":
        return "web"
    if scrape_mode == "cookie":
        use_sales_nav = bool(
            questionary.confirm(
                "Sua conta tem Sales Navigator ativo? "
                "(usa endpoints sales-api e cai para busca padrão se falhar)",
                default=False,
            ).ask()
        )
        return "linkedin_sales_navigator" if use_sales_nav else "linkedin_cookie"
    return ",".join(_ask_provider_selection(settings))


def _search_engines_for_mode(
    scrape_mode: str,
    lead_providers: str,
    settings: Settings | None = None,
) -> str:
    settings = settings or Settings()
    if scrape_mode == "cookie":
        return "auto"
    if scrape_mode == "serp":
        return _ask_serp_engines(settings)
    if "web" in lead_providers.split(","):
        return _ask_api_engines(settings)
    return "auto"


def _ask_serp_engines(settings: Settings) -> str:
    has_searxng = bool(settings.searxng_base_url)
    has_serper = bool(settings.serper_api_key)
    has_google = bool(
        settings.google_custom_search_api_key
        and settings.google_custom_search_cx
    )
    has_brave_api = bool(settings.brave_search_api_key)

    options: list[Choice] = []

    if has_searxng:
        options.append(
            Choice(
                "SearxNG local + HTML fallback (Recomendado: grátis e multi-engine)",
                value="searxng,brave_html,duckduckgo_html",
            )
        )
        options.append(Choice("Apenas SearxNG local", value="searxng"))
    if has_serper:
        options.append(
            Choice(
                "Serper API + HTML fallback - mais leads e mais estável (Recomendado)",
                value="serper,brave_html,duckduckgo_html",
            )
        )
        options.append(Choice("Apenas Serper API - mais rápido", value="serper"))
    if has_google:
        options.append(
            Choice(
                "Google Custom Search + HTML fallback",
                value="google,brave_html,duckduckgo_html",
            )
        )
    if has_brave_api:
        options.append(
            Choice(
                "Brave Search API + HTML fallback",
                value="brave,brave_html,duckduckgo_html",
            )
        )
    options.append(
        Choice(
            "Brave HTML + DuckDuckGo HTML - sem API (pode ter CAPTCHA)",
            value="brave_html,duckduckgo_html",
        )
    )
    options.append(Choice("Apenas Brave HTML", value="brave_html"))
    options.append(Choice("Apenas DuckDuckGo HTML", value="duckduckgo_html"))
    options.append(
        Choice("DuckDuckGo (pacote Python, mais resiliente)", value="duckduckgo")
    )

    if not has_searxng and not has_serper and not has_google and not has_brave_api:
        prompt = (
            "Quais motores de busca usar? "
            "(nenhuma API de busca configurada — adicione SEARXNG_BASE_URL ou "
            "SERPER_API_KEY no .env para muito mais leads)"
        )
    else:
        prompt = "Quais motores de busca usar?"

    return str(
        questionary.select(prompt, choices=options).ask()
    )


def _ask_api_engines(settings: Settings) -> str:
    has_searxng = bool(settings.searxng_base_url)
    has_serper = bool(settings.serper_api_key)
    has_google = bool(
        settings.google_custom_search_api_key
        and settings.google_custom_search_cx
    )
    has_brave_api = bool(settings.brave_search_api_key)

    options: list[Choice] = []
    if has_searxng:
        options.append(
            Choice(
                "SearxNG local + HTML fallback (Recomendado: grátis e multi-engine)",
                value="searxng,brave_html,duckduckgo_html",
            )
        )
        options.append(Choice("Apenas SearxNG local", value="searxng"))
    options.append(
        Choice(
            "Automático - usa APIs configuradas + HTML fallback (Recomendado)",
            value="auto",
        )
    )
    if has_serper:
        options.append(
            Choice("Serper API + HTML fallback", value="serper,brave_html,duckduckgo_html")
        )
        options.append(Choice("Apenas Serper API", value="serper"))
    if has_google:
        options.append(
            Choice(
                "Google Custom Search + HTML fallback",
                value="google,brave_html,duckduckgo_html",
            )
        )
    if has_brave_api:
        options.append(
            Choice(
                "Brave Search API + HTML fallback",
                value="brave,brave_html,duckduckgo_html",
            )
        )
    options.append(
        Choice("Brave HTML + DuckDuckGo HTML - sem API", value="brave_html,duckduckgo_html")
    )
    options.append(Choice("Apenas Brave HTML", value="brave_html"))
    options.append(Choice("Apenas DuckDuckGo HTML", value="duckduckgo_html"))
    options.append(Choice("DuckDuckGo pacote Python", value="duckduckgo"))
    if has_serper or has_google or has_brave_api:
        api_engines = ",".join(
            engine
            for engine, configured in [
                ("serper", has_serper),
                ("google", has_google),
                ("brave", has_brave_api),
            ]
            if configured
        )
        options.append(
            Choice(f"Apenas APIs configuradas ({api_engines})", value=api_engines)
        )

    return str(
        questionary.select(
            "Quais motores de busca pública usar?",
            choices=options,
            default="auto",
        ).ask()
    )


def _search_depth_for_mode(scrape_mode: str) -> str:
    if scrape_mode == "cookie":
        return "standard"
    return str(
        questionary.select(
            "Profundidade da busca",
            choices=[
                Choice("Padrão - mais rápida", value="standard"),
                Choice("Profunda - mais consultas e mais chance de achar leads", value="deep"),
            ],
            default="deep" if scrape_mode == "serp" else "standard",
        ).ask()
    )


def _ask_linkedin_cookie(scrape_mode: str) -> str | None:
    if scrape_mode != "cookie":
        return None
    return _optional_text("Cookie li_at atual (opcional; Enter para detectar automaticamente)")


def _ask_cookie_browser(scrape_mode: str) -> str:
    if scrape_mode != "cookie":
        return "auto"
    return str(
        questionary.select(
            "Onde tentar detectar o cookie, se você não informar manualmente?",
            choices=[
                Choice("Auto: Chrome, Edge, Brave ou Firefox", value="auto"),
                Choice("Chrome", value="chrome"),
                Choice("Edge", value="edge"),
                Choice("Brave", value="brave"),
                Choice("Firefox", value="firefox"),
                Choice("Não detectar navegador", value="none"),
            ],
            default="auto",
        ).ask()
    )


def _ask_provider_selection(settings: Settings | None = None) -> list[str]:
    settings = settings or Settings()
    choices = [
        _provider_choice("People Data Labs", "pdl", bool(settings.people_data_labs_api_key)),
        _provider_choice("Coresignal", "coresignal", bool(settings.coresignal_api_key)),
        _provider_choice("Apollo", "apollo", bool(settings.apollo_api_key)),
        _provider_choice("Lusha", "lusha", bool(settings.lusha_api_key)),
        Choice(
            "Apify LinkedIn Actor - scraping terceiro, alto cuidado de compliance"
            if settings.apify_api_key
            else "Apify LinkedIn Actor - sem chave no .env",
            value="apify_linkedin",
            checked=False,
            disabled=None if settings.apify_api_key else "Configure APIFY_API_KEY no .env para usar este Actor.",
        ),
        Choice(
            "Diretórios públicos (TheOrg, RocketReach) - sem API",
            value="public_directories",
            checked=False,
        ),
        Choice(
            "Common Crawl - mineração grátis de captures públicos (lento)",
            value="common_crawl",
            checked=False,
        ),
        Choice("Busca pública na web - sem API privada", value="web", checked=True),
    ]
    selected = questionary.checkbox(
        "Quais fontes você quer usar?",
        choices=choices,
        validate=lambda value: bool(value) or "Selecione pelo menos uma fonte.",
    ).ask()
    if selected is None:
        raise KeyboardInterrupt
    return list(selected)


def _provider_choice(name: str, value: str, configured: bool) -> Choice:
    suffix = "configurada" if configured else "sem chave no .env"
    return Choice(
        f"{name} API - {suffix}",
        value=value,
        checked=configured,
        disabled=None if configured else "Configure a chave no .env para usar esta API.",
    )


def _required_text(message: str, default: str | None = None) -> str:
    answer = questionary.text(message, default=default or "").ask()
    if answer is None:
        raise KeyboardInterrupt
    cleaned = answer.strip()
    if not cleaned:
        raise ValueError(f"{message} is required.")
    return cleaned


def _optional_text(message: str) -> str | None:
    answer = questionary.text(message).ask()
    if answer is None:
        raise KeyboardInterrupt
    cleaned = answer.strip()
    return cleaned or None


def _positive_int(message: str, default: int) -> int:
    answer = questionary.text(
        message,
        default=str(default),
        validate=lambda value: value.isdigit() and int(value) > 0,
    ).ask()
    if answer is None:
        raise KeyboardInterrupt
    return int(answer)
