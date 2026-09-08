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
