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


def test_claim_skips_when_update_id_missing(monkeypatch) -> None:
    monkeypatch.setattr("api.telegram.asyncio.run", lambda coro: True)
    monkeypatch.setattr("builtins.open", open)  # no-op, keeps linters calm
    from api.telegram import _claim_update

    assert _claim_update(None) is True
    assert _claim_update(0) is True


def test_bad_payload_is_ignored(monkeypatch) -> None:
    """Unparseable bodies are acknowledged (200) so Telegram stops retrying."""
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret")
    status, body = _call("POST", b"not-json", "s3cret")
    assert status == 200
    assert b"bad payload" in body


def test_successful_update_returns_ok(monkeypatch) -> None:
    """A claimed update that the handler processes is acknowledged with `ok`."""
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret")
    import api.telegram as tg

    monkeypatch.setattr(tg, "_claim_update", lambda update_id: True)
    seen: list[dict] = []
    monkeypatch.setattr(tg, "_process_update", lambda update: seen.append(update))
    status, body = _call("POST", b'{"update_id": 42}', "s3cret")
    assert status == 200
    assert body == b"ok"
    assert seen == [{"update_id": 42}]


def test_handler_error_replies_to_chat(monkeypatch) -> None:
    """A handler crash is swallowed, acked, and reported to the user in chat."""
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    from bazcar.config.settings import get_settings

    get_settings.cache_clear()
    import api.telegram as tg

    monkeypatch.setattr(tg, "_claim_update", lambda update_id: True)

    def _boom(update: dict) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(tg, "_process_update", _boom)

    sent: list[str] = []

    class _FakeNotifier:
        def __init__(self, token: str, chat_id: str) -> None:
            self.token, self.chat_id = token, chat_id

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def send_message(self, text: str, **kwargs) -> None:
            sent.append(text)

    monkeypatch.setattr("bazcar.notify.telegram.TelegramNotifier", _FakeNotifier)
    try:
        status, body = _call(
            "POST", b'{"update_id": 9, "message": {"chat": {"id": 555}}}', "s3cret"
        )
    finally:
        get_settings.cache_clear()

    assert status == 200
    assert b"handled with error" in body
    assert sent and "Something failed" in sent[0]
