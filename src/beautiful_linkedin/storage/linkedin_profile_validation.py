"""LinkedIn profile validation for saved leads.

This is an opt-in enrichment layer for leads the user already selected. It
visits each LinkedIn profile, reads the current Experience entry, and opens
Contact Info to capture self-published email, phone, and website/portfolio.

The storage layer keeps these facts in LinkedIn-specific columns and mirrors
contact candidates into the existing alternatives trails. That preserves
Apollo/internal/searcher values while still exposing every contact signal.
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from beautiful_linkedin.models import Lead


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LinkedInProfileValidationUpdate:
    status: str = "no_change"  # validated | no_data | no_linkedin_url | failed
    experience_title: str | None = None
    experience_company: str | None = None
    experience_start_year: int | None = None
    experience_end_year: int | None = None
    contact_email: str | None = None
    contact_website: str | None = None
    contact_phone: str | None = None
    location: str | None = None
    education: list[dict[str, Any]] = field(default_factory=list)
    # Birthday in ``DD/MM`` format. LinkedIn exposes day/month under
    # the "Dados pessoais" section (visible only to first-degree
    # connections); the year is intentionally absent. Surfaced as a
    # high-weight signal for the Telegram CPF matcher.
    birthday: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class ProfileHtmlSnapshot:
    profile_url: str
    profile_html: str = ""
    contact_html: str = ""


ProgressFn = Callable[[dict[str, Any]], None]


def extract_profile_validation_snapshot(
    *,
    lead: Lead,
    profile_html: str,
    contact_html: str = "",
    profile_url: str | None = None,
) -> LinkedInProfileValidationUpdate:
    """Extract validation facts from saved LinkedIn HTML snapshots."""
    (
        experience_title,
        experience_company,
        experience_start_year,
        experience_end_year,
        experience_period,
    ) = _extract_experience(profile_html)
    contact_email = _extract_contact_email(contact_html)
    contact_website = _extract_contact_website(contact_html)
    contact_phone = _extract_contact_phone(contact_html)
    location = _extract_profile_location(profile_html)
    education = _extract_education(profile_html)
    # Birthday lives on the contact-info overlay ("Dados pessoais"
    # block) when the operator is a 1st-degree connection. We accept
    # either snapshot since some scrape paths concatenate both.
    birthday = _extract_birthday(contact_html) or _extract_birthday(profile_html)
    found_any = any(
        [
            experience_title,
            experience_company,
            experience_start_year,
            experience_end_year,
            contact_email,
            contact_website,
            contact_phone,
            location,
            education,
            birthday,
        ]
    )
    return LinkedInProfileValidationUpdate(
        status="validated" if found_any else "no_data",
        experience_title=experience_title,
        experience_company=experience_company,
        experience_start_year=experience_start_year,
        experience_end_year=experience_end_year,
        contact_email=contact_email,
        contact_website=contact_website,
        contact_phone=contact_phone,
        location=location,
        education=education,
        birthday=birthday,
        raw={
            "profile_url": profile_url or lead.linkedin_url,
            "source": "linkedin_profile_validation",
            "experience_period": experience_period,
            "experience_start_year": experience_start_year,
            "experience_end_year": experience_end_year,
            "location": location,
            "education": education,
            "birthday": birthday,
        },
    )


def run_linkedin_profile_validation(
    *,
    leads: list[Lead],
    page_fetcher: Any,
    max_leads: int = 40,
    on_event: ProgressFn | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> list[tuple[Lead, LinkedInProfileValidationUpdate]]:
    """Run validation with an injected page fetcher.

    ``page_fetcher`` must expose ``fetch(profile_url) -> ProfileHtmlSnapshot``.
    Keeping browser I/O behind this tiny contract lets tests stay fully
    offline and makes the Playwright/CDP wiring swappable.
    """
    emit = on_event or (lambda _event: None)
    cancelled = cancel_check or (lambda: False)
    results: list[tuple[Lead, LinkedInProfileValidationUpdate]] = []
    selected = leads[: max(1, min(max_leads, 40))]

    emit({"type": "start", "total": len(selected)})
    for index, lead in enumerate(selected, start=1):
        if cancelled():
            emit({"type": "phase", "phase": "cancelled"})
            break
        profile_url = _normalize_profile_url(lead.linkedin_url)
        if not profile_url:
            update = LinkedInProfileValidationUpdate(status="no_linkedin_url")
        else:
            try:
                snapshot = page_fetcher.fetch(profile_url)
                update = extract_profile_validation_snapshot(
                    lead=lead,
                    profile_html=snapshot.profile_html,
                    contact_html=snapshot.contact_html,
                    profile_url=snapshot.profile_url,
                )
            except Exception as exc:
                logger.warning(
                    "linkedin profile validation failed for %s: %s",
                    lead.linkedin_url or lead.person_name,
                    exc,
                )
                update = LinkedInProfileValidationUpdate(
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                    raw={"profile_url": profile_url},
                )
        results.append((lead, update))
        emit(
            {
                "type": "lead",
                "lead_ref": lead.linkedin_url or lead.source_url,
                "person_name": lead.person_name,
                "company_name": lead.company_name,
                "status": update.status,
                "experience_title": update.experience_title,
                "experience_company": update.experience_company,
                "contact_email": update.contact_email,
                "contact_website": update.contact_website,
                "contact_phone": update.contact_phone,
            }
        )
        emit({"type": "progress", "completed": index, "total": len(selected)})

    emit({"type": "phase", "phase": "completed"})
    return results


class CdpLinkedInProfilePageFetcher:
    """Fetch LinkedIn profile HTML through an already-open Chrome CDP session."""

    def __init__(
        self,
        *,
        endpoint: str,
        navigation_timeout_ms: int = 30_000,
        min_delay_seconds: float = 2.0,
        max_delay_seconds: float = 4.5,
    ) -> None:
        self._endpoint = endpoint
        self._navigation_timeout_ms = navigation_timeout_ms
        self._min_delay_seconds = min_delay_seconds
        self._max_delay_seconds = max_delay_seconds
        self._runtime: Any = None
        self._context: Any = None

    def __enter__(self) -> "CdpLinkedInProfilePageFetcher":
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(f"Playwright indisponível: {exc}") from exc

        self._runtime = sync_playwright().start()
        browser = self._runtime.chromium.connect_over_cdp(self._endpoint)
        contexts = list(getattr(browser, "contexts", []) or [])
        if not contexts:
            raise RuntimeError("Chrome CDP conectado, mas sem contexto aberto")
        self._context = contexts[0]
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._runtime is not None:
            try:
                self._runtime.stop()
            except Exception:
                pass

    def fetch(self, profile_url: str) -> ProfileHtmlSnapshot:
        if self._context is None:
            raise RuntimeError("fetcher não inicializado")
        page = self._context.new_page()
        try:
            page.set_default_timeout(self._navigation_timeout_ms)
            base_url = _normalize_profile_url(profile_url)
            if not base_url:
                raise ValueError("URL de perfil LinkedIn inválida")
            experience_url = f"{base_url.rstrip('/')}/details/experience/"
            education_url = f"{base_url.rstrip('/')}/details/education/"

            page.goto(base_url, wait_until="domcontentloaded")
            _settle_like_human(page, self._min_delay_seconds, self._max_delay_seconds)
            base_html = page.content()

            page.goto(experience_url, wait_until="domcontentloaded")
            _settle_like_human(page, self._min_delay_seconds, self._max_delay_seconds)
            experience_html = page.content()

            page.goto(education_url, wait_until="domcontentloaded")
            _settle_like_human(page, self._min_delay_seconds, self._max_delay_seconds)
            education_html = page.content()

            contact_html = _fetch_contact_info_html(
                page,
                base_url=base_url,
                min_delay_seconds=self._min_delay_seconds,
                max_delay_seconds=self._max_delay_seconds,
            )
            return ProfileHtmlSnapshot(
                profile_url=base_url,
                profile_html="\n".join([base_html, experience_html, education_html]),
                contact_html=contact_html,
            )
        finally:
            try:
                page.close()
            except Exception:
                pass


class PlaywrightLinkedInProfilePageFetcher:
    """Fetch profiles with a Playwright browser plus the user's ``li_at``.

    This is the same fallback posture as ``linkedin_people_search``: prefer
    CDP, but when CDP is offline use a dedicated Chromium session with the
    operator-provided/resolved cookie. It may create a separate LinkedIn
    session, so the server warns through the same user-facing copy.
    """

    def __init__(
        self,
        *,
        li_at: str,
        headless: bool = True,
        min_delay_seconds: float = 2.0,
        max_delay_seconds: float = 4.5,
        navigation_timeout_ms: int = 30_000,
    ) -> None:
        self._li_at = li_at
        self._headless = headless
        self._min_delay_seconds = min_delay_seconds
        self._max_delay_seconds = max_delay_seconds
        self._navigation_timeout_ms = navigation_timeout_ms
        self._runtime: Any = None
        self._browser: Any = None
        self._context: Any = None

    def __enter__(self) -> "PlaywrightLinkedInProfilePageFetcher":
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(f"Playwright indisponível: {exc}") from exc

        self._runtime = sync_playwright().start()
        self._browser = self._runtime.chromium.launch(
            headless=self._headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context = self._browser.new_context(
            viewport={"width": 1366, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        self._context.add_cookies(
            [{"name": "li_at", "value": self._li_at, "url": LINKEDIN_BASE}]
        )
        self._verify_session()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._runtime is not None:
            try:
                self._runtime.stop()
            except Exception:
                pass

    def fetch(self, profile_url: str) -> ProfileHtmlSnapshot:
        if self._context is None:
            raise RuntimeError("fetcher não inicializado")
        page = self._context.new_page()
        try:
            page.set_default_timeout(self._navigation_timeout_ms)
            base_url = _normalize_profile_url(profile_url)
            if not base_url:
                raise ValueError("URL de perfil LinkedIn inválida")
            experience_url = f"{base_url.rstrip('/')}/details/experience/"
            education_url = f"{base_url.rstrip('/')}/details/education/"

            page.goto(base_url, wait_until="domcontentloaded")
            _raise_if_login(page)
            _settle_like_human(page, self._min_delay_seconds, self._max_delay_seconds)
            base_html = page.content()

            page.goto(experience_url, wait_until="domcontentloaded")
            _raise_if_login(page)
            _settle_like_human(page, self._min_delay_seconds, self._max_delay_seconds)
            experience_html = page.content()

            page.goto(education_url, wait_until="domcontentloaded")
            _raise_if_login(page)
            _settle_like_human(page, self._min_delay_seconds, self._max_delay_seconds)
            education_html = page.content()

            contact_html = _fetch_contact_info_html(
                page,
                base_url=base_url,
                min_delay_seconds=self._min_delay_seconds,
                max_delay_seconds=self._max_delay_seconds,
                raise_on_login=True,
            )
            return ProfileHtmlSnapshot(
                profile_url=base_url,
                profile_html="\n".join([base_html, experience_html, education_html]),
                contact_html=contact_html,
            )
        finally:
            try:
                page.close()
            except Exception:
                pass

    def _verify_session(self) -> None:
        if self._context is None:
            raise RuntimeError("contexto Playwright não inicializado")
        page = self._context.new_page()
        try:
            page.set_default_timeout(self._navigation_timeout_ms)
            page.goto(LINKEDIN_FEED_URL, wait_until="domcontentloaded")
            _raise_if_login(page)
            _settle_like_human(page, self._min_delay_seconds, self._max_delay_seconds)
        finally:
            try:
                page.close()
            except Exception:
                pass


def _find_top_level_blocks(section: Any) -> list[Any]:
    if not section:
        return []

    heading = section.find(["h1", "h2", "h3", "h4", "h5"])
    if not heading:
        all_lis = section.find_all("li")
        top_level_lis = []
        for li in all_lis:
            parent_li = li.find_parent("li")
            if not parent_li or parent_li not in all_lis:
                top_level_lis.append(li)
        return top_level_lis

    curr = heading
    list_container = None
    while curr and curr != section:
        for sib in curr.next_siblings:
            if not sib.name:
                continue
            if sib.name in {"ul", "ol"}:
                list_container = sib
                break
            if sib.name == "div":
                has_items = sib.find_all(attrs={"componentkey": lambda val: val and "entity-collection-item" in val})
                if has_items:
                    list_container = sib
                    break
                children = [c for c in sib.children if c.name in {"div", "li"}]
                if len(children) >= 1:
                    text = sib.get_text()
                    if any(marker in text.lower() for marker in ["o momento", "present", "current", "atual"]) or any(1900 < y < 2100 for y in _extract_years(text)):
                        list_container = sib
                        break
        if list_container:
            break
        curr = curr.parent

    if list_container:
        if list_container.name in {"ul", "ol"}:
            return [c for c in list_container.children if c.name == "li"]

        comp_items = list_container.find_all(attrs={"componentkey": lambda val: val and "entity-collection-item" in val})
        if comp_items:
            top_comp_items = []
            for item in comp_items:
                parent_item = item.find_parent(attrs={"componentkey": lambda val: val and "entity-collection-item" in val})
                if not parent_item or parent_item == list_container or parent_item not in list_container.descendants:
                    top_comp_items.append(item)
            if top_comp_items:
                return top_comp_items

        return [c for c in list_container.children if c.name in {"div", "li"}]

    all_lis = section.find_all("li")
    top_level_lis = []
    for li in all_lis:
        parent_li = li.find_parent("li")
        if not parent_li or parent_li not in all_lis:
            top_level_lis.append(li)
    return top_level_lis


def _extract_experience_structured(
    html: str,
) -> tuple[str | None, str | None, int | None, int | None, str | None]:
    soup = BeautifulSoup(html or "", "lxml")

    # Locate the experience section
    experience_section = None
    for section in soup.find_all("section"):
        if section.get("id") == "experience" or section.get("id") == "experience-section":
            experience_section = section
            break
        heading = section.find(["h2", "h3", "h4"])
        if heading and _normalize(heading.get_text()) in _EXPERIENCE_HEADINGS:
            experience_section = section
            break

    if not experience_section:
        return None, None, None, None, None

    top_level_lis = _find_top_level_blocks(experience_section)

    if not top_level_lis:
        return None, None, None, None, None

    first_block = top_level_lis[0]

    # Check if there are nested lis inside first_block
    descendant_lis = [li for li in first_block.find_all("li") if li != first_block]

    if descendant_lis:
        # Multi-role block!
        # Company Name is at the top of the block.
        block_clone = BeautifulSoup(str(first_block), "lxml")
        # Decompose any nested list blocks so we only keep the parent elements
        for nested in block_clone.find_all(["ul", "ol"]):
            nested.decompose()

        clone_strings = [s.strip() for s in block_clone.stripped_strings if s.strip()]
        clone_strings_filtered = [s for s in clone_strings if not _is_experience_noise(s)]
        company = clone_strings_filtered[0] if clone_strings_filtered else None

        # Sub-role is the first descendant li (most recent)
        sub_role_li = descendant_lis[0]
        sub_strings = [s.strip() for s in sub_role_li.stripped_strings if s.strip()]

        # Find period first in unfiltered sub_strings
        period = None
        start_year, end_year = None, None
        for s in sub_strings:
            if _looks_current_period(s) or _extract_years(s):
                period = s
                start_year, end_year = _experience_years_from_period(s)
                break

        # Filter noise to get title candidates
        sub_strings_filtered = [s for s in sub_strings if not _is_experience_noise(s) and s != period]
        title = sub_strings_filtered[0] if sub_strings_filtered else None

        return title, company, start_year, end_year, period
    else:
        # Single-role block!
        strings = [s.strip() for s in first_block.stripped_strings if s.strip()]

        # Find period first in unfiltered strings
        period = None
        start_year, end_year = None, None
        for s in strings:
            if _looks_current_period(s) or _extract_years(s):
                period = s
                start_year, end_year = _experience_years_from_period(s)
                break

        # Filter noise to get title and company candidates
        clean_candidates = [s for s in strings if not _is_experience_noise(s) and s != period]

        if len(clean_candidates) >= 2:
            title = clean_candidates[0]
            company = clean_candidates[1]
            return title, company, start_year, end_year, period
        elif len(clean_candidates) == 1:
            return clean_candidates[0], None, start_year, end_year, period

    return None, None, None, None, None


def _extract_experience(
    html: str,
) -> tuple[str | None, str | None, int | None, int | None, str | None]:
    try:
        title, company, start_year, end_year, period = _extract_experience_structured(html)
        if title and company:
            return title, company, start_year, end_year, period
    except Exception as exc:
        logger.debug("Structured experience extraction failed: %s", exc)

    lines = _visible_lines(html, prefer_aria_hidden=False)
    if not lines:
        return None, None, None, None, None
    section = _experience_section(lines)
    if not section:
        return None, None, None, None, None

    for index, line in enumerate(section):
        years = _extract_years(line)
        if (_looks_current_period(line) or years) and index >= 2:
            # Walk backwards from the period skipping noise lines (skip-link,
            # "Experiência", "Visualizar todas..." etc). Without this filter
            # a navigation banner two lines above the year hijacks ``title``.
            non_noise: list[str] = []
            for back in range(index - 1, -1, -1):
                cand = _clean_candidate(section[back])
                if not cand:
                    continue
                if _is_experience_noise(cand):
                    continue
                non_noise.append(cand)
                if len(non_noise) >= 2:
                    break
            if len(non_noise) >= 2:
                company, title = non_noise[0], non_noise[1]
                start_year, end_year = _experience_years_from_period(line)
                return title, company, start_year, end_year, _clean_candidate(line)

    candidates = [
        _clean_candidate(line)
        for line in section
        if not _is_experience_noise(line)
    ]
    candidates = [line for line in candidates if line]
    if len(candidates) >= 2:
        return candidates[0], candidates[1], None, None, None
    if len(candidates) == 1:
        return candidates[0], None, None, None, None
    return None, None, None, None, None


def _extract_profile_location(html: str) -> str | None:
    lines = _visible_lines(html, prefer_aria_hidden=False)
    for line in lines:
        candidate = _clean_candidate(line)
        if not candidate or len(candidate) > 140:
            continue
        normalized = _normalize(candidate)
        if _is_location_noise(candidate):
            continue
        if re.search(r"\b(brasil|brazil)\b", normalized):
            return candidate
        if _UF_LOCATION_RE.search(_strip_accents(candidate).upper()):
            return candidate
    return None


# Map Portuguese (and English) month names to integers. Lowercased so
# the regex can normalize whatever casing LinkedIn renders.
_MONTH_NAMES_TO_NUMBER: dict[str, int] = {
    "janeiro": 1,
    "january": 1,
    "fevereiro": 2,
    "february": 2,
    "marco": 3,
    "março": 3,
    "march": 3,
    "abril": 4,
    "april": 4,
    "maio": 5,
    "may": 5,
    "junho": 6,
    "june": 6,
    "julho": 7,
    "july": 7,
    "agosto": 8,
    "august": 8,
    "setembro": 9,
    "september": 9,
    "outubro": 10,
    "october": 10,
    "novembro": 11,
    "november": 11,
    "dezembro": 12,
    "december": 12,
}

# "Aniversário" headers we accept. LinkedIn alternates between the
# Brazilian Portuguese label and the English fallback depending on the
# operator's locale; both are caught case-insensitively.
_BIRTHDAY_LABELS: tuple[str, ...] = (
    "aniversario",
    "aniversário",
    "birthday",
    "data de nascimento",
)

# Two formats we see on LinkedIn:
#   - ``27 de dezembro`` / ``27 de Dezembro``
#   - ``27/12`` or ``27/12/1990`` (rare; only some locales)
_BIRTHDAY_PT_RE = re.compile(
    r"(\d{1,2})\s+de\s+([A-Za-zÀ-ÿ]+)",
    re.IGNORECASE,
)
_BIRTHDAY_SLASH_RE = re.compile(r"\b(\d{1,2})\s*[/\-\.]\s*(\d{1,2})\b")


def _extract_birthday(html: str) -> str | None:
    """Return the lead's ``DD/MM`` birthday from a LinkedIn snapshot.

    Searches the visible text for one of the known birthday labels,
    then looks at the next handful of lines for either ``DD de MÊS`` or
    ``DD/MM`` patterns. Returns ``None`` when nothing matches — birthday
    is privacy-gated by LinkedIn so absence is the common case.
    """
    if not html:
        return None
    lines = _visible_lines(html, prefer_aria_hidden=False)
    if not lines:
        lines = _visible_lines(html)
    if not lines:
        return None
    label_indexes = [
        idx
        for idx, line in enumerate(lines)
        if _line_matches_birthday_label(line)
    ]
    for idx in label_indexes:
        # Inspect a small window after the label — LinkedIn renders the
        # value either on the next line or two lines down (after a
        # screen-reader-only header). Stop at 4 lines so a stray match
        # from a different section can't bleed in.
        for offset in range(1, 5):
            if idx + offset >= len(lines):
                break
            candidate = _clean_candidate(lines[idx + offset])
            if not candidate:
                continue
            extracted = _parse_birthday_value(candidate)
            if extracted:
                return extracted
        # Sometimes LinkedIn renders the value on the SAME line as the
        # label ("Aniversário: 27 de dezembro"). Try that too.
        extracted = _parse_birthday_value(lines[idx])
        if extracted:
            return extracted
    return None


def _line_matches_birthday_label(line: str) -> bool:
    if not line:
        return False
    normalized = _strip_accents(line.strip()).lower()
    return any(label in normalized for label in _BIRTHDAY_LABELS)


def _parse_birthday_value(value: str) -> str | None:
    if not value:
        return None
    normalized = value.strip()
    pt_match = _BIRTHDAY_PT_RE.search(normalized)
    if pt_match:
        day = int(pt_match.group(1))
        month_token = _strip_accents(pt_match.group(2)).lower()
        month = _MONTH_NAMES_TO_NUMBER.get(month_token)
        if month is not None and 1 <= day <= 31:
            return f"{day:02d}/{month:02d}"
    slash_match = _BIRTHDAY_SLASH_RE.search(normalized)
    if slash_match:
        try:
            day = int(slash_match.group(1))
            month = int(slash_match.group(2))
        except ValueError:
            return None
        if 1 <= day <= 31 and 1 <= month <= 12:
            return f"{day:02d}/{month:02d}"
    return None


def _extract_education(html: str) -> list[dict[str, Any]]:
    lines = _visible_lines(html, prefer_aria_hidden=False)
    section = _education_section(lines)
    if not section:
        lines = _visible_lines(html)
        section = _education_section(lines)
    if not section:
        return []

    cleaned = [
        line
        for line in (_clean_candidate(item) for item in section)
        if line and not _is_education_noise(line)
    ]
    entries: list[dict[str, Any]] = []
    for index, line in enumerate(cleaned):
        years = _extract_years(line)
        if not years:
            continue
        institution = _previous_non_year_line(cleaned, index, offset=2)
        degree = _previous_non_year_line(cleaned, index, offset=1)
        if degree == institution:
            degree = None
        entry: dict[str, Any] = {
            "end_year": max(years),
            "period": line,
        }
        if institution:
            entry["institution"] = institution
        if degree:
            entry["degree"] = degree
        entries.append(entry)

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str | None, int | None, str | None]] = set()
    for entry in entries:
        key = (
            entry.get("institution"),
            entry.get("end_year"),
            entry.get("period"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


def _fetch_contact_info_html(
    page: Any,
    *,
    base_url: str,
    min_delay_seconds: float,
    max_delay_seconds: float,
    raise_on_login: bool = False,
) -> str:
    """Open the profile contact modal and return its rendered HTML.

    LinkedIn's contact panel is a modal opened from the profile page. The
    direct ``/overlay/contact-info/`` URL is kept as a fallback, but clicking
    the visible "Informações de contato" entry matches the UI path the user
    sees and is more reliable in logged-in Chrome sessions.
    """
    page.goto(base_url, wait_until="domcontentloaded")
    if raise_on_login:
        _raise_if_login(page)
    _settle_like_human(page, min_delay_seconds, max_delay_seconds)

    clicked_html = _click_contact_info_modal(page, min_delay_seconds, max_delay_seconds)
    if _looks_like_contact_info_html(clicked_html):
        return clicked_html

    contact_url = f"{base_url.rstrip('/')}/overlay/contact-info/"
    page.goto(contact_url, wait_until="domcontentloaded")
    if raise_on_login:
        _raise_if_login(page)
    _settle_like_human(page, min_delay_seconds, max_delay_seconds)
    return page.content()


def _click_contact_info_modal(
    page: Any,
    min_delay_seconds: float,
    max_delay_seconds: float,
) -> str:
    clickers = [
        lambda: page.get_by_text("Informações de contato", exact=False).first.click(
            timeout=5_000
        ),
        lambda: page.get_by_text("Contact info", exact=False).first.click(timeout=5_000),
        lambda: page.locator(
            "a[href*='overlay/contact-info'], "
            "button[aria-label*='Informações de contato'], "
            "button[aria-label*='Contact info']"
        ).first.click(timeout=5_000),
    ]
    for click in clickers:
        try:
            click()
            _settle_like_human(page, min_delay_seconds, max_delay_seconds)
            return page.content()
        except Exception:
            continue
    return ""


def _looks_like_contact_info_html(html: str) -> bool:
    if not html:
        return False
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    normalized = _normalize(text)
    return (
        "informacoes de contato" in normalized
        or "contact info" in normalized
        or bool(_extract_contact_email(html))
        or bool(_extract_contact_website(html))
        or bool(_extract_contact_phone(html))
    )


def _extract_contact_email(html: str) -> str | None:
    soup = BeautifulSoup(html or "", "lxml")
    for anchor in soup.find_all("a"):
        href = str(anchor.get("href") or "").strip()
        if href.lower().startswith("mailto:"):
            email = href.split(":", 1)[1].split("?", 1)[0].strip()
            if _EMAIL_RE.fullmatch(email):
                return email
    match = _EMAIL_RE.search(soup.get_text(" ", strip=True))
    return match.group(0) if match else None


def _extract_contact_website(html: str) -> str | None:
    soup = BeautifulSoup(html or "", "lxml")
    for anchor in soup.find_all("a"):
        href = str(anchor.get("href") or "").strip()
        website = _contact_website_url(href)
        if website:
            return website
    return None


def _extract_contact_phone(html: str) -> str | None:
    text = BeautifulSoup(html or "", "lxml").get_text(" ", strip=True)
    matches = _PHONE_RE.findall(text)
    for raw in matches:
        phone = _clean_phone(raw)
        digits = "".join(ch for ch in phone if ch.isdigit())
        if len(digits) >= 8:
            return phone
    return None


def _visible_lines(html: str, *, prefer_aria_hidden: bool = False) -> list[str]:
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    nodes = soup.select("[aria-hidden='true']") if prefer_aria_hidden else []
    raw_lines: list[str] = []
    if nodes:
        raw_lines = [node.get_text(" ", strip=True) for node in nodes]
    if not raw_lines:
        raw_lines = list(soup.stripped_strings)
    return _dedupe_lines(_clean_candidate(line) for line in raw_lines)


def _experience_section(lines: list[str]) -> list[str]:
    start = None
    for index, line in enumerate(lines):
        if _normalize(line) in _EXPERIENCE_HEADINGS:
            start = index + 1
            break
    if start is None:
        return []
    section: list[str] = []
    for line in lines[start:]:
        normalized = _normalize(line)
        if section and normalized in _SECTION_BOUNDARIES:
            break
        section.append(line)
    return section


def _education_section(lines: list[str]) -> list[str]:
    start: int | None = None
    for index, line in enumerate(lines):
        if _normalize(line) in _EDUCATION_HEADINGS:
            start = index + 1
            break
    if start is None:
        return []

    section: list[str] = []
    for line in lines[start:]:
        normalized = _normalize(line)
        if section and normalized in _EDUCATION_SECTION_BOUNDARIES:
            break
        section.append(line)
    return section


def _normalize_profile_url(url: str | None) -> str | None:
    cleaned = (url or "").strip()
    if not cleaned or "linkedin.com/in/" not in cleaned.lower():
        return None
    parsed = urlparse(cleaned)
    if not parsed.scheme:
        cleaned = "https://" + cleaned.lstrip("/")
        parsed = urlparse(cleaned)
    path = parsed.path.rstrip("/")
    if "/details/" in path:
        path = path.split("/details/", 1)[0]
    if "/overlay/" in path:
        path = path.split("/overlay/", 1)[0]
    return f"{parsed.scheme}://{parsed.netloc}{path}/"


def _is_contact_website(href: str) -> bool:
    return _contact_website_url(href) is not None


def _contact_website_url(href: str) -> str | None:
    if not href:
        return None
    lowered = href.lower()
    if lowered.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    parsed = urlparse(href)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = parsed.netloc.lower()
    if not host:
        return None
    if "linkedin.com" not in host:
        return href
    if parsed.path.startswith("/safety/go"):
        raw_target = parse_qs(parsed.query).get("url", [None])[0]
        if raw_target and _contact_website_url(raw_target):
            return raw_target
    return None


def _clean_candidate(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _dedupe_lines(values: Any) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        cleaned = _clean_candidate(value)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


def _normalize(value: str) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFD", value or "")
    ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).strip()


def _strip_accents(text: str) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFD", text or "")
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _looks_current_period(value: str) -> bool:
    normalized = _normalize(value)
    return any(
        marker in normalized
        for marker in {
            "o momento",
            "ate o momento",
            "present",
            "current",
            "atual",
        }
    )


def _is_experience_noise(value: str) -> bool:
    normalized = _normalize(value)
    if not normalized:
        return True
    if _is_navigation_noise(normalized):
        return True
    if normalized in {"experiencia", "experience", "cargo atual", "current role"}:
        return True
    if _looks_current_period(value):
        return True
    if normalized.startswith(("tempo integral", "full time", "part time")):
        return True
    if "visualizar" in normalized or "show all" in normalized:
        return True
    if any(marker in normalized for marker in {"seguidores", "follower", "conexoes", "connection"}):
        return True
    if normalized.replace(" ", "").isdigit():
        return True
    return False


def _is_location_noise(value: str) -> bool:
    normalized = _normalize(value)
    if not normalized:
        return True
    if _is_navigation_noise(normalized):
        return True
    if normalized in _SECTION_BOUNDARIES or normalized in _EDUCATION_HEADINGS:
        return True
    return any(
        marker in normalized
        for marker in {
            "informacoes de contato",
            "contact info",
            "connections",
            "conexoes",
            "perfil",
            "profile",
        }
    )


def _is_education_noise(value: str) -> bool:
    normalized = _normalize(value)
    if not normalized:
        return True
    if _is_navigation_noise(normalized):
        return True
    if normalized in _EDUCATION_HEADINGS:
        return True
    if "visualizar" in normalized or "show all" in normalized:
        return True
    if normalized.startswith(("atividades", "activities")):
        return True
    return False


def _is_navigation_noise(normalized: str) -> bool:
    return normalized == "skip to main content" or (
        normalized.startswith("pular para conte")
        and normalized.endswith("principal")
    )


def _previous_non_year_line(
    lines: list[str], index: int, *, offset: int
) -> str | None:
    target = index - offset
    if target < 0:
        return None
    candidate = lines[target]
    if _extract_years(candidate):
        return None
    return candidate


def _extract_years(value: str) -> list[int]:
    years = [int(m.group(0)) for m in re.finditer(r"\b(19|20)\d{2}\b", value or "")]
    return [year for year in years if 1900 < year < 2100]


def _experience_years_from_period(value: str) -> tuple[int | None, int | None]:
    years = _extract_years(value)
    if not years:
        return None, None
    start_year = years[0]
    normalized = _normalize(value)
    if len(years) >= 2:
        return start_year, years[-1]
    if any(marker in normalized for marker in {"o momento", "present", "current", "atual"}):
        return start_year, None
    return start_year, start_year


def _clean_phone(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip(" .;:,"))


def _settle_like_human(page: Any, low: float, high: float) -> None:
    """Small random scroll/pause sequence, matching people_search pacing."""
    try:
        page.mouse.wheel(0, random.randint(250, 900))
    except Exception:
        pass
    _human_delay(low, high)
    try:
        page.mouse.wheel(0, random.randint(-220, 360))
    except Exception:
        pass
    time.sleep(random.uniform(0.25, 0.85))


def _human_delay(low: float, high: float) -> None:
    low = max(0.0, low)
    high = max(low, high)
    if high <= 0:
        return
    time.sleep(random.uniform(low, high))


def _raise_if_login(page: Any) -> None:
    final_url = getattr(page, "url", "") or ""
    if any(marker in final_url for marker in _LOGIN_MARKERS):
        raise RuntimeError(
            f"LinkedIn redirecionou para login/checkpoint ({final_url}). "
            "Faça login no navegador ou atualize o li_at."
        )


LINKEDIN_BASE = "https://www.linkedin.com"
LINKEDIN_FEED_URL = f"{LINKEDIN_BASE}/feed/"
_LOGIN_MARKERS = ("/login", "/authwall", "/checkpoint", "/uas/login", "/signup")


_EXPERIENCE_HEADINGS = {
    "experiencia",
    "experience",
    "experiencias",
    "experiences",
    "experiencia profissional",
    "experiencias profissionais",
    "work experience",
    "historico profissional",
    "experiencia laboral",
}
_SECTION_BOUNDARIES = {
    "formacao academica",
    "educacao",
    "education",
    "licencas e certificados",
    "licenses certifications",
    "competencias",
    "skills",
    "recomendacoes",
    "recommendations",
}
_EDUCATION_HEADINGS = {"formacao academica", "educacao", "education"}
_EDUCATION_SECTION_BOUNDARIES = {
    "experiencia",
    "experience",
    "licencas e certificados",
    "licenses certifications",
    "competencias",
    "skills",
    "recomendacoes",
    "recommendations",
    "informacoes de contato",
    "contact info",
}
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{2,3}\)?[\s.-]?)?\d{4,5}[\s.-]?\d{4}")
_UF_LOCATION_RE = re.compile(
    r"(?:^|[\s,/-])(AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MT|MS|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)(?:$|[\s,/-])"
)
