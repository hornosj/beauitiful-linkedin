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
from beautiful_linkedin.processing.deduplicator import global_dedupe_key
from beautiful_linkedin.processing.lead_extractor import (
    NO_RELATED_KEYWORDS_NOTE,
    match_target_title,
)
from beautiful_linkedin.processing.normalizer import normalize_text
from beautiful_linkedin.processing.scorer import score_lead
from beautiful_linkedin.processing.title_validator import validate_lead_titles
from beautiful_linkedin.providers.lead_provider import LeadProvider
from beautiful_linkedin.providers.linkedin_people_search_progress import (
    PeopleScrapeProgress,
    ProgressStore,
)

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
    # People tab; users see less depending on the viewport, so 8 is a safe
    # middle ground.
    cards_per_cycle: int = 8
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
        progress_store: ProgressStore | None = None,
        exclude_lead_keys: set[str] | None = None,
        on_lead_found: "Callable[[Lead], None] | None" = None,
    ) -> None:
        self.cookie = cookie
        self.cookie_browser = cookie_browser
        self.options = options or PeopleSearchOptions()
        self._fetcher = fetcher
        self._progress_store = progress_store
        # Cross-table dedup: global identity keys of leads already saved. Cards
        # whose key is in this set are ignored (not counted toward
        # ``max_results``) so the loop keeps clicking until it fills the target
        # with fresh leads. Injected by the runner after construction.
        self.exclude_lead_keys: set[str] = set(exclude_lead_keys or set())
        # Real-time feedback: invoked with each accepted lead the moment it is
        # extracted during scrolling, so the UI can show progress live.
        self.on_lead_found: "Callable[[Lead], None] | None" = on_lead_found

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
        # When a global-exclusion set is active, a large fraction of the cards
        # we scroll past may already be saved. Dig deeper (roughly double the
        # budget) so the loop can still reach ``max_results`` fresh leads
        # before the cap. The cap stays the hard ceiling either way.
        if self.exclude_lead_keys:
            derived = derived * 2 + 1
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

        diagnostic = self._new_diagnostic(company)

        slug = _company_slug(company)
        if not slug:
            message = (
                f"Não consegui resolver o slug do LinkedIn para {company.company_name}."
            )
            logger.warning("linkedin_people_search: %s", message)
            diagnostic.last_error = message
            return []

        try:
            fetcher = self._fetcher or _default_fetcher(self.options)
        except LinkedInAuthError as exc:
            logger.warning("linkedin_people_search: %s", exc)
            diagnostic.last_error = str(exc)
            return []
        if fetcher is None:
            diagnostic.last_error = (
                "Backend Playwright indisponível — confirme se o Chromium do app "
                "está rodando e se o sidecar foi empacotado com Playwright."
            )
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

        title_terms = [t for t in (company.titles or []) if t and t.strip()]
        if include_uncertain and title_terms:
            logger.warning(
                "linkedin_people_search: include_uncertain=True — validador estrito "
                "DESABILITADO para esta busca (%s). Você pode receber cargos não "
                "relacionados a %s.",
                company.company_name,
                title_terms,
            )
        url = build_people_search_url(slug, title_terms if title_terms else None)
        scrolls = self._effective_scrolls(max_results)

        progress = self._restore_or_create_progress(slug, title_terms)
        leads = self._drive_iterative_scrape(
            company=company,
            fetcher=fetcher,
            url=url,
            li_at=li_at,
            max_clicks=scrolls,
            max_results=max_results,
            include_uncertain=include_uncertain,
            title_terms=title_terms,
            progress=progress,
        )
        self._persist_progress(slug, title_terms, progress)

        logger.info(
            "linkedin_people_search: %d cards vistos para %s, %d viraram leads "
            "(rejeitados conhecidos: %d, cliques: %d).",
            progress.cards_seen_total,
            company.company_name,
            len(leads),
            len(progress.rejected_urls),
            progress.clicks_performed,
        )
        if title_terms and not include_uncertain:
            return leads
        return leads[:max_results]

    # ------------------------------------------------------------------
    # Iterative drive: click → extract → validate → maybe click again
    # ------------------------------------------------------------------

    def _restore_or_create_progress(
        self, slug: str, title_terms: list[str]
    ) -> PeopleScrapeProgress:
        if self._progress_store is None:
            return PeopleScrapeProgress()
        existing = self._progress_store.load(company_slug=slug, titles=title_terms)
        if existing is None:
            return PeopleScrapeProgress()
        # We keep ``rejected_urls``/``accepted_urls`` from the last run so a
        # re-fetch on the same query short-circuits the validator. We reset
        # the per-run counters (``clicks_performed`` / ``cards_seen_total``)
        # because the new browser session starts fresh.
        existing.clicks_performed = 0
        existing.cards_seen_total = 0
        return existing

    def _persist_progress(
        self, slug: str, title_terms: list[str], progress: PeopleScrapeProgress
    ) -> None:
        if self._progress_store is None:
            return
        try:
            self._progress_store.save(
                company_slug=slug, titles=title_terms, progress=progress
            )
        except Exception as exc:  # pragma: no cover - cache best-effort
            logger.warning("linkedin_people_search: falha ao persistir progresso: %s", exc)

    def _drive_iterative_scrape(
        self,
        *,
        company: CompanyInput,
        fetcher: Any,
        url: str,
        li_at: str,
        max_clicks: int,
        max_results: int,
        include_uncertain: bool,
        title_terms: list[str],
        progress: PeopleScrapeProgress,
    ) -> list[Lead]:
        requested_title_label = ", ".join(title_terms)
        accepted_leads: list[Lead] = []
        seen_urls: set[str] = set()
        target_matched_count = 0

        def handle_step(click_index: int, html: str) -> bool:
            """Process the listing after ``click_index`` clicks have happened.

            Returns True to ask the fetcher to keep clicking, False to stop.
            """
            nonlocal target_matched_count
            if click_index > 0:
                progress.note_click()

            cards = extract_cards_from_html(html or "")
            progress.cards_seen_total = max(progress.cards_seen_total, len(cards))

            for position_zero, card in enumerate(cards):
                if card.profile_url in seen_urls:
                    continue
                seen_urls.add(card.profile_url)

                if card.profile_url in progress.accepted_urls:
                    # Already accepted on a previous run — rebuild the lead so
                    # the caller still gets it, but don't re-validate.
                    lead = _card_to_lead(
                        company, card, True, requested_title_label
                    )
                    if lead is not None:
                        if self._is_globally_known(lead):
                            progress.record_rejection(card.profile_url)
                            continue
                        if (
                            title_terms
                            and not include_uncertain
                            and not lead.matched_title
                        ):
                            _mark_without_related_keywords(lead)
                        if _counts_toward_people_search_target(
                            lead, title_terms, include_uncertain
                        ):
                            target_matched_count += 1
                        _annotate_lead(lead, progress, card.profile_url, position_zero + 1)
                        accepted_leads.append(lead)
                        self._emit_lead_found(lead)
                        if target_matched_count >= max_results:
                            return False
                    continue

                # We always pass include_uncertain=True to _card_to_lead so
                # the soft (substring/fuzzy) matcher inside it does not drop
                # leads before our strict validator gets a chance. The
                # validator below uses the wider alias map and is the single
                # source of truth for "does the title match the search?".
                lead = _card_to_lead(
                    company, card, True, requested_title_label
                )
                if lead is None:
                    progress.record_rejection(card.profile_url)
                    continue

                # Global cross-table dedup: drop leads already saved elsewhere
                # without counting them toward ``max_results`` — the loop then
                # keeps clicking to find genuinely new people.
                if self._is_globally_known(lead):
                    progress.record_rejection(card.profile_url)
                    continue

                # Strict post-extraction validation against requested titles.
                # When the user did not provide titles (general search) we
                # accept everything. When the user opted into
                # ``include_uncertain``, we skip the validator entirely so
                # the legacy behaviour is preserved.
                if title_terms and not include_uncertain:
                    outcome = validate_lead_titles(
                        [lead], title_terms, strict=True
                    )
                    if not outcome.valid:
                        _mark_without_related_keywords(lead)
                    else:
                        target_matched_count += 1
                else:
                    target_matched_count += 1

                progress.record_acceptance(
                    card.profile_url, position=position_zero + 1
                )
                _annotate_lead(lead, progress, card.profile_url, position_zero + 1)
                accepted_leads.append(lead)
                self._emit_lead_found(lead)
                if target_matched_count >= max_results:
                    return False  # tell fetcher to stop clicking

            # Not enough valid leads yet — keep clicking.
            return True

        # Prefer the iterative fetcher API if the fetcher exposes it. Wrap the
        # call with a wall-clock timer so the operator can see in the log
        # whether the time was spent inside the fetcher (CDP / Playwright)
        # versus our processing pipeline.
        fetcher_label = type(fetcher).__name__
        scrape_started = time.monotonic()
        logger.info(
            "linkedin_people_search: iniciando scrape via %s (url=%s, max_clicks=%d)",
            fetcher_label, url, max_clicks,
        )
        try:
            if hasattr(fetcher, "fetch_listing_iterative"):
                fetcher.fetch_listing_iterative(
                    url,
                    li_at=li_at,
                    max_clicks=max_clicks,
                    on_step=handle_step,
                )
            else:
                # Legacy one-shot fallback: fetch with the derived click budget
                # and run a single validation pass.
                html = fetcher.fetch_listing(url, li_at=li_at, scrolls=max_clicks)
                handle_step(0, html)
        except LinkedInAuthError as exc:
            elapsed = time.monotonic() - scrape_started
            logger.warning(
                "linkedin_people_search: %s (após %.1fs no fetcher %s)",
                exc, elapsed, fetcher_label,
            )
            if self.last_diagnostic is not None:
                self.last_diagnostic.last_error = str(exc)
            return []
        except Exception as exc:
            elapsed = time.monotonic() - scrape_started
            classified = classify_playwright_error(exc)
            if isinstance(classified, LinkedInAuthError):
                logger.warning(
                    "linkedin_people_search: %s (após %.1fs no fetcher %s)",
                    classified, elapsed, fetcher_label,
                )
                if self.last_diagnostic is not None:
                    self.last_diagnostic.last_error = str(classified)
                return []
            logger.warning(
                "linkedin_people_search: falha ao buscar %s: %s (após %.1fs no fetcher %s).",
                url, exc, elapsed, fetcher_label,
            )
            if self.last_diagnostic is not None:
                self.last_diagnostic.last_error = f"{type(exc).__name__}: {exc}"
            return []

        elapsed = time.monotonic() - scrape_started
        logger.info(
            "linkedin_people_search: scrape finalizado em %.1fs (%s) — %d leads aceitos, %d cliques.",
            elapsed, fetcher_label, len(accepted_leads), progress.clicks_performed,
        )
        return accepted_leads

    # ------------------------------------------------------------------
    # Global dedup + live feedback helpers
    # ------------------------------------------------------------------

    def _is_globally_known(self, lead: Lead) -> bool:
        """True when this lead was already saved in some other table."""
        if not self.exclude_lead_keys:
            return False
        key = global_dedupe_key(lead)
        return key is not None and key in self.exclude_lead_keys

    def _emit_lead_found(self, lead: Lead) -> None:
        """Push a freshly accepted lead to the live-feedback sink, if any."""
        if self.on_lead_found is None:
            return
        try:
            self.on_lead_found(lead)
        except Exception as exc:  # pragma: no cover - sink best-effort
            logger.debug("linkedin_people_search: on_lead_found falhou: %s", exc)


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


