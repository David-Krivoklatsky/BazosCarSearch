"""Telegram notifier: sends deal alerts via the Bot API (Phase 4)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from bazcar.core.models import Listing
from bazcar.notify.format import format_listing, format_photo_caption, listing_keyboard

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

    async def _post(self, method: str, payload: dict[str, Any]) -> bool:
        try:
            resp = await self._client.post(f"{self._base}/bot{self._token}/{method}", json=payload)
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            logger.warning("telegram %s failed: %s", method, exc)
            return False

    async def send_message(
        self,
        text: str,
        *,
        chat_id: int | None = None,
        silent: bool = False,
        reply_markup: dict | None = None,
    ) -> bool:
        """Send one HTML message; returns True when Telegram accepted it.

        ``chat_id`` defaults to the configured chat. ``reply_markup`` is an
        InlineKeyboardMarkup dict. Failures are logged and swallowed.
        """
        payload: dict[str, Any] = {
            "chat_id": chat_id or self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": silent,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return await self._post("sendMessage", payload)

    async def send_photo(
        self,
        photo_url: str,
        caption: str = "",
        *,
        chat_id: int | None = None,
        silent: bool = False,
        reply_markup: dict | None = None,
    ) -> bool:
        """Send one photo with an HTML caption (Telegram 'sendPhoto').

        ``photo_url`` may be a HTTPS URL; Telegram fetches it server-side.
        """
        payload: dict[str, Any] = {
            "chat_id": chat_id or self._chat_id,
            "photo": photo_url,
            "caption": caption[:1024],
            "parse_mode": "HTML",
            "disable_notification": silent,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return await self._post("sendPhoto", payload)

    async def answer_callback(self, callback_query_id: str, text: str | None = None) -> bool:
        """Acknowledge an inline-button press (Telegram 'answerCallbackQuery').

        Stops the client's loading spinner; ``text`` is shown as a toast.
        """
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return await self._post("answerCallbackQuery", payload)

    async def send_listings(
        self, listings: list[Listing], *, chat_id: int | None = None, silent: bool = False
    ) -> int:
        """Send notifications for each listing; returns the number of successes."""
        sent = 0
        for listing in listings:
            if await self.send_message(
                format_listing(listing),
                chat_id=chat_id,
                silent=silent,
                reply_markup=listing_keyboard(listing),
            ):
                sent += 1
        return sent

    async def send_listings_with_photos(
        self, listings: list[Listing], *, chat_id: int | None = None, silent: bool = False
    ) -> int:
        """Send each listing as a photo message when an image exists; fall back to text."""
        sent = 0
        for listing in listings:
            if listing.image_urls:
                if await self.send_photo(
                    listing.image_urls[0],
                    caption=format_photo_caption(listing),
                    chat_id=chat_id,
                    silent=silent,
                    reply_markup=listing_keyboard(listing),
                ):
                    sent += 1
            elif await self.send_message(
                format_listing(listing),
                chat_id=chat_id,
                silent=silent,
                reply_markup=listing_keyboard(listing),
            ):
                sent += 1
        return sent
