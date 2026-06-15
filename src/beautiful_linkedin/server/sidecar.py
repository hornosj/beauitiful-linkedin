"""Sidecar entrypoint launched by the Electron main process.

Boot protocol (read by Electron):
    1. Pick a free localhost port (or honor BEAUTIFUL_LINKEDIN_PORT env var).
    2. Print ``BEAUTIFUL_LINKEDIN_PORT=<port>`` followed by a newline to stdout
       and flush. The Electron main process matches this line and stores the
       port. After this, the renderer can hit ``http://127.0.0.1:<port>``.
    3. Print ``BEAUTIFUL_LINKEDIN_READY`` after uvicorn starts serving so the
       renderer can defer requests until the server is up.
    4. Exit cleanly on SIGINT / SIGTERM (Electron sends these on app quit).

Bind only to 127.0.0.1 — never expose this to the LAN.
"""

from __future__ import annotations

import os
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import socket
import sys

import uvicorn

from beautiful_linkedin.server.app import build_app

DEFAULT_HOST = "127.0.0.1"
PORT_ENV_VAR = "BEAUTIFUL_LINKEDIN_PORT"
LOG_PATH_ENV_VAR = "BEAUTIFUL_LINKEDIN_LOG_PATH"
READY_TOKEN = "BEAUTIFUL_LINKEDIN_READY"


def configure_stdio() -> None:
    """Force UTF-8 output so Windows codepages cannot crash logging/progress.

    The packaged sidecar runs as a child process of Electron. On some Windows
    machines Python picks a legacy ``charmap`` encoding for stdout/stderr; any
    Unicode spinner or symbol then raises ``UnicodeEncodeError`` and bubbles up
    as a 500. ``errors='replace'`` is intentional: logs must never crash work.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _default_log_path() -> Path:
    """Return ``<LOCALAPPDATA>\\BeautifulLinkedIn\\sidecar.log`` on Windows.

    Falls back to ``~/.beautiful-linkedin/sidecar.log`` elsewhere. Users can
    override with ``BEAUTIFUL_LINKEDIN_LOG_PATH``. The file is used by the
    bundled exe so we can read it post-mortem when a search hangs (Electron's
    DevTools is not something operators open in normal use).
    """
    override = os.environ.get(LOG_PATH_ENV_VAR)
    if override and override.strip():
        return Path(override.strip())
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "BeautifulLinkedIn" / "sidecar.log"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "BeautifulLinkedIn" / "sidecar.log"
    return Path.home() / ".beautiful-linkedin" / "sidecar.log"


def configure_logging() -> None:
    """Route application INFO logs to the sidecar process output AND a file.

    Electron already tails the Python child process stdout/stderr and
    surfaces those lines as sidecar logs. The missing piece was the
    Python sidecar defaulting to WARNING, which hid the Telegram flow's
    step-by-step INFO logs while the operator needed to debug it.

    On top of stderr we also write a rotating file so a user reporting "the
    search hangs" can attach ``%LOCALAPPDATA%\\BeautifulLinkedIn\\sidecar.log``
    instead of needing to open Electron DevTools. The file is silently
    skipped if its directory cannot be created (read-only profile, locked
    by AV, etc.) — logs always go to stderr regardless.
    """
    handlers: list[logging.Handler] = []
    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handlers.append(stream)

    log_path = _default_log_path()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        handlers.append(file_handler)
    except Exception as exc:  # pragma: no cover - best-effort: stderr is enough
        sys.stderr.write(f"[sidecar] file logging desabilitado em {log_path}: {exc}\n")

    # Default INFO; flip to DEBUG via BEAUTIFUL_LINKEDIN_LOG_LEVEL=DEBUG to
    # surface the per-poll Telegram/Telethon provider traces while debugging
    # the phone flow.
    level_name = (os.environ.get("BEAUTIFUL_LINKEDIN_LOG_LEVEL") or "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(level=level, handlers=handlers, force=True)
    if len(handlers) > 1:
        logging.getLogger(__name__).info(
            "sidecar: log em arquivo ativo: %s (nível=%s)",
            log_path,
            logging.getLevelName(level),
        )


def pick_free_port(host: str = DEFAULT_HOST) -> int:
    """Ask the kernel for a free TCP port we can bind to.

    There is a race window between releasing the socket and uvicorn binding
    again, but it's negligible for a single-tenant local sidecar.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def resolve_port() -> int:
    explicit = os.environ.get(PORT_ENV_VAR)
    if explicit and explicit.strip().isdigit():
        return int(explicit.strip())
    return pick_free_port()


def announce(port: int) -> None:
    sys.stdout.write(f"{PORT_ENV_VAR}={port}\n")
    sys.stdout.flush()


def main() -> None:
    configure_stdio()
    configure_logging()
    log = logging.getLogger(__name__)
    try:
        port = resolve_port()
        announce(port)
        app = build_app()

        @app.on_event("startup")  # type: ignore[misc]
        def _emit_ready() -> None:
            sys.stdout.write(f"{READY_TOKEN}\n")
            sys.stdout.flush()

        uvicorn.run(
            app,
            host=DEFAULT_HOST,
            port=port,
            log_level="warning",
            access_log=False,
        )
    except Exception:
        # Sem isto, um crash no boot (ex.: import/DB/config) sobe sem passar pelo
        # logger e nunca chega ao sidecar.log — o operador fica sem a causa. O
        # Electron já tratou a saída do processo; aqui só garantimos o registro.
        log.exception("Sidecar falhou ao iniciar")
        raise


if __name__ == "__main__":
    main()
