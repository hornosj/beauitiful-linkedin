"""Telegram consults via Playwright + Chrome CDP.

Providers live here, all driven through the user's already-logged-in
Telegram Web session connected via Chrome DevTools Protocol on
``http://127.0.0.1:9222``:

- :class:`GonzalesBotConsult` (``provider = "gon"``):
    1. Open ``@ConsultoriaGonzalesbot``, send ``/nome <Lead>``, Enter.
    2. ~16s post-send wait. The Gonzales bot replies inside its private chat
       with a "ver resultado completo" inline button.
    3. Click the latest button, accept the "Open external link?" popup.
    4. On the resulting tab, scrape the visible text as the raw result.

- :class:`UnixBotConsult` (``provider = "unix"``):
    1. Open ``@CONSULTASGRATIS4NV``, send ``/nome <Lead>``, Enter.
    2. ~16s post-send wait. Unix Mk publishes the result in a DM at
       ``@UnixGruposRobot``.
    3. Open a separate tab on the Unix Robot DM, click "RESULTADO (Web)".
    4. Accept the popup, click "Texto" on the result page, capture the
       ``nome_resultado_mk_unix_*.txt`` download. Retries up to N times
       when the site occasionally serves a corrupted UUID blob.

- :class:`FindexEmailConsult` (``provider = "findex"``):
    1. Open ``@FdxGP_bot``, send ``/email <email>``, Enter.
    2. Click "RESULTADO AQUI", accept the external-link popup, and
       scrape the visible text of the opened result page.

Both subclasses share :class:`TelegramGroupConsultBase` so the
boilerplate (CDP connection, tab opening, ``/nome`` send, popup
acceptance, fail-soft envelope) lives in one place. The
:class:`TelegramConsultOrchestrator` runs Gon first then Unix per lead
— Gon is cheaper and surfaces structured data more reliably, Unix is
the heavier-but-richer fallback.

Tests inject a ``fetcher`` callable on each subclass so the suite stays
fully offline.
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


TELEGRAM_ACTION_WAIT_SECONDS = 16.0
TELEGRAM_GROUP_URL = "https://web.telegram.org/k/#@CONSULTASGRATIS4NV"
TELEGRAM_UNIX_ROBOT_URL = "https://web.telegram.org/k/#@UnixGruposRobot"
GONZALES_BOT_URL = "https://web.telegram.org/k/#@ConsultoriaGonzalesbot"
FINDEX_BOT_URL = "https://web.telegram.org/k/#@FdxGP_bot"
GONZALES_BOT_USERNAME = "ConsultoriaGonzalesbot"

UNIX_RESULT_BUTTON_SELECTOR = (
    "button:has-text('RESULTADO (Web)'),"
    " a:has-text('RESULTADO (Web)'),"
    " [role='button']:has-text('RESULTADO (Web)')"
)
GON_RESULT_BUTTON_SELECTOR = (
    "button:has-text('ver resultado completo'),"
    " a:has-text('ver resultado completo'),"
    " [role='button']:has-text('ver resultado completo'),"
    " button:has-text('Ver Resultado Completo'),"
    " a:has-text('Ver Resultado Completo'),"
    " [role='button']:has-text('Ver Resultado Completo')"
)
GON_DELETE_BUTTON_SELECTOR = (
    "button:has-text('Apagar'),"
    " a:has-text('Apagar'),"
    " [role='button']:has-text('Apagar'),"
    " button:has-text('Excluir'),"
    " a:has-text('Excluir'),"
    " [role='button']:has-text('Excluir'),"
    " button:has-text('Delete'),"
    " a:has-text('Delete'),"
    " [role='button']:has-text('Delete')"
)
# SISREG-III é o botão inline que o Gonzales devolve depois de receber
# o /cpf. Diferente do "ver resultado completo", o clique aqui NÃO abre
# um link externo — ele dispara uma nova mensagem do bot, com texto
# "consulta concluída" + o CPF mascarado, e dentro DESSA mensagem é
# que aparece o botão "ver resultado completo" que abre a página com o
# telefone. As variações de caixa estão aí porque o bot ocasionalmente
# usa "Sisreg III" ou "SISREG III" (com espaço em vez de hífen).
GON_SISREG_BUTTON_SELECTOR = (
    "button:has-text('SISREG-III'),"
    " a:has-text('SISREG-III'),"
    " [role='button']:has-text('SISREG-III'),"
    " button:has-text('SISREG III'),"
    " a:has-text('SISREG III'),"
    " [role='button']:has-text('SISREG III'),"
    " button:has-text('Sisreg-III'),"
    " a:has-text('Sisreg-III'),"
    " [role='button']:has-text('Sisreg-III'),"
    " button:has-text('Sisreg III'),"
    " a:has-text('Sisreg III'),"
    " [role='button']:has-text('Sisreg III')"
)
# Regex para casar com o CPF dentro de um bubble de resposta — aceita
# com ou sem máscara e tolera espaços extras.
# A keyword ``consulta`` é checada com um lookahead: aceita "consulta
# concluída" / "consulta finalizada" / "consulta:", mas rejeita
# "Consultando..." (a mensagem de loading transitória que o Gonzales
# mostra e apaga antes do resultado).
_GON_CONSULTA_LABEL_RE = re.compile(
    r"consulta(?!\w)",
    re.IGNORECASE,
)
_GON_LOADING_RE = re.compile(r"consultando", re.IGNORECASE)
_GON_CPF_IN_BUBBLE_RE = re.compile(
    r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"
)


def _is_loading_bubble(text: str | None) -> bool:
    """Retorna True quando o bubble é o "Consultando..." transitório do
    Gonzales. Esse bubble é apagado quando o resultado real chega, mas
    durante a janela em que está visível pode confundir o matcher."""
    if not text:
        return False
    return bool(_GON_LOADING_RE.search(text))
FINDEX_RESULT_BUTTON_SELECTOR = (
    "button:has-text('RESULTADO AQUI'),"
    " a:has-text('RESULTADO AQUI'),"
    " [role='button']:has-text('RESULTADO AQUI'),"
    " button:has-text('Resultado Aqui'),"
    " a:has-text('Resultado Aqui'),"
    " [role='button']:has-text('Resultado Aqui'),"
    " button:has-text('resultado aqui'),"
    " a:has-text('resultado aqui'),"
    " [role='button']:has-text('resultado aqui')"
)
EXTERNAL_LINK_OPEN_SELECTOR = (
    ".popup-confirm-action button:has-text('Open'),"
    " .popup-confirm-action button:has-text('Abrir'),"
    " .popup-confirm-action button:has-text('OK'),"
    " .popup button:has-text('Open'),"
    " .popup button:has-text('Abrir')"
)


# Gonzales failure phrases we recognize. Lowercase substrings — the
# detector matches against ``inner_text().lower()`` of the latest
# bubble in the group chat, so accent/case differences don't matter.
#
# Keep the lists conservative: matching a phrase here aborts the Gon
# flow and the orchestrator moves straight to Unix. A false positive
# would mean we throw away a usable Gon result, so prefer specific
# wordings over generic ones (e.g. avoid bare "erro" or "aguarde").
GON_NOT_FOUND_PATTERNS: tuple[str, ...] = (
    "nao encontrado",
    "não encontrado",
    "nenhum resultado",
    "sem resultados",
    "nada encontrado",
    "consulta invalida",
    "consulta inválida",
    "nao foi possivel",
    "não foi possível",
)
GON_RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    "uso excessivo",
    "limite excedido",
    "limite de consultas",
    "muitas consultas",
    "muitas requisicoes",
    "muitas requisições",
    "tente novamente em",
    "tente mais tarde",
)


def detect_gon_failure(text: str | None) -> tuple[str, str] | None:
    """Inspect a Gon reply and decide whether to abort the flow.

    Returns ``(error_code, matched_phrase)`` when a known failure
    pattern is present, or ``None`` when the text looks like a normal
    response. ``error_code`` is one of ``gon_not_found`` /
    ``gon_rate_limit`` so the UI can present a tailored message.

    The match is substring-based on a lowercased version of the input
    — the bot occasionally uses accents and occasionally doesn't, and
    we treat both forms as equivalent.
    """
    if not text:
        return None
    lowered = text.lower()
    for phrase in GON_NOT_FOUND_PATTERNS:
        if phrase in lowered:
            return ("gon_not_found", phrase)
    for phrase in GON_RATE_LIMIT_PATTERNS:
        if phrase in lowered:
            return ("gon_rate_limit", phrase)
    return None


@dataclass(frozen=True)
class TelegramConsultResult:
    """One consult attempt. Always populated — even on failure, so the
    UI can render the error inline with the lead."""

    provider: str
    lead_name: str
    query: str
    raw_text: str | None
    source_url: str | None
    downloaded_at: str | None
    error: str | None


ConsultFetcher = Callable[[str], TelegramConsultResult]
"""Test seam: takes the lead's full name, returns the result."""


