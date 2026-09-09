"""Unit tests for the Telegram notification layer (Phase 4) — mocked API."""

from __future__ import annotations

import json

import pytest
import respx
from httpx import Response

from bazcar.core.models import DealEvaluation, Listing
from bazcar.notify.format import format_listing
from bazcar.notify.telegram import TelegramNotifier
from bazcar.pipeline.runner import _notify_targets


def _listing(**overrides) -> Listing:
    base = dict(
        ad_id=195357798,
        url="https://auto.bazos.sk/inzerat/195357798/kia-sportage.php",
        title="Kia Sportage 1.6 T-GDI Platinum 2022",
        price_eur=22500,
        city="Nitra",
        description_preview="Kia Sportage 2022, 33 650 km",
        description="Plná výbava.",
        year=2022,
        mileage_km=33650,
    )
    base.update(overrides)
    return Listing(**base)


def _listing_with_score(score: int, ad_id: int = 195357798) -> Listing:
    listing = _listing(ad_id=ad_id)
    listing.evaluation = DealEvaluation(score=score, why="Dobrá kúpa.")
    return listing


def test_format_listing_includes_key_fields() -> None:
    listing = _listing()
    listing.evaluation = DealEvaluation(score=88, why="Veľmi dobrá cena.", risk="turbo býva rizikové")
    text = format_listing(listing)
    assert "Kia Sportage" in text
    assert "22 500" in text
    assert "2022" in text
    assert "33 650 km" in text
    assert "Nitra" in text
    assert "88/100" in text
    assert "Riziko" in text
    assert "turbo" in text
    # the bare URL is hidden — it only appears inside the <a href> hyperlink
    assert ">https://auto.bazos.sk" not in text
    assert "<a href=\"https://auto.bazos.sk/inzerat/195357798/kia-sportage.php\">" in text


def test_format_listing_strips_slashes_from_title() -> None:
    listing = _listing(title="Kia Sportage /BEZ KOROZIE/ 2.0")
    text = format_listing(listing)
    assert "BEZ KOROZIE" in text
    assert "Kia Sportage /BEZ" not in text


def test_format_message_escapes_html() -> None:
    listing = _listing(title="Audi A4 <b>><br> & Co")
    text = format_listing(listing)
    assert "<b>" in text  # our own bold tag
    assert "&lt;b&gt;" in text
    assert "&amp;" in text


def test_format_message_fallback_price() -> None:
    listing = _listing(price_eur=None, price_raw=None)
    assert "Dohodou" in format_listing(listing)


def test_format_message_without_description() -> None:
    listing = _listing(description=None)
    text = format_listing(listing)
    assert "Kia Sportage" in text
    assert not text.endswith("</b>")  # title tag closed, URL last line


@respx.mock
@pytest.mark.asyncio
async def test_send_message_success() -> None:
    route = respx.post("https://api.telegram.org/botTOKEN/sendMessage").mock(
        return_value=Response(
            200,
            json={
                "ok": True,
                "result": {"message_id": 1, "chat": {"id": 42}},
            },
        )
    )
    async with TelegramNotifier("TOKEN", "42") as ntf:
        ok = await ntf.send_message("hello")
    assert ok is True
    assert route.called
    body = json.loads(route.calls[0].request.content)
    assert body["chat_id"] == "42"
    assert body["text"] == "hello"
    assert body["parse_mode"] == "HTML"
    assert body["disable_web_page_preview"] is True


@respx.mock
@pytest.mark.asyncio
async def test_send_failure_is_swallowed() -> None:
    respx.post("https://api.telegram.org/botTOKEN/sendMessage").mock(
        return_value=Response(401, json={"ok": False, "description": "Unauthorized"})
    )
    async with TelegramNotifier("TOKEN", "42") as ntf:
        ok = await ntf.send_message("hello")
    assert ok is False


