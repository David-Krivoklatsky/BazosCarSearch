"""Telegram bot daemon: preferences, catalog browsing, stats, queue, templates.

Runs ``bazcar bot`` — a long-polling loop (``getUpdates``) — or the Vercel
webhook (api/telegram.py). Scraping stays in the CLI/cron; the bot persists
preferences (criteria, model, min score, photos), lets the user browse the
WHOLE collected catalog (evaluated or not), watch the AI queue, see stats
and open the Mini App dashboard.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from bazcar.bot.store import UserStore
from bazcar.bot.templates import TEMPLATES, get_template
from bazcar.config.settings import get_settings, load_llm_config
from bazcar.core.exceptions import ConfigError
from bazcar.core.models import DealEvaluation, Listing
from bazcar.core.profiles import profile_key
from bazcar.db.repository import ListingRepository
from bazcar.notify.telegram import TelegramNotifier

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"

# Registered via Telegram setMyCommands -> typing "/" shows an autocomplete
# menu with these descriptions in every Telegram client.
#
# Visible set (22): the daily-driver commands. The rest still work as hidden
# aliases for backward compatibility (old messages, muscle memory, tests):
# /score, /photos, /models, /note, /alert, /digest, /status.
BOT_COMMANDS: list[dict[str, str]] = [
    {"command": "menu", "description": "🏠 Main menu — buttons for everything"},
    {"command": "cars", "description": "🚗 All cars (even without AI score)"},
    {"command": "car", "description": "🔍 Listing detail: /car ad_id"},
    {"command": "show", "description": "⭐ Top-rated deals"},
    {"command": "criteria", "description": "What I'm looking for — AI scores by this"},
    {"command": "templates", "description": "📦 Ready-made searches (family, city…)"},
    {"command": "search", "description": "Save a search: /search name: criteria"},
    {"command": "searches", "description": "My saved searches"},
    {"command": "use", "description": "Activate a saved search"},
    {"command": "filter", "description": "Bazoš filters: query, price, dist, psc"},
    {"command": "settings", "description": "⚙️ Settings & status"},
    {"command": "eval", "description": "Re-score cars for the active search"},
    {"command": "queue", "description": "⏳ AI evaluation queue"},
    {"command": "stats", "description": "📊 Catalog and market stats"},
    {"command": "health", "description": "🩺 Pipeline health (last run)"},
    {"command": "dashboard", "description": "📱 Open the web dashboard"},
    {"command": "export", "description": "📄 Download listings as CSV"},
    {"command": "save", "description": "Save a listing for later (ad_id)"},
    {"command": "saved", "description": "My saved listings + discounts"},
    {"command": "unsave", "description": "Remove a saved listing"},
    {"command": "model", "description": "AI model (list / switch)"},
    {"command": "help", "description": "Help — /command help for details"},
]

# Hidden aliases: accepted by _command but not advertised in the menu.
_HIDDEN_COMMANDS = {"score", "photos", "models", "note", "alert", "digest", "status"}


# ---------------------------------------------------------------------------
# Pure formatting helpers (unit-tested, no I/O)
# ---------------------------------------------------------------------------

CARS_PAGE_SIZE = 8

# Compact tokens used in paginated /cars callback data.
_CAR_STATUS = {"a": "all", "u": "unevaluated", "r": "rated"}
_CAR_SORT = {"n": "newest", "c": "price_asc", "e": "price_desc", "s": "score"}

_STATUS_LABELS = {"all": "all", "unevaluated": "unscored", "rated": "scored"}
_SORT_LABELS = {
    "newest": "newest",
    "price_asc": "cheapest",
    "price_desc": "priciest",
    "score": "top score",
}

_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[int]) -> str:
    """Render a tiny bar sparkline (e.g. ▁▃▅█) from daily counts."""
    if not values:
        return ""
    peak = max(values) or 1
    return "".join(
        _SPARK_BLOCKS[min(7, round(v / peak * 7))] if v else _SPARK_BLOCKS[0] for v in values
    )


def bar(value: int, total: int, width: int = 10) -> str:
    """Render a unicode progress bar like ▓▓▓▓▓▓░░░░."""
    if total <= 0:
        return "░" * width
    filled = round(value / total * width)
    return "▓" * filled + "░" * (width - filled)


def fmt_eur(value: Any) -> str:
    """Format a price as '12 500 €' (None-safe)."""
    if value is None:
        return "dohodou"
    try:
        return f"{int(value):,} €".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def fmt_km(value: Any) -> str:
    if value is None:
        return "? km"
    return f"{int(value):,} km".replace(",", " ")


def time_ago(dt: Any) -> str:
    """Human age of a timestamp ('12 min ago') — accepts datetime or str."""
    if dt is None:
        return "never"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return str(dt)[:16]
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=UTC)
    seconds = max(0, (datetime.now(UTC) - dt).total_seconds())
    if seconds < 120:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} h ago"
    return f"{hours // 24} d ago"


def extract_make(title: str) -> str | None:
    """Crude make extraction: the first word of the title ('Škoda Octavia…')."""
    match = re.match(r"[A-Za-zÁ-Žá-ž0-9]+", (title or "").strip())
    if not match:
        return None
    make = match.group(0)
    return make if len(make) >= 2 else None


def parse_cars_args(arg: str) -> tuple[str, str, int]:
    """Parse ``/cars [status] [sort] [days N]`` -> (status, sort, days).

    "" -> ("all", "newest", 7); "unevaluated" -> ("unevaluated", "newest", 7);
    "cheap days 30" -> ("all", "price_asc", 30). Slovak aliases included.
    """
    status, sort, days = "all", "newest", 7
    tokens = (arg or "").lower().split()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in {"unevaluated", "neohodnotene", "neohodnotené", "fresh", "nove", "nové"}:
            status = "unevaluated"
        elif token in {"rated", "ohodnotene", "ohodnotené", "scored"}:
            status = "rated"
        elif token in {"cheap", "najlacnejsie", "najlacnejšie", "lacne"}:
            sort = "price_asc"
        elif token in {"expensive", "najdrahsie", "najdrahšie", "drahe", "drahé"}:
            sort = "price_desc"
        elif token in {"score", "skore", "skóre", "best", "najlepsie", "najlepšie"}:
            sort = "score"
        elif token in {"newest", "najnovsie", "najnovšie"}:
            sort = "newest"
        elif token in {"days", "dni"} and i + 1 < len(tokens) and tokens[i + 1].isdigit():
            days = max(1, min(int(tokens[i + 1]), 3650))
            i += 1
        elif token.isdigit():
            days = max(1, min(int(token), 3650))
        i += 1
    return status, sort, days


def cars_callback_encode(status: str, sort: str, days: int, offset: int) -> str:
    s_code = next(k for k, v in _CAR_STATUS.items() if v == status)
    o_code = next(k for k, v in _CAR_SORT.items() if v == sort)
    return f"c:{s_code}:{o_code}:{days}:{offset}"


def parse_cars_callback(data: str) -> tuple[str, str, int, int] | None:
    """Decode ``c:<status>:<sort>:<days>:<offset>`` back to view parameters."""
    try:
        _, s_code, o_code, days, offset = data.split(":")
        return _CAR_STATUS[s_code], _CAR_SORT[o_code], int(days), int(offset)
    except (ValueError, KeyError):
        return None


def build_cars_page(
    rows: list[dict], *, total: int, offset: int, days: int, status: str, sort: str
) -> str:
    """Render one page of the /cars catalog (compact, one line per ad)."""
    head = (
        f"<b>🚗 Cars {offset + 1}–{offset + len(rows)} of {total}</b> "
        f"<i>({_STATUS_LABELS[status]}, {_SORT_LABELS[sort]}, {days} days)</i>\n"
    )
    lines = []
    for row in rows:
        score = row.get("score")
        is_car = row.get("is_car")
        if score is None:
            badge = "⏳"
        elif is_car is False:
            badge = "🛠"
        else:
            badge = f"⭐{score}"
        bits = [fmt_eur(row.get("price_eur"))]
        if row.get("year"):
            bits.append(str(row["year"]))
        if row.get("mileage_km") is not None:
            bits.append(fmt_km(row["mileage_km"]))
        if row.get("city"):
            bits.append(_em(str(row["city"])))
        title = _em(str(row["title"]))[:60]
        lines.append(
            f"{badge} <a href=\"{_em_url(row['url'])}\">{title}</a>\n"
            f"    {' · '.join(bits)} · 🆔 {row['ad_id']}"
        )
    tail = "\n<i>/car &lt;id&gt; — detail · /export — CSV · /dashboard — charts</i>"
    return head + "\n".join(lines) + tail


def cars_keyboard(status: str, sort: str, days: int, offset: int, total: int) -> dict:
    """Prev/next navigation for the /cars pager (edit-in-place)."""
    buttons = []
    if offset > 0:
        prev_offset = max(0, offset - CARS_PAGE_SIZE)
        buttons.append(
            {"text": "◀️ Previous", "callback_data": cars_callback_encode(status, sort, days, prev_offset)}
        )
    if offset + CARS_PAGE_SIZE < total:
        next_offset = offset + CARS_PAGE_SIZE
        buttons.append(
            {"text": "Next ▶️", "callback_data": cars_callback_encode(status, sort, days, next_offset)}
        )
    return {"inline_keyboard": [buttons] if buttons else []}


def stale_line(last_seen_at: str | None, now: datetime | None = None) -> str | None:
    """Staleness transparency: flag ads not seen in recent scrapes.

    "Not seen recently" is NOT proof of sale — the card says so explicitly.
    """
    if not last_seen_at:
        return None
    try:
        seen = last_seen_at if isinstance(last_seen_at, datetime) else datetime.fromisoformat(
            str(last_seen_at).replace("Z", "+00:00")
        )
    except ValueError:
        return None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    ref = now or datetime.now(UTC)
    days = max(0, (ref - seen).days)
    if days <= 7:
        return None
    return (
        f"🕸 Last seen {days} days ago — the ad may be gone "
        "(sold/removed), but that's not certain."
    )


def build_car_card(row: dict, history: list[dict], bench: dict | None) -> str:
    """Render the /car detail card (rich, with price history + benchmark)."""
    lines = [f"<b>{_em(str(row['title']))}</b>"]
    lines.append(f"💰 {fmt_eur(row.get('price_eur'))}")
    meta = []
    if row.get("year"):
        meta.append(f"🗓 {row['year']}")
    if row.get("mileage_km") is not None:
        meta.append(f"🛣 {fmt_km(row['mileage_km'])}")
    if row.get("city"):
        meta.append(f"📍 {_em(str(row['city']))}")
    if row.get("views") is not None:
        meta.append(f"👁 {row['views']}")
    if meta:
        lines.append(" · ".join(meta))
    if row.get("published_date"):
        lines.append(f"📅 Listed {row['published_date']}")
    stale = stale_line(row.get("last_seen_at"))
    if stale:
        lines.append(stale)

    if row.get("score") is not None:
        lines.append("")
        lines.append(f"⭐ <b>{row['score']}/100</b> — {_em(row.get('why') or '')}")
        if row.get("risk"):
            lines.append(f"⚠️ <i>Risk:</i> {_em(row['risk'])}")
        if row.get("condition"):
            lines.append(f"🔧 <i>Condition:</i> {_em(row['condition'])}")
        if row.get("target_offer"):
            lines.append(f"🤝 <i>First offer:</i> {fmt_eur(row['target_offer'])}")
        if row.get("eval_model"):
            lines.append(f"<i>model: {_em(str(row['eval_model']))}</i>")
    else:
        lines.append("")
        lines.append("⏳ <i>No AI score yet — queue it with /eval</i>")

    points = [h for h in history if h.get("price_eur") is not None]
    if len(points) >= 2:
        first, last = points[0], points[-1]
        try:
            change = round((float(last["price_eur"]) / float(first["price_eur"]) - 1) * 100)
            arrow = "📉" if change < 0 else "📈"
            lines.append(
                f"{arrow} Price trend: {fmt_eur(first['price_eur'])} → "
                f"{fmt_eur(last['price_eur'])} ({change:+d} %)"
            )
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    if bench and bench.get("n") and row.get("price_eur") is not None:
        median = bench.get("median")
        if median:
            try:
                diff = round((float(row["price_eur"]) / float(median) - 1) * 100)
                verdict = "below median" if diff < 0 else "above median"
                tag = f"n={bench['n']}"
                if bench["n"] < 3:
                    tag += ", low data ⚠️"
                elif bench.get("trimmed"):
                    tag += ", outliers removed"
                lines.append(
                    f"📊 Market ({tag}, year ±2): median {fmt_eur(int(median))} "
                    f"— this one {abs(diff)} % {verdict}"
                )
            except (TypeError, ValueError, ZeroDivisionError):
                pass
    lines.append(f"🆔 {row['ad_id']}")
    lines.append(f"<a href=\"{_em_url(row['url'])}\">🔗 Open listing</a>")
    return "\n".join(lines)


def build_stats_text(stats: dict, p_key: str) -> str:
    """Render the /stats message: totals, coverage, sparkline, score bands."""
    total = stats["total"]
    evaluated = stats["evaluated"]
    coverage = round(evaluated / total * 100) if total else 0
    lines = ["<b>📊 Catalog stats</b>", ""]
    lines.append(f"📦 Listings in database: <b>{total}</b>")
    lines.append(
        f"🤖 Scored (your profile): <b>{evaluated}</b> "
        f"{bar(evaluated, total)} {coverage} %"
    )
    price = stats.get("price") or {}
    if price.get("median") is not None:
        lines.append(
            f"💰 Prices: median <b>{fmt_eur(int(price['median']))}</b>, "
            f"min {fmt_eur(price.get('mn'))}, max {fmt_eur(price.get('mx'))}"
        )
    new_by_day = stats.get("new_by_day") or []
    if new_by_day:
        counts = [n for _, n in new_by_day]
        lines.append(
            f"🆕 New in 7 days: <b>{sum(counts)}</b>  {sparkline(counts)} "
            f"<i>({new_by_day[0][0][5:]} – {new_by_day[-1][0][5:]})</i>"
        )
    lines.append(f"📉 Discounts in 7 days: <b>{stats.get('drops_7d', 0)}</b>")
    bands = stats.get("bands") or {}
    band_total = sum(bands.values())
    if band_total:
        lines.append("")
        lines.append("<b>Score (your profile)</b>")
        for label, key in (("90+", "b90"), ("80–89", "b80"), ("70–79", "b70"),
                           ("50–69", "b50"), ("<50", "b0")):
            value = bands.get(key, 0)
            lines.append(f"  {label:>6} {bar(value, band_total, 12)} {value}")
    lines.append("")
    lines.append(f"<i>profile: {p_key} · /cars — browse · /dashboard — charts</i>")
    return "\n".join(lines)


def build_queue_text(pending: list[dict], finished: list[dict]) -> str:
    """Render the /queue message: running/queued tasks + recent outcomes."""
    lines = ["<b>⏳ AI evaluation queue</b>", ""]
    if not pending and not finished:
        lines.append("Queue is empty — everything is scored. ✅")
    for task in pending:
        criteria = (task.get("criteria") or "no criteria").strip()
        label = criteria if len(criteria) <= 40 else criteria[:37] + "…"
        total = task.get("total") or 0
        done = task.get("done") or 0
        if task.get("started_at"):
            progress = f"{bar(done, total)} {done}/{total}"
        else:
            progress = "waiting in line"
        age = time_ago(task.get("created_at"))
        attempts = task.get("attempts") or 0
        retry = f", attempt {attempts}" if attempts else ""
        lines.append(f"⏳ <i>{_em(label)}</i>\n    {progress} · {age}{retry}")
        lines.append(f"    <code>{_em(task.get('model') or '?')}</code>")
    if finished:
        lines.append("")
        lines.append("<b>Recently finished</b>")
        for task in finished:
            criteria = (task.get("criteria") or "no criteria").strip()
            label = criteria if len(criteria) <= 40 else criteria[:37] + "…"
            age = time_ago(task.get("done_at"))
            if task.get("error"):
                lines.append(f"⚠️ <i>{_em(label)}</i> — {_em(task['error'])} ({age})")
            else:
                lines.append(
                    f"✅ <i>{_em(label)}</i> — {task.get('done') or 0}/{task.get('total') or 0} cars ({age})"
                )
    lines.append("")
    lines.append("<i>The cron processes the queue every ~15 min. Speed up: button below.</i>")
    return "\n".join(lines)


MENU_KEYBOARD: dict = {
    "inline_keyboard": [
        [
            {"text": "🚗 Browse cars", "callback_data": "menu:cars"},
            {"text": "⭐ Top deals", "callback_data": "menu:show"},
        ],
        [
            {"text": "📊 Stats", "callback_data": "menu:stats"},
            {"text": "⏳ AI queue", "callback_data": "menu:queue"},
        ],
        [
            {"text": "📦 Templates", "callback_data": "menu:templates"},
            {"text": "💾 Saved", "callback_data": "menu:saved"},
        ],
        [
            {"text": "📱 Dashboard", "callback_data": "menu:dashboard"},
        ],
        [
            {"text": "⚙️ Settings", "callback_data": "menu:settings"},
            {"text": "❓ Help", "callback_data": "menu:help"},
        ],
    ]
}

COMMAND_HELP: dict[str, str] = {
    "/criteria": (
        "<b>/criteria</b> — what I'm looking for\n\n"
        "The AI scores every listing 0–100 against your criteria — "
        "the more specific you are, the sharper the score.\n\n"
        "<b>Usage:</b> <code>/criteria + text</code>\n"
        "<b>Example:</b> <code>/criteria diesel, under 200,000 km, over 80 kW</code>\n\n"
        "➡️ Changing it auto-queues a re-score of the last 3 days of cars "
        "(you'll get a 🧮 report when it's done). New listings are scraped "
        "under the active filters every ~15 min."
    ),
    "/search": (
        "<b>/search</b> — save a named search\n\n"
        "Saves the full profile: criteria + current Bazoš filters + min score.\n"
        "Keep several searches (highway, city, family car…) and switch between them.\n\n"
        "<b>Usage:</b> <code>/search name: criteria</code>\n"
        "<b>Example:</b> <code>/search highway: diesel under 200k km</code>\n\n"
        "💡 Set <code>/filter</code> and your score first, then save — "
        "everything is kept in the profile."
    ),
    "/searches": (
        "<b>/searches</b> — your saved searches\n\n"
        "Shows all saved profiles (criteria + filters + score) with their "
        "alert state. The 🔔/🔕 buttons toggle alerts per profile, "
        "▶️ activates it (same as <code>/use name</code>)."
    ),
    "/alert": (
        "<b>/alert</b> — watch a saved listing's price\n\n"
        "<code>/alert 195315165 3000</code> — alert me once the price drops below €3,000\n"
        "<code>/alert 195315165 off</code> — back to alerting on every discount\n"
        "<code>/alert 195315165</code> — show the current threshold\n\n"
        "Without a threshold I alert on every discount automatically."
    ),
    "/digest": (
        "<b>/digest</b> — weekly summary\n\n"
        "New listings, biggest discounts, top matches for your profile and the "
        "AI queue state for the last 7 days. On demand, anytime."
    ),
    "/use": (
        "<b>/use</b> — activate a saved search\n\n"
        "Sets criteria, filters and min score from the saved profile and "
        "auto-queues a re-score of recent cars under it.\n\n"
        "<b>Usage:</b> <code>/use name</code>\n"
        "<b>Example:</b> <code>/use highway</code>"
    ),
    "/settings": (
        "<b>/settings</b> — settings & status\n\n"
        "Shows your active criteria, model, min score, filters, scored-car "
        "count, best score and whether a re-score is running — with buttons "
        "to change score (±5), photos and alerts inline.\n\n"
        "Replaces the old <code>/status</code>, <code>/score</code> and "
        "<code>/photos</code> commands (they still work as aliases)."
    ),
    "/filter": (
        "<b>/filter</b> — Bazoš filters (what gets scraped)\n\n"
        "They filter results on Bazoš itself — controlling which listings load.\n"
        "  <code>/filter query skoda octavia</code> — search text\n"
        "  <code>/filter price 1500-4000</code> — price range in € (also 1500- or -4000)\n"
        "  <code>/filter dist 100</code> — radius around the ZIP in km (set /filter psc first)\n"
        "  <code>/filter psc 81101</code> — location\n"
        "  <code>/filter clear</code> — back to defaults\n"
        "  <code>/filter</code> — show active filters\n\n"
        "💡 Mileage is not a Bazoš filter — the AI weighs it via criteria."
    ),
    "/show": (
        "<b>/show</b> — matching listings from the recent window\n\n"
        "Sends your top-rated listings (active search + score threshold) "
        "as photo messages with buttons. Each message also carries the 📅 listing date.\n\n"
        "<b>Usage:</b>\n"
        "  <code>/show</code> — your default count, 3-day window\n"
        "  <code>/show 20</code> — 20 cars (saved as the new default)\n"
        "  <code>/show days 7</code> — 7-day window\n"
        "  <code>/show 20 days 100</code> — combined\n\n"
        "⚠️ The result count is driven mostly by the <b>score threshold</b> — a wider "
        "window only adds older listings. The bot always prints how many matches exist in total.\n\n"
        "💡 If the result is empty, the bot suggests the best score achieved."
    ),
    "/eval": (
        "<b>/eval</b> — re-score cars for the active search\n\n"
        "Queues a re-score under the active criteria and model. Already scored "
        "cars (same search + model) are skipped — nothing is paid for twice.\n\n"
        "<b>Usage:</b> (same parameters as /show)\n"
        "  <code>/eval</code> — last 100 cars from 3 days\n"
        "  <code>/eval 30</code> — 30 newest\n"
        "  <code>/eval days 7</code> — 7-day window\n"
        "  <code>/eval 50 days 7</code> — combined\n"
        "  <code>/eval all</code> — everything in the DB (up to 500)\n\n"
        "The bot immediately prints how many cars will be scored and a time "
        "estimate. When done, you'll get a ✅ report."
    ),
    "/model": (
        "<b>/model</b> — AI model (list / switch)\n\n"
        "<b>Usage:</b>\n"
        "  <code>/model</code> — current model + detail\n"
        "  <code>/model ID</code> — set the model and start a re-score\n"
        "  <code>/model info ID</code> — detail of one model\n"
        "  <code>/models</code> — list all JSON-capable models (alias)\n\n"
        "<b>Example:</b>\n"
        "  <code>/model ~deepseek/deepseek-v4-flash-latest</code>\n\n"
        "Key parameters (in /model info): 💰 price per 1M tokens, "
        "⚡ requests/min (∞ = no limit), 🔡 context.\n\n"
        "💡 If backfills throw 429s, switch to a model with ⚡∞."
    ),
    "/save": (
        "<b>/save</b> — save a listing for later\n\n"
        "<code>/save 195315165</code> (ad_id from a notification — the 🆔 line)\n\n"
        "💡 Faster: the <b>💾 Save</b> button right on the listing. "
        "Add a note with <code>/note id text</code>, watch the price with "
        "<code>/alert id price</code>."
    ),
    "/saved": (
        "<b>/saved</b> — my saved listings\n\n"
        "Shows saved listings as links — tap and you're on Bazoš. Each shows "
        "the current price, the drop from its max (🔔 watched automatically) "
        "and your note.\n"
        "Remove with <code>/unsave ad_id</code>, note with "
        "<code>/note ad_id text</code>, price-watch with <code>/alert ad_id price</code>."
    ),
    "/note": (
        "<b>/note</b> — note on a saved listing\n\n"
        "<code>/note 195315165 call the seller after 6pm</code>\n\n"
        "Empty text clears the note."
    ),
    "/unsave": (
        "<b>/unsave</b> — remove a saved listing\n\n"
        "<code>/unsave 195315165</code> (ad_id from /saved)"
    ),
    "/help": (
        "<b>/help</b>\n\n"
        "Overview of all commands. Per-command details: "
        "<code>/command help</code> — e.g. <code>/filter help</code>."
    ),
    "/cars": (
        "<b>/cars</b> — ALL cars in the database (even unscored)\n\n"
        "Browse the whole collected catalog — unlike /show it needs no "
        "AI score or score limit. Each row has a state: ⏳ unscored, "
        "⭐n with AI score, 🛠 probably a spare part.\n\n"
        "<b>Usage:</b>\n"
        "  <code>/cars</code> — newest, 7 days\n"
        "  <code>/cars unevaluated</code> — only unscored\n"
        "  <code>/cars rated</code> — only scored\n"
        "  <code>/cars cheap</code> / <code>expensive</code> / <code>score</code> — sort\n"
        "  <code>/cars days 30</code> — 30-day window\n\n"
        "Page with ◀️ ▶️. Detail: <code>/car id</code>."
    ),
    "/car": (
        "<b>/car</b> — one listing's detail\n\n"
        "<code>/car 195357798</code> (find ad_id in /cars or a notification)\n\n"
        "Shows: price, year, mileage, city, date, AI review (score, reason, "
        "risk, condition, first offer), price trend and the market comparison "
        "(median of similar cars with sample size)."
    ),
    "/queue": (
        "<b>/queue</b> — AI evaluation queue\n\n"
        "What's waiting/running: per task the profile, model, progress "
        "(x/y cars), age and attempt count. Below: recently finished and "
        "failed tasks. The cron processes the queue every ~15 min."
    ),
    "/stats": (
        "<b>/stats</b> — catalog stats\n\n"
        "Listing count, AI coverage (your profile), price median/min/max, "
        "new listings in 7 days (sparkline), discount count and score "
        "distribution. Interactive charts: /dashboard."
    ),
    "/health": (
        "<b>/health</b> — pipeline health\n\n"
        "Last scraper run (when, how many pages/new/discounts/scored/ "
        "notifications), queue and database state, plus 30-day AI spend vs "
        "budget. If the last run is long overdue, something is broken."
    ),
    "/templates": (
        "<b>/templates</b> — ready-made searches\n\n"
        "Pick a template (family, city, highway, first car, bargain) "
        "— sets criteria, filters and min score and starts a re-score. "
        "Then fine-tune everything via /criteria and /filter."
    ),
    "/export": (
        "<b>/export</b> — download listings as CSV\n\n"
        "<code>/export</code> — last 30 days (max 200)\n"
        "<code>/export 90</code> — last 90 days\n\n"
        "The CSV includes your profile's AI score (when present)."
    ),
    "/dashboard": (
        "<b>/dashboard</b> — web dashboard (Mini App)\n\n"
        "Opens the graphical UI right inside Telegram: overview, inventory, "
        "queue and stats with charts. Works once deployed on Vercel."
    ),
    "/menu": (
        "<b>/menu</b> — main menu\n\n"
        "Buttons for the most common actions: cars, top deals, stats, queue, "
        "templates, saved, settings and help."
    ),
}


def parse_show_args(arg: str) -> tuple[int | None, int]:
    """Parse ``/show [N] [days N]`` -> (limit, days).

    "" -> (None, 3) = use prefs default; "10" -> (10, 3); "days 7" -> (None, 7);
    "20 days 7" -> (20, 7). "dni" is accepted as a Slovak alias.
    """
    tokens = (arg or "").lower().split()
    limit: int | None = None
    days = 3
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in {"days", "dni"} and i + 1 < len(tokens) and tokens[i + 1].isdigit():
            days = max(1, min(int(tokens[i + 1]), 3650))
            i += 1
        elif token.isdigit():
            limit = max(1, min(int(token), 20))
        i += 1
    return limit, days


def _row_to_listing(row: dict) -> Listing:
    """Rebuild a Listing (+DealEvaluation when present) from a DB row.

    Tolerates rows without an evaluation (catalog views): ``evaluation`` is
    then None instead of a fabricated score.
    """
    evaluation = None
    if row.get("score") is not None:
        evaluation = DealEvaluation(
            score=row["score"],
            why=row.get("why") or "",
            risk=row.get("risk") or "",
            is_car=row.get("is_car") if row.get("is_car") is not None else True,
            model=row.get("eval_model"),
            condition=row.get("condition") or "",
            target_offer=row.get("target_offer"),
        )
    return Listing(
        ad_id=row["ad_id"],
        url=row["url"],
        title=row["title"],
        price_eur=row["price_eur"],
        city=row["city"],
        image_urls=row["image_urls"],
        year=row["year"],
        mileage_km=row["mileage_km"],
        description_preview=row.get("description_preview") or "",
        description=row.get("description"),
        published_date=row.get("published_date"),
        evaluation=evaluation,
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
    "<b>🤖 Bazcar — your used-car hunter</b>\n\n"
    "Just tell me what you're looking for (e.g. <i>\"diesel under 200k km, over 80 kW\"</i>) "
    "and every new listing arrives with an AI score of 0–100, a reason and a risk note.\n\n"
    "<b>🚗 Catalog</b>\n"
    "  /cars — all cars (even unscored), paged\n"
    "  /car id — detail: price trend + market median\n"
    "  /show — top-rated deals\n"
    "  /export — download CSV\n\n"
    "<b>🔎 What I want</b>\n"
    "  /criteria — my requirements for AI scoring\n"
    "  /templates — ready-made searches (family, city…)\n"
    "  /filter — Bazoš filters (query, price, radius, ZIP)\n"
    "  /settings — score threshold, photos, alerts\n\n"
    "<b>⭐ Search profiles</b>\n"
    "  /search name: criteria — save a profile\n"
    "  /searches — list profiles\n"
    "  /use name — activate a profile\n\n"
    "<b>📊 Insights</b>\n"
    "  /stats — catalog and market stats\n"
    "  /queue — AI evaluation queue\n"
    "  /health — pipeline health\n"
    "  /dashboard — web charts (Mini App)\n"
    "  /eval — re-score cars for a search\n\n"
    "<b>💾 Saved</b>\n"
    "  /save ad_id · /saved · /unsave ad_id\n"
    "  (notes and price-watch live on the detail card and in /saved)\n\n"
    "<b>⚙️ AI & settings</b>\n"
    "  /model · /settings · /menu\n\n"
    "💡 Details for each command: <code>/command help</code> (e.g. <code>/cars help</code>)\n"
    "💡 Type <code>/</code> and Telegram autocompletes the commands."
)


EST_SECONDS_PER_EVAL = 20  # rough per-ad LLM latency on free reasoning models


def parse_eval_args(arg: str) -> tuple[int, int]:
    """Parse ``/eval [N] [days N] [all]`` -> (max_listings, days).

    Examples: "" -> (100, 3) | "50" -> (50, 3) | "days 7" -> (100, 7)
              "50 days 7" -> (50, 7) | "all" -> (500, 3650)
    "dni" is accepted as a Slovak alias for "days".
    """
    tokens = (arg or "").lower().split()
    limit, days = 100, 3
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in {"all", "vsetky", "všetky"}:
            limit, days = 500, 3650
        elif token in {"days", "dni"} and i + 1 < len(tokens) and tokens[i + 1].isdigit():
            days = max(1, min(int(tokens[i + 1]), 3650))
            i += 1
        elif token.isdigit():
            limit = max(1, min(int(token), 500))
        i += 1
    return limit, days


class Bot:
    """Message handler bound to a TelegramNotifier and UserStore."""

    def __init__(self, store: UserStore, notifier, export_dir: Path) -> None:
        self.store = store
        self.notifier = notifier
        self.export_dir = export_dir

    async def reply(self, chat_id: int, text: str | None, reply_markup: dict | None = None) -> None:
        if text is None:
            return
        await self.notifier.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)

    @staticmethod
    def _is_allowed(chat_id: int) -> bool:
        """Access boundary: allowlist when configured, else open onboarding."""
        allowed = get_settings().allowed_chats()
        return allowed is None or chat_id in allowed

    async def handle(self, update: dict[str, Any]) -> None:
        callback = update.get("callback_query") or {}
        message = update.get("message") or {}

        # Inline-button presses (save, pager, templates, menu).
        if callback.get("data"):
            chat_id = (callback.get("message") or {}).get("chat", {}).get("id")
            message_id = (callback.get("message") or {}).get("message_id")
            cb_query_id = callback.get("id")
            if not chat_id or not cb_query_id:
                return
            if not self._is_allowed(chat_id):
                logger.info("ignoring callback from non-allowed chat %s", chat_id)
                return
            await self.store.ensure_prefs(chat_id)
            data = callback["data"]
            if data.startswith("save:"):
                await self._save(chat_id, data.split(":", 1)[1])
                await self.notifier.answer_callback(cb_query_id, "💾 Saved ✅")
            elif data.startswith("ntf:"):
                await self._toggle_search_notify(
                    chat_id, data.split(":", 1)[1], cb_query_id
                )
            elif data.startswith("ntfglobal:"):
                await self._toggle_global_notify(
                    chat_id, data.split(":", 1)[1] == "1", cb_query_id
                )
            elif data.startswith("use:"):
                name = data.split(":", 1)[1]
                reply = await self._use(chat_id, name)
                await self.notifier.answer_callback(cb_query_id, "✅ Profile activated")
                await self.reply(chat_id, reply)
            elif data.startswith("set:"):
                await self._settings_action(
                    chat_id, message_id, cb_query_id, data.split(":", 1)[1]
                )
            elif data.startswith("c:"):
                await self._cars_page(chat_id, message_id, cb_query_id, data)
            elif data.startswith("tpl:"):
                await self._apply_template(chat_id, data.split(":", 1)[1], cb_query_id)
            elif data.startswith("menu:"):
                await self._menu_action(chat_id, data.split(":", 1)[1], cb_query_id)
            else:
                await self.notifier.answer_callback(cb_query_id, "Unknown action")
            return

        text = (message.get("text") or "").strip()
        chat_id = (message.get("chat") or {}).get("id")
        if not text or not chat_id:
            return
        if not self._is_allowed(chat_id):
            logger.info("ignoring message from non-allowed chat %s", chat_id)
            return
        await self.store.ensure_prefs(chat_id)
        if text.startswith("/"):
            result = await self._command(chat_id, text)
        else:
            result = await self._set_criteria(chat_id, text)
        if isinstance(result, tuple):
            text_out, markup = result
            await self.reply(chat_id, text_out, reply_markup=markup)
        else:
            await self.reply(chat_id, result)

    async def _command(self, chat_id: int, text: str) -> str | tuple | None:
        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        # "/filter help" (or "?") -> detailed how-it-works for that command
        if arg.lower() in {"help", "?"} and cmd in COMMAND_HELP:
            return COMMAND_HELP[cmd]
        match cmd:
            case "/start":
                return (
                    "Hi! I'm Bazcar 🚗. Tell me what car you're looking for — "
                    "and new listings will arrive with an AI score.\n\n" + HELP_TEXT,
                    MENU_KEYBOARD,
                )
            case "/help":
                return HELP_TEXT, MENU_KEYBOARD
            case "/menu":
                return "🏠 <b>Main menu</b> — pick an action:", MENU_KEYBOARD
            case "/status":  # hidden alias of /settings
                return await self._settings(chat_id)
            case "/settings":
                return await self._settings(chat_id)
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
            case "/score":  # hidden alias of /settings (score section)
                return await self._score(chat_id, arg)
            case "/photos":  # hidden alias of /settings (photos section)
                return await self._photos(chat_id, arg)
            case "/filter":
                return await self._filter(chat_id, arg)
            case "/eval":
                return await self._eval(chat_id, arg)
            case "/show":
                return await self._show(chat_id, arg)
            case "/models":  # hidden alias of /model (list)
                return await self._models(chat_id, arg)
            case "/save":
                return await self._save(chat_id, arg)
            case "/saved":
                return await self._saved(chat_id)
            case "/note":  # hidden alias (also on the detail card)
                return await self._note(chat_id, arg)
            case "/alert":  # hidden alias (also on the detail card)
                return await self._alert(chat_id, arg)
            case "/digest":  # hidden alias (folded into /stats)
                return await self._digest(chat_id)
            case "/unsave":
                return await self._unsave(chat_id, arg)
            case "/cars":
                return await self._cars(chat_id, arg)
            case "/car":
                return await self._car(chat_id, arg)
            case "/queue":
                return await self._queue(chat_id)
            case "/stats":
                return await self._stats(chat_id)
            case "/health":
                return await self._health(chat_id)
            case "/templates":
                return self._templates()
            case "/export":
                return await self._export(chat_id, arg)
            case "/dashboard":
                return self._dashboard()
            case _:
                return "Unknown command. /help"

    async def _set_criteria(self, chat_id: int, text: str) -> str:
        text = text.strip()
        if not text:
            return 'Tell me what you\'re looking for, e.g.: "diesel, under 200,000 km, over 80 kW".'
        await self.store.update_prefs(chat_id, criteria=text)
        try:
            await self._queue_rehodnotenie(chat_id)
        except Exception:
            logger.warning("rehodnotenie queue failed", exc_info=True)
        return f"✅ Criteria saved:\n<i>{_em(text)}</i>"

    async def _filter(self, chat_id: int, arg: str) -> str:
        """Manage Bazoš scrape filters (/filter query|price|km|psc|clear)."""
        if not arg.strip():
            return await self._filters_status(chat_id)
        parts = arg.split(maxsplit=1)
        key = parts[0].lower()
        value = parts[1].strip() if len(parts) > 1 else ""

        if key == "clear":
            await self.store.update_prefs(chat_id, filters={})
            return "✅ Filters reset (scraping per config/scraper.yaml)."
        if not value:
            return "/filter query skoda | /filter price 1500-4000 | /filter dist 100 | /filter psc 81101 | /filter clear"

        patch: dict = {}
        if key == "query":
            patch["query"] = value
        elif key == "price":
            match = re.fullmatch(r"(\d*)\s*-\s*(\d*)", value)
            if not match or (not match.group(1) and not match.group(2)):
                return "Format: `/filter price 1500-4000` (or `1500-` / `-4000`)."
            patch["min_price"] = int(match.group(1)) if match.group(1) else None
            patch["max_price"] = int(match.group(2)) if match.group(2) else None
        elif key == "dist":
            if not value.isdigit():
                return "Format: `/filter dist 100` — radius around the ZIP in km."
            patch["distance_km"] = int(value)
        elif key == "psc":
            patch["psc"] = value
        else:
            return "Unknown filter. Use query / price / dist / psc / clear."

        filters = await self.store.update_filters(chat_id, patch)
        await _dispatch_scrape()
        return f"✅ Saved.\n{self._filters_summary(filters)}"

    @staticmethod
    def _filters_summary(filters: dict) -> str:
        if not filters:
            return "Filters: none (defaults from config/scraper.yaml)"
        bits = []
        if filters.get("query"):
            bits.append(f"search: “{_em(str(filters['query']))}”")
        lo, hi = filters.get("min_price"), filters.get("max_price")
        if lo is not None or hi is not None:
            bits.append(f"price: {lo or 0}–{hi or '∞'} €")
        if filters.get("distance_km") is not None:
            bits.append(f"within {filters['distance_km']} km of ZIP")
        if filters.get("psc"):
            bits.append(f"ZIP {_em(str(filters['psc']))}")
        return "🔎 Filters: " + " | ".join(bits)

    async def _filters_status(self, chat_id: int) -> str:
        prefs = await self.store.get_prefs(chat_id)
        return self._filters_summary(prefs.filters if prefs else {})

    async def _search(self, chat_id: int, arg: str) -> str:
        if not arg:
            return "/search name: criteria — e.g. `/search highway: diesel under 200k km`"
        name_part, sep, criteria = arg.partition(":")
        name = name_part.strip()
        criteria = criteria.strip()
        if not name or not criteria:
            return "I need `name: criteria` — e.g. `/search highway: diesel under 200k km`."
        prefs = await self.store.get_prefs(chat_id)
        filters = prefs.filters if prefs else {}
        min_score = prefs.min_score if prefs else None
        await self.store.save_search(chat_id, name, criteria, filters=filters, min_score=min_score)
        return (
            f"✅ Saved search “{_em(name)}”: <i>{_em(criteria)}</i>"
            f"\n{self._filters_summary(filters)}\nScore: {min_score or '—'}"
        )

    async def _searches(self, chat_id: int) -> tuple[str, dict] | str:
        prefs = await self.store.get_prefs(chat_id)
        searches = await self.store.list_searches(chat_id)
        if not searches:
            return "You have no saved searches. `/search name: diesel under 200 km`."
        lines = ["🔎 <b>Saved searches</b> (🔔 = alerts on):"]
        keyboard: list[list[dict]] = []
        for s in searches:
            bell = "🔔" if s.notify_enabled else "🔕"
            lines.append(
                f"• {bell} <b>{_em(s.name)}</b>: {_em(s.criteria)}"
                + (f" (filters: {self._filters_summary(s.filters)})" if s.filters else "")
            )
            keyboard.append(
                [
                    {"text": f"▶️ {s.name}", "callback_data": f"use:{s.name}"},
                    {
                        "text": f"{'🔕 Mute' if s.notify_enabled else '🔔 Unmute'}",
                        "callback_data": f"ntf:{s.name}",
                    },
                ]
            )
        global_on = bool(prefs.notify_enabled) if prefs else True
        keyboard.append(
            [
                {
                    "text": f"{'🔕 Mute all' if global_on else '🔔 Unmute all'}",
                    "callback_data": f"ntfglobal:{0 if global_on else 1}",
                }
            ]
        )
        lines.append("\n<i>Buttons toggle alerts, ▶️ activates the profile.</i>")
        return "\n".join(lines), {"inline_keyboard": keyboard}

    async def _toggle_search_notify(
        self, chat_id: int, name: str, cb_query_id: str
    ) -> None:
        searches = await self.store.list_searches(chat_id)
        match = next((s for s in searches if s.name == name), None)
        if match is None:
            await self.notifier.answer_callback(cb_query_id, "Search not found")
            return
        await self.store.set_search_notify(chat_id, name, not match.notify_enabled)
        state = "on 🔔" if not match.notify_enabled else "off 🔕"
        await self.notifier.answer_callback(cb_query_id, f"“{name}”: {state}")

    async def _toggle_global_notify(self, chat_id: int, on: bool, cb_query_id: str) -> None:
        await self.store.ensure_prefs(chat_id)
        await self.store.update_prefs(chat_id, notify_enabled=on)
        await self.notifier.answer_callback(
            cb_query_id, "Alerts on 🔔" if on else "Alerts off 🔕"
        )

    async def _use(self, chat_id: int, name: str) -> str:
        if not name:
            return "/use name"
        searches = await self.store.list_searches(chat_id)
        for s in searches:
            if s.name.lower() == name.lower():
                updates: dict = {"criteria": s.criteria, "filters": s.filters or {}}
                if s.min_score is not None:
                    updates["min_score"] = s.min_score
                await self.store.update_prefs(chat_id, **updates)
                reply = f"✅ Activated “{_em(s.name)}”: <i>{_em(s.criteria)}</i>"
                if s.filters:
                    reply += "\n" + self._filters_summary(s.filters)
                if s.min_score is not None:
                    reply += f"\n⭐ Score: {s.min_score}/100"
                try:
                    await self._queue_rehodnotenie(chat_id)
                except Exception:
                    logger.warning("rehodnotenie queue failed", exc_info=True)
                return reply
        return f"Couldn't find “{_em(name)}”. /searches"

    async def _model(self, chat_id: int, model: str) -> str:
        """/model — show current, /model info ID — detail, /model ID — set."""
        from bazcar.llm.provider import list_models

        models = {m["id"]: m for m in await list_models()}

        # /model info <ID> — full detail of a specific model
        arg = model.strip()
        if arg.lower().startswith("info "):
            target = arg.split(maxsplit=1)[1].strip()
            return self._model_detail(models, target, is_current=False)
        if arg.startswith("info") and len(arg.split()) == 1:
            return "Usage: <code>/model info ID</code> — e.g. <code>/model info ~deepseek/deepseek-v4-flash-latest</code>"

        prefs = await self.store.get_prefs(chat_id)
        current = prefs.model or load_llm_config().model if prefs else load_llm_config().model

        # /model <ID> — switch
        if arg:
            if arg not in models:
                return (
                    f"Unknown model <code>{_em(arg)}</code> (must support JSON).\n"
                    "List: <code>/models</code>, filter: <code>/models deepseek</code>"
                )
            await self.store.update_prefs(chat_id, model=arg)
            self._schedule_recheck(chat_id)  # silent: re-evaluate under new model
            return (
                f"✅ Model: <code>{_em(arg)}</code>\n\n"
                + self._model_detail(models, arg, is_current=True)
            )

        # /model — current model detail
        return self._model_detail(models, current, is_current=True)

    @staticmethod
    def _model_detail(models: dict, model_id: str, *, is_current: bool) -> str:
        m = models.get(model_id)
        if m is None:
            return f"Model <code>{_em(model_id)}</code> is not in the JSON-capable list (may be disabled)."
        rpm = m["rpm"] if m["rpm"] else "∞ (no limit) ✅"
        ctx = f"{m['ctx']:,}".replace(",", " ") if m["ctx"] else "?"
        price = "0 (free)" if m["free"] else f"${m['price_in']} / 1M input tokens, ${m['price_out']} / 1M output"
        head = "<b>⬆️ Current model</b>\n\n" if is_current else "<b>Model detail</b>\n\n"
        flags = []
        if m["rpm"] is None:
            flags.append("⚡ no rate limit (good for batches)")
        elif m["rpm"] and m["rpm"] < 60:
            flags.append("⚠️ slow rate limit — backfill may take a while")
        if m["free"]:
            flags.append("🆓 free")
        return (
            head
            + f"<b>{_em(m['name'])}</b>\n"
            f"<code>{_em(m['id'])}</code>\n\n"
            f"💰 {price}\n"
            f"⚡ Speed: {rpm}\n"
            f"🔡 Context: {ctx}\n\n"
            + ("".join(f"{f}\n" for f in flags) if flags else "")
            + ("\nSwitch: <code>/model ID</code>, list: <code>/models</code>" if is_current else "")
        )

    def _schedule_recheck(self, chat_id: int) -> None:
        """Trigger a re-score under the new model (best-effort, async fire-and-forget)."""
        async def _go() -> None:
            try:
                await self._queue_rehodnotenie(chat_id)
            except Exception:
                logger.warning("re-score after model change failed", exc_info=True)

        asyncio.ensure_future(_go())

    async def _score(self, chat_id: int, arg: str) -> str:
        if not arg:
            prefs = await self.store.get_prefs(chat_id)
            return f"Min. score: <b>{prefs.min_score if prefs else None}</b>"
        try:
            value = int(arg)
            if not 0 <= value <= 100:
                raise ValueError
        except ValueError:
            return "Value 0–100, e.g. `/score 75`."
        await self.store.update_prefs(chat_id, min_score=value)
        await _dispatch_scrape()
        return f"✅ From now on I'll show listings from <b>{value}/100</b>. Send /show for an overview."

    async def _photos(self, chat_id: int, arg: str) -> str:
        arg = arg.strip().lower()
        if arg not in {"on", "off"}:
            return "/photos on|off"
        on = arg == "on"
        await self.store.update_prefs(chat_id, show_photo=on)
        return "✅ Photos with listings: " + ("on" if on else "off")

    async def _queue_rehodnotenie(
        self, chat_id: int, max_listings: int = 100, days: int = 3
    ) -> bool:
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
            await repo.create_eval_task(p_key, prefs.criteria, model, max_listings, days)
        finally:
            await repo.close()
        await _dispatch_scrape()
        return True

    async def _eval(self, chat_id: int, arg: str) -> str:
        limit, days = parse_eval_args(arg)
        # estimate: how many ads in scope still lack an evaluation?
        to_eval, total = 0, 0
        settings = get_settings()
        prefs = await self.store.get_prefs(chat_id)
        if prefs is not None and settings.database_url:
            model = prefs.model or load_llm_config().model
            p_key = profile_key(prefs.criteria, model)
            repo = ListingRepository(settings.database_url)
            await repo.connect()
            try:
                rows = await repo.fetch_recent_listings(days=days, limit=limit)
                cached = await repo.fetch_evaluations(p_key, [r["ad_id"] for r in rows])
                total, to_eval = len(rows), sum(1 for r in rows if r["ad_id"] not in cached)
            finally:
                await repo.close()

        created = await self._queue_rehodnotenie(chat_id, max_listings=limit, days=days)
        if not created:
            return "⏳ Already re-scoring this search — send /show in a few minutes."

        if to_eval == 0:
            return "✅ All cars in range are already scored for this search+model — nothing to score. /show"
        minutes = max(1, round(to_eval * EST_SECONDS_PER_EVAL / 60))
        return (
            f"🔁 <b>Re-score queued</b>\n"
            f"Range: {total} cars (last {days} days), "
            f"<b>{to_eval}</b> of them unscored for this search+model.\n"
            f"⏱️ Estimate: <b>~{minutes} min</b> (~{EST_SECONDS_PER_EVAL}s/car)\n"
            f"➡️ When done you'll get a ✅ report and <code>/show</code> shows the results."
        )

    async def _show(self, chat_id: int, arg: str) -> str:
        """List recently seen listings matching the active search profile.

        Sends the top matches as photo notifications (same look as deal
        alerts). Uses the active profile's stored evaluations and the current
        ``/score`` threshold.
        """
        settings = get_settings()
        if not settings.database_url or not settings.telegram_bot_token:
            return "⚠️ Missing database or Telegram token — /show unavailable."
        prefs = await self.store.get_prefs(chat_id)
        if prefs is None:
            return "No settings yet. /help"
        arg_limit, days = parse_show_args(arg)
        limit = arg_limit or max(1, min(prefs.show_limit, 20))
        if arg_limit is not None and arg_limit != prefs.show_limit:
            await self.store.update_prefs(chat_id, show_limit=arg_limit)  # remember default
        model = prefs.model or (load_llm_config().model)
        p_key = profile_key(prefs.criteria, model)
        min_score = prefs.min_score or 0

        repo = ListingRepository(settings.database_url)
        await repo.connect()
        try:
            total = await repo.count_recent_matches(p_key, min_score, days=days)
            rows = await repo.fetch_recent_matches(p_key, min_score, days=days, limit=limit)
            if not rows:
                # transparency: what is the best achieved score for this profile?
                best = await repo.fetch_evaluations_max_score(p_key)
            if rows:
                # /show counts as seen — the deal-alert sweep must not re-send these
                await repo.record_notifications(chat_id, p_key, [r["ad_id"] for r in rows])
        finally:
            await repo.close()
        if not rows:
            hint = ""
            if best is not None:
                hint = f"\nBest score reached: {best}/100 — try /score {max(best - 10, 0)}."
            return (
                f"No matches (score >= {min_score}) in the last {days} days.{hint}\n"
                "Change /criteria or /score, or widen the window: /show 20 days 7."
            )
        listings = [_row_to_listing(row) for row in rows]
        async with TelegramNotifier(settings.telegram_bot_token, str(chat_id)) as notifier:
            await notifier.send_listings_with_photos(listings, chat_id=chat_id)
        extra = total - len(listings)
        tail = f" ({extra} more matches — raise the limit: /show 20)" if extra > 0 else ""
        return (
            f"📤 Sent {len(listings)} of {total} matches "
            f"(score >= {min_score}, last {days} days){tail}:"
        )

    # ------------------------------------------------------------ new commands

    async def _profile_key_for(self, chat_id: int) -> tuple[str, Any]:
        """(profile_key, prefs) for the calling user's active search profile."""
        prefs = await self.store.get_prefs(chat_id)
        model = (prefs.model if prefs else None) or load_llm_config().model
        criteria = prefs.criteria if prefs else None
        return profile_key(criteria, model), prefs

    async def _cars(self, chat_id: int, arg: str) -> tuple[str, dict] | str:
        """Browse the whole collected catalog — evaluated or not (/cars)."""
        settings = get_settings()
        if not settings.database_url:
            return "⚠️ /cars needs a database (BAZCAR_DATABASE_URL)."
        status, sort, days = parse_cars_args(arg)
        p_key, _ = await self._profile_key_for(chat_id)
        repo = ListingRepository(settings.database_url)
        await repo.connect()
        try:
            total = await repo.count_catalog(p_key, days, status)
            rows = await repo.fetch_catalog(p_key, days, CARS_PAGE_SIZE, 0, sort, status)
        finally:
            await repo.close()
        if not rows:
            return (
                f"📭 Catalog is empty ({_STATUS_LABELS[status]}, {days} days).\n"
                "Try a wider window (<code>/cars days 30</code>) or wait for the next scrape."
            )
        text = build_cars_page(rows, total=total, offset=0, days=days, status=status, sort=sort)
        return text, cars_keyboard(status, sort, days, 0, total)

    async def _cars_page(
        self, chat_id: int, message_id: int | None, cb_query_id: str, data: str
    ) -> None:
        """Pager callback: re-render the catalog page by editing the message."""
        parsed = parse_cars_callback(data)
        if parsed is None or message_id is None:
            await self.notifier.answer_callback(cb_query_id, "Invalid page")
            return
        status, sort, days, offset = parsed
        settings = get_settings()
        if not settings.database_url:
            await self.notifier.answer_callback(cb_query_id, "No database")
            return
        p_key, _ = await self._profile_key_for(chat_id)
        repo = ListingRepository(settings.database_url)
        await repo.connect()
        try:
            total = await repo.count_catalog(p_key, days, status)
            rows = await repo.fetch_catalog(
                p_key, days, CARS_PAGE_SIZE, offset, sort, status
            )
        finally:
            await repo.close()
        if not rows:
            await self.notifier.answer_callback(cb_query_id, "No more pages")
            return
        text = build_cars_page(
            rows, total=total, offset=offset, days=days, status=status, sort=sort
        )
        await self.notifier.edit_message_text(
            text,
            message_id,
            chat_id=chat_id,
            reply_markup=cars_keyboard(status, sort, days, offset, total),
        )
        await self.notifier.answer_callback(cb_query_id)

    async def _car(self, chat_id: int, arg: str) -> str | None:
        """Rich detail card for one ad: /car <ad_id>."""
        settings = get_settings()
        if not settings.database_url:
            return "⚠️ /car needs a database."
        try:
            ad_id = int(arg.strip())
        except ValueError:
            return "Usage: <code>/car 195357798</code> (ad_id from /cars or a notification)"
        p_key, prefs = await self._profile_key_for(chat_id)
        repo = ListingRepository(settings.database_url)
        await repo.connect()
        try:
            row = await repo.fetch_listing_by_id(ad_id, p_key)
            if row is None:
                return f"Listing <code>{ad_id}</code> is not in my database. Try /cars."
            history = await repo.fetch_price_history(ad_id)
            make = extract_make(str(row["title"]))
            bench = await repo.price_benchmark(make, row.get("year")) if make else None
        finally:
            await repo.close()
        text = build_car_card(row, history, bench)
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "💾 Save", "callback_data": f"save:{ad_id}"},
                    {"text": "🔗 Open listing", "url": row["url"]},
                ]
            ]
        }
        if prefs and prefs.show_photo and row.get("image_urls"):
            await self.notifier.send_photo(
                row["image_urls"][0],
                caption=text[:1024],
                chat_id=chat_id,
                reply_markup=keyboard,
            )
        else:
            await self.notifier.send_message(text, chat_id=chat_id, reply_markup=keyboard)
        return None

    async def _queue(self, chat_id: int) -> tuple[str, dict] | str:
        """Live view of the AI evaluation queue (/queue)."""
        settings = get_settings()
        if not settings.database_url:
            return "⚠️ /queue needs a database."
        repo = ListingRepository(settings.database_url)
        await repo.connect()
        try:
            pending = await repo.pending_eval_tasks()
            finished = await repo.recent_finished_eval_tasks()
        finally:
            await repo.close()
        text = build_queue_text(pending, finished)
        keyboard = {
            "inline_keyboard": [
                [{"text": "🔄 Process now", "callback_data": "menu:rescrape"}]
            ]
        }
        return text, keyboard

    async def _stats(self, chat_id: int) -> str:
        """Catalog + market statistics with sparkline and score bands (/stats)."""
        settings = get_settings()
        if not settings.database_url:
            return "⚠️ /stats needs a database."
        p_key, _ = await self._profile_key_for(chat_id)
        repo = ListingRepository(settings.database_url)
        await repo.connect()
        try:
            stats = await repo.stats_overview(p_key)
        finally:
            await repo.close()
        return build_stats_text(stats, p_key)

    async def _health(self, chat_id: int) -> str:
        """Pipeline health: last scrape run, queue, database (/health)."""
        settings = get_settings()
        if not settings.database_url:
            return "⚠️ /health needs a database."
        repo = ListingRepository(settings.database_url)
        await repo.connect()
        try:
            run = await repo.latest_scrape_run()
            pending = await repo.pending_eval_tasks()
            try:
                cost = await repo.eval_cost(None, 30)
                spend = float(cost.get("total", {}).get("cost_usd") or 0)
            except Exception:
                logger.warning("health cost lookup failed", exc_info=True)
                spend = None
        finally:
            await repo.close()
        lines = ["<b>🩺 Pipeline health</b>", ""]
        lines.append("🗄 Database: ✅ connected")
        budget = getattr(settings, "monthly_budget_usd", None)
        if spend is not None:
            if budget is not None:
                state = "⏸️ paused (budget)" if spend >= budget else "✅"
                lines.append(
                    f"💵 AI last 30 days: <b>{spend:.4f} $</b> / {budget} $ {state}"
                )
            else:
                lines.append(f"💵 AI last 30 days: <b>{spend:.4f} $</b> (no cap)")
        if run:
            age = time_ago(run.get("finished_at") or run.get("started_at"))
            lines.append(f"🔄 Last run: <b>{age}</b>")
            lines.append(
                f"   {run.get('pages', 0)} pages, {run.get('total', 0)} found, "
                f"{run.get('inserted', 0)} new, {run.get('price_drops', 0)} discounts"
            )
            lines.append(
                f"   {run.get('evaluated', 0)} scored, "
                f"{run.get('notified', 0)} notifications"
            )
            if run.get("error"):
                lines.append(f"   ⚠️ error: {_em(run['error'])}")
        else:
            lines.append("🔄 Last run: <b>none recorded</b> — cron hasn't run yet?")
        running = sum(1 for t in pending if t.get("started_at"))
        lines.append(
            f"⏳ AI queue: {len(pending)} waiting ({running} running) — /queue"
        )
        lines.append("")
        lines.append("<i>Cron: every ~15 min (GitHub Actions).</i>")
        return "\n".join(lines)

    def _templates(self) -> tuple[str, dict]:
        """Preset search profiles with one-tap apply (/templates)."""
        lines = ["<b>📦 Ready-made searches</b> — tap and start hunting:", ""]
        buttons = []
        for template in TEMPLATES:
            lines.append(f"{template.emoji} <b>{_em(template.name)}</b> — {_em(template.description)}")
            buttons.append(
                [{"text": f"{template.emoji} {template.name}", "callback_data": f"tpl:{template.key}"}]
            )
        lines.append("")
        lines.append("<i>Sets criteria + filters + score and starts a re-score.</i>")
        return "\n".join(lines), {"inline_keyboard": buttons}

    async def _apply_template(self, chat_id: int, key: str, cb_query_id: str) -> None:
        """Template button: set criteria+filters+score, then queue a re-eval."""
        template = get_template(key)
        if template is None:
            await self.notifier.answer_callback(cb_query_id, "Unknown template")
            return
        await self.store.update_prefs(
            chat_id,
            criteria=template.criteria,
            filters=template.filters,
            min_score=template.min_score,
        )
        try:
            await self._queue_rehodnotenie(chat_id)
        except Exception:
            logger.warning("template re-eval queue failed", exc_info=True)
        await self.notifier.answer_callback(cb_query_id, f"{template.emoji} Activated!")
        await self.reply(
            chat_id,
            f"{template.emoji} <b>{_em(template.name)}</b> active:\n"
            f"<i>{_em(template.criteria)}</i>\n"
            f"{self._filters_summary(template.filters)}\n"
            f"⭐ Min. score: {template.min_score}\n\n"
            "➡️ Re-scoring is running — results: /show, browse: /cars",
        )

    async def _menu_action(self, chat_id: int, action: str, cb_query_id: str) -> None:
        """Main-menu button routing — runs the same logic as the commands."""
        await self.notifier.answer_callback(cb_query_id)
        if action == "rescrape":
            ok = await _dispatch_scrape()
            await self.reply(
                chat_id,
                "🔄 Started processing the queue." if ok
                else "⚠️ Can't start (missing GITHUB_WORKFLOW_TOKEN) — wait for the cron.",
            )
            return
        result: str | tuple | None
        if action == "cars":
            result = await self._cars(chat_id, "")
        elif action == "show":
            result = await self._show(chat_id, "")
        elif action == "stats":
            result = await self._stats(chat_id)
        elif action == "queue":
            result = await self._queue(chat_id)
        elif action == "templates":
            result = self._templates()
        elif action == "saved":
            result = await self._saved(chat_id)
        elif action in {"settings", "status"}:
            result = await self._settings(chat_id)
        elif action == "dashboard":
            result = self._dashboard()
        elif action == "help":
            result = HELP_TEXT, MENU_KEYBOARD
        else:
            result = HELP_TEXT, MENU_KEYBOARD
        if isinstance(result, tuple):
            text_out, markup = result
            await self.reply(chat_id, text_out, reply_markup=markup)
        else:
            await self.reply(chat_id, result)

    async def _export(self, chat_id: int, arg: str) -> str | None:
        """CSV export of the catalog (active profile's scores) via sendDocument."""
        settings = get_settings()
        if not settings.database_url:
            return "⚠️ /export needs a database."
        days = 30
        if arg.strip().isdigit():
            days = max(1, min(int(arg.strip()), 365))
        p_key, _ = await self._profile_key_for(chat_id)
        repo = ListingRepository(settings.database_url)
        await repo.connect()
        try:
            rows = await repo.fetch_catalog(p_key, days, limit=200, sort="newest")
        finally:
            await repo.close()
        if not rows:
            return f"📭 Nothing to export for the last {days} days."
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";")
        writer.writerow(
            ["ad_id", "title", "price_eur", "year", "mileage_km", "city",
             "published", "score", "url"]
        )
        for row in rows:
            writer.writerow([
                row["ad_id"],
                row["title"],
                row.get("price_eur") or "",
                row.get("year") or "",
                row.get("mileage_km") or "",
                row.get("city") or "",
                str(row.get("published_date") or ""),
                row.get("score") if row.get("score") is not None else "",
                row["url"],
            ])
        content = buffer.getvalue().encode("utf-8-sig")
        filename = f"bazcar_{days}d_{datetime.now(UTC).strftime('%Y%m%d')}.csv"
        ok = await self.notifier.send_document(
            filename,
            content,
            chat_id=chat_id,
            caption=f"📄 Export: {len(rows)} listings from {days} days",
        )
        return None if ok else "⚠️ Couldn't send the export."

    def _dashboard(self) -> tuple[str, dict] | str:
        """Mini App button (/dashboard) — web_app opens inside Telegram."""
        settings = get_settings()
        url = settings.webapp_url
        if not url:
            vercel = os.environ.get("VERCEL_URL")
            if vercel:
                url = f"https://{vercel}/api/dashboard"
        if not url:
            return (
                "📱 Dashboard is not deployed. Deploy the project to Vercel and set "
                "<code>BAZCAR_WEBAPP_URL</code> (or VERCEL_URL is used automatically)."
            )
        keyboard = {
            "inline_keyboard": [
                [{"text": "📱 Open dashboard", "web_app": {"url": url}}]
            ]
        }
        return "📱 <b>Bazcar dashboard</b> — charts, inventory, queue and stats:", keyboard

    async def _models(self, chat_id: int, arg: str = "") -> str:
        from bazcar.llm.provider import list_models

        models = await list_models()
        query = arg.strip().lower()
        if query:
            models = [m for m in models if query in m["id"].lower()]
        free = [m for m in models if m["free"]]
        paid = [m for m in models if not m["free"]]
        free.sort(key=lambda m: m["id"])
        paid.sort(key=lambda m: m["id"])

        def _line(m: dict) -> str:
            rpm = m["rpm"] if m["rpm"] else "∞"
            ctx = f"{m['ctx'] // 1000}k" if m["ctx"] else "?"
            price = "$0" if m["free"] else f"${m['price_in']}/M"
            return (
                f"  • <code>{_em(m['id'])}</code>\n"
                f"    ─ {price} · ⚡{rpm}/min · 🔡{ctx}"
            )

        def _block(title: str, items: list[dict], cap: int) -> list[str]:
            shown = [_line(m) for m in items[:cap]]
            if len(items) > cap:
                shown.append(f"  • … +{len(items) - cap} ďalších")
            return [f"<b>{title}</b>", *shown]

        cap = 8 if query else 6
        lines: list[str] = ["<b>🤖 Models (JSON-capable)</b>", ""]
        if not models:
            lines.append("No model matches the filter.")
            lines.append("Try: /models gemini · /models deepseek · /models llama · /models flash")
            return "\n".join(lines)
        lines += _block("🆓 Free", free[:cap], cap)
        lines.append("")
        lines += _block("💰 Paid", paid[:cap], cap)
        lines.append("")
        lines.append("<i>⚡ = speed (requests/min, ∞ = no limit), 🔡 = context, $/M = price per 1M input tokens</i>")
        lines.append("Set: <code>/model ID</code> · filter: <code>/models text</code> · detail: <code>/model info ID</code>")
        return "\n".join(lines)

    async def _save(self, chat_id: int, arg: str) -> str:
        try:
            ad_id = int(arg)
        except ValueError:
            return "/save ad_id"
        title, url = await self._resolve_listing_meta(ad_id)
        await self.store.save_listing(chat_id, ad_id, title, url)
        return f"💾 Saved ad {ad_id}."

    async def _resolve_listing_meta(self, ad_id: int) -> tuple[str | None, str | None]:
        """Title/url for a saved ad: live DB first, export file as fallback.

        The export-file path only works on long-lived hosts; on serverless
        (Vercel) the export dir is empty, so the database is authoritative.
        """
        settings = get_settings()
        if settings.database_url:
            try:
                repo = ListingRepository(settings.database_url)
                await repo.connect()
                try:
                    row = await repo.fetch_listing_by_id(ad_id, "")
                finally:
                    await repo.close()
                if row:
                    return row.get("title"), row.get("url")
            except Exception:
                logger.warning("save title lookup in db failed", exc_info=True)
        listing = await self._find_listing(ad_id)
        if listing is not None:
            return listing.title, listing.url
        return None, None

    async def _saved(self, chat_id: int) -> str:
        saved = await self.store.list_saved(chat_id)
        if not saved:
            return "You have nothing saved."
        lines = ["💾 <b>Saved listings</b> (🔔 = watching discounts automatically):"]
        for item in saved:
            title = item.ad_title or f"ad {item.ad_id}"
            head = (
                f"• <a href=\"{_em_url(item.ad_url)}\">{_em(title)}</a>"
                if item.ad_url
                else f"• {_em(title)}"
            )
            bits = []
            if item.current_price is not None:
                try:
                    bits.append(f"💰 {fmt_eur(int(item.current_price))}")
                except (TypeError, ValueError):
                    pass
            if item.max_price and item.current_price is not None:
                try:
                    if float(item.max_price) > float(item.current_price) > 0:
                        pct = round((1 - float(item.current_price) / float(item.max_price)) * 100)
                        bits.append(f"🔔 −{pct} % z maxima")
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
            if item.alert_below:
                try:
                    bits.append(f"🎯 watching below {fmt_eur(int(item.alert_below))}")
                except (TypeError, ValueError):
                    pass
            if item.note:
                note = item.note if len(item.note) <= 80 else item.note[:77] + "…"
                bits.append(f"📝 {_em(note)}")
            lines.append(head + ("\n  " + " · ".join(bits) if bits else ""))
        lines.append("\n<i>/note ad_id text — note · /alert ad_id price — watch price · /unsave ad_id — remove</i>")
        return "\n".join(lines)

    async def _note(self, chat_id: int, arg: str) -> str:
        parts = (arg or "").split(maxsplit=1)
        if not parts or not parts[0].lstrip("-").isdigit():
            return "Usage: <code>/note ad_id your note</code>"
        ad_id = int(parts[0])
        note = parts[1].strip()[:500] if len(parts) > 1 else ""
        updated = await self.store.set_saved_note(chat_id, ad_id, note)
        if not updated:
            return f"Ad {ad_id} is not in your saved list. First <code>/save {ad_id}</code>."
        return f"📝 Note for {ad_id} saved." if note else f"📝 Note for {ad_id} cleared."

    async def _unsave(self, chat_id: int, arg: str) -> str:
        try:
            ad_id = int(arg)
        except ValueError:
            return "/unsave ad_id"
        removed = await self.store.remove_saved(chat_id, ad_id)
        return "Removed ✅" if removed else f"Ad {ad_id} is not in your saved list."

    async def _alert(self, chat_id: int, arg: str) -> str:
        """Per-ad price watch: /alert <ad_id> [price|off] (empty = show)."""
        parts = (arg or "").split(maxsplit=1)
        if not parts or not parts[0].lstrip("-").isdigit():
            return "Usage: <code>/alert ad_id [price|off]</code> — e.g. <code>/alert 195315165 3000</code>"
        ad_id = int(parts[0])
        saved = await self.store.list_saved(chat_id)
        if all(s.ad_id != ad_id for s in saved):
            return f"Ad {ad_id} is not in your saved list. First <code>/save {ad_id}</code>."
        if len(parts) == 1:
            current = next(s.alert_below for s in saved if s.ad_id == ad_id)
            return (
                f"🔔 Ad {ad_id}: watching below <b>{fmt_eur(current)}</b>."
                if current
                else f"🔔 Ad {ad_id}: I'll report every discount. Set a threshold: <code>/alert {ad_id} price</code>"
            )
        raw = parts[1].strip().lower()
        if raw in {"off", "0"}:
            await self.store.set_saved_alert(chat_id, ad_id, None)
            return f"🔔 Ad {ad_id}: threshold cleared — I'll report every discount."
        try:
            below = int(raw.replace(" ", "").replace("€", ""))
        except ValueError:
            return "Price must be a number — e.g. <code>/alert 195315165 3000</code>"
        if below <= 0:
            return "Price must be positive."
        await self.store.set_saved_alert(chat_id, ad_id, below)
        return f"🔔 Ad {ad_id}: I'll alert once the price drops below <b>{fmt_eur(below)}</b>."

    async def _digest(self, chat_id: int) -> str:
        """On-demand weekly summary: new ads, drops, top matches, queue."""
        settings = get_settings()
        if not settings.database_url:
            return "⚠️ /digest needs a database."
        p_key, prefs = await self._profile_key_for(chat_id)
        min_score = (prefs.min_score if prefs and prefs.min_score is not None else None)
        if min_score is None:
            min_score = settings.telegram_min_score or 0
        repo = ListingRepository(settings.database_url)
        await repo.connect()
        try:
            week = await repo.new_by_day(7)
            drops = await repo.drops_leaderboard(3, 7)
            total_matches = await repo.count_recent_matches(p_key, min_score, 7)
            top = await repo.fetch_recent_matches(p_key, min_score, 7, 3)
            pending = await repo.pending_eval_tasks()
        finally:
            await repo.close()
        lines = ["<b>📬 Weekly digest</b>"]
        lines.append(f"🆕 New listings in 7 days: <b>{sum(n for _, n in week)}</b>")
        lines.append(f"📉 Discounts in 7 days: <b>{len(drops)}</b>")
        for d in drops:
            lines.append(
                f"  −{d['pct']} % <a href=\"{_em_url(d['url'])}\">{_em(d['title'])}</a> "
                f"({fmt_eur(d['prev'])} → {fmt_eur(d['now_price'])})"
            )
        lines.append(f"⭐ Matching your profile (7 days): <b>{total_matches}</b>")
        for m in top:
            lines.append(
                f"  {m.get('score', '?')}/100 <a href=\"{_em_url(m['url'])}\">{_em(m['title'])}</a>"
            )
        lines.append(
            f"⏳ In the AI queue: <b>{len(pending)}</b>"
            + (" — the cron will process it." if pending else " ✅")
        )
        return "\n".join(lines)

    @staticmethod
    def _settings_keyboard(prefs) -> dict:
        """Inline controls for the /settings hub (score ±5, photos, alerts)."""
        score = prefs.min_score if prefs and prefs.min_score is not None else 60
        photos_on = bool(prefs.show_photo) if prefs else False
        notify_on = bool(prefs.notify_enabled) if prefs else True
        return {
            "inline_keyboard": [
                [
                    {"text": "⭐ −5", "callback_data": "set:score:-5"},
                    {"text": f"⭐ Score: {score}", "callback_data": "set:noop"},
                    {"text": "⭐ +5", "callback_data": "set:score:+5"},
                ],
                [
                    {
                        "text": f"📸 Photos: {'on' if photos_on else 'off'}",
                        "callback_data": "set:photos",
                    },
                    {
                        "text": f"🔔 Alerts: {'on' if notify_on else 'off'}",
                        "callback_data": "set:notify",
                    },
                ],
                [
                    {"text": "🔍 My searches", "callback_data": "set:searches"},
                ],
            ]
        }

    async def _settings_text(self, chat_id: int) -> str:
        prefs = await self.store.get_prefs(chat_id)
        if prefs is None:
            return "No settings yet. /help"
        model = prefs.model or load_llm_config().model
        p_key = profile_key(prefs.criteria, model)
        lines = [
            "<b>⚙️ Settings & status</b>",
            f"Criteria: {_em(prefs.criteria or '—')}",
            f"Model: {_em(model)}",
            f"Min. score: {_em(str(prefs.min_score if prefs.min_score is not None else '—'))}",
            f"Photos: {'on' if prefs.show_photo else 'off'}",
            f"Alerts: {'🔔 on' if prefs.notify_enabled else '🔕 off'}",
            self._filters_summary(prefs.filters),
            f"Profile: <code>{p_key}</code>",
        ]
        budget = getattr(get_settings(), "monthly_budget_usd", None)
        if budget is not None:
            lines.append(f"💵 Monthly AI budget: <b>{budget} $</b>")
        if get_settings().database_url:
            try:
                repo = ListingRepository(get_settings().database_url)
                await repo.connect()
                try:
                    if await repo.pending_eval_task_for(p_key):
                        lines.append("⏳ Re-scoring this search is running (try /show in a few).")
                    else:
                        best = await repo.fetch_evaluations_max_score(p_key)
                        n = await repo.count_evaluations(p_key, days=3)
                        tail = f", best score {best}/100" if best is not None else ""
                        lines.append(f"✅ Cars scored (3 days): {n}" + tail)
                finally:
                    await repo.close()
            except Exception:
                lines.append("⚠️ Database unavailable")
        return "\n".join(lines)

    async def _settings(self, chat_id: int) -> tuple[str, dict]:
        prefs = await self.store.ensure_prefs(chat_id)
        return await self._settings_text(chat_id), self._settings_keyboard(prefs)

    async def _settings_action(
        self, chat_id: int, message_id: int | None, cb_query_id: str, payload: str
    ) -> None:
        """Handle /settings hub button presses (edit the message in place)."""
        prefs = await self.store.ensure_prefs(chat_id)
        toast = ""
        if payload == "score:-5" or payload == "score:+5":
            current = prefs.min_score if prefs.min_score is not None else 60
            value = max(0, min(100, current + (5 if payload.endswith("+5") else -5)))
            await self.store.update_prefs(chat_id, min_score=value)
            toast = f"Min. score: {value}/100"
        elif payload == "photos":
            await self.store.update_prefs(chat_id, show_photo=not prefs.show_photo)
            toast = f"Photos {'on' if not prefs.show_photo else 'off'}"
        elif payload == "notify":
            await self.store.update_prefs(chat_id, notify_enabled=not prefs.notify_enabled)
            toast = "Alerts on 🔔" if not prefs.notify_enabled else "Alerts off 🔕"
        elif payload == "searches":
            text, markup = await self._searches(chat_id)
            await self.notifier.edit_message_text(
                text, message_id, chat_id=chat_id, reply_markup=markup
            )
            await self.notifier.answer_callback(cb_query_id)
            return
        elif payload == "noop":
            await self.notifier.answer_callback(cb_query_id)
            return
        else:
            await self.notifier.answer_callback(cb_query_id, "Unknown action")
            return
        text = await self._settings_text(chat_id)
        prefs = await self.store.get_prefs(chat_id)
        if message_id is not None:
            await self.notifier.edit_message_text(
                text, message_id, chat_id=chat_id,
                reply_markup=self._settings_keyboard(prefs),
            )
        await self.notifier.answer_callback(cb_query_id, toast)

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
