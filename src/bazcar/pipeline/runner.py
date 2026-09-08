"""Scrape runner: fetch -> parse -> validate -> export JSON."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from bazcar.config.settings import ScraperConfig, get_settings, load_llm_config, load_scraper_config
from bazcar.core.exceptions import ConfigError
from bazcar.core.models import DbSyncSummary, Listing, ScrapeSummary
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
    persist: bool | None = None,
    evaluate: bool | None = None,
    config: ScraperConfig | None = None,
) -> ScrapeSummary:
    """Scrape using configured search filters for up to ``max_pages`` pages and export to JSON.

    When ``persist`` is enabled (default: auto when ``BAZCAR_DATABASE_URL`` is
    configured) found listings are deduped-inserted into Postgres (Neon) and the
    price history is appended. When ``evaluate`` is enabled (default: auto when
    ``OPENROUTER_API_KEY`` is set) each listing is scored by an LLM. Returns a
    ``ScrapeSummary`` describing the run.
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

    evaluated = 0
    if evaluate is not False:
        evaluated = await _evaluate(listings, enabled=evaluate)

    # Include filters hash in filename to separate different filter combinations
    filters_tag = getattr(scraper, "filters_hash", "default")
    target = export_path or (settings.export_dir / _default_filename(filters_tag))
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_json(target, listings)

    db_summary = await _persist(listings, enabled=persist)

    summary = ScrapeSummary(
        source=platform,
        pages_scraped=pages,
        total_found=len(listings),
        exported=len(listings),
        export_path=str(target),
        evaluated=evaluated,
        db=db_summary,
    )
    logger.info(
        "scrape done: %d listings exported to %s",
        summary.exported,
        summary.export_path,
    )
    return summary


async def _evaluate(listings: list[Listing], *, enabled: bool | None) -> int:
    """Score listings with an LLM unless explicitly disabled or unconfigured.

    Returns how many listings received an evaluation. A single failed LLM call
    is logged and skipped (never fatal to the pipeline).
    """
    if not listings:
        return 0
    settings = get_settings()
    if not settings.openrouter_api_key:
        if enabled is True:
            raise ConfigError("evaluation requested but OPENROUTER_API_KEY is not set")
        return 0

    from bazcar.llm import LLMProvider

    cfg = load_llm_config()
    async with LLMProvider(
        settings.openrouter_api_key,
        base_url=cfg.base_url,
        model=settings.openrouter_model or cfg.model,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        top_p=cfg.top_p,
        frequency_penalty=cfg.frequency_penalty,
        presence_penalty=cfg.presence_penalty,
        response_format=cfg.response_format,
        stream=cfg.stream,
    ) as llm:
        for listing in listings:
            listing.evaluation = await llm.evaluate(listing)
    return sum(1 for listing in listings if listing.evaluation is not None)


async def _persist(listings: list[Listing], *, enabled: bool | None) -> DbSyncSummary | None:
    """Write listings to Postgres unless explicitly disabled or unconfigured."""
    if enabled is False:
        return None
    settings = get_settings()
    if not settings.database_url:
        if enabled is True:
            raise ConfigError("persistence requested but BAZCAR_DATABASE_URL is not set")
        return None

    from bazcar.db import ListingRepository

    async with ListingRepository(settings.database_url) as repo:
        await repo.init_schema()
        stats = await repo.sync_many(listings)
    return DbSyncSummary(**stats.as_dict())


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
