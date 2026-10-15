"""Routing tests for the single Vercel entrypoint (``api/index.py``)."""

from __future__ import annotations

import io

import api.index as entry


def _call(path: str, method: str = "GET", body: bytes = b"") -> tuple[int, bytes]:
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
    }
    captured: list = []
    result = entry.app(environ, lambda status, headers: captured.append(status))
    return int(captured[0].split(" ")[0]), b"".join(result)


def test_dashboard_route_serves_html() -> None:
    status, body = _call("/api/dashboard")
    assert status == 200
    assert b"<html" in body.lower()


def test_webapp_route_requires_init_data() -> None:
    status, _ = _call("/api/webapp")
    assert status == 401


def test_telegram_route_rejects_get() -> None:
    status, body = _call("/api/telegram")
    assert status == 405
    assert b"POST only" in body


def test_unknown_path_returns_404() -> None:
    status, body = _call("/api/whatever")
    assert status == 404
    assert b"not found" in body


def test_root_path_returns_404() -> None:
    status, _ = _call("/")
    assert status == 404


def test_routes_from_request_uri_when_path_info_empty() -> None:
    captured: list = []
    entry.app(
        {"REQUEST_METHOD": "GET", "PATH_INFO": "", "REQUEST_URI": "/api/dashboard?x=1"},
        lambda status, headers: captured.append(status),
    )
    assert captured[0].startswith("200")
