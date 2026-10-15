"""Serve the Telegram Mini App dashboard (single-page HTML).

The page itself is static (``dashboard/index.html``); serving it through a
Python endpoint guarantees it works on Vercel regardless of static-file
config. Data comes from ``/api/webapp`` (HMAC-verified), so this page can be
public without leaking anything.

The HTML is located at request time across the layouts it can be bundled in
(repo root locally, or alongside the function when Vercel's ``includeFiles``
copies it — see ``vercel.json``).
"""

from __future__ import annotations

import os
from http import HTTPStatus
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _candidates() -> list[Path]:
    """Possible on-disk locations of ``dashboard/index.html``."""
    paths: list[Path] = []
    override = os.environ.get("BAZCAR_DASHBOARD_HTML")
    if override:
        paths.append(Path(override))
    paths.extend(
        [
            _HERE.parent / "dashboard" / "index.html",  # repo-root layout (local, CI)
            _HERE / "dashboard" / "index.html",  # bundled next to the function on Vercel
            Path.cwd() / "dashboard" / "index.html",
        ]
    )
    return paths


def _page() -> bytes | None:
    for path in _candidates():
        try:
            return path.read_bytes()
        except OSError:
            continue
    return None


def app(environ, start_response):  # noqa: ANN001 — WSGI callable
    if environ.get("REQUEST_METHOD", "GET") != "GET":
        start_response("405 Method Not Allowed", [("Content-Type", "text/plain")])
        return [b"GET only"]
    body = _page()
    if body is None:
        start_response(
            f"{HTTPStatus.NOT_FOUND.value} {HTTPStatus.NOT_FOUND.phrase}",
            [("Content-Type", "text/plain")],
        )
        return [b"dashboard/index.html not found in deployment"]
    start_response(
        f"{HTTPStatus.OK.value} {HTTPStatus.OK.phrase}",
        [("Content-Type", "text/html; charset=utf-8"),
         ("Cache-Control", "public, max-age=300")],
    )
    return [body]
