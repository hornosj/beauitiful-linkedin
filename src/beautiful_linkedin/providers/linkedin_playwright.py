"""LinkedIn Playwright provider — modo ARRISCADO de coleta logada.

Este provider sobe um Chromium controlado via Playwright, carrega o cookie
``li_at`` da conta do operador, navega até a aba "People" da página da
empresa no LinkedIn, aplica o filtro de palavra-chave e extrai os perfis
visíveis. É o método com maior cobertura ("perto do que um humano logado vê"),
mas também o de maior risco — o LinkedIn pode aplicar restrição temporária
ou banimento da conta envolvida. Use com a conta certa, ritmo conservador
e consentimento explícito do operador.

A camada de orquestração (browser/contexto/página) é separada em métodos
internos para que os testes possam injetar registros sem subir Chromium.
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from beautiful_linkedin.cookie_resolver import resolve_linkedin_li_at_cookie
from beautiful_linkedin.models import CompanyInput, Lead
from beautiful_linkedin.processing.lead_extractor import match_target_title
from beautiful_linkedin.processing.normalizer import normalize_text
from beautiful_linkedin.processing.scorer import score_lead
from beautiful_linkedin.providers.lead_provider import LeadProvider

# Imported lazily inside the methods that use it to avoid an import cycle:
# linkedin_llm_extractor itself depends on PlaywrightProfileRecord defined here.

logger = logging.getLogger(__name__)


LINKEDIN_BASE = "https://www.linkedin.com"
PEOPLE_URL_TEMPLATE = LINKEDIN_BASE + "/company/{slug}/people/"
DEFAULT_NAV_TIMEOUT_MS = 45_000
DEFAULT_MAX_SCROLLS = 25
DEFAULT_MIN_DELAY_SECONDS = 2.5
DEFAULT_MAX_DELAY_SECONDS = 6.0
PROFILE_LINK_SELECTOR = "a[href*='/in/']"


@dataclass(frozen=True)
class PlaywrightProfileRecord:
    """Raw profile data extracted from one profile card on the People tab."""

    full_name: str | None
    headline: str | None
    location: str | None
    profile_url: str


@dataclass
class PlaywrightCollectorOptions:
    headless: bool = True
    nav_timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS
    max_scrolls: int = DEFAULT_MAX_SCROLLS
    min_delay_seconds: float = DEFAULT_MIN_DELAY_SECONDS
    max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS
    user_data_dir: str | None = None
    extra_browser_args: list[str] = field(default_factory=list)


class LinkedInPlaywrightProvider(LeadProvider):
    """RISKY logged-in scraper for the company People tab via Playwright.

    The provider keeps the heavy browser orchestration in
    ``collect_profile_records`` so unit tests can monkeypatch it with a fake
    list of records and exercise the lead-conversion path end-to-end without a
    real browser.
    """

    name = "linkedin_playwright"

    def __init__(
        self,
        cookie: str | None = None,
        cookie_browser: str = "auto",
        options: PlaywrightCollectorOptions | None = None,
        llm_extraction_config: Any | None = None,
    ) -> None:
        self.cookie = cookie
        self.cookie_browser = cookie_browser
        self.options = options or PlaywrightCollectorOptions()
        self.llm_extraction_config = llm_extraction_config

    def find_leads(
        self,
        company: CompanyInput,
        max_results: int,
        include_uncertain: bool,
        search_depth: str = "standard",
        offset: int = 0,
    ) -> list[Lead]:
        if offset > 0:
            return []

        li_at = resolve_linkedin_li_at_cookie(self.cookie, browser=self.cookie_browser)
        if not li_at:
            logger.warning(
                "Modo navegador (Playwright) ARRISCADO: cookie li_at não encontrado. "
                "Faça login no LinkedIn no navegador ou configure LINKEDIN_LI_AT_COOKIE."
            )
            return []

        slug = company_universal_name(company)
        if not slug:
            logger.warning(
                "Modo navegador: não consegui resolver slug do LinkedIn para %s.",
                company.company_name,
            )
            return []

        try:
            records = self.collect_profile_records(
                li_at=li_at,
                company_slug=slug,
                titles=company.titles,
                max_results=max_results,
            )
        except RuntimeError as exc:
            logger.warning(
                "Modo navegador (Playwright) falhou para %s: %s",
                company.company_name, exc,
            )
            return []
        except Exception as exc:
            logger.warning(
                "Erro inesperado no modo navegador para %s. %s: %s",
                company.company_name, type(exc).__name__, exc,
            )
            return []

        leads: list[Lead] = []
        for record in records:
            lead = record_to_lead(company, record, include_uncertain)
            if lead:
                leads.append(lead)
        logger.info(
            "Playwright (logado): %d perfis brutos para %s, %d passaram no filtro local de cargo",
            len(records), company.company_name, len(leads),
        )
        return leads[:max_results]

    def collect_profile_records(
        self,
        li_at: str,
        company_slug: str,
        titles: list[str],
        max_results: int,
    ) -> list[PlaywrightProfileRecord]:
        """Drive a real Chromium via Playwright and return raw profile records.

        Raises ``RuntimeError`` if Playwright is not installed or if LinkedIn
        rejects the session. Tests should monkeypatch this method to avoid
        spawning a browser.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright não está instalado. Rode: pip install 'beautiful-linkedin[risky]' "
                "e depois 'playwright install chromium'."
            ) from exc

        opts = self.options
        records: list[PlaywrightProfileRecord] = []

        with sync_playwright() as pw:
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                *opts.extra_browser_args,
            ]
            if opts.user_data_dir:
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=opts.user_data_dir,
                    headless=opts.headless,
                    args=launch_args,
                    viewport={"width": 1366, "height": 800},
                )
                browser = None
            else:
                browser = pw.chromium.launch(headless=opts.headless, args=launch_args)
                context = browser.new_context(
                    viewport={"width": 1366, "height": 800},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                )

            try:
                _apply_stealth(context)
                context.add_cookies([
                    {
                        "name": "li_at",
                        "value": li_at,
                        "domain": ".linkedin.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                    }
                ])
                page = context.new_page()
                page.set_default_navigation_timeout(opts.nav_timeout_ms)

                seen: set[str] = set()
                title_terms = titles or [""]
                for title in title_terms:
                    if len(records) >= max_results:
                        break
                    url = build_people_url(company_slug, title)
                    page.goto(url, wait_until="domcontentloaded")
                    _human_delay(opts)
                    if _is_blocked(page.url):
                        raise RuntimeError(
                            f"LinkedIn redirecionou para {page.url} — sessão pode ter expirado ou estar restrita."
                        )

                    title_start = len(records)
                    for _ in range(max(1, opts.max_scrolls)):
                        if len(records) >= max_results:
                            break
                        new_records = _extract_records_from_page(page)
                        added = 0
                        for record in new_records:
                            if record.profile_url in seen:
                                continue
                            seen.add(record.profile_url)
                            records.append(record)
                            added += 1
                            if len(records) >= max_results:
                                break
                        if added == 0:
                            break
                        page.mouse.wheel(0, 1500)
                        _human_delay(opts)

                    if self.llm_extraction_config is not None:
                        title_records = records[title_start:]
                        merged = _maybe_apply_llm_fallback(
                            page=page,
                            title_records=title_records,
                            llm_config=self.llm_extraction_config,
                        )
                        if merged is not None:
                            del records[title_start:]
                            for record in merged:
                                if record.profile_url in seen and record.profile_url not in {
                                    r.profile_url for r in title_records
                                }:
                                    continue
                                seen.add(record.profile_url)
                                records.append(record)
                                if len(records) >= max_results:
                                    break
            finally:
                try:
                    context.close()
                finally:
                    if browser is not None:
                        browser.close()

        return records


def build_people_url(company_slug: str, title: str | None) -> str:
    base = PEOPLE_URL_TEMPLATE.format(slug=company_slug)
    if not title:
        return base
    safe = title.strip().replace('"', "")
    if not safe:
        return base
    return f"{base}?keywords={_urlencode(safe)}"


def company_universal_name(company: CompanyInput) -> str:
    if company.linkedin_url:
        parsed = urlparse(company.linkedin_url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] == "company":
            return parts[1].lower()
    cleaned = re.sub(r"[^a-z0-9-]+", "-", normalize_text(company.company_name)).strip("-")
    return cleaned


def record_to_lead(
    company: CompanyInput,
    record: PlaywrightProfileRecord,
    include_uncertain: bool,
) -> Lead | None:
    headline = record.headline or ""
    title = record.headline or ""
    matched = match_target_title(f"{title} {headline}", company.titles)
    if not include_uncertain and not matched:
        return None

    snippet_parts = [part for part in [headline, record.location] if part]
    lead = Lead(
        company_name=company.company_name,
        company_domain=company.company_domain,
        person_name=record.full_name,
        title=headline or None,
        linkedin_url=record.profile_url,
        source_url=record.profile_url,
        source_type="linkedin_playwright",
        snippet=" | ".join(snippet_parts),
        matched_title=matched,
        confidence_score=30,
    )
    return lead.model_copy(update={"confidence_score": score_lead(lead)})


def _apply_stealth(context: Any) -> None:
    """Light stealth: hide webdriver flag and set plausible navigator props.

    Not a defeat-all evasion. Reduces the cheapest detection signals; serious
    fingerprinting (Iovation, Arkose) will still flag the browser.
    """
    try:
        context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = window.chrome || { runtime: {} };
            Object.defineProperty(navigator, 'languages', {
                get: () => ['pt-BR', 'pt', 'en-US', 'en']
            });
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });
            """
        )
    except Exception:  # pragma: no cover - context may not support init scripts in tests
        pass


def _human_delay(opts: PlaywrightCollectorOptions) -> None:
    low = max(0.0, opts.min_delay_seconds)
    high = max(low, opts.max_delay_seconds)
    if high <= 0:
        return
    time.sleep(random.uniform(low, high))


def _is_blocked(current_url: str) -> bool:
    parsed = urlparse(current_url)
    if parsed.netloc.endswith("linkedin.com") and (
        "/checkpoint/" in parsed.path or "/authwall" in parsed.path
    ):
        return True
    if parsed.netloc.endswith("linkedin.com") and parsed.path.startswith("/login"):
        return True
    return False


def _extract_records_from_page(page: Any) -> list[PlaywrightProfileRecord]:
    """Walk anchor tags pointing to /in/ profiles and harvest visible text."""
    records: list[PlaywrightProfileRecord] = []
    try:
        anchors = page.query_selector_all(PROFILE_LINK_SELECTOR)
    except Exception as exc:
        logger.debug("Falha ao consultar selectors de perfis: %s", exc)
        return records

    for anchor in anchors:
        try:
            href = anchor.get_attribute("href") or ""
        except Exception:
            href = ""
        if not href or "/in/" not in href:
            continue
        profile_url = _canonical_profile_url(href)
        try:
            card = anchor.evaluate(
                "el => {"
                " const card = el.closest('li, article, div[data-test-id], div[class*=member]');"
                " if (!card) return {};"
                " const texts = Array.from(card.querySelectorAll('*')).map(n => (n.innerText || '').trim());"
                " const unique = []; for (const t of texts) { if (t && !unique.includes(t)) unique.push(t); }"
                " return { texts: unique };"
                "}"
            )
        except Exception:
            card = {}
        texts = (card or {}).get("texts") or []
        records.append(
            PlaywrightProfileRecord(
                full_name=_pick_name(texts),
                headline=_pick_headline(texts),
                location=_pick_location(texts),
                profile_url=profile_url,
            )
        )
    return records


def _canonical_profile_url(href: str) -> str:
    if href.startswith("http"):
        return href.split("?")[0].rstrip("/") + "/"
    if href.startswith("/in/"):
        return f"{LINKEDIN_BASE}{href.split('?')[0].rstrip('/')}/"
    return href


def _pick_name(texts: list[str]) -> str | None:
    for text in texts:
        candidate = text.strip()
        if not candidate or len(candidate) > 80:
            continue
        if any(ch.isdigit() for ch in candidate):
            continue
        if "•" in candidate or "·" in candidate:
            continue
        words = candidate.split()
        if 2 <= len(words) <= 5 and all(w[:1].isupper() or not w[:1].isalpha() for w in words):
            return candidate
    return None


def _pick_headline(texts: list[str]) -> str | None:
    for text in texts:
        if any(token in text.lower() for token in ("at ", "na ", "no ", "@")):
            return text.strip()
    return None


def _pick_location(texts: list[str]) -> str | None:
    for text in reversed(texts):
        cleaned = text.strip()
        if not cleaned:
            continue
        if "," in cleaned and len(cleaned) <= 80:
            return cleaned
    return None


def _urlencode(value: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(value)


def _maybe_apply_llm_fallback(
    *,
    page: Any,
    title_records: list[PlaywrightProfileRecord],
    llm_config: Any,
) -> list[PlaywrightProfileRecord] | None:
    """Trigger crawl4ai LLM extraction when heuristic records look incomplete.

    Returns the merged record list (heuristic + LLM-filled) when the fallback
    runs, or ``None`` when the heuristic output already looks healthy.
    """
    from beautiful_linkedin.providers.linkedin_llm_extractor import (
        extract_records_from_html,
        merge_records,
        should_run_llm_fallback,
    )

    if not should_run_llm_fallback(title_records):
        return None

    try:
        html = page.content()
    except Exception as exc:
        logger.info("LLM fallback: falha ao capturar HTML da página: %s", exc)
        return None

    llm_records = extract_records_from_html(html, config=llm_config)
    if not llm_records:
        return None

    logger.info(
        "LLM fallback acionado: heurística devolveu %d records, LLM extraiu %d.",
        len(title_records),
        len(llm_records),
    )
    return merge_records(title_records, llm_records)
