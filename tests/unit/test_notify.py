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
    assert "Risk" in text
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
    assert "Ask" in format_listing(listing)


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
    assert sent == [1, 3]


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
    assert sent == [5]
    assert photo_route.called
    body = json.loads(photo_route.calls[0].request.content)
    assert body["photo"] == "https://img/x.jpg"
    assert "5" in body["caption"]
    assert body["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "save:5"


@respx.mock
@pytest.mark.asyncio
async def test_send_listings_with_photos_falls_back_to_text_on_photo_failure() -> None:
    """A rejected photo (dead URL / bad format) must not drop the listing."""
    respx.post("https://api.telegram.org/botTOKEN/sendPhoto").mock(
        return_value=Response(400, json={"ok": False, "description": "wrong file type"})
    )
    text_route = respx.post("https://api.telegram.org/botTOKEN/sendMessage").mock(
        return_value=Response(200, json={"ok": True, "result": {}})
    )
    listing = _listing(ad_id=5, image_urls=["https://img/x.jpg"])
    async with TelegramNotifier("TOKEN", "42") as ntf:
        sent = await ntf.send_listings_with_photos([listing])
    assert sent == [5]
    assert text_route.called
    body = json.loads(text_route.calls[0].request.content)
    assert "Kia Sportage" in body["text"]


@respx.mock
@pytest.mark.asyncio
async def test_send_message_clips_oversized_body() -> None:
    route = respx.post("https://api.telegram.org/botTOKEN/sendMessage").mock(
        return_value=Response(200, json={"ok": True, "result": {}})
    )
    async with TelegramNotifier("TOKEN", "42") as ntf:
        await ntf.send_message("x" * 5000)
    body = json.loads(route.calls[0].request.content)
    assert len(body["text"]) <= 4096
    assert body["text"].endswith("…")


@respx.mock
@pytest.mark.asyncio
async def test_send_photo_clips_oversized_caption() -> None:
    route = respx.post("https://api.telegram.org/botTOKEN/sendPhoto").mock(
        return_value=Response(200, json={"ok": True, "result": {"photo": []}})
    )
    async with TelegramNotifier("TOKEN", "42") as ntf:
        await ntf.send_photo("https://img/x.jpg", caption="y" * 2000)
    body = json.loads(route.calls[0].request.content)
    assert len(body["caption"]) <= 1024


@respx.mock
@pytest.mark.asyncio
async def test_answer_callback_acknowledges() -> None:
    route = respx.post("https://api.telegram.org/botTOKEN/answerCallbackQuery").mock(
        return_value=Response(200, json={"ok": True})
    )
    async with TelegramNotifier("TOKEN", "42") as ntf:
        ok = await ntf.answer_callback("123", "💾 Saved ✅")
    assert ok is True
    body = json.loads(route.calls[0].request.content)
    assert body["callback_query_id"] == "123"
    assert body["text"] == "💾 Saved ✅"


@respx.mock
@pytest.mark.asyncio
async def test_edit_message_text_updates_in_place() -> None:
    route = respx.post("https://api.telegram.org/botTOKEN/editMessageText").mock(
        return_value=Response(200, json={"ok": True, "result": {}})
    )
    async with TelegramNotifier("TOKEN", "42") as ntf:
        ok = await ntf.edit_message_text(
            "page 2", 55, reply_markup={"inline_keyboard": []}
        )
    assert ok is True
    body = json.loads(route.calls[0].request.content)
    assert body["message_id"] == 55
    assert body["chat_id"] == "42"
    assert body["parse_mode"] == "HTML"
    assert body["disable_web_page_preview"] is True
    assert body["reply_markup"] == {"inline_keyboard": []}


@respx.mock
@pytest.mark.asyncio
async def test_edit_message_text_clips_oversized_body() -> None:
    route = respx.post("https://api.telegram.org/botTOKEN/editMessageText").mock(
        return_value=Response(200, json={"ok": True, "result": {}})
    )
    async with TelegramNotifier("TOKEN", "42") as ntf:
        await ntf.edit_message_text("z" * 5000, 1)
    body = json.loads(route.calls[0].request.content)
    assert len(body["text"]) <= 4096


@respx.mock
@pytest.mark.asyncio
async def test_send_document_uploads_csv() -> None:
    route = respx.post("https://api.telegram.org/botTOKEN/sendDocument").mock(
        return_value=Response(200, json={"ok": True, "result": {}})
    )
    async with TelegramNotifier("TOKEN", "42") as ntf:
        ok = await ntf.send_document("cars.csv", b"ad_id,price\n1,2\n", caption="export")
    assert ok is True
    raw = route.calls[0].request.content
    assert b'name="document"' in raw
    assert b"cars.csv" in raw


@respx.mock
@pytest.mark.asyncio
async def test_send_document_failure_is_swallowed() -> None:
    respx.post("https://api.telegram.org/botTOKEN/sendDocument").mock(
        return_value=Response(500, json={"ok": False})
    )
    async with TelegramNotifier("TOKEN", "42") as ntf:
        ok = await ntf.send_document("cars.csv", b"x", caption="")
    assert ok is False


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
