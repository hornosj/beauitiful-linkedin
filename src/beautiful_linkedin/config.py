from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


USER_AGENT = (
    "Mozilla/5.0 (compatible; BeautifulLinkedIn/1.0; "
    "+https://github.com/beautiful-linkedin/beautiful-linkedin)"
)


@dataclass(frozen=True)
class Settings:
    default_max_results: int = 20
    company_site_timeout_seconds: float = 8.0
    company_site_max_pages: int = 8
    brave_search_api_key: str | None = None
    google_custom_search_api_key: str | None = None
    google_custom_search_cx: str | None = None
    serper_api_key: str | None = None
    searxng_base_url: str | None = None
    people_data_labs_api_key: str | None = None
    coresignal_api_key: str | None = None
    apollo_api_key: str | None = None
    lusha_api_key: str | None = None
    snovio_client_id: str | None = None
    snovio_client_secret: str | None = None
    apify_api_key: str | None = None
    linkedin_li_at_cookie: str | None = None
    linkedin_cookie_browser: str = "auto"
    playwright_user_data_dir: str | None = None
    playwright_headless: bool = True
    cache_path: str = "data/cache.sqlite"
    cache_ttl_seconds: int | None = None
    saved_leads_path: str = "data/saved_leads.sqlite"
    web_query_limit: int = 48
    provider_timeout_seconds: float = 120.0
    openai_api_key: str | None = None
    linkedin_llm_extraction_model: str = "openai/gpt-4o-mini"
    linkedin_llm_extraction_enabled: bool = True
    linkedin_llm_extraction_timeout_seconds: float = 45.0
    linkedin_cdp_endpoint: str = "http://127.0.0.1:9222"
    linkedin_cdp_enabled: bool = True
    linkedin_cards_per_cycle: int = 8
    telegram_api_id: str | None = None
    telegram_api_hash: str | None = None
    telegram_session_name: str = "data/telegram_phone_lookup"
    telegram_phone_bot_username: str = "@ConsultoriaGonzalesbot"
    telegram_group_username: str | None = None
    telegram_group_session_name: str = "data/telegram_group_phone_lookup"
    telegram_group_capture_seconds: float = 30.0
    telegram_group_throttle_seconds: float = 5.0
    # Pausa fixa após pressionar Enter no composer. Em vez de um valor único
    # determinístico (que vira fingerprint), usamos jitter uniforme entre min
    # e max — defaults conservadores baseados no que os bots Gonzales/Findex
    # levam para emitir o primeiro botão "Resultado".
    telegram_post_send_min_seconds: float = 4.5
    telegram_post_send_max_seconds: float = 6.5
    # Pausa humana entre processar dois leads consecutivos no loop do
    # endpoint /telegram-phone. Sem isso o operador disparava 10 consultas em
    # ~60s e os bots começavam a rate-limitar. Default pequeno mas presente.
    telegram_inter_lead_min_seconds: float = 3.0
    telegram_inter_lead_max_seconds: float = 6.0
    # Quanto tempo a automação espera o Gonzales responder antes de
    # desistir (em segundos, contado APÓS o post_send_wait). 45s é folgado
    # o suficiente para casos lentos sem travar a UI indefinidamente
    # quando o bot realmente não responde.
    telegram_gon_abort_timeout_seconds: float = 45.0


