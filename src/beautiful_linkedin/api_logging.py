"""Helpers for logging API failures with rich context.

Every API provider and search engine in this project hits an external HTTP
endpoint that can fail. The default `httpx` exception messages are not very
informative on their own (status code only, no body), so this module wraps the
two most common failure modes — ``HTTPStatusError`` and ``TransportError`` —
with helpers that emit a single, structured log line containing:

- the provider/engine name (``Serper``, ``PDL``...),
- the HTTP method and URL hit,
- the status code (when available),
- a short excerpt of the response body (first ~500 chars), so HTTP 401 / 422
  responses immediately reveal whether the API key is invalid or the payload is
  malformed,
- optional context (e.g. company name being searched).

The helpers are deliberately small and side-effect-only: they do not raise, do
not modify the response, and always return ``None``. Call sites still decide
what to return to the caller (usually an empty list / dict).
"""

from __future__ import annotations

import logging

import httpx

MAX_BODY_EXCERPT = 500


def log_http_error(
    logger: logging.Logger,
    *,
    provider: str,
    error: httpx.HTTPStatusError,
    context: str | None = None,
) -> None:
    """Log an HTTP error with status, URL, and response body excerpt."""

    response = error.response
    request = response.request
    body_excerpt = _excerpt_body(response)
    suffix = _format_context_suffix(context)
    logger.warning(
        "%s retornou HTTP %s em %s %s%s. Resposta: %s",
        provider,
        response.status_code,
        request.method,
        request.url,
        suffix,
        body_excerpt or "<corpo vazio>",
    )


def log_transport_error(
    logger: logging.Logger,
    *,
    provider: str,
    error: Exception,
    endpoint: str | None = None,
    context: str | None = None,
) -> None:
    """Log a network/serialization failure with provider name and endpoint."""

    suffix = _format_context_suffix(context)
    target = f" em {endpoint}" if endpoint else ""
    error_type = type(error).__name__
    message = str(error) or "(sem detalhes)"
    logger.warning(
        "Requisição %s falhou%s%s. %s: %s",
        provider,
        target,
        suffix,
        error_type,
        message,
    )


def log_unexpected_error(
    logger: logging.Logger,
    *,
    provider: str,
    error: Exception,
    endpoint: str | None = None,
    context: str | None = None,
) -> None:
    """Log an unexpected exception during an API call.

    Use for ``except Exception`` boundaries that should not surface to the user
    but still need diagnostics. ``exc_info=True`` ensures the traceback is
    written to the logs for debugging.
    """

    suffix = _format_context_suffix(context)
    target = f" em {endpoint}" if endpoint else ""
    logger.warning(
        "Erro inesperado em %s%s%s: %s",
        provider,
        target,
        suffix,
        error,
        exc_info=True,
    )


def _excerpt_body(response: httpx.Response) -> str:
    try:
        text = response.text
    except Exception:  # pragma: no cover - extremely rare
        return "<corpo ilegível>"
    text = text.strip()
    if not text:
        return ""
    if len(text) <= MAX_BODY_EXCERPT:
        return text
    return f"{text[:MAX_BODY_EXCERPT]}… (+{len(text) - MAX_BODY_EXCERPT} chars)"


def _format_context_suffix(context: str | None) -> str:
    if not context:
        return ""
    return f" [{context}]"


def truncate(value: str, max_length: int) -> str:
    """Return a single-line excerpt of ``value`` capped at ``max_length`` chars."""
    cleaned = " ".join((value or "").split())
    if len(cleaned) <= max_length:
        return cleaned
    return f"{cleaned[:max_length]}…"


def format_http_error_short(stage: str, exc: httpx.HTTPStatusError) -> str:
    """Produce a one-line diagnostic string for an HTTP error.

    Used by providers to populate ProviderDiagnostic.last_error so the renderer
    can surface "401 Unauthorized" or "422 Invalid payload" instead of the
    generic "engines bloqueados" guess.
    """
    response = exc.response
    code = response.status_code
    body = (response.text or "").strip()
    if len(body) > 160:
        body = body[:160] + "…"
    return f"{stage} HTTP {code}" + (f": {body}" if body else "")


def format_transport_error_short(stage: str, exc: Exception) -> str:
    return f"{stage} {type(exc).__name__}: {exc}"
