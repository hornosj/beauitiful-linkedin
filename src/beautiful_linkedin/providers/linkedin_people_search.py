"""LinkedIn People-tab search provider — listing-only, no profile visits.

This provider is the safer cousin of ``linkedin_playwright``. Both rely on
the operator's ``li_at`` cookie, both navigate to ``/company/<slug>/people/``,
both filter by job-title via the in-page search bar (encoded in the URL as
``?keywords=<title>``). The difference is the contract: this provider is
deliberately **listing-only**:

- It never opens an individual ``/in/<handle>`` profile page.
- It only reads what the People listing already shows for each card:
  full name, headline (cargo at a empresa) and location.
- It uses a chain of CSS selectors so a class-name shuffle on LinkedIn's
  side doesn't immediately blank the output.

The fetcher is injected. The default backend tries ``Scrapling`` (better
stealth + cookie support) and falls back to Playwright if Scrapling is not
installed. Tests never need either: pass a fake fetcher to drive the
provider end-to-end offline.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote_plus, urlparse

from bs4 import BeautifulSoup, Tag

from beautiful_linkedin.cookie_resolver import resolve_linkedin_li_at_cookie
from beautiful_linkedin.models import CompanyInput, Lead
from beautiful_linkedin.processing.company_size import parse_company_size_from_html
from beautiful_linkedin.processing.lead_extractor import match_target_title
from beautiful_linkedin.processing.normalizer import normalize_text
from beautiful_linkedin.processing.scorer import score_lead
from beautiful_linkedin.providers.lead_provider import LeadProvider

logger = logging.getLogger(__name__)


LINKEDIN_BASE = "https://www.linkedin.com"
PEOPLE_URL_TEMPLATE = LINKEDIN_BASE + "/company/{slug}/people/"
LINKEDIN_FEED_URL = LINKEDIN_BASE + "/feed/"


class LinkedInAuthError(RuntimeError):
    """Raised when the li_at cookie is missing, expired, malformed or rejected.

    The provider catches this exception and skips the remaining titles for the
    company instead of looping through every title and waiting on the same
    redirect storm 30 seconds at a time.
    """


def looks_like_valid_li_at(value: str | None) -> bool:
    """Cheap sanity check on the cookie value.

    A real ``li_at`` is base64url-ish: ASCII printable, mostly alphanumeric
    plus ``-_``, length 60–500. ``browser_cookie3`` on Windows + Chrome 127+
    can hand us raw encrypted bytes that look like a string but contain
    control characters or non-ASCII — those will hit ``ERR_TOO_MANY_REDIRECTS``
    when sent to LinkedIn. Catch them here.
    """
    if not value or not isinstance(value, str):
        return False
    cleaned = value.strip()
    if len(cleaned) < 50 or len(cleaned) > 600:
        return False
    # Must be 100% ASCII printable, no control chars.
    return all(0x20 <= ord(ch) <= 0x7E for ch in cleaned)


def classify_playwright_error(exc: BaseException) -> BaseException:
    """Map a Playwright/network exception into a more actionable error.

    The user-facing problem behind ``ERR_TOO_MANY_REDIRECTS`` on
    ``/company/<slug>/people/`` is almost always an invalid or expired
    ``li_at``. Surface that explicitly so the provider stops looping.
    """
    msg = str(exc)
    if "ERR_TOO_MANY_REDIRECTS" in msg or "too many redirects" in msg.lower():
        return LinkedInAuthError(
            "li_at parece expirado ou inválido — LinkedIn redirecionou em loop. "
            "Faça login no navegador e refaça a detecção, ou cole um li_at fresco "
            "em Conta > Cookie li_at."
        )
    return exc


@dataclass(frozen=True)
class PeopleCard:
    full_name: str | None
    headline: str | None
    location: str | None
    profile_url: str


@dataclass
class PeopleSearchOptions:
    scrolls: int = 6
    min_delay_seconds: float = 2.0
    max_delay_seconds: float = 4.5
    headless: bool = True
    user_data_dir: str | None = None
    extra_browser_args: list[str] = field(default_factory=list)
    # CDP: connect to a Chrome the user already has open with
    # --remote-debugging-port. When this works, the provider reuses the user's
    # existing logged-in session and never injects a new li_at — that's the
    # only reliable way to avoid getting them logged out of LinkedIn.
    cdp_endpoint: str = "http://127.0.0.1:9222"
    cdp_enabled: bool = True
    # Approximate number of new cards rendered by each "Exibir mais resultados"
    # click. Used to derive how many clicks the load-more loop needs to satisfy
    # ``max_results``. LinkedIn renders 12 cards per page by default on the
    # People tab; users see less depending on the viewport, so 10 is a safe
    # middle ground.
    cards_per_cycle: int = 10
    # Hard cap on derived iterations, irrespective of max_results. Prevents an
    # accidental max_results=500 from clicking the button 50 times in a row.
    max_scrolls_cap: int = 50


class PeopleListFetcher(Protocol):
    """Anything that can render a People-tab listing into raw HTML."""

    needs_li_at: bool

    def fetch_listing(self, url: str, *, li_at: str, scrolls: int) -> str:
        ...


def _http_get(url: str, timeout: float):
    """Indirection so tests can monkeypatch the HTTP probe."""
    import httpx

    return httpx.get(url, timeout=timeout)


def probe_cdp_endpoint(endpoint: str, *, timeout: float = 1.5) -> bool:
    """Return True if a Chrome (or Edge) DevTools Protocol endpoint is alive.

    Hits ``<endpoint>/json/version``. The endpoint is the value passed to
    ``--remote-debugging-port`` on Chrome's command line, exposed at
    ``http://127.0.0.1:<port>`` by default.
    """
    if not endpoint:
        return False
    try:
        response = _http_get(f"{endpoint.rstrip('/')}/json/version", timeout=timeout)
    except Exception:
        return False
    status = getattr(response, "status_code", None)
    return status == 200


class LinkedInPeopleSearchProvider(LeadProvider):
    """Logged-in but profile-free scraper of the LinkedIn People listing."""

    name = "linkedin_people_search"

    def __init__(
        self,
        cookie: str | None = None,
        cookie_browser: str = "auto",
        fetcher: PeopleListFetcher | None = None,
        options: PeopleSearchOptions | None = None,
    ) -> None:
        self.cookie = cookie
        self.cookie_browser = cookie_browser
        self.options = options or PeopleSearchOptions()
        self._fetcher = fetcher

    def _effective_scrolls(self, max_results: int) -> int:
        """How many load-more iterations the fetcher should run.

        ``options.scrolls == 0`` is honoured as an explicit "disable expansion"
        (used by unit tests). Otherwise the count is derived from how many
        cards are typically rendered per click — see
        :attr:`PeopleSearchOptions.cards_per_cycle`. We add a small buffer to
        compensate for cards that get filtered out locally, then cap with
        ``max_scrolls_cap`` so a runaway ``max_results`` can't trigger a
        suspicious-looking 100-click burst.
        """
        import math

        if self.options.scrolls == 0:
            return 0
        cards = max(1, int(self.options.cards_per_cycle or 1))
        wanted = max(1, int(max_results))
        derived = math.ceil(wanted / cards) + 1
        cap = max(1, int(self.options.max_scrolls_cap or 1))
        return min(derived, cap)

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

        slug = _company_slug(company)
        if not slug:
            logger.warning(
                "linkedin_people_search: não consegui resolver o slug do LinkedIn para %s.",
                company.company_name,
            )
            return []

        fetcher = self._fetcher or _default_fetcher(self.options)
        if fetcher is None:
            return []

        # CDP-style fetchers reuse the user's logged-in Chrome and don't need
        # an out-of-band li_at. Avoid touching the cookie store at all in that
        # case — reading it can both fail (ABE) and isn't required.
        needs_li_at = bool(getattr(fetcher, "needs_li_at", True))
        li_at = ""
        if needs_li_at:
            li_at = resolve_linkedin_li_at_cookie(
                self.cookie, browser=self.cookie_browser
            ) or ""
            if not li_at:
                # cookie_resolver já emitiu warning detalhado.
                return []
            if not looks_like_valid_li_at(li_at):
                logger.warning(
                    "linkedin_people_search: o valor recuperado para li_at não tem o formato "
                    "esperado (provável cookie criptografado retornado por browser_cookie3 em "
                    "Chrome 127+/Windows). Cole o li_at manualmente em Conta > Cookie li_at, "
                    "ou inicie o Chrome com --remote-debugging-port=9222 para usar a sessão "
                    "ativa via CDP (não desloga)."
                )
                return []

        seen: set[str] = set()
        leads: list[Lead] = []
        title_terms = [t for t in (company.titles or []) if t and t.strip()]

        # One request per company: LinkedIn renders each comma-separated
        # keyword as its own filter chip. This lets us click "Exibir mais
        # resultados" against the union of all wanted roles instead of doing N
        # separate paginations.
        url = build_people_search_url(slug, title_terms if title_terms else None)
        scrolls = self._effective_scrolls(max_results)
        try:
            html = fetcher.fetch_listing(url, li_at=li_at, scrolls=scrolls)
        except LinkedInAuthError as exc:
            logger.warning("linkedin_people_search: %s", exc)
            return []
        except Exception as exc:
            classified = classify_playwright_error(exc)
            if isinstance(classified, LinkedInAuthError):
                logger.warning("linkedin_people_search: %s", classified)
                return []
            logger.warning(
                "linkedin_people_search: falha ao buscar %s: %s.", url, exc
            )
            return []

        requested_title_label = ", ".join(title_terms)
        cards = extract_cards_from_html(html or "")
        for card in cards:
            if card.profile_url in seen:
                continue
            seen.add(card.profile_url)
            lead = _card_to_lead(company, card, include_uncertain, requested_title_label)
            if lead is not None:
                leads.append(lead)
            if len(leads) >= max_results:
                break

        logger.info(
            "linkedin_people_search: %d cards únicos para %s, %d viraram leads.",
            len(seen),
            company.company_name,
            len(leads),
        )
        return leads[:max_results]


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def build_people_search_url(
    company_slug: str, titles: str | list[str] | None
) -> str:
    """Build a LinkedIn /company/<slug>/people/ URL with an optional keyword filter.

    Passing a list joins the entries with commas so LinkedIn renders each one
    as its own filter chip (e.g. ``rh,people,talent`` → three chips). Passing
    a single string preserves the old behaviour.
    """
    base = PEOPLE_URL_TEMPLATE.format(slug=company_slug)
    if titles is None:
        return base
    if isinstance(titles, str):
        candidates = [titles]
    else:
        candidates = list(titles)
    cleaned = [t.strip().replace('"', "") for t in candidates]
    cleaned = [t for t in cleaned if t]
    if not cleaned:
        return base
    # LinkedIn splits ``keywords=`` on commas (no spaces) into chips.
    joined = ",".join(cleaned)
    return f"{base}?keywords={quote_plus(joined)}"


def _company_slug(company: CompanyInput) -> str:
    if company.linkedin_url:
        parsed = urlparse(company.linkedin_url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] == "company":
            return parts[1].lower()
    cleaned = re.sub(r"[^a-z0-9-]+", "-", normalize_text(company.company_name)).strip("-")
    return cleaned


def _canonical_profile_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        href = f"https:{href}"
    if href.startswith("/in/"):
        href = f"{LINKEDIN_BASE}{href}"
    if not href.startswith("http"):
        return ""
    parsed = urlparse(href)
    if "/in/" not in parsed.path:
        return ""
    path = parsed.path.split("?", 1)[0].rstrip("/") + "/"
    netloc = parsed.netloc or "www.linkedin.com"
    return f"{parsed.scheme or 'https'}://{netloc}{path}"


# ---------------------------------------------------------------------------
# HTML extraction
# ---------------------------------------------------------------------------


_CARD_SELECTORS: tuple[str, ...] = (
    "li.org-people-profile-card__profile-card-spacing",
    "li[class*=org-people-profile-card]",
    "div[class*=entity-result__item]",
    "div[class*=org-people-profile-card]",
)

_NAME_SELECTORS: tuple[str, ...] = (
    ".artdeco-entity-lockup__title",
    ".org-people-profile-card__profile-info .artdeco-entity-lockup__title",
    ".entity-result__title-text",
    ".entity-result__title-text a",
)

_HEADLINE_SELECTORS: tuple[str, ...] = (
    ".artdeco-entity-lockup__subtitle",
    ".entity-result__primary-subtitle",
    ".org-people-profile-card__profile-position",
)

_LOCATION_SELECTORS: tuple[str, ...] = (
    ".artdeco-entity-lockup__caption",
    ".entity-result__secondary-subtitle",
    ".org-people-profile-card__profile-location",
)


def extract_cards_from_html(html: str) -> list[PeopleCard]:
    """Extract people-listing cards from the LinkedIn People-tab HTML.

    The function walks a chain of CSS selectors (most specific first). The
    first selector that returns at least one card whose anchor points at
    ``/in/`` wins. Cards are deduplicated by canonical profile URL.
    """
    if not html or "<" not in html:
        return []

    soup = BeautifulSoup(html, "lxml")
    candidate_blocks: list[Tag] = []
    for selector in _CARD_SELECTORS:
        blocks = [block for block in soup.select(selector) if isinstance(block, Tag)]
        if any(_first_profile_anchor(block) for block in blocks):
            candidate_blocks = blocks
            break

    if not candidate_blocks:
        # Wide net fallback: every anchor that points at /in/.
        candidate_blocks = []
        for anchor in soup.select("a[href*='/in/']"):
            container = _closest_card_container(anchor)
            if container is not None:
                candidate_blocks.append(container)

    seen: set[str] = set()
    cards: list[PeopleCard] = []
    for block in candidate_blocks:
        anchor = _first_profile_anchor(block)
        if anchor is None:
            continue
        profile_url = _canonical_profile_url(str(anchor.get("href") or ""))
        if not profile_url or profile_url in seen:
            continue
        seen.add(profile_url)
        cards.append(
            PeopleCard(
                full_name=_first_text(block, _NAME_SELECTORS) or _anchor_inner_text(anchor),
                headline=_first_text(block, _HEADLINE_SELECTORS),
                location=_first_text(block, _LOCATION_SELECTORS),
                profile_url=profile_url,
            )
        )
    return cards


def _first_profile_anchor(block: Tag) -> Tag | None:
    for anchor in block.select("a[href*='/in/']"):
        href = str(anchor.get("href") or "")
        if "/in/" in href and "/company/" not in href:
            return anchor
    return None


def _closest_card_container(anchor: Tag) -> Tag | None:
    parent = anchor.parent
    while parent is not None and isinstance(parent, Tag):
        if parent.name in {"li", "article"} or "card" in " ".join(
            parent.get("class") or []
        ):
            return parent
        parent = parent.parent
    return anchor.parent if isinstance(anchor.parent, Tag) else None


def _first_text(block: Tag, selectors: tuple[str, ...]) -> str | None:
    for selector in selectors:
        node = block.select_one(selector)
        if node is None:
            continue
        text = node.get_text(" ", strip=True)
        if text:
            return re.sub(r"\s+", " ", text).strip()
    return None


def _anchor_inner_text(anchor: Tag) -> str | None:
    text = anchor.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip() or None


# ---------------------------------------------------------------------------
# Card → Lead
# ---------------------------------------------------------------------------


def _card_to_lead(
    company: CompanyInput,
    card: PeopleCard,
    include_uncertain: bool,
    requested_title: str,
) -> Lead | None:
    headline = card.headline or ""
    # Match strictly against the lead's real headline — never against the
    # requested keyword. Including the requested keyword as evidence creates
    # false positives: a "Backend Engineer" headline would "match" a
    # 'marketing' search just because we appended the requested term to the
    # haystack. The user's spec is explicit about this.
    if not company.titles:
        # General-search mode: no keyword constraint, keep every card.
        matched: str | None = None
    else:
        matched = match_target_title(headline, company.titles)
        if not include_uncertain and not matched:
            return None

    snippet_parts = [part for part in [headline, card.location] if part]
    lead = Lead(
        company_name=company.company_name,
        company_domain=company.company_domain,
        person_name=card.full_name,
        title=headline or None,
        linkedin_url=card.profile_url,
        source_url=card.profile_url,
        source_type="linkedin_people_search",
        snippet=" | ".join(snippet_parts),
        matched_title=matched,
        confidence_score=35,
    )
    lead.confidence_score = score_lead(lead)
    return lead


# ---------------------------------------------------------------------------
# Default fetcher: Scrapling first, Playwright fallback
# ---------------------------------------------------------------------------


def _default_fetcher(options: PeopleSearchOptions) -> PeopleListFetcher | None:
    # 1) Best path: connect to the user's already-running Chrome via CDP.
    #    Same fingerprint, same cookies in active use → no logout.
    if (
        getattr(options, "cdp_enabled", True)
        and getattr(options, "cdp_endpoint", "")
        and probe_cdp_endpoint(options.cdp_endpoint)
    ):
        logger.info(
            "linkedin_people_search: usando CDP em %s (Chrome aberto). "
            "Você não será deslogado.",
            options.cdp_endpoint,
        )
        return CDPPeopleFetcher(options.cdp_endpoint, options)

    # 2) Stealthy out-of-process fetcher.
    fetcher = _try_build_scrapling_fetcher(options)
    if fetcher is not None:
        return fetcher

    # 3) Last resort: vanilla Playwright with the resolver's li_at. May log
    #    the user out of their main browser; warn loudly.
    if getattr(options, "cdp_enabled", True):
        logger.warning(
            "linkedin_people_search: Chrome com --remote-debugging-port=%s não está rodando. "
            "Caindo para Playwright com li_at — esse caminho pode deslogar você do LinkedIn. "
            "Para evitar, inicie o Chrome com --remote-debugging-port=9222.",
            options.cdp_endpoint,
        )
    return _try_build_playwright_fetcher(options)


def _try_build_scrapling_fetcher(
    options: PeopleSearchOptions,
) -> PeopleListFetcher | None:
    try:
        from scrapling.fetchers import StealthyFetcher  # type: ignore[import-not-found]
    except Exception as exc:
        logger.info(
            "linkedin_people_search: Scrapling indisponível (%s). "
            "Usando Playwright como fallback.",
            exc,
        )
        return None

    return _ScraplingPeopleFetcher(StealthyFetcher, options)


def _try_build_playwright_fetcher(
    options: PeopleSearchOptions,
) -> PeopleListFetcher | None:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]  # noqa: F401
    except Exception as exc:
        logger.warning(
            "linkedin_people_search: nem Scrapling nem Playwright instalados (%s). "
            "Instale com `pip install beautiful-linkedin[risky]` ou Scrapling.",
            exc,
        )
        return None
    return _PlaywrightPeopleFetcher(options)


class _ScraplingPeopleFetcher:
    needs_li_at = True

    def __init__(self, stealthy_fetcher: Any, options: PeopleSearchOptions) -> None:
        self._stealthy_fetcher = stealthy_fetcher
        self._options = options

    def fetch_listing(self, url: str, *, li_at: str, scrolls: int) -> str:
        cookies = [
            {
                "name": "li_at",
                "value": li_at,
                "domain": ".linkedin.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
            }
        ]
        try:
            page = self._stealthy_fetcher.fetch(  # type: ignore[attr-defined]
                url,
                headless=self._options.headless,
                network_idle=True,
                cookies=cookies,
                google_search=False,
                wait=2_000,
                scroll_into_view="footer" if scrolls else None,
            )
        except TypeError:
            # Older Scrapling signatures may differ; fall back to .get.
            page = self._stealthy_fetcher.get(url, cookies=cookies)
        html = getattr(page, "html_content", None) or getattr(page, "content", None) or ""
        if callable(html):
            html = html()
        return str(html)


class CDPPeopleFetcher:
    """Connect to the user's already-running Chrome via remote debugging.

    Why this exists: every other approach reuses the user's ``li_at`` cookie
    in a *separate* browser fingerprint. LinkedIn detects the divergence and
    invalidates the original session, logging the user out of their daily
    Chrome window. By connecting to the exact Chrome the user is already
    logged into, we add zero new sessions and zero new cookies. The browser
    sees one session, our work is invisible.

    Requirements: Chrome must have been launched with
    ``--remote-debugging-port=9222`` (or whatever endpoint is configured).
    """

    needs_li_at = False

    def __init__(
        self,
        endpoint: str,
        options: PeopleSearchOptions,
        connect: Callable[[str], Any] | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._options = options
        self._connect = connect

    def fetch_listing(self, url: str, *, li_at: str, scrolls: int) -> str:
        connect = self._connect or self._default_connect
        try:
            browser = connect(self._endpoint)
        except Exception as exc:
            raise LinkedInAuthError(
                f"Não consegui conectar ao Chrome via CDP em {self._endpoint}. "
                "Inicie o Chrome com --remote-debugging-port=9222."
            ) from exc

        contexts = list(getattr(browser, "contexts", []) or [])
        if not contexts:
            raise LinkedInAuthError(
                "Chrome conectado mas sem contextos abertos. Abra uma janela e tente de novo."
            )
        context = contexts[0]
        page = context.new_page()
        try:
            try:
                page.goto(url, wait_until="domcontentloaded")
            except Exception as exc:
                raise classify_playwright_error(exc) from exc

            final_url = getattr(page, "url", "") or ""
            if any(
                marker in final_url
                for marker in ("/login", "/authwall", "/checkpoint", "/uas/login")
            ):
                raise LinkedInAuthError(
                    f"Você não está logado no LinkedIn neste Chrome (URL final: {final_url}). "
                    "Faça login na sua janela do Chrome e tente de novo."
                )

            _human_delay(self._options)
            _expand_results(page, self._options, max_iterations=max(0, scrolls))
            return page.content()
        finally:
            try:
                page.close()
            except Exception:
                pass

    def _default_connect(self, endpoint: str) -> Any:
        from playwright.sync_api import sync_playwright

        # Note: we deliberately do NOT use ``with sync_playwright()`` here — the
        # caller is fetch_listing which is short-lived and we must not close
        # the user's Chrome at the end. We open the playwright runtime, take
        # a connection, and let the runtime live until the process exits.
        runtime = sync_playwright().start()
        return runtime.chromium.connect_over_cdp(endpoint)


class _PlaywrightPeopleFetcher:
    needs_li_at = True

    def __init__(self, options: PeopleSearchOptions) -> None:
        self._options = options

    def fetch_listing(self, url: str, *, li_at: str, scrolls: int) -> str:
        from playwright.sync_api import sync_playwright

        opts = self._options
        with sync_playwright() as pw:
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                *opts.extra_browser_args,
            ]
            browser = pw.chromium.launch(headless=opts.headless, args=launch_args)
            try:
                context = browser.new_context(
                    viewport={"width": 1366, "height": 800},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                )
                context.add_cookies(
                    [
                        {
                            "name": "li_at",
                            "value": li_at,
                            "url": LINKEDIN_BASE,
                        }
                    ]
                )
                page = context.new_page()

                _verify_session_or_raise(page, opts)

                try:
                    page.goto(url, wait_until="domcontentloaded")
                except Exception as exc:
                    raise classify_playwright_error(exc) from exc
                if "/login" in page.url or "/authwall" in page.url:
                    raise LinkedInAuthError(
                        "LinkedIn redirecionou para /login ao abrir a aba People. "
                        "li_at provavelmente expirou."
                    )
                _human_delay(opts)
                _expand_results(page, opts, max_iterations=max(0, scrolls))
                return page.content()
            finally:
                browser.close()


def _verify_session_or_raise(page: Any, opts: PeopleSearchOptions) -> None:
    """Pre-flight: hit /feed/ once to confirm li_at is accepted.

    If LinkedIn bounces us to /login or to a checkpoint URL, raise
    :class:`LinkedInAuthError` immediately so we don't spend 30 s + 30 s + 30 s
    looping on each title only to find out the cookie was bad all along.
    """
    try:
        page.goto(LINKEDIN_FEED_URL, wait_until="domcontentloaded")
    except Exception as exc:
        raise classify_playwright_error(exc) from exc

    final_url = page.url or ""
    blocked_markers = ("/login", "/authwall", "/checkpoint", "/uas/login")
    if any(marker in final_url for marker in blocked_markers):
        raise LinkedInAuthError(
            f"li_at não foi aceito pelo LinkedIn (redirecionou para {final_url}). "
            "Faça login no navegador e refaça a detecção, ou cole um li_at fresco."
        )
    _human_delay(opts)


def _human_delay(opts: PeopleSearchOptions) -> None:
    import random

    low = max(0.0, opts.min_delay_seconds)
    high = max(low, opts.max_delay_seconds)
    if high <= 0:
        return
    time.sleep(random.uniform(low, high))


# ---------------------------------------------------------------------------
# Load-more automation
# ---------------------------------------------------------------------------

_LOAD_MORE_SELECTORS: tuple[str, ...] = (
    "button.scaffold-finite-scroll__load-button",
    "button[aria-label*='Exibir mais resultados' i]",
    "button[aria-label*='Show more results' i]",
    "button:has-text('Exibir mais resultados')",
    "button:has-text('Show more results')",
)

_CARD_COUNT_SELECTOR = (
    "li.org-people-profile-card__profile-card-spacing, "
    "li[class*='org-people-profile-card'], "
    "div[class*='org-people-profile-card']"
)


def _expand_results(page: Any, opts: PeopleSearchOptions, *, max_iterations: int) -> None:
    """Click 'Exibir mais resultados' (or scroll) until the list stops growing.

    Behaviour is intentionally noisy on the timing axis: we hover before
    clicking, randomize wait windows between actions, and occasionally scroll
    the wheel before pressing the button. LinkedIn watches for robotic
    pacing — a fully deterministic loop is the easiest signal to flag.
    """
    import random

    if max_iterations <= 0:
        return

    consecutive_no_growth = 0
    for _ in range(max_iterations):
        before = _count_cards(page)
        # Sometimes the load-more button is below the viewport and needs a
        # scroll first to materialize in the DOM.
        try:
            page.mouse.wheel(0, random.randint(900, 1700))
        except Exception:
            pass
        time.sleep(random.uniform(0.4, 1.1))

        button = _find_load_more_button(page)
        if button is None:
            # No button visible: either the page is still loading more on
            # scroll, or we hit the end of the list. Give the lazy loader a
            # moment, then re-check.
            _human_delay(opts)
            after = _count_cards(page)
            if after > before:
                consecutive_no_growth = 0
                continue
            consecutive_no_growth += 1
            if consecutive_no_growth >= 2:
                break
            continue

        try:
            button.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        time.sleep(random.uniform(0.3, 0.9))
        try:
            button.hover(timeout=2000)
        except Exception:
            # Hover isn't strictly required; press on without it.
            pass
        time.sleep(random.uniform(0.15, 0.55))
        clicked = False
        try:
            button.click(timeout=3000)
            clicked = True
        except Exception:
            # Common cause: an overlay intercepted the click. Force it via JS.
            try:
                button.evaluate("(el) => el.click()")
                clicked = True
            except Exception:
                pass
        if not clicked:
            break

        _wait_for_card_growth(page, before, timeout_seconds=6.5)
        _human_delay(opts)
        after = _count_cards(page)
        if after <= before:
            consecutive_no_growth += 1
            if consecutive_no_growth >= 2:
                break
        else:
            consecutive_no_growth = 0


def _find_load_more_button(page: Any) -> Any | None:
    for selector in _LOAD_MORE_SELECTORS:
        try:
            locator = page.locator(selector).first
            if locator.count() == 0:
                continue
            if not locator.is_visible():
                continue
            return locator
        except Exception:
            continue
    return None


def _count_cards(page: Any) -> int:
    try:
        return page.locator(_CARD_COUNT_SELECTOR).count()
    except Exception:
        return 0


def _wait_for_card_growth(page: Any, baseline: int, *, timeout_seconds: float) -> None:
    deadline = time.time() + max(0.0, timeout_seconds)
    while time.time() < deadline:
        if _count_cards(page) > baseline:
            return
        time.sleep(0.25)


_LOGIN_MARKERS = ("/login", "/authwall", "/checkpoint", "/uas/login", "/signup")


def resolve_company_size_via_page(
    *,
    company_name: str,
    linkedin_url: str | None,
    company_domain: str | None,
    emit: Callable[[str, dict[str, Any]], None],
    page_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Navigate to ``/company/<slug>/people/`` and extract size signals.

    ``page_factory`` returns a Playwright-style page object (anything with
    ``goto``, ``content`` and ``close``). Tests pass a fake; production
    wires it to a CDP-connected Chrome context.

    Emits step events via ``emit`` so the caller can stream progress to
    the UI: ``recognizing`` → ``recognized`` → ``fetching`` →
    ``employees_seen`` (or ``login_required``).
    """
    emit("recognizing", {"company_name": company_name})
    slug = _company_slug(
        CompanyInput(
            company_name=company_name,
            company_domain=company_domain,
            linkedin_url=linkedin_url,
            titles=["*"],
        )
    )
    emit("recognized", {"slug": slug})

    url = build_people_search_url(slug, None)
    emit("fetching", {"url": url})

    page = page_factory()
    try:
        try:
            page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            emit("error", {"message": f"navigation failed: {exc}"})
            return {}
        final_url = getattr(page, "url", "") or ""
        if any(marker in final_url for marker in _LOGIN_MARKERS):
            emit("login_required", {"final_url": final_url})
            return {}
        html = page.content()
    finally:
        try:
            page.close()
        except Exception:
            pass

    signals = parse_company_size_from_html(html)
    if signals.get("employee_count") is not None:
        emit(
            "employees_seen",
            {
                "count": signals["employee_count"],
                "raw_text": signals.get("employee_count_text"),
            },
        )
    elif signals.get("visible_card_count"):
        emit(
            "cards_seen",
            {"count": signals["visible_card_count"]},
        )
    else:
        emit("no_signal", {})
    return signals


# Late import of Callable to keep top-level imports tidy.
from typing import Callable  # noqa: E402
