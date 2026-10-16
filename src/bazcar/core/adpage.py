"""Bazoš ad-page access without bs4 (import-light: safe on Vercel and in the sweeper).

``fetch_ad_page`` reads one ad. Bazoš answers a removed/sold ad with a redirect
to the category page, which carries no ``Inzerát <id>:`` meta description — that
is the reliable "gone" signal. ``diagnose_missing`` explains, for an ad that is
live on Bazoš but absent from the database, which scrape filter excluded it.
"""

from __future__ import annotations

import logging
import re
from enum import StrEnum
from typing import Any

import httpx

logger = logging.getLogger(__name__)

AD_URL = "https://auto.bazos.sk/inzerat/{ad_id}/"
_META_RE = re.compile(
    r"Inzer.t \S+?\s+(\d+):\s*(.*?), Cena:\s*(.*?), Lokalita:\s*(.*?)\.",
    re.S,
)
_UA = {"User-Agent": "Mozilla/5.0"}

# Markers of a bot-check / rate-limit interstitial. Such a page never mentions
# the ad id, so without this guard it would look identical to a removed ad and
# we would hide live inventory. A blocked page means "retry later", never "gone".
_BLOCK_MARKERS = (
    "cf-browser-verification",
    "cf_chl_opt",
    "Just a moment",
    "Attention Required",
    "Access denied",
    "Too many requests",
    "captcha",
)


class AdState(StrEnum):
    """Outcome of a single ad-page probe."""

    LIVE = "live"            # ad served, meta parsed
    UNPARSED = "unparsed"    # ad id present but markup changed — treat as alive
    GONE = "gone"            # 404 or confirmed redirect to a category page
    BLOCKED = "blocked"      # bot-check / rate-limit page — retry later


def _mentioned(html: str, ad_id: int) -> bool:
    """True when the exact ad id appears (word-boundary, so 123 ≠ 1234)."""
    return re.search(rf"(?<!\d){ad_id}(?!\d)", html) is not None


def _is_blocked(html: str) -> bool:
    return any(marker in html for marker in _BLOCK_MARKERS)


def parse_ad_meta(html: str, ad_id: int) -> dict[str, Any]:
    """Parse the ad page. ``state="gone"`` only when the ad is truly absent.

    Safety rule: a page that still mentions this ad's id is alive even when its
    meta description cannot be read (markup change), and a bot-check page is
    "blocked" (retry) — we would rather keep a car than hide a live one.
    Category/redirect pages never mention the id and carry no block markers.
    """
    match = _META_RE.search(html)
    if not match or int(match.group(1)) != ad_id:
        if _mentioned(html, ad_id):
            return {"title": None, "price_raw": None, "price": None, "city": None,
                    "unparsed": True, "state": AdState.UNPARSED.value}
        if _is_blocked(html):
            return {"title": None, "price_raw": None, "price": None, "city": None,
                    "state": AdState.BLOCKED.value}
        return {"gone": True, "state": AdState.GONE.value}
    title = re.sub(r"\s+", " ", match.group(2)).strip()[:200]
    price_raw = re.sub(r"\s+", " ", match.group(3)).strip()[:40]
    city = re.sub(r"\s+", " ", match.group(4)).strip()[:80]
    # Ranges ("1 500 – 2 000 €") list two numbers; keep the lower bound.
    price_match = re.search(r"\d[\d ]*", price_raw)
    digits = re.sub(r"[^\d]", "", price_match.group(0)) if price_match else ""
    return {
        "title": title or None,
        "price_raw": price_raw or None,
        "price": int(digits) if digits else None,
        "city": city or None,
        "state": AdState.LIVE.value,
    }


async def fetch_ad_page(ad_id: int, client: httpx.AsyncClient | None = None) -> dict[str, Any] | None:
    """Live state of one ad.

    Returns ``None`` when the state cannot be determined (network error, 5xx,
    403/429 bot-check) — the caller must retry later, never remove. Only a 404
    or a 200 page that parses as absent yields ``state="gone"``.
    """
    owns = client is None
    client = client or httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=_UA)
    try:
        resp = await client.get(AD_URL.format(ad_id=ad_id))
    except httpx.HTTPError:
        logger.warning("ad page fetch failed for %s", ad_id, exc_info=True)
        return None
    finally:
        if owns:
            await client.aclose()
    if resp.status_code == 404:
        return {"gone": True, "state": AdState.GONE.value}
    if resp.status_code != 200:
        logger.info("ad %s probe inconclusive (HTTP %s) — retry later", ad_id, resp.status_code)
        return None
    return parse_ad_meta(resp.text, ad_id)


def _num(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None


def diagnose_missing(ad: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    """Why an ad that exists on Bazoš is absent from the database (pure).

    ``ad`` = live title/price/city, ``scope`` = the active scrape filters.
    """
    title = ad.get("title") or ""
    price = ad.get("price")
    checks: list[dict[str, Any]] = []
    query = str(scope.get("query") or "").strip()
    if query:
        checks.append({
            "key": "query",
            "label": f"search text “{query}” in title",
            # Bazoš matches more loosely than substring; treat as approximate.
            "passes": query.lower() in title.lower(),
            "approx": True,
        })
    min_price, max_price = _num(scope.get("min_price")), _num(scope.get("max_price"))
    if min_price is not None:
        checks.append({
            "key": "min_price",
            "label": f"price ≥ €{min_price}",
            "passes": None if price is None else price >= min_price,
        })
    if max_price is not None:
        checks.append({
            "key": "max_price",
            "label": f"price ≤ €{max_price}",
            "passes": None if price is None else price <= max_price,
        })
    failed = [c for c in checks if c["passes"] is False]
    unknown = [c for c in checks if c["passes"] is None]
    if failed:
        worst = failed[0]
        detail = ""
        if worst["key"] == "query":
            detail = "The title does not contain your search text."
        elif worst["key"] == "min_price":
            detail = f"The ad costs €{price} — below your minimum."
        elif worst["key"] == "max_price":
            detail = f"The ad costs €{price} — above your maximum."
        return {
            "verdict": "excluded",
            "reason": f"Excluded by your {worst['key']} filter ({worst['label']}). {detail}",
            "checks": checks,
        }
    if unknown:
        return {
            "verdict": "uncertain",
            "reason": "The ad price could not be read automatically — compare it against your filters manually.",
            "checks": checks,
        }
    return {
        "verdict": "in_scope_unseen",
        "reason": (
            "Matches your scope but was never scraped — likely beyond the page depth, "
            "freshly posted, or briefly removed. It should appear within the next runs."
        ),
        "checks": checks,
    }
