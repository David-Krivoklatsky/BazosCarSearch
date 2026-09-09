"""Telegram bot daemon (Phase 4): set preferences, saved lists, model, criteria.

Runs ``bazcar bot`` — a long-polling loop (``getUpdates``) that answers the
user's commands. Scraping itself stays in the CLI/cron; the bot only persists
preferences that the pipeline reads back (criteria, model, min score, photos)
and lets the user save/bookmark listings for later.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from bazcar.bot.store import UserStore
from bazcar.config.settings import get_settings
from bazcar.core.exceptions import ConfigError
from bazcar.core.models import Listing
from bazcar.db.repository import ListingRepository

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


def _em(text: str) -> str:
    """HTML-escape a plain user-supplied string for Telegram parse_mode=HTML."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

HELP_TEXT = (
    "<b>Bazcar bot</b> 🚗\n"
    "Príkazy:\n"
    "/help — nápoveda\n"
    "/status — aktuálne nastavenia\n"
    "/criteria + text — čo hľadám (napr.: diesel do 200t km, nad 80 kW)\n"
    "/search meno: kritériá — uložiť pomenované hľadanie\n"
    "/searches — zoznam uložených hľadaní\n"
    "/use meno — aktivovať uložené hľadanie\n"
    "/model nazov — zmeniť AI model (napr. openrouter/free)\n"
    "/score 0-100 — ukazovať len inzeráty od daného hodnotenia\n"
    "/photos on|off — posielať fotky k inzerátom\n"
    "/save ad_id — uložiť inzerát na neskôr\n"
    "/saved — moje uložené inzeráty\n"
    "/unsave ad_id — odstrániť uložený"
)


