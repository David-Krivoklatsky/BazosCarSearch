"""Unit tests for the Vercel Telegram webhook handler (Phase 5.5)."""

from __future__ import annotations

import io

from api.telegram import app


def _call(method: str, body: bytes, secret: str | None = "s3cret") -> tuple[int, bytes]:
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": "/api/telegram",
        "CONTENT_LENGTH": str(len(body)),
        "CONTENT_TYPE": "application/json",
        "wsgi.input": io.BytesIO(body),
    }
    if secret is not None:
        environ["HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN"] = secret
    status_headers: list = []
    result = app(environ, lambda status, headers: status_headers.append((status, headers)))
    return int(status_headers[0][0].split(" ")[0]), b"".join(result)


def test_rejects_get(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret")
    status, _ = _call("GET", b"", "s3cret")
    assert status == 405


def test_rejects_missing_secret_header(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret")
    status, _ = _call("POST", b"{}", None)
    assert status == 403


def test_rejects_wrong_secret(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret")
    status, _ = _call("POST", b"{}", "wrong")
    assert status == 403


def test_rejects_when_secret_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    status, _ = _call("POST", b"{}", "anything")
    assert status == 403


def test_duplicate_update_short_circuits(monkeypatch) -> None:
    """Already-claimed updates are acknowledged without running the handler."""
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("BAZCAR_DATABASE_URL", "")  # would crash if handler ran
    monkeypatch.setattr("api.telegram._claim_update", lambda update_id: False)
    status, body = _call("POST", b'{"update_id": 5}', "s3cret")
    assert status == 200
    assert b"duplicate" in body
