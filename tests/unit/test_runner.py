"""Unit tests for the multi-user scrape pipeline (offline — no network/DB).

Covers the pure/scope logic that makes one scrape serve every registered chat:
filter-scope dedupe, YAML override semantics, the prefs loader (including its
legacy fallback) and price-drop alert routing.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from bazcar.bot.store import UserPrefs
from bazcar.config.settings import load_scraper_config
from bazcar.core.models import Listing
from bazcar.pipeline.runner import (
    EVAL_SCHEMA_VERSION,
    _apply_filters,
    _distinct_scopes,
    _legacy_prefs,
    _load_all_prefs,
    _notify_price_drops,
    profile_key,
)


class _Settings:
    """Minimal Settings stub — only the fields the loader reads."""

    def __init__(self, **kw):
        self.database_url = kw.get("database_url")
        self.telegram_chat_id = kw.get("telegram_chat_id")
        self.telegram_min_score = kw.get("telegram_min_score", 70)
        self.telegram_bot_token = kw.get("telegram_bot_token")
        self.openrouter_api_key = kw.get("openrouter_api_key")
        self.opencode_api_key = kw.get("opencode_api_key")
        self.openrouter_model = kw.get("openrouter_model")
        self.monthly_budget_usd = kw.get("monthly_budget_usd")

    @property
    def llm_api_key(self):
        return self.openrouter_api_key or self.opencode_api_key


def _run(coro):
    return asyncio.run(coro)


def _user(chat_id: int, filters, criteria=None, model=None, min_score=None):
    return {
        "chat_id": chat_id,
        "criteria": criteria,
        "model": model,
        "min_score": min_score,
        "show_photo": False,
        "filters": filters,
    }


# ------------------------------------------------------------- filter scopes


def test_apply_filters_none_keeps_yaml_defaults():
    """No user preference -> the YAML scrape scope is untouched."""
    cfg = load_scraper_config()
    before = cfg.base.search_filters.model_dump()
    assert _apply_filters(cfg, None) is cfg
    assert cfg.base.search_filters.model_dump() == before


def test_apply_filters_empty_dict_means_no_filters():
    """``/filter clear`` -> genuinely empty scope (whole catalogue)."""
    cfg = load_scraper_config()
    out = _apply_filters(cfg, {})
    assert out is not cfg  # copy, never the original
    dumped = out.base.search_filters.model_dump()
    assert not any(dumped.values()), dumped


def test_apply_filters_overrides_and_does_not_mutate_original():
    cfg = load_scraper_config()
    original = cfg.base.search_filters.model_dump()
    out = _apply_filters(cfg, {"query": "octavia", "min_price": 1000, "max_price": 9000})
    assert out.base.search_filters.query == "octavia"
    assert out.base.search_filters.min_price == 1000
    assert out.base.search_filters.max_price == 9000
    assert cfg.base.search_filters.model_dump() == original


def test_distinct_scopes_no_users_uses_yaml_scope():
    assert _distinct_scopes([]) == [None]


def test_distinct_scopes_dedupes_identical_filter_sets():
    users = [
        _user(1, {"query": "golf"}),
        _user(2, {"query": "golf"}),  # same filters -> one scrape
        _user(3, {"query": "octavia"}),
        _user(4, None),               # YAML scope
        _user(5, None),               # duplicate YAML scope
    ]
    scopes = _distinct_scopes(users)
    assert scopes == [{"query": "golf"}, {"query": "octavia"}, None]


def test_distinct_scopes_treats_empty_and_none_as_different():
    """{} (no filters) and None (YAML defaults) are different scrape scopes."""
    scopes = _distinct_scopes([_user(1, None), _user(2, {})])
    assert scopes == [None, {}]


# ---------------------------------------------------------------- profile key


def test_profile_key_is_stable_and_versioned():
    a = profile_key("diesel")
    assert a == profile_key("diesel")
    # the schema version is hashed in -> a contract change invalidates old evals
    assert a != profile_key("diesel", schema_version="v1")
    assert profile_key("diesel", schema_version=EVAL_SCHEMA_VERSION) == a
    # criteria matters (and None differs from a real criteria string)
    assert profile_key("diesel") != profile_key("benzin")
    assert profile_key(None) != profile_key("diesel")


def test_profile_key_is_model_independent() -> None:
    """Switching the AI model must NOT fork the score cache (scores are reused)."""
    from bazcar.core.profiles import legacy_profile_key

    # the model argument is gone from the key entirely: same spec, same key
    assert profile_key("diesel") == profile_key("diesel")
    # the legacy (model-bound) key is still computable for the migration
    assert legacy_profile_key("diesel", "m/1") != legacy_profile_key("diesel", "m/2")
    assert legacy_profile_key("diesel", "m/1") != profile_key("diesel")


# ---------------------------------------------------------------- prefs loader


def test_legacy_prefs_shape():
    prefs = _legacy_prefs(_Settings(telegram_chat_id="111", telegram_min_score=80))
    assert prefs["chat_id"] == 111
    assert prefs["criteria"] is None
    assert prefs["min_score"] == 80
    assert prefs["filters"] is None  # None -> YAML scope, never an override


def test_load_all_prefs_without_db_returns_legacy_or_empty(monkeypatch):
    monkeypatch.setattr(
        "bazcar.pipeline.runner.get_settings", lambda: _Settings(telegram_chat_id=None)
    )
    assert _run(_load_all_prefs()) == []


def test_load_all_prefs_reads_store_and_appends_legacy(monkeypatch):
    monkeypatch.setattr(
        "bazcar.pipeline.runner.get_settings",
        lambda: _Settings(database_url="postgres://x", telegram_chat_id="999"),
    )

    class _Store:
        def __init__(self, dsn):
            self.dsn = dsn

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def list_all_prefs(self):
            return [
                UserPrefs(chat_id=1, criteria="diesel", model="m/1", min_score=70),
                UserPrefs(chat_id=999, criteria="benzin"),  # already registered
            ]

    monkeypatch.setattr("bazcar.bot.store.UserStore", _Store)
    users = _run(_load_all_prefs())
    by_id = {u["chat_id"]: u for u in users}
    assert set(by_id) == {1, 999}
    assert by_id[1]["criteria"] == "diesel"
    assert by_id[1]["model"] == "m/1"
    assert by_id[1]["min_score"] == 70
    # the registered row wins over the synthesized legacy entry
    assert by_id[999]["criteria"] == "benzin"


def test_load_all_prefs_survives_store_failure(monkeypatch):
    """A broken store must not kill the pipeline — fall back to legacy."""
    monkeypatch.setattr(
        "bazcar.pipeline.runner.get_settings",
        lambda: _Settings(database_url="postgres://x", telegram_chat_id="42"),
    )

    class _Broken:
        def __init__(self, dsn):
            raise RuntimeError("db down")

    monkeypatch.setattr("bazcar.bot.store.UserStore", _Broken)
    users = _run(_load_all_prefs())
    assert [u["chat_id"] for u in users] == [42]


# ------------------------------------------------------------ price-drop alerts


class _FakeNotifier:
    def __init__(self, token: str = "", chat_id: str = "") -> None:
        self.sent: list[tuple[int, str]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def send_message(self, text, *, chat_id=None, reply_markup=None, silent=False):
        self.sent.append((chat_id, text))
        return True


def _listing(ad_id: int = 1, price=Decimal("5000")) -> Listing:
    return Listing(
        ad_id=ad_id,
        url=f"https://auto.bazos.sk/inzerat/{ad_id}/x.php",
        title="Škoda Octavia 1.9 TDI",
        price_eur=price,
        description_preview="popis",
    )


def test_price_drops_noop_without_token(monkeypatch):
    monkeypatch.setattr(
        "bazcar.pipeline.runner.get_settings",
        lambda: _Settings(telegram_bot_token=None, database_url="postgres://x"),
    )
    assert _run(_notify_price_drops([_listing()], [(1, Decimal("6000"), Decimal("5000"))])) == 0


def test_price_drops_noop_without_savers(monkeypatch):
    monkeypatch.setattr(
        "bazcar.pipeline.runner.get_settings",
        lambda: _Settings(telegram_bot_token="T", database_url="postgres://x"),
    )

    class _Repo:
        def __init__(self, dsn):
            pass

        async def connect(self):
            return None

        async def close(self):
            return None

        async def fetch_chats_saving(self, ad_ids):
            return {}  # nobody saved it

    monkeypatch.setattr("bazcar.db.ListingRepository", _Repo)
    monkeypatch.setattr("bazcar.notify.TelegramNotifier", _FakeNotifier)
    assert _run(_notify_price_drops([_listing()], [(1, Decimal("6000"), Decimal("5000"))])) == 0


def test_price_drops_notify_savers_with_delta(monkeypatch):
    monkeypatch.setattr(
        "bazcar.pipeline.runner.get_settings",
        lambda: _Settings(telegram_bot_token="T", database_url="postgres://x"),
    )

    class _Repo:
        def __init__(self, dsn):
            pass

        async def connect(self):
            return None

        async def close(self):
            return None

        async def fetch_chats_saving(self, ad_ids):
            assert ad_ids == [1]
            return {1: [7, 8]}  # two users saved this ad

    notifier = _FakeNotifier()
    notifier.sent = []
    monkeypatch.setattr("bazcar.db.ListingRepository", _Repo)
    monkeypatch.setattr("bazcar.notify.TelegramNotifier", lambda *a, **kw: notifier)
    sent = _run(_notify_price_drops([_listing()], [(1, Decimal("6000"), Decimal("5000"))]))
    assert sent == 2
    assert [chat for chat, _ in notifier.sent] == [7, 8]
    text = notifier.sent[0][1]
    assert "Price drop 17%" in text  # 6000 -> 5000
    assert "6 000 €" in text and "5 000 €" in text


def test_price_drops_skips_event_for_unknown_listing(monkeypatch):
    """A drop for an ad not in this scrape batch has nothing to render."""
    monkeypatch.setattr(
        "bazcar.pipeline.runner.get_settings",
        lambda: _Settings(telegram_bot_token="T", database_url="postgres://x"),
    )

    class _Repo:
        def __init__(self, dsn):
            pass

        async def connect(self):
            return None

        async def close(self):
            return None

        async def fetch_chats_saving(self, ad_ids):
            return {1: [7]}

    notifier = _FakeNotifier()
    notifier.sent = []
    monkeypatch.setattr("bazcar.db.ListingRepository", _Repo)
    monkeypatch.setattr("bazcar.notify.TelegramNotifier", lambda *a, **kw: notifier)
    sent = _run(_notify_price_drops([_listing(ad_id=99)], [(1, Decimal("6000"), Decimal("5000"))]))
    assert sent == 0
    assert notifier.sent == []


@pytest.mark.parametrize("total", [0])
def test_bar_total_zero_is_safe(total: int) -> None:
    from bazcar.bot.daemon import bar

    assert len(bar(1, total)) == 10


# ------------------------------------------------- per-user match sweep


def _match_row(ad_id: int, score: int = 90, is_car: bool = True) -> dict:
    """Row shaped like ``fetch_recent_matches`` output."""
    return {
        "ad_id": ad_id,
        "url": f"https://auto.bazos.sk/inzerat/{ad_id}/x.php",
        "title": f"Škoda Octavia {ad_id}",
        "price_eur": Decimal("8000"),
        "city": "BA",
        "image_urls": [],
        "year": 2019,
        "mileage_km": 150000,
        "description_preview": "popis",
        "published_date": None,
        "score": score,
        "why": "ok",
        "risk": None,
        "is_car": is_car,
        "eval_model": "m/1",
    }


class _SweepNotifier(_FakeNotifier):
    def __init__(self, token: str = "", chat_id: str = "") -> None:
        super().__init__()

    async def send_listings(self, listings, *, chat_id=None):
        self.sent.append((chat_id, ",".join(str(li.ad_id) for li in listings)))
        return len(listings)

    async def send_listings_with_photos(self, listings, *, chat_id=None):
        return await self.send_listings(listings, chat_id=chat_id)


class _SweepRepo:
    """Repo stub: returns rows per profile and remembers what was recorded."""

    rows_by_profile: dict[str, list[dict]] = {}
    already: set[tuple[int, str, int]] = set()
    recorded: list[tuple[int, str, list[int]]] = []

    def __init__(self, dsn: str) -> None:
        pass

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def fetch_recent_matches(self, profile_key, min_score, days, limit):
        return list(self.rows_by_profile.get(profile_key, []))

    async def fetch_notified_ids(self, chat_id, profile_key, ad_ids):
        return {ad for (c, p, ad) in self.already if c == chat_id and p == profile_key and ad in ad_ids}

    async def record_notifications(self, chat_id, profile_key, ad_ids):
        type(self).recorded.append((chat_id, profile_key, list(ad_ids)))


def _sweep_settings(monkeypatch):
    _SweepRepo.rows_by_profile = {}
    _SweepRepo.already = set()
    _SweepRepo.recorded = []
    notifier = _SweepNotifier()
    notifier.sent = []
    monkeypatch.setattr(
        "bazcar.pipeline.runner.get_settings",
        lambda: _Settings(
            telegram_bot_token="T",
            telegram_chat_id="0",
            database_url="postgres://x",
            telegram_min_score=70,
        ),
    )
    monkeypatch.setattr("bazcar.db.ListingRepository", _SweepRepo)
    monkeypatch.setattr("bazcar.notify.TelegramNotifier", lambda *a, **kw: notifier)
    return notifier


def test_match_sweep_sends_only_unseen_per_user(monkeypatch):
    from bazcar.pipeline.runner import _notify_unseen_matches

    notifier = _sweep_settings(monkeypatch)
    user = _user(7, None, criteria="diesel", model="m/1", min_score=70)
    p_key = profile_key("diesel")
    _SweepRepo.rows_by_profile[p_key] = [_match_row(1), _match_row(2)]
    _SweepRepo.already = {(7, p_key, 1)}  # ad 1 already pushed

    sent = _run(_notify_unseen_matches([user], enabled=True))
    assert sent == 1
    assert notifier.sent == [(7, "2")]
    assert _SweepRepo.recorded == [(7, p_key, [2])]


def test_match_sweep_isolates_users_by_profile(monkeypatch):
    """Two users with different criteria get their own alerts, not each other's."""
    from bazcar.pipeline.runner import _notify_unseen_matches

    notifier = _sweep_settings(monkeypatch)
    diesel = _user(7, None, criteria="diesel", model="m/1", min_score=70)
    benzin = _user(8, None, criteria="benzin", model="m/1", min_score=70)
    key_d, key_b = profile_key("diesel"), profile_key("benzin")
    _SweepRepo.rows_by_profile[key_d] = [_match_row(1)]
    _SweepRepo.rows_by_profile[key_b] = [_match_row(2)]
    sent = _run(_notify_unseen_matches([diesel, benzin], enabled=True))
    assert sent == 2
    assert (7, "1") in notifier.sent and (8, "2") in notifier.sent
    recorded = {(c, p): ads for c, p, ads in _SweepRepo.recorded}
    assert recorded[(7, key_d)] == [1]
    assert recorded[(8, key_b)] == [2]


