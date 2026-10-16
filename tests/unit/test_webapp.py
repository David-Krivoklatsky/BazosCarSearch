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
    def __init__(self, dsn: str, conn=None) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_prefs(self, chat_id: int):
        return _Prefs()


class _FakeListRepo:
    def __init__(self, dsn: str, conn=None) -> None:
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

    async def price_benchmark(self, make: str, year: int | None, model=None, mileage_km=None):
        assert make == "Škoda"
        return {"n": 12, "median": 8500, "p10": 6000, "p90": 12000}


class _DbSettings:
    database_url = "postgres://fake"
    telegram_bot_token = BOT_TOKEN
    monthly_budget_usd = 1.0


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

    payload, status = asyncio.run(webapp._load_view("listing", 42, 0, 30, 9))
    assert status == 200
    assert payload["listing"]["ad_id"] == 9
    assert payload["listing"]["target_offer"] == 7200
    assert len(payload["history"]) == 2
    assert payload["benchmark"] == {"n": 12, "median": 8500, "p10": 6000, "p90": 12000}


def test_load_listing_view_requires_id(_fake_listing_deps) -> None:
    from api import webapp

    payload, status = asyncio.run(webapp._load_view("listing", 42, 0, 30, None))
    assert status == 400
    assert payload == {"error": "id required"}


def test_load_listing_view_not_found(_fake_listing_deps) -> None:
    from api import webapp

    payload, status = asyncio.run(webapp._load_view("listing", 42, 0, 30, 404))
    assert status == 200
    assert payload == {"error": "not found"}


# ------------------------------------------------- filters, saved, profiles,
# stats, compare views + POST actions


class _RichStore(_FakeStore):
    def __init__(self) -> None:
        self.calls: list = []
        self.searches = [
            {"name": "diesel", "criteria": "diesel", "min_score": 70, "notify": True}
        ]
        self.prefs = {"notify": True, "score": 60, "photos": False}
        self.updates: dict = {}

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def list_saved(self, chat_id: int):
        from bazcar.bot.store import SavedListing

        return [
            SavedListing(
                ad_id=1, ad_title="A", ad_url="https://x/1",
                note="zavolat", current_price=4000, max_price=5000,
                last_seen_at="2026-10-01T00:00:00+00:00",
            )
        ]

    async def list_saved_ids(self, chat_id: int):
        return {1}

    async def list_searches(self, chat_id: int):
        from bazcar.bot.store import SearchProfile

        return [
            SearchProfile(
                name=s["name"], criteria=s["criteria"],
                min_score=s["min_score"], notify_enabled=s["notify"],
            )
            for s in self.searches
        ]

    async def get_prefs(self, chat_id: int):
        class _P:
            criteria = "diesel"
            model = "m/1"
            min_score = 60
            show_photo = False
            notify_enabled = True

        prefs = _P()
        for key, value in self.updates.items():
            setattr(prefs, key, value)
        return prefs

    async def ensure_prefs(self, chat_id: int):
        return await self.get_prefs(chat_id)

    async def update_prefs(self, chat_id: int, **fields) -> None:
        self.calls.append(("prefs", fields))
        self.updates.update(fields)

    async def update_filters(self, chat_id: int, patch: dict) -> dict:
        self.calls.append(("filters", patch))
        return {"query": "octavia"}

    async def save_search(self, chat_id, name, criteria, filters=None, min_score=None) -> None:
        self.calls.append(("search_create", name, criteria))

    async def delete_search(self, chat_id: int, name: str) -> bool:
        self.calls.append(("search_delete", name))
        return True

    async def set_search_notify(self, chat_id: int, name: str, enabled: bool) -> bool:
        self.calls.append(("search_notify", name, enabled))
        return True

    async def save_listing(self, chat_id, ad_id, title, url, note=None) -> None:
        self.calls.append(("save", ad_id))

    async def remove_saved(self, chat_id, ad_id) -> bool:
        self.calls.append(("unsave", ad_id))
        return True

    async def set_saved_note(self, chat_id, ad_id, note) -> bool:
        self.calls.append(("note", ad_id, note))
        return True

    async def set_saved_alert(self, chat_id, ad_id, below) -> bool:
        self.calls.append(("alert", ad_id, below))
        return True


