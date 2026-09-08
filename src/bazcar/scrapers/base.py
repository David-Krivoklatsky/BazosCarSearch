"""Abstract HTTP scraper shared by all platform scrapers.

Responsibilities:
  * owning the shared ``httpx.AsyncClient`` (timeouts, defaults, redirects),
  * per-request user-agent rotation,
  * polite rate limiting,
  * retries with exponential backoff on transient failures,
  * block page (ban) detection.
"""

from __future__ import annotations

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from typing import Any

import httpx

from bazcar.config.settings import ScraperConfig
from bazcar.core.exceptions import BanDetected, RateLimited, ScraperError
from bazcar.core.utils import AsyncRateLimiter

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    def __init__(self, config: ScraperConfig) -> None:
        self.config = config
        http = config.http
        self._rate_limiter = AsyncRateLimiter(
            min_interval_ms=http.min_interval_ms, jitter_ms=http.jitter_ms
        )
        headers = {"User-Agent": self._random_ua()}
        headers.update(
            {
                "Accept": http.headers.get("accept", "*/*"),
                "Accept-Language": http.headers.get("accept_language", "sk-SK,sk;q=0.9"),
            }
        )
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(http.timeout_seconds),
            follow_redirects=True,
        )

    @property
    def config(self) -> ScraperConfig:
        return self._config

    @config.setter
    def config(self, value: ScraperConfig) -> None:
        self._config = value

    def _random_ua(self) -> str:
        return random.choice(self.config.user_agents)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> BaseScraper:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    def _detect_ban(self, status_code: int, text: str) -> bool:
        """Return True when the response looks like a block page."""
        if status_code == 403:
            return True
        lowered = text.lower()
        return any(marker in lowered for marker in self.config.markers.ban_markers)

    async def fetch_text(self, url: str) -> str:
        """GET ``url`` with rate limiting, UA rotation, retries and ban detection."""
        last_error: ScraperError | None = None
        for attempt in range(self.config.http.retries + 1):
            await self._rate_limiter.wait()
            request_headers = {"User-Agent": self._random_ua()}
            try:
                response = await self._client.get(url, headers=request_headers)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = ScraperError(f"transport error for {url}: {exc}")
                await self._backoff(attempt)
                continue

            if response.status_code == 429:
                last_error = RateLimited(f"{url} -> HTTP 429")
                await self._backoff(attempt, retry_after=response.headers.get("Retry-After"))
                continue
            if response.status_code >= 500:
                last_error = ScraperError(f"{url} -> HTTP {response.status_code}")
                await self._backoff(attempt)
                continue

            if self._detect_ban(response.status_code, response.text):
                raise BanDetected(f"blocked by anti-bot at {url} (HTTP {response.status_code})")

            response.raise_for_status()
            return response.text

        raise last_error or ScraperError(f"request failed after retries: {url}")

    async def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        if retry_after is not None and retry_after.isdigit():
            delay = min(float(retry_after), self.config.http.backoff_max_seconds)
        else:
            base = self.config.http.backoff_base_seconds
            delay = min(base * (2**attempt), self.config.http.backoff_max_seconds)
        delay += random.uniform(0.0, 0.5)
        logger.debug("backoff %.1fs (attempt %d)", delay, attempt + 1)
        await asyncio.sleep(delay)

    @abstractmethod
    async def scrape_category(self, category_url: str, max_pages: int = 1) -> list:
        """Return parsed listings from the first ``max_pages`` pages of ``category_url``."""
