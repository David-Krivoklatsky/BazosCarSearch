"""Schema / model tests."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from bazcar.core.models import Listing, ScrapeSummary


def test_listing_accepts_valid_input() -> None:
    listing = Listing(
        ad_id=1,
        url="https://auto.bazos.sk/inzerat/1/x.php",
        title="Test Auto",
        price_eur=Decimal("22500"),
        description_preview="text",
    )
    assert listing.ad_id == 1
    assert listing.price_eur == Decimal("22500")
    assert listing.source == "bazos"


def test_listing_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Listing(
            ad_id=1,
            url="https://auto.bazos.sk/",
            title="x",
            description_preview="x",
            unexpected="boom",
        )


def test_listing_requires_core_fields() -> None:
    with pytest.raises(ValidationError):
        Listing(ad_id=1, url="https://auto.bazos.sk/")


def test_scrape_summary_defaults() -> None:
    summary = ScrapeSummary(source="bazos", total_found=0)
    assert summary.exported == 0
    assert summary.pages_scraped == []
    assert summary.export_path is None