def test_match_sweep_skips_parts(monkeypatch):
    from bazcar.pipeline.runner import _notify_unseen_matches

    notifier = _sweep_settings(monkeypatch)
    user = _user(7, None, criteria="diesel", model="m/1", min_score=70)
    p_key = profile_key("diesel")
    _SweepRepo.rows_by_profile[p_key] = [
        _match_row(1, is_car=False),  # flagged as a part by the LLM
        _match_row(2, is_car=True),
    ]
    sent = _run(_notify_unseen_matches([user], enabled=True))
    assert sent == 1
    assert notifier.sent == [(7, "2")]


def test_match_sweep_respects_per_run_cap(monkeypatch):
    from bazcar.pipeline.runner import NOTIFY_CAP_PER_RUN, _notify_unseen_matches

    notifier = _sweep_settings(monkeypatch)
    user = _user(7, None, criteria="diesel", model="m/1", min_score=70)
    p_key = profile_key("diesel")
    _SweepRepo.rows_by_profile[p_key] = [_match_row(i) for i in range(1, 60)]
    sent = _run(_notify_unseen_matches([user], enabled=True))
    assert sent == NOTIFY_CAP_PER_RUN
    # the batch is capped, and only the delivered ads are recorded
    _chat, pushed = notifier.sent[0]
    assert len(pushed.split(",")) == NOTIFY_CAP_PER_RUN
    assert _SweepRepo.recorded == [(7, p_key, [int(a) for a in pushed.split(",")])]


