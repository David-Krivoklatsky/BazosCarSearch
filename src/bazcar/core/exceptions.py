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


class DbError(BazcarError):
    """Database connection or query failure (Phase 2 persistence)."""


class LlmError(BazcarError):
    """LLM/OpenRouter call failure (Phase 3 evaluation)."""


class RateLimitedError(LlmError):
    """OpenRouter returned HTTP 429.

    ``retry_after`` mirrors the response's ``Retry-After`` header (seconds)
    when present, so the caller can back off for as long as asked.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after
