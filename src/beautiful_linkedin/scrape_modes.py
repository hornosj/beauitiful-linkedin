from __future__ import annotations

from dataclasses import dataclass


ScrapeMode = str

DEFAULT_SERP_QUERY_LIMIT = 48


@dataclass(frozen=True)
class ScrapeModeOptions:
    scrape_mode: ScrapeMode
    label: str
    lead_providers: list[str]
    official_sites: bool
    web_query_limit: int
    parallelism: int


def parse_scrape_mode(value: str | None) -> ScrapeMode:
    normalized = (value or "api").strip().lower().replace("_", "-")
    aliases = {
        "api": "api",
        "apis": "api",
        "providers": "api",
        "externos": "api",
        "serp": "serp",
        "serp-only": "serp",
        "sem-cookie": "serp",
        "sem-cookies": "serp",
        "no-cookie": "serp",
        "no-cookies": "serp",
        "public": "serp",
        "publico": "serp",
        "público": "serp",
        "sem-conta": "serp",
        "cookie": "cookie",
        "cookies": "cookie",
        "com-cookie": "cookie",
        "com-cookies": "cookie",
        "linkedin-cookie": "cookie",
        "browser": "browser",
        "navegador": "browser",
        "playwright": "browser",
        "linkedin-playwright": "browser",
        "arriscado": "browser",
        "risky": "browser",
        "arriscar-conta": "browser",
        "people-search": "people_search",
        "people": "people_search",
        "people_search": "people_search",
        "linkedin-people": "people_search",
        "listing": "people_search",
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ValueError(
            "Modo desconhecido. Use api, serp, cookie, browser ou people_search."
        ) from exc


def apply_scrape_mode(
    scrape_mode: str | None,
    lead_providers: list[str],
    official_sites: bool,
    web_query_limit: int,
    parallelism: int,
) -> ScrapeModeOptions:
    mode = parse_scrape_mode(scrape_mode)
    if mode == "serp":
        return ScrapeModeOptions(
            scrape_mode=mode,
            label="SERP-only - Sem APIs externas e sem cookies",
            lead_providers=["web"],
            official_sites=False,
            web_query_limit=web_query_limit if web_query_limit > 0 else DEFAULT_SERP_QUERY_LIMIT,
            parallelism=max(1, min(parallelism, 2)),
        )

    if mode == "people_search":
        return ScrapeModeOptions(
            scrape_mode=mode,
            label=(
                "LinkedIn People Search - busca pelo filtro da aba People sem visitar perfis. "
                "Usa li_at, conservador."
            ),
            lead_providers=["linkedin_people_search"],
            official_sites=False,
            web_query_limit=0,
            parallelism=1,
        )

    if mode == "browser":
        return ScrapeModeOptions(
            scrape_mode=mode,
            label=(
                "ARRISCADO - navegador logado via Playwright. "
                "Pode bloquear sua conta do LinkedIn. Use por sua conta e risco."
            ),
            lead_providers=["linkedin_playwright"],
            official_sites=False,
            web_query_limit=0,
            parallelism=1,
        )

    if mode == "cookie":
        cookie_aliases = {
            "linkedin_cookie",
            "cookie",
            "li_at",
            "linkedin_sales_navigator",
            "sales_navigator",
            "sales_nav",
            "salesnav",
        }
        cookie_providers = [p for p in lead_providers if p in cookie_aliases]
        if not cookie_providers:
            cookie_providers = ["linkedin_cookie"]
        uses_sales_nav = any(
            p in {"linkedin_sales_navigator", "sales_navigator", "sales_nav", "salesnav"}
            for p in cookie_providers
        )
        label = (
            "LinkedIn com Sales Navigator - usa li_at + endpoints sales-api"
            if uses_sales_nav
            else "LinkedIn com cookie - usa li_at atual quando disponível"
        )
        return ScrapeModeOptions(
            scrape_mode=mode,
            label=label,
            lead_providers=cookie_providers,
            official_sites=False,
            web_query_limit=0,
            parallelism=1,
        )

    return ScrapeModeOptions(
        scrape_mode=mode,
        label="APIs externas / busca pública configurada",
        lead_providers=lead_providers,
        official_sites=official_sites,
        web_query_limit=web_query_limit,
        parallelism=parallelism,
    )