def test_match_sweep_no_users_is_zero(monkeypatch):
    from bazcar.pipeline.runner import _notify_unseen_matches

    _sweep_settings(monkeypatch)
    assert _run(_notify_unseen_matches([], enabled=True)) == 0


def test_match_sweep_respects_show_photo(monkeypatch):
    from bazcar.pipeline.runner import _notify_unseen_matches

    notifier = _sweep_settings(monkeypatch)
    user = _user(7, None, criteria="diesel", model="m/1", min_score=70)
    user["show_photo"] = True
    p_key = profile_key("diesel")
    _SweepRepo.rows_by_profile[p_key] = [_match_row(1)]
    assert _run(_notify_unseen_matches([user], enabled=True)) == 1
    assert notifier.sent == [(7, "1")]


def test_sweep_profiles_dedupes_active_and_searches():
    from bazcar.pipeline.runner import _sweep_profiles

    class _S:
        telegram_min_score = 70

    user = {
        "criteria": "diesel", "model": "m/1", "min_score": 80,
        "searches": [
            {"name": "d", "criteria": "diesel", "min_score": 80},
            {"name": "b", "criteria": "benzin", "min_score": None},
        ],
    }
    profiles = _sweep_profiles(user, _S(), "m/1")
    assert profiles[0] == (profile_key("diesel"), 80)
    assert (profile_key("benzin"), 70) in profiles
    assert len(profiles) == 2  # duplicate diesel collapsed


