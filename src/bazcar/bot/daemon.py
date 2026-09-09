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
import os
import re
from pathlib import Path
from typing import Any

import httpx

from bazcar.bot.store import UserStore
from bazcar.config.settings import get_settings, load_llm_config
from bazcar.core.exceptions import ConfigError
from bazcar.core.models import DealEvaluation, Listing
from bazcar.db.repository import ListingRepository
from bazcar.notify.telegram import TelegramNotifier
from bazcar.pipeline.runner import profile_key

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


def _row_to_listing(row: dict) -> Listing:
    """Rebuild a Listing (+DealEvaluation) from a /show DB row."""
    return Listing(
        ad_id=row["ad_id"],
        url=row["url"],
        title=row["title"],
        price_eur=row["price_eur"],
        city=row["city"],
        image_urls=row["image_urls"],
        year=row["year"],
        mileage_km=row["mileage_km"],
        description_preview=row["description_preview"] or "",
        description=row["description"],
        evaluation=DealEvaluation(
            score=row["score"],
            why=row["why"] or "",
            risk=row["risk"] or "",
            model=row["eval_model"],
        ),
    )


def _em_url(url: str) -> str:
    """Escape a URL for use inside an HTML attribute (href)."""
    return url.replace("&", "&amp;").replace('"', "&quot;")