def _annotate_lead(
    lead: Lead,
    progress: PeopleScrapeProgress,
    profile_url: str,
    position: int,
) -> None:
    """Stamp ``lead.consultation_note`` with provenance: how many "Exibir
    mais resultados" clicks had happened when this lead surfaced, and at
    which 1-indexed position it appeared in the listing."""
    clicks = progress.clicks_at_extraction(profile_url) or progress.clicks_performed
    plural = "" if clicks == 1 else "s"
    note = f"Extraído após {clicks} clique{plural}, posição {position}."
    if lead.consultation_note and note not in lead.consultation_note:
        lead.consultation_note = f"{lead.consultation_note} {note}".strip()
    else:
        lead.consultation_note = note


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


def _mark_without_related_keywords(lead: Lead) -> None:
    lead.validation_status = "maybe_incorrect"
    lead.validation_note = NO_RELATED_KEYWORDS_NOTE
    lead.matched_title = None
    lead.confidence_score = score_lead(lead)


def _counts_toward_people_search_target(
    lead: Lead,
    title_terms: list[str],
    include_uncertain: bool,
) -> bool:
    if not title_terms or include_uncertain:
        return True
    return bool(lead.matched_title)


# ---------------------------------------------------------------------------
# Default fetcher: Scrapling first, Playwright fallback
# ---------------------------------------------------------------------------