@respx.mock
@pytest.mark.asyncio
async def test_send_listings_counts_successes() -> None:
    respx.post("https://api.telegram.org/botTOKEN/sendMessage").mock(
        side_effect=[
            Response(200, json={"ok": True, "result": {}}),
            Response(500, json={"ok": False}),
            Response(200, json={"ok": True, "result": {}}),
        ]
    )
    listings = [_listing(ad_id=1), _listing(ad_id=2), _listing(ad_id=3)]
    async with TelegramNotifier("TOKEN", "42") as ntf:
        sent = await ntf.send_listings(listings)
    assert sent == 2


@respx.mock
@pytest.mark.asyncio
async def test_send_listings_attaches_inline_keyboard() -> None:
    route = respx.post("https://api.telegram.org/botTOKEN/sendMessage").mock(
        return_value=Response(200, json={"ok": True, "result": {}})
    )
    listing = _listing(ad_id=195357798)
    async with TelegramNotifier("TOKEN", "42") as ntf:
        await ntf.send_listings([listing])
    body = json.loads(route.calls[0].request.content)
    markup = body["reply_markup"]
    buttons = markup["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == "save:195357798"
    assert buttons[1]["url"] == listing.url


@respx.mock
@pytest.mark.asyncio
async def test_send_listings_with_photos_uses_send_photo_and_keyboard() -> None:
    photo_route = respx.post("https://api.telegram.org/botTOKEN/sendPhoto").mock(
        return_value=Response(200, json={"ok": True, "result": {"photo": []}})
    )
    listing = _listing(ad_id=5, image_urls=["https://img/x.jpg"])
    async with TelegramNotifier("TOKEN", "42") as ntf:
        sent = await ntf.send_listings_with_photos([listing])
    assert sent == 1
    assert photo_route.called
    body = json.loads(photo_route.calls[0].request.content)
    assert body["photo"] == "https://img/x.jpg"
    assert "5" in body["caption"]
    assert body["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "save:5"


@respx.mock
@pytest.mark.asyncio
async def test_answer_callback_acknowledges() -> None:
    route = respx.post("https://api.telegram.org/botTOKEN/answerCallbackQuery").mock(
        return_value=Response(200, json={"ok": True})
    )
    async with TelegramNotifier("TOKEN", "42") as ntf:
        ok = await ntf.answer_callback("123", "💾 Uložené ✅")
    assert ok is True
    body = json.loads(route.calls[0].request.content)
    assert body["callback_query_id"] == "123"
    assert body["text"] == "💾 Uložené ✅"


def test_notify_targets_new_ids_only() -> None:
    listings = [_listing(ad_id=1), _listing(ad_id=2)]
    targets = _notify_targets(listings, inserted_ids=[1], persist_enabled=True, min_score=None)
    assert [t.ad_id for t in targets] == [1]


def test_notify_targets_all_when_no_ids() -> None:
    listings = [_listing(ad_id=1), _listing(ad_id=2)]
    targets = _notify_targets(listings, inserted_ids=[], persist_enabled=True, min_score=None)
    assert targets == []


def test_notify_targets_all_when_persistence_disabled() -> None:
    listings = [_listing(ad_id=1), _listing(ad_id=2)]
    targets = _notify_targets(listings, inserted_ids=[], persist_enabled=False, min_score=None)
    assert [t.ad_id for t in targets] == [1, 2]


def test_notify_targets_filters_by_min_score() -> None:
    listings = [_listing(ad_id=1), _listing(ad_id=2), _listing(ad_id=3)]
    listings[0].evaluation = DealEvaluation(score=60, why="x")
    listings[1].evaluation = DealEvaluation(score=80, why="y")
    targets = _notify_targets(listings, inserted_ids=[], persist_enabled=False, min_score=70)
    assert [t.ad_id for t in targets] == [2]


def test_notify_targets_unrated_and_low_score_excluded() -> None:
    listings = [_listing(ad_id=1), _listing(ad_id=2)]
    listings[1].evaluation = DealEvaluation(score=50, why="x")
    targets = _notify_targets(listings, inserted_ids=[], persist_enabled=False, min_score=70)
    assert targets == []
