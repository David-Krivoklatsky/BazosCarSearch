"""Scrape runner: fetch -> parse -> validate -> export JSON."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from bazcar.config.settings import (
    ScraperConfig,
    SearchFiltersConfig,
    get_settings,
    load_llm_config,
    load_scraper_config,
)
from bazcar.core.exceptions import ConfigError, LlmError
from bazcar.core.models import DbSyncSummary, DealEvaluation, Listing, ScrapeSummary
from bazcar.core.profiles import EVAL_SCHEMA_VERSION, profile_key  # noqa: F401  (re-export)
from bazcar.llm.costgate import model_price
from bazcar.scrapers.factory import get_scraper

if TYPE_CHECKING:
    from bazcar.db.repository import SyncResult

logger = logging.getLogger(__name__)


def _apply_filters(cfg: ScraperConfig, filters: dict | None) -> ScraperConfig:
    """Override the YAML scrape filters with a user's bot-configured ones.

    ``None`` = no user preference -> keep the YAML defaults (legacy behavior).
    A dict — INCLUDING an empty one — overrides the YAML filters completely,
    so ``/filter clear`` genuinely means "no filters" (whole catalogue).
    """
    if filters is None:
        return cfg
    cfg = cfg.model_copy(deep=True)
    cfg.base.search_filters = SearchFiltersConfig.model_validate(filters)
    return cfg


def _distinct_scopes(users: list[dict]) -> list[dict | None]:
    """Distinct filter sets to scrape for the given users (union semantics).

    Each entry is either ``None`` (YAML default scope) or a filters dict.
    Scraping every distinct set once and merging by ``ad_id`` preserves each
    user's scrape-level filters without duplicate ads.
    """
    if not users:
        return [None]
    scopes: list[dict | None] = []
    seen: set[str] = set()
    for user in users:
        filters = user.get("filters")  # None = never configured -> YAML scope
        key = "default" if filters is None else json.dumps(filters, sort_keys=True)
        if key not in seen:
            seen.add(key)
            scopes.append(filters)
    return scopes or [None]


def _scope_detail(
    scope_filters: dict | None, scraper, cards: int, new: int
) -> dict:
    """One scope's scrape telemetry for the run record (testable, no I/O)."""
    return {
        "filters": scope_filters,  # None = YAML default scope
        "pages": list(getattr(scraper, "last_page_stats", []) or []),
        "skipped": getattr(scraper, "last_parse_skipped", 0) or 0,
        "cards": cards,
        "new": new,
    }