def _default_fetcher(options: PeopleSearchOptions) -> PeopleListFetcher | None:
    """Return the only supported backend: Playwright over CDP.

    The desktop app exposes the embedded Electron Chromium at 127.0.0.1:9223
    and the CLI/Chrome path uses 127.0.0.1:9222. Both require the CDP endpoint
    to be alive. We deliberately do NOT fall back to Scrapling or to a fresh
    Playwright browser carrying ``li_at``: the first is unbundled, the second
    routinely logs the user out of LinkedIn. Surface a clear error instead so
    the caller can react.
    """
    endpoint = getattr(options, "cdp_endpoint", "") or ""
    cdp_enabled = bool(getattr(options, "cdp_enabled", True))

    if not cdp_enabled or not endpoint:
        raise LinkedInAuthError(
            "Playwright/CDP é o único modo suportado. Configure linkedin_cdp_endpoint "
            "(o app desktop usa http://127.0.0.1:9223 automaticamente)."
        )

    if not probe_cdp_endpoint(endpoint):
        raise LinkedInAuthError(
            f"O endpoint CDP ({endpoint}) não respondeu. "
            "Reinicie o app — o Chromium embutido expõe a porta 9223 no boot. "
            "Se estiver via Chrome externo, abra-o com --remote-debugging-port=9222."
        )

    # Embedded Electron browser uses port 9223; reuse the existing page so we
    # don't navigate away from the tab the app already loaded via
    # ``embeddedBrowser.prepare()``.
    embedded_port = ":9223"
    reuse = embedded_port in endpoint
    logger.info(
        "linkedin_people_search: usando CDP em %s (%s). %s",
        endpoint,
        "browser embutido no app" if reuse else "Chrome aberto",
        "Página existente será reaproveitada." if reuse else "Você não será deslogado.",
    )
    return CDPPeopleFetcher(endpoint, options, reuse_page=reuse)


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


