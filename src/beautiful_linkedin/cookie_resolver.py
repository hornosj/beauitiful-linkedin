"""Resolve the LinkedIn ``li_at`` cookie from explicit input, env or browser.

Public surface:

- :func:`resolve_linkedin_li_at_cookie` returns the cookie value or ``None``.
- :func:`diagnose_li_at_cookie` returns a :class:`CookieDiagnostic` describing
  which step found the cookie (or which steps were tried unsuccessfully). The
  Electron UI hits this via ``/diagnostics/cookie`` so the user can see
  exactly why detection failed (ABE, no LinkedIn login, locked profile, etc.).


Resolution order:

1. ``explicit_cookie`` argument (``"li_at=...; bcookie=..."`` or raw value).
2. Env vars: ``LINKEDIN_LI_AT_COOKIE``, ``LINKEDIN_COOKIE``, ``LI_AT``.
3. Browser cookie store, in priority order:
   - When ``browser="auto"`` on Windows: the user's default browser is tried
     first (read from the registry), then a stable fallback list.
   - When ``browser`` is a specific name: only that browser is tried.
   - When ``browser`` is ``"none"``/``"manual"``/``"off"``: skipped entirely.

Two browser backends are tried per browser:

a. ``rookiepy`` — Rust binding that handles modern Chrome/Edge encryption,
   including the AppBound Encryption (ABE) introduced in Chrome 127. Strongly
   preferred. Optional dep, install with ``pip install rookiepy``.
b. ``browser_cookie3`` — pure-Python fallback. Works for Firefox always; for
   Chrome/Edge it stopped working on Windows ~Chrome 127 because of ABE.

If both fail, a single warning is emitted with concrete remediation hints
instead of a silent ``None``.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from http.cookiejar import CookieJar

logger = logging.getLogger(__name__)

ENV_COOKIE_NAMES = (
    "LINKEDIN_LI_AT_COOKIE",
    "LINKEDIN_COOKIE",
    "LI_AT",
)

LINKEDIN_COOKIE_DOMAINS: tuple[str, ...] = (
    ".linkedin.com",
    "www.linkedin.com",
    "linkedin.com",
)

KNOWN_BROWSERS: tuple[str, ...] = ("chrome", "edge", "brave", "firefox", "opera")


# Windows registry ProgId -> friendly name.
# Keys are matched as case-insensitive prefixes against the ProgId returned by
# the registry (e.g. ``FirefoxURL-308046B0AF4A39CB`` should map to firefox).
_PROGID_PREFIX_TO_BROWSER: tuple[tuple[str, str], ...] = (
    ("ChromeHTML", "chrome"),
    ("ChromeSSHTM", "chrome"),
    ("Google Chrome", "chrome"),
    ("MSEdgeHTM", "edge"),
    ("AppXq0fevzme2pys62n3e0fbqa7peapykr8v", "edge"),
    ("BraveHTML", "brave"),
    ("Brave", "brave"),
    ("FirefoxURL", "firefox"),
    ("FirefoxHTML", "firefox"),
    ("Mozilla Firefox", "firefox"),
    ("OperaStable", "opera"),
    ("Opera", "opera"),
)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def extract_li_at_cookie_value(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().strip('"').strip("'")
    if not cleaned:
        return None

    match = re.search(r"(?:^|;\s*)li_at=([^;]+)", cleaned, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip().strip('"').strip("'") or None
    return cleaned


def resolve_linkedin_li_at_cookie(
    explicit_cookie: str | None = None,
    browser: str = "auto",
) -> str | None:
    explicit = extract_li_at_cookie_value(explicit_cookie)
    if explicit and explicit.lower() != "auto":
        return explicit

    for env_name in ENV_COOKIE_NAMES:
        cookie = extract_li_at_cookie_value(os.getenv(env_name))
        if cookie:
            return cookie

    if browser.strip().lower() in {"none", "manual", "off", "false", "0"}:
        return None

    cookie = _resolve_from_browsers(browser)
    if cookie is None:
        _log_no_cookie_hint(browser)
    return cookie


# ---------------------------------------------------------------------------
# Browser-priority resolution
# ---------------------------------------------------------------------------


def resolve_browser_priority(
    requested: str,
    default_detector: Callable[[], str | None] | None = None,
) -> list[str]:
    """Return the ordered browser list to try, given the requested choice.

    Public so it can be unit-tested without touching the registry.
    """
    requested = (requested or "auto").strip().lower()
    if requested != "auto" and requested in KNOWN_BROWSERS:
        return [requested]

    detector = default_detector or _detect_default_browser
    default = detector()

    ordered: list[str] = []
    if default and default in KNOWN_BROWSERS:
        ordered.append(default)
    for name in KNOWN_BROWSERS:
        if name not in ordered:
            ordered.append(name)
    return ordered


def _detect_default_browser() -> str | None:
    if sys.platform != "win32":
        return None
    try:
        return _detect_windows_default_browser()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Falha ao detectar navegador padrão do Windows: %s", exc)
        return None


def _detect_windows_default_browser() -> str | None:
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return None

    try:
        with winreg.OpenKey(  # type: ignore[attr-defined]
            winreg.HKEY_CURRENT_USER,  # type: ignore[attr-defined]
            r"Software\Microsoft\Windows\Shell\Associations\URLAssociations\https\UserChoice",
        ) as key:
            progid, _ = winreg.QueryValueEx(key, "ProgId")  # type: ignore[attr-defined]
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.debug("Não consegui ler UserChoice de https: %s", exc)
        return None

    return _windows_default_browser_from_progid(str(progid))


def _windows_default_browser_from_progid(progid: str | None) -> str | None:
    if not progid:
        return None
    pid = progid.strip()
    if not pid:
        return None
    for prefix, name in _PROGID_PREFIX_TO_BROWSER:
        if pid.lower().startswith(prefix.lower()):
            return name
    return None


# ---------------------------------------------------------------------------
# Backend dispatch
# ---------------------------------------------------------------------------


def _resolve_from_browsers(browser: str) -> str | None:
    ordered = resolve_browser_priority(browser, _detect_default_browser)
    if not ordered:
        return None

    rookiepy_failures: list[str] = []
    for name in ordered:
        try:
            cookies = _read_with_rookiepy(name, list(LINKEDIN_COOKIE_DOMAINS))
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("rookiepy falhou em %s: %s", name, exc)
            cookies = []
            rookiepy_failures.append(name)
        cookie = _li_at_from_dicts(cookies)
        if cookie:
            logger.info("Cookie li_at encontrado via rookiepy (%s).", name)
            return cookie

    for name in ordered:
        try:
            cookie = _read_with_browser_cookie3(name)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("browser_cookie3 falhou em %s: %s", name, exc)
            cookie = None
        if cookie:
            logger.info("Cookie li_at encontrado via browser_cookie3 (%s).", name)
            return cookie

    return None


def _read_with_rookiepy(browser_name: str, domains: list[str]) -> list[dict]:
    try:
        import rookiepy  # type: ignore[import-not-found]
    except ImportError:
        return []
    reader = getattr(rookiepy, browser_name, None)
    if not callable(reader):
        return []
    raw = reader(domains)
    return list(raw or [])


def _read_with_browser_cookie3(browser_name: str) -> str | None:
    try:
        import browser_cookie3  # type: ignore[import-not-found]
    except ImportError:
        return None
    reader = getattr(browser_cookie3, browser_name, None)
    if not callable(reader):
        return None
    return _li_at_from_cookiejar(reader(domain_name=".linkedin.com"))


# ---------------------------------------------------------------------------
# Cookie-shape helpers
# ---------------------------------------------------------------------------


def _li_at_from_dicts(cookies: Iterable[dict]) -> str | None:
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "").strip()
        if name != "li_at":
            continue
        value = str(cookie.get("value") or "").strip().strip('"').strip("'")
        if value:
            return value
    return None


def _li_at_from_cookiejar(cookiejar: Iterable[object]) -> str | None:
    for cookie in cookiejar:
        name = str(getattr(cookie, "name", ""))
        value = str(getattr(cookie, "value", ""))
        if name == "li_at" and value:
            return value
    return None


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


@dataclass
class BackendAttempt:
    backend: str  # "rookiepy" or "browser_cookie3"
    browser: str
    found: bool
    error: str | None = None


@dataclass
class CookieDiagnostic:
    found: bool
    source: str  # "explicit", "env:LINKEDIN_LI_AT_COOKIE", "rookiepy:chrome", "browser_cookie3:edge", "missing"
    browser_priority: list[str] = field(default_factory=list)
    default_browser: str | None = None
    rookiepy_available: bool = False
    browser_cookie3_available: bool = False
    attempts: list[BackendAttempt] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    preview: str | None = None  # e.g. "AQED…wxyz"


def diagnose_li_at_cookie(
    explicit_cookie: str | None = None,
    browser: str = "auto",
) -> CookieDiagnostic:
    """Run the same resolution logic and return a structured trace of what happened.

    This is the function the Electron sidecar exposes via
    ``/diagnostics/cookie`` so the UI can show *why* detection failed instead
    of a vague "not found".
    """
    explicit = extract_li_at_cookie_value(explicit_cookie)
    if explicit and explicit.lower() != "auto":
        return CookieDiagnostic(
            found=True,
            source="explicit",
            preview=_mask_cookie(explicit),
        )

    for env_name in ENV_COOKIE_NAMES:
        cookie = extract_li_at_cookie_value(os.getenv(env_name))
        if cookie:
            return CookieDiagnostic(
                found=True,
                source=f"env:{env_name}",
                preview=_mask_cookie(cookie),
            )

    diagnostic = CookieDiagnostic(
        found=False,
        source="missing",
        rookiepy_available=_module_available("rookiepy"),
        browser_cookie3_available=_module_available("browser_cookie3"),
    )

    if browser.strip().lower() in {"none", "manual", "off", "false", "0"}:
        diagnostic.hints.append(
            "Detecção automática está desligada (browser=none). Configure LINKEDIN_LI_AT_COOKIE."
        )
        return diagnostic

    diagnostic.default_browser = _detect_default_browser()
    diagnostic.browser_priority = resolve_browser_priority(
        browser, lambda: diagnostic.default_browser
    )

    for name in diagnostic.browser_priority:
        try:
            cookies = _read_with_rookiepy(name, list(LINKEDIN_COOKIE_DOMAINS))
            cookie = _li_at_from_dicts(cookies)
            diagnostic.attempts.append(
                BackendAttempt(backend="rookiepy", browser=name, found=bool(cookie))
            )
            if cookie:
                diagnostic.found = True
                diagnostic.source = f"rookiepy:{name}"
                diagnostic.preview = _mask_cookie(cookie)
                return diagnostic
        except Exception as exc:  # pragma: no cover - defensive
            diagnostic.attempts.append(
                BackendAttempt(backend="rookiepy", browser=name, found=False, error=str(exc))
            )

    for name in diagnostic.browser_priority:
        try:
            cookie = _read_with_browser_cookie3(name)
            diagnostic.attempts.append(
                BackendAttempt(
                    backend="browser_cookie3", browser=name, found=bool(cookie)
                )
            )
            if cookie:
                diagnostic.found = True
                diagnostic.source = f"browser_cookie3:{name}"
                diagnostic.preview = _mask_cookie(cookie)
                return diagnostic
        except Exception as exc:  # pragma: no cover - defensive
            diagnostic.attempts.append(
                BackendAttempt(
                    backend="browser_cookie3",
                    browser=name,
                    found=False,
                    error=str(exc),
                )
            )

    diagnostic.hints = _build_hints(diagnostic)
    return diagnostic


def _build_hints(diagnostic: CookieDiagnostic) -> list[str]:
    hints: list[str] = []
    hints.append(
        "Faça login em https://linkedin.com no navegador escolhido. O cookie li_at só existe se você estiver logado."
    )
    if not diagnostic.rookiepy_available:
        hints.append(
            "Instale o backend que lida com Chrome 127+ AppBound Encryption: `pip install rookiepy`."
        )
    if sys.platform == "win32" and diagnostic.rookiepy_available:
        hints.append(
            "No Chrome 130+ (Windows) o cookie só é decifrável por processo elevado. Rode o app como Administrador uma vez para validar."
        )
    if diagnostic.default_browser is None and sys.platform == "win32":
        hints.append(
            "Não consegui detectar seu navegador padrão. Selecione manualmente em Conta > Navegador do cookie."
        )
    hints.append(
        "Como alternativa: copie o valor de `li_at` (DevTools > Application > Cookies) e cole em Conta > Cookie li_at."
    )
    return hints


def _module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def _mask_cookie(value: str) -> str:
    if len(value) <= 8:
        return "…"
    return f"{value[:4]}…{value[-4:]}"


def _log_no_cookie_hint(browser: str) -> None:
    """Emit a single warning explaining likely causes and remediations."""
    is_windows = sys.platform == "win32"

    hints: list[str] = []
    hints.append(
        "1) Faça login em https://linkedin.com no navegador escolhido (li_at só existe se você estiver logado)."
    )
    if is_windows:
        hints.append(
            "2) Chrome/Edge 127+ no Windows criptografa cookies com AppBound Encryption (ABE). "
            "Instale o backend que lida com isso: `pip install rookiepy`. "
            "Em Chrome 130+ pode pedir admin."
        )
    hints.append(
        "3) Como alternativa, copie o valor de `li_at` (DevTools > Application > Cookies) "
        "para a variável de ambiente LINKEDIN_LI_AT_COOKIE no .env."
    )
    hints.append(
        "4) Para forçar um navegador específico, use `--linkedin-cookie-browser chrome|edge|brave|firefox`."
    )

    logger.warning(
        "Cookie li_at não encontrado automaticamente (browser=%s). Possíveis causas: %s",
        browser,
        " ".join(hints),
    )
