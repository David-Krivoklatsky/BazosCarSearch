"""Telegram notifier: sends deal alerts via the Bot API (Phase 4)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from bazcar.core.models import Listing
from bazcar.notify.format import format_listing

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class TelegramNotifier:
    """Thin async client over the Telegram ``sendMessage`` API."""

    def __init__(self, bot_token: str, chat_id: str, *, api_base: str = TELEGRAM_API) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._base = api_base.rstrip("/")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> TelegramNotifier:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def send_message(self, text: str, *, silent: bool = False) -> bool:
        """Send one HTML message; returns True when Telegram accepted it.

        Failures are logged and swallowed (notification must never kill the
        pipeline), so callers just count successes.
        """
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": silent,
        }
        try:
            resp = await self._client.post(
                f"{self._base}/bot{self._token}/sendMessage", json=payload
            )
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            logger.warning("telegram send failed: %s", exc)
            return False

    async def send_listings(self, listings: list[Listing], *, silent: bool = False) -> int:
        """Send notifications for each listing; returns the number of successes."""
        sent = 0
        for listing in listings:
            if await self.send_message(format_listing(listing), silent=silent):
                sent += 1
        return sent
