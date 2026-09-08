"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest
from bazcar.config.settings import ScraperConfig, load_scraper_config

from bazcar.scrapers.bazos import BazosScraper

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def scraper_config() -> ScraperConfig:
    return load_scraper_config()


@pytest.fixture(scope="session")
def bazos_page_html() -> str:
    return (FIXTURES / "bazos_page.html").read_text(encoding="utf-8")


@pytest.fixture
def bazos_scraper(scraper_config: ScraperConfig) -> BazosScraper:
    return BazosScraper(scraper_config)
