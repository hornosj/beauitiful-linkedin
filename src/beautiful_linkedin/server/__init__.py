"""FastAPI sidecar exposing the prospecting runner over HTTP for the Electron app."""

from beautiful_linkedin.server.app import build_app
from beautiful_linkedin.server.sidecar import main

__all__ = ["build_app", "main"]