class _RichRepo(_FakeListRepo):
    def __init__(self) -> None:
        self.catalog_kwargs: dict | None = None

    async def fetch_catalog(self, p_key, days, limit, offset=0, sort="newest",
                            status="all", **kw):
        self.catalog_kwargs = {"sort": sort, "status": status, **kw}
        self.catalog_limit = limit
        return [{"ad_id": 7, "title": "Skoda Fabia", "url": "https://x/7"}]

    async def count_catalog(self, p_key, days, status="all", **kw):
        return 1

    async def stats_overview(self, p_key):
        return {"total": 10, "evaluated": 4, "price": {}, "bands": {}, "new_by_day": [], "drops_7d": 1}

    async def latest_scrape_run(self):
        return None

    async def pending_eval_tasks(self):
        return []

    async def recent_finished_eval_tasks(self, limit=5):
        return []

    async def price_histogram(self, buckets=8):
        return [{"bucket": 1, "n": 10, "lo": 1000, "hi": 2000}]

    async def top_makes(self, limit=10):
        return [{"make": "Skoda", "n": 6, "median": 5000}]

    async def score_vs_price(self, p_key):
        return [{"band": "80-89", "n": 2, "avg_price": 6000, "median_price": 6000}]

    async def new_by_day(self, days=30):
        return [("2026-10-01", 3)]

    async def coverage_trend(self, p_key, days=14):
        return [("2026-10-01", 2)]

    async def drops_leaderboard(self, limit=10, days=30):
        return [{"ad_id": 7, "title": "Skoda", "url": "https://x/7",
                 "city": "BA", "prev": 6000, "now_price": 5000, "pct": 16.7}]

    async def funnel(self, p_key, chat_id=None):
        return {"scraped": 10, "evaluated": 4, "notified": 2, "saved": 1}

    async def eval_cost(self, p_key=None, days=30):
        return {"total": {"n": 4, "cost_usd": 0.01}, "by_model": []}

    async def catalog_bounds(self, p_key, days, status="all", q=None):
        self.bounds_kwargs = {"status": status, "q": q}
        return {"min_price": 1000, "max_price": 9000,
                "min_year": 2010, "max_year": 2020, "n": 5}

    async def fetch_recent_matches(self, p_key, min_score, days, limit):
        return [{"ad_id": 7, "title": "Skoda", "url": "https://x/7",
                 "price_eur": 5000, "score": 88}]

    async def get_model_cache(self):
        import datetime

        return {
            "models": [                {"id": "x/model", "name": "X Model",
                 "supported_parameters": ["response_format"],
                 "pricing": {"prompt": "0", "completion": "0"},
                 "per_request_limits": {"requests_per_minute": 50},
                 "context_length": 128000},
                {"id": "x/chat", "name": "X Chat",
                 "supported_parameters": ["tools"],
                 "pricing": {"prompt": "1", "completion": "2"},
                 "per_request_limits": {},
                 "context_length": 8000},
            ],
            "fetched_at": datetime.datetime.now(datetime.UTC),
        }


@pytest.fixture()
def _rich_deps(monkeypatch):
    store = _RichStore()
    repo = _RichRepo()
    monkeypatch.setattr("bazcar.config.settings.get_settings", lambda: _DbSettings())
    monkeypatch.setattr("bazcar.config.settings.load_llm_config", lambda: type("L", (), {"model": "m/1"})())
    monkeypatch.setattr("bazcar.bot.store.UserStore", lambda dsn, conn=None: store)
    monkeypatch.setattr("bazcar.db.ListingRepository", lambda dsn, conn=None: repo)
    return store, repo


def _run_view(view, extra=None, **kw):
    from api import webapp

    return asyncio.run(webapp._load_view(view, 42, 0, 30, kw.get("ad_id"), extra))


def test_inventory_passes_filters(_rich_deps) -> None:
    _, repo = _rich_deps
    payload, status = _run_view("inventory", {
        "q": "octavia", "sort": "price_asc", "status": "rated",
        "min_price": 1000, "max_price": 9000, "min_year": 2010,
        "max_year": 2020, "per": "50",
    })
    assert status == 200
    assert payload["total"] == 1
    assert repo.catalog_kwargs["q"] == "octavia"
    assert repo.catalog_kwargs["sort"] == "price_asc"
    assert repo.catalog_kwargs["status"] == "rated"
    assert repo.catalog_kwargs["min_price"] == 1000
    assert repo.catalog_kwargs["max_year"] == 2020


def test_saved_view_computes_drop(_rich_deps) -> None:
    payload, status = _run_view("saved")
    assert status == 200
    item = payload["items"][0]
    assert item["drop_pct"] == 20.0
    assert item["note"] == "zavolat"
    assert payload["saved_ids"] == [1]


