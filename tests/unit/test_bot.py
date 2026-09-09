"""Unit tests for car classification + bot command handling (Phase 4)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from bazcar.bot.classify import filter_cars, is_car_listing
from bazcar.bot.daemon import Bot
from bazcar.bot.store import SavedListing, SearchProfile, UserPrefs
from bazcar.core.models import DealEvaluation, Listing


def _listing(ad_id: int, title: str, **over) -> Listing:
    base = dict(
        ad_id=ad_id,
        url=f"https://auto.bazos.sk/inzerat/{ad_id}/slug.php",
        title=title,
        description_preview=over.pop("description_preview", "opis"),
    )
    base.update(over)
    return Listing(**base)


# --------------------------------------------------------------- classification


def test_is_car_detects_parts_keyword() -> None:
    listing = _listing(1, "Svetl Octavia zadne")
    assert is_car_listing(listing) is False


def test_is_car_accepts_whole_car() -> None:
    listing = _listing(1, "Kia Sportage 1.6 2022")
    assert is_car_listing(listing) is True


def test_is_car_respects_llm_verdict() -> None:
    listing = _listing(1, "Komplet disky na Audi")
    listing.evaluation = DealEvaluation(score=90, why="ok", is_car=True)
    assert is_car_listing(listing) is True  # LLM says car → keep


def test_is_car_ignores_part_words_in_description() -> None:
    # A real car whose description mentions tyres/wheels must NOT be dropped.
    listing = _listing(
        1, "Volkswagen Golf 5 GT 125kW", description_preview="nové pneu, alu disky, LED svetlá"
    )
    assert is_car_listing(listing) is True


def test_filter_cars_keeps_order_and_drops_parts() -> None:
    good = _listing(1, "VW Golf 1.9 TDI")
    part = _listing(2, "Pneumatiky zimné 4ks")
    filtered = filter_cars([good, part])
    assert [ad.ad_id for ad in filtered] == [1]


# ----------------------------------------------------------------- bot commands


class _FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.answered: list[tuple[str, str]] = []

    async def send_message(self, text: str, *, chat_id: int | None = None, silent: bool = False) -> bool:
        self.sent.append((chat_id or 0, text))
        return True

    async def answer_callback(self, callback_query_id: str, text: str | None = None) -> bool:
        self.answered.append((callback_query_id, text or ""))
        return True


class _FakeStore:
    def __init__(self) -> None:
        self.prefs: dict[int, UserPrefs] = {}
        self.searches: dict[int, list[SearchProfile]] = {}
        self.saved: dict[int, list[SavedListing]] = {}

    async def ensure_prefs(self, chat_id: int) -> UserPrefs:
        return self.prefs.setdefault(chat_id, UserPrefs(chat_id=chat_id))

    async def get_prefs(self, chat_id: int) -> UserPrefs | None:
        return self.prefs.get(chat_id)

    async def update_prefs(self, chat_id: int, **fields) -> None:
        prefs = await self.ensure_prefs(chat_id)
        for key, value in fields.items():
            setattr(prefs, key, value)

    async def update_filters(self, chat_id: int, patch: dict) -> dict:
        prefs = await self.ensure_prefs(chat_id)
        filters = dict(prefs.filters)
        for key, value in patch.items():
            if value in (None, ""):
                filters.pop(key, None)
            else:
                filters[key] = value
        prefs.filters = filters
        return filters

    async def list_searches(self, chat_id: int) -> list[SearchProfile]:
        return self.searches.get(chat_id, [])

    async def save_search(self, chat_id: int, name: str, criteria: str, filters: dict | None = None) -> None:
        searches = self.searches.setdefault(chat_id, [])
        searches = [s for s in searches if s.name != name]
        searches.append(SearchProfile(name=name, criteria=criteria, filters=filters or {}))
        self.searches[chat_id] = searches

    async def delete_search(self, chat_id: int, name: str) -> bool:
        return False

    async def save_listing(self, chat_id: int, ad_id: int, title: str | None, url: str | None) -> None:
        self.saved.setdefault(chat_id, []).append(SavedListing(ad_id=ad_id, ad_title=title, ad_url=url))

    async def list_saved(self, chat_id: int) -> list[SavedListing]:
        return self.saved.get(chat_id, [])

    async def remove_saved(self, chat_id: int, ad_id: int) -> bool:
        saved = self.saved.get(chat_id, [])
        before = len(saved)
        self.saved[chat_id] = [s for s in saved if s.ad_id != ad_id]
        return len(self.saved[chat_id]) < before


def _run(handler, *args) -> None:
    async def _go():
        await handler(*args)

    asyncio.run(_go())


def _bot() -> Bot:
    return Bot(store=_FakeStore(), notifier=_FakeNotifier(), export_dir=Path("data/exports"))


def test_criteria_command_sets_prefs():
    bot = _bot()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/criteria diesel do 200t km"}})
    prefs = asyncio.run(bot.store.get_prefs(42))
    assert prefs.criteria == "diesel do 200t km"


def test_plain_text_sets_criteria():
    bot = _bot()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "elektromobil do 30t"}})
    prefs = asyncio.run(bot.store.get_prefs(42))
    assert prefs.criteria == "elektromobil do 30t"


def test_score_and_photos_commands():
    bot = _bot()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/score 75"}})
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/photos on"}})
    prefs = asyncio.run(bot.store.get_prefs(42))
    assert prefs.min_score == 75
    assert prefs.show_photo is True


def test_save_and_saved_commands():
    bot = _bot()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/save 123456"}})
    saved = asyncio.run(bot.store.list_saved(42))
    assert [s.ad_id for s in saved] == [123456]


def test_inline_save_button_stores_ad_and_answers():
    bot = _bot()
    _run(
        bot.handle,
        {
            "callback_query": {
                "id": "cb-1",
                "message": {"chat": {"id": 42}, "message_id": 7},
                "data": "save:987654",
            }
        },
    )
    saved = asyncio.run(bot.store.list_saved(42))
    assert [s.ad_id for s in saved] == [987654]
    assert bot.notifier.answered == [("cb-1", "💾 Uložené ✅")]
    assert bot.notifier.sent == []  # toast only, no extra messages


def test_filter_query_command():
    bot = _bot()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/filter query skoda octavia"}})
    prefs = asyncio.run(bot.store.get_prefs(42))
    assert prefs.filters["query"] == "skoda octavia"


def test_filter_price_range():
    bot = _bot()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/filter price 1500-4000"}})
    prefs = asyncio.run(bot.store.get_prefs(42))
    assert prefs.filters == {"min_price": 1500, "max_price": 4000}


def test_filter_price_one_sided_and_km():
    bot = _bot()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/filter price -4000"}})
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/filter km 200000"}})
    prefs = asyncio.run(bot.store.get_prefs(42))
    assert prefs.filters == {"max_price": 4000, "max_km": 200000}


def test_filter_clear_resets():
    bot = _bot()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/filter query skoda"}})
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/filter clear"}})
    prefs = asyncio.run(bot.store.get_prefs(42))
    assert prefs.filters == {}


def test_search_snapshots_and_use_restores_filters():
    bot = _bot()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/filter query skoda"}})
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/filter price 1500-4000"}})
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/search moje: diesel do 200t km"}})
    # change filters, then restore via /use
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/filter clear"}})
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/use moje"}})
    prefs = asyncio.run(bot.store.get_prefs(42))
    assert prefs.criteria == "diesel do 200t km"
    assert prefs.filters == {"query": "skoda", "min_price": 1500, "max_price": 4000}