def test_match_sweep_skips_notify_disabled_user(monkeypatch):
    from bazcar.pipeline.runner import _notify_unseen_matches

    notifier = _sweep_settings(monkeypatch)
    user = _user(7, None, criteria="diesel", model="m/1", min_score=70)
    user["notify_enabled"] = False
    p_key = profile_key("diesel")
    _SweepRepo.rows_by_profile[p_key] = [_match_row(1)]
    assert _run(_notify_unseen_matches([user], enabled=True)) == 0
    assert notifier.sent == []


def test_match_sweep_covers_enabled_searches(monkeypatch):
    from bazcar.pipeline.runner import _notify_unseen_matches

    notifier = _sweep_settings(monkeypatch)
    user = _user(7, None, criteria="diesel", model="m/1", min_score=70)
    user["searches"] = [{"name": "b", "criteria": "benzin", "min_score": 60}]
    p_active = profile_key("diesel")
    p_search = profile_key("benzin")
    _SweepRepo.rows_by_profile[p_active] = []
    _SweepRepo.rows_by_profile[p_search] = [_match_row(2)]
    assert _run(_notify_unseen_matches([user], enabled=True)) == 1
    assert notifier.sent == [(7, "2")]


def test_price_drops_respect_alert_thresholds(monkeypatch):
    """Saver with alert_below=4000 is skipped when the price only fell to 5000."""
    from bazcar.pipeline.runner import _notify_price_drops

    monkeypatch.setattr(
        "bazcar.pipeline.runner.get_settings",
        lambda: _Settings(telegram_bot_token="T", database_url="postgres://x"),
    )

    class _Repo:
        def __init__(self, dsn):
            pass

        async def connect(self):
            return None

        async def close(self):
            return None

        async def fetch_chats_saving(self, ad_ids):
            return {1: [7, 8]}

    class _Store:
        def __init__(self, dsn):
            pass

        async def connect(self):
            return None

        async def close(self):
            return None

        async def alert_thresholds(self, chat_ids, ad_id):
            return {7: 4000, 8: None}

    notifier = _FakeNotifier()
    notifier.sent = []
    monkeypatch.setattr("bazcar.db.ListingRepository", _Repo)
    monkeypatch.setattr("bazcar.bot.store.UserStore", _Store)
    monkeypatch.setattr("bazcar.notify.TelegramNotifier", lambda *a, **kw: notifier)
    sent = _run(_notify_price_drops([_listing()], [(1, Decimal("6000"), Decimal("5000"))]))
    assert sent == 1
    assert [chat for chat, _ in notifier.sent] == [8]


