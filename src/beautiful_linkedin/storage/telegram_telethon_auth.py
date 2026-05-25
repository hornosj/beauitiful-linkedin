"""Interactive Telethon login helpers for the sidecar UI.

The experimental Telethon-backed Telegram consult flow shares a session
file with this module. The UI walks the user through a two-step login
(send code → sign in with code + optional 2FA password) so the sidecar
never needs stdin/TTY input.

Both ``send_code`` and ``sign_in`` are stateless across calls: each
opens its own ``TelegramClient``, performs one MTProto round-trip, and
disconnects. Telethon's ``phone_code_hash`` is what links the two
calls — no in-process state needed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger(__name__)


DEFAULT_SESSION_NAME = "data/telegram_telethon_lookup"


@dataclass(frozen=True)
class SendCodeResult:
    phone_code_hash: str
    next_type: str | None = None
    timeout: int | None = None


@dataclass(frozen=True)
class SignInResult:
    user_id: int | None
    username: str | None
    first_name: str | None
    requires_password: bool = False


class TelethonAuthError(RuntimeError):
    """Raised when an authentication step fails with a user-actionable cause."""


def _coerce_api_id(value: int | str | None) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _import_telethon() -> Any:
    try:
        import telethon  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - optional dependency
        raise TelethonAuthError("telethon_not_installed") from exc
    return telethon


def _ensure_credentials(api_id: int | str | None, api_hash: str | None) -> tuple[int, str]:
    coerced_id = _coerce_api_id(api_id)
    cleaned_hash = (api_hash or "").strip()
    if coerced_id is None or not cleaned_hash:
        raise TelethonAuthError("telegram_not_configured")
    return coerced_id, cleaned_hash


async def _is_authorized_async(
    session_name: str, api_id: int, api_hash: str
) -> bool:
    telethon = _import_telethon()
    client = telethon.TelegramClient(session_name, api_id, api_hash)
    await client.connect()
    try:
        return bool(await client.is_user_authorized())
    finally:
        await client.disconnect()


def is_authorized(
    *,
    session_name: str = DEFAULT_SESSION_NAME,
    api_id: int | str | None,
    api_hash: str | None,
) -> bool:
    coerced_id, cleaned_hash = _ensure_credentials(api_id, api_hash)
    return asyncio.run(_is_authorized_async(session_name, coerced_id, cleaned_hash))


async def _log_out_async(
    session_name: str, api_id: int, api_hash: str
) -> bool:
    telethon = _import_telethon()
    client = telethon.TelegramClient(session_name, api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return False
        # ``log_out`` revokes the session server-side AND deletes the local
        # ``.session`` file, so the next login starts clean. Returns True on
        # success.
        return bool(await client.log_out())
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def log_out(
    *,
    session_name: str = DEFAULT_SESSION_NAME,
    api_id: int | str | None,
    api_hash: str | None,
) -> bool:
    """Revoke the current Telegram session and clear the local ``.session``.

    Lets the operator switch accounts (or re-login with the same one). Safe
    to call when no session exists — returns ``False`` in that case instead
    of raising.
    """
    coerced_id, cleaned_hash = _ensure_credentials(api_id, api_hash)
    try:
        return asyncio.run(_log_out_async(session_name, coerced_id, cleaned_hash))
    except TelethonAuthError:
        raise
    except Exception as exc:  # pragma: no cover - network/telethon failures
        logger.debug("Telethon log_out failed: %s", exc)
        # Fall back to deleting the local session file so the UI can still
        # recover into a logged-out state even if the server round-trip fails.
        _delete_session_file(session_name)
        return True


def _delete_session_file(session_name: str) -> None:
    from pathlib import Path

    for candidate in (Path(session_name), Path(f"{session_name}.session")):
        try:
            if candidate.exists():
                candidate.unlink()
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("Falha ao remover sessão Telethon %s: %s", candidate, exc)


async def _send_code_async(
    session_name: str, api_id: int, api_hash: str, phone: str
) -> SendCodeResult:
    telethon = _import_telethon()
    client = telethon.TelegramClient(session_name, api_id, api_hash)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        next_type = getattr(getattr(sent, "next_type", None), "__class__", None)
        return SendCodeResult(
            phone_code_hash=str(getattr(sent, "phone_code_hash", "")),
            next_type=next_type.__name__ if next_type else None,
            timeout=getattr(sent, "timeout", None),
        )
    finally:
        await client.disconnect()


def send_code(
    *,
    phone: str,
    session_name: str = DEFAULT_SESSION_NAME,
    api_id: int | str | None,
    api_hash: str | None,
) -> SendCodeResult:
    cleaned = (phone or "").strip()
    if not cleaned:
        raise TelethonAuthError("phone_required")
    coerced_id, cleaned_hash = _ensure_credentials(api_id, api_hash)
    try:
        return asyncio.run(
            _send_code_async(session_name, coerced_id, cleaned_hash, cleaned)
        )
    except TelethonAuthError:
        raise
    except Exception as exc:  # pragma: no cover - network/telethon failures
        logger.debug("Telethon send_code failed for phone=%s: %s", cleaned, exc)
        raise TelethonAuthError(f"send_code_failed:{type(exc).__name__}") from exc


async def _sign_in_async(
    session_name: str,
    api_id: int,
    api_hash: str,
    phone: str,
    code: str,
    phone_code_hash: str,
    password: str | None,
) -> SignInResult:
    telethon = _import_telethon()
    errors = telethon.errors  # type: ignore[attr-defined]
    client = telethon.TelegramClient(session_name, api_id, api_hash)
    await client.connect()
    try:
        try:
            user = await client.sign_in(
                phone=phone,
                code=code,
                phone_code_hash=phone_code_hash,
            )
        except errors.SessionPasswordNeededError as exc:
            if not password:
                return SignInResult(
                    user_id=None,
                    username=None,
                    first_name=None,
                    requires_password=True,
                )
            try:
                user = await client.sign_in(password=password)
            except errors.PasswordHashInvalidError as inner:
                raise TelethonAuthError("password_invalid") from inner
            except Exception as inner:
                raise TelethonAuthError(
                    f"sign_in_password_failed:{type(inner).__name__}"
                ) from inner
            _ = exc
        except errors.PhoneCodeInvalidError as exc:
            raise TelethonAuthError("code_invalid") from exc
        except errors.PhoneCodeExpiredError as exc:
            raise TelethonAuthError("code_expired") from exc
        return SignInResult(
            user_id=getattr(user, "id", None),
            username=getattr(user, "username", None),
            first_name=getattr(user, "first_name", None),
            requires_password=False,
        )
    finally:
        await client.disconnect()


def sign_in(
    *,
    phone: str,
    code: str,
    phone_code_hash: str,
    password: str | None = None,
    session_name: str = DEFAULT_SESSION_NAME,
    api_id: int | str | None,
    api_hash: str | None,
) -> SignInResult:
    cleaned_phone = (phone or "").strip()
    cleaned_code = (code or "").strip()
    cleaned_hash = (phone_code_hash or "").strip()
    if not cleaned_phone or not cleaned_code or not cleaned_hash:
        raise TelethonAuthError("missing_sign_in_fields")
    coerced_id, cleaned_api_hash = _ensure_credentials(api_id, api_hash)
    try:
        return asyncio.run(
            _sign_in_async(
                session_name,
                coerced_id,
                cleaned_api_hash,
                cleaned_phone,
                cleaned_code,
                cleaned_hash,
                (password or None) and password.strip() or None,
            )
        )
    except TelethonAuthError:
        raise
    except Exception as exc:  # pragma: no cover - network/telethon failures
        logger.debug("Telethon sign_in failed for phone=%s: %s", cleaned_phone, exc)
        raise TelethonAuthError(f"sign_in_failed:{type(exc).__name__}") from exc