class Bot:
    """Message handler bound to a TelegramNotifier and UserStore."""

    def __init__(self, store: UserStore, notifier, export_dir: Path) -> None:
        self.store = store
        self.notifier = notifier
        self.export_dir = export_dir

    async def reply(self, chat_id: int, text: str) -> None:
        await self.notifier.send_message(chat_id=chat_id, text=text)

    async def handle(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        chat_id = (message.get("chat") or {}).get("id")
        if not text or not chat_id:
            return
        await self.store.ensure_prefs(chat_id)
        if text.startswith("/"):
            reply = await self._command(chat_id, text)
        else:
            reply = await self._set_criteria(chat_id, text)
        await self.reply(chat_id, reply)

    async def _command(self, chat_id: int, text: str) -> str:
        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        match cmd:
            case "/start":
                return "Ahoj! Som Bazcar 🚗. Pošli mi, aké auto hľadáš.\n\n" + HELP_TEXT
            case "/help":
                return HELP_TEXT
            case "/status":
                return await self._status(chat_id)
            case "/criteria":
                return await self._set_criteria(chat_id, arg)
            case "/search":
                return await self._search(chat_id, arg)
            case "/searches":
                return await self._searches(chat_id)
            case "/use":
                return await self._use(chat_id, arg)
            case "/model":
                return await self._model(chat_id, arg)
            case "/score":
                return await self._score(chat_id, arg)
            case "/photos":
                return await self._photos(chat_id, arg)
            case "/save":
                return await self._save(chat_id, arg)
            case "/saved":
                return await self._saved(chat_id)
            case "/unsave":
                return await self._unsave(chat_id, arg)
            case _:
                return "Neznámy príkaz. /help"

    async def _set_criteria(self, chat_id: int, text: str) -> str:
        text = text.strip()
        if not text:
            return 'Pošli, čo hľadáš, napr.: "diesel, do 200 000 km, nad 80kW".'
        await self.store.update_prefs(chat_id, criteria=text)
        return f"✅ Kritériá uložené:\n<i>{_em(text)}</i>"

    async def _search(self, chat_id: int, arg: str) -> str:
        if not arg:
            return "/search meno: kriteriá — napr. `/search diaľnica: diesel do 200t km`"
        name_part, sep, criteria = arg.partition(":")
        name = name_part.strip()
        criteria = criteria.strip()
        if not name or not criteria:
            return "Potrebujem `meno: kritériá` — napr. `/search diaľnica: diesel do 200t km`."
        await self.store.save_search(chat_id, name, criteria)
        return f"✅ Uložené hľadanie „{_em(name)}“: <i>{_em(criteria)}</i>"

    async def _searches(self, chat_id: int) -> str:
        searches = await self.store.list_searches(chat_id)
        if not searches:
            return "Nemáš uložené hľadania. `/search meno: diesel do 200 km`."
        lines = ["Uložené hľadania:"] + [f"• <b>{_em(s.name)}</b>: {_em(s.criteria)}" for s in searches]
        return "\n".join(lines)

    async def _use(self, chat_id: int, name: str) -> str:
        if not name:
            return "/use meno"
        searches = await self.store.list_searches(chat_id)
        for s in searches:
            if s.name.lower() == name.lower():
                await self.store.update_prefs(chat_id, criteria=s.criteria)
                return f"✅ Aktivované „{_em(s.name)}“: <i>{_em(s.criteria)}</i>"
        return f"Nenašiel som „{_em(name)}“. /searches"

    async def _model(self, chat_id: int, model: str) -> str:
        if not model:
            return "/model nazov — napr. `/model openrouter/free`."
        await self.store.update_prefs(chat_id, model=model.strip())
        return f"✅ Model: <code>{_em(model.strip())}</code>"

    async def _score(self, chat_id: int, arg: str) -> str:
        if not arg:
            prefs = await self.store.get_prefs(chat_id)
            return f"Min. hodnotenie: <b>{prefs.min_score if prefs else None}</b>"
        try:
            value = int(arg)
            if not 0 <= value <= 100:
                raise ValueError
        except ValueError:
            return "Hodnota 0–100, napr. `/score 75`."
        await self.store.update_prefs(chat_id, min_score=value)
        return f"✅ Odteraz ukazujem inzeráty od <b>{value}/100</b>."

    async def _photos(self, chat_id: int, arg: str) -> str:
        arg = arg.strip().lower()
        if arg not in {"on", "off"}:
            return "/photos on|off"
        on = arg == "on"
        await self.store.update_prefs(chat_id, show_photo=on)
        return "✅ Fotky pri inzerátoch: " + ("zapnuté" if on else "vypnuté")

    async def _save(self, chat_id: int, arg: str) -> str:
        try:
            ad_id = int(arg)
        except ValueError:
            return "/save ad_id"
        listing = await self._find_listing(ad_id)
        title = listing.title if listing else None
        url = listing.url if listing else None
        await self.store.save_listing(chat_id, ad_id, title, url)
        return f"💾 Uložené ad {ad_id}."

    async def _saved(self, chat_id: int) -> str:
        saved = await self.store.list_saved(chat_id)
        if not saved:
            return "Nemáš nič uložené."
        lines = ["Uložené inzeráty:"] + [
            f"• <b>{_em(item.ad_title or f'ad {item.ad_id}')}</b> — {_em(item.ad_url or str(item.ad_id))}"
            for item in saved
        ]
        return "\n".join(lines)

    async def _unsave(self, chat_id: int, arg: str) -> str:
        try:
            ad_id = int(arg)
        except ValueError:
            return "/unsave ad_id"
        removed = await self.store.remove_saved(chat_id, ad_id)
        return "Odstránené ✅" if removed else f"Ad {ad_id} nemáš uložený."

    async def _status(self, chat_id: int) -> str:
        prefs = await self.store.get_prefs(chat_id)
        if prefs is None:
            return "Žiadne nastavenia. /help"
        return (
            "<b>Stav</b>\n"
            f"Kritériá: {_em(prefs.criteria or '—')}\n"
            f"Model: {_em(prefs.model or 'default')}\n"
            f"Min. hodnotenie: {_em(str(prefs.min_score or '—'))}\n"
            f"Fotky: {'áno' if prefs.show_photo else 'nie'}"
        )

    async def _find_listing(self, ad_id: int) -> Listing | None:
        """Best-effort: resolve a saved ad title/url from the latest export."""
        try:
            files = sorted(self.export_dir.glob("*.json"))
            if not files:
                return None
            data = json.loads(files[-1].read_text(encoding="utf-8"))
            for item in data.get("items", []):
                if item["ad_id"] == ad_id:
                    return Listing(**item)
        except Exception:
            return None
        return None


async def poll(token: str, store: UserStore, notifier, export_dir: Path, timeout: int = 5) -> int:
    """Fetch one batch of updates and process them; returns handled count."""
    bot = Bot(store=store, notifier=notifier, export_dir=export_dir)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{TELEGRAM_API}/bot{token}/getUpdates",
            json={"timeout": timeout, "allowed_updates": ["message"]},
        )
        resp.raise_for_status()
        updates = resp.json().get("result", [])
    for update in updates:
        try:
            await bot.handle(update)
        except Exception:
            logger.exception("update %s failed", update.get("update_id"))
    return len(updates)


async def run_bot_loop(once: bool = False) -> None:
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise ConfigError("bot needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    if not settings.database_url:
        raise ConfigError("bot needs BAZCAR_DATABASE_URL (stores preferences)")

    async with UserStore(settings.database_url) as store:
        async with ListingRepository(settings.database_url) as repo:
            await repo.init_schema()
        from bazcar.notify import TelegramNotifier

        async with TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id) as notifier:
            if once:
                handled = await poll(
                    settings.telegram_bot_token, store, notifier, settings.export_dir, timeout=1
                )
                logger.info("polled once, handled %d updates", handled)
                return
            logger.info("bot polling every ~1s...")
            while True:
                try:
                    await poll(settings.telegram_bot_token, store, notifier, settings.export_dir)
                except httpx.HTTPError as exc:
                    logger.warning("poll error: %s", exc)
                await asyncio.sleep(1.0)
