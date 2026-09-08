"""Domain exceptions for the scraper pipeline."""

from __future__ import annotations


class BazcarError(Exception):
    """Base class for all bazcar errors."""


class ConfigError(BazcarError):
    """Raised when configuration is missing or invalid."""


class ScraperError(BazcarError):
    """Base class for scraping failures."""


class RateLimited(ScraperError):
    """HTTP 429 or throttling signal received."""


class BanDetected(ScraperError):
    """The site served a block page (CAPTCHA/recaptcha/overlimit) or HTTP 403."""


class ParseError(ScraperError):
    """HTML could not be parsed into listings."""