def test_evaluate_pauses_when_budget_exhausted(monkeypatch):
    """Monthly cap reached -> no fresh LLM calls, cached scores still served."""
    from bazcar.pipeline.runner import _evaluate

    class _Repo:
        def __init__(self, dsn):
            pass

        async def connect(self):
            return None

        async def close(self):
            return None

        async def init_schema(self):
            return None

        async def eval_cost(self, profile_key=None, days=30):
            return {"total": {"n": 10, "cost_usd": 5.0}, "by_model": []}

    monkeypatch.setattr(
        "bazcar.pipeline.runner.get_settings",
        lambda: _Settings(
            telegram_bot_token="T",
            database_url="postgres://x",
            openrouter_api_key="K",
            monthly_budget_usd=1.0,
        ),
    )
    monkeypatch.setattr("bazcar.db.ListingRepository", _Repo)
    users = [_user(7, None, criteria="diesel", model="m/1", min_score=70)]
    assert _run(_evaluate([_listing()], enabled=True, users=users)) == 0


def test_budget_cap_none_or_zero_disables_guard() -> None:
    """BAZCAR_MONTHLY_BUDGET_USD unset or 0 = no cap (never freezes scoring)."""
    from bazcar.pipeline.runner import _budget_cap

    assert _budget_cap(_Settings(monthly_budget_usd=None)) is None
    assert _budget_cap(_Settings(monthly_budget_usd=0)) is None
    assert _budget_cap(_Settings(monthly_budget_usd=-1)) is None
    assert _budget_cap(_Settings(monthly_budget_usd=5)) == 5.0


