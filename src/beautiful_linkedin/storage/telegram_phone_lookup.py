"""Telegram bot-backed phone lookup provider.

This adapter treats a Telegram bot conversation as another
``PhoneLookupProvider``. The current production target is
``@ConsultoriaGonzalesbot`` whose query format is:

    /nome Nome Completo

The provider is deliberately small and fail-soft: Telegram I/O errors,
missing Telethon, login/session problems, and unparsable responses all
return ``[]`` and append a diagnostic line to ``errors``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from beautiful_linkedin.storage.phone_harvester import (
    _PHONE_REGEX,
    _digits_only,
    _is_plausible_phone,
)
from beautiful_linkedin.storage.phone_lookup import LookupQuery, PhoneCandidate


logger = logging.getLogger(__name__)


TelegramRequester = Callable[[str], str]


class TelegramBotPhoneLookupProvider:
    """Lookup phones by sending ``/nome <full name>`` to a Telegram bot."""

    name = "consultoria_gonzales_bot"

    def __init__(
        self,
        *,
        api_id: int | str | None = None,
        api_hash: str | None = None,
        session_name: str = "data/telegram_phone_lookup",
        bot_username: str = "@ConsultoriaGonzalesbot",
        timeout_seconds: float = 60.0,
        requester: TelegramRequester | None = None,
    ) -> None:
        self.errors: list[str] = []
        self._api_id = self._coerce_api_id(api_id)
        self._api_hash = (api_hash or "").strip() or None
        self._session_name = session_name
        self._bot_username = _normalize_bot_username(bot_username)
        self._timeout_seconds = max(5.0, timeout_seconds)
        self._requester = requester

    def lookup(self, query: LookupQuery) -> list[PhoneCandidate]:
        full_name = (query.full_name or "").strip()
        if not full_name:
            return []

        message = f"/nome {full_name}"
        try:
            response = (
                self._requester(message)
                if self._requester is not None
                else self._request_via_telethon(message)
            )
        except Exception as exc:
            self.errors.append(f"{self.name}:{type(exc).__name__}:{exc}")
            logger.debug("Telegram lookup falhou para %s: %s", full_name, exc)
            return []

        return self._candidates_from_response(response, query, message)

    def _request_via_telethon(self, message: str) -> str:
        if self._api_id is None or not self._api_hash:
            raise RuntimeError("telegram_not_configured")
        # ``asyncio.run`` cannot run inside an already-running loop.
        # The current FastAPI sync routes execute in a worker thread, but
        # this message is clearer if that assumption changes.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._request_via_telethon_async(message))
        raise RuntimeError("telegram_lookup_requires_sync_context")

    def _coerce_api_id(self, value: int | str | None) -> int | None:
        if value in {None, ""}:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            self.errors.append(f"{self.name}:invalid_api_id")
            return None

    async def _request_via_telethon_async(self, message: str) -> str:
        try:
            from telethon import TelegramClient  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - depends on optional package
            raise RuntimeError("telethon_not_installed") from exc

        async with TelegramClient(
            self._session_name,
            self._api_id,
            self._api_hash,
        ) as client:
            async with client.conversation(
                self._bot_username,
                timeout=self._timeout_seconds,
            ) as conv:
                await conv.send_message(message)
                response = await conv.get_response()
                return getattr(response, "raw_text", "") or ""

    def _candidates_from_response(
        self,
        response: str | None,
        query: LookupQuery,
        message: str,
    ) -> list[PhoneCandidate]:
        if not response:
            return []

        seen_digits: set[str] = set()
        out: list[PhoneCandidate] = []
        for match in _PHONE_REGEX.finditer(response):
            raw = match.group(1).strip()
            digits = _digits_only(raw)
            if not _is_plausible_phone(digits):
                continue
            if digits in seen_digits:
                continue
            seen_digits.add(digits)
            out.append(
                PhoneCandidate(
                    raw=raw,
                    source=self.name,
                    source_url=f"https://t.me/{self._bot_username.lstrip('@')}",
                    context="telegram_bot_name_match",
                    confidence_hint=90,
                    extra={
                        "bot": self._bot_username,
                        "query": message,
                        "company": query.company_name,
                    },
                )
            )
        return out


def _normalize_bot_username(value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return "@ConsultoriaGonzalesbot"
    return cleaned if cleaned.startswith("@") else f"@{cleaned}"
