# Bazcar

Autonómny vyhľadávač výhodných ojazdených áut na [Bazoš.sk](https://auto.bazos.sk/).
6-fázový projekt: **scrape → dedupe → LLM eval → notifikácia → kontakt → prevádzka 24/7**.

## Architektúra (6 fáz)

| Fáza | Cieľ | Test akceptácie |
|------|------|-----------------|
| 1 | Modulárny scraper Bazoš (osobné autá, celé SK) | Export 50 najnovších inzerátov do JSON |
| 2 | Perzistencia (Neon Postgres) + dedupe | 2. spustenie bez duplicit; sledovanie zmien ceny |
| 3 | Vyhodnotenie obchodov cez OpenRouter (LLMProvider) | AI ohodnotí inzeráty + `why` |
| 4 | Telegram notifikácie, prepínanie modelu cez bot | Príchod notifikácie k novému inzerátu |
| 5 | Samostatné spustenie na GitHub Actions (cron ~15 min) | Automatický beh na GHA + logy |
| 6 | Autonómny kontakt predajcov (čakanie, zľava, kúpa) | Bot úspešne ponúkne kúpu predajcovi |

## Technický stack
Python 3.11+ / `uv`, `httpx` (async) + BeautifulSoup4 + lxml, Pydantic v2, Typer.
Žiadny Playwright — Bazoš nevyžaduje JS.

## Rýchly štart
```bash
uv sync --extra dev
uv run bazcar version
uv run bazcar scrape --category-url https://auto.bazos.sk/ --pages 3 --limit 50
```

Výstup: JSON v `data/exports/`, suma v CLI.

Testy:
```bash
uv run pytest            # unit (offline, mocked)
uv run pytest -m live    # live scrape proti skutočnému Bazoš (pozor, sieť)
```

## Konfigurácia
- `config/scraper.yaml` — URL, CSS selektory, rate-limity, UA pool, regexe.
- `.env` (z `.env.example`) — runtime premenné, žiadne tajomstvá (repo je public).