def test_profiles_view_shape(_rich_deps) -> None:
    payload, status = _run_view("profiles")
    assert status == 200
    assert payload["active"]["min_score"] == 60
    assert payload["searches"][0]["name"] == "diesel"
    assert payload["searches"][0]["notify_enabled"] is True


def test_stats_view_bundles_v2(_rich_deps) -> None:
    payload, status = _run_view("stats")
    assert status == 200
    assert payload["histogram"][0]["n"] == 10
    assert payload["top_makes"][0]["make"] == "Skoda"
    assert payload["score_vs_price"][0]["band"] == "80-89"
    assert payload["trend30"] == [("2026-10-01", 3)]
    assert payload["drops"][0]["pct"] == 16.7
    assert payload["funnel"]["saved"] == 1
    assert payload["cost"]["total"]["n"] == 4


def test_inventory_includes_saved_ids(_rich_deps) -> None:
    payload, status = _run_view("inventory", {})
    assert status == 200
    assert payload["saved_ids"] == [1]


def test_compare_view_bundles_up_to_three(_rich_deps) -> None:
    payload, status = _run_view("compare", {"ids": [9, 404]})
    assert status == 200
    assert [i["listing"]["ad_id"] for i in payload["items"]] == [9]


def test_unknown_view_is_400(_rich_deps) -> None:
    payload, status = _run_view("nope")
    assert status == 400
    assert "unknown view" in payload["error"]


def test_inventory_per_page_is_clamped(_rich_deps) -> None:
    _, repo = _rich_deps
    _run_view("inventory", {"per": "500"})
    assert repo.catalog_limit == 200
    _run_view("inventory", {"per": "abc"})
    assert repo.catalog_limit == 15
    _run_view("inventory", {})
    assert repo.catalog_limit == 15


def test_use_action_activates_search(_fresh_settings, _rich_deps) -> None:
    store, _ = _rich_deps
    init_data = _make_init_data(user_id=42)
    status, payload = _call_post({"action": "use", "name": "diesel"}, init_data)
    assert status.startswith("200") and payload == {"ok": True}
    assert ("prefs", {"criteria": "diesel", "filters": {}, "min_score": 70}) in [
        c for c in store.calls if c[0] == "prefs"
    ]
    status, payload = _call_post({"action": "use", "name": "missing"}, init_data)
    assert status.startswith("404")
    assert payload["error"] == "search not found"


def _call_post(body: dict, init_header: str, token: str = BOT_TOKEN):
    import io

    from api.webapp import app

    raw = json.dumps(body).encode()
    environ = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/api/webapp",
        "CONTENT_LENGTH": str(len(raw)),
        "CONTENT_TYPE": "application/json",
        "wsgi.input": io.BytesIO(raw),
        "HTTP_X_TELEGRAM_INIT_DATA": init_header,
    }
    captured: list = []
    result = app(environ, lambda status, headers: captured.append(status))
    return captured[0], json.loads(b"".join(result))


def test_post_actions_save_unsave_note(_fresh_settings, _rich_deps) -> None:
    store, _ = _rich_deps
    init_data = _make_init_data(user_id=42)
    status, payload = _call_post({"action": "save", "id": 9}, init_data)
    assert status.startswith("200") and payload == {"ok": True}
    status, payload = _call_post({"action": "unsave", "id": 9}, init_data)
    assert payload["removed"] is True
    status, payload = _call_post({"action": "note", "id": 9, "note": "hi"}, init_data)
    assert payload["updated"] is True
    assert ("note", 9, "hi") in store.calls


def test_post_actions_prefs_and_search(_fresh_settings, _rich_deps) -> None:
    store, _ = _rich_deps
    init_data = _make_init_data(user_id=42)
    _, payload = _call_post(
        {"action": "toggle_search", "name": "diesel", "enabled": False}, init_data
    )
    assert payload["updated"] is True
    _, payload = _call_post({"action": "notify", "enabled": False}, init_data)
    assert payload == {"ok": True}
    _, payload = _call_post({"action": "score", "min_score": 80}, init_data)
    assert payload == {"ok": True}
    _, payload = _call_post({"action": "photos", "enabled": True}, init_data)
    assert payload == {"ok": True}
    _, payload = _call_post({"action": "bogus"}, init_data)
    assert payload["error"].startswith("unknown action")


