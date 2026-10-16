"""Telegram Mini App JSON API (Vercel serverless).

Serves the dashboard SPA (api/dashboard.py) with live data from Neon.

Auth: every request must carry the Telegram WebApp ``initData`` — either as
the ``initData`` query parameter, the ``X-Telegram-Init-Data`` header, or (for
POST actions) the ``initData`` field of the JSON body. The HMAC-SHA256
signature is verified against the bot token (Telegram's WebAppData scheme)
and the embedded user id becomes the caller's chat_id, so each user only
ever sees their own profile's data. Anything else gets a 401.

GET views (``?view=...``): ``overview`` | ``inventory`` | ``listing``
(needs ``&id=<ad_id>``) | ``compare`` (``&ids=1,2,3``) | ``queue`` |
``stats`` | ``saved`` | ``profiles``.

Inventory accepts ``q`` (text), ``sort`` (newest/price_asc/price_desc/score),
``status`` (all/unevaluated/rated), ``min_price``, ``max_price``,
``min_year``, ``max_year``, ``page`` and ``days``.

POST actions (JSON body ``{"action": ...}``): ``save`` | ``unsave`` |
``note`` | ``alert`` (``id`` + ``below`` in EUR, null clears) | ``use``
(activate a saved search) | ``criteria`` (edit active search text) |
``toggle_search`` | ``notify`` | ``score`` | ``photos``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import sys
import time
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

logger = logging.getLogger("bazcar.webapp")
logging.basicConfig(level=logging.INFO)

MAX_INIT_DATA_AGE_S = 86_400  # 24 h

_SORTS = {"newest", "price_asc", "price_desc", "score"}
_STATUSES = {"all", "unevaluated", "rated"}


def validate_init_data(init_data: str, bot_token: str, *, now: float | None = None) -> dict | None:
    """Verify Telegram WebApp initData; return the user dict or None.

    Implements the official scheme: secret = HMAC_SHA256("WebAppData",
    bot_token), then HMAC_SHA256(secret, data-check-string) must equal the
    provided ``hash``. ``auth_date`` older than 24 h is rejected.
    """
    if not init_data or not bot_token:
        return None
    pairs = parse_qsl(init_data, keep_blank_values=True)
    data = dict(pairs)
    received_hash = data.pop("hash", None)
    if not received_hash:
        return None
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        return None
    try:
        auth_date = int(data.get("auth_date", "0"))
    except (TypeError, ValueError):
        return None
    current = time.time() if now is None else now
    if current - auth_date > MAX_INIT_DATA_AGE_S or auth_date > current + 300:
        return None
    user_raw = data.get("user")
    if not user_raw:
        return None
    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        return None
    return user


def _json_default(value):
    from datetime import date, datetime
    from decimal import Decimal

    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def _to_int(raw: str | None, *, lo: int, hi: int) -> int | None:
    try:
        return max(lo, min(int((raw or "").strip()), hi))
    except (ValueError, AttributeError):
        return None


async def _listing_payload(repo, ad_id: int, p_key: str) -> dict:
    row = await repo.fetch_listing_by_id(ad_id, p_key)
    if row is None:
        return {"error": "not found"}
    history = await repo.fetch_price_history(ad_id)
    from bazcar.bot.daemon import extract_make

    make = extract_make(row.get("title") or "")
    bench = None
    if make:
        try:
            bench = await repo.price_benchmark(
                make, row.get("year"), mileage_km=row.get("mileage_km")
            )
        except TypeError:
            bench = await repo.price_benchmark(make, row.get("year"))
    return {"listing": row, "history": history, "benchmark": bench}


async def _load_view(
    view: str, chat_id: int, page: int, days: int, ad_id: int | None = None,
    extra: dict | None = None,
) -> tuple[dict, HTTPStatus]:
    """Load one dashboard view from Neon (repository methods shared with bot)."""
    from bazcar.bot.store import UserStore
    from bazcar.config.settings import get_settings, load_llm_config
    from bazcar.core.profiles import profile_key
    from bazcar.db import ListingRepository

    # The SPA's Profile tab requests view=profile; the backend's canonical
    # name is "profiles" (kept for backward compatibility).
    if view == "profile":
        view = "profiles"
    extra = extra or {}
    settings = get_settings()
    if not settings.database_url:
        return {"error": "database not configured"}, HTTPStatus.OK

    repo = ListingRepository(settings.database_url)
    await repo.connect()
    # Share the repo's live connection with the store so each request uses a
    # single DB connection (fakes in tests have no _conn -> falls back to two).
    conn = getattr(repo, "_conn", None)
    try:
        criteria, model = None, load_llm_config().model
        prompt_name, fallback_models = "standard", []
        saved_ids: set[int] = set()
        try:
            async with UserStore(settings.database_url, conn=conn) as store:
                prefs = await store.get_prefs(chat_id)
                if prefs is not None:
                    criteria = prefs.criteria
                    model = prefs.model or model
                    prompt_name = getattr(prefs, "prompt_name", None) or "standard"
                    fallback_models = [
                        m for m in (getattr(prefs, "fallback_models", None) or []) if m
                    ][:3]
                if view in {"inventory", "listing", "compare"}:
                    try:
                        saved_ids = await store.list_saved_ids(chat_id)
                    except Exception:
                        logger.warning("saved-ids load failed in webapp", exc_info=True)
        except Exception:
            logger.warning("prefs load failed in webapp", exc_info=True)
        p_key = profile_key(criteria, model, prompt=prompt_name)

        if view == "overview":
            stats = await repo.stats_overview(p_key)
            run = await repo.latest_scrape_run()
            pending = await repo.pending_eval_tasks()
            return {
                "profile_key": p_key,
                "stats": stats,
                "last_run": run,
                "queue_pending": len(pending),
            }, HTTPStatus.OK
        if view == "inventory":
            try:
                limit = max(1, min(int((extra.get("per") if isinstance(extra, dict) else None) or 15), 200))
            except (TypeError, ValueError):
                limit = 15
            offset = max(0, page) * limit
            sort = extra.get("sort") if extra.get("sort") in _SORTS else "newest"
            status = extra.get("status") if extra.get("status") in _STATUSES else "all"
            rows = await repo.fetch_catalog(
                p_key, days, limit, offset, sort, status,
                q=extra.get("q"),
                min_price=extra.get("min_price"),
                max_price=extra.get("max_price"),
                min_year=extra.get("min_year"),
                max_year=extra.get("max_year"),
            )
            total = await repo.count_catalog(
                p_key, days, status,
                q=extra.get("q"),
                min_price=extra.get("min_price"),
                max_price=extra.get("max_price"),
                min_year=extra.get("min_year"),
                max_year=extra.get("max_year"),
            )
            return {
                "profile_key": p_key,
                "total": total,
                "page": page,
                "page_size": limit,
                "items": rows,
                "saved_ids": sorted(saved_ids),
            }, HTTPStatus.OK
        if view == "queue":
            return {
                "pending": await repo.pending_eval_tasks(),
                "finished": await repo.recent_finished_eval_tasks(limit=5),
            }, HTTPStatus.OK
        if view == "listing":
            if ad_id is None:
                return {"error": "id required"}, HTTPStatus.BAD_REQUEST
            payload = await _listing_payload(repo, ad_id, p_key)
            if "error" not in payload and ad_id is not None:
                payload["is_saved"] = ad_id in saved_ids
            return payload, HTTPStatus.OK
        if view == "compare":
            ids = extra.get("ids") or []
            out = []
            for one in ids[:3]:
                payload = await _listing_payload(repo, one, p_key)
                if "error" not in payload:
                    payload["is_saved"] = one in saved_ids
                    out.append(payload)
            if not out:
                return {"error": "no listings found"}, HTTPStatus.NOT_FOUND
            return {"items": out}, HTTPStatus.OK
        if view == "saved":
            try:
                async with UserStore(settings.database_url) as store:
                    saved = await store.list_saved(chat_id)
                    saved_ids = {s.ad_id for s in saved}
            except Exception:
                logger.warning("saved load failed in webapp", exc_info=True)
                return {"error": "saved unavailable"}, HTTPStatus.OK
            items = []
            for s in saved:
                drop_pct = None
                if s.current_price is not None and s.max_price:
                    try:
                        if float(s.max_price) > 0:
                            drop_pct = round(
                                (1 - float(s.current_price) / float(s.max_price)) * 100, 1
                            )
                    except (TypeError, ValueError):
                        drop_pct = None
                items.append(
                    {
                        "ad_id": s.ad_id,
                        "title": s.ad_title,
                        "url": s.ad_url,
                        "note": s.note,
                        "current_price": s.current_price,
                        "drop_pct": drop_pct if (drop_pct or 0) > 0 else None,
                        "last_seen_at": s.last_seen_at,
                        "alert_below": s.alert_below,
                    }
                )
            return {"items": items, "saved_ids": sorted(saved_ids)}, HTTPStatus.OK
        if view == "profiles":
            try:
                async with UserStore(settings.database_url) as store:
                    prefs = await store.get_prefs(chat_id)
                    searches = await store.list_searches(chat_id)
            except Exception:
                logger.warning("profiles load failed in webapp", exc_info=True)
                return {"error": "profiles unavailable"}, HTTPStatus.OK
            from bazcar.llm.provider import PROMPT_META

            return {
                "active": {
                    "criteria": prefs.criteria if prefs else None,
                    "model": prefs.model if prefs else None,
                    "min_score": prefs.min_score if prefs else None,
                    "show_photo": bool(prefs.show_photo) if prefs else False,
                    "notify_enabled": bool(prefs.notify_enabled) if prefs else True,
                    "prompt_name": (
                        getattr(prefs, "prompt_name", None) or "standard"
                    ) if prefs else "standard",
                    "prompts": PROMPT_META,
                    "fallback_models": (
                        [m for m in (getattr(prefs, "fallback_models", None) or []) if m][:3]
                    ) if prefs else [],
                    "filters": dict(getattr(prefs, "filters", None) or {}) if prefs else {},
                },
                "searches": [
                    {
                        "name": s.name,
                        "criteria": s.criteria,
                        "min_score": s.min_score,
                        "notify_enabled": s.notify_enabled,
                    }
                    for s in searches
                ],
            }, HTTPStatus.OK
        if view == "stats":
            stats = await repo.stats_overview(p_key)
            histogram = await repo.price_histogram()
            top_makes = await repo.top_makes()
            svp = await repo.score_vs_price(p_key)
            trend30 = await repo.new_by_day(30)
            coverage = await repo.coverage_trend(p_key)
            drops = await repo.drops_leaderboard()
            funnel = await repo.funnel(p_key, chat_id)
            cost = await repo.eval_cost(p_key)
            return {
                "profile_key": p_key,
                "stats": stats,
                "histogram": histogram,
                "top_makes": top_makes,
                "score_vs_price": svp,
                "trend30": trend30,
                "coverage": coverage,
                "drops": drops,
                "funnel": funnel,
                "cost": cost,
            }, HTTPStatus.OK
        if view == "insights":
            # Merged overview + stats + queue for the unified Insights tab.
            # overview/stats/queue stay available for backward compatibility.
            stats = await repo.stats_overview(p_key)
            run = await repo.latest_scrape_run()
            pending = await repo.pending_eval_tasks()
            return {
                "profile_key": p_key,
                "stats": stats,
                "last_run": run,
                "queue_pending": len(pending),
                "pending": pending,
                "finished": await repo.recent_finished_eval_tasks(limit=5),
                "histogram": await repo.price_histogram(),
                "top_makes": await repo.top_makes(),
                "score_vs_price": await repo.score_vs_price(p_key),
                "trend30": await repo.new_by_day(30),
                "coverage": await repo.coverage_trend(p_key),
                "drops": await repo.drops_leaderboard(),
                "funnel": await repo.funnel(p_key, chat_id),
                "cost": await repo.eval_cost(p_key),
            }, HTTPStatus.OK
        if view == "bounds":
            # Price/year span of the browsable set (ignores the price/year
            # filters themselves) — initializes the dual sliders.
            status_f = extra.get("status") if extra.get("status") in _STATUSES else "all"
            bounds = await repo.catalog_bounds(p_key, days, status_f, extra.get("q"))
            return {"profile_key": p_key, "bounds": bounds}, HTTPStatus.OK
        if view == "models":
            # Real model list (cached, refreshed daily) + current selection.
            from bazcar.llm.provider import PROMPT_META, list_models_cached

            models, stamp = await list_models_cached(
                getattr(settings, "llm_api_key", None), repo
            )
            return {
                "models": models,
                "current": model,
                "fetched_at": stamp.isoformat() if stamp is not None and hasattr(stamp, "isoformat") else stamp,
                "prompt_name": prompt_name,
                "prompts": PROMPT_META,
                "fallback_models": fallback_models,
            }, HTTPStatus.OK
        if view == "templates":
            # Static ready-made searches (import-light, no DB needed).
            import dataclasses

            from bazcar.bot.templates import TEMPLATES

            return {
                "templates": [dataclasses.asdict(t) for t in TEMPLATES],
            }, HTTPStatus.OK
        if view == "health":
            # Pipeline health: last run, queue state, 30-day AI spend vs budget.
            run = await repo.latest_scrape_run()
            pending = await repo.pending_eval_tasks()
            cost = await repo.eval_cost(None, 30)
            try:
                spent = float((cost.get("total") or {}).get("cost_usd") or 0)
            except (TypeError, ValueError):
                spent = 0.0
            return {
                "last_run": run,
                "queue_pending": len(pending),
                "pending": pending[:5],
                "finished": await repo.recent_finished_eval_tasks(limit=3),
                "spend_30d_usd": spent,
                "budget_usd": settings.monthly_budget_usd,
                "database": bool(settings.database_url),
                "llm_key": bool(getattr(settings, "llm_api_key", None)),
            }, HTTPStatus.OK
        if view == "digest":
            # 7-day summary: new listings, biggest discounts, top matches, queue.
            trend = await repo.new_by_day(7)
            try:
                new_total = sum(int(n) for _, n in trend)
            except (TypeError, ValueError):
                new_total = 0
            top: list[dict] = []
            try:
                from bazcar.bot.store import UserStore as _US

                async with _US(settings.database_url, conn=conn) as _store:
                    _prefs = await _store.get_prefs(chat_id)
                    _ms = _prefs.min_score if _prefs and _prefs.min_score is not None else 0
                top = await repo.fetch_recent_matches(p_key, _ms, 7, 5)
            except Exception:
                logger.warning("digest top-matches failed", exc_info=True)
            pending = await repo.pending_eval_tasks()
            return {
                "days": 7,
                "new_total": new_total,
                "trend": [{"day": d, "n": n} for d, n in trend],
                "drops": await repo.drops_leaderboard(limit=5, days=7),
                "top": [
                    {
                        "ad_id": t.get("ad_id"),
                        "title": t.get("title"),
                        "url": t.get("url"),
                        "price_eur": t.get("price_eur"),
                        "score": t.get("score"),
                    }
                    for t in top
                ],
                "queue_pending": len(pending),
            }, HTTPStatus.OK
        return {"error": f"unknown view: {view}"}, HTTPStatus.BAD_REQUEST
    finally:
        await repo.close()


async def _queue_eval_task(
    chat_id: int, store, *, max_listings: int, days: int,
    ad_ids: list[int] | None = None,
) -> tuple[bool, str]:
    """Queue a re-score task for the user's active profile (Mini App side).

    Returns ``(created, note)`` — False when a task for the exact profile is
    already pending. The cron picks the task up (~15 min); there is no
    serverless-safe way to run the scrape inline. Never raises: a queue
    failure must not roll back the prefs change that triggered it.
    """
    from bazcar.config.settings import get_settings, load_llm_config
    from bazcar.core.profiles import profile_key
    from bazcar.db import ListingRepository
    from bazcar.llm.provider import normalize_prompt_name

    try:
        settings = get_settings()
        if not settings.database_url:
            return False, "database not configured"
        prefs = await store.get_prefs(chat_id)
        if prefs is None:
            return False, "no profile yet"
        model = (prefs.model if prefs else None) or load_llm_config().model
        prompt = normalize_prompt_name(getattr(prefs, "prompt_name", None) if prefs else None)
        fallbacks = [m for m in (getattr(prefs, "fallback_models", None) or []) if m][:3]
        criteria = prefs.criteria if prefs else None
        p_key = profile_key(criteria, model, prompt=prompt)
        repo = ListingRepository(settings.database_url)
        await repo.connect()
        try:
            await repo.init_schema()
            pending = await repo.pending_eval_tasks()
            if any(t["profile_key"] == p_key for t in pending):
                return False, "already queued"
            await repo.create_eval_task(
                p_key, criteria, model, max_listings, days,
                prompt_name=prompt, fallback_models=fallbacks, ad_ids=ad_ids,
            )
        finally:
            await repo.close()
        return True, "queued — the pipeline picks it up within ~15 min"
    except Exception as exc:
        logger.warning("webapp queue failed", exc_info=True)
        return False, f"queue failed: {exc}"


async def _handle_action(chat_id: int, body: dict) -> tuple[dict, HTTPStatus]:
    """Mutating Mini App actions (saved list, profiles, prefs)."""
    from bazcar.bot.store import UserStore
    from bazcar.config.settings import get_settings

    action = (body.get("action") or "").strip().lower()
    settings = get_settings()
    if not settings.database_url:
        return {"error": "database not configured"}, HTTPStatus.OK

    store = UserStore(settings.database_url)
    await store.connect()
    try:
        if action == "save":
            ad_id = _to_int(str(body.get("id") or ""), lo=1, hi=10**12)
            if ad_id is None:
                return {"error": "id required"}, HTTPStatus.BAD_REQUEST
            title = str(body.get("title") or "")[:200] or None
            url = str(body.get("url") or "")[:500] or None
            await store.save_listing(chat_id, ad_id, title, url)
            return {"ok": True}, HTTPStatus.OK
        if action == "unsave":
            ad_id = _to_int(str(body.get("id") or ""), lo=1, hi=10**12)
            if ad_id is None:
                return {"error": "id required"}, HTTPStatus.BAD_REQUEST
            removed = await store.remove_saved(chat_id, ad_id)
            return {"ok": True, "removed": removed}, HTTPStatus.OK
        if action == "note":
            ad_id = _to_int(str(body.get("id") or ""), lo=1, hi=10**12)
            note = str(body.get("note") or "")[:500]
            if ad_id is None:
                return {"error": "id required"}, HTTPStatus.BAD_REQUEST
            updated = await store.set_saved_note(chat_id, ad_id, note)
            return {"ok": True, "updated": updated}, HTTPStatus.OK
        if action == "alert":
            ad_id = _to_int(str(body.get("id") or ""), lo=1, hi=10**12)
            if ad_id is None:
                return {"error": "id required"}, HTTPStatus.BAD_REQUEST
            raw_below = body.get("below")
            if raw_below is None:
                updated = await store.set_saved_alert(chat_id, ad_id, None)
                return {"ok": True, "updated": updated}, HTTPStatus.OK
            try:
                below_raw = int(str(raw_below).strip())
            except (TypeError, ValueError):
                below_raw = 0
            if below_raw <= 0:
                return {"error": "below must be a positive number"}, HTTPStatus.BAD_REQUEST
            below = _to_int(str(below_raw), lo=1, hi=10**9)
            updated = await store.set_saved_alert(chat_id, ad_id, below)
            return {"ok": True, "updated": updated}, HTTPStatus.OK
        if action == "criteria":
            criteria = str(body.get("criteria") or "").strip()[:500]
            if not criteria:
                return {"error": "criteria required"}, HTTPStatus.BAD_REQUEST
            await store.ensure_prefs(chat_id)
            await store.update_prefs(chat_id, criteria=criteria)
            return {"ok": True}, HTTPStatus.OK
        if action == "use":
            name = str(body.get("name") or "").strip()
            if not name:
                return {"error": "name required"}, HTTPStatus.BAD_REQUEST
            searches = await store.list_searches(chat_id)
            match = next((s for s in searches if s.name.lower() == name.lower()), None)
            if match is None:
                return {"error": "search not found"}, HTTPStatus.NOT_FOUND
            await store.update_prefs(
                chat_id,
                criteria=match.criteria,
                filters=match.filters,
                min_score=match.min_score,
            )
            return {"ok": True}, HTTPStatus.OK
        if action == "toggle_search":
            name = str(body.get("name") or "").strip()
            enabled = body.get("enabled")
            if not name or not isinstance(enabled, bool):
                return {"error": "name + enabled required"}, HTTPStatus.BAD_REQUEST
            updated = await store.set_search_notify(chat_id, name, enabled)
            return {"ok": True, "updated": updated}, HTTPStatus.OK
        if action == "notify":
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                return {"error": "enabled required"}, HTTPStatus.BAD_REQUEST
            await store.ensure_prefs(chat_id)
            await store.update_prefs(chat_id, notify_enabled=enabled)
            return {"ok": True}, HTTPStatus.OK
        if action == "score":
            score = _to_int(str(body.get("min_score") if body.get("min_score") is not None else ""), lo=0, hi=100)
            if score is None:
                return {"error": "min_score 0-100 required"}, HTTPStatus.BAD_REQUEST
            await store.ensure_prefs(chat_id)
            await store.update_prefs(chat_id, min_score=score)
            return {"ok": True}, HTTPStatus.OK
        if action == "photos":
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                return {"error": "enabled required"}, HTTPStatus.BAD_REQUEST
            await store.ensure_prefs(chat_id)
            await store.update_prefs(chat_id, show_photo=enabled)
            return {"ok": True}, HTTPStatus.OK
        if action == "model":
            model_id = str(body.get("id") or body.get("model") or "").strip()[:200]
            if not model_id:
                return {"error": "id required"}, HTTPStatus.BAD_REQUEST
            await store.ensure_prefs(chat_id)
            await store.update_prefs(chat_id, model=model_id)
            created, note = await _queue_eval_task(chat_id, store, max_listings=100, days=3)
            return {"ok": True, "queued": created, "note": note}, HTTPStatus.OK
        if action == "prompt":
            from bazcar.llm.provider import prompt_names

            name = str(body.get("name") or "").strip().lower()
            if name not in prompt_names():
                return {
                    "error": f"unknown prompt (one of: {', '.join(prompt_names())})"
                }, HTTPStatus.BAD_REQUEST
            await store.ensure_prefs(chat_id)
            await store.update_prefs(chat_id, prompt_name=name)
            created, note = await _queue_eval_task(chat_id, store, max_listings=100, days=3)
            return {"ok": True, "queued": created, "note": note}, HTTPStatus.OK
        if action == "fallbacks":
            raw = body.get("models")
            if isinstance(raw, str):
                raw = raw.replace(";", ",").split(",")
            models = [str(m).strip()[:200] for m in (raw or []) if str(m).strip()][:3]
            await store.ensure_prefs(chat_id)
            await store.update_prefs(chat_id, fallback_models=models)
            return {"ok": True, "fallback_models": models}, HTTPStatus.OK
        if action == "filters":
            patch = body.get("filters")
            if not isinstance(patch, dict):
                return {"error": "filters object required"}, HTTPStatus.BAD_REQUEST
            clean: dict = {}
            if "query" in patch:
                clean["query"] = str(patch["query"] or "")[:80]
            for key in ("min_price", "max_price"):
                if key in patch:
                    raw_v = patch[key]
                    clean[key] = (
                        _to_int(str(raw_v), lo=0, hi=10**9)
                        if raw_v not in (None, "") else ""
                    )
            if "psc" in patch:
                clean["psc"] = str(patch["psc"] or "")[:10]
            dist_raw = patch.get("distance_km", patch.get("dist", "##unset##"))
            if dist_raw != "##unset##":
                clean["distance_km"] = (
                    _to_int(str(dist_raw), lo=1, hi=500)
                    if dist_raw not in (None, "") else ""
                )
            filters = await store.update_filters(chat_id, clean)
            return {"ok": True, "filters": filters}, HTTPStatus.OK
        if action == "search_create":
            name = str(body.get("name") or "").strip()[:60]
            criteria = str(body.get("criteria") or "").strip()[:500]
            if not name or not criteria:
                return {"error": "name + criteria required"}, HTTPStatus.BAD_REQUEST
            filters = body.get("filters") if isinstance(body.get("filters"), dict) else {}
            raw_ms = body.get("min_score")
            min_score = (
                _to_int(str(raw_ms), lo=0, hi=100) if raw_ms not in (None, "") else None
            )
            await store.ensure_prefs(chat_id)
            await store.save_search(
                chat_id, name, criteria, filters=filters, min_score=min_score
            )
            return {"ok": True}, HTTPStatus.OK
        if action == "search_delete":
            name = str(body.get("name") or "").strip()
            if not name:
                return {"error": "name required"}, HTTPStatus.BAD_REQUEST
            removed = await store.delete_search(chat_id, name)
            return {"ok": True, "removed": removed}, HTTPStatus.OK
        if action == "template":
            from bazcar.bot.templates import get_template

            key = str(body.get("key") or "").strip().lower()
            tpl = get_template(key)
            if tpl is None:
                return {"error": "unknown template"}, HTTPStatus.NOT_FOUND
            await store.ensure_prefs(chat_id)
            await store.update_prefs(
                chat_id,
                criteria=tpl.criteria,
                filters=dict(tpl.filters),
                min_score=tpl.min_score,
            )
            await store.save_search(
                chat_id, tpl.name, tpl.criteria,
                filters=dict(tpl.filters), min_score=tpl.min_score,
            )
            created, note = await _queue_eval_task(chat_id, store, max_listings=100, days=3)
            return {"ok": True, "queued": created, "note": note}, HTTPStatus.OK
        if action == "eval":
            limit = _to_int(str(body.get("limit") if body.get("limit") is not None else "100"),
                            lo=1, hi=500) or 100
            days = _to_int(str(body.get("days") if body.get("days") is not None else "3"),
                           lo=1, hi=365) or 3
            created, note = await _queue_eval_task(
                chat_id, store, max_listings=limit, days=days
            )
            return {"ok": True, "queued": created, "note": note}, HTTPStatus.OK
        if action == "rescore":
            raw = body.get("ids") or []
            parts = raw if isinstance(raw, list) else str(raw).replace(",", " ").split()
            ids = []
            for part in parts:
                text = str(part).strip()
                if text.isdigit() and int(text) > 0:
                    ids.append(int(text))
            ids = ids[:20]
            if not ids:
                return {"error": "ids required"}, HTTPStatus.BAD_REQUEST
            created, note = await _queue_eval_task(
                chat_id, store, max_listings=len(ids), days=365, ad_ids=ids
            )
            return {"ok": True, "queued": created, "ids": ids, "note": note}, HTTPStatus.OK
        return {"error": f"unknown action: {action}"}, HTTPStatus.BAD_REQUEST
    finally:
        await store.close()


def _respond(start_response, status: HTTPStatus, payload: dict) -> list[bytes]:
    body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
    start_response(
        f"{status.value} {status.phrase}",
        [("Content-Type", "application/json; charset=utf-8"),
         ("Cache-Control", "no-store")],
    )
    return [body]


def _read_json_body(environ) -> dict:
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        return {}
    if length <= 0 or length > 65536:
        return {}
    try:
        raw = environ["wsgi.input"].read(length) or b"{}"
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, KeyError):
        return {}


def app(environ, start_response):  # noqa: ANN001 — WSGI callable
    from bazcar.config.settings import get_settings

    settings = get_settings()
    if not settings.telegram_bot_token:
        return _respond(
            start_response, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "bot not configured"}
        )

    method = environ.get("REQUEST_METHOD", "GET")
    query = parse_qs(environ.get("QUERY_STRING", ""))
    body: dict = {}
    if method == "POST":
        body = _read_json_body(environ)
    init_data = (
        environ.get("HTTP_X_TELEGRAM_INIT_DATA")
        or (query.get("initData") or [""])[0]
        or str(body.get("initData") or "")
    )
    user = validate_init_data(init_data, settings.telegram_bot_token)
    if user is None:
        return _respond(start_response, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})

    if method == "POST":
        try:
            payload, status = asyncio.run(_handle_action(user["id"], body))
            return _respond(start_response, status, payload)
        except Exception:
            logger.exception("webapp action failed")
            return _respond(
                start_response, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"}
            )

    if method != "GET":
        return _respond(start_response, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "GET only"})

    view = (query.get("view") or ["overview"])[0]
    try:
        page = max(0, int((query.get("page") or ["0"])[0]))
        days = max(1, min(int((query.get("days") or ["30"])[0]), 365))
    except ValueError:
        page, days = 0, 30
    ad_id: int | None = None
    raw_id = (query.get("id") or [""])[0]
    if raw_id.strip().lstrip("-").isdigit():
        ad_id = int(raw_id)
    ids: list[int] = []
    for part in (query.get("ids") or [""])[0].split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))

    def _opt_int(name: str, lo: int, hi: int) -> int | None:
        return _to_int((query.get(name) or [""])[0] or None, lo=lo, hi=hi)

    sort = (query.get("sort") or ["newest"])[0]
    status = (query.get("status") or ["all"])[0]
    q = (query.get("q") or [""])[0].strip()[:80] or None
    extra = {
        "sort": sort if sort in _SORTS else "newest",
        "status": status if status in _STATUSES else "all",
        "q": q,
        "min_price": _opt_int("min_price", 0, 10**9),
        "max_price": _opt_int("max_price", 0, 10**9),
        "min_year": _opt_int("min_year", 1950, 2030),
        "max_year": _opt_int("max_year", 1950, 2030),
        "ids": ids[:3],
        "per": (query.get("per") or ["15"])[0],
    }

    try:
        payload, status = asyncio.run(_load_view(view, user["id"], page, days, ad_id, extra))
        return _respond(start_response, status, payload)
    except Exception:
        logger.exception("webapp view %s failed", view)
        return _respond(
            start_response, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"}
        )
