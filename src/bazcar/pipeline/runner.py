"""Scrape runner: fetch -> parse -> validate -> export JSON."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from bazcar.config.settings import (
    ScraperConfig,
    SearchFiltersConfig,
    get_settings,
    load_llm_config,
    load_scraper_config,
)
from bazcar.core.exceptions import ConfigError, LlmError
from bazcar.core.models import DbSyncSummary, Listing, ScrapeSummary
from bazcar.scrapers.factory import get_scraper

logger = logging.getLogger(__name__)


def _apply_filters(cfg: ScraperConfig, filters: dict) -> ScraperConfig:
    """Override the YAML scrape filters with the user's bot-configured ones."""
    if not filters:
        return cfg
    cfg = cfg.model_copy(deep=True)
    cfg.base.search_filters = SearchFiltersConfig.model_validate(filters)
    return cfg


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
    notify: bool | None = None,
    config: ScraperConfig | None = None,
) -> ScrapeSummary:
    """Scrape using configured search filters for up to ``max_pages`` pages and export to JSON.

    When ``persist`` is enabled (default: auto when ``BAZCAR_DATABASE_URL`` is
    configured) found listings are deduped-inserted into Postgres (Neon) and the
    price history is appended. When ``evaluate`` is enabled (default: auto when
    ``OPENROUTER_API_KEY`` is set) each listing is scored by an LLM. When
    ``notify`` is enabled (default: auto when ``TELEGRAM_BOT_TOKEN`` is set)
    newly inserted deals are pushed to Telegram. Returns a ``ScrapeSummary``.
    """
    settings = get_settings()
    cfg = config or load_scraper_config()

    # Bot-configured Bazoš filters override the YAML defaults (per chat).
    prefs = await _load_prefs()
    cfg = _apply_filters(cfg, (prefs or {}).get("filters") or {})

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
        evaluated = await _evaluate(listings, enabled=evaluate, criteria=(prefs or {}).get("criteria"))

    # Include filters hash in filename to separate different filter combinations
    filters_tag = getattr(scraper, "filters_hash", "default")
    target = export_path or (settings.export_dir / _default_filename(filters_tag))
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_json(target, listings)

    db_summary, inserted_ids = await _persist(listings, enabled=persist)

    notified = 0
    if notify is not False:
        notified = await _notify(
            listings,
            inserted_ids=inserted_ids,
            persist_enabled=db_summary is not None,
            prefs=prefs,
            enabled=notify,
        )

    summary = ScrapeSummary(
        source=platform,
        pages_scraped=pages,
        total_found=len(listings),
        exported=len(listings),
        export_path=str(target),
        evaluated=evaluated,
        notified=notified,
        db=db_summary,
    )
    logger.info(
        "scrape done: %d listings exported to %s",
        summary.exported,
        summary.export_path,
    )
    return summary


async def _load_prefs() -> dict | None:
    """Load Telegram-user preferences for the notify chat from Postgres.

    Returns a plain dict (criteria/model/min_score/show_photo/filters) or None
    when the database is unavailable or not configured. Never raises.
    """
    settings = get_settings()
    chat_id = settings.telegram_chat_id
    if not settings.database_url or not chat_id:
        return None
    try:
        from bazcar.bot.store import UserStore

        async with UserStore(settings.database_url) as store:
            prefs = await store.get_prefs(int(chat_id))
            if prefs is None:
                return None
            return {
                "criteria": prefs.criteria,
                "model": prefs.model,
                "min_score": prefs.min_score,
                "show_photo": prefs.show_photo,
                "filters": prefs.filters,
            }
    except Exception:
        return None


async def _evaluate(listings: list[Listing], *, enabled: bool | None, criteria: str | None = None) -> int:
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
    prefs = await _load_prefs()
    model = settings.openrouter_model or (prefs or {}).get("model") or cfg.model
    async with LLMProvider(
        settings.openrouter_api_key,
        base_url=cfg.base_url,
        model=model,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        top_p=cfg.top_p,
        frequency_penalty=cfg.frequency_penalty,
        presence_penalty=cfg.presence_penalty,
        response_format=cfg.response_format,
        stream=cfg.stream,
    ) as llm:
        for listing in listings:
            try:
                listing.evaluation = await llm.evaluate(
                    listing, criteria=criteria or (prefs or {}).get("criteria")
                )
            except LlmError as exc:
                logger.warning("eval failed for ad %s: %s", listing.ad_id, exc)
                listing.evaluation = None
    return sum(1 for listing in listings if listing.evaluation is not None)


async def _persist(
    listings: list[Listing], *, enabled: bool | None
) -> tuple[DbSyncSummary | None, list[int]]:
    """Write listings to Postgres unless explicitly disabled or unconfigured.

    Returns ``(summary, inserted_ids)`` where ``inserted_ids`` are the ad ids
    that were newly created in the database (empty when persistence is off).
    """
    if enabled is False:
        return None, []
    settings = get_settings()
    if not settings.database_url:
        if enabled is True:
            raise ConfigError("persistence requested but BAZCAR_DATABASE_URL is not set")
        return None, []

    from bazcar.db import ListingRepository

    async with ListingRepository(settings.database_url) as repo:
        await repo.init_schema()
        result = await repo.sync_many(listings)
    return DbSyncSummary(**result.stats.as_dict()), result.inserted_ids


async def _notify(
    listings: list[Listing],
    *,
    inserted_ids: list[int],
    persist_enabled: bool,
    prefs: dict | None,
    enabled: bool | None,
) -> int:
    """Send Telegram alerts for the newly inserted listings.

    Only deals that are genuinely new are notified when persistence ran. When
    persistence did not run (``persist_enabled`` is False) we cannot know what
    is new, so we notify everything — that matches a manual `--no-db` run.

    Non-car listings (parts, accessories) are dropped, and only listings with
    evaluation score at least ``min_score`` (from user prefs, else env) are
    notified. When the user enabled photos, ``sendPhoto`` is used. Returns the
    number of messages sent.
    """
    if not listings:
        return 0
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        if enabled is True:
            raise ConfigError("notification requested but TELEGRAM_BOT_TOKEN/CHAT_ID is not set")
        return 0

    from bazcar.bot.classify import filter_cars
    from bazcar.notify import TelegramNotifier

    part_markers = load_scraper_config().markers.part_markers or None
    min_score = (prefs or {}).get("min_score")
    if min_score is None:
        min_score = settings.telegram_min_score
    targets = filter_cars(
        _notify_targets(
            listings,
            inserted_ids=inserted_ids,
            persist_enabled=persist_enabled,
            min_score=min_score,
        ),
        part_markers=part_markers,
    )
    if not targets:
        return 0
    show_photo = bool((prefs or {}).get("show_photo"))
    async with TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id) as ntf:
        if show_photo:
            return await ntf.send_listings_with_photos(targets)
        return await ntf.send_listings(targets)


def _notify_targets(
    listings: list[Listing], *, inserted_ids: list[int], persist_enabled: bool, min_score: int | None
) -> list[Listing]:
    """Pure selection of which listings deserve a notification."""
    if persist_enabled:
        by_id = {listing.ad_id: listing for listing in listings}
        selected = [by_id[ad_id] for ad_id in inserted_ids if ad_id in by_id]
    else:
        selected = list(listings)
    if min_score is not None:
        selected = [
            listing
            for listing in selected
            if listing.evaluation is not None and listing.evaluation.score >= min_score
        ]
    return selected


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
