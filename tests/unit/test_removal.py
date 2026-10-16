"""Sold/unlisted detection: ad-page parsing, live verification, the sweeper, diagnosis."""

from __future__ import annotations

import asyncio

import httpx
import respx

from bazcar.core.adpage import diagnose_missing, fetch_ad_page, parse_ad_meta

LIVE_HTML = (
    '<html><head><title>Nissan Note - Lučenec | Bazoš.sk</title>'
    '<meta name="description" content="Inzerát č. 196539114: Nissan Note E12 1.2 59 kW Visia, '
    'Cena: 2 250 €, Lokalita: Lučenec. Popis: Predám tento aktuálne lacný Nissan."></head></html>'
)
CATEGORY_HTML = "<html><head><title>Volkswagen Passat | Bazoš.sk</title></head><body>list</body></html>"


def test_parse_live_ad_extracts_title_price_city() -> None:
    meta = parse_ad_meta(LIVE_HTML, 196539114)
    assert meta["title"] == "Nissan Note E12 1.2 59 kW Visia"
    assert meta["price"] == 2250
    assert meta["price_raw"] == "2 250 €"
    assert meta["city"] == "Lučenec"


def test_category_page_means_gone() -> None:
    meta = parse_ad_meta(CATEGORY_HTML, 196539114)
    assert meta["gone"] is True and meta["state"] == "gone"


def test_wrong_id_in_meta_means_gone() -> None:
    # a different ad id in the page is not our ad
    assert parse_ad_meta(LIVE_HTML, 111111111)["state"] == "gone"


def test_bot_check_page_is_blocked_not_gone() -> None:
    # No ad id and a bot-check marker: must NOT be treated as removed.
    html = "<html><body><title>Just a moment...</title><div id='cf-browser-verification'></div></body></html>"
    meta = parse_ad_meta(html, 196539114)
    assert meta["state"] == "blocked" and "gone" not in meta


def test_id_substring_does_not_count_as_mentioned() -> None:
    # ad 123 must not be considered present just because 1234 appears
    html = "<html><body>ad 12345678 unrelated</body></html>"
    assert parse_ad_meta(html, 123)["state"] == "gone"


def test_live_page_with_changed_markup_is_not_treated_as_gone() -> None:
    # meta regex no longer matches, but the ad id is still on the page -> alive
    html = "<html><body><div data-ad='196539114'>new markup</div></body></html>"
    meta = parse_ad_meta(html, 196539114)
    assert meta.get("unparsed") is True and "gone" not in meta


def test_ad_page_price_range_takes_lower_bound() -> None:
    html = LIVE_HTML.replace("Cena: 2 250 €", "Cena: 1 500 – 2 000 €")
    assert parse_ad_meta(html, 196539114)["price"] == 1500


def test_unreadable_price_is_none_not_error() -> None:
    html = LIVE_HTML.replace("Cena: 2 250 €", "Cena: Dohodou")
    meta = parse_ad_meta(html, 196539114)
    assert meta["price"] is None and meta["price_raw"] == "Dohodou"


@respx.mock
def test_fetch_live_ad() -> None:
    respx.get("https://auto.bazos.sk/inzerat/196539114/").mock(
        return_value=httpx.Response(200, text=LIVE_HTML)
    )
    state = asyncio.run(fetch_ad_page(196539114))
    assert state["price"] == 2250


@respx.mock
def test_fetch_removed_ad_redirect_is_gone() -> None:
    respx.get("https://auto.bazos.sk/inzerat/196539114/").mock(
        return_value=httpx.Response(200, text=CATEGORY_HTML)
    )
    assert asyncio.run(fetch_ad_page(196539114))["state"] == "gone"


@respx.mock
def test_fetch_error_is_unreachable_not_removed() -> None:
    respx.get("https://auto.bazos.sk/inzerat/196539114/").mock(side_effect=httpx.ConnectError("x"))
    assert asyncio.run(fetch_ad_page(196539114)) is None


@respx.mock
def test_fetch_404_is_gone() -> None:
    respx.get("https://auto.bazos.sk/inzerat/196539114/").mock(return_value=httpx.Response(404))
    assert asyncio.run(fetch_ad_page(196539114))["state"] == "gone"


@respx.mock
def test_fetch_403_and_429_are_unreachable_not_gone() -> None:
    for code in (403, 429, 503):
        respx.get("https://auto.bazos.sk/inzerat/196539114/").mock(
            return_value=httpx.Response(code)
        )
        assert asyncio.run(fetch_ad_page(196539114)) is None


# ----------------------------------------------------------------- diagnosis


def test_diagnose_excluded_by_price_bound() -> None:
    out = diagnose_missing({"title": "Skoda", "price": 5000}, {"max_price": 3000})
    assert out["verdict"] == "excluded"
    assert "above your maximum" in out["reason"]


def test_diagnose_excluded_by_query() -> None:
    out = diagnose_missing({"title": "Skoda Octavia", "price": 5000}, {"query": "bmw"})
    assert out["verdict"] == "excluded" and out["checks"][0]["key"] == "query"


