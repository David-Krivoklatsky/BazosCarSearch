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
BOT_COMMANDS: list[dict[str, str]] = [
    {"command": "menu", "description": "🏠 Hlavné menu — tlačidlá na všetko"},
    {"command": "cars", "description": "🚗 Všetky autá (aj bez AI hodnotenia)"},
    {"command": "car", "description": "🔍 Detail inzerátu: /car ad_id"},
    {"command": "show", "description": "⭐ Najlepšie ohodnotené ponuky"},
    {"command": "criteria", "description": "Čo hľadám — AI boduje inzeráty podľa toho"},
    {"command": "templates", "description": "📦 Hotové hľadania (rodinné, mesto, diaľnica…)"},
    {"command": "search", "description": "Uložiť hľadanie: /search meno: kritériá"},
    {"command": "searches", "description": "Moje uložené hľadania"},
    {"command": "use", "description": "Aktivovať uložené hľadanie"},
    {"command": "filter", "description": "Bazoš filtre: query, price, dist, psc"},
    {"command": "score", "description": "Min. hodnotenie inzerátov (0–100)"},
    {"command": "eval", "description": "Prehodnotiť autá podľa aktívneho searchu"},
    {"command": "queue", "description": "⏳ Fronta AI vyhodnocovania"},
    {"command": "stats", "description": "📊 Štatistiky katalógu a trhu"},
    {"command": "health", "description": "🩺 Zdravie pipeline (posledný beh)"},
    {"command": "dashboard", "description": "📱 Otvoriť webový dashboard"},
    {"command": "export", "description": "📄 Stiahnuť inzeráty ako CSV"},
    {"command": "status", "description": "Aktuálne nastavenia a stavy"},
    {"command": "photos", "description": "Fotky pri inzerátoch on/off"},
    {"command": "models", "description": "Dostupné AI modely (free/paid)"},
    {"command": "model", "description": "Zmeniť AI model"},
    {"command": "save", "description": "Uložiť inzerát na neskôr (ad_id)"},
    {"command": "saved", "description": "Moje uložené inzeráty + zľavy"},
    {"command": "note", "description": "Poznámka k uloženému: /note id text"},
    {"command": "alert", "description": "Strážiť cenu: /alert id [cena|off]"},
    {"command": "digest", "description": "📬 Týždenný súhrn noviniek"},
    {"command": "unsave", "description": "Odstrániť uložený inzerát"},
    {"command": "help", "description": "Nápoveda — /prikaz help pre detaily"},
]


# ---------------------------------------------------------------------------
# Pure formatting helpers (unit-tested, no I/O)
# ---------------------------------------------------------------------------

CARS_PAGE_SIZE = 8

# Compact tokens used in paginated /cars callback data.
_CAR_STATUS = {"a": "all", "u": "unevaluated", "r": "rated"}
_CAR_SORT = {"n": "newest", "c": "price_asc", "e": "price_desc", "s": "score"}

_STATUS_LABELS = {"all": "všetky", "unevaluated": "bez AI", "rated": "s AI"}
_SORT_LABELS = {
    "newest": "najnovšie",
    "price_asc": "najlacnejšie",
    "price_desc": "najdrahšie",
    "score": "najlepšie skóre",
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
    """Human age of a timestamp ('pred 12 min') — accepts datetime or str."""
    if dt is None:
        return "nikdy"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return str(dt)[:16]
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=UTC)
    seconds = max(0, (datetime.now(UTC) - dt).total_seconds())
    if seconds < 120:
        return "práve teraz"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"pred {minutes} min"
    hours = minutes // 60
    if hours < 24:
        return f"pred {hours} h"
    return f"pred {hours // 24} d"


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
        f"<b>🚗 Autá {offset + 1}–{offset + len(rows)} z {total}</b> "
        f"<i>({_STATUS_LABELS[status]}, {_SORT_LABELS[sort]}, {days} dní)</i>\n"
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
    tail = "\n<i>/car &lt;id&gt; — detail · /export — CSV · /dashboard — grafy</i>"
    return head + "\n".join(lines) + tail


