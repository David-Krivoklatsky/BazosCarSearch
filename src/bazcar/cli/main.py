"""Typer CLI for bazcar."""

from __future__ import annotations

import asyncio
import logging
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
) -> None:
    """Run a Phase-1 scrape of a Bazoš category and export listings to JSON."""
    from bazcar.pipeline.runner import run_scrape

    summary = asyncio.run(
        run_scrape(
            max_pages=pages,
            limit=limit,
            detail=detail,
            detail_limit=detail_limit,
            export_path=export,
        )
    )
    typer.echo(
        f"[OK] scraped {summary.total_found} listings "
        f"(pages: {len(summary.pages_scraped)}), exported -> {summary.export_path}"
    )


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