def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        default_max_results=int(os.getenv("BEAUTIFUL_LINKEDIN_MAX_RESULTS", "20")),
        company_site_timeout_seconds=float(
            os.getenv("BEAUTIFUL_LINKEDIN_SITE_TIMEOUT_SECONDS", "8")
        ),
        company_site_max_pages=int(os.getenv("BEAUTIFUL_LINKEDIN_SITE_MAX_PAGES", "8")),
        brave_search_api_key=_empty_to_none(os.getenv("BRAVE_SEARCH_API_KEY")),
        google_custom_search_api_key=_empty_to_none(
            os.getenv("GOOGLE_CUSTOM_SEARCH_API_KEY")
        ),
        google_custom_search_cx=_empty_to_none(os.getenv("GOOGLE_CUSTOM_SEARCH_CX")),
        serper_api_key=_empty_to_none(os.getenv("SERPER_API_KEY")),
        searxng_base_url=_empty_to_none(os.getenv("SEARXNG_BASE_URL")),
        people_data_labs_api_key=_empty_to_none(
            os.getenv("PEOPLE_DATA_LABS_API_KEY") or os.getenv("PDL_API_KEY")
        ),
        coresignal_api_key=_empty_to_none(os.getenv("CORESIGNAL_API_KEY")),
        apollo_api_key=_empty_to_none(os.getenv("APOLLO_API_KEY")),
        lusha_api_key=_empty_to_none(os.getenv("LUSHA_API_KEY")),
        snovio_client_id=_empty_to_none(os.getenv("SNOVIO_CLIENT_ID")),
        snovio_client_secret=_empty_to_none(os.getenv("SNOVIO_CLIENT_SECRET")),
        apify_api_key=_empty_to_none(os.getenv("APIFY_API_KEY")),
        linkedin_li_at_cookie=_empty_to_none(
            os.getenv("LINKEDIN_LI_AT_COOKIE")
            or os.getenv("LINKEDIN_COOKIE")
            or os.getenv("LI_AT")
        ),
        linkedin_cookie_browser=os.getenv("LINKEDIN_COOKIE_BROWSER", "auto"),
        playwright_user_data_dir=_empty_to_none(
            os.getenv("BEAUTIFUL_LINKEDIN_PLAYWRIGHT_USER_DATA_DIR")
        ),
        playwright_headless=_parse_bool(
            os.getenv("BEAUTIFUL_LINKEDIN_PLAYWRIGHT_HEADLESS"), default=True
        ),
        cache_path=os.getenv("BEAUTIFUL_LINKEDIN_CACHE_PATH", "data/cache.sqlite"),
        cache_ttl_seconds=_optional_int(os.getenv("BEAUTIFUL_LINKEDIN_CACHE_TTL_SECONDS")),
        saved_leads_path=os.getenv(
            "BEAUTIFUL_LINKEDIN_SAVED_LEADS_PATH", "data/saved_leads.sqlite"
        ),
        web_query_limit=int(os.getenv("BEAUTIFUL_LINKEDIN_WEB_QUERY_LIMIT", "48")),
        provider_timeout_seconds=float(
            os.getenv("BEAUTIFUL_LINKEDIN_PROVIDER_TIMEOUT_SECONDS", "120")
        ),
        openai_api_key=_empty_to_none(os.getenv("OPENAI_API_KEY")),
        linkedin_llm_extraction_model=os.getenv(
            "LINKEDIN_LLM_EXTRACTION_MODEL", "openai/gpt-4o-mini"
        ),
        linkedin_llm_extraction_enabled=_parse_bool(
            os.getenv("LINKEDIN_LLM_EXTRACTION_ENABLED"), default=True
        ),
        linkedin_llm_extraction_timeout_seconds=float(
            os.getenv("LINKEDIN_LLM_EXTRACTION_TIMEOUT_SECONDS", "45")
        ),
        linkedin_cdp_endpoint=os.getenv(
            "LINKEDIN_CDP_ENDPOINT", "http://127.0.0.1:9222"
        ),
        linkedin_cdp_enabled=_parse_bool(
            os.getenv("LINKEDIN_CDP_ENABLED"), default=True
        ),
        linkedin_cards_per_cycle=int(
            os.getenv("LINKEDIN_CARDS_PER_CYCLE", "8") or "8"
        ),
        telegram_api_id=_empty_to_none(
            os.getenv("BEAUTIFUL_LINKEDIN_TELEGRAM_API_ID")
            or os.getenv("TELEGRAM_API_ID")
        ),
        telegram_api_hash=_empty_to_none(
            os.getenv("BEAUTIFUL_LINKEDIN_TELEGRAM_API_HASH")
            or os.getenv("TELEGRAM_API_HASH")
        ),
        telegram_session_name=os.getenv(
            "BEAUTIFUL_LINKEDIN_TELEGRAM_SESSION_NAME",
            "data/telegram_phone_lookup",
        ),
        telegram_phone_bot_username=os.getenv(
            "BEAUTIFUL_LINKEDIN_TELEGRAM_PHONE_BOT",
            "@ConsultoriaGonzalesbot",
        ),
        telegram_group_username=_empty_to_none(
            os.getenv("BEAUTIFUL_LINKEDIN_TELEGRAM_GROUP_USERNAME")
        ),
        telegram_group_session_name=os.getenv(
            "BEAUTIFUL_LINKEDIN_TELEGRAM_GROUP_SESSION_NAME",
            "data/telegram_group_phone_lookup",
        ),
        telegram_group_capture_seconds=float(
            os.getenv("BEAUTIFUL_LINKEDIN_TELEGRAM_GROUP_CAPTURE_SECONDS", "30") or "30"
        ),
        telegram_group_throttle_seconds=float(
            os.getenv("BEAUTIFUL_LINKEDIN_TELEGRAM_GROUP_THROTTLE_SECONDS", "5") or "5"
        ),
        telegram_post_send_min_seconds=float(
            os.getenv("BEAUTIFUL_LINKEDIN_TELEGRAM_POST_SEND_MIN", "4.5") or "4.5"
        ),
        telegram_post_send_max_seconds=float(
            os.getenv("BEAUTIFUL_LINKEDIN_TELEGRAM_POST_SEND_MAX", "6.5") or "6.5"
        ),
        telegram_inter_lead_min_seconds=float(
            os.getenv("BEAUTIFUL_LINKEDIN_TELEGRAM_INTER_LEAD_MIN", "3.0") or "3.0"
        ),
        telegram_inter_lead_max_seconds=float(
            os.getenv("BEAUTIFUL_LINKEDIN_TELEGRAM_INTER_LEAD_MAX", "6.0") or "6.0"
        ),
        telegram_gon_abort_timeout_seconds=float(
            os.getenv("BEAUTIFUL_LINKEDIN_TELEGRAM_GON_ABORT_TIMEOUT", "45.0") or "45.0"
        ),
    )


def _empty_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    return int(value)


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    cleaned = value.strip().lower()
    if not cleaned:
        return default
    if cleaned in {"true", "1", "yes", "y", "sim", "s"}:
        return True
    if cleaned in {"false", "0", "no", "n", "nao", "não"}:
        return False
    return default
