from __future__ import annotations

import logging

from beautiful_linkedin.cache.sqlite_cache import SqliteJsonCache
from beautiful_linkedin.config import Settings
from beautiful_linkedin.providers.apify_linkedin import ApifyLinkedInEmployeesProvider
from beautiful_linkedin.providers.common_crawl import CommonCrawlProvider
from beautiful_linkedin.providers.coresignal import CoresignalEmployeeProvider
from beautiful_linkedin.providers.lead_provider import LeadProvider
from beautiful_linkedin.providers.people_data_labs import PeopleDataLabsProvider
from beautiful_linkedin.providers.apollo import ApolloProvider
from beautiful_linkedin.providers.lusha import LushaProvider
from beautiful_linkedin.providers.linkedin_cookie import LinkedInCookieEmployeesProvider
from beautiful_linkedin.providers.linkedin_llm_extractor import LLMExtractionConfig
from beautiful_linkedin.providers.linkedin_people_search import (
    LinkedInPeopleSearchProvider,
    PeopleSearchOptions,
)
from beautiful_linkedin.providers.linkedin_playwright import (
    LinkedInPlaywrightProvider,
    PlaywrightCollectorOptions,
)
from beautiful_linkedin.providers.linkedin_sales_navigator import (
    LinkedInSalesNavigatorProvider,
)
from beautiful_linkedin.providers.public_directories import PublicDirectoriesProvider
from beautiful_linkedin.providers.public_search import PublicSearchLeadProvider
from beautiful_linkedin.search.duckduckgo_search import DuckDuckGoSearchEngine
from beautiful_linkedin.search.search_engine import SearchEngine

logger = logging.getLogger(__name__)


def build_lead_providers(
    settings: Settings,
    search_engine: SearchEngine | None,
    provider_names: list[str] | None = None,
    search_parallelism: int = 3,
    web_query_limit: int | None = None,
    cache: SqliteJsonCache | None = None,
) -> list[LeadProvider]:
    selected = [name.strip().lower() for name in (provider_names or ["auto"]) if name.strip()]
    if "auto" in selected:
        selected = ["pdl", "coresignal", "apollo", "lusha", "web"]

    providers: list[LeadProvider] = []
    for name in selected:
        if name in {"web", "search", "public_search"}:
            if web_query_limit == 0:
                logger.warning("Busca pública ignorada: o limite de consultas web é 0.")
                continue
            providers.append(
                PublicSearchLeadProvider(
                    search_engine or DuckDuckGoSearchEngine(),
                    parallelism=search_parallelism,
                    query_limit=web_query_limit,
                )
            )
        elif name in {"pdl", "people_data_labs", "peopledatalabs"}:
            if settings.people_data_labs_api_key:
                providers.append(
                    PeopleDataLabsProvider(
                        api_key=settings.people_data_labs_api_key,
                        cache=cache,
                    )
                )
            else:
                logger.warning("People Data Labs ignorada: PEOPLE_DATA_LABS_API_KEY não está configurada.")
        elif name == "coresignal":
            if settings.coresignal_api_key:
                providers.append(
                    CoresignalEmployeeProvider(
                        api_key=settings.coresignal_api_key,
                        cache=cache,
                    )
                )
            else:
                logger.warning("Coresignal ignorada: CORESIGNAL_API_KEY não está configurada.")
        elif name == "apollo":
            if settings.apollo_api_key:
                providers.append(
                    ApolloProvider(
                        api_key=settings.apollo_api_key,
                        cache=cache,
                    )
                )
            else:
                logger.warning("Apollo ignorada: APOLLO_API_KEY não está configurada.")
        elif name == "lusha":
            if settings.lusha_api_key:
                providers.append(
                    LushaProvider(
                        api_key=settings.lusha_api_key,
                        cache=cache,
                    )
                )
            else:
                logger.warning("Lusha ignorada: LUSHA_API_KEY não está configurada.")
        elif name in {"apify_linkedin", "apify", "linkedin_apify"}:
            if settings.apify_api_key:
                providers.append(
                    ApifyLinkedInEmployeesProvider(
                        api_key=settings.apify_api_key,
                        cache=cache,
                    )
                )
            else:
                logger.warning("Apify LinkedIn Actor ignorado: APIFY_API_KEY não está configurada.")
        elif name in {"public_directories", "public_dirs"}:
            providers.append(PublicDirectoriesProvider(cache=cache))
        elif name == "theorg":
            providers.append(PublicDirectoriesProvider(sources=["theorg"], cache=cache))
        elif name == "rocketreach_public":
            providers.append(
                PublicDirectoriesProvider(sources=["rocketreach"], cache=cache)
            )
        elif name in {"common_crawl", "commoncrawl", "cc"}:
            providers.append(CommonCrawlProvider(cache=cache))
        elif name in {"linkedin_cookie", "cookie", "li_at"}:
            providers.append(
                LinkedInCookieEmployeesProvider(
                    cookie=settings.linkedin_li_at_cookie,
                    cookie_browser=settings.linkedin_cookie_browser,
                )
            )
        elif name in {
            "linkedin_sales_navigator",
            "sales_navigator",
            "sales_nav",
            "salesnav",
        }:
            providers.append(
                LinkedInSalesNavigatorProvider(
                    cookie=settings.linkedin_li_at_cookie,
                    cookie_browser=settings.linkedin_cookie_browser,
                )
            )
        elif name in {
            "linkedin_people_search",
            "people_search",
            "people",
            "linkedin_listing",
        }:
            providers.append(
                LinkedInPeopleSearchProvider(
                    cookie=settings.linkedin_li_at_cookie,
                    cookie_browser=settings.linkedin_cookie_browser,
                    options=PeopleSearchOptions(
                        headless=settings.playwright_headless,
                        user_data_dir=settings.playwright_user_data_dir,
                        cdp_endpoint=settings.linkedin_cdp_endpoint,
                        cdp_enabled=settings.linkedin_cdp_enabled,
                        cards_per_cycle=settings.linkedin_cards_per_cycle,
                    ),
                )
            )
        elif name in {
            "linkedin_playwright",
            "playwright",
            "browser",
            "navegador",
            "linkedin_browser",
        }:
            logger.warning(
                "Modo navegador (Playwright) ARRISCADO ativado: a conta do LinkedIn pode ser bloqueada. "
                "Use por sua conta e risco."
            )
            llm_config = _build_llm_extraction_config(settings)
            providers.append(
                LinkedInPlaywrightProvider(
                    cookie=settings.linkedin_li_at_cookie,
                    cookie_browser=settings.linkedin_cookie_browser,
                    options=PlaywrightCollectorOptions(
                        headless=settings.playwright_headless,
                        user_data_dir=settings.playwright_user_data_dir,
                    ),
                    llm_extraction_config=llm_config,
                )
            )
        else:
            logger.warning("Provider de leads desconhecido '%s'. Ignorando.", name)

    if not providers and web_query_limit != 0:
        providers.append(
            PublicSearchLeadProvider(
                search_engine,
                parallelism=search_parallelism,
                query_limit=web_query_limit,
            )
        )
    return providers


def _build_llm_extraction_config(settings: Settings) -> LLMExtractionConfig | None:
    if not settings.linkedin_llm_extraction_enabled:
        return None
    if not settings.openai_api_key:
        logger.info(
            "LLM fallback do LinkedIn desabilitado: OPENAI_API_KEY não configurada."
        )
        return None
    return LLMExtractionConfig(
        model=settings.linkedin_llm_extraction_model,
        api_token=settings.openai_api_key,
        timeout_seconds=settings.linkedin_llm_extraction_timeout_seconds,
    )
