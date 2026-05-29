"""Telethon-backed Telegram consult drivers.

This module is intentionally parallel to
``telegram_group_playwright_lookup``. It does not replace the CDP /
Telegram-Web flow; it only exposes the same result contract using
Telegram's MTProto client through Telethon.

Flow shape (same shape as the Playwright drivers):

1. Send the slash command (``/nome <Lead>``) to the bot.
2. The bot replies with a *loading* placeholder ("Buscando...",
   "Consultando...") and then either edits that placeholder or sends a
   NEW message carrying the actual result. The result message is
   identified by either:
     - an inline ``KeyboardButtonUrl`` (Gon, Findex, Unix all expose the
       result behind an external-link button), or
     - a media attachment (some bots ship the result as a downloadable
       file).
   Loading-shaped messages are skipped via a small phrase list so we do
   not persist "Buscando informação, aguarde!" as the lead's evidence.
3. When a URL button is found, the driver follows the link with httpx
   and scrapes the visible body text — that mirrors what the Playwright
   driver does with ``page.locator('body').inner_text()``. The text
   becomes the ``raw_text`` of the evidence row. The original Telegram
   message is kept as a small header so the operator can still see what
   the bot said. If the fetch fails, we fall back to the bot text alone
   so the row is never empty.

Access control & operational risks
-----------------------------------
Two pre-conditions can break a consult silently if not handled:

- **Bot never started.** A fresh Telegram account must tap *Start* before a
  bot replies. We detect an empty chat history and send ``/start`` once
  (:func:`_ensure_bot_conversation_started`).
- **Not in the group.** Unix/Void publish via ``@CONSULTASGRATIS4NV``; an
  account that is not a member cannot post. We attempt to join the public
  group (:func:`_ensure_group_membership`) and, on a write error, surface
  ``telethon_not_in_group`` instead of a generic timeout.

Risks the operator should be aware of (and why a *virtual/secondary number*
is recommended): these bots and groups operate in a grey area. Automated
``send_message`` / ``click`` traffic can trigger Telegram ``FloodWaitError``
rate limits (handled as ``rate_limited:<seconds>``), temporary or permanent
account bans, or the bot blocking the account. The shared
:class:`TelegramActionThrottle` spaces actions to reduce that risk but does
not eliminate it. Joining unknown public groups also exposes the account to
spam/abuse. None of this should run on a personal primary Telegram number.
"""

from __future__ import annotations

import asyncio
import html as html_module
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from beautiful_linkedin.storage.telegram_group_playwright_lookup import (
    FINDEX_BOT_URL,
    GONZALES_BOT_URL,
    TELEGRAM_GROUP_URL,
    TELEGRAM_UNIX_ROBOT_URL,
    TelegramConsultResult,
)


logger = logging.getLogger(__name__)


# Phrases the bots use in the "loading" placeholder reply. The match is
# case-insensitive and substring-based; if any of these appears in the
# message and the message has no URL button / media attached, we treat
# it as a placeholder and keep waiting for the real result.
_LOADING_PHRASES: tuple[str, ...] = (
    "aguarde",
    "buscando",
    "consultando",
    "processando",
    "carregando",
    "wait",
)

_DEFAULT_RESULT_WAIT_SECONDS = 60.0
_DEFAULT_RESULT_POLL_INTERVAL = 2.0
# After we see the bot's result message we sleep this many seconds
# before fetching the URL. Some bots register the result page with
# their backend AFTER replying on Telegram — fetching too soon yields
# an empty/half-loaded page. The 6 s post-load wait in
# ``CdpResultFetcher`` already covers client-side hydration; this beat
# covers the bot's server-side prep.
_DEFAULT_PRE_FETCH_SETTLE_SECONDS = 3.0

# Minimum spacing between consecutive Telegram actions sharing the same
# Telethon session. Keep this deliberately conservative: a full phone
# extraction normally performs several Telegram actions, so 15 seconds
# per action makes a complete extraction take roughly a minute or more.
TELETHON_ACTION_MIN_SPACING_SECONDS: float = 15.0


class TelegramActionThrottle:
    """Coalesces all Telegram-side actions through a shared async lock.

    Every send_message, button click, and follow-up query must hold the
    lock while ensuring at least ``min_spacing_seconds`` has elapsed
    since the previous action. The lock guarantees the bots see actions
    one-at-a-time even across providers (Findex/Gon/Unix share the
    Telegram session, so the rate-limit budget is global).
    """

    def __init__(self, *, min_spacing_seconds: float = TELETHON_ACTION_MIN_SPACING_SECONDS):
        self._lock = asyncio.Lock()
        self._min_spacing = max(0.0, float(min_spacing_seconds))
        # ``monotonic`` is the right clock — we only care about elapsed
        # time, not wall-clock changes.
        self._last_action_at: float = 0.0

    async def acquire(self) -> None:
        await self._lock.acquire()
        elapsed = time.monotonic() - self._last_action_at
        wait_for = self._min_spacing - elapsed
        if wait_for > 0:
            await asyncio.sleep(wait_for)

    def release(self) -> None:
        self._last_action_at = time.monotonic()
        self._lock.release()

    async def __aenter__(self) -> "TelegramActionThrottle":
        await self.acquire()
        return self

    async def __aexit__(self, *_: object) -> None:
        self.release()


# Module-level default throttle. The pipeline reuses this instance so a
# pipeline run that goes name → cpf → click → click respects the spacing
# across ALL of those actions, not per-provider.
_default_throttle: TelegramActionThrottle | None = None


def get_default_throttle() -> TelegramActionThrottle:
    global _default_throttle
    if _default_throttle is None:
        _default_throttle = TelegramActionThrottle()
    return _default_throttle


# ---------------------------------------------------------------------------
# Per-provider lead-to-lead pacing. The action throttle above spaces every
# individual Telegram action (send/click); this layer adds a stronger,
# provider-specific floor BETWEEN consults (i.e. lead-to-lead). Gonzales is
# the strictest base — it bans accounts that hammer it — so we never start
# two Gonzales consults less than 90 seconds apart, regardless of how fast
# the bot replied. The state is module-level so it survives across the
# fresh consult instances the server builds per request.
# ---------------------------------------------------------------------------
_PROVIDER_MIN_LEAD_SPACING_SECONDS: dict[str, float] = {
    "gon": 90.0,
    "gon_cpf": 90.0,
}
_provider_last_consult_monotonic: dict[str, float] = {}


def _enforce_provider_lead_spacing(provider: str) -> None:
    """Block until ``provider``'s minimum lead-to-lead spacing has elapsed.

    No-op for providers without a configured floor. Records the (post-wait)
    start instant so consecutive consults for the same provider are spaced
    by at least the configured minimum.
    """
    min_spacing = _PROVIDER_MIN_LEAD_SPACING_SECONDS.get(provider, 0.0)
    if min_spacing <= 0:
        return
    last = _provider_last_consult_monotonic.get(provider)
    if last is not None:
        wait_for = min_spacing - (time.monotonic() - last)
        if wait_for > 0:
            logger.info(
                "[telethon/%s] pacing: aguardando %.0fs antes da próxima "
                "consulta (mínimo %.0fs lead-a-lead)",
                provider,
                wait_for,
                min_spacing,
            )
            time.sleep(wait_for)
    _provider_last_consult_monotonic[provider] = time.monotonic()


URLFetcher = Callable[[str], str | None]


@dataclass(frozen=True)
class TelethonBotResponse:
    raw_text: str | None
    source_url: str | None = None
    downloaded_at: str | None = None
    downloaded_paths: tuple[str, ...] = ()


