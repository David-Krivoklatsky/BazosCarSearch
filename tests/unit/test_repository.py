"""Unit tests for DB sync classification (offline — no database needed)."""

from __future__ import annotations

from decimal import Decimal

from bazcar.core.models import Listing
from bazcar.db import plan_sync


def _listing(ad_id: int, price: Decimal | None) -> Listing:
    return Listing(
        ad_id=ad_id,
        url=f"https://auto.bazos.sk/inzerat/{ad_id}/some-slug.php",
        title=f"Ad {ad_id}",
        price_eur=price,
        description_preview="preview",
    )


def test_plan_new_inserts_and_records_first_history():
    plan = plan_sync([_listing(1, Decimal("5000"))], existing={})
    assert plan.stats.total == 1
    assert plan.stats.inserted == 1
    assert len(plan.upserts) == 1
    assert plan.history == [(1, Decimal("5000"))]


def test_plan_price_drop_detected():
    plan = plan_sync([_listing(1, Decimal("5500"))], existing={1: Decimal("6000")})
    assert plan.stats.price_changed == 1
    assert plan.stats.price_drops == 1
    assert plan.history == [(1, Decimal("5500"))]


def test_plan_price_increase_is_change_but_not_drop():
    plan = plan_sync([_listing(1, Decimal("5500"))], existing={1: Decimal("5000")})
    assert plan.stats.price_changed == 1
    assert plan.stats.price_drops == 0


def test_plan_unchanged_refreshes_without_history():
    plan = plan_sync([_listing(1, Decimal("5000"))], existing={1: Decimal("5000")})
    assert plan.stats.unchanged == 1
    assert plan.history == []
    assert len(plan.upserts) == 1


def test_plan_price_to_none_counts_as_change():
    plan = plan_sync([_listing(1, None)], existing={1: Decimal("5000")})
    assert plan.stats.price_changed == 1
    assert plan.history == [(1, None)]


def test_plan_existing_unknown_price_is_not_new():
    """An ad stored with NULL price must NOT be counted as inserted again."""
    plan = plan_sync([_listing(1, None)], existing={1: None})
    assert plan.stats.inserted == 0
    assert plan.stats.unchanged == 1
    assert plan.history == []
    assert plan.inserted_ids == []


def test_plan_unknown_price_to_known_counts_as_change():
    plan = plan_sync([_listing(1, Decimal("5000"))], existing={1: None})
    assert plan.stats.inserted == 0
    assert plan.stats.price_changed == 1
    assert plan.history == [(1, Decimal("5000"))]


def test_plan_price_drop_events():
    plan = plan_sync([_listing(1, Decimal("5500"))], existing={1: Decimal("6000")})
    assert plan.price_drop_events == [(1, Decimal("6000"), Decimal("5500"))]


def test_plan_no_drop_event_on_increase_or_new():
    up = plan_sync([_listing(1, Decimal("6000"))], existing={1: Decimal("5500")})
    assert up.price_drop_events == []
    new = plan_sync([_listing(2, Decimal("3000"))], existing={})
    assert new.price_drop_events == []


def test_plan_mixed_batch():
    existing = {1: Decimal("5000"), 2: Decimal("9000"), 3: Decimal("7000")}
    listings = [
        _listing(1, Decimal("5000")),  # unchanged
        _listing(2, Decimal("8000")),  # drop
        _listing(3, Decimal("7000")),  # unchanged
        _listing(4, Decimal("4000")),  # new
    ]
    plan = plan_sync(listings, existing)
    assert plan.stats.as_dict() == {
        "total": 4,
        "inserted": 1,
        "price_changed": 1,
        "price_drops": 1,
        "unchanged": 2,
    }
    assert len(plan.history) == 2
    assert len(plan.upserts) == 4


def test_plan_empty_input():
    plan = plan_sync([], existing={})
    assert plan.stats.total == 0
    assert plan.upserts == []
    assert plan.history == []