async def _record_failed_run(settings, pages: list[str], scope_details: list[dict], exc: BaseException) -> None:
    """Best-effort telemetry for a run that died mid-scrape (never raises)."""
    if not settings.database_url:
        return
    try:
        from bazcar.db import ListingRepository

        async with ListingRepository(settings.database_url) as repo:
            await repo.record_scrape_run(
                pages=len(pages),
                total=0,
                inserted=0,
                price_changed=0,
                price_drops=0,
                evaluated=0,
                notified=0,
                error=f"{type(exc).__name__}: {exc}"[:500],
                detail={"scopes": scope_details},
            )
    except Exception:
        logger.warning("failed-run telemetry failed", exc_info=True)


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
    """Scrape, persist, evaluate per user profile and notify matches.

    Multi-user: every registered bot chat (plus the legacy env-configured
    notify chat) gets the scraped listings evaluated under its own search
    profile (criteria + model, cached) and its own match sweep. Scraping runs
    once per distinct user filter-set and merges by ad id.
    """
    settings = get_settings()
    cfg = config or load_scraper_config()

    await _migrate_profile_keys(settings)
    users = await _load_all_prefs()
    scopes = _distinct_scopes(users)

    listings: list[Listing] = []
    seen_ids: set[int] = set()
    pages: list[str] = []
    filters_tag = "default"
    scope_details: list[dict] = []
    try:
        for scope_filters in scopes:
            scope_cfg = _apply_filters(cfg, scope_filters)
            async with get_scraper(platform, scope_cfg) as scraper:
                found = await scraper.scrape_category(max_pages=max_pages)
                pages.extend(scraper._page_url(i) for i in range(max_pages))
                if filters_tag == "default":
                    filters_tag = getattr(scraper, "filters_hash", "default")
                new_in_scope = 0
                for listing in found:
                    if listing.ad_id not in seen_ids:
                        seen_ids.add(listing.ad_id)
                        listings.append(listing)
                        new_in_scope += 1
                scope_details.append(
                    _scope_detail(scope_filters, scraper, len(found), new_in_scope)
                )
    except Exception as exc:
        # A dead scrape must still leave a trace (which scope died, on what).
        await _record_failed_run(settings, pages, scope_details, exc)
        raise
    if detail:
        async with get_scraper(platform, cfg) as detail_scraper:
            for i, listing in enumerate(listings):
                if detail_limit and i >= detail_limit:
                    break
                await detail_scraper.scrape_detail(listing)

    if limit is not None:
        listings = listings[:limit]

    # Persist first so evaluations (FK to listings) can be cached per profile.
    db_summary, inserted_ids, sync_result = await _persist(listings, enabled=persist)

    # Price-drop alerts: users who saved an ad get pinged when it gets cheaper.
    if db_summary is not None and sync_result.price_drop_events:
        try:
            await _notify_price_drops(listings, sync_result.price_drop_events)
        except Exception:
            logger.warning("price drop alerts failed", exc_info=True)

    removal_stats: dict = {}
    if db_summary is not None:
        try:
            removal_stats = await _sweep_removed()
        except Exception:
            logger.warning("removal sweep failed", exc_info=True)

    evaluated = 0
    if evaluate is not False:
        evaluated = await _evaluate(listings, enabled=evaluate)
        try:
            await _auto_backfill_missing(users)
            await _process_eval_tasks()
        except Exception:
            logger.warning("eval task processing failed", exc_info=True)

    target = export_path or (settings.export_dir / _default_filename(filters_tag))
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_json(target, listings)

    notified = 0
    if notify is not False:
        if db_summary is not None:
            # DB runs: per-user match-driven alerts.
            try:
                notified = await _notify_unseen_matches(users, enabled=notify)
            except Exception:
                logger.warning("match sweep failed", exc_info=True)
        else:
            # Manual --no-db run: notify everything scraped (no dedupe possible).
            notified = await _notify(
                listings,
                inserted_ids=inserted_ids,
                persist_enabled=False,
                prefs=users[0] if users else None,
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
    if db_summary is not None:
        try:
            from bazcar.db import ListingRepository

            async with ListingRepository(settings.database_url) as repo:
                await repo.record_scrape_run(
                    pages=len(pages),
                    total=db_summary.total,
                    inserted=db_summary.inserted,
                    price_changed=db_summary.price_changed,
                    price_drops=db_summary.price_drops,
                    evaluated=evaluated,
                    notified=notified,
                    detail={"scopes": scope_details, "removal": removal_stats},
                )
        except Exception:
            logger.warning("scrape run telemetry failed", exc_info=True)
    logger.info(
        "scrape done: %d listings exported to %s",
        summary.exported,
        summary.export_path,
    )
    return summary


def _legacy_prefs(settings) -> dict:
    """Synthesized preferences for the env-configured notify chat (no bot row)."""
    return {
        "chat_id": int(settings.telegram_chat_id),
        "criteria": None,
        "model": None,
        "min_score": settings.telegram_min_score,
        "show_photo": False,
        "filters": None,  # None = YAML default scrape scope (legacy behavior)
        "notify_enabled": True,
        "searches": [],
    }


async def _load_all_prefs() -> list[dict]:
    """Preferences of every registered chat + the legacy notify chat.

    Returns a list of plain dicts (chat_id/criteria/model/min_score/
    show_photo/filters). Never raises — on failure falls back to the legacy
    single-chat configuration.
    """
    settings = get_settings()
    if not settings.database_url:
        return [_legacy_prefs(settings)] if settings.telegram_chat_id else []
    users: list[dict] = []
    try:
        from bazcar.bot.store import UserStore

        async with UserStore(settings.database_url) as store:
            prefs_rows = await store.list_all_prefs()
            users = []
            for p in prefs_rows:
                try:
                    searches = [
                        {
                            "name": s.name,
                            "criteria": s.criteria,
                            "min_score": s.min_score,
                        }
                        for s in await store.list_searches(p.chat_id)
                        if s.notify_enabled
                    ]
                except Exception:
                    searches = []
                users.append(
                    {
                        "chat_id": p.chat_id,
                        "criteria": p.criteria,
                        "model": p.model,
                        "min_score": p.min_score,
                        "show_photo": p.show_photo,
                        "filters": p.filters,
                        "notify_enabled": p.notify_enabled,
                        "prompt_name": p.prompt_name,
                        "fallback_models": p.fallback_models,
                        "searches": searches,
                    }
                )
    except Exception:
        logger.warning("prefs load failed, falling back to legacy chat", exc_info=True)
        users = []
    if settings.telegram_chat_id:
        chat_id = int(settings.telegram_chat_id)
        if not any(u["chat_id"] == chat_id for u in users):
            users.append(_legacy_prefs(settings))
    return users


async def _evaluate_profile(
    listings: list[Listing],
    *,
    criteria: str | None,
    model: str,
    cfg,
    repo=None,
    delay: float = 0.0,
    prompt_name: str = "standard",
    fallback_models: list[str] | None = None,
    force: bool = False,
) -> tuple[int, int]:
    """Score ``listings`` under one search profile (criteria + model + prompt).

    Ads already scored for this exact profile are skipped — everything is
    cached in the ``evaluations`` table. When the primary model fails outright,
    each fallback model is tried in order on the still-unscored remainder.

    Returns ``(attached, fresh)`` — how many listings carry an evaluation and
    how many were scored in THIS call (0 fresh + pending work = all LLM calls
    failed, caller should retry later instead of marking the task done).
    """
    if not listings:
        return 0, 0
    settings = get_settings()
    p_key = profile_key(criteria, prompt=prompt_name)

    cached: dict[int, DealEvaluation] = {}
    if repo is not None and not force:
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

        chain = [model, *[m for m in (fallback_models or []) if m and m != model]]
        api_key = getattr(settings, "llm_api_key", None) or settings.openrouter_api_key
        for attempt_model in chain:
            remaining = [listing for listing in to_eval if listing.evaluation is None]
            if not remaining:
                break
            if attempt_model != model:
                logger.warning(
                    "primary model %s failed — falling back to %s for %d ads",
                    model,
                    attempt_model,
                    len(remaining),
                )
            async with LLMProvider(
                api_key,
                base_url=cfg.base_url,
                model=attempt_model,
                temperature=cfg.temperature,
                max_tokens=cfg.max_tokens,
                top_p=cfg.top_p,
                frequency_penalty=cfg.frequency_penalty,
                presence_penalty=cfg.presence_penalty,
                response_format=cfg.response_format,
                stream=cfg.stream,
            ) as llm:
                for listing in remaining:
                    if delay:
                        await asyncio.sleep(delay)
                    try:
                        listing.evaluation = await llm.evaluate(
                            listing, criteria=criteria, prompt_name=prompt_name
                        )
                        if listing.evaluation is not None:
                            listing.evaluation.model = attempt_model
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
            if all(listing.evaluation is not None for listing in to_eval):
                break
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
    if not settings.database_url or not getattr(settings, "llm_api_key", None):
        return 0
    from bazcar.db import ListingRepository

    cfg = load_llm_config()
    processed = 0
    started = 0
    repo = ListingRepository(settings.database_url)
    await repo.connect()
    try:
        await repo.init_schema()
        prices = await _model_prices(settings, repo)
        budget = _budget_cap(settings)
        spent = await _monthly_spend(repo) if budget is not None else 0.0
        for task in await repo.pending_eval_tasks():
            if not task.get("confirmed", True):
                continue  # parked until the user confirms the cost in Telegram
            prompt_name = task.get("prompt_name") or "standard"
            fallbacks = [m for m in (task.get("fallback_models") or []) if m][:3]
            price = model_price(prices, task["model"])
            paid = price is None or price[0] + price[1] > 0
            if paid and budget is not None and spent >= budget:
                logger.warning(
                    "eval task %d held: monthly AI budget reached ($%.2f >= $%.2f)",
                    task["id"], spent, budget,
                )
                continue
            if started >= EVAL_TASKS_PER_RUN:
                logger.warning(
                    "eval task %d deferred: per-run cap of %d scoring tasks reached",
                    task["id"],
                    EVAL_TASKS_PER_RUN,
                )
                break
            started += 1
            task_ids = [int(a) for a in (task.get("ad_ids") or []) if int(a) > 0]
            if task_ids:
                rows = await repo.fetch_listings_by_ids(task_ids)
            else:
                rows = await repo.fetch_recent_listings(
                    days=task.get("days") or 3, limit=task["max_listings"]
                )
            # DB rows carry extra columns (removed_at ...); Listing forbids them.
            fields = set(Listing.model_fields)
            listings = [Listing(**{k: v for k, v in row.items() if k in fields}) for row in rows]
            await repo.start_eval_task(task["id"], total=len(listings))
            attached, fresh = await _evaluate_profile(
                listings,
                criteria=task["criteria"],
                model=task["model"],
                cfg=cfg,
                repo=repo,
                delay=2.0,  # stay under free-tier rate limits during backfill
                prompt_name=prompt_name,
                fallback_models=fallbacks,
                force=bool(task.get("force")),
            )
            await repo.set_eval_task_progress(task["id"], attached)
            if budget is not None:
                # Re-read spend so later tasks in the same run honour the cap
                # (one run may drain several paid backfills).
                spent = await _monthly_spend(repo)
            if fresh == 0 and len(listings) > attached:
                # every LLM call failed (e.g. model stuck behind 429) — bump the
                # counter; after MAX we abandon the task so it cannot block the
                # pipeline forever behind a dead model.
                attempts = (task.get("attempts") or 0) + 1
                await repo.bump_eval_task_attempt(task["id"])
                if attempts >= MAX_EVAL_TASK_RETRIES:
                    await repo.fail_eval_task(
                        task["id"],
                        f"model {task['model']} failed {attempts}x (429/limit)",
                    )
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

        criteria_short = (task.get("criteria") or "no criteria").strip()
        label = criteria_short if len(criteria_short) <= 60 else criteria_short[:57] + "…"
        scored_now = sum(
            1
            for listing in listings
            if listing.evaluation is not None and listing.evaluation.model == task.get("model")
        )
        async with TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id) as ntf:
            await ntf.send_message(
                f"✅ <b>Re-score finished</b>\n"
                f"Search: <i>{_tg_escape(label)}</i>\n"
                f"Scored: <b>{scored_now}</b> cars ({count} total in range)\n"
                f"➡️ <code>/show</code> shows the results"
            )
    except Exception:
        logger.warning("eval task report failed", exc_info=True)