def _find_page_by_url(browser: Any, url_hint: str) -> Any | None:
    """Return the first open page whose URL contains *url_hint*, or None.

    Used by CDPPeopleFetcher in reuse_page mode to find the LinkedIn page
    that was already navigated by the Electron embedded browser, avoiding
    the need to open (and navigate) a new tab.
    """
    contexts = getattr(browser, "contexts", []) or []
    logger.debug(
        "[EmbeddedCDP] _find_page_by_url: hint=%r | %d contexto(s) encontrado(s)",
        url_hint,
        len(contexts),
    )
    for i, ctx in enumerate(contexts):
        pages = getattr(ctx, "pages", []) or []
        logger.debug("[EmbeddedCDP] contexto[%d]: %d página(s)", i, len(pages))
        for j, page in enumerate(pages):
            page_url = getattr(page, "url", "") or ""
            logger.debug("[EmbeddedCDP]   página[%d][%d]: %s", i, j, page_url)
            if url_hint in page_url:
                logger.info("[EmbeddedCDP] Página encontrada: contexto[%d] página[%d] → %s", i, j, page_url)
                return page
    logger.warning("[EmbeddedCDP] Nenhuma página com hint=%r encontrada entre os contextos", url_hint)
    return None


def _is_app_shell_url(url: str) -> bool:
    """True for the Electron React app shell page (never the scrape view).

    The host exposes the renderer as ``file://.../renderer/index.html`` in the
    packaged build and as ``http://localhost:<port>`` in dev. We must never
    navigate that page — it is the app UI itself.
    """
    u = (url or "").lower()
    if u.startswith("file://"):
        return True
    if u.startswith("devtools://") or u.startswith("chrome://"):
        return True
    if "localhost" in u or "127.0.0.1" in u:
        return True
    return False