TelethonRequester = Callable[[str, str], TelethonBotResponse | str | None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_username(value: str) -> str:
    cleaned = (value or "").strip()
    return cleaned if cleaned.startswith("@") else f"@{cleaned}"


# Telethon raises distinct exceptions when the account cannot reach a chat.
# We map the relevant ones to stable, user-facing codes. The lookup is by
# class *name* so the module never has to import telethon at definition time
# (it is an optional dependency).
_ACCESS_ERROR_MAP: dict[str, str] = {
    # Not a member of the group / cannot post in it.
    "UserNotParticipantError": "telethon_not_in_group",
    "ChatWriteForbiddenError": "telethon_not_in_group",
    "ChannelPrivateError": "telethon_not_in_group",
    "ChatAdminRequiredError": "telethon_not_in_group",
    # Join request sent to a group that approves members manually.
    "InviteRequestSentError": "telethon_group_join_pending",
    # We blocked the bot — Telegram refuses to deliver our message.
    "YouBlockedUserError": "telethon_blocked_bot",
}


def _classify_access_error(exc: BaseException) -> str | None:
    """Map a Telethon access exception to a stable code, or None."""
    return _ACCESS_ERROR_MAP.get(type(exc).__name__)


async def _chat_has_history(client: Any, entity: Any) -> bool:
    """True when we have at least one message in this chat.

    Used to decide whether a bot conversation was ever started. On any
    uncertainty we answer True so we never spam ``/start`` needlessly.
    """
    try:
        messages = await client.get_messages(entity, limit=1)
    except Exception:
        return True
    return bool(messages)


async def _ensure_bot_conversation_started(
    client: Any, entity: Any, throttle: "TelegramActionThrottle"
) -> None:
    """Press *Start* on a bot we have never talked to.

    A brand-new user must tap the bot's *Start* button before it answers;
    sending ``/start`` over MTProto is the programmatic equivalent. We only
    do it when the chat has no history so existing conversations are left
    untouched (and the Telethon throttle is respected).
    """
    try:
        if await _chat_has_history(client, entity):
            return
    except Exception:
        return
    try:
        async with throttle:
            await client.send_message(entity, "/start")
        # Give the bot a beat to register the new conversation.
        await asyncio.sleep(1.0)
    except Exception as exc:
        logger.debug("Telethon /start best-effort failed entity=%s: %s", entity, exc)


async def _ensure_group_membership(client: Any, entity: Any) -> None:
    """Join a public group/channel so we are allowed to post the query.

    Best-effort and idempotent: joining a group we are already in is a
    no-op on Telegram's side. Errors are swallowed here — if the join
    genuinely failed, the subsequent ``send_message`` raises a write
    error that :func:`_classify_access_error` turns into a clear code.
    """
    try:
        from telethon.tl.functions.channels import (  # type: ignore[import-not-found]
            JoinChannelRequest,
        )
    except Exception:
        return
    try:
        await client(JoinChannelRequest(entity))
        await asyncio.sleep(0.5)
    except Exception as exc:
        logger.debug("Telethon join channel best-effort entity=%s: %s", entity, exc)


async def _guarded_send_message(
    client: Any,
    entity: Any,
    query: str,
    throttle: "TelegramActionThrottle",
    *,
    is_group: bool,
) -> None:
    """Send ``query`` to ``entity`` with start/join remediation.

    Order of operations:

    1. Pre-flight: join the group (groups) or press Start (bots, first
       contact only).
    2. Send the message.
    3. On a classified access error, remediate once more and retry. If the
       retry still fails, raise ``RuntimeError(<stable code>)`` so the
       orchestrator records a clean, actionable failure instead of a raw
       Telethon class name or a silent timeout.
    """
    async def _prepare() -> None:
        if is_group:
            await _ensure_group_membership(client, entity)
        else:
            await _ensure_bot_conversation_started(client, entity, throttle)

    await _prepare()
    try:
        async with throttle:
            await client.send_message(entity, query)
        return
    except Exception as exc:
        code = _classify_access_error(exc)
        if code is None:
            raise
    # One remediation + retry pass.
    await _prepare()
    try:
        async with throttle:
            await client.send_message(entity, query)
    except Exception as retry_exc:
        raise RuntimeError(
            _classify_access_error(retry_exc) or "telethon_not_in_group"
        ) from retry_exc


def _coerce_api_id(value: int | str | None) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _response_from_value(value: TelethonBotResponse | str | None) -> TelethonBotResponse:
    if isinstance(value, TelethonBotResponse):
        return value
    if value is None:
        return TelethonBotResponse(raw_text=None)
    return TelethonBotResponse(raw_text=str(value))


def _format_raw_text(response: TelethonBotResponse) -> str | None:
    text = response.raw_text or None
    if not response.downloaded_paths:
        return text
    artifact_block = "=== telegram_artifacts ===\n" + "\n".join(
        response.downloaded_paths
    )
    return f"{text}\n{artifact_block}" if text else artifact_block


def _error_code(exc: Exception) -> str:
    if type(exc).__name__ == "FloodWaitError":
        seconds = getattr(exc, "seconds", None)
        return f"rate_limited:{seconds}" if seconds is not None else "rate_limited"
    message = str(exc)
    if isinstance(exc, RuntimeError) and message in {
        "telegram_not_configured",
        "telegram_telethon_requires_sync_context",
        "telethon_not_installed",
        "telethon_session_not_authorized",
        "telethon_result_timeout",
        "telethon_serasa_result_button_missing",
        "telethon_serasa_result_timeout",
        # Access-control remediation outcomes (see _classify_access_error):
        "telethon_not_in_group",
        "telethon_group_join_pending",
        "telethon_blocked_bot",
        "telethon_bot_not_started",
    }:
        return message
    # Telethon access errors that escaped remediation map to a stable code
    # so the UI can show actionable guidance instead of a raw class name.
    access_code = _classify_access_error(exc)
    if access_code is not None:
        return access_code
    return f"{type(exc).__name__}:{message}" if message else type(exc).__name__


def _looks_like_loading(text: str | None) -> bool:
    if not text:
        return False
    lowered = text.strip().lower()
    if not lowered:
        return False
    return any(phrase in lowered for phrase in _LOADING_PHRASES)


# The group @CONSULTASGRATIS4NV is shared by several bots, and more than one
# answers a /nome with a menu that also exposes base buttons like SI-PNI /
# RECEITA. To click the *right* one we require a Void-Search marker. The
# decisive signal lives in the BUTTON LABELS: Void's menu carries its own
# "VOID" button alongside the bases (real menu:
# ['NACIONAL', 'SI-PNI', 'RECEITA', 'VOID', '🗑️']). The body text
# ("- NOME: ...\n🔎 SELECIONE UMA BASE") has no marker, so a text-only check
# misses it.
_VOID_MENU_MARKERS: tuple[str, ...] = (
    "void search",
    "void",
)


def _looks_like_void_menu(text: str | None, buttons: list[str] | None = None) -> bool:
    """True when the menu belongs to the Void Search bot.

    Checks both the message text and the button labels for a Void marker.
    The button labels are the reliable signal — Void's base menu always
    carries a self-named ``VOID`` button — so we only click the SI-PNI base
    on Void's own menu and never on another bot's identical-looking menu in
    the shared group.
    """
    haystacks: list[str] = []
    if text:
        haystacks.append(text.lower())
    for label in buttons or []:
        if label:
            haystacks.append(label.lower())
    if not haystacks:
        return False
    return any(
        marker in haystack
        for haystack in haystacks
        for marker in _VOID_MENU_MARKERS
    )


def _message_has_url_button(message: Any) -> bool:
    return _first_button_url(message) is not None


def _message_has_media(message: Any) -> bool:
    return getattr(message, "media", None) is not None


def _is_outgoing(message: Any) -> bool:
    """True when the message was sent by us (our query echo).

    Telethon exposes outgoing flag via ``message.out``. Polling the
    chat head returns ALL messages including the user's own; we need
    to skip those so we don't treat the ``/nome ...`` query echo as a
    result.
    """
    return bool(getattr(message, "out", False))


def _is_incoming_result(message: Any) -> bool:
    """A bot message is a real result when it carries a URL button or
    media — both signal the operator has something to click. Outgoing
    messages and loading-shaped text are filtered out.
    """
    if message is None or _is_outgoing(message):
        return False
    if _message_has_url_button(message) or _message_has_media(message):
        return True
    return False


def _is_result_message(message: Any) -> bool:
    """A message is a "real" result when it carries a button or media,
    OR when its text is not a known loading placeholder.

    Bots usually attach the URL button or a file to the result message,
    so the button/media check is the strong signal. The text-only path
    is the safety net for unusual bot variants whose result is plain
    text without a button (still rare in this product)."""
    if _message_has_url_button(message) or _message_has_media(message):
        return True
    text = getattr(message, "raw_text", None) or getattr(message, "message", None)
    if not text:
        return False
    return not _looks_like_loading(text)


def _strip_html(html: str) -> str:
    """Best-effort HTML → text conversion that mirrors Playwright's
    body.inner_text well enough for downstream parsers.

    We deliberately avoid pulling in BeautifulSoup just for this — the
    downstream parser already tolerates noisy text. The few regex passes
    drop scripts/styles, collapse tags, decode entities, and trim
    whitespace.
    """
    if not html:
        return ""
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?s)<br\s*/?>", "\n", text)
    text = re.sub(r"(?s)</p>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html_module.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n\n", text)
    return text.strip()


def _resolve_cdp_endpoint() -> str:
    import os

    return (
        os.environ.get("BEAUTIFUL_LINKEDIN_CDP_ENDPOINT")
        or "http://127.0.0.1:9222"
    ).strip()


# Below this size the scraped body is almost certainly an SPA shell, a
# Cloudflare challenge, or some other "the page didn't really load"
# artifact. We treat it as not-useful and try the other transport.
MIN_USEFUL_CONTENT_CHARS = 80

# Sentinel that marks the body string as a scraper-side diagnostic
# rather than real page content. ``_compose_raw_text`` surfaces these
# under ``=== telegram_link_fetch_status ===`` instead of
# ``=== telegram_link_body ===`` so the operator can tell a "fetch
# failed" row apart from a "fetched but page was empty" row.
_FETCH_DIAGNOSTIC_PREFIX = "__telegram_scrape_failed__:"


def _format_fetch_failure(reasons: list[str]) -> str:
    """Compose the diagnostic body persisted when both CDP and httpx fail.

    Lives behind ``_FETCH_DIAGNOSTIC_PREFIX`` so downstream parsers
    that expect raw_text to be HTML-like content can ignore it cleanly
    (the prefix never matches CPF/phone regex), while the UI still
    surfaces "we tried and here is what went wrong".
    """
    lines = [
        "Não consegui abrir o link automaticamente.",
        "",
        "Tentativas:",
    ]
    for reason in reasons:
        lines.append(f"  • {reason}")
    lines.append("")
    lines.append("Dicas:")
    lines.append("  • Confirme que o Chrome com --remote-debugging-port=9222 está aberto.")
    lines.append("  • Abra o link manualmente — pode ser que ele esteja sob proteção anti-bot.")
    lines.append("  • O link normalmente expira em ~10 minutos.")
    return _FETCH_DIAGNOSTIC_PREFIX + "\n".join(lines)