async def _report_eval_task_abandoned(settings, task: dict) -> None:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return
    try:
        from bazcar.notify import TelegramNotifier

        criteria_short = (task.get("criteria") or "no criteria").strip()
        label = criteria_short if len(criteria_short) <= 60 else criteria_short[:57] + "…"
        async with TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id) as ntf:
            await ntf.send_message(
                f"⚠️ <b>Re-score cancelled</b>\n"
                f"Search: <i>{_tg_escape(label)}</i>\n"
                f"Model <code>{_tg_escape(task.get('model') or '?')}</code> keeps failing "
                f"(e.g. 429 rate-limit). Its backend is catching up — try /models and another /model."
            )
    except Exception:
        logger.warning("eval task abandon report failed", exc_info=True)


def _tg_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _auto_backfill_missing(users: list[dict] | None) -> None:
    """Self-healing: queue backfills for specs whose ads lack any score.

    A spec is judged model-independently: an ad already scored under ANY model
    counts as rated, so switching models never floods the queue. Paid backfills
    go through the cost gate (parked until the user confirms); a spec the user
    cancelled is not asked again for a week.
    """
    settings = get_settings()
    if not settings.database_url or not getattr(settings, "llm_api_key", None) or not users:
        return
    from bazcar.db import ListingRepository
    from bazcar.llm.costgate import enqueue_evaluation

    repo = ListingRepository(settings.database_url)
    await repo.connect()
    try:
        prices = await _model_prices(settings, repo)
        notifier = _make_notifier(settings)
        pending = await repo.pending_eval_tasks()
        pending_keys = {t["profile_key"] for t in pending}
        seen_keys: set[str] = set()
        for user in users:
            model = user.get("model") or load_llm_config().model
            prompt = user.get("prompt_name") or "standard"
            p_key = profile_key(user.get("criteria"), prompt=prompt)
            if p_key in seen_keys or p_key in pending_keys:
                continue
            seen_keys.add(p_key)
            if await repo.was_recently_cancelled(p_key, days=7):
                continue
            missing, total = await repo.count_missing_evaluations(p_key, days=30, limit=500)
            if missing <= 0:
                continue
            result = await enqueue_evaluation(
                repo,
                notifier=notifier,
                chat_id=user.get("chat_id"),
                profile_key=p_key,
                criteria=user.get("criteria"),
                model=model,
                prompt_name=prompt,
                fallback_models=user.get("fallback_models"),
                max_listings=min(total or 500, 500),
                days=30,
                ad_ids=None,
                force=False,
                cars=missing,
                prices=prices,
                scope_label="auto backfill, last 30 days",
            )
            logger.info(
                "auto-backfill %s: %d/%d ads missing eval for %s",
                result["status"], missing, total, p_key,
            )
    finally:
        await repo.close()


