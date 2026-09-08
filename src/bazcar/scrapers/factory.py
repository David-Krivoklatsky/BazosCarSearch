"""Scraper registry / factory."""

from __future__ import annotations

from bazcar.config.settings import ScraperConfig
from bazcar.core.exceptions import ConfigError
from bazcar.scrapers.base import BaseScraper
from bazcar.scrapers.bazos import BazosScraper


def get_scraper(platform: str, config: ScraperConfig) -> BaseScraper:
    """Return the scraper implementation for ``platform``."""
    if platform == "bazos":
        return BazosScraper(config)
    raise ConfigError(f"unsupported platform: {platform!r}")
