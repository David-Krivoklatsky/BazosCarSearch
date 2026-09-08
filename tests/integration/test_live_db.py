"""Live Neon/Postgres integration test — run with: pytest -m live

Requires BAZCAR_DATABASE_URL (see .env). Verifies the Phase-2 acceptance
criteria: a second sync produces no duplicates, and price changes are tracked
in the price history.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio

from bazcar.config.settings import get_settings
from bazcar.core.models import Listing
from bazcar.db import ListingRepository

pytestmark = pytest.mark.live

TEST_AD_IDS = (900001, 900002)


def _listing(ad_id: int, price: Decimal | None) -> Listing:
    return Listing(
        ad_id=ad_id,
        url=f"https://auto.bazos.sk/inzerat/{ad_id}/live-slug.php",
        title=f"Live Test Car {ad_id}",
        price_eur=price,
        description_preview="live integration test",
    )


@pytest_asyncio.fixture
async def repo() -> ListingRepository:
    settings = get_settings()
    if not settings.database_url:
        pytest.skip("BAZCAR_DATABASE_URL not set")
    instance = ListingRepository(settings.database_url)
    await instance.connect()
    await instance.init_schema()
    yield instance
    try:
        await instance._conn.execute(
            "DELETE FROM listings WHERE ad_id = ANY($1::bigint[])", list(TEST_AD_IDS)
        )
    except Exception:
        pass
    await instance.close()


@pytest.mark.asyncio
async def test_second_sync_produces_no_duplicates_and_tracks_price_changes(
    repo: ListingRepository,
) -> None:
    a = _listing(900001, Decimal("10000"))
    b = _listing(900002, Decimal("20000"))

    first = await repo.sync_many([a, b])
    assert (first.inserted, first.unchanged, first.price_changed) == (2, 0, 0)

    same = await repo.sync_many([a, b])
    assert (same.inserted, same.price_changed, same.unchanged) == (0, 0, 2)

    count = await repo._conn.fetchval(
        "SELECT count(*) FROM listings WHERE ad_id = ANY($1::bigint[])", list(TEST_AD_IDS)
    )
    assert count == 2

    a.price_eur = Decimal("9000")
    changed = await repo.sync_many([a, b])
    assert (changed.price_changed, changed.price_drops) == (1, 1)

    rows = await repo._conn.fetch(
        "SELECT price_eur FROM price_history WHERE ad_id = $1 ORDER BY id", 900001
    )
    assert [r["price_eur"] for r in rows] == [Decimal("10000"), Decimal("9000")]