class CdpResultFetcher:
    """Base CDP-first scraper. Opens the URL in the operator's
    CDP-connected Chrome, falls back to httpx for static HTML pages
    that don't need a browser, and persists a structured diagnostic
    body when both transports fail so the operator can act.

    Subclasses customize ``_extract_result`` for provider-specific
    pages. The shared bits (CDP connect, ``page.goto``, post-load
    wait, page cleanup, exception swallowing) live here so every
    provider inherits the same retry/fallback envelope.

    ``post_load_wait_seconds`` is the fixed sleep after
    ``wait_until="domcontentloaded"``. SPA result pages (Findex/Unix)
    fetch their data via API after DCL, so a short hardcoded wait is
    the safe pragmatic choice — networkidle is unreliable for these
    pages and per-element waits are subclass concerns.
    """

    post_load_wait_seconds: float = 6.0
    inner_text_timeout_ms: int = 12_000
    navigation_timeout_ms: int = 30_000

    def __init__(
        self,
        *,
        cdp_endpoint: str | None = None,
        post_load_wait_seconds: float | None = None,
    ) -> None:
        self._endpoint = (cdp_endpoint or _resolve_cdp_endpoint()).strip()
        if post_load_wait_seconds is not None:
            self.post_load_wait_seconds = max(0.0, float(post_load_wait_seconds))

    def __call__(self, url: str) -> str | None:
        if not url:
            return None
        reasons: list[str] = []
        cdp_text, cdp_reason = self._fetch_via_cdp_with_reason(url)
        if cdp_text and len(cdp_text.strip()) >= MIN_USEFUL_CONTENT_CHARS:
            logger.info(
                "[telethon/fetch] CDP ok chars=%d endpoint=%s",
                len(cdp_text.strip()),
                self._endpoint,
            )
            return cdp_text
        if cdp_reason:
            reasons.append(cdp_reason)
        elif cdp_text and cdp_text.strip():
            reasons.append(
                f"cdp_body_too_short:{len(cdp_text.strip())}chars"
            )
        logger.info(
            "[telethon/fetch] CDP miss reason=%s; falling back to httpx",
            cdp_reason or "body_too_short",
        )

        httpx_text, httpx_reason = _fetch_via_httpx_with_reason(url)
        if httpx_text and len(httpx_text.strip()) >= MIN_USEFUL_CONTENT_CHARS:
            logger.info(
                "[telethon/fetch] httpx ok chars=%d", len(httpx_text.strip())
            )
            return httpx_text
        if httpx_reason:
            reasons.append(httpx_reason)
        elif httpx_text and httpx_text.strip():
            reasons.append(
                f"httpx_body_too_short:{len(httpx_text.strip())}chars"
            )

        # Last resort: return whichever transport produced ANY content,
        # even if it was below the threshold — better than dropping
        # evidence entirely.
        for candidate in (cdp_text, httpx_text):
            if candidate and candidate.strip():
                logger.warning(
                    "[telethon/fetch] both transports below threshold; "
                    "returning short body chars=%d reasons=%s",
                    len(candidate.strip()),
                    reasons,
                )
                return candidate

        logger.warning("[telethon/fetch] fetch failed reasons=%s", reasons)
        return _format_fetch_failure(reasons or ["unknown_error"])

    def _fetch_via_cdp_with_reason(self, url: str) -> tuple[str | None, str | None]:
        """Return ``(body, reason)``. Exactly one is non-None on the
        happy path; both can be non-None when the page loaded but the
        body was unexpectedly empty, in which case the caller decides
        which signal wins via the threshold.
        """
        try:
            from playwright.sync_api import (  # type: ignore[import-not-found]
                sync_playwright,
            )
        except Exception as exc:
            return None, f"playwright_unavailable:{type(exc).__name__}"
        try:
            with sync_playwright() as runtime:
                try:
                    browser = runtime.chromium.connect_over_cdp(self._endpoint)
                except Exception as exc:
                    return None, f"cdp_connect_failed:{type(exc).__name__}"
                contexts = list(getattr(browser, "contexts", []) or [])
                if contexts:
                    context = contexts[0]
                else:
                    # Chrome is open but every tab was closed. Spin up
                    # a fresh context so we can still scrape — this is
                    # the same as opening a new private window.
                    try:
                        context = browser.new_context()
                    except Exception as exc:
                        return None, f"cdp_new_context_failed:{type(exc).__name__}"
                page = context.new_page()
                try:
                    try:
                        page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=self.navigation_timeout_ms,
                        )
                    except Exception as exc:
                        return None, f"cdp_goto_failed:{type(exc).__name__}"
                    if self.post_load_wait_seconds > 0:
                        page.wait_for_timeout(int(self.post_load_wait_seconds * 1000))
                    body = self._extract_result(page, context=context) or ""
                    return (body or None), None
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
        except Exception as exc:  # pragma: no cover - defensive
            return None, f"cdp_unexpected:{type(exc).__name__}"

    def _extract_result(self, page: Any, *, context: Any) -> str:  # noqa: ARG002
        try:
            return page.locator("body").inner_text(
                timeout=self.inner_text_timeout_ms
            )
        except Exception:
            return ""


class CdpFindexJsonFetcher(CdpResultFetcher):
    """Findex result page exposes an "Exportar JSON" download button.

    The data the operator actually wants is in that JSON — scraping the
    visible body text loses the structured payload (CPF/nome/endereço
    arrays). This fetcher clicks the button, captures the triggered
    download, and returns its contents as ``raw_text``. Falls back to
    body text on any failure so the evidence row is never empty.
    """

    export_button_timeout_ms: int = 20_000
    download_timeout_ms: int = 30_000

    def _extract_result(self, page: Any, *, context: Any) -> str:  # noqa: ARG002
        button = page.locator(
            "button:has-text('Exportar JSON'),"
            " a:has-text('Exportar JSON'),"
            " [role='button']:has-text('Exportar JSON'),"
            " button:has-text('exportar json'),"
            " a:has-text('exportar json')"
        ).last
        try:
            button.wait_for(state="visible", timeout=self.export_button_timeout_ms)
            try:
                button.scroll_into_view_if_needed()
            except Exception:
                pass
            with page.expect_download(timeout=self.download_timeout_ms) as dl_info:
                button.click()
            download = dl_info.value
            path = download.path()
            if path is not None:
                with open(path, "rb") as handle:
                    contents = handle.read().decode("utf-8", errors="replace")
                if contents.strip():
                    suggested = download.suggested_filename or "dados_pessoais.json"
                    header = f"=== findex_exportar_json ({suggested}) ===\n"
                    return header + contents
        except Exception as exc:
            logger.debug("Findex JSON export failed: %s", exc)
        try:
            return page.locator("body").inner_text(
                timeout=self.inner_text_timeout_ms
            )
        except Exception:
            return ""


class CdpUnixTextFetcher(CdpResultFetcher):
    """Unix (MK | UNIX) result page exposes a "Texto" download button.

    Like Findex, the visible body of the MK UNIX SPA is awkward to scrape
    (it lazy-renders the "PESSOAS ENCONTRADAS" cards), and the operator's
    "Texto" button yields a clean text dump with NOME/CPF/DATA DE
    NASCIMENTO/etc. We click it, capture the download, and return its
    contents so the downstream parser can extract the CPF and the birth
    date that drive the /cpf phone follow-up. Falls back to body text on
    any failure so the evidence row is never empty.
    """

    export_button_timeout_ms: int = 20_000
    download_timeout_ms: int = 30_000
    # The MK UNIX SPA renders its cards a beat after domcontentloaded; give
    # it a little longer than the default so "Texto" is present and the
    # body fallback is populated.
    post_load_wait_seconds: float = 8.0

    _TEXT_BUTTON_SELECTOR = (
        "button:has-text('Texto'),"
        " a:has-text('Texto'),"
        " [role='button']:has-text('Texto'),"
        " button:has-text('Baixar texto'),"
        " a:has-text('Baixar texto'),"
        " button:has-text('Exportar texto'),"
        " a:has-text('Exportar texto')"
    )

    def _extract_result(self, page: Any, *, context: Any) -> str:  # noqa: ARG002
        button = page.locator(self._TEXT_BUTTON_SELECTOR).last
        try:
            button.wait_for(state="visible", timeout=self.export_button_timeout_ms)
            try:
                button.scroll_into_view_if_needed()
            except Exception:
                pass
            with page.expect_download(timeout=self.download_timeout_ms) as dl_info:
                button.click()
            download = dl_info.value
            path = download.path()
            if path is not None:
                with open(path, "rb") as handle:
                    contents = handle.read().decode("utf-8", errors="replace")
                if contents.strip():
                    suggested = download.suggested_filename or "unix_resultado.txt"
                    logger.info(
                        "[telethon/fetch] Unix 'Texto' download captured "
                        "chars=%d file=%s",
                        len(contents),
                        suggested,
                    )
                    header = f"=== unix_texto ({suggested}) ===\n"
                    return header + contents
        except Exception as exc:
            logger.info(
                "[telethon/fetch] Unix 'Texto' download failed (%s); "
                "falling back to body text",
                type(exc).__name__,
            )
        try:
            return page.locator("body").inner_text(
                timeout=self.inner_text_timeout_ms
            )
        except Exception:
            return ""


# Module-level singletons — same Chrome contexts reused across consults.
_DEFAULT_BODY_TEXT_FETCHER: CdpResultFetcher | None = None
_DEFAULT_FINDEX_FETCHER: CdpFindexJsonFetcher | None = None
_DEFAULT_UNIX_FETCHER: CdpUnixTextFetcher | None = None


def _default_body_text_fetcher() -> CdpResultFetcher:
    global _DEFAULT_BODY_TEXT_FETCHER
    if _DEFAULT_BODY_TEXT_FETCHER is None:
        _DEFAULT_BODY_TEXT_FETCHER = CdpResultFetcher()
    return _DEFAULT_BODY_TEXT_FETCHER


def _default_unix_fetcher() -> CdpUnixTextFetcher:
    global _DEFAULT_UNIX_FETCHER
    if _DEFAULT_UNIX_FETCHER is None:
        _DEFAULT_UNIX_FETCHER = CdpUnixTextFetcher()
    return _DEFAULT_UNIX_FETCHER


def _default_findex_fetcher() -> CdpFindexJsonFetcher:
    global _DEFAULT_FINDEX_FETCHER
    if _DEFAULT_FINDEX_FETCHER is None:
        _DEFAULT_FINDEX_FETCHER = CdpFindexJsonFetcher()
    return _DEFAULT_FINDEX_FETCHER


def _default_url_fetcher(url: str) -> str | None:
    """Backward-compatible entry point for tests that monkeypatch the
    module-level fetcher. Production code goes through the per-class
    fetcher attribute on each consult.
    """
    return _default_body_text_fetcher()(url)


_BROWSER_LIKE_HTTPX_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "application/json;q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
}


