"""Pydantic models shared across the pipeline."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class Listing(BaseModel):
    """One real estate (car) listing as extracted from a Bazoš page.

    ``year`` and ``mileage_km`` are heuristically parsed from the free-text
    preview because Bazoš does not expose them as structured fields.
    """

    model_config = ConfigDict(extra="forbid")

    ad_id: int
    url: str
    title: str
    price_eur: Decimal | None = None
    price_raw: str | None = None
    city: str | None = None
    postal_code: str | None = None
    published_date: date | None = None
    views: int | None = None
    description_preview: str
    description: str | None = None
    image_urls: list[str] = Field(default_factory=list)
    year: int | None = None
    mileage_km: int | None = None
    source: str = "bazos"
    evaluation: DealEvaluation | None = None


class DealEvaluation(BaseModel):
    """LLM deal-score for one listing (Phase 3): AI ohodnotí inzerát + ``why``."""

    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=0, le=100)
    why: str = ""
    risk: str = ""
    is_car: bool = True
    model: str | None = None


class DbSyncSummary(BaseModel):
    """Database persistence result of one scrape run (Phase 2)."""

    model_config = ConfigDict(extra="forbid")

    total: int = 0
    inserted: int = 0
    price_changed: int = 0
    price_drops: int = 0
    unchanged: int = 0


class ScrapeSummary(BaseModel):
    """Result of one scrape run."""

    model_config = ConfigDict(extra="forbid")

    source: str
    pages_scraped: list[str] = Field(default_factory=list)
    total_found: int = 0
    exported: int = 0
    export_path: str | None = None
    evaluated: int = 0
    notified: int = 0
    db: DbSyncSummary | None = None
