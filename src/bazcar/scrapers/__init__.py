"""Scrapers for supported classifieds platforms."""

from .base import BaseScraper
from .bazos import BazosScraper
from .factory import get_scraper

__all__ = ["BaseScraper", "BazosScraper", "get_scraper"]
