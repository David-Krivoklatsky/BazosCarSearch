"""Pure formatting + parsing helpers for the Telegram bot (no I/O).

Extracted from ``daemon.py`` — everything here is side-effect free and
unit-tested. Imported and re-exported by ``daemon.py``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from bazcar.core.models import DealEvaluation, Listing

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
        return "Ask"
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
        if task.get("confirmed") is False:
            est = task.get("estimate_usd")
            cost = f" (≈ ${float(est):.2f})" if est is not None else ""
            progress = f"awaiting your cost confirmation{cost}"
        elif task.get("started_at"):
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