def test_diagnose_in_scope_unseen() -> None:
    out = diagnose_missing({"title": "Skoda", "price": 5000}, {})
    assert out["verdict"] == "in_scope_unseen"


def test_diagnose_uncertain_without_price() -> None:
    out = diagnose_missing({"title": "x", "price": None}, {"min_price": 1000})
    assert out["verdict"] == "uncertain"


# ------------------------------------------------------------------ sweeper


class _SweepRepo:
    def __init__(self, candidates):
        self.candidates = candidates
        self.removed: list[int] = []
        self.verified: list[int] = []

    async def connect(self):
        pass

    async def close(self):
        pass

    async def removal_candidates(self, limit):
        return self.candidates[:limit]

    async def mark_listing_removed(self, ad_id):
        self.removed.append(ad_id)

    async def mark_listing_verified(self, ad_id):
        self.verified.append(ad_id)


class _Settings:
    database_url = "postgres://fake"


@respx.mock
def test_sweeper_removes_only_confirmed_gone_ads(monkeypatch) -> None:
    from bazcar.pipeline import runner

    monkeypatch.setattr(runner, "REMOVAL_CHECK_DELAY_S", 0)
    monkeypatch.setattr(runner, "get_settings", lambda: _Settings())
    repo = _SweepRepo([
        {"ad_id": 1, "title": "alive"},
        {"ad_id": 2, "title": "sold"},
        {"ad_id": 3, "title": "network"},
    ])
    monkeypatch.setattr("bazcar.db.ListingRepository", lambda dsn: repo)
    respx.get("https://auto.bazos.sk/inzerat/1/").mock(return_value=httpx.Response(200, text=LIVE_HTML.replace("196539114", "1")))
    respx.get("https://auto.bazos.sk/inzerat/2/").mock(return_value=httpx.Response(200, text=CATEGORY_HTML))
    respx.get("https://auto.bazos.sk/inzerat/3/").mock(side_effect=httpx.ConnectError("x"))

    stats = asyncio.run(runner._sweep_removed(limit=10))
    assert repo.removed == [2]          # the sold one (category page)
    assert repo.verified == [1]         # live page for its own id
    assert stats == {"checked": 3, "removed": 1, "alive": 1, "unreachable": 1}


@respx.mock
def test_sweeper_marks_live_ads_verified(monkeypatch) -> None:
    from bazcar.pipeline import runner

    monkeypatch.setattr(runner, "REMOVAL_CHECK_DELAY_S", 0)
    monkeypatch.setattr(runner, "get_settings", lambda: _Settings())
    repo = _SweepRepo([{"ad_id": 196539114, "title": "alive"}])
    monkeypatch.setattr("bazcar.db.ListingRepository", lambda dsn: repo)
    respx.get("https://auto.bazos.sk/inzerat/196539114/").mock(return_value=httpx.Response(200, text=LIVE_HTML))
    stats = asyncio.run(runner._sweep_removed(limit=5))
    assert repo.verified == [196539114] and repo.removed == []
    assert stats["alive"] == 1


@respx.mock
def test_sweeper_treats_bot_check_as_unreachable(monkeypatch) -> None:
    from bazcar.pipeline import runner

    monkeypatch.setattr(runner, "REMOVAL_CHECK_DELAY_S", 0)
    monkeypatch.setattr(runner, "get_settings", lambda: _Settings())
    repo = _SweepRepo([{"ad_id": 4, "title": "blocked"}])
    monkeypatch.setattr("bazcar.db.ListingRepository", lambda dsn: repo)
    blocked = "<html><title>Just a moment...</title><div id='cf-browser-verification'></div></html>"
    respx.get("https://auto.bazos.sk/inzerat/4/").mock(return_value=httpx.Response(200, text=blocked))
    stats = asyncio.run(runner._sweep_removed(limit=5))
    assert repo.removed == [] and repo.verified == []
    assert stats["unreachable"] == 1


def test_sweeper_is_a_noop_without_database(monkeypatch) -> None:
    from bazcar.pipeline import runner

    class _NoDb:
        database_url = None

    monkeypatch.setattr(runner, "get_settings", lambda: _NoDb())
    assert asyncio.run(runner._sweep_removed()) == {"checked": 0, "removed": 0, "alive": 0, "unreachable": 0}


def test_candidates_query_skips_removed_and_recently_verified() -> None:
    from bazcar.db import ListingRepository

    seen: dict = {}

    class _Conn:
        async def fetch(self, sql, *args):
            seen["sql"] = sql
            return []

    repo = ListingRepository("postgres://fake")
    repo._conn = _Conn()
    asyncio.run(repo.removal_candidates(5))
    assert "removed_at IS NULL" in seen["sql"]
    assert "verified_at IS NULL OR verified_at <" in seen["sql"]
    assert "ORDER BY verified_at NULLS FIRST" in seen["sql"]