def _em(text: str) -> str:
    """HTML-escape a plain user-supplied string for Telegram parse_mode=HTML."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _dispatch_scrape() -> bool:
    """Best-effort: trigger the scrape workflow right after a preference change.

    Requires ``GITHUB_WORKFLOW_TOKEN`` (repo scope, actions:write) in the env —
    on Vercel set it as an env var; locally it simply skips. The next scrape
    (and thus new results) then arrives within a few minutes instead of up to
    the next 15-minute cron slot.
    """
    token = os.environ.get("GITHUB_WORKFLOW_TOKEN")
    repo = os.environ.get("GITHUB_REPO", "David-Krivoklatsky/BazosCarSearch")
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://api.github.com/repos/{repo}/actions/workflows/scrape.yml/dispatches",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                json={"ref": "master"},
            )
            return resp.status_code == 204
    except httpx.HTTPError:
        logger.warning("scrape dispatch failed", exc_info=True)
        return False

HELP_TEXT = (
    "<b>🤖 Bazcar — pomocník pri hľadaní auta</b>\n\n"
    "Napíš mi voľne, čo hľadáš (napr. <i>\"diesel do 200t km, nad 80 kW\"</i>)\n"
    "a bodovanie inzerátov sa prispôsobí tvojim kritériám.\n\n"
    "<b>🧭 Príkazy</b>\n"
    "  <b>/criteria</b> — zmeniť kritériá hľadania\n"
    "  <b>/search</b> — uložiť pomenované hľadanie (meno: kritériá)\n"
    "  <b>/searches</b> — zoznam uložených hľadaní\n"
    "  <b>/use</b> — aktivovať uložené hľadanie\n"
    "  <b>/status</b> — aktuálne nastavenia\n\n"
    "<b>🎛️ Bodovanie a zobrazenie</b>\n"
    "  <b>/score</b> — ukazovať len inzeráty od daného hodnotenia (0–100)\n"
    "  <b>/photos</b> — on/off fotky pri inzerátoch\n"
    "  <b>/model</b> — zmeniť AI model (napr. openrouter/free)\n\n"
    "<b>🔎 Bazoš filtre</b>\n"
    "  <b>/filter</b> — aktuálne filtre\n"
    "  <b>/filter query</b> + text — hľadané slovo (napr. skoda octavia)\n"
    "  <b>/filter price</b> + 1500-4000 — rozsah ceny v €\n"
    "  <b>/filter dist</b> + 100 — okruh okolo PSČ v km (najprv nastav /filter psc)\n"
    "  <b>/filter psc</b> + 81101 — lokalita\n"
    "  <b>/filter clear</b> — vymazať filtre\n\n"
    "<i>Pozn.: najazdené km nie je Bazoš filter — zohľadná ho AI cez tvoje kritériá.</i>\n\n"
    "<b>📊 Zobrazenie</b>\n"
    "  <b>/show</b> — všetky vyhovujúce inzeráty z posledných dní\n"
    "  <b>/eval</b> — prehodnotiť autá z posledných dní podľa aktívneho searchu\n"
    "  <b>/models</b> — dostupné AI modely (free / paid)\n\n"
    "<b>💾 Uložené inzeráty</b>\n"
    "  <b>/save</b> — uložiť inzerát (ad_id) na neskôr\n"
    "  <b>/saved</b> — zoznam uložených inzerátov\n"
    "  <b>/unsave</b> — odstrániť uložený inzerát\n\n"
    "Všetko ukladám do databázy, tvoje nastavenia prežijú aj reštart."
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
        callback = update.get("callback_query") or {}
        message = update.get("message") or {}

        # Inline-button presses ("💾 Uložiť" on a deal notification).
        if callback.get("data"):
            chat_id = (callback.get("message") or {}).get("chat", {}).get("id")
            cb_query_id = callback.get("id")
            if not chat_id or not cb_query_id:
                return
            await self.store.ensure_prefs(chat_id)
            data = callback["data"]
            if data.startswith("save:"):
                await self._save(chat_id, data.split(":", 1)[1])
                await self.notifier.answer_callback(cb_query_id, "💾 Uložené ✅")
            else:
                await self.notifier.answer_callback(cb_query_id, "Neznáma akcia")
            return

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
            case "/filter":
                return await self._filter(chat_id, arg)
            case "/eval":
                return await self._eval(chat_id, arg)
            case "/show":
                return await self._show(chat_id, arg)
            case "/models":
                return await self._models(chat_id)
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
        try:
            await self._queue_rehodnotenie(chat_id)
        except Exception:
            logger.warning("rehodnotenie queue failed", exc_info=True)
        return f"✅ Kritériá uložené:\n<i>{_em(text)}</i>"

    async def _filter(self, chat_id: int, arg: str) -> str:
        """Manage Bazoš scrape filters (/filter query|price|km|psc|clear)."""
        if not arg.strip():
            return await self._filters_status(chat_id)
        parts = arg.split(maxsplit=1)
        key = parts[0].lower()
        value = parts[1].strip() if len(parts) > 1 else ""

        if key == "clear":
            await self.store.update_prefs(chat_id, filters={})
            return "✅ Filtre vynulované (scrapuje sa podľa config/scraper.yaml)."
        if not value:
            return "/filter query skoda | /filter price 1500-4000 | /filter dist 100 | /filter psc 81101 | /filter clear"

        patch: dict = {}
        if key == "query":
            patch["query"] = value
        elif key == "price":
            match = re.fullmatch(r"(\d*)\s*-\s*(\d*)", value)
            if not match or (not match.group(1) and not match.group(2)):
                return "Formát: `/filter price 1500-4000` (alebo `1500-` / `-4000`)."
            patch["min_price"] = int(match.group(1)) if match.group(1) else None
            patch["max_price"] = int(match.group(2)) if match.group(2) else None
        elif key == "dist":
            if not value.isdigit():
                return "Formát: `/filter dist 100` — okruh okolo PSČ v km."
            patch["distance_km"] = int(value)
        elif key == "psc":
            patch["psc"] = value
        else:
            return "Neznámy filter. Použi query / price / dist / psc / clear."

        filters = await self.store.update_filters(chat_id, patch)
        await _dispatch_scrape()
        return f"✅ Uložené.\n{self._filters_summary(filters)}"

    @staticmethod
    def _filters_summary(filters: dict) -> str:
        if not filters:
            return "Filtre: žiadne (default z config/scraper.yaml)"
        bits = []
        if filters.get("query"):
            bits.append(f"hľadať: „{_em(str(filters['query']))}“")
        lo, hi = filters.get("min_price"), filters.get("max_price")
        if lo is not None or hi is not None:
            bits.append(f"cena: {lo or 0}–{hi or '∞'} €")
        if filters.get("distance_km") is not None:
            bits.append(f"okruh {filters['distance_km']} km od PSČ")
        if filters.get("psc"):
            bits.append(f"PSČ {_em(str(filters['psc']))}")
        return "🔎 Filtre: " + " | ".join(bits)

    async def _filters_status(self, chat_id: int) -> str:
        prefs = await self.store.get_prefs(chat_id)
        return self._filters_summary(prefs.filters if prefs else {})

    async def _search(self, chat_id: int, arg: str) -> str:
        if not arg:
            return "/search meno: kriteriá — napr. `/search diaľnica: diesel do 200t km`"
        name_part, sep, criteria = arg.partition(":")
        name = name_part.strip()
        criteria = criteria.strip()
        if not name or not criteria:
            return "Potrebujem `meno: kritériá` — napr. `/search diaľnica: diesel do 200t km`."
        prefs = await self.store.get_prefs(chat_id)
        filters = prefs.filters if prefs else {}
        await self.store.save_search(chat_id, name, criteria, filters=filters)
        return f"✅ Uložené hľadanie „{_em(name)}“: <i>{_em(criteria)}</i>"

    async def _searches(self, chat_id: int) -> str:
        searches = await self.store.list_searches(chat_id)
        if not searches:
            return "Nemáš uložené hľadania. `/search meno: diesel do 200 km`."
        lines = ["Uložené hľadania:"] + [
            f"• <b>{_em(s.name)}</b>: {_em(s.criteria)}"
            + (f" (filtre: {self._filters_summary(s.filters)})" if s.filters else "")
            for s in searches
        ]
        return "\n".join(lines)

    async def _use(self, chat_id: int, name: str) -> str:
        if not name:
            return "/use meno"
        searches = await self.store.list_searches(chat_id)
        for s in searches:
            if s.name.lower() == name.lower():
                await self.store.update_prefs(
                    chat_id, criteria=s.criteria, filters=s.filters or {}
                )
                reply = f"✅ Aktivované „{_em(s.name)}“: <i>{_em(s.criteria)}</i>"
                if s.filters:
                    reply += "\n" + self._filters_summary(s.filters)
                try:
                    await self._queue_rehodnotenie(chat_id)
                except Exception:
                    logger.warning("rehodnotenie queue failed", exc_info=True)
                return reply
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
        await _dispatch_scrape()
        return f"✅ Odteraz ukazujem inzeráty od <b>{value}/100</b>. Pre prehľad pošli /show."

    async def _photos(self, chat_id: int, arg: str) -> str:
        arg = arg.strip().lower()
        if arg not in {"on", "off"}:
            return "/photos on|off"
        on = arg == "on"
        await self.store.update_prefs(chat_id, show_photo=on)
        return "✅ Fotky pri inzerátoch: " + ("zapnuté" if on else "vypnuté")

    async def _queue_rehodnotenie(self, chat_id: int, max_listings: int = 100) -> bool:
        """Queue an eval backfill for the active profile and trigger a scrape.

        Skip-safe: scoring runs only for ads whose (criteria, model) evaluation
        does not exist yet — cached profiles cost nothing.
        """
        settings = get_settings()
        if not settings.database_url:
            return False
        prefs = await self.store.get_prefs(chat_id)
        if prefs is None:
            return False
        model = prefs.model or load_llm_config().model
        p_key = profile_key(prefs.criteria, model)
        repo = ListingRepository(settings.database_url)
        await repo.connect()
        try:
            pending = await repo.pending_eval_tasks()
            if any(t["profile_key"] == p_key for t in pending):
                return False  # already queued for this exact profile
            await repo.create_eval_task(p_key, prefs.criteria, model, max_listings)
        finally:
            await repo.close()
        await _dispatch_scrape()
        return True

    async def _eval(self, chat_id: int, arg: str) -> str:
        try:
            limit = max(1, min(int(arg or 100), 200))
        except ValueError:
            limit = 100
        created = await self._queue_rehodnotenie(chat_id, max_listings=limit)
        if created:
            return (
                "🔁 Zaraďujem prehodnotenie áut z posledných dní podľa aktívneho "
                "searchu. Vyhodnotené (rovnaký search + model) sa preskočia — "
                "hotové bude o pár minút, potom skús /show."
            )
        return "⏳ Toto vyhľadanie už prehodnocujem — pošli /show o pár minút."

    async def _show(self, chat_id: int, arg: str) -> str:
        """List recently seen listings matching the active search profile.

        Sends the top matches as photo notifications (same look as deal
        alerts). Uses the active profile's stored evaluations and the current
        ``/score`` threshold.
        """
        try:
            limit = max(1, min(int(arg or 5), 10))
        except ValueError:
            limit = 5
        prefs = await self.store.get_prefs(chat_id)
        if prefs is None:
            return "Žiadne nastavenia. /help"
        model = prefs.model or (load_llm_config().model)
        p_key = profile_key(prefs.criteria, model)
        min_score = prefs.min_score or 0

        from bazcar.db import ListingRepository

        repo = ListingRepository(get_settings().database_url)
        await repo.connect()
        try:
            rows = await repo.fetch_recent_matches(p_key, min_score, days=3, limit=limit)
        finally:
            await repo.close()
        if not rows:
            return (
                f"Nič vyhovujúce (skóre >= {min_score}) za posledné 3 dni.\n"
                "Zmeň /criteria alebo /score a počkaj na ďalší scrape."
            )
        listings = [_row_to_listing(row) for row in rows]
        async with TelegramNotifier(
            get_settings().telegram_bot_token, get_settings().telegram_chat_id
        ) as notifier:
            await notifier.send_listings_with_photos(listings)
        return f"📤 Poslal som {len(listings)} najlepších (skóre >= {min_score}):"

    async def _models(self, chat_id: int) -> str:
        from bazcar.llm.provider import list_models

        free, paid = await list_models()

        def _lines(models: list[str], cap: int) -> list[str]:
            shown = [f"  • <code>{_em(m)}</code>" for m in models[:cap]]
            if len(models) > cap:
                shown.append(f"  • … +{len(models) - cap} ďalších (openrouter.ai/models)")
            return shown

        return (
            "<b>🤖 Dostupné modely</b>\n\n"
            "<b>🆓 Free</b>\n" + "\n".join(_lines(free, 30) or ["  • —"]) + "\n\n"
            "<b>💰 Paid</b>\n" + "\n".join(_lines(paid, 20))
            + "\n\nNastaviť: <code>/model názov-modelu</code>"
        )

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
            f"• <a href=\"{_em_url(item.ad_url)}\">{_em(item.ad_title or f'ad {item.ad_id}')}</a>"
            if item.ad_url
            else f"• {_em(item.ad_title or f'ad {item.ad_id}')}"
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
            f"Fotky: {'áno' if prefs.show_photo else 'nie'}\n"
            f"{self._filters_summary(prefs.filters)}"
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


async def poll(
    token: str,
    store: UserStore,
    notifier,
    export_dir: Path,
    *,
    offset: int | None = None,
    timeout: int = 5,
) -> tuple[int, int | None]:
    """Fetch one batch of updates and process them.

    Returns ``(handled, next_offset)``. ``next_offset`` must be passed back on
    the next call so Telegram confirms the processed updates (getUpdates
    without a rising offset re-delivers the same updates forever).
    """
    bot = Bot(store=store, notifier=notifier, export_dir=export_dir)
    payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        payload["offset"] = offset
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{TELEGRAM_API}/bot{token}/getUpdates", json=payload)
        resp.raise_for_status()
        updates = resp.json().get("result", [])
    next_offset = offset
    for update in updates:
        try:
            await bot.handle(update)
        except Exception:
            logger.exception("update %s failed", update.get("update_id"))
        next_offset = update["update_id"] + 1
    return len(updates), next_offset


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
                handled, nxt = await poll(
                    settings.telegram_bot_token,
                    store,
                    notifier,
                    settings.export_dir,
                    timeout=1,
                )
                # Confirm the processed updates with a follow-up call so they
                # are not re-delivered on the next run.
                if nxt is not None:
                    await poll(
                        settings.telegram_bot_token,
                        store,
                        notifier,
                        settings.export_dir,
                        offset=nxt,
                        timeout=0,
                    )
                logger.info("polled once, handled %d updates", handled)
                return
            logger.info("bot polling every ~1s...")
            offset: int | None = None
            while True:
                try:
                    _, offset = await poll(
                        settings.telegram_bot_token,
                        store,
                        notifier,
                        settings.export_dir,
                        offset=offset,
                    )
                except httpx.HTTPError as exc:
                    logger.warning("poll error: %s", exc)
                await asyncio.sleep(1.0)