def _fetch_via_httpx_with_reason(url: str) -> tuple[str | None, str | None]:
    """Return ``(body, reason)``. Same contract as the CDP variant.

    Some result pages return JSON directly — we keep ``response.text``
    verbatim in that case so downstream parsing can still pick up CPF
    and phone fields. HTML pages get stripped to visible text so they
    match what Playwright would yield.
    """
    if not url:
        return None, "empty_url"
    try:
        with httpx.Client(
            timeout=25.0,
            follow_redirects=True,
            headers=_BROWSER_LIKE_HTTPX_HEADERS,
        ) as client:
            response = client.get(url)
            status = response.status_code
            if status >= 400:
                snippet = (response.text or "").strip()[:80].replace("\n", " ")
                return None, f"httpx_http_{status}:{snippet!r}"
            content_type = response.headers.get("content-type", "").lower()
            if "html" in content_type or "xhtml" in content_type:
                return _strip_html(response.text), None
            return response.text, None
    except httpx.TimeoutException:
        return None, "httpx_timeout"
    except httpx.HTTPError as exc:
        return None, f"httpx_error:{type(exc).__name__}"
    except Exception as exc:  # pragma: no cover - defensive
        return None, f"httpx_unexpected:{type(exc).__name__}"


def _fetch_via_httpx(url: str) -> str | None:
    """Backward-compatible shim retained for tests that monkeypatch the
    legacy entry point. New code should call
    ``_fetch_via_httpx_with_reason`` for the failure diagnostic.
    """
    body, _ = _fetch_via_httpx_with_reason(url)
    return body


def _compose_raw_text(bot_text: str | None, url: str | None, page_text: str | None) -> str | None:
    """Pick the single most useful payload to persist as ``raw_text``.

    The bot's announcement message ("✅ Consulta concluída… 👇 Clique no
    botão…") is chat boilerplate, not data — when the bot exposes a URL
    button we follow the link and persist the scraped page directly,
    dropping the boilerplate. The URL itself stays available through
    ``source_url`` on the row so the UI can still render "Fonte ↗".

    Outcomes, in priority order:

    1. ``page_text`` carries the fetch-failure sentinel → return the
       diagnostic body (stripped of the sentinel) so the operator sees
       *why* the scrape failed and can act on it.
    2. ``page_text`` is real scraped content → return it as-is. No
       headers, no wrapper.
    3. No ``page_text`` but ``bot_text`` exists AND there is no URL →
       the bot returned inline data with no link to follow; persist
       the bot text as evidence.
    4. No ``page_text``, URL exists → minimal fallback so the row is
       never empty.
    5. Nothing useful → ``None``.
    """
    if page_text and page_text.strip():
        stripped = page_text.strip()
        if stripped.startswith(_FETCH_DIAGNOSTIC_PREFIX):
            return stripped[len(_FETCH_DIAGNOSTIC_PREFIX):].strip() or None
        return stripped
    if not url:
        return bot_text.strip() if bot_text and bot_text.strip() else None
    if bot_text and bot_text.strip():
        # The bot announced a clickable link but we did not get content
        # back from the scraper at all. Keep the URL so the operator can
        # still open it manually.
        return f"Link: {url}"
    return f"Link: {url}"


