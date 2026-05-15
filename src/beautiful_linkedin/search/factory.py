from __future__ import annotations

import logging

from beautiful_linkedin.config import Settings
from beautiful_linkedin.search.bing_html_search import BingHtmlSearchEngine
from beautiful_linkedin.search.brave_html_search import BraveHtmlSearchEngine
from beautiful_linkedin.search.brave_search import BraveSearchEngine
from beautiful_linkedin.search.composite_search import CompositeSearchEngine
from beautiful_linkedin.search.crawl4ai_search import Crawl4aiSearchEngine
from beautiful_linkedin.search.duckduckgo_html_search import DuckDuckGoHtmlSearchEngine
from beautiful_linkedin.search.duckduckgo_search import DuckDuckGoSearchEngine
from beautiful_linkedin.search.google_custom_search import GoogleCustomSearchEngine
from beautiful_linkedin.search.google_html_search import GoogleHtmlSearchEngine
from beautiful_linkedin.search.search_engine import SearchEngine
from beautiful_linkedin.search.searxng_search import SearxngSearchEngine
from beautiful_linkedin.search.serper_search import SerperSearchEngine
from beautiful_linkedin.search.smart_composite_search import SmartCompositeSearchEngine

logger = logging.getLogger(__name__)


def build_search_engine(settings: Settings, engine_names: list[str] | None = None) -> SearchEngine:
    selected = [name.strip().lower() for name in (engine_names or ["auto"]) if name.strip()]
    if "auto" in selected:
        selected = _build_auto_engine_list(settings)

    engines: list[SearchEngine] = []

    for name in selected:
        if name in {"google_html", "google-html"}:
            engines.append(GoogleHtmlSearchEngine())
        elif name in {"bing_html", "bing-html", "bing"}:
            engines.append(BingHtmlSearchEngine())
        elif name in {"brave_html", "brave-html"}:
            engines.append(BraveHtmlSearchEngine())
        elif name in {"duckduckgo_html", "ddg_html", "duckduckgo-html"}:
            engines.append(DuckDuckGoHtmlSearchEngine())
        elif name == "duckduckgo":
            engines.append(DuckDuckGoSearchEngine())
        elif name == "brave":
            if settings.brave_search_api_key:
                engines.append(BraveSearchEngine(api_key=settings.brave_search_api_key))
            else:
                logger.warning("Brave Search ignorado: BRAVE_SEARCH_API_KEY não está configurada.")
        elif name in {"google", "google_cse"}:
            if settings.google_custom_search_api_key and settings.google_custom_search_cx:
                engines.append(
                    GoogleCustomSearchEngine(
                        api_key=settings.google_custom_search_api_key,
                        search_engine_id=settings.google_custom_search_cx,
                    )
                )
            else:
                logger.warning(
                    "Google Custom Search ignorado: GOOGLE_CUSTOM_SEARCH_API_KEY ou GOOGLE_CUSTOM_SEARCH_CX não está configurado."
                )
        elif name == "serper":
            if settings.serper_api_key:
                engines.append(SerperSearchEngine(api_key=settings.serper_api_key))
            else:
                logger.warning("Serper ignorado: SERPER_API_KEY não está configurada.")
        elif name in {"searxng", "searx"}:
            if settings.searxng_base_url:
                engines.append(SearxngSearchEngine(base_url=settings.searxng_base_url))
            else:
                logger.warning("SearxNG ignorado: SEARXNG_BASE_URL não configurada.")
        elif name in {"crawl4ai", "crawl_4_ai", "crawl-4-ai"}:
            engines.append(Crawl4aiSearchEngine())
        else:
            logger.warning("Motor de busca desconhecido '%s'. Ignorando.", name)

    if not engines:
        logger.warning("Nenhum motor de busca configurado. Usando DuckDuckGo + Google HTML + Bing HTML.")
        engines.append(DuckDuckGoSearchEngine())
        engines.append(GoogleHtmlSearchEngine())
        engines.append(BingHtmlSearchEngine())

    if len(engines) == 1:
        return engines[0]

    return SmartCompositeSearchEngine(engines)


def _build_auto_engine_list(settings: Settings) -> list[str]:
    """Monta a ordem: SearxNG, Serper, Google CSE, DuckDuckGo e fallbacks HTML."""
    engines: list[str] = []

    if settings.searxng_base_url:
        engines.append("searxng")
        logger.info("Auto: SearxNG local configurado e será usado como engine principal.")

    if settings.serper_api_key:
        engines.append("serper")
        logger.info("Auto: Serper API configurado e será usado como engine principal.")

    if settings.google_custom_search_api_key and settings.google_custom_search_cx:
        engines.append("google_cse")

    engines.append("duckduckgo")

    engines.append("google_html")
    engines.append("bing_html")

    return engines
