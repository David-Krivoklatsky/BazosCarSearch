"""Format listings as short Telegram messages (Phase 4)."""

from __future__ import annotations

from bazcar.core.models import Listing

_MAX_TEXT_LEN = 4000  # Telegram hard limit for a single message


def _em(text: str) -> str:
    """Minimal HTML-escape for Telegram parse_mode=HTML."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _em_url(url: str) -> str:
    """Escape a URL for use inside an HTML attribute (href)."""
    return url.replace("&", "&amp;").replace('"', "&quot;")


def clean_title(title: str) -> str:
    """Strip slashes from a listing title (Telegram treats a leading '/' as a
    command; slashes also read poorly in messages)."""
    return title.replace("/", "").strip()


def _link(url: str, label: str = "🔗 Otvoriť inzerát") -> str:
    """Render the listing URL as a tidy hyperlink instead of a bare URL."""
    return f"<a href=\"{_em_url(url)}\">{label}</a>"


def format_listing(listing: Listing) -> str:
    """Render one listing as a compact HTML message for Telegram."""
    lines = [f"<b>{_em(clean_title(listing.title))}</b>"]

    price = listing.price_eur
    price_text = (
        f"{price:,.0f} €".replace(",", " ")
        if price is not None
        else (listing.price_raw or "Dohodou")
    )
    lines.append(f"💰 {_em(str(price_text))}")

    meta: list[str] = []
    if listing.year:
        meta.append(str(listing.year))
    if listing.mileage_km is not None:
        meta.append(f"{listing.mileage_km:,} km".replace(",", " "))
    if listing.city:
        meta.append(listing.city)
    if meta:
        lines.append(f"<i>{' | '.join(_em(m) for m in meta)}</i>")

    if listing.evaluation is not None:
        lines.append(f"⭐ {listing.evaluation.score}/100 — {_em(listing.evaluation.why)}")
        if listing.evaluation.risk:
            lines.append(f"⚠️ <i>Riziko:</i> {_em(listing.evaluation.risk)}")

    lines.append(f"🆔 {listing.ad_id}")
    lines.append(_link(listing.url))
    text = "\n".join(lines)
    return text[:_MAX_TEXT_LEN]


_PHOTO_CAPTION_LIMIT = 1024  # Telegram hard limit for a photo caption


def format_photo_caption(listing: Listing) -> str:
    """Full rich message used as a photo caption (fits the 1024-char limit)."""
    return format_listing(listing)[:_PHOTO_CAPTION_LIMIT]


def listing_keyboard(listing: Listing) -> dict:
    """Inline keyboard for one deal notification (InlineKeyboardMarkup).

    * "💾 Uložiť" — inline button posting ``save:<ad_id>`` as callback_data.
    * "🔗 Otvoriť inzerát" — native URL button (opens the ad in Telegram).
    """
    return {
        "inline_keyboard": [
            [
                {"text": "💾 Uložiť", "callback_data": f"save:{listing.ad_id}"},
                {"text": "🔗 Otvoriť inzerát", "url": listing.url},
            ]
        ]
    }
