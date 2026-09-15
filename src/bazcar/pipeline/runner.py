"""Scrape runner: fetch -> parse -> validate -> export JSON."""

from __future__ import annotations

import asyncio
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
from bazcar.core.models import DbSyncSummary, DealEvaluation, Listing, ScrapeSummary
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

    # Persist first so evaluations (FK to listings) can be cached per profile.
    db_summary, inserted_ids = await _persist(listings, enabled=persist)

    evaluated = 0
    if evaluate is not False:
        evaluated = await _evaluate(listings, enabled=evaluate, criteria=(prefs or {}).get("criteria"))
        try:
            await _auto_backfill_missing(prefs)
            await _process_eval_tasks()
        except Exception:
            logger.warning("eval task processing failed", exc_info=True)

    # Include filters hash in filename to separate different filter combinations
    filters_tag = getattr(scraper, "filters_hash", "default")
    target = export_path or (settings.export_dir / _default_filename(filters_tag))
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_json(target, listings)

    notified = 0
    if notify is not False:
        if db_summary is not None:
            # DB runs: match-driven alerts (heals failed evals + search switches).
            try:
                notified = await _notify_unseen_matches(prefs, enabled=notify)
            except Exception:
                logger.warning("match sweep failed", exc_info=True)
        else:
            # Manual --no-db run: notify everything scraped (no dedupe possible).
            notified = await _notify(
                listings,
                inserted_ids=inserted_ids,
                persist_enabled=False,
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


def profile_key(criteria: str | None, model: str) -> str:
    """Stable key for a search profile (criteria + model) used to cache evals."""
    import hashlib

    return hashlib.md5(f"{criteria or ''}|{model}".encode()).hexdigest()[:12]


async def _evaluate_profile(
    listings: list[Listing],
    *,
    criteria: str | None,
    model: str,
    cfg,
    repo=None,
    delay: float = 0.0,
) -> tuple[int, int]:
    """Score ``listings`` under one search profile (criteria + model).

    Ads already scored for this exact profile (same criteria, same model) are
    skipped — everything is cached in the ``evaluations`` table.

    Returns ``(attached, fresh)`` — how many listings carry an evaluation and
    how many were scored in THIS call (0 fresh + pending work = all LLM calls
    failed, caller should retry later instead of marking the task done).
    """
    if not listings:
        return 0, 0
    settings = get_settings()
    p_key = profile_key(criteria, model)

    cached: dict[int, DealEvaluation] = {}
    if repo is not None:
        try:
            cached = await repo.fetch_evaluations(p_key, [listing.ad_id for listing in listings])
        except Exception:
            logger.warning("evaluation cache unavailable, evaluating fresh", exc_info=True)

    for listing in listings:
        ev = cached.get(listing.ad_id)
        if ev is not None:
            listing.evaluation = ev

    fresh_items: list[tuple[int, DealEvaluation]] = []
    flushed = 0
    to_eval = [listing for listing in listings if listing.evaluation is None]
    if to_eval:
        from bazcar.llm import LLMProvider

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
            for listing in to_eval:
                if delay:
                    await asyncio.sleep(delay)
                try:
                    listing.evaluation = await llm.evaluate(listing, criteria=criteria)
                    if listing.evaluation is not None:
                        listing.evaluation.model = model
                except LlmError as exc:
                    logger.warning("eval failed for ad %s: %s", listing.ad_id, exc)
                    listing.evaluation = None
                if listing.evaluation is not None:
                    fresh_items.append((listing.ad_id, listing.evaluation))
                # Flush incrementally so long backfills survive process timeouts —
                # progress is committed every EVAL_FLUSH_BATCH listings.
                if len(fresh_items) - flushed >= EVAL_FLUSH_BATCH and repo is not None:
                    try:
                        await repo.upsert_evaluations(p_key, fresh_items[flushed:])
                        flushed = len(fresh_items)
                    except Exception:
                        logger.warning("evaluation cache write failed", exc_info=True)
        if repo is not None and len(fresh_items) > flushed:
            try:
                await repo.upsert_evaluations(p_key, fresh_items[flushed:])
            except Exception:
                logger.warning("evaluation cache write failed", exc_info=True)

    attached = sum(1 for listing in listings if listing.evaluation is not None)
    return attached, len(fresh_items)


async def _process_eval_tasks() -> int:
    """Run queued backfill tasks (re-score stored ads under task profiles).

    After each task completes, a progress message is pushed to the Telegram
    chat so the user can see what happened (transparency of pipeline steps).
    """
    settings = get_settings()
    if not settings.database_url or not settings.openrouter_api_key:
        return 0
    from bazcar.db import ListingRepository

    cfg = load_llm_config()
    processed = 0
    repo = ListingRepository(settings.database_url)
    await repo.connect()
    try:
        await repo.init_schema()
        for task in await repo.pending_eval_tasks():
            rows = await repo.fetch_recent_listings(
                days=task.get("days") or 3, limit=task["max_listings"]
            )
            listings = [Listing(**row) for row in rows]
            attached, fresh = await _evaluate_profile(
                listings,
                criteria=task["criteria"],
                model=task["model"],
                cfg=cfg,
                repo=repo,
                delay=2.0,  # stay under free-tier rate limits during backfill
            )
            if fresh == 0 and len(listings) > attached:
                # every LLM call failed (e.g. model stuck behind 429) — bump the
                # counter; after MAX we abandon the task so it cannot block the
                # pipeline forever behind a dead model.
                attempts = (task.get("attempts") or 0) + 1
                await repo.bump_eval_task_attempt(task["id"])
                if attempts >= MAX_EVAL_TASK_RETRIES:
                    await repo.complete_eval_task(task["id"])
                    logger.warning(
                        "eval task %d abandoned after %d failed attempts (model %s)",
                        task["id"],
                        attempts,
                        task["model"],
                    )
                    await _report_eval_task_abandoned(settings, task)
                else:
                    logger.warning(
                        "eval task %d scored 0/%d (model %s) — attempt %d/%d, left pending",
                        task["id"],
                        len(listings) - attached,
                        task["model"],
                        attempts,
                        MAX_EVAL_TASK_RETRIES,
                    )
                continue
            await repo.complete_eval_task(task["id"])
            processed += 1
            logger.info(
                "eval task %d done (profile %s): %d ads scored",
                task["id"],
                task["profile_key"],
                fresh,
            )
            await _report_eval_task_done(settings, task, attached, listings)
    finally:
        await repo.close()
    return processed


async def _report_eval_task_done(settings, task: dict, count: int, listings: list[Listing]) -> None:
    """Push a completion message when a re-evaluation backfill finishes."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return
    try:
        from bazcar.notify import TelegramNotifier

        criteria_short = (task.get("criteria") or "bez kritérií").strip()
        label = criteria_short if len(criteria_short) <= 60 else criteria_short[:57] + "…"
        scored_now = sum(
            1
            for listing in listings
            if listing.evaluation is not None and listing.evaluation.model == task.get("model")
        )
        async with TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id) as ntf:
            await ntf.send_message(
                f"✅ <b>Prehodnotenie hotové</b>\n"
                f"Search: <i>{_tg_escape(label)}</i>\n"
                f"Skórovaných: <b>{scored_now}</b> áut (celkom v rozsahu {count})\n"
                f"➡️ <code>/show</code> ukáže výsledky podľa <code>/score</code>"
            )
    except Exception:
        logger.warning("eval task report failed", exc_info=True)


async def _report_eval_task_abandoned(settings, task: dict) -> None:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return
    try:
        from bazcar.notify import TelegramNotifier

        criteria_short = (task.get("criteria") or "bez kritérií").strip()
        label = criteria_short if len(criteria_short) <= 60 else criteria_short[:57] + "…"
        async with TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id) as ntf:
            await ntf.send_message(
                f"⚠️ <b>Prehodnotenie zrušené</b>\n"
                f"Search: <i>{_tg_escape(label)}</i>\n"
                f"Model <code>{_tg_escape(task.get('model') or '?')}</code> opakovane zlyháva "
                f"(napr. 429 rate-limit). Má dobiehajúci backend — skús /models a /model iný."
            )
    except Exception:
        logger.warning("eval task abandon report failed", exc_info=True)


def _tg_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _auto_backfill_missing(prefs: dict | None) -> None:
    """Self-healing: queue a backfill when ads lack a score for the active profile.

    Covers ads whose first-run LLM eval failed (flaky free tier) and ads
    inserted before the profile switch. Skips when a task for the profile is
    already pending. Tasks converge over a few runs (cached evals skip).
    """
    settings = get_settings()
    if not settings.database_url or not settings.openrouter_api_key or not prefs:
        return
    from bazcar.db import ListingRepository

    model = prefs.get("model") or load_llm_config().model
    p_key = profile_key(prefs.get("criteria"), model)
    repo = ListingRepository(settings.database_url)
    await repo.connect()
    try:
        missing, total = await repo.count_missing_evaluations(p_key, days=30, limit=500)
        if missing <= 0:
            return
        if any(t["profile_key"] == p_key for t in await repo.pending_eval_tasks()):
            return
        await repo.create_eval_task(p_key, prefs.get("criteria"), model, max_listings=min(total or 500, 500), days=30)
        logger.info("auto-backfill queued: %d/%d ads missing eval for %s", missing, total, p_key)
    finally:
        await repo.close()


async def _evaluate(listings: list[Listing], *, enabled: bool | None, criteria: str | None = None) -> int:
    """Score scraped listings under the active search profile.

    Evaluations are cached per profile (criteria + model): the same ad is
    scored once per profile, so switching searches re-evaluates but repeated
    runs do not. A failed LLM call is logged and skipped (never fatal).
    """
    if not listings:
        return 0
    settings = get_settings()
    if not settings.openrouter_api_key:
        if enabled is True:
            raise ConfigError("evaluation requested but OPENROUTER_API_KEY is not set")
        return 0

    from bazcar.llm import LLMProvider  # noqa: F401 — re-exported for tests

    cfg = load_llm_config()
    prefs = await _load_prefs()
    model = settings.openrouter_model or (prefs or {}).get("model") or cfg.model

    repo = None
    if settings.database_url:
        try:
            from bazcar.db import ListingRepository

            repo = ListingRepository(settings.database_url)
            await repo.connect()
            await repo.init_schema()
        except Exception:
            logger.warning("evaluation cache unavailable", exc_info=True)
            if repo is not None:
                await repo.close()
                repo = None

    try:
        attached, _ = await _evaluate_profile(
            listings,
            criteria=criteria or (prefs or {}).get("criteria"),
            model=model,
            cfg=cfg,
            repo=repo,
        )
        return attached
    finally:
        if repo is not None:
            await repo.close()


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


NOTIFY_WINDOW_DAYS = 7      # how far back deal alerts look
NOTIFY_CAP_PER_RUN = 10     # max deal alerts per run (rest stays for /show)
MAX_EVAL_TASK_RETRIES = 2   # abandon a backfill after this many empty attempts
EVAL_FLUSH_BATCH = 5        # commit evaluations incrementally during long backfills


async def _notify_unseen_matches(prefs: dict | None, *, enabled: bool | None) -> int:
    """Match-driven deal alerts (DB runs).

    Instead of notifying only on first DB insert, every run finds listings that
    match the active search profile (score >= min_score, whole cars) which have
    NOT yet been pushed to the chat under this profile — and pushes them. This
    heals every historical hole: ads whose first-run eval failed, ads inserted
    under a previous search, and backfilled evaluations.

    Each (chat, ad, profile) is sent at most once (notifications table). Cap
    per run keeps the chat readable — the rest is available via /show.
    """
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        if enabled is True:
            raise ConfigError("notification requested but TELEGRAM_BOT_TOKEN/CHAT_ID is not set")
        return 0
    if not settings.database_url:
        return 0

    from bazcar.bot.classify import filter_cars
    from bazcar.db import ListingRepository
    from bazcar.notify import TelegramNotifier

    min_score = (prefs or {}).get("min_score")
    if min_score is None:
        min_score = settings.telegram_min_score or 0
    model = (prefs or {}).get("model") or load_llm_config().model
    p_key = profile_key((prefs or {}).get("criteria"), model)
    chat_id = int(settings.telegram_chat_id)

    part_markers = load_scraper_config().markers.part_markers or None
    repo = ListingRepository(settings.database_url)
    await repo.connect()
    try:
        matches = await repo.fetch_recent_matches(p_key, min_score, NOTIFY_WINDOW_DAYS, limit=50)
        seen = await repo.fetch_notified_ids(chat_id, p_key, [m["ad_id"] for m in matches])
        targets = filter_cars(
            [_row_to_notification_listing(m) for m in matches if m["ad_id"] not in seen],
            part_markers=part_markers,
        )[:NOTIFY_CAP_PER_RUN]
        if not targets:
            return 0
        show_photo = bool((prefs or {}).get("show_photo"))
        async with TelegramNotifier(settings.telegram_bot_token, chat_id) as ntf:
            if show_photo:
                sent = await ntf.send_listings_with_photos(targets, chat_id=chat_id)
            else:
                sent = await ntf.send_listings(targets, chat_id=chat_id)
        if sent:
            await repo.record_notifications(chat_id, p_key, [t.ad_id for t in targets])
        return sent
    finally:
        await repo.close()


def _row_to_notification_listing(row: dict) -> Listing:
    """Rebuild a Listing from a fetch_recent_matches row (for deal alerts)."""
    from bazcar.bot.daemon import _row_to_listing

    return _row_to_listing(row)


async def _notify(
    listings: list[Listing],
    *,
    inserted_ids: list[int],
    persist_enabled: bool,
    prefs: dict | None,
    enabled: bool | None,
) -> int:
    """Legacy insert-driven alert path — only used for ``--no-db`` runs.

    Without a database we cannot dedupe, so every scraped listing that scores
    >= min_score and is a whole car is pushed (a manual one-off run).
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