def test_evaluate_scores_when_budget_is_zero(monkeypatch):
    """A 0 cap must not pause fresh scoring even with prior spend recorded."""
    from bazcar.pipeline.runner import _evaluate

    class _Repo:
        def __init__(self, dsn):
            pass

        async def connect(self):
            return None

        async def close(self):
            return None

        async def init_schema(self):
            return None

        async def eval_cost(self, profile_key=None, days=30):
            return {"total": {"n": 10, "cost_usd": 5.0}, "by_model": []}

    seen: list = []

    async def _fake_evaluate_profile(listings, **kw):
        seen.append(kw.get("model"))
        return len(listings), len(listings)

    monkeypatch.setattr(
        "bazcar.pipeline.runner.get_settings",
        lambda: _Settings(
            telegram_bot_token="T",
            database_url="postgres://x",
            openrouter_api_key="K",
            monthly_budget_usd=0,
        ),
    )
    monkeypatch.setattr("bazcar.db.ListingRepository", _Repo)
    monkeypatch.setattr("bazcar.pipeline.runner._evaluate_profile", _fake_evaluate_profile)
    users = [_user(7, None, criteria="diesel", model="m/1", min_score=70)]
    assert _run(_evaluate([_listing()], enabled=True, users=users)) == 1
    assert seen == ["m/1"]


