"""Scrape runner: fetch -> parse -> validate -> export JSON."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from bazcar.config.settings import ScraperConfig, get_settings, load_scraper_config
from bazcar.core.models import Listing, ScrapeSummary
from bazcar.scrapers.factory import get_scraper

logger = logging.getLogger(__name__)


async def run_scrape(
    *,
    platform: str = "bazos",
    max_pages: int = 1,
    limit: int | None = None,
    detail: bool = True,
    detail_limit: int = 0,
    export_path: Path | None = None,
    config: ScraperConfig | None = None,
) -> ScrapeSummary:
    """Scrape using configured search filters for up to ``max_pages`` pages and export to JSON.

    Returns a ``ScrapeSummary`` describing the run.
    """
    settings = get_settings()
    cfg = config or load_scraper_config()

    listings: list[Listing] = []
    pages: list[str] = []
    async with get_scraper(platform, cfg) as scraper:
        found = await scraper.scrape_category(max_pages=max_pages)
        pages = [scraper._page_url(i) for i in range(max_pages)]
        listings = found
        if detail:
            for i, listing in enumerate(listings):
                if detail_limit and i >= detail_limit:
                    break
                await scraper.scrape_detail(listing)

    if limit is not None:
        listings = listings[:limit]

    # Include filters hash in filename to separate different filter combinations
    filters_tag = getattr(scraper, "filters_hash", "default")
    target = export_path or (settings.export_dir / _default_filename(filters_tag))
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_json(target, listings)

    summary = ScrapeSummary(
        source=platform,
        pages_scraped=pages,
        total_found=len(listings),
        exported=len(listings),
        export_path=str(target),
    )
    logger.info(
        "scrape done: %d listings exported to %s",
        summary.exported,
        summary.export_path,
    )
    return summary


def _default_filename(filters_tag: str = "default") -> str:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"bazos_{filters_tag}_{ts}.json"


def _write_json(path: Path, listings: list[Listing]) -> None:
    payload = {
        "exported_at": datetime.now(UTC).isoformat(),
        "count": len(listings),
        "items": [item.model_dump(mode="json") for item in listings],
    }
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
