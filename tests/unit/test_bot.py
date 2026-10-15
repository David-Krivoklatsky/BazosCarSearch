"""Unit tests for car classification + bot command handling (Phase 4)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bazcar.bot.classify import filter_cars, is_car_listing
from bazcar.bot.daemon import Bot, parse_eval_args
from bazcar.bot.store import SavedListing, SearchProfile, UserPrefs
from bazcar.core.models import DealEvaluation, Listing


def test_parse_eval_args_defaults() -> None:
    assert parse_eval_args("") == (100, 3)
    assert parse_eval_args("30") == (30, 3)
    assert parse_eval_args("days 7") == (100, 7)
    assert parse_eval_args("50 days 7") == (50, 7)
    assert parse_eval_args("dni 7") == (100, 7)  # slovak alias
    assert parse_eval_args("all") == (500, 3650)
    assert parse_eval_args("vsetky") == (500, 3650)
    assert parse_eval_args("9999") == (500, 3)  # capped


def test_parse_show_args() -> None:
    from bazcar.bot.daemon import parse_show_args

    assert parse_show_args("") == (None, 3)          # use prefs default
    assert parse_show_args("10") == (10, 3)
    assert parse_show_args("days 7") == (None, 7)
    assert parse_show_args("20 days 7") == (20, 7)
    assert parse_show_args("dni 7") == (None, 7)     # slovak alias
    assert parse_show_args("9999") == (20, 3)        # capped at 20


@pytest.fixture(autouse=True)
def _no_real_db(monkeypatch) -> None:
    """Unit tests must never touch the real database (eval task queue)."""
    async def _noop(self, chat_id: int, max_listings: int = 100, days: int = 3) -> bool:
        return True

    monkeypatch.setattr(Bot, "_queue_rehodnotenie", _noop)


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
        self.edited: list[tuple[int, int, str]] = []
        self.docs: list[tuple[int, str]] = []
        self.markups: list[dict | None] = []

    async def send_message(
        self,
        text: str,
        *,
        chat_id: int | None = None,
        silent: bool = False,
        reply_markup: dict | None = None,
    ) -> bool:
        self.sent.append((chat_id or 0, text))
        self.markups.append(reply_markup)
        return True

    async def answer_callback(self, callback_query_id: str, text: str | None = None) -> bool:
        self.answered.append((callback_query_id, text or ""))
        return True

    async def edit_message_text(
        self,
        text: str,
        message_id: int,
        *,
        chat_id: int | None = None,
        reply_markup: dict | None = None,
    ) -> bool:
        self.edited.append((chat_id or 0, message_id, text))
        return True

    async def send_photo(
        self,
        photo_url: str,
        caption: str = "",
        *,
        chat_id: int | None = None,
        silent: bool = False,
        reply_markup: dict | None = None,
    ) -> bool:
        self.sent.append((chat_id or 0, caption))
        return True

    async def send_document(
        self,
        filename: str,
        content: bytes,
        *,
        chat_id: int | None = None,
        caption: str = "",
    ) -> bool:
        self.docs.append((chat_id or 0, filename))
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

    async def save_search(
        self,
        chat_id: int,
        name: str,
        criteria: str,
        filters: dict | None = None,
        min_score: int | None = None,
    ) -> None:
        searches = self.searches.setdefault(chat_id, [])
        searches = [s for s in searches if s.name != name]
        searches.append(
            SearchProfile(name=name, criteria=criteria, filters=filters or {}, min_score=min_score)
        )
        self.searches[chat_id] = searches

    async def delete_search(self, chat_id: int, name: str) -> bool:
        return False

    async def save_listing(self, chat_id: int, ad_id: int, title: str | None, url: str | None) -> None:
        self.saved.setdefault(chat_id, []).append(SavedListing(ad_id=ad_id, ad_title=title, ad_url=url))

    async def set_saved_note(self, chat_id: int, ad_id: int, note: str) -> bool:
        for s in self.saved.get(chat_id, []):
            if s.ad_id == ad_id:
                s.note = note
                return True
        return False

    async def set_search_notify(self, chat_id: int, name: str, enabled: bool) -> bool:
        for s in self.searches.get(chat_id, []):
            if s.name == name:
                s.notify_enabled = enabled
                return True
        return False

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


def test_filter_price_one_sided_and_distance():
    bot = _bot()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/filter price -4000"}})
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/filter dist 100"}})
    prefs = asyncio.run(bot.store.get_prefs(42))
    assert prefs.filters == {"max_price": 4000, "distance_km": 100}


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


# ------------------------------------------------------- new: catalog & views


def test_parse_cars_args():
    from bazcar.bot.daemon import parse_cars_args

    assert parse_cars_args("") == ("all", "newest", 7)
    assert parse_cars_args("unevaluated") == ("unevaluated", "newest", 7)
    assert parse_cars_args("neohodnotené") == ("unevaluated", "newest", 7)
    assert parse_cars_args("rated") == ("rated", "newest", 7)
    assert parse_cars_args("cheap") == ("all", "price_asc", 7)
    assert parse_cars_args("expensive days 30") == ("all", "price_desc", 30)
    assert parse_cars_args("score dni 14") == ("all", "score", 14)
    assert parse_cars_args("90") == ("all", "newest", 90)


def test_cars_callback_roundtrip():
    from bazcar.bot.daemon import cars_callback_encode, parse_cars_callback

    for status in ("all", "unevaluated", "rated"):
        for sort in ("newest", "price_asc", "price_desc", "score"):
            data = cars_callback_encode(status, sort, 30, 16)
            assert len(data) <= 64  # Telegram callback_data limit
            assert parse_cars_callback(data) == (status, sort, 30, 16)
    assert parse_cars_callback("garbage") is None


def test_sparkline_and_bar():
    from bazcar.bot.daemon import bar, sparkline

    assert sparkline([]) == ""
    assert sparkline([0, 0, 0]) == "▁▁▁"
    assert sparkline([1, 5, 10])[-1] == "█"
    assert len(sparkline([3, 7, 2, 9])) == 4
    assert bar(0, 10) == "░" * 10
    assert bar(10, 10) == "▓" * 10
    assert len(bar(5, 10)) == 10
    assert bar(1, 0) == "░" * 10


def test_extract_make():
    from bazcar.bot.daemon import extract_make

    assert extract_make("Škoda Octavia 1.6 TDI") == "Škoda"
    assert extract_make("BMW 320d touring") == "BMW"
    assert extract_make("VW Golf 5") == "VW"
    assert extract_make("!!!") is None
    assert extract_make("") is None


def test_time_ago():
    from datetime import UTC, datetime, timedelta

    from bazcar.bot.daemon import time_ago

    assert time_ago(None) == "nikdy"
    assert time_ago(datetime.now(UTC)) == "práve teraz"
    assert time_ago(datetime.now(UTC) - timedelta(minutes=12)) == "pred 12 min"
    assert time_ago(datetime.now(UTC) - timedelta(hours=3)) == "pred 3 h"
    assert time_ago(datetime.now(UTC) - timedelta(days=2)) == "pred 2 d"


def test_build_cars_page_badges():
    from bazcar.bot.daemon import build_cars_page

    rows = [
        {"ad_id": 1, "title": "Škoda Octavia", "url": "https://x/1", "price_eur": 5000,
         "year": 2018, "mileage_km": 150000, "city": "BA", "score": None},
        {"ad_id": 2, "title": "VW Golf", "url": "https://x/2", "price_eur": 7000,
         "year": 2019, "mileage_km": 90000, "city": "KE", "score": 82, "is_car": True},
        {"ad_id": 3, "title": "Svetlá Octavia", "url": "https://x/3", "price_eur": 100,
         "year": None, "mileage_km": None, "city": None, "score": 12, "is_car": False},
    ]
    text = build_cars_page(rows, total=3, offset=0, days=7, status="all", sort="newest")
    assert "⏳" in text  # unevaluated badge
    assert "⭐82" in text
    assert "🛠" in text  # part badge
    assert "1–3 z 3" in text
    assert "/car &lt;id&gt;" in text


def test_build_car_card_sections():
    from bazcar.bot.daemon import build_car_card

    row = {
        "ad_id": 9, "title": "Škoda Octavia 2.0 TDI", "url": "https://x/9",
        "price_eur": 8000, "year": 2019, "mileage_km": 160000, "city": "Nitra",
        "views": 320, "published_date": "2026-10-01",
        "score": 85, "why": "Dobrá cena", "risk": "turbo", "condition": "zachovalé",
        "target_offer": 7300, "eval_model": "m/1",
    }
    history = [
        {"price_eur": 9000, "seen_at": "2026-09-01"},
        {"price_eur": 8000, "seen_at": "2026-10-01"},
    ]
    bench = {"n": 12, "median": 9000, "p10": 7000, "p90": 11000}
    text = build_car_card(row, history, bench)
    assert "8 000 €" in text
    assert "85/100" in text
    assert "zachovalé" in text
    assert "7 300 €" in text
    assert "📉 Vývoj ceny" in text and "-11 %" in text
    assert "n=12" in text and "pod mediánom" in text
    assert "195" not in text  # no leakage of unrelated ids


def test_build_car_card_unevaluated():
    from bazcar.bot.daemon import build_car_card

    row = {
        "ad_id": 9, "title": "VW Golf", "url": "https://x/9", "price_eur": 4000,
        "year": 2015, "mileage_km": None, "city": None, "views": None,
        "published_date": None, "score": None,
    }
    text = build_car_card(row, [], None)
    assert "⏳" in text and "Zatiaľ bez AI hodnotenia" in text
    assert "Vývoj ceny" not in text  # needs >= 2 points


def test_build_stats_text_renders():
    from bazcar.bot.daemon import build_stats_text

    stats = {
        "total": 100,
        "evaluated": 40,
        "price": {"mn": 500, "mx": 20000, "avg": 6000, "median": 5500},
        "bands": {"b90": 5, "b80": 10, "b70": 15, "b50": 8, "b0": 2},
        "new_by_day": [("2026-10-09", 3), ("2026-10-10", 7)],
        "drops_7d": 4,
    }
    text = build_stats_text(stats, "abc123")
    assert "100" in text and "40 %" in text
    assert "5 500 €" in text
    assert "10-09" in text
    assert "90+" in text


def test_build_queue_text_states():
    from datetime import UTC, datetime

    from bazcar.bot.daemon import build_queue_text

    pending = [{
        "criteria": "diesel do 200t", "model": "m/1", "total": 50, "done": 10,
        "started_at": datetime.now(UTC), "created_at": datetime.now(UTC), "attempts": 1,
    }]
    finished = [{
        "criteria": "suv", "model": "m/1", "total": 20, "done": 20, "error": None,
        "done_at": datetime.now(UTC),
    }, {
        "criteria": "lactea", "model": "m/2", "total": 10, "done": 3,
        "error": "model m/2 zlyhal 2x (429/limit)", "done_at": datetime.now(UTC),
    }]
    text = build_queue_text(pending, finished)
    assert "10/50" in text
    assert "pokus 1" in text
    assert "✅" in text and "20/20" in text
    assert "⚠️" in text and "zlyhal" in text
    assert "prázdna" in build_queue_text([], [])


def test_menu_command_returns_keyboard():
    bot = _bot()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/menu"}})
    assert bot.notifier.sent
    # /menu result is a tuple (text, markup) — sent via reply with markup
    assert any("Hlavné menu" in text for _, text in bot.notifier.sent)


def test_templates_command_lists_presets():
    bot = _bot()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/templates"}})
    assert any("Rodinné" in text and "Príležitosť" in text for _, text in bot.notifier.sent)


def test_apply_template_updates_prefs():
    bot = _bot()
    _run(
        bot.handle,
        {
            "callback_query": {
                "id": "cb-t",
                "message": {"chat": {"id": 42}, "message_id": 5},
                "data": "tpl:mesto",
            }
        },
    )
    prefs = asyncio.run(bot.store.get_prefs(42))
    assert "mesta" in prefs.criteria
    assert prefs.filters == {"min_price": 1000, "max_price": 5000}
    assert prefs.min_score == 60
    assert bot.notifier.answered == [("cb-t", "🏙️ Aktivované!")]


class _NoDbSettings:
    """Settings stub: no DB, no tokens — commands must degrade gracefully."""

    database_url = None
    telegram_bot_token = None
    telegram_chat_id = None
    webapp_url = None

    def __init__(self, allowed: set[int] | None = None) -> None:
        self._allowed = allowed

    def allowed_chats(self) -> set[int] | None:
        return self._allowed


def test_cars_without_db_degrades(monkeypatch):
    monkeypatch.setattr(
        "bazcar.bot.daemon.get_settings", lambda: _NoDbSettings()
    )
    bot = _bot()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/cars"}})
    assert any("databázu" in text for _, text in bot.notifier.sent)


def test_allowlist_blocks_strangers(monkeypatch):
    monkeypatch.setattr(
        "bazcar.bot.daemon.get_settings", lambda: _NoDbSettings(allowed={42})
    )
    bot = _bot()
    _run(bot.handle, {"message": {"chat": {"id": 99}, "text": "/start"}})
    assert bot.notifier.sent == []  # silent ignore
    assert asyncio.run(bot.store.get_prefs(99)) is None
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/start"}})
    assert bot.notifier.sent  # allowed chat gets through


def test_allowlist_blocks_stranger_callbacks(monkeypatch):
    monkeypatch.setattr(
        "bazcar.bot.daemon.get_settings", lambda: _NoDbSettings(allowed={42})
    )
    bot = _bot()
    _run(
        bot.handle,
        {
            "callback_query": {
                "id": "cb-x",
                "message": {"chat": {"id": 99}, "message_id": 1},
                "data": "save:123",
            }
        },
    )
    assert bot.notifier.answered == []
    assert asyncio.run(bot.store.list_saved(99)) == []


# ------------------------------------------------- catalog views (fake repo)


class _DbSettings(_NoDbSettings):
    """Settings stub that DOES have a database, so view commands run."""

    def __init__(self) -> None:
        self.database_url = "postgres://fake"
        self.telegram_bot_token = None
        self.telegram_chat_id = None
        self.webapp_url = None

    def allowed_chats(self) -> set[int] | None:
        return None


class _FakeRepo:
    """Minimal ListingRepository stand-in recording the calls it receives."""

    rows: list[dict] = []
    total: int = 0
    history: list[dict] = []
    bench: dict | None = None
    stats: dict = {}
    calls: list[tuple] = []

    def __init__(self, dsn: str) -> None:
        pass

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def fetch_catalog(self, profile_key, days, limit, offset=0, sort="newest", status="all"):
        type(self).calls.append(("fetch_catalog", profile_key, days, limit, offset, sort, status))
        return self.rows[offset : offset + limit]

    async def count_catalog(self, profile_key, days, status="all"):
        type(self).calls.append(("count_catalog", profile_key, days, status))
        return self.total

    async def fetch_listing_by_id(self, ad_id, profile_key):
        type(self).calls.append(("fetch_listing_by_id", ad_id))
        return self.rows[0] if self.rows and self.rows[0]["ad_id"] == ad_id else None

    async def fetch_price_history(self, ad_id, limit=10):
        return self.history

    async def price_benchmark(self, make, year):
        return self.bench

    async def stats_overview(self, profile_key):
        return self.stats

    async def latest_scrape_run(self):
        return None

    async def pending_eval_tasks(self):
        return []

    async def recent_finished_eval_tasks(self, limit=3):
        return []


@pytest.fixture()
def fake_repo(monkeypatch):
    _FakeRepo.rows = []
    _FakeRepo.total = 0
    _FakeRepo.history = []
    _FakeRepo.bench = None
    _FakeRepo.stats = {}
    _FakeRepo.calls = []
    monkeypatch.setattr("bazcar.bot.daemon.get_settings", lambda: _DbSettings())
    monkeypatch.setattr("bazcar.bot.daemon.ListingRepository", _FakeRepo)
    return _FakeRepo


def _bot_with_prefs() -> Bot:
    bot = _bot()
    bot.store.prefs[42] = UserPrefs(chat_id=42, criteria="diesel", model="m/1")
    return bot


def _row(ad_id: int) -> dict:
    return {
        "ad_id": ad_id, "title": f"Škoda Octavia {ad_id}", "url": f"https://x/{ad_id}",
        "price_eur": 5000 + ad_id, "city": "BA", "year": 2018, "mileage_km": 150000,
        "views": 10, "published_date": "2026-10-01", "score": 80, "is_car": True,
        "image_urls": [],
    }


def test_cars_lists_catalog_with_pager(fake_repo):
    fake_repo.rows = [_row(i) for i in range(1, 21)]
    fake_repo.total = 20
    bot = _bot_with_prefs()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/cars"}})
    text = bot.notifier.sent[0][1]
    assert "1–8 z 20" in text  # CARS_PAGE_SIZE = 8
    assert "⭐80" in text
    # first page: counted, then fetched with the default window/sort/status
    assert fake_repo.calls[0][0] == "count_catalog"
    assert fake_repo.calls[1] == ("fetch_catalog", fake_repo.calls[1][1], 7, 8, 0, "newest", "all")
    # only a "next" button on the first page
    buttons = bot.notifier.markups[0]["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["Ďalšie ▶️"]
    assert buttons[0]["callback_data"] == "c:a:n:7:8"


def test_cars_empty_catalog_explains(fake_repo):
    bot = _bot_with_prefs()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/cars"}})
    text = bot.notifier.sent[0][1]
    assert "📭" in text and "/cars days 30" in text


def test_cars_page_callback_edits_message(fake_repo):
    fake_repo.rows = [_row(i) for i in range(1, 21)]
    fake_repo.total = 20
    bot = _bot_with_prefs()
    _run(
        bot.handle,
        {
            "callback_query": {
                "id": "cb-page",
                "message": {"chat": {"id": 42}, "message_id": 55},
                "data": "c:a:n:7:8",
            }
        },
    )
    assert bot.notifier.edited, "pager must edit the existing message"
    _chat, message_id, text = bot.notifier.edited[0]
    assert message_id == 55
    assert "9–16 z 20" in text
    assert bot.notifier.answered == [("cb-page", "")]


def test_cars_page_callback_rejects_garbage():
    bot = _bot_with_prefs()
    _run(
        bot.handle,
        {
            "callback_query": {
                "id": "cb-bad",
                "message": {"chat": {"id": 42}, "message_id": 55},
                "data": "c:zzz:qqq",
            }
        },
    )
    assert bot.notifier.edited == []
    assert bot.notifier.answered == [("cb-bad", "Neplatná stránka")]


def test_car_detail_card_with_history_and_benchmark(fake_repo):
    fake_repo.rows = [_row(9)]
    fake_repo.total = 1
    fake_repo.history = [
        {"price_eur": 9000, "seen_at": "2026-09-01"},
        {"price_eur": 5009, "seen_at": "2026-10-01"},
    ]
    fake_repo.bench = {"n": 30, "median": 9000}
    bot = _bot_with_prefs()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/car 9"}})
    text = bot.notifier.sent[0][1]
    assert "5 009 €" in text
    assert "Vývoj ceny" in text
    assert "n=30" in text
    # offer a one-tap save + deep link back to the ad
    buttons = bot.notifier.markups[0]["inline_keyboard"][0]
    assert any(b.get("callback_data") == "save:9" for b in buttons)
    assert any(b.get("url") == "https://x/9" for b in buttons)


def test_car_detail_unknown_id(fake_repo):
    bot = _bot_with_prefs()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/car 123"}})
    assert "nemám v databáze" in bot.notifier.sent[0][1]


def test_car_bad_usage(fake_repo):
    bot = _bot_with_prefs()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/car abc"}})
    assert "Použitie:" in bot.notifier.sent[0][1]


def test_stats_command_renders(fake_repo):
    fake_repo.stats = {
        "total": 10, "evaluated": 4, "price": {"mn": 1000, "mx": 9000, "median": 5000},
        "bands": {"b90": 1, "b80": 3}, "new_by_day": [("2026-10-10", 2)], "drops_7d": 1,
    }
    bot = _bot_with_prefs()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/stats"}})
    text = bot.notifier.sent[0][1]
    assert "📊 Štatistiky katalógu" in text
    assert "5 000 €" in text
    assert "40 %" in text


def test_queue_command_renders(fake_repo):
    bot = _bot_with_prefs()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/queue"}})
    text = bot.notifier.sent[0][1]
    assert "Fronta je prázdna" in text


def test_health_command_reports_no_runs(fake_repo):
    bot = _bot_with_prefs()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/health"}})
    text = bot.notifier.sent[0][1]
    assert "Databáza: ✅" in text
    assert "žiadny zaznamenaný" in text


def test_export_without_rows(fake_repo):
    bot = _bot_with_prefs()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/export"}})
    assert "Nič na export" in bot.notifier.sent[0][1]


def test_export_sends_csv_document(fake_repo):
    fake_repo.rows = [_row(9)]
    fake_repo.total = 1
    bot = _bot_with_prefs()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/export 30"}})
    assert bot.notifier.docs, "export must send a document"
    assert bot.notifier.docs[0][1].endswith(".csv")
    assert fake_repo.calls[-1][0] == "fetch_catalog"


# ------------------------------------------------- mini app dashboard


class _DashSettings(_NoDbSettings):
    def __init__(self, url: str | None) -> None:
        self.database_url = None
        self.telegram_bot_token = None
        self.telegram_chat_id = None
        self.webapp_url = url

    def allowed_chats(self) -> set[int] | None:
        return None


def test_dashboard_without_url_explains(monkeypatch):
    monkeypatch.delenv("VERCEL_URL", raising=False)
    monkeypatch.setattr("bazcar.bot.daemon.get_settings", lambda: _DashSettings(None))
    bot = _bot()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/dashboard"}})
    assert "nie je nasadený" in bot.notifier.sent[0][1]


def test_dashboard_with_url_offers_web_app_button(monkeypatch):
    monkeypatch.setattr(
        "bazcar.bot.daemon.get_settings", lambda: _DashSettings("https://x/dash")
    )
    bot = _bot()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/dashboard"}})
    assert bot.notifier.sent  # text + markup tuple routed through reply
    button = bot.notifier.markups[0]["inline_keyboard"][0][0]
    assert button["web_app"] == {"url": "https://x/dash"}


def test_dashboard_falls_back_to_vercel_url(monkeypatch):
    monkeypatch.setenv("VERCEL_URL", "bazcar.vercel.app")
    monkeypatch.setattr("bazcar.bot.daemon.get_settings", lambda: _DashSettings(None))
    bot = _bot()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/dashboard"}})
    button = bot.notifier.markups[0]["inline_keyboard"][0][0]
    assert button["web_app"]["url"] == "https://bazcar.vercel.app/api/dashboard"


def test_menu_dashboard_button_routes_to_dashboard(monkeypatch):
    monkeypatch.setattr(
        "bazcar.bot.daemon.get_settings", lambda: _DashSettings("https://x/dash")
    )
    bot = _bot()
    _run(
        bot.handle,
        {
            "callback_query": {
                "id": "cb-d",
                "message": {"chat": {"id": 42}, "message_id": 1},
                "data": "menu:dashboard",
            }
        },
    )
    button = bot.notifier.markups[0]["inline_keyboard"][0][0]
    assert button["web_app"]["url"] == "https://x/dash"
    # the menu keyboard advertises the dashboard entry
    from bazcar.bot.daemon import MENU_KEYBOARD

    flat = [b["callback_data"] for row in MENU_KEYBOARD["inline_keyboard"] for b in row]
    assert "menu:dashboard" in flat


def test_note_command_sets_and_clears():
    bot = _bot()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/save 111"}})
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/note 111 zavolat po 18:00"}})
    saved = asyncio.run(bot.store.list_saved(42))
    assert saved[0].note == "zavolat po 18:00"
    assert "Poznámka" in bot.notifier.sent[-1][1]
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/note 111"}})
    saved = asyncio.run(bot.store.list_saved(42))
    assert saved[0].note == ""


def test_note_on_unsaved_ad_explains():
    bot = _bot()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/note 999 ahoj"}})
    assert "nemáš uložený" in bot.notifier.sent[-1][1]


def test_stale_line_flags_old_ads():
    from bazcar.bot.daemon import stale_line

    assert stale_line(None) is None
    assert stale_line("2026-10-14T00:00:00+00:00") is None  # fresh
    old = stale_line("2026-09-01T00:00:00+00:00")
    assert old is not None and "pred" in old and "nie je to však isté" in old
    assert stale_line("not-a-date") is None


def test_build_car_card_shows_stale_and_small_n_benchmark():
    from bazcar.bot.daemon import build_car_card

    row = {
        "ad_id": 5, "title": "Skoda Fabia", "url": "https://x/5",
        "price_eur": 4000, "last_seen_at": "2026-09-01T00:00:00+00:00",
    }
    text = build_car_card(row, [], {"n": 2, "median": 5000})
    assert "Naposledy videné" in text
    assert "málo dát" in text
    fresh = build_car_card(dict(row, last_seen_at="2026-10-14T00:00:00+00:00"), [], None)
    assert "Naposledy videné" not in fresh


def test_searches_lists_notify_state_with_toggle_buttons():
    bot = _bot()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/search praca: diesel do 200t km"}})
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/searches"}})
    text = bot.notifier.sent[-1][1]
    markup = bot.notifier.markups[-1]
    assert "🔔" in text and "praca" in text
    flat = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert "ntf:praca" in flat and "use:praca" in flat


def test_search_notify_toggle_callback_flips_state():
    bot = _bot()
    _run(bot.handle, {"message": {"chat": {"id": 42}, "text": "/search praca: diesel"}})
    _run(
        bot.handle,
        {
            "callback_query": {
                "id": "cb-n",
                "message": {"chat": {"id": 42}, "message_id": 3},
                "data": "ntf:praca",
            }
        },
    )
    searches = asyncio.run(bot.store.list_searches(42))
    assert searches[0].notify_enabled is False
    assert bot.notifier.answered[-1][0] == "cb-n"


def test_global_notify_toggle_callback():
    bot = _bot()
    _run(
        bot.handle,
        {
            "callback_query": {
                "id": "cb-g",
                "message": {"chat": {"id": 42}, "message_id": 4},
                "data": "ntfglobal:0",
            }
        },
    )
    prefs = asyncio.run(bot.store.get_prefs(42))
    assert prefs.notify_enabled is False
