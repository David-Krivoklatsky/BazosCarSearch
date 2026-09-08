# Bazcar

Autonómny vyhľadávač výhodných ojazdených áut na [Bazoš.sk](https://auto.bazos.sk/).
6-fázový projekt: **scrape → dedupe → LLM eval → notifikácia → kontakt → prevádzka 24/7**.

## Architektúra (6 fáz)

| Fáza | Cieľ | Test akceptácie | Stav |
|------|------|-----------------|------|
| 1 | Modulárny scraper Bazoš (osobné autá, celé SK) | Export 50 najnovších inzerátov do JSON | hotové |
| 2 | Perzistencia (Neon Postgres) + dedupe | 2. spustenie bez duplicit; sledovanie zmien ceny | hotové |
| 3 | Vyhodnotenie obchodov cez OpenRouter (LLMProvider) | AI ohodnotí inzeráty + `why` | hotové (live overenie s kľúčom) |
| 4 | Telegram notifikácie, prepínanie modelu cez bot | Príchod notifikácie k novému inzerátu | — |
| 5 | Samostatné spustenie na GitHub Actions (cron ~15 min) | Automatický beh na GHA + logy | — |
| 6 | Autonómny kontakt predajcov (čakanie, zľava, kúpa) | Bot úspešne ponúkne kúpu predajcovi | — |

## Technický stack
Python 3.11+ / `uv`, `httpx` (async) + BeautifulSoup4 + lxml, Pydantic v2, Typer,
`asyncpg` + [Neon](https://neon.tech) Postgres (storage + dedupe, Fáza 2),
[OpenRouter](https://openrouter.ai) Chat Completions API (LLM eval, Fáza 3).
Žiadny Playwright — Bazoš nevyžaduje JS.

## Rýchly štart
```bash
uv sync --extra dev
uv run bazcar version
# scrape 1 stránka a nahrať do Postgres (automaticky, keď je nastavená BAZCAR_DATABASE_URL)
uv run bazcar scrape --pages 1 --db
# LLM ohodnotenie inzerátov cez OpenRouter (automaticky, keď je nastavená OPENROUTER_API_KEY)
uv run bazcar scrape --pages 1 --db --eval
# bez databázy len export do JSON
uv run bazcar scrape --pages 3 --limit 50 --no-db --no-eval
```

Výstup: JSON v `data/exports/`, suma v CLI, inzeráty dedupované v Postgres
(`ad_id` = primárny kľúč; zmeny cien v `price_history`).
Druhé spustenie `--db` vráti `0 new ... N unchanged` (žiadne duplicity).

Testy:
```bash
uv run pytest            # unit (offline, mocked)
uv run pytest -m live    # live: scrape proti skutočnému Bazoš + Neon DB (pozor, sieť)
```

Live DB test (`tests/integration/test_live_db.py`) overí dedupe a sledovanie cien
proti reálnemu Neon pomocou `BAZCAR_DATABASE_URL` z `.env`.

LLM eval (`--eval`) je unit-testovaný s mocknutým OpenRouter (`tests/unit/test_llm.py`).
Live overenie: nastav `OPENROUTER_API_KEY` v `.env` a spusti `bazcar scrape --pages 1 --eval`.

## Konfigurácia
- `config/scraper.yaml` — URL, CSS selektory, rate-limity, UA pool, regexe.
- `config/llm.yaml` — model, teplota, max tokenov pre LLM eval (Fáza 3); model nastavíš aj cez `BAZCAR_OPENROUTER_MODEL`.
- `.env` (z `.env.example`) — runtime premenné (vrát. `BAZCAR_DATABASE_URL`, `OPENROUTER_API_KEY`), žiadne tajomstvá v repo (je public).