def test_schema_bootstrap_covers_code_selected_columns():
    """init_schema must create every newer column the code SELECTs/writes.

    The webhook auto-migrates on each call, but a deploy that selects a
    column before any migration ran would 500 — this pins the DDL side.
    """
    from bazcar.db.repository import SCHEMA_SQL

    for column in (
        "prompt_tokens",
        "completion_tokens",
        "cost_usd",
        "note",
        "alert_below",
        "notify_enabled",
        "condition",
        "target_offer",
    ):
        assert column in SCHEMA_SQL, f"schema bootstrap missing {column}"


def test_upsert_evaluation_sql_matches_usage_columns():
    """The evaluation upsert must persist token usage (12 params)."""
    from bazcar.db.repository import _UPSERT_EVALUATION_SQL

    assert _UPSERT_EVALUATION_SQL.count("$") == 12
    for column in ("prompt_tokens", "completion_tokens", "cost_usd"):
        assert column in _UPSERT_EVALUATION_SQL

class _RecordingConn:
    """Minimal asyncpg stand-in: records execute/fetch calls."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.executes: list = []
        self.fetches: list = []

    async def execute(self, sql, *args):
        self.executes.append((sql, args))
        return "DELETE 1" if sql.strip().upper().startswith("DELETE") else "INSERT 0 1"

    async def fetch(self, sql, *args):
        self.fetches.append((sql, args))
        return self.rows


def _repo_with(conn) -> object:
    from bazcar.db import ListingRepository

    repo = ListingRepository("postgres://fake")
    repo._conn = conn
    return repo


def test_create_eval_task_defaults_survive_none_ids() -> None:
    """Regression: queueing without ad_ids must not raise (model/prompt/eval)."""
    conn = _RecordingConn()
    repo = _repo_with(conn)
    import asyncio

    asyncio.run(repo.create_eval_task("pk", "crit", "m/1", 100))
    assert len(conn.executes) == 1
    sql, args = conn.executes[0]
    assert "prompt_name" in sql and "ad_ids" in sql
    # (profile_key, criteria, model, max_listings, days, prompt, fallbacks, ad_ids)
    assert args[5] == "standard"
    assert args[7] is None  # NULL ad_ids = whole-window task


def test_create_eval_task_targeted_ids_prompt_and_fallbacks() -> None:
    import asyncio
    import json

    conn = _RecordingConn()
    repo = _repo_with(conn)
    asyncio.run(
        repo.create_eval_task(
            "pk", "crit", "m/1", 20, days=365, prompt_name="bargain",
            fallback_models=["a/1", "b/2"], ad_ids=[7, 0, 8],
        )
    )
    _, args = conn.executes[0]
    assert args[5] == "bargain"
    assert json.loads(args[6]) == ["a/1", "b/2"]
    assert args[7] == [7, 8]  # non-positive ids dropped


def test_fetch_and_delete_by_ids_tolerate_none() -> None:
    import asyncio

    class _ExplodingConn(_RecordingConn):
        async def execute(self, sql, *args):
            raise AssertionError("no SQL expected")

        async def fetch(self, sql, *args):
            raise AssertionError("no SQL expected")

    repo = _repo_with(_ExplodingConn())
    assert asyncio.run(repo.fetch_listings_by_ids(None)) == []
    assert asyncio.run(repo.delete_evaluations(None, "pk")) == 0


def test_pending_tasks_parse_null_json_columns() -> None:
    """Pre-migration/NULL fallback_models + ad_ids must parse, not crash."""
    import asyncio

    conn = _RecordingConn(rows=[{
        "id": 1, "profile_key": "pk", "criteria": "c", "model": "m",
        "max_listings": 10, "days": 3, "attempts": 0, "total": 0, "done": 0,
        "started_at": None, "created_at": None, "prompt_name": None,
        "fallback_models": None, "ad_ids": None,
    }])
    repo = _repo_with(conn)
    tasks = asyncio.run(repo.pending_eval_tasks())
    assert tasks[0]["fallback_models"] == []
    assert tasks[0]["ad_ids"] is None
