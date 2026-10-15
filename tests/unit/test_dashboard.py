"""Unit tests for the Mini App dashboard HTML server (Vercel function)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import api.dashboard as dashboard

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _call(method: str = "GET") -> tuple[int, bytes, dict]:
    headers: dict = {}
    result = dashboard.app(
        {"REQUEST_METHOD": method},
        lambda status, hdrs: headers.update({"status": status, "headers": hdrs}),
    )
    return int(headers["status"].split(" ")[0]), b"".join(result), headers


def test_serves_html_from_repo_root_layout() -> None:
    assert (_REPO_ROOT / "dashboard" / "index.html").is_file()
    status, body, headers = _call("GET")
    assert status == 200
    assert b"<html" in body.lower()
    assert dict(headers["headers"])["Content-Type"] == "text/html; charset=utf-8"


def test_rejects_non_get() -> None:
    status, _, _ = _call("POST")
    assert status == 405


def test_env_override_points_at_missing_file_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("BAZCAR_DASHBOARD_HTML", str(_REPO_ROOT / "does-not-exist.html"))
    status, body, _ = _call("GET")
    assert status == 200  # falls back to the real file
    assert b"<html" in body.lower()


def test_missing_everywhere_returns_404(monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "_candidates", lambda: [Path("nope/missing.html")])
    status, body, _ = _call("GET")
    assert status == 404
    assert b"not found" in body


def test_module_loads_by_path_as_vercel_would() -> None:
    """Vercel imports the entrypoint by path; ensure it exposes a WSGI `app`."""
    spec = importlib.util.spec_from_file_location(
        "dashboard_entry", _REPO_ROOT / "api" / "dashboard.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.app)
