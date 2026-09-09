"""Typer CLI for bazcar."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import typer

from bazcar import __version__
from bazcar.config.settings import get_settings
from bazcar.core.exceptions import BazcarError

app = typer.Typer(
    name="bazcar",
    help="Autonomous used-car deal finder for Bazoš.sk.",
    add_completion=False,
)


@app.command()
def scrape(
    pages: int = typer.Option(1, "--pages", "-p", min=1, help="Number of category pages to fetch."),
    limit: int | None = typer.Option(
        None, "--limit", "-l", min=1, help="Keep only the first N listings (dedupe by ID)."
    ),
    detail: bool = typer.Option(True, "--detail/--no-detail", help="Fetch each listing detail page for the full description."),
    detail_limit: int = typer.Option(0, "--detail-limit", min=0, help="Limit detail fetches (0 = all)."),
    export: Path | None = typer.Option(
        None, "--export", help="Explicit JSON export path (default: data/exports/<filters_hash>_<timestamp>.json)."
    ),
    db: bool | None = typer.Option(
        None,
        "--db/--no-db",
        help="Persist listings to Postgres/Neon (default: auto when BAZCAR_DATABASE_URL is set).",
    ),
    eval: bool | None = typer.Option(
        None,
        "--eval/--no-eval",
        help="LLM-evaluate deals via OpenRouter (default: auto when OPENROUTER_API_KEY is set).",
    ),
    notify: bool | None = typer.Option(
        None,
        "--notify/--no-notify",
        help="Send Telegram alerts for new deals (default: auto when TELEGRAM_BOT_TOKEN is set).",
    ),
) -> None:
    """Scrape a Bazoš category and export listings to JSON (+ optional Postgres / LLM / Telegram)."""
    from bazcar.pipeline.runner import run_scrape

    summary = asyncio.run(
        run_scrape(
            max_pages=pages,
            limit=limit,
            detail=detail,
            detail_limit=detail_limit,
            export_path=export,
            persist=db,
            evaluate=eval,
            notify=notify,
        )
    )
    typer.echo(
        f"[OK] scraped {summary.total_found} listings "
        f"(pages: {len(summary.pages_scraped)}), exported -> {summary.export_path}"
    )
    if summary.evaluated:
        typer.echo(f"[LLM] evaluated {summary.evaluated} listings")
    if summary.notified:
        typer.echo(f"[TG] notified {summary.notified} listings")
    if summary.db is not None:
        db = summary.db
        typer.echo(
            f"[DB] {db.total} rows: {db.inserted} new, {db.price_changed} price changed "
            f"({db.price_drops} drops), {db.unchanged} unchanged"
        )


@app.command()
def bot(
    once: bool = typer.Option(False, "--once", help="Handle one update batch and exit (testing)."),
) -> None:
    """Run the Telegram bot (long-polling) — set preferences, save ads, switch model."""
    from bazcar.bot.daemon import run_bot_loop

    asyncio.run(run_bot_loop(once=once))


@app.command()
def webhook(
    url: str | None = typer.Option(None, "--url", help="Public HTTPS webhook URL (e.g. https://bazcar.vercel.app/api/telegram)."),
    remove: bool = typer.Option(False, "--remove", help="Delete the webhook (back to polling mode)."),
    info: bool = typer.Option(False, "--info", help="Show current webhook info."),
) -> None:
    """Manage the Telegram webhook (Vercel serverless bot mode)."""
    import httpx

    from bazcar.config.settings import get_settings

    settings = get_settings()
    if not settings.telegram_bot_token:
        raise typer.BadParameter("TELEGRAM_BOT_TOKEN is not set")
    api = f"https://api.telegram.org/bot{settings.telegram_bot_token}"

    if info:
        resp = httpx.get(f"{api}/getWebhookInfo", timeout=30)
        typer.echo(json.dumps(resp.json(), indent=2, ensure_ascii=False))
        return
    if remove:
        resp = httpx.post(f"{api}/deleteWebhook", json={"drop_pending_updates": True}, timeout=30)
        typer.echo("webhook removed" if resp.json().get("ok") else f"failed: {resp.text}")
        return
    if not url:
        typer.echo("Usage: bazcar webhook --url https://.../api/telegram  |  --remove  |  --info")
        raise typer.Exit(code=1)

    secret = settings.telegram_webhook_secret or os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if not secret:
        typer.echo("[ERROR] TELEGRAM_WEBHOOK_SECRET not set (add it to .env and to Vercel env).", err=True)
        raise typer.Exit(code=1)
    resp = httpx.post(
        f"{api}/setWebhook",
        json={"url": url, "secret_token": secret, "allowed_updates": ["message", "callback_query"]},
        timeout=30,
    )
    data = resp.json()
    if data.get("ok"):
        typer.echo(f"[OK] webhook set -> {url}")
        typer.echo("[NOTE] polling (bazcar bot) is now disabled — Telegram pushes updates instead.")
    else:
        typer.echo(f"[ERROR] {data}", err=True)
        raise typer.Exit(code=1)


@app.command()
def version() -> None:
    """Print version."""
    typer.echo(f"bazcar {__version__}")


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        app()
    except BazcarError as exc:
        typer.echo(f"[ERROR] {exc}", err=True)
        raise typer.Exit(code=1) from None


if __name__ == "__main__":
    main()
