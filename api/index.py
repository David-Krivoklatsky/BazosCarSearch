"""Single Vercel entrypoint that dispatches to the bot / Mini App / dashboard.

Vercel's Python runtime loads ONE entrypoint and routes every request to it
(files under ``api/`` do not become separate functions when an entrypoint is
configured). This module is that entrypoint: it routes by request path to the
owning WSGI app, so the surfaces keep their URLs —
``/api/telegram`` (webhook), ``/api/webapp`` (JSON API), ``/api/dashboard``
(SPA). Unknown paths fall through to the webhook (safe: Telegram-only, POST).
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

logger = logging.getLogger("bazcar.api")
logging.basicConfig(level=logging.INFO)

_API_DIR = Path(__file__).resolve().parent
_MODULES: dict[str, object] = {}


def _load(name: str):
    """Import a sibling ``api/<name>.py`` by path (no package import needed)."""
    if name not in _MODULES:
        spec = importlib.util.spec_from_file_location(f"_bazcar_api_{name}", _API_DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _MODULES[name] = module
    return _MODULES[name]


def _path(environ) -> str:  # noqa: ANN001
    path = environ.get("PATH_INFO") or ""
    if not path:
        raw = environ.get("REQUEST_URI") or environ.get("RAW_URI") or ""
        path = raw.split("?", 1)[0]
    return path.rstrip("/")


_ROUTES = ("/webapp", "/dashboard", "/telegram")


def app(environ, start_response):  # noqa: ANN001 — WSGI callable
    path = _path(environ)
    if path.endswith("/webapp"):
        return _load("webapp").app(environ, start_response)
    if path.endswith("/dashboard"):
        return _load("dashboard").app(environ, start_response)
    if path.endswith("/telegram"):
        return _load("telegram").app(environ, start_response)
    logger.warning("unknown api path: %s", path or "/")
    start_response("404 Not Found", [("Content-Type", "text/plain")])
    return [b"not found"]