def test_post_action_alert_sets_and_clears(_fresh_settings, _rich_deps) -> None:
    store, _ = _rich_deps
    init_data = _make_init_data(user_id=42)
    _, payload = _call_post({"action": "alert", "id": 9, "below": 3000}, init_data)
    assert payload == {"ok": True, "updated": True}
    assert ("alert", 9, 3000) in store.calls
    _, payload = _call_post({"action": "alert", "id": 9}, init_data)
    assert payload == {"ok": True, "updated": True}
    assert ("alert", 9, None) in store.calls
    _, payload = _call_post({"action": "alert", "id": 9, "below": -5}, init_data)
    assert payload["error"] == "below must be a positive number"
    _, payload = _call_post({"action": "alert"}, init_data)
    assert payload["error"] == "id required"


def test_post_action_criteria_updates_profile(_fresh_settings, _rich_deps) -> None:
    store, _ = _rich_deps
    init_data = _make_init_data(user_id=42)
    _, payload = _call_post({"action": "criteria", "criteria": "diesel wagon"}, init_data)
    assert payload == {"ok": True}
    assert ("prefs", {"criteria": "diesel wagon"}) in [
        c for c in store.calls if c[0] == "prefs"
    ]
    _, payload = _call_post({"action": "criteria", "criteria": "  "}, init_data)
    assert payload["error"] == "criteria required"


def test_insights_view_merges_overview_stats_queue(_rich_deps) -> None:
    payload, status = _run_view("insights")
    assert status == 200
    for key in ("stats", "last_run", "queue_pending", "pending", "finished",
                "histogram", "top_makes", "score_vs_price", "trend30",
                "coverage", "drops", "funnel", "cost"):
        assert key in payload, f"insights missing {key}"


def test_listing_view_reports_saved_state(_rich_deps) -> None:
    # saved_ids = {1}; ad 9 is not saved
    from api import webapp

    payload, status = asyncio.run(webapp._load_view("listing", 42, 0, 30, 9))
    assert status == 200
    assert payload["is_saved"] is False


def test_post_requires_auth(_fresh_settings) -> None:
    status, payload = _call_post({"action": "save", "id": 1}, "garbage")
    assert status.startswith("401")


def test_post_model_queues_rescore_end_to_end(_fresh_settings, _rich_deps, monkeypatch) -> None:
    """The model action must queue a task (regression: ad_ids=None crashed it)."""
    created: list = []

    class _QueueRepo:
        def __init__(self, dsn):
            pass

        async def connect(self):
            pass

        async def close(self):
            pass

        async def init_schema(self):
            pass

        async def pending_eval_tasks(self):
            return []

        async def create_eval_task(self, p_key, criteria, model, limit, days,
                                   prompt_name="standard", fallback_models=None,
                                   ad_ids=None):
            created.append({
                "model": model, "prompt": prompt_name,
                "fallbacks": fallback_models, "ad_ids": ad_ids,
            })

    monkeypatch.setattr("bazcar.db.ListingRepository", _QueueRepo)
    init_data = _make_init_data(user_id=42)
    _, payload = _call_post({"action": "model", "id": "x/model"}, init_data)
    assert payload["ok"] is True and payload["queued"] is True
    assert created[0]["model"] == "x/model"
    assert created[0]["ad_ids"] is None
    _, payload = _call_post({"action": "prompt", "name": "family"}, init_data)
    assert payload["ok"] is True and payload["queued"] is True
    assert created[1]["prompt"] == "family"
    _, payload = _call_post({"action": "rescore", "ids": [7]}, init_data)
    assert payload["ok"] is True and payload["queued"] is True
    assert created[2]["ad_ids"] == [7]


def test_profile_alias_resolves_to_profiles(_rich_deps) -> None:
    """The SPA Profile tab requests view=profile — must not 400."""
    payload, status = _run_view("profile")
    assert status == 200
    assert "active" in payload and "searches" in payload
    assert payload["active"]["prompt_name"] == "standard"
    assert "standard" in payload["active"]["prompts"]
    assert payload["active"]["fallback_models"] == []
    assert isinstance(payload["active"]["filters"], dict)


def test_bounds_view_spans_full_range(_rich_deps) -> None:
    _, repo = _rich_deps
    payload, status = _run_view("bounds", {"status": "all", "q": "oct"})
    assert status == 200
    assert payload["bounds"]["min_price"] == 1000
    assert payload["bounds"]["max_price"] == 9000
    assert payload["bounds"]["min_year"] == 2010
    assert repo.bounds_kwargs == {"status": "all", "q": "oct"}


def test_models_view_uses_cache_without_network(_rich_deps) -> None:
    """Fresh cache => the cached list is served, no live request."""
    payload, status = _run_view("models")
    assert status == 200
    assert [m["id"] for m in payload["models"]] == ["x/model"]  # x/chat lacks JSON
    assert payload["current"] == "m/1"
    assert "T" in str(payload["fetched_at"])  # ISO timestamp of the cache entry
    assert payload["prompt_name"] == "standard"


