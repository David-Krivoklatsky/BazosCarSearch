"""Static bot text: command menu, help, templates intro (no I/O).

Extracted from ``daemon.py`` — imported and re-exported by ``daemon.py``.
"""

from __future__ import annotations

# Registered via Telegram setMyCommands -> typing "/" shows an autocomplete
# menu with these descriptions in every Telegram client.
#
# Visible set (22): the daily-driver commands. The rest still work as hidden
# aliases for backward compatibility (old messages, muscle memory, tests):
# /score, /photos, /models, /note, /alert, /digest, /status, /prompt, /fallbacks.
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
_HIDDEN_COMMANDS = {
    "score", "photos", "models", "note", "alert", "digest", "status",
    "prompt", "fallbacks",
}


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
        "  <code>/eval all</code> — everything in the DB (up to 500)\n"
        "  <code>/eval 195357798,195357799</code> — re-score just these cars (fresh)\n\n"
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