async def _migrate_profile_keys(settings) -> dict:
    """One-time (idempotent) v3 migration: model-bound score keys -> spec keys."""
    if not settings.database_url:
        return {}
    from bazcar.core.profiles import legacy_key_mapping
    from bazcar.db import ListingRepository

    repo = ListingRepository(settings.database_url)
    await repo.connect()
    try:
        await repo.init_schema()
        criteria, models = await repo.legacy_key_inputs()
        models = sorted(set(models) | {load_llm_config().model})
        mapping = legacy_key_mapping(criteria, models)
        if not mapping:
            return {}
        counts = await repo.migrate_profile_keys(mapping)
        logger.info("profile key migration: %s", counts)
        return counts
    except Exception:
        logger.warning("profile key migration failed", exc_info=True)
        return {}
    finally:
        await repo.close()


REMOVAL_CHECKS_PER_RUN = 15   # ad pages verified per pipeline run (paced, ~20 s)
REMOVAL_CHECK_DELAY_S = 1.5


async def _sweep_removed(limit: int = REMOVAL_CHECKS_PER_RUN) -> dict:
    """Verify stale listings against Bazoš and soft-remove the sold/unlisted ones.

    A listing not seen for a day is fetched directly: if Bazoš no longer serves
    the ad (redirect to a category page) it is marked ``removed_at`` — hidden
    from the catalog, kept for saved lists and history. Live ads get
    ``verified_at``. Unreachable pages are retried in a later run, never removed.
    """
    stats = {"checked": 0, "removed": 0, "alive": 0, "unreachable": 0}
    settings = get_settings()
    if not settings.database_url:
        return stats
    import httpx

    from bazcar.core.adpage import AdState, fetch_ad_page
    from bazcar.db import ListingRepository

    repo = ListingRepository(settings.database_url)
    await repo.connect()
    try:
        candidates = await repo.removal_candidates(limit)
        if not candidates:
            return stats
        async with httpx.AsyncClient(
            timeout=20.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}
        ) as client:
            for row in candidates:
                state = await fetch_ad_page(row["ad_id"], client=client)
                stats["checked"] += 1
                blocked = state is not None and state.get("state") == AdState.BLOCKED.value
                if state is None or blocked:
                    stats["unreachable"] += 1
                elif state.get("gone"):
                    await repo.mark_listing_removed(row["ad_id"])
                    stats["removed"] += 1
                    logger.info("removed (sold/unlisted): ad %s %s", row["ad_id"], row.get("title"))
                else:
                    await repo.mark_listing_verified(row["ad_id"])
                    stats["alive"] += 1
                await asyncio.sleep(REMOVAL_CHECK_DELAY_S)
    finally:
        await repo.close()
    return stats


