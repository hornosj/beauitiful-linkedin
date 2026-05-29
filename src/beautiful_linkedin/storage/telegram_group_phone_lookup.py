"""Telegram group-backed phone lookup provider.

Same shape as :mod:`telegram_phone_lookup` but targets a public *group*
(e.g. ``t.me/CONSULTASGRATIS4NV``) instead of a 1:1 bot conversation.
The flow is:

1. Send ``/nome <full name>`` to the configured group.
2. Wait ``capture_seconds`` (default 30s) collecting every new message
   posted in the group during that window.
3. Concatenate the captured texts, extract phones via the shared regex,
   and return them as ``PhoneCandidate``s.

Group automation is riskier than bot DMs — flooding can earn a Telegram
ban on the operator account. A module-level throttle gates consecutive
sends to ``throttle_seconds`` (default 5s) regardless of how many leads
the orchestrator runs in parallel.

Same fail-soft contract as the rest of Bucket B/C: every I/O or parse
exception lands in ``errors`` and the method returns ``[]``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable

from beautiful_linkedin.storage.phone_harvester import (
    _PHONE_REGEX,
    _digits_only,
    _is_plausible_phone,
)
from beautiful_linkedin.storage.phone_lookup import LookupQuery, PhoneCandidate


logger = logging.getLogger(__name__)


GroupRequester = Callable[[str], list[str]]
"""Test seam: takes the message to send, returns the captured replies."""


_throttle_lock = threading.Lock()
_last_send_ts: float = 0.0


def _wait_for_throttle(min_gap_seconds: float) -> None:
    """Block the current thread until ``min_gap_seconds`` has passed
    since any other instance last sent. Shared across all provider
    instances so parallel lead workers don't collide."""
    global _last_send_ts
    if min_gap_seconds <= 0:
        return
    with _throttle_lock:
        now = time.monotonic()
        delta = now - _last_send_ts
        if delta < min_gap_seconds:
            time.sleep(min_gap_seconds - delta)
        _last_send_ts = time.monotonic()


class TelegramGroupPhoneLookupProvider:
    """Lookup phones by posting ``/nome <full name>`` in a Telegram group
    and harvesting every message that arrives during a capture window."""

    name = "telegram_group_consultasgratis"
    command = "/nome"
    context = "telegram_group_name_match"
    confidence_hint = 70

    def __init__(
        self,
        *,
        api_id: int | str | None = None,
        api_hash: str | None = None,
        session_name: str = "data/telegram_group_phone_lookup",
        group_username: str = "@CONSULTASGRATIS4NV",
        capture_seconds: float = 30.0,
        throttle_seconds: float = 5.0,
        requester: GroupRequester | None = None,
    ) -> None:
        self.errors: list[str] = []
        self._api_id = self._coerce_api_id(api_id)
        self._api_hash = (api_hash or "").strip() or None
        self._session_name = session_name
        self._group_username = _normalize_group_username(group_username)
        self._capture_seconds = max(2.0, capture_seconds)
        self._throttle_seconds = max(0.0, throttle_seconds)
        self._requester = requester

    def lookup(self, query: LookupQuery) -> list[PhoneCandidate]:
        full_name = (query.full_name or "").strip()
        if not full_name:
            return []

        message = f"{self.command} {full_name}"

        _wait_for_throttle(self._throttle_seconds)

        try:
            responses = (
                self._requester(message)
                if self._requester is not None
                else self._request_via_telethon(message)
            )
        except Exception as exc:
            self.errors.append(f"{self.name}:{type(exc).__name__}:{exc}")
            logger.debug("Telegram group lookup falhou para %s: %s", full_name, exc)
            return []

        return self._candidates_from_responses(responses, query, message)

    def _coerce_api_id(self, value: int | str | None) -> int | None:
        if value in {None, ""}:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            self.errors.append(f"{self.name}:invalid_api_id")
            return None

    def _request_via_telethon(self, message: str) -> list[str]:
        if self._api_id is None or not self._api_hash:
            raise RuntimeError("telegram_not_configured")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._request_via_telethon_async(message))
        # Same constraint as the bot provider: FastAPI sync routes run in
        # a worker thread so no loop is live; if that ever changes the
        # error message tells future-maintainers what broke.
        raise RuntimeError("telegram_group_lookup_requires_sync_context")

    async def _request_via_telethon_async(self, message: str) -> list[str]:
        try:
            from telethon import TelegramClient, events  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - depends on optional package
            raise RuntimeError("telethon_not_installed") from exc

        captured: list[str] = []

        async with TelegramClient(
            self._session_name,
            self._api_id,
            self._api_hash,
        ) as client:
            entity = await client.get_entity(self._group_username)

            @client.on(events.NewMessage(chats=entity))
            async def _collect(event):  # pragma: no cover - exercised via integration
                text = getattr(event.message, "raw_text", None) or getattr(
                    event.message, "message", ""
                ) or ""
                if text:
                    captured.append(text)

            sent = await client.send_message(entity, message)
            try:
                await asyncio.sleep(self._capture_seconds)
            finally:
                client.remove_event_handler(_collect)

            # Belt-and-suspenders: also sweep history for anything we
            # may have missed (e.g. handler raced with rate-limit).
            try:
                async for msg in client.iter_messages(
                    entity,
                    min_id=sent.id,
                    limit=50,
                ):
                    text = getattr(msg, "raw_text", None) or getattr(msg, "message", "") or ""
                    if text and text not in captured:
                        captured.append(text)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("iter_messages sweep falhou: %s", exc)

        return captured

    def _candidates_from_responses(
        self,
        responses: list[str] | None,
        query: LookupQuery,
        message: str,
    ) -> list[PhoneCandidate]:
        if not responses:
            return []

        seen_digits: set[str] = set()
        out: list[PhoneCandidate] = []
        for idx, response in enumerate(responses):
            if not response:
                continue
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
                        source_url=(
                            f"https://t.me/{self._group_username.lstrip('@')}"
                        ),
                        context=self.context,
                        confidence_hint=self.confidence_hint,
                        extra={
                            "group": self._group_username,
                            "query": message,
                            "company": query.company_name,
                            "reply_index": str(idx),
                        },
                    )
                )
        return out


class TelegramVoidPhoneLookupProvider(TelegramGroupPhoneLookupProvider):
    """Void Search phone lookup via the same public group channel.

    The transport is identical to :class:`TelegramGroupPhoneLookupProvider`;
    only the command and provenance differ. Void's phone route is modeled
    as its own provider so the operator can filter/debug it independently
    while the UI still renders generic source labels.
    """

    name = "void_phone_consultasgratis"
    command = "/telefone"
    context = "telegram_bot_name_match"
    confidence_hint = 90


def _normalize_group_username(value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return "@CONSULTASGRATIS4NV"
    # Accept "t.me/foo", "https://t.me/foo", "@foo" or "foo".
    lowered = cleaned.lower()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    cleaned = cleaned.lstrip("@")
    return f"@{cleaned}"