def cars_keyboard(status: str, sort: str, days: int, offset: int, total: int) -> dict:
    """Prev/next navigation for the /cars pager (edit-in-place)."""
    buttons = []
    if offset > 0:
        prev_offset = max(0, offset - CARS_PAGE_SIZE)
        buttons.append(
            {"text": "◀️ Predošlé", "callback_data": cars_callback_encode(status, sort, days, prev_offset)}
        )
    if offset + CARS_PAGE_SIZE < total:
        next_offset = offset + CARS_PAGE_SIZE
        buttons.append(
            {"text": "Ďalšie ▶️", "callback_data": cars_callback_encode(status, sort, days, next_offset)}
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
        f"🕸 Naposledy videné pred {days} dňami — inzerát možno zmizol "
        "(predané/stiahnuté), nie je to však isté."
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
        lines.append(f"📅 Inzerát z {row['published_date']}")
    stale = stale_line(row.get("last_seen_at"))
    if stale:
        lines.append(stale)

    if row.get("score") is not None:
        lines.append("")
        lines.append(f"⭐ <b>{row['score']}/100</b> — {_em(row.get('why') or '')}")
        if row.get("risk"):
            lines.append(f"⚠️ <i>Riziko:</i> {_em(row['risk'])}")
        if row.get("condition"):
            lines.append(f"🔧 <i>Stav:</i> {_em(row['condition'])}")
        if row.get("target_offer"):
            lines.append(f"🤝 <i>Prvá ponuka:</i> {fmt_eur(row['target_offer'])}")
        if row.get("eval_model"):
            lines.append(f"<i>model: {_em(str(row['eval_model']))}</i>")
    else:
        lines.append("")
        lines.append("⏳ <i>Zatiaľ bez AI hodnotenia — zaradím cez /eval</i>")

    points = [h for h in history if h.get("price_eur") is not None]
    if len(points) >= 2:
        first, last = points[0], points[-1]
        try:
            change = round((float(last["price_eur"]) / float(first["price_eur"]) - 1) * 100)
            arrow = "📉" if change < 0 else "📈"
            lines.append(
                f"{arrow} Vývoj ceny: {fmt_eur(first['price_eur'])} → "
                f"{fmt_eur(last['price_eur'])} ({change:+d} %)"
            )
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    if bench and bench.get("n") and row.get("price_eur") is not None:
        median = bench.get("median")
        if median:
            try:
                diff = round((float(row["price_eur"]) / float(median) - 1) * 100)
                verdict = "pod mediánom" if diff < 0 else "nad mediánom"
                tag = f"n={bench['n']}"
                if bench["n"] < 3:
                    tag += ", málo dát ⚠️"
                elif bench.get("trimmed"):
                    tag += ", bez extrémov"
                lines.append(
                    f"📊 Trh ({tag}, rok ±2): medián {fmt_eur(int(median))} "
                    f"— toto {abs(diff)} % {verdict}"
                )
            except (TypeError, ValueError, ZeroDivisionError):
                pass
    lines.append(f"🆔 {row['ad_id']}")
    lines.append(f"<a href=\"{_em_url(row['url'])}\">🔗 Otvoriť inzerát</a>")
    return "\n".join(lines)


def build_stats_text(stats: dict, p_key: str) -> str:
    """Render the /stats message: totals, coverage, sparkline, score bands."""
    total = stats["total"]
    evaluated = stats["evaluated"]
    coverage = round(evaluated / total * 100) if total else 0
    lines = ["<b>📊 Štatistiky katalógu</b>", ""]
    lines.append(f"📦 Inzerátov v databáze: <b>{total}</b>")
    lines.append(
        f"🤖 Ohodnotené (tvoj profil): <b>{evaluated}</b> "
        f"{bar(evaluated, total)} {coverage} %"
    )
    price = stats.get("price") or {}
    if price.get("median") is not None:
        lines.append(
            f"💰 Ceny: medián <b>{fmt_eur(int(price['median']))}</b>, "
            f"min {fmt_eur(price.get('mn'))}, max {fmt_eur(price.get('mx'))}"
        )
    new_by_day = stats.get("new_by_day") or []
    if new_by_day:
        counts = [n for _, n in new_by_day]
        lines.append(
            f"🆕 Nové za 7 dní: <b>{sum(counts)}</b>  {sparkline(counts)} "
            f"<i>({new_by_day[0][0][5:]} – {new_by_day[-1][0][5:]})</i>"
        )
    lines.append(f"📉 Zľavy za 7 dní: <b>{stats.get('drops_7d', 0)}</b>")
    bands = stats.get("bands") or {}
    band_total = sum(bands.values())
    if band_total:
        lines.append("")
        lines.append("<b>Skóre (tvoj profil)</b>")
        for label, key in (("90+", "b90"), ("80–89", "b80"), ("70–79", "b70"),
                           ("50–69", "b50"), ("<50", "b0")):
            value = bands.get(key, 0)
            lines.append(f"  {label:>6} {bar(value, band_total, 12)} {value}")
    lines.append("")
    lines.append(f"<i>profil: {p_key} · /cars — prehľad · /dashboard — grafy</i>")
    return "\n".join(lines)


def build_queue_text(pending: list[dict], finished: list[dict]) -> str:
    """Render the /queue message: running/queued tasks + recent outcomes."""
    lines = ["<b>⏳ Fronta AI vyhodnocovania</b>", ""]
    if not pending and not finished:
        lines.append("Fronta je prázdna — všetko je ohodnotené. ✅")
    for task in pending:
        criteria = (task.get("criteria") or "bez kritérií").strip()
        label = criteria if len(criteria) <= 40 else criteria[:37] + "…"
        total = task.get("total") or 0
        done = task.get("done") or 0
        if task.get("started_at"):
            progress = f"{bar(done, total)} {done}/{total}"
        else:
            progress = "čaká v rade"
        age = time_ago(task.get("created_at"))
        attempts = task.get("attempts") or 0
        retry = f", pokus {attempts}" if attempts else ""
        lines.append(f"⏳ <i>{_em(label)}</i>\n    {progress} · {age}{retry}")
        lines.append(f"    <code>{_em(task.get('model') or '?')}</code>")
    if finished:
        lines.append("")
        lines.append("<b>Posledné dokončené</b>")
        for task in finished:
            criteria = (task.get("criteria") or "bez kritérií").strip()
            label = criteria if len(criteria) <= 40 else criteria[:37] + "…"
            age = time_ago(task.get("done_at"))
            if task.get("error"):
                lines.append(f"⚠️ <i>{_em(label)}</i> — {_em(task['error'])} ({age})")
            else:
                lines.append(
                    f"✅ <i>{_em(label)}</i> — {task.get('done') or 0}/{task.get('total') or 0} áut ({age})"
                )
    lines.append("")
    lines.append("<i>Frontu spracúva cron každých ~15 min. Zrýchli: tlačidlo nižšie.</i>")
    return "\n".join(lines)


MENU_KEYBOARD: dict = {
    "inline_keyboard": [
        [
            {"text": "🚗 Všetky autá", "callback_data": "menu:cars"},
            {"text": "⭐ Top ponuky", "callback_data": "menu:show"},
        ],
        [
            {"text": "📊 Štatistiky", "callback_data": "menu:stats"},
            {"text": "⏳ Fronta AI", "callback_data": "menu:queue"},
        ],
        [
            {"text": "📦 Šablóny", "callback_data": "menu:templates"},
            {"text": "💾 Uložené", "callback_data": "menu:saved"},
        ],
        [
            {"text": "📱 Dashboard", "callback_data": "menu:dashboard"},
        ],
        [
            {"text": "⚙️ Stav", "callback_data": "menu:status"},
            {"text": "❓ Nápoveda", "callback_data": "menu:help"},
        ],
    ]
}

COMMAND_HELP: dict[str, str] = {
    "/criteria": (
        "<b>/criteria</b> — čo hľadám\n\n"
        "AI (LLM) hodnotí každý inzerát 0–100 presne podľa tvojich kritérií — "
        "čím viac špecifikuješ, tým presnejšie skóre.\n\n"
        "<b>Použitie:</b> <code>/criteria + text</code>\n"
        "<b>Príklad:</b> <code>/criteria diesel, do 200 000 km, nad 80 kW</code>\n\n"
        "➡️ Zmena automaticky spustí prehodnotenie áut z posledných 3 dní "
        "(príde ti 🧮 hlásenie, keď bude hotové). Nové inzeráty sa scrapujú "
        "podľa aktívnych filtrov každých ~15 min."
    ),
    "/search": (
        "<b>/search</b> — uložiť pomenované hľadanie\n\n"
        "Uloží kompletný profil: kritériá + aktuálne Bazoš filtre + min. skóre.\n"
        "Môžeš mať viac searchov (diaľnica, mesto, rodinné auto…) a prepínať medzi nimi.\n\n"
        "<b>Použitie:</b> <code>/search meno: kritériá</code>\n"
        "<b>Príklad:</b> <code>/search diaľnica: diesel do 200t km</code>\n\n"
        "💡 Najprv si nastav <code>/filter</code> a <code>/score</code>, potom ulož — "
        "všetko sa zachová do profilu."
    ),
    "/searches": (
        "<b>/searches</b> — zoznam uložených hľadaní\n\n"
        "Ukáže všetky uložené profily (kritériá + filtre + skóre) aj so stavom "
        "upozornení. Tlačidlami 🔔/🔕 prepneš upozornenia pre každý profil, "
        "▶️ ho aktivuješ (to isté ako <code>/use meno</code>)."
    ),
    "/alert": (
        "<b>/alert</b> — strážiť cenu uloženého inzerátu\n\n"
        "<code>/alert 195315165 3000</code> — upozorním, až cena klesne pod 3 000 €\n"
        "<code>/alert 195315165 off</code> — späť na upozornenie pri každej zľave\n"
        "<code>/alert 195315165</code> — ukáže aktuálny prah\n\n"
        "Bez prahu upozorním na každú zľavu automaticky."
    ),
    "/digest": (
        "<b>/digest</b> — týždenný súhrn\n\n"
        "Nové inzeráty, najväčšie zľavy, top zhody s tvojím profilom a stav "
        "AI fronty za posledných 7 dní. Na vyžiadanie, kedykoľvek."
    ),
    "/use": (
        "<b>/use</b> — aktivovať uložené hľadanie\n\n"
        "Nastaví kritériá, filtre aj min. skóre z uloženého profilu a "
        "automaticky spustí prehodnotenie áut z posledných dní podľa neho.\n\n"
        "<b>Použitie:</b> <code>/use meno</code>\n"
        "<b>Príklad:</b> <code>/use dialnica</code>"
    ),
    "/status": (
        "<b>/status</b> — čo práve platí\n\n"
        "Ukáže aktívne kritériá, model, min. skóre, filtre, počet ohodnotených "
        "áut za posledné 3 dni, najlepšie skóre a či práve beží prehodnotenie."
    ),
    "/filter": (
        "<b>/filter</b> — Bazoš filtre (čo sa scrapuje)\n\n"
        "Filtrujú výsledky už na Bazoši — ovplyvňujú, ktoré inzeráty sa načítajú.\n"
        "  <code>/filter query skoda octavia</code> — hľadaný text\n"
        "  <code>/filter price 1500-4000</code> — rozsah ceny v € (aj 1500- alebo -4000)\n"
        "  <code>/filter dist 100</code> — okruh okolo PSČ v km (najprv /filter psc)\n"
        "  <code>/filter psc 81101</code> — lokalita\n"
        "  <code>/filter clear</code> — späť na default\n"
        "  <code>/filter</code> — ukáže aktívne filtre\n\n"
        "💡 Najazdené km nie je Bazoš filter — zohľadná ho AI cez kritériá."
    ),
    "/score": (
        "<b>/score</b> — min. hodnotenie pre zobrazenie\n\n"
        "Inzeráty s AI skóre pod touto hranicou ti neprídu ako notifikácie "
        "a nezobrazia sa v /show (0–100).\n\n"
        "<b>Použitie:</b> <code>/score 75</code>\n\n"
        "💡 Zmena platí okamžite na už ohodnotené autá — skús hneď /show."
    ),
    "/show": (
        "<b>/show</b> — vyhovujúce inzeráty z posledného obdobia\n\n"
        "Pošle ti najlepšie ohodnotené inzeráty (podľa aktívneho searchu a /score) "
        "ako správy s fotkou a tlačidlami. Každá správa obsahuje aj 📅 dátum inzerátu.\n\n"
        "<b>Použitie:</b>\n"
        "  <code>/show</code> — tvoj predvolený počet, okno 3 dni\n"
        "  <code>/show 20</code> — 20 áut (uloží ako nový default)\n"
        "  <code>/show days 7</code> — okno 7 dní\n"
        "  <code>/show 20 days 100</code> — kombinácia\n\n"
        "⚠️ Počet výsledkov limituje hlavne <b>/score</b> — šírka okna pridá len "
        "staršie inzeráty. Bot ti vždy vypíše, koľko vyhovujúcich celkovo našiel.\n\n"
        "💡 Ak je výsledok prázdny, bot poradí najlepšie dosiahnuté skóre."
    ),
    "/eval": (
        "<b>/eval</b> — prehodnotiť autá podľa aktívneho searchu\n\n"
        "Zaradí prehodnotenie podľa aktívnych kritérií a modelu. Už ohodnotené "
        "(rovnaký search + model) sa preskočia — nič sa nepreplatí zbytočne.\n\n"
        "<b>Použitie:</b> (rovnaké parametre ako /show)\n"
        "  <code>/eval</code> — posledných 100 áut z 3 dní\n"
        "  <code>/eval 30</code> — 30 najnovších\n"
        "  <code>/eval days 7</code> — okno 7 dní\n"
        "  <code>/eval 50 days 7</code> — kombinácia\n"
        "  <code>/eval all</code> — všetko v DB (do 500)\n\n"
        "Bot ti hneď vypíše, koľko áut sa bude vyhodnocovať a odhad času. "
        "Keď bude hotovo, príde ti ✅ hlásenie."
    ),
    "/photos": (
        "<b>/photos</b> — fotky pri inzerátoch\n\n"
        "<code>/photos on</code> — inzeráty prichádzajú ako fotka + popis + tlačidlá\n"
        "<code>/photos off</code> — len textový popis\n\n"
        "Pri každom inzeráte máš vždy tlačidlá 💾 Uložiť a 🔗 Otvoriť."
    ),
    "/models": (
        "<b>/models</b> — AI modely (len tie s JSON výstupom)\n\n"
        "Zoznam rozdelený na 🆓 Free a 💰 Paid. Pre každý model:\n"
        "  • 💰 <b>cena</b> za 1M tokenov ($/M; free = 0)\n"
        "  • ⚡ <b>Rychlosť</b> = <i>requests za minútu</i> — TOTO rozhoduje, či model stíha: "
        "∞ = bez limitu (backfill rýchly), nízke číslo = pomalý backfill (429)\n"
        "  • 🔡 kontext (veľkosť okna)\n\n"
        "<b>Použitie:</b>\n"
        "  <code>/models</code> — zoznam\n"
        "  <code>/models deepseek</code> — filtrovanie podľa textu\n"
        "  <code>/model info ID</code> — detail jedného modelu\n"
        "  <code>/model ID</code> — zmeniť model\n\n"
        "💡 Ak backfill hádže 429, prejdi na model s ⚡∞."
    ),
    "/model": (
        "<b>/model</b> — zmeniť AI model\n\n"
        "<b>Použitie:</b>\n"
        "  <code>/model</code> — ukáže aktuálny model + detail\n"
        "  <code>/model ID</code> — nastaví model a spustí prehodnotenie\n"
        "  <code>/model info ID</code> — detail konkrétneho modelu\n\n"
        "<b>Príklad:</b>\n"
        "  <code>/model ~deepseek/deepseek-v4-flash-latest</code>\n\n"
        "Key parametre (pri /model info): 💰 cena za 1M tokenov, "
        "⚡ requests/min (∞ = bez limitu), 🔡 kontext.\n"
        "Zoznam všetkých: <code>/models</code>."
    ),
    "/save": (
        "<b>/save</b> — uložiť inzerát na neskôr\n\n"
        "<code>/save 195315165</code> (ad_id z notifikácie — riadok 🆔)\n\n"
        "💡 Rýchlejšie: tlačidlo <b>💾 Uložiť</b> priamo pri inzeráte."
    ),
    "/saved": (
        "<b>/saved</b> — moje uložené inzeráty\n\n"
        "Zobrazí uložené inzeráty ako odkazy — klik a si na Bazoši. Pri každom "
        "vidíš aktuálnu cenu, zľavu oproti maximu (🔔 strážim automaticky) "
        "a tvoju poznámku.\n"
        "Odstrániš cez <code>/unsave ad_id</code>, poznámku pridáš cez "
        "<code>/note ad_id text</code>."
    ),
    "/note": (
        "<b>/note</b> — poznámka k uloženému inzerátu\n\n"
        "<code>/note 195315165 zavolať predajcovi po 18:00</code>\n\n"
        "Prázdny text poznámku vymaže."
    ),
    "/unsave": (
        "<b>/unsave</b> — odstrániť uložený inzerát\n\n"
        "<code>/unsave 195315165</code> (ad_id z /saved)"
    ),
    "/help": (
        "<b>/help</b>\n\n"
        "Prehľad všetkých príkazov. Detailné vysvetlenie každého: "
        "<code>/prikaz help</code> — napr. <code>/filter help</code>."
    ),
    "/cars": (
        "<b>/cars</b> — VŠETKY autá v databáze (aj bez AI hodnotenia)\n\n"
        "Prehľad celého nazbieraného katalógu — na rozdiel od /show nepotrebuje "
        "AI skóre ani /score limit. Každý riadok má stav: ⏳ bez hodnotenia, "
        "⭐n s AI skóre, 🛠 pravdepodobne náhradný diel.\n\n"
        "<b>Použitie:</b>\n"
        "  <code>/cars</code> — najnovšie, 7 dní\n"
        "  <code>/cars unevaluated</code> — len bez AI hodnotenia\n"
        "  <code>/cars rated</code> — len ohodnotené\n"
        "  <code>/cars cheap</code> / <code>expensive</code> / <code>score</code> — zoradenie\n"
        "  <code>/cars days 30</code> — okno 30 dní\n\n"
        "Stránkuješ tlačidlami ◀️ ▶️. Detail: <code>/car id</code>."
    ),
    "/car": (
        "<b>/car</b> — detail jedného inzerátu\n\n"
        "<code>/car 195357798</code> (ad_id nájdeš v /cars alebo notifikácii)\n\n"
        "Ukáže: cenu, rok, km, mesto, dátum, AI hodnotenie (skóre, dôvod, "
        "riziko, stav, prvú ponuku), vývoj ceny a porovnanie s trhom "
        "(medián podobných áut s veľkosťou vzorky)."
    ),
    "/queue": (
        "<b>/queue</b> — fronta AI vyhodnocovania\n\n"
        "Čo práve čaká/beží: pre každú úlohu profil, model, progres "
        "(x/y áut), vek a počet pokusov. Pod tým posledné dokončené a zlyhané "
        "úlohy. Frontu spracúva cron každých ~15 min."
    ),
    "/stats": (
        "<b>/stats</b> — štatistiky katalógu\n\n"
        "Počet inzerátov, pokrytie AI hodnotením (tvoj profil), cenový medián/"
        "min/max, nové inzeráty za 7 dní (sparkline), počet zliav a rozdelenie "
        "skóre. Interaktívne grafy: /dashboard."
    ),
    "/health": (
        "<b>/health</b> — zdravie pipeline\n\n"
        "Posledný beh scraperu (kedy, koľko strán/nových/zliav/hodnotení/"
        "notifikácií), stav fronty a databázy. Ak posledný beh chýba dlho, "
        "niečo je pokazené."
    ),
    "/templates": (
        "<b>/templates</b> — hotové hľadania\n\n"
        "Vyber šablónu (rodinné, do mesta, na diaľnicu, prvé auto, príležitosť) "
        "— nastaví kritériá, filtre a min. skóre a spustí prehodnotenie. "
        "Potom si všetko doladíš cez /criteria a /filter."
    ),
    "/export": (
        "<b>/export</b> — stiahni inzeráty ako CSV\n\n"
        "<code>/export</code> — posledných 30 dní (max 200)\n"
        "<code>/export 90</code> — posledných 90 dní\n\n"
        "CSV obsahuje aj AI skóre tvojho profilu (ak existuje)."
    ),
    "/dashboard": (
        "<b>/dashboard</b> — webový dashboard (Mini App)\n\n"
        "Otvorí grafické rozhranie priamo v Telegrame: prehľad, inventár, "
        "fronta a štatistiky s grafmi. Funguje len po nasadení na Vercel."
    ),
    "/menu": (
        "<b>/menu</b> — hlavné menu\n\n"
        "Tlačidlá na najčastejšie akcie: autá, top ponuky, štatistiky, fronta, "
        "šablóny, uložené, stav a nápoveda."
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
    "<b>🤖 Bazcar — tvoj lovec ojazdených áut</b>\n\n"
    "Napíš mi voľne, čo hľadáš (napr. <i>\"diesel do 200t km, nad 80 kW\"</i>) "
    "a každý nový inzerát ti príde s AI skóre 0–100, dôvodom a rizikom.\n\n"
    "<b>🚗 Katalóg</b>\n"
    "  /cars — všetky autá (aj bez AI), stránkovanie\n"
    "  /car id — detail: vývoj ceny + trhový medián\n"
    "  /show — najlepšie ohodnotené ponuky\n"
    "  /export — stiahni CSV\n\n"
    "<b>🔎 Čo hľadám</b>\n"
    "  /criteria — moje požiadavky pre AI bodovanie\n"
    "  /templates — hotové hľadania (rodinné, mesto…)\n"
    "  /filter — Bazoš filtre (query, cena, okruh, PSČ)\n"
    "  /score — min. hodnotenie pre notifikácie\n\n"
    "<b>⭐ Search profily</b>\n"
    "  /search meno: kritériá — uložiť profil\n"
    "  /searches — zoznam profilov\n"
    "  /use meno — aktivovať profil\n\n"
    "<b>📊 Prehľady</b>\n"
    "  /stats — štatistiky katalógu a trhu\n"
    "  /queue — fronta AI vyhodnocovania\n"
    "  /health — zdravie pipeline\n"
    "  /dashboard — webové grafy (Mini App)\n"
    "  /eval — prehodnotiť autá podľa searchu\n\n"
    "<b>💾 Uložené</b>\n"
    "  /save ad_id · /saved · /note ad_id text · /alert ad_id cena · /digest · /unsave ad_id\n\n"
    "<b>⚙️ AI a nastavenia</b>\n"
    "  /models · /model · /photos · /status · /menu\n\n"
    "💡 Detail každého príkazu: <code>/prikaz help</code> (napr. <code>/cars help</code>)\n"
    "💡 Napíš <code>/</code> a Telegram ti sám doplní príkazy."
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
                await self.notifier.answer_callback(cb_query_id, "💾 Uložené ✅")
            elif data.startswith("ntf:"):
                await self._toggle_search_notify(
                    chat_id, data.split(":", 1)[1], cb_query_id
                )
            elif data.startswith("ntfglobal:"):
                await self._toggle_global_notify(
                    chat_id, data.split(":", 1)[1] == "1", cb_query_id
                )
            elif data.startswith("use:"):
                await self._use(chat_id, data.split(":", 1)[1])
                await self.notifier.answer_callback(cb_query_id, "✅ Profil aktivovaný")
            elif data.startswith("c:"):
                await self._cars_page(chat_id, message_id, cb_query_id, data)
            elif data.startswith("tpl:"):
                await self._apply_template(chat_id, data.split(":", 1)[1], cb_query_id)
            elif data.startswith("menu:"):
                await self._menu_action(chat_id, data.split(":", 1)[1], cb_query_id)
            else:
                await self.notifier.answer_callback(cb_query_id, "Neznáma akcia")
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
                    "Ahoj! Som Bazcar 🚗. Napíš mi, aké auto hľadáš — a nechám ti "
                    "prísť nové inzeráty s AI skóre.\n\n" + HELP_TEXT,
                    MENU_KEYBOARD,
                )
            case "/help":
                return HELP_TEXT, MENU_KEYBOARD
            case "/menu":
                return "🏠 <b>Hlavné menu</b> — vyber akciu:", MENU_KEYBOARD
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
                return await self._models(chat_id, arg)
            case "/save":
                return await self._save(chat_id, arg)
            case "/saved":
                return await self._saved(chat_id)
            case "/note":
                return await self._note(chat_id, arg)
            case "/alert":
                return await self._alert(chat_id, arg)
            case "/digest":
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
        min_score = prefs.min_score if prefs else None
        await self.store.save_search(chat_id, name, criteria, filters=filters, min_score=min_score)
        return (
            f"✅ Uložené hľadanie „{_em(name)}“: <i>{_em(criteria)}</i>"
            f"\n{self._filters_summary(filters)}\nSkóre: {min_score or '—'}"
        )

    async def _searches(self, chat_id: int) -> tuple[str, dict] | str:
        prefs = await self.store.get_prefs(chat_id)
        searches = await self.store.list_searches(chat_id)
        if not searches:
            return "Nemáš uložené hľadania. `/search meno: diesel do 200 km`."
        lines = ["🔎 <b>Uložené hľadania</b> (🔔 = upozornenia zapnuté):"]
        keyboard: list[list[dict]] = []
        for s in searches:
            bell = "🔔" if s.notify_enabled else "🔕"
            lines.append(
                f"• {bell} <b>{_em(s.name)}</b>: {_em(s.criteria)}"
                + (f" (filtre: {self._filters_summary(s.filters)})" if s.filters else "")
            )
            keyboard.append(
                [
                    {"text": f"▶️ {s.name}", "callback_data": f"use:{s.name}"},
                    {
                        "text": f"{'🔕 Vypnúť' if s.notify_enabled else '🔔 Zapnúť'}",
                        "callback_data": f"ntf:{s.name}",
                    },
                ]
            )
        global_on = bool(prefs.notify_enabled) if prefs else True
        keyboard.append(
            [
                {
                    "text": f"{'🔕 Vypnúť všetky' if global_on else '🔔 Zapnúť všetky'}",
                    "callback_data": f"ntfglobal:{0 if global_on else 1}",
                }
            ]
        )
        lines.append("\n<i>Tlačidlom prepneš upozornenia, ▶️ aktivuje profil.</i>")
        return "\n".join(lines), {"inline_keyboard": keyboard}

    async def _toggle_search_notify(
        self, chat_id: int, name: str, cb_query_id: str
    ) -> None:
        searches = await self.store.list_searches(chat_id)
        match = next((s for s in searches if s.name == name), None)
        if match is None:
            await self.notifier.answer_callback(cb_query_id, "Hľadanie som nenašiel")
            return
        await self.store.set_search_notify(chat_id, name, not match.notify_enabled)
        state = "zapnuté 🔔" if not match.notify_enabled else "vypnuté 🔕"
        await self.notifier.answer_callback(cb_query_id, f"„{name}“: {state}")

    async def _toggle_global_notify(self, chat_id: int, on: bool, cb_query_id: str) -> None:
        await self.store.ensure_prefs(chat_id)
        await self.store.update_prefs(chat_id, notify_enabled=on)
        await self.notifier.answer_callback(
            cb_query_id, "Upozornenia zapnuté 🔔" if on else "Upozornenia vypnuté 🔕"
        )

    async def _use(self, chat_id: int, name: str) -> str:
        if not name:
            return "/use meno"
        searches = await self.store.list_searches(chat_id)
        for s in searches:
            if s.name.lower() == name.lower():
                updates: dict = {"criteria": s.criteria, "filters": s.filters or {}}
                if s.min_score is not None:
                    updates["min_score"] = s.min_score
                await self.store.update_prefs(chat_id, **updates)
                reply = f"✅ Aktivované „{_em(s.name)}“: <i>{_em(s.criteria)}</i>"
                if s.filters:
                    reply += "\n" + self._filters_summary(s.filters)
                if s.min_score is not None:
                    reply += f"\n⭐ Skóre: {s.min_score}/100"
                try:
                    await self._queue_rehodnotenie(chat_id)
                except Exception:
                    logger.warning("rehodnotenie queue failed", exc_info=True)
                return reply
        return f"Nenašiel som „{_em(name)}“. /searches"

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
            return "Použitie: <code>/model info ID</code> — napr. <code>/model info ~deepseek/deepseek-v4-flash-latest</code>"

        prefs = await self.store.get_prefs(chat_id)
        current = prefs.model or load_llm_config().model if prefs else load_llm_config().model

        # /model <ID> — switch
        if arg:
            if arg not in models:
                return (
                    f"Nenájdený model <code>{_em(arg)}</code> (musí podporovať JSON).\n"
                    "Zoznam: <code>/models</code>, filtruj: <code>/models deepseek</code>"
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
            return f"Model <code>{_em(model_id)}</code> nie je v JSON-capable zozname (môže byť vypnutý)."
        rpm = m["rpm"] if m["rpm"] else "∞ (bez limitu) ✅"
        ctx = f"{m['ctx']:,}".replace(",", " ") if m["ctx"] else "?"
        price = "0 (zadarmo)" if m["free"] else f"{m['price_in']} $ / 1M tokenov vstupu, {m['price_out']} $ / 1M výstupu"
        head = "<b>⬆️ Aktuálny model</b>\n\n" if is_current else "<b>Detail modelu</b>\n\n"
        flags = []
        if m["rpm"] is None:
            flags.append("⚡ žiadny rate limit (vhodné na dávku)")
        elif m["rpm"] and m["rpm"] < 60:
            flags.append("⚠️ pomalý rate limit — backfill môže trvať")
        if m["free"]:
            flags.append("🆓 zadarmo")
        return (
            head
            + f"<b>{_em(m['name'])}</b>\n"
            f"<code>{_em(m['id'])}</code>\n\n"
            f"💰 {price}\n"
            f"⚡ Rychlosť: {rpm}\n"
            f"🔡 Kontext: {ctx}\n\n"
            + ("".join(f"{f}\n" for f in flags) if flags else "")
            + ("\nZmeniť: <code>/model ID</code>, zoznam: <code>/models</code>" if is_current else "")
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
            return "⏳ Toto vyhľadanie už prehodnocujem — pošli /show o pár minút."

        if to_eval == 0:
            return "✅ Všetky autá v rozsahu už majú hodnotenie pre tento search+model — nič sa nevyhodnocuje. /show"
        minutes = max(1, round(to_eval * EST_SECONDS_PER_EVAL / 60))
        return (
            f"🔁 <b>Prehodnotenie zaradené</b>\n"
            f"Rozsah: {total} áut (posledných {days} dní), z toho "
            f"<b>{to_eval}</b> bez hodnotenia pre tento search+model.\n"
            f"⏱️ Odhad: <b>~{minutes} min</b> (~{EST_SECONDS_PER_EVAL}s/auto)\n"
            f"➡️ Keď bude hotovo, príde ti ✅ hlásenie a <code>/show</code> ukáže výsledky."
        )

    async def _show(self, chat_id: int, arg: str) -> str:
        """List recently seen listings matching the active search profile.

        Sends the top matches as photo notifications (same look as deal
        alerts). Uses the active profile's stored evaluations and the current
        ``/score`` threshold.
        """
        settings = get_settings()
        if not settings.database_url or not settings.telegram_bot_token:
            return "⚠️ Chýba databáza alebo Telegram token — /show nedostupné."
        prefs = await self.store.get_prefs(chat_id)
        if prefs is None:
            return "Žiadne nastavenia. /help"
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
                hint = f"\nNajlepšie dosiahnuté skóre: {best}/100 — skús /score {max(best - 10, 0)}."
            return (
                f"Nič vyhovujúce (skóre >= {min_score}) za posledných {days} dní.{hint}\n"
                "Zmeň /criteria alebo /score, alebo rozšír okno: /show 20 days 7."
            )
        listings = [_row_to_listing(row) for row in rows]
        async with TelegramNotifier(settings.telegram_bot_token, str(chat_id)) as notifier:
            await notifier.send_listings_with_photos(listings, chat_id=chat_id)
        extra = total - len(listings)
        tail = f" (ďalších {extra} vyhovujúcich — zvýš limit: /show 20)" if extra > 0 else ""
        return (
            f"📤 Poslal som {len(listings)} z {total} vyhovujúcich "
            f"(skóre >= {min_score}, okno {days} dní){tail}:"
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
            return "⚠️ /cars potrebuje databázu (BAZCAR_DATABASE_URL)."
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
                f"📭 V katalógu nič nie je ({_STATUS_LABELS[status]}, {days} dní).\n"
                "Skús širšie okno (<code>/cars days 30</code>) alebo počkaj na ďalší scrape."
            )
        text = build_cars_page(rows, total=total, offset=0, days=days, status=status, sort=sort)
        return text, cars_keyboard(status, sort, days, 0, total)

    async def _cars_page(
        self, chat_id: int, message_id: int | None, cb_query_id: str, data: str
    ) -> None:
        """Pager callback: re-render the catalog page by editing the message."""
        parsed = parse_cars_callback(data)
        if parsed is None or message_id is None:
            await self.notifier.answer_callback(cb_query_id, "Neplatná stránka")
            return
        status, sort, days, offset = parsed
        settings = get_settings()
        if not settings.database_url:
            await self.notifier.answer_callback(cb_query_id, "Bez databázy")
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
            await self.notifier.answer_callback(cb_query_id, "Žiadne ďalšie")
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
            return "⚠️ /car potrebuje databázu."
        try:
            ad_id = int(arg.strip())
        except ValueError:
            return "Použitie: <code>/car 195357798</code> (ad_id z /cars alebo notifikácie)"
        p_key, prefs = await self._profile_key_for(chat_id)
        repo = ListingRepository(settings.database_url)
        await repo.connect()
        try:
            row = await repo.fetch_listing_by_id(ad_id, p_key)
            if row is None:
                return f"Inzerát <code>{ad_id}</code> nemám v databáze. Skús /cars."
            history = await repo.fetch_price_history(ad_id)
            make = extract_make(str(row["title"]))
            bench = await repo.price_benchmark(make, row.get("year")) if make else None
        finally:
            await repo.close()
        text = build_car_card(row, history, bench)
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "💾 Uložiť", "callback_data": f"save:{ad_id}"},
                    {"text": "🔗 Otvoriť inzerát", "url": row["url"]},
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
            return "⚠️ /queue potrebuje databázu."
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
                [{"text": "🔄 Spracovať teraz", "callback_data": "menu:rescrape"}]
            ]
        }
        return text, keyboard

    async def _stats(self, chat_id: int) -> str:
        """Catalog + market statistics with sparkline and score bands (/stats)."""
        settings = get_settings()
        if not settings.database_url:
            return "⚠️ /stats potrebuje databázu."
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
            return "⚠️ /health potrebuje databázu."
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
        lines = ["<b>🩺 Zdravie pipeline</b>", ""]
        lines.append("🗄 Databáza: ✅ spojená")
        budget = getattr(settings, "monthly_budget_usd", None)
        if spend is not None:
            if budget is not None:
                state = "⏸️ stopnuté (rozpočet)" if spend >= budget else "✅"
                lines.append(
                    f"💵 AI za 30 dní: <b>{spend:.4f} $</b> / {budget} $ {state}"
                )
            else:
                lines.append(f"💵 AI za 30 dní: <b>{spend:.4f} $</b> (bez limitu)")
        if run:
            age = time_ago(run.get("finished_at") or run.get("started_at"))
            lines.append(f"🔄 Posledný beh: <b>{age}</b>")
            lines.append(
                f"   strán {run.get('pages', 0)}, nájdených {run.get('total', 0)}, "
                f"nových {run.get('inserted', 0)}, zliav {run.get('price_drops', 0)}"
            )
            lines.append(
                f"   ohodnotených {run.get('evaluated', 0)}, "
                f"notifikácií {run.get('notified', 0)}"
            )
            if run.get("error"):
                lines.append(f"   ⚠️ chyba: {_em(run['error'])}")
        else:
            lines.append("🔄 Posledný beh: <b>žiadny zaznamenaný</b> — cron ešte nebežal?")
        running = sum(1 for t in pending if t.get("started_at"))
        lines.append(
            f"⏳ Fronta AI: {len(pending)} čakajúcich ({running} bežiacich) — /queue"
        )
        lines.append("")
        lines.append("<i>Cron: každých ~15 min (GitHub Actions).</i>")
        return "\n".join(lines)

    def _templates(self) -> tuple[str, dict]:
        """Preset search profiles with one-tap apply (/templates)."""
        lines = ["<b>📦 Hotové hľadania</b> — ťukni a hneď hľadáš:", ""]
        buttons = []
        for template in TEMPLATES:
            lines.append(f"{template.emoji} <b>{_em(template.name)}</b> — {_em(template.description)}")
            buttons.append(
                [{"text": f"{template.emoji} {template.name}", "callback_data": f"tpl:{template.key}"}]
            )
        lines.append("")
        lines.append("<i>Nastaví kritériá + filtre + skóre a spustí prehodnotenie.</i>")
        return "\n".join(lines), {"inline_keyboard": buttons}

    async def _apply_template(self, chat_id: int, key: str, cb_query_id: str) -> None:
        """Template button: set criteria+filters+score, then queue a re-eval."""
        template = get_template(key)
        if template is None:
            await self.notifier.answer_callback(cb_query_id, "Neznáma šablóna")
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
        await self.notifier.answer_callback(cb_query_id, f"{template.emoji} Aktivované!")
        await self.reply(
            chat_id,
            f"{template.emoji} <b>{_em(template.name)}</b> aktívne:\n"
            f"<i>{_em(template.criteria)}</i>\n"
            f"{self._filters_summary(template.filters)}\n"
            f"⭐ Min. skóre: {template.min_score}\n\n"
            "➡️ Prehodnotenie beží — výsledky: /show, prehľad: /cars",
        )

    async def _menu_action(self, chat_id: int, action: str, cb_query_id: str) -> None:
        """Main-menu button routing — runs the same logic as the commands."""
        await self.notifier.answer_callback(cb_query_id)
        if action == "rescrape":
            ok = await _dispatch_scrape()
            await self.reply(
                chat_id,
                "🔄 Spustil som spracovanie fronty." if ok
                else "⚠️ Nedá sa spustiť (chýba GITHUB_WORKFLOW_TOKEN) — počkaj na cron.",
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
        elif action == "status":
            result = await self._status(chat_id)
        elif action == "dashboard":
            result = self._dashboard()
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
            return "⚠️ /export potrebuje databázu."
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
            return f"📭 Nič na export za posledných {days} dní."
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
            caption=f"📄 Export: {len(rows)} inzerátov za {days} dní",
        )
        return None if ok else "⚠️ Export sa nepodarilo odoslať."

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
                "📱 Dashboard nie je nasadený. Nasaď projekt na Vercel a nastav "
                "<code>BAZCAR_WEBAPP_URL</code> (alebo VERCEL_URL sa použije automaticky)."
            )
        keyboard = {
            "inline_keyboard": [
                [{"text": "📱 Otvoriť dashboard", "web_app": {"url": url}}]
            ]
        }
        return "📱 <b>Bazcar dashboard</b> — grafy, inventár, fronta a štatistiky:", keyboard

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
        lines: list[str] = ["<b>🤖 Modely (JSON-capable)</b>", ""]
        if not models:
            lines.append("Žiadny model nevyhovuje filtru.")
            lines.append("Skús: /models gemini · /models deepseek · /models llama · /models flash")
            return "\n".join(lines)
        lines += _block("🆓 Free", free[:cap], cap)
        lines.append("")
        lines += _block("💰 Paid", paid[:cap], cap)
        lines.append("")
        lines.append("<i>⚡ = rýchlosť (requests/min, ∞ = bez limitu), 🔡 = kontext, $/M = cena za 1M tokenov vstupu</i>")
        lines.append("Nastav: <code>/model ID</code> · filtruj: <code>/models text</code> · detail: <code>/model info ID</code>")
        return "\n".join(lines)

    async def _save(self, chat_id: int, arg: str) -> str:
        try:
            ad_id = int(arg)
        except ValueError:
            return "/save ad_id"
        title, url = await self._resolve_listing_meta(ad_id)
        await self.store.save_listing(chat_id, ad_id, title, url)
        return f"💾 Uložené ad {ad_id}."

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
            return "Nemáš nič uložené."
        lines = ["💾 <b>Uložené inzeráty</b> (🔔 = strážim zľavu automaticky):"]
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
                    bits.append(f"🎯 strážim pod {fmt_eur(int(item.alert_below))}")
                except (TypeError, ValueError):
                    pass
            if item.note:
                note = item.note if len(item.note) <= 80 else item.note[:77] + "…"
                bits.append(f"📝 {_em(note)}")
            lines.append(head + ("\n  " + " · ".join(bits) if bits else ""))
        lines.append("\n<i>/note ad_id text — poznámka · /alert ad_id cena — strážiť cenu · /unsave ad_id — odstrániť</i>")
        return "\n".join(lines)

    async def _note(self, chat_id: int, arg: str) -> str:
        parts = (arg or "").split(maxsplit=1)
        if not parts or not parts[0].lstrip("-").isdigit():
            return "Použitie: <code>/note ad_id tvoja poznámka</code>"
        ad_id = int(parts[0])
        note = parts[1].strip()[:500] if len(parts) > 1 else ""
        updated = await self.store.set_saved_note(chat_id, ad_id, note)
        if not updated:
            return f"Ad {ad_id} nemáš uložený. Najprv <code>/save {ad_id}</code>."
        return f"📝 Poznámka k {ad_id} uložená." if note else f"📝 Poznámka k {ad_id} vymazaná."

    async def _unsave(self, chat_id: int, arg: str) -> str:
        try:
            ad_id = int(arg)
        except ValueError:
            return "/unsave ad_id"
        removed = await self.store.remove_saved(chat_id, ad_id)
        return "Odstránené ✅" if removed else f"Ad {ad_id} nemáš uložený."

    async def _alert(self, chat_id: int, arg: str) -> str:
        """Per-ad price watch: /alert <ad_id> [cena|off] (empty = show)."""
        parts = (arg or "").split(maxsplit=1)
        if not parts or not parts[0].lstrip("-").isdigit():
            return "Použitie: <code>/alert ad_id [cena|off]</code> — napr. <code>/alert 195315165 3000</code>"
        ad_id = int(parts[0])
        saved = await self.store.list_saved(chat_id)
        if all(s.ad_id != ad_id for s in saved):
            return f"Ad {ad_id} nemáš uložený. Najprv <code>/save {ad_id}</code>."
        if len(parts) == 1:
            current = next(s.alert_below for s in saved if s.ad_id == ad_id)
            return (
                f"🔔 Ad {ad_id}: strážim pod <b>{fmt_eur(current)}</b>."
                if current
                else f"🔔 Ad {ad_id}: upozorním na každú zľavu. Prah nastavíš: <code>/alert {ad_id} cena</code>"
            )
        raw = parts[1].strip().lower()
        if raw in {"off", "0", "vyp", "vypnúť"}:
            await self.store.set_saved_alert(chat_id, ad_id, None)
            return f"🔔 Ad {ad_id}: prah zrušený — upozorním na každú zľavu."
        try:
            below = int(raw.replace(" ", "").replace("€", ""))
        except ValueError:
            return "Cena musí byť číslo — napr. <code>/alert 195315165 3000</code>"
        if below <= 0:
            return "Cena musí byť kladná."
        await self.store.set_saved_alert(chat_id, ad_id, below)
        return f"🔔 Ad {ad_id}: upozorním, až cena klesne pod <b>{fmt_eur(below)}</b>."

    async def _digest(self, chat_id: int) -> str:
        """On-demand weekly summary: new ads, drops, top matches, queue."""
        settings = get_settings()
        if not settings.database_url:
            return "⚠️ /digest potrebuje databázu."
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
        lines = ["<b>📬 Týždenný súhrn</b>"]
        lines.append(f"🆕 Nových inzerátov za 7 dní: <b>{sum(n for _, n in week)}</b>")
        lines.append(f"📉 Zliav za 7 dní: <b>{len(drops)}</b>")
        for d in drops:
            lines.append(
                f"  −{d['pct']} % <a href=\"{_em_url(d['url'])}\">{_em(d['title'])}</a> "
                f"({fmt_eur(d['prev'])} → {fmt_eur(d['now_price'])})"
            )
        lines.append(f"⭐ Zhoduje sa s tvojím profilom (7 dní): <b>{total_matches}</b>")
        for m in top:
            lines.append(
                f"  {m.get('score', '?')}/100 <a href=\"{_em_url(m['url'])}\">{_em(m['title'])}</a>"
            )
        lines.append(
            f"⏳ Vo fronte AI: <b>{len(pending)}</b>"
            + (" — spracuje cron." if pending else " ✅")
        )
        return "\n".join(lines)

    async def _status(self, chat_id: int) -> str:
        prefs = await self.store.get_prefs(chat_id)
        if prefs is None:
            return "Žiadne nastavenia. /help"
        model = prefs.model or load_llm_config().model
        p_key = profile_key(prefs.criteria, model)
        lines = [
            "<b>Stav</b>",
            f"Kritériá: {_em(prefs.criteria or '—')}",
            f"Model: {_em(model)}",
            f"Min. hodnotenie: {_em(str(prefs.min_score or '—'))}",
            f"Fotky: {'áno' if prefs.show_photo else 'nie'}",
            f"Upozornenia: {'🔔 zapnuté' if prefs.notify_enabled else '🔕 vypnuté'}",
            self._filters_summary(prefs.filters),
 f"Profil: <code>{p_key}</code>",
        ]
        budget = getattr(get_settings(), "monthly_budget_usd", None)
        if budget is not None:
            lines.append(f"💵 Mesačný AI rozpočet: <b>{budget} $</b>")
        if get_settings().database_url:
            try:
                repo = ListingRepository(get_settings().database_url)
                await repo.connect()
                try:
                    if await repo.pending_eval_task_for(p_key):
                        lines.append("⏳ Prehodnotenie tohto searchu beží (o pár minút /show).")
                    else:
                        best = await repo.fetch_evaluations_max_score(p_key)
                        n = await repo.count_evaluations(p_key, days=3)
                        tail = f", najlepšie skóre {best}/100" if best is not None else ""
                        lines.append(f"✅ Ohodnotených áut (3 dni): {n}" + tail)
                finally:
                    await repo.close()
            except Exception:
                lines.append("⚠️ Databáza nedostupná")
        return "\n".join(lines)

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