def test_templates_view_lists_static_profiles(_rich_deps) -> None:
    payload, status = _run_view("templates")
    assert status == 200
    keys = [t["key"] for t in payload["templates"]]
    assert {"family", "city", "highway", "first", "bargain"} <= set(keys)
    assert all(t["criteria"] for t in payload["templates"])


def test_health_view_reports_pipeline(_rich_deps) -> None:
    payload, status = _run_view("health")
    assert status == 200
    assert payload["queue_pending"] == 0
    assert payload["spend_30d_usd"] == 0.01
    assert payload["database"] is True
    assert payload["budget_usd"] == 1.0
    assert "budget_usd" in payload


def test_digest_view_summarizes_week(_rich_deps) -> None:
    payload, status = _run_view("digest")
    assert status == 200
    assert payload["days"] == 7
    assert payload["new_total"] == 3
    assert payload["drops"][0]["pct"] == 16.7
    assert payload["top"][0]["score"] == 88
    assert payload["queue_pending"] == 0


def test_post_actions_model_prompt_fallbacks(_fresh_settings, _rich_deps) -> None:
    store, _ = _rich_deps
    init_data = _make_init_data(user_id=42)
    _, payload = _call_post({"action": "model", "id": "x/model"}, init_data)
    assert payload["ok"] is True  # queue fails safe without a real DB
    assert ("prefs", {"model": "x/model"}) in [c for c in store.calls if c[0] == "prefs"]
    _, payload = _call_post({"action": "model"}, init_data)
    assert payload["error"] == "id required"
    _, payload = _call_post({"action": "prompt", "name": "bargain"}, init_data)
    assert payload["ok"] is True
    assert ("prefs", {"prompt_name": "bargain"}) in [c for c in store.calls if c[0] == "prefs"]
    _, payload = _call_post({"action": "prompt", "name": "nope"}, init_data)
    assert "unknown prompt" in payload["error"]
    _, payload = _call_post(
        {"action": "fallbacks", "models": ["a/one", "b/two"]}, init_data
    )
    assert payload == {"ok": True, "fallback_models": ["a/one", "b/two"]}
    _, payload = _call_post({"action": "fallbacks", "models": "a/one;b/two"}, init_data)
    assert payload["fallback_models"] == ["a/one", "b/two"]


def test_post_actions_filters_searches_template_eval_rescore(
    _fresh_settings, _rich_deps,
) -> None:
    store, _ = _rich_deps
    init_data = _make_init_data(user_id=42)
    _, payload = _call_post(
        {"action": "filters",
         "filters": {"query": "octavia", "min_price": "1000", "psc": "81101"}},
        init_data,
    )
    assert payload == {"ok": True, "filters": {"query": "octavia"}}
    assert ("filters", {"query": "octavia", "min_price": 1000, "psc": "81101"}) in store.calls
    _, payload = _call_post({"action": "filters", "filters": "nope"}, init_data)
    assert payload["error"] == "filters object required"
    _, payload = _call_post(
        {"action": "search_create", "name": "hwy", "criteria": "diesel"}, init_data
    )
    assert payload == {"ok": True}
    assert ("search_create", "hwy", "diesel") in store.calls
    _, payload = _call_post({"action": "search_create", "name": "x"}, init_data)
    assert payload["error"] == "name + criteria required"
    _, payload = _call_post({"action": "search_delete", "name": "hwy"}, init_data)
    assert payload == {"ok": True, "removed": True}
    _, payload = _call_post({"action": "template", "key": "city"}, init_data)
    assert payload["ok"] is True
    assert ("prefs", {"criteria": "small city car, low fuel consumption, easy maintenance, under 120,000 km",
                      "filters": {"min_price": 1000, "max_price": 5000},
                      "min_score": 60}) in [c for c in store.calls if c[0] == "prefs"]
    _, payload = _call_post({"action": "template", "key": "nope"}, init_data)
    assert payload["error"] == "unknown template"
    _, payload = _call_post({"action": "eval", "limit": 30, "days": 7}, init_data)
    assert payload["ok"] is True and payload["queued"] is False  # no real DB to queue
    _, payload = _call_post({"action": "rescore", "ids": [7, 8]}, init_data)
    assert payload["ok"] is True and payload["ids"] == [7, 8]
    _, payload = _call_post({"action": "rescore", "ids": []}, init_data)
    assert payload["error"] == "ids required"
