"""Telegram Mini App JSON API (Vercel serverless).

Serves the dashboard SPA (api/dashboard.py) with live data from Neon.

Auth: every request must carry the Telegram WebApp ``initData`` — either as
the ``initData`` query parameter or the ``X-Telegram-Init-Data`` header. The
HMAC-SHA256 signature is verified against the bot token (Telegram's
WebAppData scheme) and the embedded user id becomes the caller's chat_id, so
each user only ever sees their own profile's data. Anything else gets a 401.

Views (``?view=...``): ``overview`` | ``inventory`` | ``listing`` (needs
``&id=<ad_id>``) | ``queue`` | ``stats``.
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


async def _load_view(
    view: str, chat_id: int, page: int, days: int, ad_id: int | None = None
) -> dict:
    """Load one dashboard view from Neon (repository methods shared with bot)."""
    from bazcar.bot.store import UserStore
    from bazcar.config.settings import get_settings, load_llm_config
    from bazcar.core.profiles import profile_key
    from bazcar.db import ListingRepository

    settings = get_settings()
    if not settings.database_url:
        return {"error": "database not configured"}

    repo = ListingRepository(settings.database_url)
    await repo.connect()
    try:
        criteria, model = None, load_llm_config().model
        try:
            async with UserStore(settings.database_url) as store:
                prefs = await store.get_prefs(chat_id)
                if prefs is not None:
                    criteria = prefs.criteria
                    model = prefs.model or model
        except Exception:
            logger.warning("prefs load failed in webapp", exc_info=True)
        p_key = profile_key(criteria, model)

        if view == "overview":
            stats = await repo.stats_overview(p_key)
            run = await repo.latest_scrape_run()
            pending = await repo.pending_eval_tasks()
            return {
                "profile_key": p_key,
                "stats": stats,
                "last_run": run,
                "queue_pending": len(pending),
            }
        if view == "inventory":
            limit = 15
            offset = max(0, page) * limit
            rows = await repo.fetch_catalog(p_key, days, limit, offset, "newest", "all")
            total = await repo.count_catalog(p_key, days, "all")
            return {
                "profile_key": p_key,
                "total": total,
                "page": page,
                "page_size": limit,
                "items": rows,
            }
        if view == "queue":
            return {
                "pending": await repo.pending_eval_tasks(),
                "finished": await repo.recent_finished_eval_tasks(limit=5),
            }
        if view == "listing":
            if ad_id is None:
                return {"error": "id required"}
            row = await repo.fetch_listing_by_id(ad_id, p_key)
            if row is None:
                return {"error": "not found"}
            history = await repo.fetch_price_history(ad_id)
            from bazcar.bot.daemon import extract_make

            make = extract_make(row.get("title") or "")
            bench = await repo.price_benchmark(make, row.get("year")) if make else None
            return {"listing": row, "history": history, "benchmark": bench}
        if view == "stats":
            return {"profile_key": p_key, "stats": await repo.stats_overview(p_key)}
        return {"error": f"unknown view: {view}"}
    finally:
        await repo.close()


def _respond(start_response, status: HTTPStatus, payload: dict) -> list[bytes]:
    body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
    start_response(
        f"{status.value} {status.phrase}",
        [("Content-Type", "application/json; charset=utf-8"),
         ("Cache-Control", "no-store")],
    )
    return [body]


def app(environ, start_response):  # noqa: ANN001 — WSGI callable
    if environ.get("REQUEST_METHOD", "GET") != "GET":
        return _respond(start_response, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "GET only"})

    query = parse_qs(environ.get("QUERY_STRING", ""))
    init_data = (
        environ.get("HTTP_X_TELEGRAM_INIT_DATA")
        or (query.get("initData") or [""])[0]
    )

    from bazcar.config.settings import get_settings

    settings = get_settings()
    if not settings.telegram_bot_token:
        return _respond(
            start_response, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "bot not configured"}
        )
    user = validate_init_data(init_data, settings.telegram_bot_token)
    if user is None:
        return _respond(start_response, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})

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

    try:
        payload = asyncio.run(_load_view(view, user["id"], page, days, ad_id))
        return _respond(start_response, HTTPStatus.OK, payload)
    except Exception:
        logger.exception("webapp view %s failed", view)
        return _respond(
            start_response, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"}
        )
