# Bazcar

Autonomous deal finder for used cars on [Bazoš.sk](https://auto.bazos.sk/).
Pipeline: **scrape → dedupe → LLM eval → notify → (seller contact) → 24/7 ops**,
plus multi-user profiles and a Telegram Mini App dashboard.

## Architecture (phases)

| Phase | Goal | Acceptance test | State |
|-------|------|-----------------|-------|
| 1 | Modular Bazoš scraper (passenger cars, whole SK) | Export 50 newest listings to JSON | done |
| 2 | Persistence (Neon Postgres) + dedupe | 2nd run without duplicates; price-change tracking | done |
| 3 | Deal scoring via OpenRouter (LLMProvider) | AI scores listings + `why` | done (live, with key) |
| 4 | Telegram notifications, model switching via bot | Notification arrives for a new listing; criteria/model change via bot | done (live) |
| 5 | Standalone runs on GitHub Actions (cron ~15 min) | Automatic GHA runs + logs | done (`scrape.yml`, secrets) |
| 6 | Autonomous seller contact (wait, discount, buy) | Bot successfully makes an offer to a seller | — |
| 7 | Multi-user profiles + Telegram Mini App dashboard | Every chat has its own profile; `/dashboard` opens the data web UI | done |
| 8 | Stats v2, price-watch alerts, settings hub, English UI | Histogram/top-makes/funnel/cost; `/alert`; `/settings` | done |

## Tech stack
Python 3.11+ / `uv`, `httpx` (async) + BeautifulSoup4 + lxml, Pydantic v2, Typer,
`asyncpg` + [Neon](https://neon.tech) Postgres (storage + dedupe, Phase 2),
[OpenRouter](https://openrouter.ai) Chat Completions API (LLM eval, Phase 3).
No Playwright — Bazoš needs no JS.

## Quick start
```bash
uv sync --extra dev
uv run bazcar version
# scrape 1 page into Postgres (automatic when BAZCAR_DATABASE_URL is set)
uv run bazcar scrape --pages 1 --db
# LLM scoring via OpenRouter (automatic when OPENROUTER_API_KEY is set)
uv run bazcar scrape --pages 1 --db --eval
# + Telegram alerts for new listings only (when TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID
#   are set; with BAZCAR_TELEGRAM_MIN_SCORE only highly scored deals)
uv run bazcar scrape --pages 1 --db --eval --notify
# no database: JSON export only
uv run bazcar scrape --pages 3 --limit 50 --no-db --no-eval
```

Output: JSON in `data/exports/`, a CLI summary, listings deduped in Postgres
(`ad_id` = primary key; price changes in `price_history`).
A second `--db` run returns `0 new ... N unchanged` (no duplicates).

Tests:
```bash
uv run pytest            # unit (offline, mocked)
uv run pytest -m live    # live: scrape against real Bazoš + Neon DB (network)
node --test "tests/js/**/*.test.mjs"   # Mini App SPA in a real JS engine (needs node)
```

The Mini App SPA is embedded in `dashboard/index.html`; `tests/js/spa.test.mjs`
executes the shipped script in a stubbed DOM to catch syntax errors and
formatter regressions. It also runs (and auto-skips without node) as part of
`uv run pytest` via `tests/unit/test_spa_runtime.py`.

The live DB test (`tests/integration/test_live_db.py`) verifies dedupe and price
tracking against real Neon using `BAZCAR_DATABASE_URL` from `.env`.

LLM eval (`--eval`) is unit-tested with a mocked OpenRouter (`tests/unit/test_llm.py`).
Live check: set `OPENROUTER_API_KEY` in `.env` and run `bazcar scrape --pages 1 --eval`.

Telegram notifications (Phase 4): unit-tested with a mocked Telegram API (`tests/unit/test_notify.py`).
Live check: set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` and run
`bazcar scrape --pages 1 --notify` — new listings arrive on Telegram.

Bot (Phase 4): `bazcar bot` — long-polling; stores criteria, model, min score,
photos and saved listings in Neon (`tests/unit/test_bot.py`). Needs
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and `BAZCAR_DATABASE_URL`.

**Multi-user**: every chat has its own profile (criteria, filters, model, score) —
a user just sends `/start`. Without `BAZCAR_ALLOWED_CHAT_IDS` the bot is open
(onboarding); with it, it's a whitelist (e.g. only you).

Commands (22 in the `/` menu; `/score`, `/photos`, `/models`, `/note`, `/alert`,
`/digest`, `/status` still work as hidden aliases):
- Basics: `/start`, `/help`, `/menu`, `/settings`, `/criteria`, `/model`.
- Searches: `/search name: criteria`, `/searches`, `/use name`,
  `/filter query|price|dist|psc`, `/filter clear`.
- **Catalog**: `/cars [status] [sort] [days N]` (status `all|unevaluated|rated`,
  sort `cheap|expensive|score|newest`) — browse ALL listings, even unscored ones;
  button paging. `/car <ad_id>` — detail with price trend and market benchmark.
- **Insights**: `/stats` (AI coverage, sparkline, score bands), `/queue`
  (AI queue, progress), `/health` (last run, DB, AI spend), `/export [days]` (CSV),
  `/templates` (ready-made profiles), `/dashboard` (Mini App), `/eval`, `/show`.
- Saved: `/save ad_id`, `/saved`, `/unsave ad_id` (+ `/note`, `/alert` via card/buttons).

## Dashboard (Telegram Mini App)
`/dashboard` opens the web dashboard inside Telegram (overview, inventory,
saved, AI queue, stats, profiles) — it lives on Vercel as one Python function
with a path dispatcher:
- `api/index.py` — single entrypoint routing to the three surfaces below,
- `api/dashboard.py` — serves `dashboard/index.html` (static SPA),
- `api/webapp.py` — JSON API
  (`?view=overview|inventory|saved|listing&id=<ad_id>|compare&ids=…|queue|stats|profiles`,
  plus POST actions: save/unsave/note/use/toggles/score/photos).

Clicking 🔎 in the inventory opens the listing detail (price trend + market
benchmark — the same market-value signal the bot shows via `/car`), now with a
photo gallery, full description, save button and a 3-way compare view.

Auth: every request carries signed Telegram `initData` (HMAC-SHA256), verified
against the bot token → each user only sees their own profile's data. Unsigned
requests get 401, so the page can stay public without leaking data.

Set `BAZCAR_WEBAPP_URL=https://<project>.vercel.app/api/dashboard`
(otherwise derived from `VERCEL_URL` at runtime). One Python function serves
all `/api/*` paths via the `api/index.py` dispatcher (see `[tool.vercel]`
in `pyproject.toml`).

Optional AI guardrail: `BAZCAR_MONTHLY_BUDGET_USD` pauses fresh scoring once the
trailing-30-day evaluation spend is reached (cached scores keep serving).

## Configuration
- `.env` (from `.env.example`) — runtime variables (incl. `BAZCAR_DATABASE_URL`,
  `OPENROUTER_API_KEY`), no secrets in the repo.

## Automation (Phase 5)
Two workflows (public repo = unlimited Actions minutes):
- `scrape.yml` — every ~15 min: scrape → dedupe → LLM eval → Telegram.
- `bot.yml` — every ~2 min: process bot messages (`bazcar bot --once`),
  so the bot answers without a local daemon.

Secrets: `BAZCAR_DATABASE_URL`, `OPENROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`. Manual run: `gh workflow run scrape|bot`.

Alternative for instant replies: deploy `api/` to Vercel and set the
webhook (`bazcar webhook --url https://<project>.vercel.app/api/telegram`) —
Telegram then delivers updates straight to the function (see `.env.example`,
`TELEGRAM_WEBHOOK_SECRET`). GitHub Actions stays the scrape/backfill scheduler.