def test_process_eval_tasks_rechecks_budget_between_tasks(monkeypatch):
    """One run must not drain several paid backfills past the monthly cap."""
    from bazcar.core.models import DealEvaluation
    from bazcar.pipeline.runner import _process_eval_tasks

    spends = [1.0, 9.0]  # spend before task 1, then after task 1 (over a $5 cap)

    def _task(task_id: int, ad_id: int) -> dict:
        return {
            "id": task_id, "profile_key": "pk", "criteria": "diesel", "model": "m/1",
            "max_listings": 5, "days": 3, "attempts": 0, "total": 0, "done": 0,
            "prompt_name": "standard", "fallback_models": [], "ad_ids": [ad_id],
            "force": False, "confirmed": True,
        }

    class _Repo:
        def __init__(self, dsn):
            self._i = 0

        async def connect(self):
            pass

        async def close(self):
            pass

        async def init_schema(self):
            pass

        async def pending_eval_tasks(self):
            return [_task(1, 7), _task(2, 8)]

        async def fetch_listings_by_ids(self, ad_ids):
            return [{"ad_id": ad_ids[0], "url": f"https://x/{ad_ids[0]}", "title": "A",
                     "description_preview": "d"}]

        async def start_eval_task(self, task_id, total):
            pass

        async def set_eval_task_progress(self, task_id, done):
            pass

        async def complete_eval_task(self, task_id):
            pass

        async def eval_cost(self, profile_key=None, days=30):
            value = spends[min(self._i, len(spends) - 1)]
            self._i += 1
            return {"total": {"cost_usd": value}, "by_model": []}

    scored: list = []

    async def _fake_evaluate(listings, **kw):
        scored.append(kw)
        for listing in listings:
            listing.evaluation = DealEvaluation(score=80, why="ok", model=kw.get("model"))
        return len(listings), len(listings)

    monkeypatch.setattr(
        "bazcar.pipeline.runner.get_settings",
        lambda: _Settings(
            telegram_bot_token="T",  # no chat_id -> no report network call
            database_url="postgres://x",
            openrouter_api_key="K",
            monthly_budget_usd=5.0,
        ),
    )
    monkeypatch.setattr("bazcar.db.ListingRepository", _Repo)
    monkeypatch.setattr("bazcar.pipeline.runner._evaluate_profile", _fake_evaluate)
    assert _run(_process_eval_tasks()) == 1  # second task held: spend refreshed to $9
    assert len(scored) == 1


def test_profile_key_prompt_forks_cache() -> None:
    # standard reproduces the historical key (existing caches keep hitting)
    assert profile_key("c") == profile_key("c", prompt="standard")
    assert profile_key("c") != profile_key("c", prompt="bargain")
    assert profile_key("c", prompt="bargain") != profile_key("c", prompt="family")


def test_evaluate_profile_falls_back_to_next_model(monkeypatch):
    from bazcar.core.models import DealEvaluation
    from bazcar.pipeline.runner import _evaluate_profile

    seen: list = []

    class _FakeLLM:
        def __init__(self, api_key, **kw):
            self.model = kw.get("model")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def evaluate(self, listing, *, criteria=None, prompt_name="standard"):
            seen.append((self.model, prompt_name))
            if self.model == "bad/primary":
                return None  # total failure -> fallback must kick in
            return DealEvaluation(score=80, why="ok", is_car=True)

    monkeypatch.setattr("bazcar.llm.LLMProvider", _FakeLLM)
    monkeypatch.setattr(
        "bazcar.pipeline.runner.get_settings",
        lambda: _Settings(database_url="postgres://x", openrouter_api_key="K"),
    )
    cfg = type("C", (), {"base_url": "https://x", "temperature": 0.0,
                         "max_tokens": 10, "top_p": None, "frequency_penalty": None,
                         "presence_penalty": None, "response_format": None,
                         "stream": False})()
    attached, fresh = _run(_evaluate_profile(
        [_listing()], criteria="diesel", model="bad/primary", cfg=cfg, repo=None,
        prompt_name="bargain", fallback_models=["good/second"],
    ))
    assert (attached, fresh) == (1, 1)
    assert ("bad/primary", "bargain") in seen
    assert ("good/second", "bargain") in seen


