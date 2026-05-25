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
import socket
import sys

import uvicorn

from beautiful_linkedin.server.app import build_app

DEFAULT_HOST = "127.0.0.1"
PORT_ENV_VAR = "BEAUTIFUL_LINKEDIN_PORT"
READY_TOKEN = "BEAUTIFUL_LINKEDIN_READY"


def configure_logging() -> None:
    """Route application INFO logs to the sidecar process output.

    Electron already tails the Python child process stdout/stderr and
    surfaces those lines as sidecar logs. The missing piece was the
    Python sidecar defaulting to WARNING, which hid the Telegram flow's
    step-by-step INFO logs while the operator needed to debug it.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
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
    configure_logging()
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


if __name__ == "__main__":
    main()
