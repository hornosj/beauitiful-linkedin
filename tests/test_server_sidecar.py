import io
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest

from beautiful_linkedin.server import sidecar


def test_pick_free_port_returns_int_in_user_range():
    port = sidecar.pick_free_port()
    assert isinstance(port, int)
    assert 1024 <= port <= 65535


def test_resolve_port_honors_env_var(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(sidecar.PORT_ENV_VAR, "39712")
    assert sidecar.resolve_port() == 39712


def test_resolve_port_falls_back_to_free_port_when_env_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(sidecar.PORT_ENV_VAR, raising=False)
    port = sidecar.resolve_port()
    assert 1024 <= port <= 65535


def test_announce_prints_protocol_line(monkeypatch: pytest.MonkeyPatch):
    buffer = io.StringIO()
    monkeypatch.setattr(sidecar.sys, "stdout", buffer)
    sidecar.announce(39712)
    assert buffer.getvalue().strip() == f"{sidecar.PORT_ENV_VAR}=39712"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_python_dash_m_actually_starts_uvicorn():
    """Regression for the '__main__.py only imports main()' bug.

    Spawns the sidecar as the Electron main process would, watches stdout
    for the boot protocol tokens, and confirms /health answers. Without
    `if __name__ == '__main__': main()` the process exits with code 0
    before binding the port.
    """
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "beautiful_linkedin.server"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={
            **dict(__import__("os").environ),
            sidecar.PORT_ENV_VAR: str(port),
            "PYTHONUNBUFFERED": "1",
        },
    )
    try:
        deadline = time.time() + 15.0
        seen_port = False
        seen_ready = False
        assert proc.stdout is not None
        while time.time() < deadline and not (seen_port and seen_ready):
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    pytest.fail(f"Sidecar saiu antes de inicializar (rc={proc.returncode})")
                continue
            if f"{sidecar.PORT_ENV_VAR}={port}" in line:
                seen_port = True
            if sidecar.READY_TOKEN in line:
                seen_ready = True

        assert seen_port, "Sidecar não imprimiu o token de porta"
        assert seen_ready, "Sidecar não imprimiu o token READY"

        response = urlopen(f"http://127.0.0.1:{port}/health", timeout=2.0)
        assert response.status == 200
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
