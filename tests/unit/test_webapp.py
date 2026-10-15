"""Unit tests for the Telegram Mini App JSON API (api/webapp.py)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

BOT_TOKEN = "123:TESTTOKEN"


def _sign(fields: dict, token: str = BOT_TOKEN) -> str:
    """Compute the Telegram WebApp hash for a data-check dict (test helper)."""
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()


def _fields(user_id: int = 42, age_s: int = 0) -> dict:
    return {
        "auth_date": str(int(time.time()) - age_s),
        "user": json.dumps({"id": user_id, "first_name": "Test"}),
        "query_id": "AAtest",
    }


def _make_init_data(user_id: int = 42, age_s: int = 0, token: str = BOT_TOKEN) -> str:
    fields = _fields(user_id, age_s)
    fields["hash"] = _sign(fields, token)
    return urlencode(fields)


def test_validate_init_data_accepts_valid() -> None:
    from api.webapp import validate_init_data

    init_data = _make_init_data(user_id=42)
    user = validate_init_data(init_data, BOT_TOKEN)
    assert user is not None
    assert user["id"] == 42


def test_validate_init_data_rejects_tampered() -> None:
    from api.webapp import validate_init_data

    original = _fields(user_id=42)
    tampered_fields = dict(original, user=json.dumps({"id": 99, "first_name": "Test"}))
    tampered = urlencode({**tampered_fields, "hash": _sign(original)})
    assert validate_init_data(tampered, BOT_TOKEN) is None


def test_validate_init_data_rejects_wrong_token() -> None:
    from api.webapp import validate_init_data

    init_data = _make_init_data(user_id=42)
    assert validate_init_data(init_data, "999:WRONG") is None


def test_validate_init_data_rejects_expired() -> None:
    from api.webapp import validate_init_data

    old = _make_init_data(user_id=42, age_s=90000)  # > 24 h
    assert validate_init_data(old, BOT_TOKEN) is None


def test_validate_init_data_rejects_garbage() -> None:
    from api.webapp import validate_init_data

    assert validate_init_data("", BOT_TOKEN) is None
    assert validate_init_data("foo=bar", BOT_TOKEN) is None
    assert validate_init_data("auth_date=abc&hash=deadbeef", BOT_TOKEN) is None
    assert validate_init_data(None, BOT_TOKEN) is None  # type: ignore[arg-type]


def _call_wsgi(query: str, init_header: str | None = None):
    from api.webapp import app

    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/api/webapp",
        "QUERY_STRING": query,
    }
    if init_header is not None:
        environ["HTTP_X_TELEGRAM_INIT_DATA"] = init_header
    status_headers: list = []
    result = app(environ, lambda status, headers: status_headers.append((status, headers)))
    return status_headers[0][0], b"".join(result)


@pytest.fixture()
def _fresh_settings(monkeypatch):
    """Reset the cached Settings and pin env (process env beats the .env file)."""
    from bazcar.config.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("BAZCAR_DATABASE_URL", "")  # overrides .env, stays falsy
    monkeypatch.setenv("DATABASE_URL", "")
    yield
    get_settings.cache_clear()


def test_wsgi_rejects_missing_init_data(_fresh_settings) -> None:
    status, body = _call_wsgi("view=overview")
    assert status.startswith("401")
    assert b"unauthorized" in body


def test_wsgi_rejects_bad_init_data(_fresh_settings) -> None:
    status, _ = _call_wsgi("view=overview&initData=foo%3Dbar")
    assert status.startswith("401")


def test_wsgi_accepts_valid_auth_then_needs_db(_fresh_settings) -> None:
    """A validly-signed request passes auth; with no DB it reports the error."""
    init_data = _make_init_data(user_id=42)
    query = urlencode({"view": "overview", "initData": init_data})
    status, body = _call_wsgi(query)
    assert status.startswith("200")
    payload = json.loads(body)
    assert payload.get("error") == "database not configured"


# ------------------------------------------------- listing detail view


class _Prefs:
    criteria = "diesel"
    model = "m/1"


class _FakeStore:
    def __init__(self, dsn: str) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_prefs(self, chat_id: int):
        return _Prefs()


class _FakeListRepo:
    def __init__(self, dsn: str) -> None:
        pass

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def fetch_listing_by_id(self, ad_id: int, profile_key: str):
        if ad_id == 404:
            return None
        return {
            "ad_id": ad_id,
            "title": "Škoda Octavia 1.6 TDI",
            "url": f"https://auto.bazos.sk/inzerat/{ad_id}/x.php",
            "price_eur": 8000,
            "year": 2019,
            "score": 82,
            "condition": "zachovalé",
            "target_offer": 7200,
        }

    async def fetch_price_history(self, ad_id: int, limit: int = 10):
        return [
            {"price_eur": 9000, "seen_at": "2026-01-01"},
            {"price_eur": 8000, "seen_at": "2026-02-01"},
        ]

    async def price_benchmark(self, make: str, year: int | None):
        assert make == "Škoda"
        return {"n": 12, "median": 8500, "p10": 6000, "p90": 12000}


class _DbSettings:
    database_url = "postgres://fake"
    telegram_bot_token = "x"


@pytest.fixture()
def _fake_listing_deps(monkeypatch):
    monkeypatch.setattr("bazcar.config.settings.get_settings", lambda: _DbSettings())

    class _LLM:
        model = "m/1"

    monkeypatch.setattr("bazcar.config.settings.load_llm_config", lambda: _LLM())
    monkeypatch.setattr("bazcar.bot.store.UserStore", _FakeStore)
    monkeypatch.setattr("bazcar.db.ListingRepository", _FakeListRepo)
    yield


def test_load_listing_view_bundles_history_and_benchmark(_fake_listing_deps) -> None:
    from api import webapp

    payload = asyncio.run(webapp._load_view("listing", 42, 0, 30, 9))
    assert payload["listing"]["ad_id"] == 9
    assert payload["listing"]["target_offer"] == 7200
    assert len(payload["history"]) == 2
    assert payload["benchmark"] == {"n": 12, "median": 8500, "p10": 6000, "p90": 12000}


def test_load_listing_view_requires_id(_fake_listing_deps) -> None:
    from api import webapp

    payload = asyncio.run(webapp._load_view("listing", 42, 0, 30, None))
    assert payload == {"error": "id required"}


def test_load_listing_view_not_found(_fake_listing_deps) -> None:
    from api import webapp

    payload = asyncio.run(webapp._load_view("listing", 42, 0, 30, 404))
    assert payload == {"error": "not found"}