class TelegramGroupConsultBase:
    """Shared Playwright + CDP infrastructure for both consult flows.

    Subclasses override :meth:`_consult_via_playwright` to implement
    their provider-specific orchestration but reuse the connection,
    tab management, ``/nome`` send, and "Open external link?" popup
    handling defined here.
    """

    provider: str = "base"

    def __init__(
        self,
        *,
        cdp_endpoint: str = "http://127.0.0.1:9222",
        group_url: str = TELEGRAM_GROUP_URL,
        # Default fixo após enviar a query no composer.
        # Praticamente todos os bots (Gonzales, Unix, Findex) mostram
        # algum bubble transitório ("Consultando...", "Aguarde...")
        # imediatamente após o Enter, que aparece e some antes da
        # resposta real. Sair direto pro poll do botão tende a inspecionar
        # esse estado intermediário e leva a flake. Os 16s passivos dão
        # tempo do bubble de loading resolver antes de qualquer matcher
        # tocar no DOM. ``settle_wait_seconds`` segue como antes — ele
        # cobre só o tempo de aplicar a troca de chat na SPA.
        post_send_wait_seconds: float | None = None,
        post_send_min_seconds: float = TELEGRAM_ACTION_WAIT_SECONDS,
        post_send_max_seconds: float | None = None,
        settle_wait_seconds: float = 1.5,
        navigation_timeout_ms: int = 30_000,
        fetcher: ConsultFetcher | None = None,
    ) -> None:
        self._cdp_endpoint = cdp_endpoint
        self._group_url = group_url
        # Backwards compat: callers (tests) that pass ``post_send_wait_seconds``
        # get a deterministic wait at that exact value (min == max). New callers
        # supply min/max to get uniform jitter, which avoids the rhythmic
        # fingerprint that some Telegram bots use to flag automated traffic.
        if post_send_wait_seconds is not None:
            fixed = max(0.5, post_send_wait_seconds)
            self._post_send_min_seconds = fixed
            self._post_send_max_seconds = fixed
        else:
            self._post_send_min_seconds = max(0.5, post_send_min_seconds)
            self._post_send_max_seconds = max(
                self._post_send_min_seconds,
                post_send_max_seconds
                if post_send_max_seconds is not None
                else post_send_min_seconds,
            )
        self._settle_wait_seconds = max(0.0, settle_wait_seconds)
        self._navigation_timeout_ms = navigation_timeout_ms
        self._fetcher = fetcher

    @property
    def _post_send_wait_seconds(self) -> float:
        """Sample a fresh jittered wait each access (uniform [min, max])."""
        if self._post_send_min_seconds >= self._post_send_max_seconds:
            return self._post_send_min_seconds
        return random.uniform(
            self._post_send_min_seconds, self._post_send_max_seconds
        )

    def _build_query(self, value: str) -> str:
        """Compose the Telegram command sent to the bot for ``value``.

        Defaults to ``/nome <value>`` because Gon and Unix both expose
        the name-stage lookup that way. Subclasses targeting a different
        verb (e.g. :class:`GonzalesCpfConsult` for ``/cpf <cpf>``)
        override this single method instead of reimplementing the rest
        of the Playwright flow.
        """
        return f"/nome {value}"

    def consult(self, lead_name: str) -> TelegramConsultResult:
        name = (lead_name or "").strip()
        query = self._build_query(name)
        if not name:
            return TelegramConsultResult(
                provider=self.provider,
                lead_name=name,
                query=query,
                raw_text=None,
                source_url=None,
                downloaded_at=None,
                error="empty_lead_name",
            )
        try:
            if self._fetcher is not None:
                base_result = self._fetcher(name)
                # Force the fetcher's provider to match this instance
                # — keeps tests honest even if they pass a generic stub.
                return TelegramConsultResult(
                    provider=self.provider,
                    lead_name=base_result.lead_name,
                    query=base_result.query,
                    raw_text=base_result.raw_text,
                    source_url=base_result.source_url,
                    downloaded_at=base_result.downloaded_at,
                    error=base_result.error,
                )
            return self._consult_via_playwright(name, query)
        except Exception as exc:
            logger.exception("Telegram %s consult falhou para %s", self.provider, name)
            return TelegramConsultResult(
                provider=self.provider,
                lead_name=name,
                query=query,
                raw_text=None,
                source_url=None,
                downloaded_at=None,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _consult_via_playwright(
        self, name: str, query: str
    ) -> TelegramConsultResult:
        raise NotImplementedError

    # ---- shared step helpers ---------------------------------------------

    def _send_query_in_group(self, page: Any, query: str) -> float:
        """Type ``/nome <Name>`` into the active chat's composer. Verifies
        the URL hash matches the group before typing — protects against a
        SPA hash race silently routing the message into the wrong chat.

        **Regra global**: TODA mensagem enviada para um bot Telegram
        DEVE ser seguida por uma espera fixa de
        ``post_send_wait_seconds`` (default 16s, herdado por todos os
        drivers via ``TelegramGroupConsultBase``). Essa espera está
        embutida aqui — callers não precisam (e não devem) chamar
        ``time.sleep(self._post_send_wait_seconds)`` de novo. Praticamente
        todos os bots renderizam algum bubble transitório ("Consultando…",
        "Aguarde…") logo após o Enter; sair direto pro poll faz a
        automação inspecionar estados intermediários e flake.
        """
        expected_hash = (
            self._group_url.split("#", 1)[1] if "#" in self._group_url else ""
        )
        try:
            current = page.url or ""
        except Exception:
            current = ""
        current_hash = current.split("#", 1)[1] if "#" in current else ""
        if expected_hash and current_hash != expected_hash:
            raise RuntimeError(
                "Aborto: chat ativo é '"
                f"{current_hash or '(vazio)'}' mas esperado '{expected_hash}'."
                " A navegação SPA não trocou de chat —"
                " '/nome' não vai ser enviado pra evitar mandar no chat errado."
            )
        editor = page.locator(
            "div.input-message-input[contenteditable='true']"
        ).first
        editor.wait_for(state="visible")

        # Click composer to focus and clear any leftover text
        editor.click()
        time.sleep(0.2)
        page.keyboard.press("Control+A")
        time.sleep(0.1)
        page.keyboard.press("Backspace")
        time.sleep(0.2)

        # Type the search command with a slightly slower typing speed for reliability
        page.keyboard.type(query, delay=25)
        time.sleep(0.3)

        # Try sending and check if the editor gets cleared (indicating it sent successfully)
        sent = False
        send_selectors = [
            "button.btn-send",
            "button.send",
            ".btn-send",
            "button.btn-icon.tgico-send",
            "button:has(.tgico-send)",
            "button:has(.icon-send)",
            ".btn-send-container button",
            "button[title='Send Message']"
        ]

        # First attempt: press enter directly on editor
        editor.press("Enter")
        time.sleep(0.4)

        # Fallback loop: try Enter on page keyboard or click the send button if not cleared
        for attempt in range(5):
            val = (editor.inner_text() or "").strip()
            if not val:
                sent = True
                break

            page.keyboard.press("Enter")
            time.sleep(0.4)

            val = (editor.inner_text() or "").strip()
            if not val:
                sent = True
                break

            for selector in send_selectors:
                try:
                    btn = page.locator(selector).first
                    if btn.is_visible() and btn.is_enabled():
                        btn.click()
                        time.sleep(0.4)
                        break
                except Exception:
                    pass

            val = (editor.inner_text() or "").strip()
            if not val:
                sent = True
                break

            time.sleep(0.5)

        if not sent:
            logger.warning("Telegram input was not cleared. The command may not have been sent.")

        # Regra global: 16s fixos (default) após qualquer envio para o
        # bot. Centralizar aqui garante que nenhum caller esqueça.
        time.sleep(self._post_send_wait_seconds)
        return time.time()

    def _accept_external_link_popup(
        self, page: Any, *, PWTimeoutError: type
    ) -> None:
        """Click "Open"/"Abrir" on Telegram K's external-link confirm
        popup if it appears. If it doesn't show up in a short window,
        assume the link opened directly (some users have trusted the
        host already).
        """
        try:
            btn = page.locator(EXTERNAL_LINK_OPEN_SELECTOR).last
            btn.wait_for(state="visible", timeout=4_000)
            btn.click()
        except PWTimeoutError:
            pass


class UnixBotConsult(TelegramGroupConsultBase):
    """Provider 'unix' — Unix Mk via @UnixGruposRobot DM, downloads a
    ``nome_resultado_mk_unix_*.txt`` file. See module docstring."""

    provider = "unix"

    def __init__(
        self,
        *,
        unix_robot_url: str = TELEGRAM_UNIX_ROBOT_URL,
        download_timeout_ms: int = 30_000,
        max_download_attempts: int = 5,
        **base_kwargs: Any,
    ) -> None:
        super().__init__(**base_kwargs)
        self._unix_robot_url = unix_robot_url
        self._download_timeout_ms = download_timeout_ms
        # The result site occasionally serves a corrupted blob (UUID
        # filename, no extension) instead of the proper
        # ``nome_resultado_mk_unix_*.txt``. Retry the click until a
        # valid file lands.
        self._max_download_attempts = max(1, max_download_attempts)

    def _consult_via_playwright(
        self, name: str, query: str
    ) -> TelegramConsultResult:
        try:
            from playwright.sync_api import (  # type: ignore[import-not-found]
                TimeoutError as PWTimeoutError,
                sync_playwright,
            )
        except Exception as exc:  # pragma: no cover - depends on optional package
            raise RuntimeError(f"Playwright indisponível: {exc}") from exc

        with sync_playwright() as runtime:
            browser = runtime.chromium.connect_over_cdp(self._cdp_endpoint)
            contexts = list(getattr(browser, "contexts", []) or [])
            if not contexts:
                raise RuntimeError(
                    "Chrome CDP conectado mas sem contexto aberto. "
                    "Abra o Chrome com --remote-debugging-port=9222."
                )
            context = contexts[0]
            group_page: Any = None
            bot_page: Any = None
            try:
                # Open Unix robot chat page first to establish baseline count of existing buttons/bubbles
                bot_page = context.new_page()
                bot_page.goto(self._unix_robot_url, wait_until="domcontentloaded")
                bot_page.set_default_timeout(self._navigation_timeout_ms)
                time.sleep(self._settle_wait_seconds)

                initial_button_count = bot_page.locator(UNIX_RESULT_BUTTON_SELECTOR).count()
                initial_bubble_count = bot_page.locator("div.bubble").count()

                group_page = context.new_page()
                group_page.goto(self._group_url, wait_until="domcontentloaded")
                group_page.set_default_timeout(self._navigation_timeout_ms)
                time.sleep(self._settle_wait_seconds)
                group_page.locator(
                    "div.input-message-input[contenteditable='true']"
                ).first.wait_for(state="visible")
                # _send_query_in_group já dorme o post_send fixo (regra
                # global) — não duplicar o sleep aqui.
                self._send_query_in_group(group_page, query)

                # Bring bot page to front and wait for a new message with the button
                bot_page.bring_to_front()

                deadline = time.time() + (self._navigation_timeout_ms / 1000.0)
                new_button_found = False
                while time.time() < deadline:
                    try:
                        if bot_page.locator(UNIX_RESULT_BUTTON_SELECTOR).count() > initial_button_count:
                            new_button_found = True
                            break
                    except Exception:
                        pass

                    # Also look at failure bubbles
                    try:
                        loc = bot_page.locator("div.bubble")
                        if loc.count() > initial_bubble_count:
                            last_text = loc.last.inner_text(timeout=400)
                            failure = detect_gon_failure(last_text)
                            if failure is not None:
                                code, phrase = failure
                                raise RuntimeError(f"unix_{code}: {last_text.strip()[:140]}")
                    except RuntimeError:
                        raise
                    except Exception:
                        pass

                    time.sleep(0.5)

                if not new_button_found:
                    raise RuntimeError(
                        "unix_timeout: nenhuma resposta do Unix no chat do bot em "
                        f"{self._navigation_timeout_ms / 1000.0}s"
                    )

                popup_page = self._click_button_and_capture_popup(
                    bot_page,
                    UNIX_RESULT_BUTTON_SELECTOR,
                    PWTimeoutError=PWTimeoutError,
                )
                time.sleep(self._settle_wait_seconds)
                raw_text, source_url = self._download_text_with_retry(
                    popup_page, PWTimeoutError=PWTimeoutError
                )
                return TelegramConsultResult(
                    provider=self.provider,
                    lead_name=name,
                    query=query,
                    raw_text=raw_text,
                    source_url=source_url,
                    downloaded_at=datetime.now(timezone.utc).isoformat(),
                    error=None,
                )
            finally:
                for tmp in (group_page, bot_page):
                    if tmp is not None:
                        try:
                            tmp.close()
                        except Exception:
                            pass

    # ---- Unix-specific helpers -------------------------------------------

    def _click_button_and_capture_popup(
        self, page: Any, button_selector: str, *, PWTimeoutError: type
    ) -> Any:
        button = page.locator(button_selector).last
        button.wait_for(state="visible")
        try:
            button.scroll_into_view_if_needed()
        except Exception:
            pass
        context = page.context
        with context.expect_page(timeout=self._navigation_timeout_ms) as page_info:
            button.click()
            self._accept_external_link_popup(page, PWTimeoutError=PWTimeoutError)
        popup_page = page_info.value
        popup_page.wait_for_load_state("domcontentloaded")
        try:
            popup_page.bring_to_front()
        except Exception:
            pass
        return popup_page

    def _download_text_with_retry(
        self, page: Any, *, PWTimeoutError: type
    ) -> tuple[str, str | None]:
        button = self._locate_texto_button(page)
        button.wait_for(state="visible", timeout=self._navigation_timeout_ms)
        try:
            button.scroll_into_view_if_needed()
        except Exception:
            pass

        last_filename: str | None = None
        last_error: str | None = None
        for attempt in range(1, self._max_download_attempts + 1):
            try:
                with page.expect_download(
                    timeout=self._download_timeout_ms
                ) as dl_info:
                    button.click()
                download = dl_info.value
            except PWTimeoutError:
                last_error = f"timeout no expect_download (tentativa {attempt})"
                logger.warning(last_error)
                time.sleep(1.0)
                continue

            filename = (download.suggested_filename or "").strip()
            last_filename = filename
            path = download.path()
            if path is None:
                last_error = f"tentativa {attempt}: download iniciado mas sem path"
                logger.warning(last_error)
                time.sleep(1.0)
                continue
            if not self._looks_like_valid_text_download(filename):
                last_error = (
                    f"tentativa {attempt}: filename corrompido {filename!r}"
                )
                logger.warning(
                    "Unix consult: download corrompido na tentativa %s (%r)."
                    " Clicando 'Texto' de novo.",
                    attempt,
                    filename,
                )
                time.sleep(1.0)
                continue
            try:
                with open(path, "rb") as handle:
                    data = handle.read()
            except OSError as exc:
                last_error = f"tentativa {attempt}: falha lendo {path}: {exc}"
                logger.warning(last_error)
                time.sleep(1.0)
                continue
            text = data.decode("utf-8", errors="replace")
            logger.info(
                "Unix consult: .txt válido baixado na tentativa %s (%s).",
                attempt,
                filename,
            )
            return text, page.url

        try:
            body_text = page.locator("body").inner_text(timeout=5_000)
            if body_text and body_text.strip():
                logger.warning(
                    "Unix consult: todas as %s tentativas baixaram blob"
                    " corrompido; caindo no body.inner_text como fallback.",
                    self._max_download_attempts,
                )
                return body_text, page.url
        except Exception:
            pass
        raise RuntimeError(
            f"Não consegui baixar um .txt válido após"
            f" {self._max_download_attempts} tentativas."
            f" Último filename: {last_filename!r}. Último erro: {last_error}."
        )

    def _locate_texto_button(self, page: Any) -> Any:
        try:
            role_locator = page.get_by_role("button", name="Texto", exact=True)
            if role_locator.count() > 0:
                return role_locator.first
        except Exception:
            pass
        text_locator = page.locator("button:visible", has_text="Texto")
        if text_locator.count() > 0:
            return text_locator.first
        return page.locator(
            "button:has-text('Texto'):not(:has-text('Copiar')),"
            " a:has-text('Texto'):not(:has-text('Copiar')),"
            " [role='button']:has-text('Texto'):not(:has-text('Copiar'))"
        ).first

    def _looks_like_valid_text_download(self, filename: str) -> bool:
        """Heuristic: real file is ``nome_resultado_mk_unix_<digits>.txt``;
        corrupted blob is a UUID with no extension."""
        if not filename:
            return False
        name = filename.strip().lower()
        if not name.endswith(".txt"):
            return False
        return "nome_resultado_mk_unix" in name


class GonzalesBotConsult(TelegramGroupConsultBase):
    """Provider 'gon' — ConsultoriaGonzalesbot replies in its private chat with a
    "ver resultado completo" inline button. Lighter than Unix and the
    page is HTML we can scrape directly. See module docstring."""

    provider = "gon"

    def __init__(
        self,
        *,
        scrape_timeout_ms: int = int(TELEGRAM_ACTION_WAIT_SECONDS * 1000),
        # Max time we wait for Gon's reply after sending /nome before
        # giving up. Bumped from 16s to 45s after a real-world bug where
        # the operator saw "Ver resultado completo" rendered in the chat
        # but the polling had already given up. 16s was tight: combined
        # with the new shorter post-send jitter (4.5–6.5s) the total
        # window was ~22s, and Gon occasionally takes longer than that.
        # 45s favours "wait a bit more" over "fail fast" — the worst
        # case is the operator waits 45s before falling back to the
        # next attempt, which is still cheaper than retrying the whole
        # consult manually.
        gon_abort_timeout_seconds: float = 45.0,
        **base_kwargs: Any,
    ) -> None:
        base_kwargs.setdefault("group_url", GONZALES_BOT_URL)
        super().__init__(**base_kwargs)
        self._scrape_timeout_ms = scrape_timeout_ms
        self._gon_abort_timeout_seconds = max(2.0, gon_abort_timeout_seconds)

    def _result_button_selector(self) -> str:
        return GON_RESULT_BUTTON_SELECTOR

    def _consult_via_playwright(
        self, name: str, query: str
    ) -> TelegramConsultResult:
        try:
            from playwright.sync_api import (  # type: ignore[import-not-found]
                TimeoutError as PWTimeoutError,
                sync_playwright,
            )
        except Exception as exc:  # pragma: no cover - depends on optional package
            raise RuntimeError(f"Playwright indisponível: {exc}") from exc

        with sync_playwright() as runtime:
            browser = runtime.chromium.connect_over_cdp(self._cdp_endpoint)
            contexts = list(getattr(browser, "contexts", []) or [])
            if not contexts:
                raise RuntimeError(
                    "Chrome CDP conectado mas sem contexto aberto. "
                    "Abra o Chrome com --remote-debugging-port=9222."
                )
            context = contexts[0]
            group_page: Any = None
            try:
                group_page = context.new_page()
                group_page.goto(self._group_url, wait_until="domcontentloaded")
                group_page.set_default_timeout(self._navigation_timeout_ms)
                time.sleep(self._settle_wait_seconds)
                group_page.locator(
                    "div.input-message-input[contenteditable='true']"
                ).first.wait_for(state="visible")
                if self._should_clear_gonzales_private_chat():
                    self._clear_gonzales_private_chat(group_page)

                # Count initial buttons and bubbles before sending
                selector = self._result_button_selector()
                initial_button_count = group_page.locator(selector).count()
                initial_bubble_count = group_page.locator("div.bubble").count()

                self._send_query_in_group(group_page, query)

                # Gonzales replies inside this same chat. Instead of
                # blocking on the result button for the full navigation
                # timeout, poll for either (a) the button appearing
                # (success) or (b) one of the known Gon failure phrases
                # in the latest bubble (abort fast so the orchestrator
                # can move to Unix). ``_send_query_in_group`` já cobriu
                # o sleep pós-envio (regra global de 16s) — o polling
                # adiciona no máximo ``gon_abort_timeout_seconds`` em
                # cima disso.
                self._wait_for_gon_result_or_abort(group_page, initial_button_count, initial_bubble_count)

                button = self._result_button_for_query(group_page, selector, query)
                try:
                    button.scroll_into_view_if_needed()
                except Exception:
                    pass
                with context.expect_page(
                    timeout=self._navigation_timeout_ms
                ) as page_info:
                    button.click()
                    self._accept_external_link_popup(
                        group_page, PWTimeoutError=PWTimeoutError
                    )
                popup_page = page_info.value
                popup_page.wait_for_load_state("domcontentloaded")
                time.sleep(self._settle_wait_seconds)

                # HTML scrape: pull the visible text from the result page.
                # The parser downstream extracts CPF/name/etc. from it.
                try:
                    body_text = popup_page.locator("body").inner_text(
                        timeout=self._scrape_timeout_ms
                    )
                except Exception:
                    body_text = ""
                if not body_text or not body_text.strip():
                    raise RuntimeError(
                        "Gon: página externa abriu mas body.inner_text veio vazio."
                    )
                source_url = popup_page.url
                return TelegramConsultResult(
                    provider=self.provider,
                    lead_name=name,
                    query=query,
                    raw_text=body_text,
                    source_url=source_url,
                    downloaded_at=datetime.now(timezone.utc).isoformat(),
                    error=None,
                )
            finally:
                if group_page is not None:
                    try:
                        group_page.close()
                    except Exception:
                        pass

    # ---- Gon-specific helpers --------------------------------------------

    def _should_clear_gonzales_private_chat(self) -> bool:
        return "@ConsultoriaGonzalesbot" in self._group_url

    def _clear_gonzales_private_chat(self, page: Any) -> None:
        """Remove visible old Gonzales result bubbles before sending.

        The bot leaves each prior result with a red ``Apagar`` inline
        button. Old result bubbles are exactly what cause the wrong link
        click, so clear the visible private chat first and then take the
        baseline counts for the new query.
        """
        max_rounds = 5
        max_clicks_per_round = 25
        for _round in range(max_rounds):
            try:
                buttons = page.locator(GON_DELETE_BUTTON_SELECTOR)
                count = buttons.count()
            except Exception:
                return
            if count <= 0:
                return
            clicked = 0
            for index in range(min(count, max_clicks_per_round) - 1, -1, -1):
                try:
                    button = buttons.nth(index)
                    button.scroll_into_view_if_needed()
                    button.click()
                    clicked += 1
                    time.sleep(0.2)
                except Exception:
                    continue
            if clicked <= 0:
                return
            time.sleep(0.5)
            try:
                remaining = page.locator(GON_DELETE_BUTTON_SELECTOR).count()
            except Exception:
                return
            if remaining >= count:
                return

    def _result_button_for_query(self, page: Any, selector: str, query: str) -> Any:
        """Return the result button inside the bubble for ``query``.

        Telegram Web can leave several Gonzales result bubbles visible.
        A global ``locator(selector).last`` is not a reliable association
        between the button and the just-sent command because DOM order can
        diverge from the visual order while old messages are still
        mounted. The bot echoes the command at the top of the result
        bubble (``/nome Fulano``), so scope the button lookup to the
        newest bubble whose text contains that exact query and fall back
        to the old global-last behavior only when the echo is absent.
        """
        query_key = self._normalize_query_text(query)
        try:
            bubbles = page.locator("div.bubble")
            total = bubbles.count()
        except Exception:
            total = 0

        for index in range(total - 1, -1, -1):
            try:
                bubble = bubbles.nth(index)
                text = bubble.inner_text(timeout=400) or ""
            except Exception:
                continue
            if query_key not in self._normalize_query_text(text):
                continue
            try:
                scoped = bubble.locator(selector)
                if scoped.count() > 0:
                    return scoped.last
            except Exception:
                continue
        return page.locator(selector).last

    def _normalize_query_text(self, value: str | None) -> str:
        return re.sub(r"\s+", " ", (value or "").strip()).casefold()

    def _wait_for_gon_result_or_abort(self, page: Any, initial_button_count: int, initial_bubble_count: int) -> None:
        """Race the success button against known failure phrases.

        Polls every 500ms for up to ``gon_abort_timeout_seconds``:

        - If a new ``ver resultado completo`` button shows up, return — the
          caller proceeds to click it.
        - If the latest visible bubble contains a known Gon failure
          phrase (e.g. "não encontrado", "uso excessivo"), raise a
          tagged ``RuntimeError``. The outer ``consult`` wrapper
          translates that into a ``TelegramConsultResult`` with the
          error code set, and the orchestrator continues with Unix.
        - If neither happens before the deadline, raise
          ``gon_timeout`` with the final button/bubble counts logged so
          a real race ("the bot DID reply, the poll missed it") is
          distinguishable from "the bot never replied".
        """
        start = time.time()
        deadline = start + self._gon_abort_timeout_seconds
        poll_interval = 0.5
        selector = self._result_button_selector()
        last_button_count = initial_button_count
        iterations = 0
        while time.time() < deadline:
            iterations += 1
            try:
                current_count = page.locator(selector).count()
            except Exception:
                current_count = last_button_count
            if current_count > initial_button_count:
                logger.info(
                    "gon polling: button detected after %.1fs "
                    "(initial=%d, current=%d, iter=%d)",
                    time.time() - start,
                    initial_button_count,
                    current_count,
                    iterations,
                )
                return
            last_button_count = current_count

            last_text = self._latest_bubble_text(page, initial_bubble_count)
            failure = detect_gon_failure(last_text)
            if failure is not None:
                code, phrase = failure
                snippet = (last_text or "").strip().splitlines()
                preview = snippet[0][:140] if snippet else phrase
                raise RuntimeError(f"{code}: {preview}")

            time.sleep(poll_interval)

        # Timeout. Capture the final DOM state so we can tell whether the
        # button was actually missing (real "no reply") or whether the
        # polling missed it (real race — bump the timeout or fix the
        # selector). Without this info every timeout looks the same and
        # the operator has no signal to act on.
        try:
            final_button_count = page.locator(selector).count()
        except Exception:
            final_button_count = -1
        try:
            final_bubble_count = page.locator("div.bubble").count()
        except Exception:
            final_bubble_count = -1
        last_text_at_timeout = self._latest_bubble_text(page, initial_bubble_count)
        last_text_preview = (last_text_at_timeout or "").strip()[:160]
        logger.warning(
            "gon polling TIMEOUT after %.1fs: buttons %d->%d, bubbles %d->%d, "
            "iterations=%d, last_bubble=%r",
            self._gon_abort_timeout_seconds,
            initial_button_count,
            final_button_count,
            initial_bubble_count,
            final_bubble_count,
            iterations,
            last_text_preview,
        )
        raise RuntimeError(
            "gon_timeout: nenhuma resposta do Gon em "
            f"{self._gon_abort_timeout_seconds}s "
            f"(buttons {initial_button_count}->{final_button_count}, "
            f"bubbles {initial_bubble_count}->{final_bubble_count})"
        )

    def _latest_bubble_text(self, page: Any, initial_count: int) -> str:
        """Return the visible text of the most recent message bubble in
        the chat, only if there is a new bubble. Empty string on any locator
        failure or if no new bubble exists."""
        try:
            loc = page.locator("div.bubble")
            cnt = loc.count()
            if cnt > initial_count:
                return loc.last.inner_text(timeout=400)
        except Exception:
            pass
        return ""


class GonzalesCpfConsult(GonzalesBotConsult):
    """Gonzales-bot driver for the ``/cpf <cpf>`` follow-up query.

    Fluxo diferente do ``/nome``:

    1. Manda ``/cpf <cpf>`` no chat privado do Gonzales.
    2. O bot responde com uma mensagem nova que carrega o botão
       **SISREG-III**. Esperamos esse botão aparecer (poll com
       deadline).
    3. Clicamos no SISREG-III — não é link externo, é um botão que
       dispara uma NOVA mensagem do Gonzales no mesmo chat.
    4. Esperamos ~16 segundos (configurável) para o bot escrever a
       consulta SISREG. O bot mostra texto contendo "consulta
       concluída" + o CPF mascarado.
    5. Procuramos o ÚLTIMO bubble do Gonzales que tem "consulta" no
       texto E carrega um CPF (qualquer formato). Dentro DESSE bubble
       específico tem o "ver resultado completo".
    6. Clique no "ver resultado completo" → popup externo → scrape do
       body para extrair o telefone via parser downstream.

    Manter ``detect_gon_failure`` rodando em ambas as etapas (antes do
    SISREG e antes do "ver resultado completo") permite abortar cedo
    quando o bot devolve "uso excessivo" e o orquestrador trocar para
    Unix sem queimar o tempo da automação.

    ``provider`` distinto do ``GonzalesBotConsult`` mantém o cooldown
    escopado — um rate-limit no /cpf não silencia o /nome do mesmo
    bot.
    """

    provider = "gon_cpf"

    def __init__(
        self,
        *,
        sisreg_post_click_wait_seconds: float = TELEGRAM_ACTION_WAIT_SECONDS,
        sisreg_button_timeout_seconds: float = TELEGRAM_ACTION_WAIT_SECONDS,
        consulta_bubble_timeout_seconds: float = TELEGRAM_ACTION_WAIT_SECONDS,
        **base_kwargs: Any,
    ) -> None:
        super().__init__(**base_kwargs)
        self._sisreg_post_click_wait_seconds = max(
            0.0, sisreg_post_click_wait_seconds
        )
        self._sisreg_button_timeout_seconds = max(
            2.0, sisreg_button_timeout_seconds
        )
        self._consulta_bubble_timeout_seconds = max(
            2.0, consulta_bubble_timeout_seconds
        )

    def _build_query(self, value: str) -> str:
        return f"/cpf {value}"

    def _consult_via_playwright(
        self, name: str, query: str
    ) -> TelegramConsultResult:
        try:
            from playwright.sync_api import (  # type: ignore[import-not-found]
                TimeoutError as PWTimeoutError,
                sync_playwright,
            )
        except Exception as exc:  # pragma: no cover - depends on optional package
            raise RuntimeError(f"Playwright indisponível: {exc}") from exc

        with sync_playwright() as runtime:
            browser = runtime.chromium.connect_over_cdp(self._cdp_endpoint)
            contexts = list(getattr(browser, "contexts", []) or [])
            if not contexts:
                raise RuntimeError(
                    "Chrome CDP conectado mas sem contexto aberto. "
                    "Abra o Chrome com --remote-debugging-port=9222."
                )
            context = contexts[0]
            group_page: Any = None
            try:
                group_page = context.new_page()
                group_page.goto(self._group_url, wait_until="domcontentloaded")
                group_page.set_default_timeout(self._navigation_timeout_ms)
                time.sleep(self._settle_wait_seconds)
                group_page.locator(
                    "div.input-message-input[contenteditable='true']"
                ).first.wait_for(state="visible")
                if self._should_clear_gonzales_private_chat():
                    self._clear_gonzales_private_chat(group_page)

                initial_sisreg_count = group_page.locator(
                    GON_SISREG_BUTTON_SELECTOR
                ).count()
                initial_consulta_button_count = group_page.locator(
                    GON_RESULT_BUTTON_SELECTOR
                ).count()
                initial_bubble_count = group_page.locator("div.bubble").count()

                # _send_query_in_group já cobre o sleep pós-envio (16s
                # globais). A mensagem do bot com o menu (SISREG-III /
                # Credilink / CNH / SI-PNI) aparece dentro dessa janela.
                self._send_query_in_group(group_page, query)

                self._wait_for_sisreg_button(
                    group_page,
                    initial_button_count=initial_sisreg_count,
                    initial_bubble_count=initial_bubble_count,
                )

                sisreg_button = group_page.locator(
                    GON_SISREG_BUTTON_SELECTOR
                ).last
                try:
                    sisreg_button.scroll_into_view_if_needed()
                except Exception:
                    pass
                sisreg_button.click()

                # Mesma regra do envio: depois de qualquer ação que
                # dispare resposta do bot, esperar 16s antes de inspecionar
                # o DOM. O Gonzales mostra "Consultando..." durante esse
                # intervalo e só então renderiza a mensagem final.
                time.sleep(self._sisreg_post_click_wait_seconds)

                # Pós-SISREG: o bot manda UMA nova mensagem com a
                # consulta SISREG-III, contendo o botão "ver resultado
                # completo". A pré-mensagem (menu de bases) NÃO carrega
                # esse botão — então uma checagem simples de "a contagem
                # de botões cresceu?" é suficiente, sem precisar parsear
                # texto/CPF do bubble (que pode vir mascarado). Mesmo
                # pattern usado pelo /nome em ``_wait_for_gon_result_or_abort``.
                self._wait_for_post_sisreg_result_button(
                    group_page,
                    initial_button_count=initial_consulta_button_count,
                    initial_bubble_count=initial_bubble_count,
                )

                result_button = group_page.locator(
                    GON_RESULT_BUTTON_SELECTOR
                ).last
                try:
                    result_button.scroll_into_view_if_needed()
                except Exception:
                    pass
                with context.expect_page(
                    timeout=self._navigation_timeout_ms
                ) as page_info:
                    result_button.click()
                    self._accept_external_link_popup(
                        group_page, PWTimeoutError=PWTimeoutError
                    )
                popup_page = page_info.value
                popup_page.wait_for_load_state("domcontentloaded")
                time.sleep(self._settle_wait_seconds)

                try:
                    body_text = popup_page.locator("body").inner_text(
                        timeout=self._scrape_timeout_ms
                    )
                except Exception:
                    body_text = ""
                if not body_text or not body_text.strip():
                    raise RuntimeError(
                        "Gon /cpf: página externa abriu mas body.inner_text veio vazio."
                    )
                return TelegramConsultResult(
                    provider=self.provider,
                    lead_name=name,
                    query=query,
                    raw_text=body_text,
                    source_url=popup_page.url,
                    downloaded_at=datetime.now(timezone.utc).isoformat(),
                    error=None,
                )
            finally:
                if group_page is not None:
                    try:
                        group_page.close()
                    except Exception:
                        pass

    # ---- /cpf specific helpers --------------------------------------------

    def _extract_cpf_from_query(self, query: str) -> str:
        """Devolve só os dígitos do CPF presentes em ``query``.

        Aceitamos máscaras variadas (``/cpf 111.222.333-44``,
        ``/cpf 11122233344``) e devolvemos os 11 dígitos puros, o que
        torna a comparação no bubble robusta a formatação."""
        digits = re.sub(r"\D", "", query or "")
        return digits[-11:] if len(digits) >= 11 else digits

    def _wait_for_sisreg_button(
        self,
        page: Any,
        *,
        initial_button_count: int,
        initial_bubble_count: int,
    ) -> None:
        """Polla até o botão SISREG-III aparecer ou abortar com falha
        conhecida do Gonzales. Espelha :meth:`_wait_for_gon_result_or_abort`
        mas troca o seletor — o /cpf não passa pelo botão "ver
        resultado completo" ainda nessa etapa."""
        deadline = time.time() + self._sisreg_button_timeout_seconds
        poll_interval = 0.5
        while time.time() < deadline:
            try:
                if page.locator(GON_SISREG_BUTTON_SELECTOR).count() > initial_button_count:
                    return
            except Exception:
                pass

            last_text = self._latest_bubble_text(page, initial_bubble_count)
            failure = detect_gon_failure(last_text)
            if failure is not None:
                code, phrase = failure
                snippet = (last_text or "").strip().splitlines()
                preview = snippet[0][:140] if snippet else phrase
                raise RuntimeError(f"{code}: {preview}")

            time.sleep(poll_interval)

        raise RuntimeError(
            "sisreg_timeout: botão SISREG-III não apareceu em "
            f"{self._sisreg_button_timeout_seconds}s após /cpf"
        )

    def _wait_for_post_sisreg_result_button(
        self,
        page: Any,
        *,
        initial_button_count: int,
        initial_bubble_count: int,
    ) -> None:
        """Após clicar no SISREG-III + dormir o post-click fixo, polla
        até a contagem de "ver resultado completo" crescer (= o bot
        terminou de renderizar a consulta SISREG e o botão final está
        visível). Aborta cedo se um bubble de falha conhecida (rate
        limit, não encontrado) aparecer.

        Diferente do matcher antigo, NÃO tenta parsear texto/CPF do
        bubble — o pré-SISREG (menu de bases) não traz "ver resultado
        completo", então a contagem é discriminadora suficiente."""
        deadline = time.time() + self._consulta_bubble_timeout_seconds
        poll_interval = 0.5
        while time.time() < deadline:
            try:
                if page.locator(GON_RESULT_BUTTON_SELECTOR).count() > initial_button_count:
                    return
            except Exception:
                pass

            last_text = self._latest_bubble_text(page, initial_bubble_count)
            if last_text and not _is_loading_bubble(last_text):
                failure = detect_gon_failure(last_text)
                if failure is not None:
                    code, phrase = failure
                    snippet = (last_text or "").strip().splitlines()
                    preview = snippet[0][:140] if snippet else phrase
                    raise RuntimeError(f"{code}: {preview}")

            time.sleep(poll_interval)

        raise RuntimeError(
            "post_sisreg_timeout: botão 'ver resultado completo' não "
            f"apareceu em {self._consulta_bubble_timeout_seconds}s após o "
            "clique no SISREG-III"
        )

    def _wait_for_consulta_bubble_with_cpf(
        self,
        page: Any,
        *,
        cpf: str,
        initial_button_count: int,
        min_bubble_index: int = 0,
    ) -> Any:
        """Encontra o bubble mais recente cujo texto contém 'consulta' e
        o CPF consultado, retornando o ``Locator`` apontando para esse
        bubble — o caller usa o locator para escopar o clique no
        "ver resultado completo".

        ``min_bubble_index`` é a contagem de ``div.bubble`` capturada
        ANTES do clique no SISREG-III. Bubbles em índices abaixo desse
        limite são ignorados — assim a busca nunca acerta o bubble da
        resposta pre-SISREG (que carrega o mesmo "consulta concluída +
        CPF" e levaria a clicar no botão errado).

        Se o bubble correto ainda não chegou (tempo de escrita do bot
        ou loading "Consultando..." ainda visível), polla até
        ``consulta_bubble_timeout_seconds`` extras.
        """
        deadline = time.time() + self._consulta_bubble_timeout_seconds
        poll_interval = 0.5
        while True:
            bubble_locator = self._find_latest_consulta_bubble_with_cpf(
                page, cpf=cpf, min_index=min_bubble_index
            )
            if bubble_locator is not None:
                return bubble_locator

            # Fail-fast em rate-limit/erro detectado pelo Gonzales.
            # Ignora o bubble de loading ("Consultando..."), que aparece
            # e some sozinho antes da resposta real.
            try:
                cnt = page.locator("div.bubble").count()
                if cnt > 0:
                    last_text = page.locator("div.bubble").last.inner_text(
                        timeout=400
                    )
                    if not _is_loading_bubble(last_text):
                        failure = detect_gon_failure(last_text)
                        if failure is not None:
                            code, _phrase = failure
                            raise RuntimeError(
                                f"{code}: {(last_text or '').strip()[:140]}"
                            )
            except RuntimeError:
                raise
            except Exception:
                pass

            if time.time() >= deadline:
                # Como fallback, se ao menos um novo botão "ver resultado
                # completo" surgiu (mesmo que sem CPF detectável no
                # bubble), procuramos o último bubble APÓS o snapshot
                # pre-SISREG que carregue esse botão — pulando o
                # loading transitório.
                try:
                    if (
                        page.locator(GON_RESULT_BUTTON_SELECTOR).count()
                        > initial_button_count
                    ):
                        fallback = self._latest_post_sisreg_bubble(
                            page, min_index=min_bubble_index
                        )
                        if fallback is not None:
                            return fallback
                except Exception:
                    pass
                raise RuntimeError(
                    "consulta_bubble_timeout: nenhuma mensagem com 'consulta' +"
                    f" CPF apareceu em {self._consulta_bubble_timeout_seconds}s"
                )

            time.sleep(poll_interval)

    def _find_latest_consulta_bubble_with_cpf(
        self, page: Any, *, cpf: str, min_index: int = 0
    ) -> Any | None:
        """Itera os bubbles de baixo pra cima e devolve o ``Locator`` do
        primeiro que tenha 'consulta concluída'/'consulta:'/etc. e um
        CPF (com ou sem máscara) que case com ``cpf``.

        Bubbles com texto "Consultando..." (loading transitório) são
        ignorados, assim como qualquer bubble cujo índice seja inferior
        a ``min_index`` (snapshot tirado antes do clique no SISREG-III).
        ``None`` quando nada bate ainda.
        """
        try:
            bubbles = page.locator("div.bubble")
            total = bubbles.count()
        except Exception:
            return None
        if not total or total <= min_index:
            return None

        # Compara apenas pelos 11 dígitos para não depender da máscara.
        target_digits = re.sub(r"\D", "", cpf or "")

        for index in range(total - 1, min_index - 1, -1):
            try:
                bubble = bubbles.nth(index)
                text = bubble.inner_text(timeout=400) or ""
            except Exception:
                continue
            if _is_loading_bubble(text):
                continue
            if not _GON_CONSULTA_LABEL_RE.search(text):
                continue
            matches = _GON_CPF_IN_BUBBLE_RE.findall(text)
            if not matches:
                continue
            if not target_digits:
                # Sem CPF conhecido, qualquer bubble com 'consulta' + CPF serve.
                return bubble
            for match in matches:
                if re.sub(r"\D", "", match) == target_digits:
                    return bubble
        return None

    def _latest_post_sisreg_bubble(
        self, page: Any, *, min_index: int
    ) -> Any | None:
        """Último bubble com índice >= ``min_index`` que NÃO é o loading
        transitório. Usado como fallback quando o matcher de CPF não
        achou nada mas um botão "ver resultado completo" já apareceu —
        protege contra mascaramento exótico do CPF no texto."""
        try:
            bubbles = page.locator("div.bubble")
            total = bubbles.count()
        except Exception:
            return None
        for index in range(total - 1, min_index - 1, -1):
            try:
                bubble = bubbles.nth(index)
                text = bubble.inner_text(timeout=400) or ""
            except Exception:
                continue
            if _is_loading_bubble(text):
                continue
            return bubble
        return None


class FindexEmailConsult(GonzalesBotConsult):
    """Findex/FDX driver for the e-mail -> phone fallback.

    O bot vive em ``@FdxGP_bot`` e aceita ``/email <email>``. A resposta
    fica atrás de um botão "RESULTADO AQUI"; esta classe reusa o fluxo
    HTML-scrape do Gonzales e só troca a URL do chat, o verbo do
    comando, o seletor do botão e a tag de provider.
    """

    provider = "findex"

    def __init__(self, **base_kwargs: Any) -> None:
        base_kwargs.setdefault("group_url", FINDEX_BOT_URL)
        super().__init__(**base_kwargs)

    def _build_query(self, value: str) -> str:
        return f"/email {value}"

    def _result_button_selector(self) -> str:
        return FINDEX_RESULT_BUTTON_SELECTOR


class FindexNameConsult(GonzalesBotConsult):
    """Findex/FDX driver for the experimental ``/nome`` lookup.

    The Telegram side is the same as :class:`FindexEmailConsult`:
    send a command to ``@FdxGP_bot``, click "Resultado Aqui", accept the
    external-link popup. The result page differs: for name lookups the
    operator wants the exported JSON, so this driver clicks
    "Exportar JSON" and persists that file's contents as ``raw_text``.
    If the site does not trigger a download, it falls back to the page's
    visible text so the comparison pipeline still has evidence.
    """

    provider = "finder"

    def __init__(
        self,
        *,
        download_timeout_ms: int = 30_000,
        **base_kwargs: Any,
    ) -> None:
        base_kwargs.setdefault("group_url", FINDEX_BOT_URL)
        super().__init__(**base_kwargs)
        self._download_timeout_ms = download_timeout_ms

    def _build_query(self, value: str) -> str:
        return f"/nome {value}"

    def _consult_via_playwright(
        self, name: str, query: str
    ) -> TelegramConsultResult:
        try:
            popup_page = self._open_findex_result_page(name=name, query=query)
            raw_text = self._export_json_or_body_text(popup_page)
            if not raw_text or not raw_text.strip():
                raise RuntimeError(
                    "Finder /nome: página externa abriu mas não trouxe JSON/texto."
                )
            return TelegramConsultResult(
                provider=self.provider,
                lead_name=name,
                query=query,
                raw_text=raw_text,
                source_url=popup_page.url,
                downloaded_at=datetime.now(timezone.utc).isoformat(),
                error=None,
            )
        finally:
            runtime = getattr(self, "_active_playwright_runtime", None)
            if runtime is not None:
                try:
                    runtime.stop()
                except Exception:
                    pass
                self._active_playwright_runtime = None

    def _open_findex_result_page(self, *, name: str, query: str) -> Any:  # noqa: ARG002
        try:
            from playwright.sync_api import (  # type: ignore[import-not-found]
                TimeoutError as PWTimeoutError,
                sync_playwright,
            )
        except Exception as exc:  # pragma: no cover - depends on optional package
            raise RuntimeError(f"Playwright indisponível: {exc}") from exc

        runtime = sync_playwright().start()
        self._active_playwright_runtime = runtime
        try:
            browser = runtime.chromium.connect_over_cdp(self._cdp_endpoint)
            contexts = list(getattr(browser, "contexts", []) or [])
            if not contexts:
                raise RuntimeError(
                    "Chrome CDP conectado mas sem contexto aberto. "
                    "Abra o Chrome com --remote-debugging-port=9222."
                )
            context = contexts[0]
            group_page = context.new_page()
            try:
                group_page.goto(self._group_url, wait_until="domcontentloaded")
                group_page.set_default_timeout(self._navigation_timeout_ms)
                time.sleep(self._settle_wait_seconds)
                group_page.locator(
                    "div.input-message-input[contenteditable='true']"
                ).first.wait_for(state="visible")

                initial_button_count = group_page.locator(
                    FINDEX_RESULT_BUTTON_SELECTOR
                ).count()
                self._send_query_in_group(group_page, query)
                self._wait_for_findex_result_button(
                    group_page, initial_button_count=initial_button_count
                )

                button = group_page.locator(FINDEX_RESULT_BUTTON_SELECTOR).last
                try:
                    button.scroll_into_view_if_needed()
                except Exception:
                    pass
                with context.expect_page(
                    timeout=self._navigation_timeout_ms
                ) as page_info:
                    button.click()
                    self._accept_external_link_popup(
                        group_page, PWTimeoutError=PWTimeoutError
                    )
                popup_page = page_info.value
                popup_page.wait_for_load_state("domcontentloaded")
                time.sleep(self._settle_wait_seconds)
                # Keep popup_page alive for the caller; close only the
                # Telegram tab. The Playwright runtime remains attached
                # until the returned page is garbage-collected.
                return popup_page
            finally:
                try:
                    group_page.close()
                except Exception:
                    pass
        except Exception:
            try:
                runtime.stop()
            except Exception:
                pass
            raise

    def _wait_for_findex_result_button(
        self, page: Any, *, initial_button_count: int
    ) -> None:
        deadline = time.time() + TELEGRAM_ACTION_WAIT_SECONDS
        while time.time() < deadline:
            try:
                if page.locator(FINDEX_RESULT_BUTTON_SELECTOR).count() > initial_button_count:
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError("finder_timeout: botão Resultado Aqui não apareceu")

    def _export_json_or_body_text(self, page: Any) -> str:
        button = page.locator(
            "button:has-text('Exportar JSON'),"
            " a:has-text('Exportar JSON'),"
            " [role='button']:has-text('Exportar JSON')"
        ).last
        try:
            button.wait_for(state="visible", timeout=5_000)
            try:
                button.scroll_into_view_if_needed()
            except Exception:
                pass
            with page.expect_download(timeout=self._download_timeout_ms) as dl_info:
                button.click()
            download = dl_info.value
            path = download.path()
            if path is not None:
                with open(path, "rb") as handle:
                    return handle.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        try:
            return page.locator("body").inner_text(timeout=self._scrape_timeout_ms)
        except Exception:
            return ""


class FindexCpfConsult(FindexNameConsult):
    """Findex/FDX driver for the experimental ``/cpf`` lookup.

    The result page's primary action is "Copiar Tudo"; after clicking it
    we read the clipboard when available and fall back to body text. Any
    image URLs visible in the result page are downloaded under
    ``data/telegram_artifacts/findex`` and appended to ``raw_text`` as
    an audit trail.
    """

    provider = "finder_cpf"

    def _build_query(self, value: str) -> str:
        return f"/cpf {value}"

    def _consult_via_playwright(
        self, name: str, query: str
    ) -> TelegramConsultResult:
        try:
            popup_page = self._open_findex_result_page(name=name, query=query)
            raw_text = self._copy_all_or_body_text(popup_page)
            photo_paths = self._download_visible_images(popup_page, lead_name=name)
            if photo_paths:
                raw_text = (
                    (raw_text or "").rstrip()
                    + "\n\n=== finder_fotos_salvas ===\n"
                    + "\n".join(str(path) for path in photo_paths)
                )
            if not raw_text or not raw_text.strip():
                raise RuntimeError(
                    "Finder /cpf: página externa abriu mas não trouxe texto."
                )
            return TelegramConsultResult(
                provider=self.provider,
                lead_name=name,
                query=query,
                raw_text=raw_text,
                source_url=popup_page.url,
                downloaded_at=datetime.now(timezone.utc).isoformat(),
                error=None,
            )
        finally:
            runtime = getattr(self, "_active_playwright_runtime", None)
            if runtime is not None:
                try:
                    runtime.stop()
                except Exception:
                    pass
                self._active_playwright_runtime = None

    def _copy_all_or_body_text(self, page: Any) -> str:
        button = page.locator(
            "button:has-text('Copiar Tudo'),"
            " a:has-text('Copiar Tudo'),"
            " [role='button']:has-text('Copiar Tudo'),"
            " button:has-text('Copiar tudo'),"
            " a:has-text('Copiar tudo'),"
            " [role='button']:has-text('Copiar tudo')"
        ).last
        try:
            button.wait_for(state="visible", timeout=5_000)
            try:
                button.scroll_into_view_if_needed()
            except Exception:
                pass
            button.click()
            time.sleep(0.5)
            copied = page.evaluate("navigator.clipboard && navigator.clipboard.readText ? navigator.clipboard.readText() : ''")
            if copied:
                return str(copied)
        except Exception:
            pass
        try:
            return page.locator("body").inner_text(timeout=self._scrape_timeout_ms)
        except Exception:
            return ""

    def _download_visible_images(self, page: Any, *, lead_name: str) -> list[Path]:
        try:
            sources = page.locator("img").evaluate_all(
                "(imgs) => imgs.map((img) => img.src).filter(Boolean)"
            )
        except Exception:
            return []
        if not isinstance(sources, list):
            return []
        out_dir = Path("data") / "telegram_artifacts" / "findex"
        out_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", lead_name or "lead").strip("_")
        saved: list[Path] = []
        for index, src in enumerate(sources, start=1):
            if not isinstance(src, str) or not src.strip():
                continue
            try:
                response = page.context.request.get(src, timeout=10_000)
                if not response.ok:
                    continue
                content_type = response.headers.get("content-type", "")
                ext = ".jpg"
                if "png" in content_type:
                    ext = ".png"
                elif "webp" in content_type:
                    ext = ".webp"
                path = out_dir / (
                    f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
                    f"_{safe_name or 'lead'}_{index}{ext}"
                )
                path.write_bytes(response.body())
                saved.append(path)
            except Exception:
                continue
        return saved


class TelegramConsultOrchestrator:
    """Runs Gon first, then Unix, returning both results per lead.

    Sequential by design — the providers share the same Chrome window
    and Telegram session; running them in parallel would corrupt tab
    state. Gon is cheap and structured; Unix is the heavier fallback
    with file downloads.

    The orchestrator exposes ``provider_names`` and ``consult_provider``
    so the pipeline layer can dispatch one provider at a time and skip
    providers currently in cooldown. ``consult(lead_name)`` keeps the
    original list-returning shape for backwards-compatible callers and
    tests.
    """

    def __init__(
        self,
        *,
        gon: GonzalesBotConsult,
        unix: UnixBotConsult,
        finder: FindexNameConsult | None = None,
    ) -> None:
        self._gon = gon
        self._unix = unix
        self._finder = finder
        self._by_name: dict[str, TelegramGroupConsultBase] = {
            self._gon.provider: self._gon,
            self._unix.provider: self._unix,
        }
        if self._finder is not None:
            self._by_name[self._finder.provider] = self._finder

    @property
    def provider_names(self) -> tuple[str, ...]:
        # Gon runs first per the operator's spec; do not reorder without
        # also revisiting the matcher's preference for the cheaper /
        # structured provider's candidates.
        if self._finder is not None:
            return (self._finder.provider, self._gon.provider, self._unix.provider)
        return (self._gon.provider, self._unix.provider)

    def consult_provider(
        self, provider: str, lead_name: str
    ) -> TelegramConsultResult:
        target = self._by_name.get(provider)
        if target is None:
            return TelegramConsultResult(
                provider=provider,
                lead_name=lead_name,
                query="",
                raw_text=None,
                source_url=None,
                downloaded_at=None,
                error=f"unknown_provider:{provider}",
            )
        return target.consult(lead_name)

    def consult(self, lead_name: str) -> list[TelegramConsultResult]:
        results: list[TelegramConsultResult] = []
        for provider in self.provider_names:
            results.append(self.consult_provider(provider, lead_name))
        return results


# Backwards-compat alias: the v1 API only had a single class named
# ``TelegramGroupPlaywrightLookup`` that did the Unix flow. Tests and
# external callers still import that name — keep it pointing at the
# Unix subclass so legacy imports keep working.
TelegramGroupPlaywrightLookup = UnixBotConsult