async def _model_prices(settings, repo) -> list[dict]:
    """Cached OpenRouter model list (with numeric prices); [] when unavailable."""
    from bazcar.llm.provider import list_models_cached

    try:
        models, _ = await list_models_cached(getattr(settings, "llm_api_key", None), repo)
        return models
    except Exception:
        logger.warning("model price lookup failed", exc_info=True)
        return []


def _make_notifier(settings):
    """Telegram notifier for confirmation prompts; None when not configured."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return None
    from bazcar.notify import TelegramNotifier

    return TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)


async def _notify_price_drops(
    listings: list[Listing], drops: list[tuple[int, Decimal, Decimal]]
) -> int:
    """Alert users who saved an ad whose price dropped in this scrape run."""
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.database_url or not drops:
        return 0
    from bazcar.db import ListingRepository
    from bazcar.notify import TelegramNotifier
    from bazcar.notify.format import _em, _em_url, clean_title, listing_keyboard

    by_id = {listing.ad_id: listing for listing in listings}
    repo = ListingRepository(settings.database_url)
    await repo.connect()
    try:
        chats_by_ad = await repo.fetch_chats_saving([d[0] for d in drops])
    finally:
        await repo.close()
    if not chats_by_ad:
        return 0
    from bazcar.bot.store import UserStore

    thresholds: dict[int, dict[int, int | None]] = {}
    try:
        store = UserStore(settings.database_url)
        await store.connect()
        try:
            for ad_id in chats_by_ad:
                try:
                    thresholds[ad_id] = await store.alert_thresholds(chats_by_ad[ad_id], ad_id)
                except Exception:
                    logger.warning("alert thresholds unavailable for ad %s", ad_id, exc_info=True)
                    thresholds[ad_id] = {}
        finally:
            await store.close()
    except Exception:
        # Thresholds are best-effort: without them every saver is alerted.
        logger.warning("alert thresholds lookup failed, alerting all savers", exc_info=True)
    sent = 0
    async with TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id or "") as ntf:
        for ad_id, old_price, new_price in drops:
            targets = chats_by_ad.get(ad_id)
            listing = by_id.get(ad_id)
            if not targets or listing is None:
                continue
            limits = thresholds.get(ad_id, {})
            targets = [
                chat_id
                for chat_id in targets
                if limits.get(chat_id) is None
                or float(new_price) <= float(limits[chat_id])
            ]
            if not targets:
                continue
            pct = round((1 - float(new_price) / float(old_price)) * 100) if old_price else 0
            old_fmt = f"{old_price:,.0f}".replace(",", " ")
            new_fmt = f"{new_price:,.0f}".replace(",", " ")
            text = (
                f"📉 <b>Price drop {pct}%!</b>\n"
                f"<b>{_em(clean_title(listing.title))}</b>\n"
                f"Price: <s>{old_fmt} €</s> → <b>{new_fmt} €</b>\n"
                f"🆔 {ad_id}\n"
                f"<a href=\"{_em_url(listing.url)}\">🔗 Open listing</a>"
            )
            for chat_id in targets:
                if await ntf.send_message(
                    text, chat_id=chat_id, reply_markup=listing_keyboard(listing)
                ):
                    sent += 1
    return sent


async def _evaluate(
    listings: list[Listing], *, enabled: bool | None, users: list[dict] | None = None
) -> int:
    """Score scraped listings under every user's search profile.

    Evaluations are cached per profile (criteria + model + schema): the same
    ad is scored once per profile, so repeated runs and users sharing the
    same profile cost nothing. A failed LLM call is logged and skipped
    (never fatal). Returns the number of FRESH evaluations made this run.
    """
    if not listings:
        return 0
    settings = get_settings()
    if not getattr(settings, "llm_api_key", None):
        if enabled is True:
            raise ConfigError("evaluation requested but no LLM key is set")
        return 0

    from bazcar.llm import LLMProvider  # noqa: F401 — re-exported for tests

    cfg = load_llm_config()
    if users is None:
        users = await _load_all_prefs()

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

    budget = _budget_cap(settings)
    if repo is not None and budget is not None:
        spent = await _monthly_spend(repo)
        if spent >= budget:
            logger.warning(
                "monthly AI budget exhausted ($%.4f >= $%.4f) — serving cached scores only",
                spent,
                budget,
            )
            await repo.close()
            return 0

    total_fresh = 0
    try:
        for user in users:
            model = settings.openrouter_model or user.get("model") or cfg.model
            _, fresh = await _evaluate_profile(
                listings,
                criteria=user.get("criteria"),
                model=model,
                cfg=cfg,
                repo=repo,
                prompt_name=user.get("prompt_name") or "standard",
                fallback_models=user.get("fallback_models"),
            )
            total_fresh += fresh
        return total_fresh
    finally:
        if repo is not None:
            await repo.close()


async def _monthly_spend(repo) -> float:
    """Trailing-30-day evaluation spend in USD (0.0 when unknown)."""
    try:
        cost = await repo.eval_cost(None, 30)
        return float(cost.get("total", {}).get("cost_usd") or 0)
    except Exception:
        logger.warning("monthly spend lookup failed", exc_info=True)
        return 0.0


def _budget_cap(settings) -> float | None:
    """Effective monthly AI cap: a positive value enables the guard, else off.

    ``None`` (unset) and ``0``/negative both mean "no cap" — the config value is
    a ceiling, never a way to freeze all paid scoring.
    """
    try:
        value = float(getattr(settings, "monthly_budget_usd", None))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


async def _persist(
    listings: list[Listing], *, enabled: bool | None
) -> tuple[DbSyncSummary | None, list[int], SyncResult | None]:
    """Write listings to Postgres unless explicitly disabled or unconfigured.

    Returns ``(summary, inserted_ids, sync_result)`` — ``sync_result`` carries
    the price-drop events used for drop alerts (None when persistence is off).
    """
    if enabled is False:
        return None, [], None
    settings = get_settings()
    if not settings.database_url:
        if enabled is True:
            raise ConfigError("persistence requested but BAZCAR_DATABASE_URL is not set")
        return None, [], None

    from bazcar.db import ListingRepository

    async with ListingRepository(settings.database_url) as repo:
        await repo.init_schema()
        result: SyncResult = await repo.sync_many(listings)
    return DbSyncSummary(**result.stats.as_dict()), result.inserted_ids, result


NOTIFY_WINDOW_DAYS = 7      # how far back deal alerts look
NOTIFY_CAP_PER_RUN = 10     # max deal alerts per run (rest stays for /show)
MAX_EVAL_TASK_RETRIES = 2   # abandon a backfill after this many empty attempts
EVAL_FLUSH_BATCH = 5        # commit evaluations incrementally during long backfills
EVAL_TASKS_PER_RUN = 20     # max backfill tasks scored per run (bounds one run)


def _sweep_profiles(user: dict, settings, default_model: str) -> list[tuple[str, int]]:
    """(profile_key, min_score) pairs to sweep: active profile + enabled searches.

    Duplicates collapse so shared criteria cost one sweep; the active profile
    always runs first.
    """
    prompt = user.get("prompt_name") or "standard"
    profiles: list[tuple[str, int]] = []
    seen: set[str] = set()

    def _add(criteria: str | None, min_score: int | None) -> None:
        score = min_score if min_score is not None else (settings.telegram_min_score or 0)
        key = profile_key(criteria, prompt=prompt)
        if key not in seen:
            seen.add(key)
            profiles.append((key, score))

    _add(user.get("criteria"), user.get("min_score"))
    for search in user.get("searches") or []:
        _add(search.get("criteria"), search.get("min_score"))
    return profiles


async def _notify_unseen_matches(users: list[dict] | None, *, enabled: bool | None) -> int:
    """Match-driven deal alerts (DB runs), one sweep per registered user.

    Every run finds listings that match each user's search profiles (score >=
    their min_score, whole cars) which have NOT yet been pushed to their chat
    under that profile — and pushes them. Each (chat, ad, profile) is sent at
    most once (notifications table). Cap per run keeps chats readable.
    Users (or searches) with notifications disabled are skipped entirely.
    """
    settings = get_settings()
    if not settings.telegram_bot_token:
        if enabled is True:
            raise ConfigError("notification requested but TELEGRAM_BOT_TOKEN is not set")
        return 0
    if not settings.database_url or not users:
        return 0

    from bazcar.bot.classify import filter_cars
    from bazcar.db import ListingRepository
    from bazcar.notify import TelegramNotifier

    part_markers = load_scraper_config().markers.part_markers or None
    total_sent = 0
    repo = ListingRepository(settings.database_url)
    await repo.connect()
    try:
        async with TelegramNotifier(
            settings.telegram_bot_token, settings.telegram_chat_id or ""
        ) as ntf:
            for user in users:
                chat_id = user.get("chat_id")
                if chat_id is None:
                    continue
                if user.get("notify_enabled") is False:
                    continue
                model = user.get("model") or load_llm_config().model
                for p_key, min_score in _sweep_profiles(user, settings, model):
                    if total_sent >= NOTIFY_CAP_PER_RUN:
                        break
                    matches = await repo.fetch_recent_matches(
                        p_key, min_score, NOTIFY_WINDOW_DAYS, limit=50
                    )
                    seen = await repo.fetch_notified_ids(
                        chat_id, p_key, [m["ad_id"] for m in matches]
                    )
                    targets = filter_cars(
                        [_row_to_notification_listing(m) for m in matches if m["ad_id"] not in seen],
                        part_markers=part_markers,
                    )[: NOTIFY_CAP_PER_RUN - total_sent]
                    if not targets:
                        continue
                    if user.get("show_photo"):
                        sent_ids = await ntf.send_listings_with_photos(targets, chat_id=chat_id)
                    else:
                        sent_ids = await ntf.send_listings(targets, chat_id=chat_id)
                    if sent_ids:
                        # Record only what actually reached the chat: a failed
                        # send must stay "unseen" so the next run retries it.
                        await repo.record_notifications(chat_id, p_key, sent_ids)
                    total_sent += len(sent_ids)
        return total_sent
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
            return len(await ntf.send_listings_with_photos(targets))
        return len(await ntf.send_listings(targets))


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