class TelethonBotConsultBase:
    provider = "base"
    bot_username = "@ConsultoriaGonzalesbot"
    # ``send_username``/``read_username`` decouple "where do we type the
    # command" from "where does the bot publish the result". Unix routes
    # the /nome query through the public group @CONSULTASGRATIS4NV but
    # the bot DMs the answer at @UnixGruposRobot, so the two differ.
    # When ``None``, both default to ``bot_username``.
    send_username: str | None = None
    read_username: str | None = None
    # True when the *send* target is a public group/channel rather than a
    # direct bot chat. Drives the access remediation: groups need a join,
    # bots need a first-contact ``/start``. See _guarded_send_message.
    send_is_group: bool = False
    source_url = GONZALES_BOT_URL
    command = "/nome"

    def __init__(
        self,
        *,
        api_id: int | str | None = None,
        api_hash: str | None = None,
        session_name: str = "data/telegram_telethon_lookup",
        timeout_seconds: float = 60.0,
        artifact_dir: str | Path = "data/telegram_artifacts/telethon",
        requester: TelethonRequester | None = None,
        url_fetcher: URLFetcher | None = None,
        result_wait_seconds: float = _DEFAULT_RESULT_WAIT_SECONDS,
        result_poll_interval: float = _DEFAULT_RESULT_POLL_INTERVAL,
        pre_fetch_settle_seconds: float = _DEFAULT_PRE_FETCH_SETTLE_SECONDS,
        throttle: TelegramActionThrottle | None = None,
    ) -> None:
        self._api_id = _coerce_api_id(api_id)
        self._api_hash = (api_hash or "").strip() or None
        self._session_name = session_name
        self._timeout_seconds = max(5.0, float(timeout_seconds))
        self._artifact_dir = Path(artifact_dir)
        self._requester = requester
        self._url_fetcher = url_fetcher or self._build_default_url_fetcher()
        self._result_wait_seconds = max(5.0, float(result_wait_seconds))
        self._result_poll_interval = max(0.2, float(result_poll_interval))
        self._pre_fetch_settle_seconds = max(0.0, float(pre_fetch_settle_seconds))
        self._throttle = throttle

    def _build_default_url_fetcher(self) -> URLFetcher:
        """Subclasses override to swap in a provider-specific scraper.

        The Findex result page exposes an ``Exportar JSON`` download
        button — for that provider we want the JSON payload, not the
        body text. Gon/Unix's default body-text scraper is fine for
        every other provider.
        """
        return _default_body_text_fetcher()

    def consult(self, lead_name: str) -> TelegramConsultResult:
        query = self._build_query(lead_name)
        username = _normalize_username(self.bot_username)
        started_at = time.monotonic()
        logger.info(
            "[telethon/%s] consult start command=%s bot=%s query=%r via=%s",
            self.provider,
            self.command,
            username,
            query,
            "requester_stub" if self._requester is not None else "mtproto",
        )
        try:
            if self._requester is not None:
                response = _response_from_value(self._requester(username, query))
            else:
                # Lead-to-lead pacing (e.g. Gonzales 90s) only matters for
                # the real network path; test stubs bypass it.
                _enforce_provider_lead_spacing(self.provider)
                response = self._request_via_telethon(username, query)
            raw_text = _format_raw_text(response)
            logger.info(
                "[telethon/%s] consult ok elapsed=%.1fs raw_chars=%d source_url=%s artifacts=%d",
                self.provider,
                time.monotonic() - started_at,
                len(raw_text or ""),
                response.source_url or self.source_url,
                len(response.downloaded_paths),
            )
            return TelegramConsultResult(
                provider=self.provider,
                lead_name=lead_name,
                query=query,
                raw_text=raw_text,
                source_url=response.source_url or self.source_url,
                downloaded_at=response.downloaded_at or _utc_now(),
                error=None,
            )
        except Exception as exc:
            logger.warning(
                "[telethon/%s] consult failed elapsed=%.1fs error_type=%s code=%s message=%s",
                self.provider,
                time.monotonic() - started_at,
                type(exc).__name__,
                _error_code(exc),
                exc,
            )
            return TelegramConsultResult(
                provider=self.provider,
                lead_name=lead_name,
                query=query,
                raw_text=None,
                source_url=self.source_url,
                downloaded_at=_utc_now(),
                error=_error_code(exc),
            )

    def _build_query(self, lead_name: str) -> str:
        return f"{self.command} {(lead_name or '').strip()}".strip()

    def _request_via_telethon(
        self, username: str, query: str
    ) -> TelethonBotResponse:
        if self._api_id is None or not self._api_hash:
            raise RuntimeError("telegram_not_configured")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._request_via_telethon_async(username, query))
        raise RuntimeError("telegram_telethon_requires_sync_context")

    async def _request_via_telethon_async(
        self, username: str, query: str
    ) -> TelethonBotResponse:
        try:
            from telethon import TelegramClient  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("telethon_not_installed") from exc

        send_target = _normalize_username(self.send_username or username)
        read_target = _normalize_username(self.read_username or username)
        cross_chat = send_target != read_target
        logger.info(
            "[telethon/%s] connecting send=%s read=%s cross_chat=%s is_group=%s",
            self.provider,
            send_target,
            read_target,
            cross_chat,
            self.send_is_group,
        )

        client = TelegramClient(self._session_name, self._api_id, self._api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                logger.warning("[telethon/%s] session not authorized", self.provider)
                raise RuntimeError("telethon_session_not_authorized")
            send_entity = await client.get_entity(send_target)
            read_entity = (
                send_entity if not cross_chat else await client.get_entity(read_target)
            )
            logger.debug("[telethon/%s] entities resolved", self.provider)
            await self._clear_private_chat_before_query(client, read_entity)
            if cross_chat:
                # When send and read entities differ we cannot use
                # ``client.conversation`` (it is bound to one entity).
                # Snapshot the read entity's last message id, send the
                # query through send_entity, and poll the read side for
                # new messages above the baseline.
                recent = await client.get_messages(read_entity, limit=1)
                baseline_id = recent[0].id if recent else 0
                throttle = self._throttle or get_default_throttle()
                logger.info(
                    "[telethon/%s] sending query (cross-chat) baseline_id=%d",
                    self.provider,
                    baseline_id,
                )
                await _guarded_send_message(
                    client,
                    send_entity,
                    query,
                    throttle,
                    is_group=self.send_is_group,
                )
                logger.info(
                    "[telethon/%s] query sent; awaiting result (timeout=%.0fs)",
                    self.provider,
                    self._result_wait_seconds,
                )
                message = await self._await_result_message_polling(
                    client, read_entity, baseline_id=baseline_id
                )
                return await self._response_from_message(client, message)
            throttle = self._throttle or get_default_throttle()
            # A bot that was never started stays silent — press Start first
            # so opening the Conversation context does not just time out.
            if not self.send_is_group:
                await _ensure_bot_conversation_started(client, read_entity, throttle)
            async with client.conversation(read_entity, timeout=self._timeout_seconds) as conv:
                try:
                    logger.info("[telethon/%s] sending query (same-chat)", self.provider)
                    async with throttle:
                        await conv.send_message(query)
                except Exception as exc:
                    code = _classify_access_error(exc)
                    if code is not None:
                        logger.warning(
                            "[telethon/%s] send blocked code=%s", self.provider, code
                        )
                        raise RuntimeError(code) from exc
                    raise
                logger.info(
                    "[telethon/%s] query sent; awaiting result (timeout=%.0fs)",
                    self.provider,
                    self._result_wait_seconds,
                )
                message = await self._await_result_message(client, conv, read_entity)
                return await self._response_from_message(client, message)
        finally:
            await client.disconnect()

    async def _clear_private_chat_before_query(self, client: Any, entity: Any) -> None:
        return None

    async def _await_result_message_polling(
        self, client: Any, entity: Any, *, baseline_id: int
    ) -> Any:
        """Cross-chat variant of ``_await_result_message``.

        Polls ``client.get_messages(entity, min_id=baseline_id)`` and
        returns the first INCOMING message that carries a URL button or
        a media attachment. Outgoing messages (the query we just sent)
        and loading placeholders ("Consultando…", "Aguarde…") are
        ignored. Raises ``telethon_result_timeout`` if the deadline
        expires with nothing usable.
        """
        deadline = asyncio.get_event_loop().time() + self._result_wait_seconds
        polls = 0
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                messages = await client.get_messages(
                    entity, limit=5, min_id=baseline_id
                )
            except Exception as exc:
                logger.debug("Telethon poll failed entity=%s: %s", entity, exc)
                messages = []
            polls += 1
            logger.debug(
                "[telethon/%s] poll #%d new_messages=%d remaining=%.0fs",
                self.provider,
                polls,
                len(messages or []),
                remaining,
            )
            for candidate in reversed(messages or []):
                if _is_incoming_result(candidate):
                    logger.info(
                        "[telethon/%s] result message found after %d poll(s)",
                        self.provider,
                        polls,
                    )
                    return candidate
                # Diagnose why incoming bot replies are NOT accepted as a
                # result (e.g. the bot answered with a callback menu or a
                # plain-text "selecione no privado" instead of a URL/media).
                if not _is_outgoing(candidate):
                    text = (
                        getattr(candidate, "raw_text", None)
                        or getattr(candidate, "message", None)
                        or ""
                    )
                    logger.info(
                        "[telethon/%s] ignored reply poll=%d has_url=%s "
                        "has_media=%s buttons=%s text=%r",
                        self.provider,
                        polls,
                        bool(_first_button_url(candidate)),
                        _message_has_media(candidate),
                        _describe_buttons(candidate),
                        text[:120],
                    )
            await asyncio.sleep(self._result_poll_interval)
        logger.warning(
            "[telethon/%s] result timeout after %d poll(s) (waited %.0fs)",
            self.provider,
            polls,
            self._result_wait_seconds,
        )
        raise RuntimeError("telethon_result_timeout")

    async def _await_result_message(
        self, client: Any, conv: Any, entity: Any
    ) -> Any:
        """Same-chat variant. Waits for the bot's real result message.

        Strategy:

        - Drain new messages via ``conv.get_response`` and accept the
          first one that carries a URL button or media (and is
          incoming, not the query we just sent).
        - Re-read the chat head each tick to catch bots that EDIT the
          loading placeholder in place — ``get_response`` does not fire
          on edits, but a fresh ``get_messages`` returns the message
          with its updated buttons.
        - Loading-shaped messages ("Consultando…", "Aguarde…", and the
          like) are never returned; if the deadline expires with only
          loading content we raise ``telethon_result_timeout`` so the
          orchestrator records a clean failure instead of persisting
          the placeholder as the lead's evidence.
        """
        deadline = asyncio.get_event_loop().time() + self._result_wait_seconds
        polls = 0
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                message = await asyncio.wait_for(
                    conv.get_response(),
                    timeout=min(self._result_poll_interval * 4, remaining),
                )
            except (asyncio.TimeoutError, Exception):
                message = None

            polls += 1
            if message is not None and _is_incoming_result(message):
                logger.info(
                    "[telethon/%s] result via get_response after %d poll(s)",
                    self.provider,
                    polls,
                )
                return message

            try:
                recent = await client.get_messages(entity, limit=3)
            except Exception:
                recent = []
            logger.debug(
                "[telethon/%s] poll #%d head_messages=%d remaining=%.0fs",
                self.provider,
                polls,
                len(recent or []),
                remaining,
            )
            for candidate in recent or []:
                if _is_incoming_result(candidate):
                    logger.info(
                        "[telethon/%s] result via chat-head after %d poll(s)",
                        self.provider,
                        polls,
                    )
                    return candidate

            await asyncio.sleep(self._result_poll_interval)

        logger.warning(
            "[telethon/%s] result timeout after %d poll(s) (waited %.0fs)",
            self.provider,
            polls,
            self._result_wait_seconds,
        )
        raise RuntimeError("telethon_result_timeout")

    async def _response_from_message(
        self, client: Any, message: Any
    ) -> TelethonBotResponse:
        bot_text = (
            getattr(message, "raw_text", None)
            or getattr(message, "message", None)
            or None
        )
        url = _first_button_url(message)
        logger.info(
            "[telethon/%s] processing result message has_url=%s has_media=%s bot_text_chars=%d",
            self.provider,
            bool(url),
            _message_has_media(message),
            len(bot_text or ""),
        )
        downloaded_paths: list[str] = []
        if _message_has_media(message):
            self._artifact_dir.mkdir(parents=True, exist_ok=True)
            try:
                downloaded = await client.download_media(
                    message, file=str(self._artifact_dir)
                )
            except Exception as exc:
                logger.warning(
                    "[telethon/%s] media download failed error_type=%s message=%s",
                    self.provider,
                    type(exc).__name__,
                    exc,
                )
                downloaded = None
            if downloaded:
                downloaded_paths.append(str(downloaded))
                logger.info(
                    "[telethon/%s] media downloaded path=%s", self.provider, downloaded
                )

        page_text: str | None = None
        if url:
            # Let the bot's backend register/prepare the result page
            # before we hit it. Bots like Findex respond on Telegram
            # the moment the job is queued, not when the URL is ready
            # — fetching too soon yields a half-loaded SPA shell.
            if self._pre_fetch_settle_seconds > 0:
                await asyncio.sleep(self._pre_fetch_settle_seconds)
            logger.info("[telethon/%s] fetching result url=%s", self.provider, url)
            page_text = await asyncio.to_thread(self._url_fetcher, url)
            logger.info(
                "[telethon/%s] url fetch done page_chars=%d",
                self.provider,
                len(page_text or ""),
            )

        raw_text = _compose_raw_text(bot_text, url, page_text)
        return TelethonBotResponse(
            raw_text=raw_text,
            source_url=url or self.source_url,
            downloaded_at=_utc_now(),
            downloaded_paths=tuple(downloaded_paths),
        )


def _first_button_url(message: Any) -> str | None:
    buttons = getattr(message, "buttons", None) or []
    for row in buttons:
        for button in row if isinstance(row, list) else [row]:
            url = getattr(button, "url", None)
            if url:
                return str(url)
    return None


class TelethonFindexNameConsult(TelethonBotConsultBase):
    provider = "finder"
    bot_username = "@FdxGP_bot"
    source_url = FINDEX_BOT_URL
    command = "/nome"

    def _build_default_url_fetcher(self) -> URLFetcher:
        # Findex's result page wants the "Exportar JSON" click, not
        # scraping body text. See ``CdpFindexJsonFetcher`` for the
        # download capture path.
        return _default_findex_fetcher()


class TelethonGonzalesNameConsult(TelethonBotConsultBase):
    provider = "gon"
    bot_username = "@ConsultoriaGonzalesbot"
    source_url = GONZALES_BOT_URL
    command = "/nome"

    async def _clear_private_chat_before_query(self, client: Any, entity: Any) -> None:
        await _clear_telethon_private_chat_messages(client, entity)


async def _clear_telethon_private_chat_messages(client: Any, entity: Any) -> None:
    try:
        messages = await client.get_messages(entity, limit=100)
    except Exception as exc:
        logger.debug("Telethon Gon cleanup get_messages failed entity=%s: %s", entity, exc)
        return
    ids: list[int] = []
    for message in messages or []:
        msg_id = getattr(message, "id", None)
        if isinstance(msg_id, int) and msg_id > 0:
            ids.append(msg_id)
    if not ids:
        return
    try:
        await client.delete_messages(entity, ids, revoke=True)
        await asyncio.sleep(0.5)
    except Exception as exc:
        logger.debug("Telethon Gon cleanup delete_messages failed entity=%s: %s", entity, exc)


# ---- Button helpers --------------------------------------------------------


def _iter_message_buttons(message: Any) -> list[Any]:
    """Flatten a message's ``buttons`` (rows of buttons) into a single list."""
    flattened: list[Any] = []
    for row in getattr(message, "buttons", None) or []:
        if isinstance(row, list):
            flattened.extend(row)
        else:
            flattened.append(row)
    return flattened


def _describe_buttons(message: Any) -> list[str]:
    """Return the visible labels of a message's buttons, for debug logs."""
    return [
        (getattr(button, "text", None) or "").strip()
        for button in _iter_message_buttons(message)
    ]


def _button_matches_text(button: Any, options: tuple[str, ...]) -> bool:
    label = (getattr(button, "text", None) or "").strip().lower()
    if not label:
        return False
    return any(option.lower() in label for option in options)


def _first_button_matching(message: Any, options: tuple[str, ...]) -> Any | None:
    for button in _iter_message_buttons(message):
        if _button_matches_text(button, options):
            return button
    return None


# SISREG-III is the Gonzales callback button after ``/cpf``. The bot
# occasionally renders the label as ``SISREG-III``, ``SISREG III``,
# ``Sisreg-III`` or ``Sisreg III``; lower-case substring match (handled
# by ``_button_matches_text``) covers all four. ``RESULTADO`` matches the
# final external-link button across providers.
_SISREG_BUTTON_OPTIONS: tuple[str, ...] = (
    "sisreg-iii",
    "sisreg iii",
    "sisreg-ii",  # safety net for ``SISREG-IIA`` style variants
)
_RESULT_BUTTON_OPTIONS: tuple[str, ...] = (
    "ver resultado completo",
    "resultado completo",
    "resultado aqui",
    "resultado (web)",
)
# Void's name→CPF base. We target the SI-PNI base (real menu:
# ['NACIONAL', 'SI-PNI', 'RECEITA', 'VOID', '🗑️']) because its result TXT
# carries the full "DADOS CADASTRAIS" block — CPF + NASC + ENDEREÇO — which
# is exactly what the matcher's location-weighted score needs. The variants
# cover Telegram's casing/spacing quirks for the same button.
_VOID_SIPNI_BUTTON_OPTIONS: tuple[str, ...] = ("si-pni", "sipni", "si pni", "si-pini")


class TelethonGonzalesCpfConsult(TelethonBotConsultBase):
    """Gonzales ``/cpf <cpf>`` follow-up via Telethon.

    Flow:

    1. ``/cpf <cpf>`` to ``@ConsultoriaGonzalesbot``.
    2. Bot replies with a "menu" message that carries the SISREG-III
       callback button (alongside other bases like Credilink/CNH).
    3. We click SISREG-III via Telethon's ``message.click(...)``. This
       fires a callback, NOT an external link — the bot answers with a
       NEW message in the same chat that contains the
       "ver resultado completo" URL button.
    4. We follow that URL through the configured URL fetcher (CDP first,
       httpx fallback) to scrape the phone-bearing result page.

    Every Telegram-side action (send, click) goes through the shared
    throttle so back-to-back queries cannot fire faster than the
    configured minimum spacing.
    """

    provider = "gon_cpf"
    bot_username = "@ConsultoriaGonzalesbot"
    source_url = GONZALES_BOT_URL
    command = "/cpf"

    async def _clear_private_chat_before_query(self, client: Any, entity: Any) -> None:
        await _clear_telethon_private_chat_messages(client, entity)

    def __init__(
        self,
        *,
        sisreg_wait_seconds: float = 30.0,
        result_wait_seconds: float = 45.0,
        **base_kwargs: Any,
    ) -> None:
        super().__init__(
            result_wait_seconds=result_wait_seconds,
            **base_kwargs,
        )
        self._sisreg_wait_seconds = max(5.0, float(sisreg_wait_seconds))

    async def _request_via_telethon_async(
        self, username: str, query: str
    ) -> TelethonBotResponse:
        try:
            from telethon import TelegramClient  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("telethon_not_installed") from exc

        client = TelegramClient(self._session_name, self._api_id, self._api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("telethon_session_not_authorized")
            entity = await client.get_entity(_normalize_username(username))
            await self._clear_private_chat_before_query(client, entity)

            recent = await client.get_messages(entity, limit=1)
            baseline_id = recent[0].id if recent else 0

            throttle = self._throttle or get_default_throttle()
            logger.info(
                "[telethon/%s] sending /cpf query; awaiting SISREG menu (timeout=%.0fs)",
                self.provider,
                self._sisreg_wait_seconds,
            )
            await _guarded_send_message(
                client, entity, query, throttle, is_group=self.send_is_group
            )

            menu = await self._await_button_message(
                client,
                entity,
                baseline_id=baseline_id,
                options=_SISREG_BUTTON_OPTIONS,
                deadline_seconds=self._sisreg_wait_seconds,
                fail_reason="telethon_sisreg_button_missing",
            )
            sisreg_label = _pick_button_label(menu, _SISREG_BUTTON_OPTIONS)
            logger.info(
                "[telethon/%s] SISREG menu found; clicking button=%r; "
                "awaiting result (timeout=%.0fs)",
                self.provider,
                sisreg_label,
                self._result_wait_seconds,
            )

            # Snapshot before the click so the next poll only sees the
            # bot's response to SISREG, not the menu message itself.
            post_click_baseline = menu.id

            async with throttle:
                await menu.click(text=sisreg_label)

            result = await self._await_button_message(
                client,
                entity,
                baseline_id=post_click_baseline,
                options=_RESULT_BUTTON_OPTIONS,
                deadline_seconds=self._result_wait_seconds,
                fail_reason="telethon_post_sisreg_result_missing",
            )
            logger.info("[telethon/%s] SISREG result button found", self.provider)
            return await self._response_from_message(client, result)
        finally:
            await client.disconnect()

    async def _await_button_message(
        self,
        client: Any,
        entity: Any,
        *,
        baseline_id: int,
        options: tuple[str, ...],
        deadline_seconds: float,
        fail_reason: str,
    ) -> Any:
        deadline = asyncio.get_event_loop().time() + deadline_seconds
        polls = 0
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                messages = await client.get_messages(
                    entity, limit=5, min_id=baseline_id
                )
            except Exception as exc:
                logger.debug("Telethon poll failed entity=%s: %s", entity, exc)
                messages = []
            polls += 1
            logger.debug(
                "[telethon/%s] button poll #%d new_messages=%d remaining=%.0fs "
                "looking_for=%s",
                self.provider,
                polls,
                len(messages or []),
                remaining,
                options,
            )
            for candidate in reversed(messages or []):
                if _first_button_matching(candidate, options) is not None:
                    return candidate
            await asyncio.sleep(self._result_poll_interval)
        logger.warning(
            "[telethon/%s] button timeout reason=%s after %d poll(s) (waited %.0fs)",
            self.provider,
            fail_reason,
            polls,
            deadline_seconds,
        )
        raise RuntimeError(fail_reason)


def _pick_button_label(message: Any, options: tuple[str, ...]) -> str:
    """Return the exact button label string Telethon expects in ``click(text=...)``.

    Telethon matches the click target by case-sensitive substring of the
    visible label, so we hand it the actual text the bot rendered for
    the matching button rather than one of our normalized options.
    """
    for button in _iter_message_buttons(message):
        if _button_matches_text(button, options):
            return getattr(button, "text", None) or ""
    # Fallback: return the first option literal so Telethon at least
    # tries something deterministic. The caller will see the failure
    # surface through the next poll deadline.
    return options[0]


# ---------------------------------------------------------------------------
# SERASA — /cpf phone lookup via the @puxada2026 group + @OraculoPuxadaBot DM.
# ---------------------------------------------------------------------------

# Where we type ``/cpf`` and where the answer is published.
SERASA_GROUP_USERNAME = "@puxada2026"
SERASA_BOT_USERNAME = "@OraculoPuxadaBot"
SERASA_GROUP_URL = "https://t.me/puxada2026"

# The group card carries a "VER RESULTADO" button; lower-case substring
# match tolerates Telegram's casing/emoji quirks.
_SERASA_RESULT_BUTTON_OPTIONS: tuple[str, ...] = ("ver resultado",)

# Markers that identify the bot's "CPF Encontrado" answer (inline text)
# versus a welcome/loading message. Any one is enough.
_SERASA_RESULT_MARKERS: tuple[str, ...] = (
    "cpf encontrado",
    "telefone",
    "nascimento",
    "naturalidade",
    "encontrado",
)


def _telegram_query_param(url: str, name: str) -> str | None:
    match = re.search(rf"[?&]{re.escape(name)}=([^&#\s]+)", url, re.IGNORECASE)
    return match.group(1) if match else None


def _parse_start_deeplink(url: str | None) -> tuple[str | None, str | None]:
    """Parse a Telegram bot start deep link into ``(bot_username, payload)``.

    Handles the two shapes Telegram uses for "open this bot and press
    Start with this token":

    - ``https://t.me/OraculoPuxadaBot?start=<token>`` (web/app link)
    - ``tg://resolve?domain=OraculoPuxadaBot&start=<token>`` (internal)

    Returns ``(None, None)`` when ``url`` carries neither a bot handle nor
    a payload so the caller can fall back to the configured bot username.
    """
    if not url:
        return None, None
    start = _telegram_query_param(url, "start")
    path_match = re.search(
        r"t(?:elegram)?\.me/([A-Za-z0-9_]+)", url, re.IGNORECASE
    )
    if path_match:
        return path_match.group(1), start
    domain = _telegram_query_param(url, "domain")
    if domain:
        return domain, start
    return None, start


def _url_targets_serasa_bot(url: str | None) -> bool:
    if not url:
        return False
    handle = SERASA_BOT_USERNAME.lstrip("@").lower()
    return handle in url.lower()


def _looks_like_serasa_result(text: str | None) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _SERASA_RESULT_MARKERS)


class TelethonSerasaCpfConsult(TelethonBotConsultBase):
    """SERASA — the preferred ``/cpf`` phone lookup.

    Flow observed in Telegram:

    1. Send ``/cpf <cpf>`` in the public group ``@puxada2026``.
    2. The group bot replies with a "CONSULTA CPF" card carrying a
       ``VER RESULTADO`` URL button. That button is a Telegram deep link
       (``t.me/OraculoPuxadaBot?start=<token>``) — clicking it in the app
       opens a private chat with ``@OraculoPuxadaBot`` and fires
       ``/start <token>``.
    3. We replicate the click by parsing the deep link and issuing the
       same ``/start <token>`` to the bot over MTProto (``StartBotRequest``
       with a plain ``/start`` text fallback).
    4. ``@OraculoPuxadaBot`` answers privately with a "CPF Encontrado"
       message whose inline text carries NOME/CPF/NASCIMENTO and the
       ``TELEFONES`` block. That text becomes ``raw_text`` so the existing
       phone harvester picks the numbers out — no URL to scrape.

    Every Telegram-side action (group send, bot start) goes through the
    shared throttle so back-to-back queries respect the global spacing.
    """

    provider = "serasa_cpf"
    bot_username = SERASA_BOT_USERNAME
    send_username = SERASA_GROUP_USERNAME
    read_username = SERASA_BOT_USERNAME
    send_is_group = True
    source_url = SERASA_GROUP_URL
    command = "/cpf"

    def __init__(
        self,
        *,
        group_wait_seconds: float = 45.0,
        result_wait_seconds: float = 60.0,
        **base_kwargs: Any,
    ) -> None:
        super().__init__(result_wait_seconds=result_wait_seconds, **base_kwargs)
        self._group_wait_seconds = max(5.0, float(group_wait_seconds))

    async def _request_via_telethon_async(
        self, username: str, query: str
    ) -> TelethonBotResponse:
        try:
            from telethon import TelegramClient  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("telethon_not_installed") from exc

        group_target = _normalize_username(self.send_username or username)
        bot_target = _normalize_username(self.read_username or self.bot_username)
        client = TelegramClient(self._session_name, self._api_id, self._api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("telethon_session_not_authorized")
            group_entity = await client.get_entity(group_target)
            bot_entity = await client.get_entity(bot_target)
            group_recent = await client.get_messages(group_entity, limit=1)
            group_baseline = group_recent[0].id if group_recent else 0
            return await self._run_serasa_flow(
                client,
                group_entity=group_entity,
                bot_entity=bot_entity,
                bot_target=bot_target,
                query=query,
                group_baseline=group_baseline,
            )
        finally:
            await client.disconnect()

    async def _run_serasa_flow(
        self,
        client: Any,
        *,
        group_entity: Any,
        bot_entity: Any,
        bot_target: str,
        query: str,
        group_baseline: int,
    ) -> TelethonBotResponse:
        throttle = self._throttle or get_default_throttle()
        logger.info(
            "[telethon/%s] sending /cpf to group; awaiting VER RESULTADO "
            "(timeout=%.0fs)",
            self.provider,
            self._group_wait_seconds,
        )
        await _guarded_send_message(
            client, group_entity, query, throttle, is_group=True
        )

        result_button_msg = await self._await_result_button_message(
            client,
            group_entity,
            baseline_id=group_baseline,
            deadline_seconds=self._group_wait_seconds,
        )
        url = _first_button_url(result_button_msg)
        bot_name, start_param = _parse_start_deeplink(url)
        logger.info(
            "[telethon/%s] VER RESULTADO found url=%s deeplink_bot=%s "
            "has_payload=%s",
            self.provider,
            url,
            bot_name,
            bool(start_param),
        )
        # Trust the deep link's bot handle when it differs from the
        # configured default (the group may rotate result bots).
        if bot_name and bot_name.lower() != bot_target.lstrip("@").lower():
            bot_entity = await client.get_entity(_normalize_username(bot_name))

        bot_recent = await client.get_messages(bot_entity, limit=1)
        bot_baseline = bot_recent[0].id if bot_recent else 0

        await self._trigger_bot_start(client, bot_entity, start_param, throttle)

        logger.info(
            "[telethon/%s] /start fired; awaiting CPF result (timeout=%.0fs)",
            self.provider,
            self._result_wait_seconds,
        )
        result_message = await self._await_text_result_message(
            client,
            bot_entity,
            baseline_id=bot_baseline,
            deadline_seconds=self._result_wait_seconds,
        )
        logger.info("[telethon/%s] CPF result message found", self.provider)
        return await self._response_from_message(client, result_message)

    async def _trigger_bot_start(
        self, client: Any, bot_entity: Any, start_param: str | None, throttle: Any
    ) -> None:
        """Open the bot with the deep-link token, replicating the button tap.

        ``StartBotRequest`` is the exact MTProto call a Telegram client
        issues when the user taps a ``?start=`` deep link. We fall back to
        sending ``/start <token>`` as plain text when Telethon's helper is
        unavailable or rejects the call — most bots parse that identically.
        """
        try:
            from telethon.tl.functions.messages import (  # type: ignore[import-not-found]
                StartBotRequest,
            )
        except Exception:
            StartBotRequest = None  # type: ignore[assignment]
        async with throttle:
            if StartBotRequest is not None and start_param:
                try:
                    await client(
                        StartBotRequest(
                            bot=bot_entity,
                            peer=bot_entity,
                            start_param=start_param,
                        )
                    )
                    return
                except Exception as exc:
                    logger.info(
                        "[telethon/%s] StartBotRequest failed (%s); "
                        "falling back to /start text",
                        self.provider,
                        type(exc).__name__,
                    )
            message = f"/start {start_param}".strip() if start_param else "/start"
            await client.send_message(bot_entity, message)

    async def _await_result_button_message(
        self, client: Any, entity: Any, *, baseline_id: int, deadline_seconds: float
    ) -> Any:
        """Poll the group for the incoming card carrying VER RESULTADO.

        The group is shared, so we only accept an incoming message whose
        button is labeled VER RESULTADO *or* whose URL points at the
        SERASA result bot — never our own ``/cpf`` echo or another bot's
        unrelated card.
        """
        deadline = asyncio.get_event_loop().time() + deadline_seconds
        polls = 0
        while True:
            if deadline - asyncio.get_event_loop().time() <= 0:
                break
            try:
                messages = await client.get_messages(
                    entity, limit=8, min_id=baseline_id
                )
            except Exception as exc:
                logger.debug("[telethon/%s] group poll failed: %s", self.provider, exc)
                messages = []
            polls += 1
            for candidate in reversed(messages or []):
                if _is_outgoing(candidate):
                    continue
                url = _first_button_url(candidate)
                if not url:
                    continue
                label_match = (
                    _first_button_matching(candidate, _SERASA_RESULT_BUTTON_OPTIONS)
                    is not None
                )
                if label_match or _url_targets_serasa_bot(url):
                    return candidate
            await asyncio.sleep(self._result_poll_interval)
        logger.warning(
            "[telethon/%s] VER RESULTADO timeout after %d poll(s) (waited %.0fs)",
            self.provider,
            polls,
            deadline_seconds,
        )
        raise RuntimeError("telethon_serasa_result_button_missing")

    async def _await_text_result_message(
        self, client: Any, entity: Any, *, baseline_id: int, deadline_seconds: float
    ) -> Any:
        """Poll the bot DM for the "CPF Encontrado" inline-text result.

        Accepts the first incoming message that matches a result marker
        (TELEFONE/NASCIMENTO/…) or carries media. Welcome/loading messages
        are skipped. If the deadline passes without a marker hit we return
        the last non-loading incoming message so a format change never
        silently drops a real answer.
        """
        deadline = asyncio.get_event_loop().time() + deadline_seconds
        polls = 0
        last_incoming: Any | None = None
        while True:
            if deadline - asyncio.get_event_loop().time() <= 0:
                break
            try:
                messages = await client.get_messages(
                    entity, limit=5, min_id=baseline_id
                )
            except Exception as exc:
                logger.debug("[telethon/%s] bot poll failed: %s", self.provider, exc)
                messages = []
            polls += 1
            for candidate in reversed(messages or []):
                if _is_outgoing(candidate):
                    continue
                if _message_has_media(candidate):
                    return candidate
                text = (
                    getattr(candidate, "raw_text", None)
                    or getattr(candidate, "message", None)
                    or ""
                )
                if not text.strip() or _looks_like_loading(text):
                    continue
                last_incoming = candidate
                if _looks_like_serasa_result(text):
                    return candidate
            await asyncio.sleep(self._result_poll_interval)
        if last_incoming is not None:
            logger.info(
                "[telethon/%s] no marker-matched result after %d poll(s); "
                "returning last incoming message",
                self.provider,
                polls,
            )
            return last_incoming
        logger.warning(
            "[telethon/%s] CPF result timeout after %d poll(s) (waited %.0fs)",
            self.provider,
            polls,
            deadline_seconds,
        )
        raise RuntimeError("telethon_serasa_result_timeout")


class TelethonUnixNameConsult(TelethonBotConsultBase):
    """Unix Mk consult via the public group + private-DM split.

    Unlike Findex/Gon the Unix bot does not accept ``/nome`` in a direct
    chat. Operators send the query in the public group
    ``@CONSULTASGRATIS4NV`` and the Unix robot DMs the user privately at
    ``@UnixGruposRobot`` with the actual result (a "RESULTADO (Web)"
    button). This consult routes the send/read pair accordingly.
    """

    provider = "unix"
    bot_username = "@UnixGruposRobot"
    send_username = "@CONSULTASGRATIS4NV"
    read_username = "@UnixGruposRobot"
    send_is_group = True
    source_url = TELEGRAM_UNIX_ROBOT_URL
    command = "/nome"

    def _build_default_url_fetcher(self) -> URLFetcher:
        # The MK UNIX result page exposes a "Texto" download button with a
        # clean NOME/CPF/DATA DE NASCIMENTO dump — capture that instead of
        # scraping the SPA body, so the CPF/birth date feed the phone stage.
        return _default_unix_fetcher()


class TelethonVoidNameConsult(TelethonBotConsultBase):
    """Void Search consult via the public ``@CONSULTASGRATIS4NV`` group.

    Flow observed in Telegram Web:

    1. Send ``/nome <lead>`` in the group.
    2. Wait for the ``Void Search`` menu message with database buttons
       (``NACIONAL | SI-PNI | RECEITA | VOID``).
    3. Click ``SI-PNI``.
    4. Wait for the bot to publish a ``.txt`` media result and download it.
       The SI-PNI base returns the full ``DADOS CADASTRAIS`` block — NOME,
       CPF, NASC and ENDEREÇO — so the parser harvests the CPF plus the
       address the location-weighted matcher relies on.

    The downloaded TXT contents become ``raw_text`` so the existing
    parser/scorer/storage path can treat Void like any other name-stage
    provider.
    """

    provider = "void"
    bot_username = "@CONSULTASGRATIS4NV"
    send_username = "@CONSULTASGRATIS4NV"
    read_username = "@CONSULTASGRATIS4NV"
    send_is_group = True
    source_url = TELEGRAM_GROUP_URL
    command = "/nome"

    def __init__(
        self,
        *,
        menu_wait_seconds: float = 45.0,
        txt_wait_seconds: float = 45.0,
        **base_kwargs: Any,
    ) -> None:
        super().__init__(**base_kwargs)
        self._menu_wait_seconds = max(5.0, float(menu_wait_seconds))
        self._txt_wait_seconds = max(5.0, float(txt_wait_seconds))

    async def _request_via_telethon_async(
        self, username: str, query: str
    ) -> TelethonBotResponse:
        try:
            from telethon import TelegramClient  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("telethon_not_installed") from exc

        send_target = _normalize_username(self.send_username or username)
        read_target = _normalize_username(self.read_username or username)
        client = TelegramClient(self._session_name, self._api_id, self._api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("telethon_session_not_authorized")
            send_entity = await client.get_entity(send_target)
            read_entity = (
                send_entity
                if send_target == read_target
                else await client.get_entity(read_target)
            )
            recent = await client.get_messages(read_entity, limit=1)
            baseline_id = recent[0].id if recent else 0
            return await self._run_void_flow(
                client,
                send_entity=send_entity,
                read_entity=read_entity,
                query=query,
                baseline_id=baseline_id,
            )
        finally:
            await client.disconnect()

    async def _run_void_flow(
        self,
        client: Any,
        *,
        send_entity: Any,
        read_entity: Any,
        query: str,
        baseline_id: int,
    ) -> TelethonBotResponse:
        throttle = self._throttle or get_default_throttle()
        logger.info(
            "[telethon/%s] sending /nome to group; awaiting SI-PNI menu "
            "(timeout=%.0fs)",
            self.provider,
            self._menu_wait_seconds,
        )
        await _guarded_send_message(
            client, send_entity, query, throttle, is_group=self.send_is_group
        )

        menu = await self._await_button_message(
            client,
            read_entity,
            baseline_id=baseline_id,
            options=_VOID_SIPNI_BUTTON_OPTIONS,
            deadline_seconds=self._menu_wait_seconds,
            fail_reason="telethon_void_sipni_button_missing",
        )
        post_click_baseline = getattr(menu, "id", baseline_id) or baseline_id
        base_label = _pick_button_label(menu, _VOID_SIPNI_BUTTON_OPTIONS)
        logger.info(
            "[telethon/%s] SI-PNI menu found; clicking %r; awaiting TXT "
            "(timeout=%.0fs)",
            self.provider,
            base_label,
            self._txt_wait_seconds,
        )

        async with throttle:
            await menu.click(text=base_label)

        txt_message = await self._await_txt_media_message(
            client,
            read_entity,
            baseline_id=post_click_baseline,
        )
        logger.info("[telethon/%s] TXT media message found", self.provider)
        return await self._response_from_txt_message(client, txt_message)

    async def _await_button_message(
        self,
        client: Any,
        entity: Any,
        *,
        baseline_id: int,
        options: tuple[str, ...],
        deadline_seconds: float,
        fail_reason: str,
    ) -> Any:
        # The public group @CONSULTASGRATIS4NV is shared by several bots and
        # MANY of them answer the same /nome with a menu that also has a
        # base button (SI-PNI/RECEITA). Clicking the wrong bot's button is
        # exactly why "o Void não está clicando" — we must pick the message
        # that belongs to Void Search. We disambiguate by requiring a Void
        # marker in the menu text/buttons and refuse to click another bot's.
        deadline = asyncio.get_event_loop().time() + deadline_seconds
        polls = 0
        seen_non_void = False
        while True:
            if deadline - asyncio.get_event_loop().time() <= 0:
                break
            try:
                messages = await client.get_messages(
                    entity, limit=8, min_id=baseline_id
                )
            except Exception as exc:
                logger.debug("Telethon Void button poll failed entity=%s: %s", entity, exc)
                messages = []
            polls += 1
            for candidate in reversed(messages or []):
                if _is_outgoing(candidate):
                    continue
                if _first_button_matching(candidate, options) is None:
                    continue
                text = (
                    getattr(candidate, "raw_text", None)
                    or getattr(candidate, "message", None)
                    or ""
                )
                button_labels = _describe_buttons(candidate)
                is_void = _looks_like_void_menu(text, button_labels)
                logger.info(
                    "[telethon/void] menu candidate poll=%d is_void=%s "
                    "buttons=%s text=%r",
                    polls,
                    is_void,
                    button_labels,
                    text[:120],
                )
                if is_void:
                    return candidate
                seen_non_void = True
            await asyncio.sleep(self._result_poll_interval)
        if seen_non_void:
            logger.warning(
                "[telethon/void] saw a base menu but none carried a Void "
                "marker — refusing to click another bot's button (reason=%s)",
                fail_reason,
            )
        logger.warning(
            "[telethon/%s] Void button timeout reason=%s after %d poll(s) (waited %.0fs)",
            self.provider,
            fail_reason,
            polls,
            deadline_seconds,
        )
        raise RuntimeError(fail_reason)

    async def _await_txt_media_message(
        self,
        client: Any,
        entity: Any,
        *,
        baseline_id: int,
    ) -> Any:
        deadline = asyncio.get_event_loop().time() + self._txt_wait_seconds
        polls = 0
        while True:
            if deadline - asyncio.get_event_loop().time() <= 0:
                break
            try:
                messages = await client.get_messages(
                    entity, limit=8, min_id=baseline_id
                )
            except Exception as exc:
                logger.debug("Telethon Void txt poll failed entity=%s: %s", entity, exc)
                messages = []
            polls += 1
            for candidate in reversed(messages or []):
                if _is_outgoing(candidate):
                    continue
                # Log every bot reply after the SI-PNI click so we can tell
                # whether the click registered and what shape the answer took
                # (TXT media vs. a URL button vs. another menu).
                text = (
                    getattr(candidate, "raw_text", None)
                    or getattr(candidate, "message", None)
                    or ""
                )
                logger.info(
                    "[telethon/void] post-click reply poll=%d has_media=%s "
                    "has_url=%s buttons=%s text=%r",
                    polls,
                    _message_has_media(candidate),
                    bool(_first_button_url(candidate)),
                    _describe_buttons(candidate),
                    text[:120],
                )
                if _message_has_media(candidate):
                    return candidate
            await asyncio.sleep(self._result_poll_interval)
        logger.warning(
            "[telethon/%s] Void TXT media timeout after %d poll(s) (waited %.0fs)",
            self.provider,
            polls,
            self._txt_wait_seconds,
        )
        raise RuntimeError("telethon_void_txt_missing")

    async def _response_from_txt_message(self, client: Any, message: Any) -> TelethonBotResponse:
        bot_text = (
            getattr(message, "raw_text", None)
            or getattr(message, "message", None)
            or None
        )
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        downloaded = await client.download_media(message, file=str(self._artifact_dir))
        downloaded_path = Path(downloaded) if downloaded else None
        txt_text = ""
        if downloaded_path:
            ready = await self._wait_for_downloaded_txt_ready(downloaded_path)
            if ready:
                txt_text = downloaded_path.read_text(encoding="utf-8", errors="replace")
        filename = downloaded_path.name if downloaded_path else "void_sipni.txt"
        chunks: list[str] = []
        if bot_text and bot_text.strip():
            chunks.append("=== telegram_bot_message ===\n" + bot_text.strip())
        if txt_text.strip():
            chunks.append(f"=== telegram_artifact:{filename} ===\n" + txt_text.strip())
        raw_text = "\n\n".join(chunks) if chunks else None
        return TelethonBotResponse(
            raw_text=raw_text,
            source_url=self.source_url,
            downloaded_at=_utc_now(),
            downloaded_paths=(str(downloaded_path),) if downloaded_path else (),
        )

    async def _wait_for_downloaded_txt_ready(
        self, path: Path, *, timeout_seconds: float = 10.0
    ) -> bool:
        """Wait until Telethon's downloaded TXT exists and is non-empty.

        On Windows/slow disks ``download_media`` can return a path before
        the file is immediately readable by the next line. The Void flow
        persists the downloaded text as the evidence row, so treating a
        not-yet-materialized path as "empty result" loses the consult.
        """
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        last_size = -1
        stable_ticks = 0
        while asyncio.get_event_loop().time() < deadline:
            try:
                size = path.stat().st_size
            except OSError:
                size = -1
            if size > 0 and size == last_size:
                stable_ticks += 1
                if stable_ticks >= 2:
                    return True
            else:
                stable_ticks = 0
                last_size = size
            await asyncio.sleep(0.1)
        return path.exists() and path.stat().st_size > 0


class TelethonTelegramConsultOrchestrator:
    def __init__(
        self,
        *,
        finder: TelethonFindexNameConsult | None = None,
        gon: TelethonGonzalesNameConsult | None = None,
        unix: TelethonUnixNameConsult | None = None,
        void: TelethonVoidNameConsult | None = None,
        api_id: int | str | None = None,
        api_hash: str | None = None,
        session_name: str = "data/telegram_telethon_lookup",
        requester: TelethonRequester | None = None,
        url_fetcher: URLFetcher | None = None,
        throttle: TelegramActionThrottle | None = None,
        pre_fetch_settle_seconds: float = _DEFAULT_PRE_FETCH_SETTLE_SECONDS,
    ) -> None:
        common = {
            "api_id": api_id,
            "api_hash": api_hash,
            "session_name": session_name,
            "requester": requester,
            "url_fetcher": url_fetcher,
            "throttle": throttle,
            "pre_fetch_settle_seconds": pre_fetch_settle_seconds,
        }
        self._finder = finder or TelethonFindexNameConsult(**common)
        self._gon = gon or TelethonGonzalesNameConsult(**common)
        self._unix = unix or TelethonUnixNameConsult(**common)
        self._void = void or TelethonVoidNameConsult(**common)
        self._by_name: dict[str, TelethonBotConsultBase] = {
            self._finder.provider: self._finder,
            self._gon.provider: self._gon,
            self._unix.provider: self._unix,
            self._void.provider: self._void,
        }

    @property
    def provider_names(self) -> tuple[str, ...]:
        return (
            self._finder.provider,
            self._gon.provider,
            self._unix.provider,
            self._void.provider,
        )

    def consult_provider(self, provider: str, lead_name: str) -> TelegramConsultResult:
        target = self._by_name.get(provider)
        if target is None:
            logger.warning("[telethon/orchestrator] unknown provider=%s", provider)
            return TelegramConsultResult(
                provider=provider,
                lead_name=lead_name,
                query="",
                raw_text=None,
                source_url=None,
                downloaded_at=_utc_now(),
                error=f"unknown_provider:{provider}",
            )
        return target.consult(lead_name)

    def consult(self, lead_name: str) -> list[TelegramConsultResult]:
        return [
            self.consult_provider(provider, lead_name)
            for provider in self.provider_names
        ]