def _find_embedded_view_page(browser: Any) -> Any | None:
    """Return the embedded WebContentsView page used for scraping.

    Electron exposes two page targets over CDP: the React app shell and the
    ``WebContentsView`` we drive for scraping (``about:blank`` until we
    navigate it). Electron does **not** implement ``Target.createTarget``, so
    Playwright cannot open a fresh page over CDP — we must reuse this existing
    view and navigate it ourselves. Preference order:

    1. a page already on ``linkedin.com`` (warmed up by a prior step),
    2. an ``about:blank`` page (our pre-loaded scrape view),
    3. any page that is not the app shell.
    """
    candidates: list[tuple[str, Any]] = []
    for ctx in getattr(browser, "contexts", []) or []:
        for page in getattr(ctx, "pages", []) or []:
            candidates.append((getattr(page, "url", "") or "", page))

    logger.info(
        "[EmbeddedCDP] _find_embedded_view_page: %d página(s) candidata(s): %s",
        len(candidates),
        [u[:60] or "(vazia)" for u, _ in candidates],
    )

    for url, page in candidates:
        if "linkedin.com" in url.lower():
            logger.info("[EmbeddedCDP] Reusando página já no LinkedIn: %s", url)
            return page
    for url, page in candidates:
        if url == "" or url.lower().startswith("about:blank"):
            logger.info("[EmbeddedCDP] Reusando view about:blank pré-carregada.")
            return page
    for url, page in candidates:
        if not _is_app_shell_url(url):
            logger.info("[EmbeddedCDP] Reusando página não-shell: %s", url)
            return page
    logger.warning("[EmbeddedCDP] Nenhuma view embutida reutilizável encontrada.")
    return None


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

    When *reuse_page* is True (embedded browser mode), the fetcher finds
    an existing LinkedIn page instead of opening a new one and skips the
    initial ``goto`` since the Electron host already navigated there.
    The page is also not closed at the end (it belongs to the host window).
    """

    needs_li_at = False

    def __init__(
        self,
        endpoint: str,
        options: PeopleSearchOptions,
        connect: Callable[[str], Any] | None = None,
        reuse_page: bool = False,
    ) -> None:
        self._endpoint = endpoint
        self._options = options
        self._connect = connect
        self._reuse_page = reuse_page

    def fetch_listing(self, url: str, *, li_at: str, scrolls: int) -> str:
        return self._run(url, max_clicks=scrolls, on_step=None)

    def fetch_listing_iterative(
        self,
        url: str,
        *,
        li_at: str,
        max_clicks: int,
        on_step: Callable[[int, str], bool],
    ) -> str:
        return self._run(url, max_clicks=max_clicks, on_step=on_step)

    def _run(
        self,
        url: str,
        *,
        max_clicks: int,
        on_step: Callable[[int, str], bool] | None,
    ) -> str:
        mode_label = "EMBUTIDO (reuse_page)" if self._reuse_page else "CHROME EXTERNO"
        logger.info(
            "[CDPPeopleFetcher] _run iniciado. modo=%s endpoint=%s url=%s max_clicks=%d",
            mode_label,
            self._endpoint,
            url,
            max_clicks,
        )
        connect = self._connect or self._default_connect
        try:
            browser = connect(self._endpoint)
            logger.info("[CDPPeopleFetcher] Conectado ao CDP em %s ✓", self._endpoint)
        except LinkedInAuthError:
            # _default_connect already raised a user-facing message (e.g.
            # "Playwright não está disponível no sidecar"). Preserve it.
            raise
        except Exception as exc:
            logger.error("[CDPPeopleFetcher] FALHA ao conectar em %s: %s", self._endpoint, exc)
            raise LinkedInAuthError(
                f"Não consegui conectar ao browser via CDP em {self._endpoint}. "
                "Verifique se o app está rodando ou inicie o Chrome com --remote-debugging-port=9222."
            ) from exc

        page_owned = not self._reuse_page
        if self._reuse_page:
            logger.info("[CDPPeopleFetcher] Modo embutido: reutilizando a view interna do app")
            page = _find_embedded_view_page(browser)
            if page is None:
                # Electron não implementa Target.createTarget, então new_page()
                # via CDP falha. Tentamos mesmo assim (funciona em Chrome real),
                # mas o caminho normal é reaproveitar a view embutida acima.
                logger.warning(
                    "[CDPPeopleFetcher] View embutida não encontrada. "
                    "Tentando abrir nova aba (só funciona em Chrome externo)."
                )
                contexts = list(getattr(browser, "contexts", []) or [])
                if not contexts:
                    raise LinkedInAuthError(
                        "Browser conectado mas sem contextos. Tente reabrir o app."
                    )
                try:
                    page = contexts[0].new_page()
                    page_owned = True
                except Exception as exc:
                    raise LinkedInAuthError(
                        "Não encontrei a aba interna do LinkedIn para reaproveitar. "
                        "Feche e reabra o app e tente novamente."
                    ) from exc
            else:
                logger.info("[CDPPeopleFetcher] Página reutilizada. URL atual: %s", getattr(page, "url", "?"))
        else:
            contexts = list(getattr(browser, "contexts", []) or [])
            logger.info("[CDPPeopleFetcher] %d contexto(s) disponível(is). Abrindo nova página.", len(contexts))
            if not contexts:
                raise LinkedInAuthError(
                    "Chrome conectado mas sem contextos abertos. Abra uma janela e tente de novo."
                )
            context = contexts[0]
            page = context.new_page()

        try:
            # Always navigate to the full keyword URL, even in reuse_page mode.
            # Electron's prepare() loads /people/ without ?keywords= — Python must
            # navigate to the keyword-filtered URL or the People tab shows everyone.
            if self._reuse_page:
                page.set_default_timeout(15000)
            logger.info(
                "[CDPPeopleFetcher] Navegando para: %s (reuse_page=%s)",
                url, self._reuse_page,
            )
            try:
                page.goto(url, wait_until="domcontentloaded")
                logger.info("[CDPPeopleFetcher] page.goto() concluído. URL final: %s", getattr(page, "url", "?"))
            except Exception as exc:
                logger.error("[CDPPeopleFetcher] page.goto() FALHOU: %s", exc)
                raise classify_playwright_error(exc) from exc

            final_url = getattr(page, "url", "") or ""
            if any(
                marker in final_url
                for marker in ("/login", "/authwall", "/checkpoint", "/uas/login")
            ):
                logger.error("[CDPPeopleFetcher] AUTHWALL/LOGIN detectado. URL: %s", final_url)
                raise LinkedInAuthError(
                    f"Não está logado no LinkedIn (URL: {final_url}). "
                    "Faça login e tente de novo."
                )

            logger.info("[CDPPeopleFetcher] Iniciando _expand_results. max_clicks=%d", max_clicks)
            _human_delay(self._options)
            if self._reuse_page:
                _expand_results_dom(
                    page,
                    self._options,
                    max_iterations=max(0, max_clicks),
                    on_step=on_step,
                )
            else:
                _expand_results(
                    page,
                    self._options,
                    max_iterations=max(0, max_clicks),
                    on_step=on_step,
                )
            html = _page_html_snapshot(page)
            logger.info(
                "[CDPPeopleFetcher] _expand_results concluído. HTML coletado: %d bytes. page_owned=%s",
                len(html),
                page_owned,
            )
            return html
        finally:
            if page_owned:
                logger.info("[CDPPeopleFetcher] Fechando página (page_owned=True)")
                try:
                    page.close()
                except Exception:
                    pass
            else:
                logger.info("[CDPPeopleFetcher] Página NÃO fechada (pertence ao browser embutido)")

    def _default_connect(self, endpoint: str) -> Any:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise LinkedInAuthError(
                "Playwright não está disponível no sidecar empacotado. "
                "Reinstale a versão mais recente do app — esse build não inclui "
                "o backend de scraping. (detalhe: {})".format(exc)
            ) from exc

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
        return self._run(url, li_at=li_at, max_clicks=scrolls, on_step=None)

    def fetch_listing_iterative(
        self,
        url: str,
        *,
        li_at: str,
        max_clicks: int,
        on_step: Callable[[int, str], bool],
    ) -> str:
        return self._run(url, li_at=li_at, max_clicks=max_clicks, on_step=on_step)

    def _run(
        self,
        url: str,
        *,
        li_at: str,
        max_clicks: int,
        on_step: Callable[[int, str], bool] | None,
    ) -> str:
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
                _expand_results(
                    page,
                    opts,
                    max_iterations=max(0, max_clicks),
                    on_step=on_step,
                )
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


def _page_html_snapshot(page: Any) -> str:
    """Return the current DOM HTML with a bounded first attempt.

    Electron's CDP target can leave Playwright's ``page.content()`` waiting
    long enough for the provider-level timeout to fire. Evaluating the root
    element gives us the same parser input with an explicit timeout.
    """
    try:
        html = page.locator("html").evaluate("(el) => el.outerHTML", timeout=5000)
        return str(html or "")
    except Exception as exc:
        logger.debug("linkedin_people_search: snapshot via locator falhou: %s", exc)
    try:
        return str(page.content() or "")
    except Exception as exc:
        logger.debug("linkedin_people_search: snapshot via page.content falhou: %s", exc)
        return ""


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


def _expand_results(
    page: Any,
    opts: PeopleSearchOptions,
    *,
    max_iterations: int,
    on_step: Callable[[int, str], bool] | None = None,
) -> None:
    """Click 'Exibir mais resultados' (or scroll) until the list stops growing.

    When ``on_step`` is provided, it is invoked after each successful click
    (and once at the start, before any click) with ``(clicks_done, html)``.
    Returning ``False`` from the callback stops the loop early — this is how
    the provider drives the iterative validation cycle without re-launching
    the browser.

    Behaviour is intentionally noisy on the timing axis: we hover before
    clicking, randomize wait windows between actions, and occasionally scroll
    the wheel before pressing the button. LinkedIn watches for robotic
    pacing — a fully deterministic loop is the easiest signal to flag.
    """
    import random

    # Initial step (before any click) — let the caller inspect the first
    # render and bail early if it already has what it needs.
    if on_step is not None:
        initial_html = _page_html_snapshot(page)
        logger.info(
            "linkedin_people_search: snapshot inicial capturado (%d bytes).",
            len(initial_html),
        )
        if not on_step(0, initial_html):
            return

    if max_iterations <= 0:
        return

    clicks_done = 0
    consecutive_no_growth = 0
    for iteration in range(max_iterations):
        before = _count_cards(page)
        logger.info(
            "linkedin_people_search: expand iteration=%d before_cards=%d.",
            iteration + 1,
            before,
        )
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
            logger.info(
                "linkedin_people_search: sem botão visível; after_cards=%d no_growth=%d.",
                after,
                consecutive_no_growth,
            )
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
            logger.info("linkedin_people_search: botão load-more não clicável; encerrando expansão.")
            break

        _wait_for_card_growth(page, before, timeout_seconds=6.5)
        _human_delay(opts)
        after = _count_cards(page)
        logger.info(
            "linkedin_people_search: clique=%d concluído; after_cards=%d.",
            clicks_done + 1,
            after,
        )
        if after <= before:
            consecutive_no_growth += 1
            if consecutive_no_growth >= 2:
                break
        else:
            consecutive_no_growth = 0
        clicks_done += 1
        if on_step is not None:
            snapshot = _page_html_snapshot(page)
            logger.info(
                "linkedin_people_search: snapshot pós-clique=%d capturado (%d bytes).",
                clicks_done,
                len(snapshot),
            )
            if not on_step(clicks_done, snapshot):
                return


def _expand_results_dom(
    page: Any,
    opts: PeopleSearchOptions,
    *,
    max_iterations: int,
    on_step: Callable[[int, str], bool] | None = None,
) -> None:
    """Expand an embedded Electron CDP page using DOM APIs only.

    The in-app WebContentsView may be hidden or unfocused while Python is
    scraping it. Playwright's mouse/actionability path can stall in that
    state, so the embedded mode uses direct DOM scroll/click operations.
    """
    if on_step is not None:
        initial_html = _page_html_snapshot(page)
        logger.info(
            "linkedin_people_search: embedded snapshot inicial (%d bytes).",
            len(initial_html),
        )
        if not on_step(0, initial_html):
            return

    if max_iterations <= 0:
        return

    for click_index in range(1, max_iterations + 1):
        before = _count_cards_dom(page)
        logger.info(
            "linkedin_people_search: embedded expand click=%d before_cards=%d.",
            click_index,
            before,
        )
        _scroll_people_page_dom(page)
        _human_delay(opts)
        clicked = _click_load_more_dom(page)
        if not clicked:
            logger.info("linkedin_people_search: embedded sem botão load-more; encerrando.")
            break
        _wait_for_card_growth_dom(page, before, timeout_seconds=6.5)
        _human_delay(opts)
        after = _count_cards_dom(page)
        logger.info(
            "linkedin_people_search: embedded click=%d after_cards=%d.",
            click_index,
            after,
        )
        if on_step is not None:
            snapshot = _page_html_snapshot(page)
            logger.info(
                "linkedin_people_search: embedded snapshot pós-clique=%d (%d bytes).",
                click_index,
                len(snapshot),
            )
            if not on_step(click_index, snapshot):
                return
        if after <= before:
            break


def _count_cards_dom(page: Any) -> int:
    try:
        count = page.evaluate(
            """() => document.querySelectorAll(
              "li.org-people-profile-card__profile-card-spacing, li[class*='org-people-profile-card'], div[class*='org-people-profile-card']"
            ).length"""
        )
        return int(count or 0)
    except Exception as exc:
        logger.debug("linkedin_people_search: DOM count falhou: %s", exc)
        return _count_cards(page)


def _scroll_people_page_dom(page: Any) -> None:
    try:
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    except Exception as exc:
        logger.debug("linkedin_people_search: DOM scroll falhou: %s", exc)


def _click_load_more_dom(page: Any) -> bool:
    try:
        clicked = page.evaluate(
            """() => {
              const needles = [
                'exibir mais resultados',
                'show more results',
                'mostrar mais resultados'
              ];
              const buttons = Array.from(document.querySelectorAll('button'));
              const button = buttons.find((el) => {
                const label = `${el.getAttribute('aria-label') || ''} ${el.textContent || ''}`.toLowerCase();
                return needles.some((needle) => label.includes(needle));
              });
              if (!button) return false;
              button.scrollIntoView({ block: 'center' });
              button.click();
              return true;
            }"""
        )
        return bool(clicked)
    except Exception as exc:
        logger.debug("linkedin_people_search: DOM click load-more falhou: %s", exc)
        return False


def _wait_for_card_growth_dom(page: Any, baseline: int, *, timeout_seconds: float) -> None:
    deadline = time.time() + max(0.0, timeout_seconds)
    while time.time() < deadline:
        if _count_cards_dom(page) > baseline:
            return
        time.sleep(0.25)


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
