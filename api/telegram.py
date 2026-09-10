"""Telegram webhook endpoint for Vercel (serverless bot, instant replies).

Architecture split:
  * Vercel (this file): ALL bot interactivity — commands, inline-button
    callbacks, DB reads/writes (Neon) and replies. Telegram pushes each update
    here via HTTPS (setWebhook), so no polling is needed.
  * GitHub Actions (scrape.yml): heavy scraping + LLM eval + deal notifications.

Security: every request must carry the ``X-Telegram-Bot-Api-Secret-Token``
header matching ``TELEGRAM_WEBHOOK_SECRET`` (set via setWebhook secret_token).

Reliability: updates are de-duplicated by ``update_id`` (Telegram delivers
at-least-once); the handler always answers 200 after claiming an update so
Telegram does not retry-storm; failures are reported to the user in chat.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import sys
from http import HTTPStatus
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

logger = logging.getLogger("bazcar.webhook")
logging.basicConfig(level=logging.INFO)


def _process_update(update: dict) -> None:
    """Run the bot handler for one Telegram update against Neon + Bot API."""
    from bazcar.bot.daemon import Bot
    from bazcar.bot.store import UserStore
    from bazcar.config.settings import get_settings
    from bazcar.notify.telegram import TelegramNotifier

    settings = get_settings()
    if not settings.telegram_bot_token or not settings.database_url:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / BAZCAR_DATABASE_URL missing in env")

    async def run() -> None:
        # Keep the schema fresh (eval_tasks/evaluations may be created after a
        # deploy, before the next cron scrape) — idempotent, cheap.
        try:
            from bazcar.db import ListingRepository

            repo = ListingRepository(settings.database_url)
            await repo.connect()
            try:
                await repo.init_schema()
            finally:
                await repo.close()
        except Exception:
            logger.exception("schema init in webhook failed (continuing)")

        store = UserStore(settings.database_url)
        await store.connect()
        try:
            notifier = TelegramNotifier(
                settings.telegram_bot_token, settings.telegram_chat_id or ""
            )
            try:
                bot = Bot(store=store, notifier=notifier, export_dir=settings.export_dir)
                await bot.handle(update)
            finally:
                await notifier.close()
        finally:
            await store.close()

    asyncio.run(run())


def _claim_update(update_id: int | None) -> bool:
    """Atomically claim a Telegram update (True = first delivery)."""

    async def run() -> bool:
        from bazcar.bot.store import UserStore
        from bazcar.config.settings import get_settings

        settings = get_settings()
        if not update_id:
            return True  # unknown payload shape — let the handler decide
        store = UserStore(settings.database_url)
        await store.connect()
        try:
            return await store.claim_update(int(update_id))
        finally:
            await store.close()

    return asyncio.run(run())


def _respond(start_response, status: HTTPStatus, body: bytes) -> list[bytes]:
    start_response(
        f"{status.value} {status.phrase}", [("Content-Type", "text/plain")]
    )
    return [body]


def app(environ, start_response):  # noqa: ANN001 — WSGI callable
    method = environ.get("REQUEST_METHOD", "GET")
    if method != "POST":
        return _respond(start_response, HTTPStatus.METHOD_NOT_ALLOWED, b"POST only")

    secret = environ.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN", "")
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    if not expected or not hmac.compare_digest(secret.encode(), expected.encode()):
        return _respond(start_response, HTTPStatus.FORBIDDEN, b"forbidden")

    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        update = json.loads(environ["wsgi.input"].read(length) or b"{}")
    except (ValueError, json.JSONDecodeError):
        # Cannot parse -> nothing to process; 200 so Telegram drops it.
        return _respond(start_response, HTTPStatus.OK, b"bad payload ignored")

    if not isinstance(update, dict):
        return _respond(start_response, HTTPStatus.OK, b"bad payload ignored")

    # At-least-once delivery from Telegram -> skip updates we already handled.
    if not _claim_update(update.get("update_id") or 0):
        return _respond(start_response, HTTPStatus.OK, b"duplicate ignored")

    try:
        _process_update(update)
        return _respond(start_response, HTTPStatus.OK, b"ok")
    except Exception:  # never let Telegram redeliver / kill the worker
        logger.exception("update %s failed", update.get("update_id"))
        try:
            from bazcar.config.settings import get_settings
            from bazcar.notify.telegram import TelegramNotifier

            settings = get_settings()
            chat_id = (update.get("message") or update.get("callback_query", {}).get("message") or {}).get(
                "chat", {}
            ).get("id")
            if chat_id and settings.telegram_bot_token:
                async def _err() -> None:
                    async with TelegramNotifier(settings.telegram_bot_token, str(chat_id)) as ntf:
                        await ntf.send_message("⚠️ Chyba pri spracovaní — skús to znova.")

                asyncio.run(_err())
        except Exception:
            logger.exception("error reply also failed")
        return _respond(start_response, HTTPStatus.OK, b"handled with error")