def test_evaluate_profile_uses_prompt_in_cache_key(monkeypatch):
    from bazcar.pipeline.runner import _evaluate_profile

    keys: list = []

    class _Repo:
        async def fetch_evaluations(self, p_key, ids):
            keys.append(p_key)
            return {}

        async def upsert_evaluations(self, p_key, items):
            keys.append(("write", p_key))

    class _FakeLLM:
        def __init__(self, api_key, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def evaluate(self, listing, *, criteria=None, prompt_name="standard"):
            from bazcar.core.models import DealEvaluation

            return DealEvaluation(score=70, why="ok", is_car=True)

    monkeypatch.setattr("bazcar.llm.LLMProvider", _FakeLLM)
    monkeypatch.setattr(
        "bazcar.pipeline.runner.get_settings",
        lambda: _Settings(database_url="postgres://x", openrouter_api_key="K"),
    )
    cfg = type("C", (), {"base_url": "https://x", "temperature": 0.0,
                         "max_tokens": 10, "top_p": None, "frequency_penalty": None,
                         "presence_penalty": None, "response_format": None,
                         "stream": False})()
    _run(_evaluate_profile([_listing()], criteria="c", model="m", cfg=cfg,
                           repo=_Repo(), prompt_name="family"))
    expected = profile_key("c", prompt="family")
    assert keys[0] == expected
    assert expected != profile_key("c")

def test_process_eval_tasks_targeted_ids_score_fresh(monkeypatch):
    """ad_ids tasks: fetch by ids, drop cached evals, score with prompt+fallbacks."""
    from bazcar.pipeline.runner import _process_eval_tasks

    calls: list = []
    evaluated_kwargs: dict = {}

    class _Repo:
        def __init__(self, dsn):
            pass

        async def connect(self):
            pass

        async def close(self):
            pass

        async def init_schema(self):
            pass

        async def pending_eval_tasks(self):
            return [{
                "id": 9, "profile_key": "pk", "criteria": "diesel", "model": "m/1",
                "max_listings": 2, "days": 365, "attempts": 0, "total": 0, "done": 0,
                "prompt_name": "bargain", "fallback_models": ["f/1"], "ad_ids": [7, 8],
                "force": True, "confirmed": True,
            }]

        async def fetch_listings_by_ids(self, ad_ids):
            calls.append(("fetch_ids", list(ad_ids)))
            return [
                {"ad_id": 7, "url": "https://x/7", "title": "A",
                 "description_preview": "d"},
                {"ad_id": 8, "url": "https://x/8", "title": "B",
                 "description_preview": "d"},
            ]

        async def fetch_recent_listings(self, days, limit):
            raise AssertionError("targeted task must not use the window")

        async def delete_evaluations(self, ad_ids, p_key):
            calls.append(("delete", list(ad_ids), p_key))
            return 2

        async def start_eval_task(self, task_id, total):
            calls.append(("start", task_id, total))

        async def set_eval_task_progress(self, task_id, done):
            pass

        async def complete_eval_task(self, task_id):
            calls.append(("complete", task_id))

        async def bump_eval_task_attempt(self, task_id):
            raise AssertionError("scored task must not bump attempts")

    async def _fake_evaluate(listings, **kw):
        evaluated_kwargs.update(kw)
        return 2, 2

    monkeypatch.setattr("bazcar.db.ListingRepository", _Repo)
    monkeypatch.setattr("bazcar.pipeline.runner._evaluate_profile", _fake_evaluate)
    monkeypatch.setattr(
        "bazcar.pipeline.runner.get_settings",
        lambda: _Settings(database_url="postgres://x", openrouter_api_key="K"),
    )
    assert _run(_process_eval_tasks()) == 1
    assert ("fetch_ids", [7, 8]) in calls
    assert ("complete", 9) in calls
    assert evaluated_kwargs.get("force") is True  # explicit re-score overwrites the cache
    assert evaluated_kwargs.get("prompt_name") == "bargain"
    assert evaluated_kwargs.get("fallback_models") == ["f/1"]

def test_scope_detail_collects_page_stats_and_skips() -> None:
    from bazcar.pipeline.runner import _scope_detail

    class _Scraper:
        last_page_stats = [{"page": 1, "cards": 20}, {"page": 2, "cards": 5}]
        last_parse_skipped = 2

    detail = _scope_detail({"query": "octavia"}, _Scraper(), 25, 3)
    assert detail["filters"] == {"query": "octavia"}
    assert detail["pages"] == [{"page": 1, "cards": 20}, {"page": 2, "cards": 5}]
    assert detail["skipped"] == 2
    assert (detail["cards"], detail["new"]) == (25, 3)
    # scrapers without telemetry degrade to empty (never crash the run)
    detail = _scope_detail(None, object(), 0, 0)
    assert detail == {"filters": None, "pages": [], "skipped": 0, "cards": 0, "new": 0}
