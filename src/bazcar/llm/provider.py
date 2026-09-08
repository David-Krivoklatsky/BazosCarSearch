"""OpenRouter-backed LLM provider for deal evaluation (Phase 3)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from bazcar.core.exceptions import LlmError
from bazcar.core.models import DealEvaluation, Listing

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Si analytik ojazdených áut pre slovenský trh (Bazoš.sk). Na základe ceny, "
    "roku výroby, najazdených km, názvu a popisu ohodnoť, aká dobrá kúpa je "
    "daný inzerát na stupnici 0–100 (100 = vynikajúci obchod). "
    "Odpovedaj IBA JSON: {\"score\": <int 0-100>, \"why\": \"<1-2 viet po slovensky: dôvod>\"}."
)


def _listing_text(listing: Listing) -> str:
    parts = [
        f"Title: {listing.title}",
        f"Price: {listing.price_eur} EUR" if listing.price_eur is not None else "Price: unknown (Dohodou)",
        f"Year: {listing.year}" if listing.year else "",
        f"Mileage: {listing.mileage_km} km" if listing.mileage_km else "",
        f"City: {listing.city}" if listing.city else "",
        f"Views: {listing.views}" if listing.views else "",
        f"Description: {listing.description or listing.description_preview}",
    ]
    return "\n".join(p for p in parts if p)


def parse_evaluation(content: str) -> DealEvaluation | None:
    """Parse the model's JSON reply into a DealEvaluation (None on failure)."""
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        logger.warning("LLM reply contained no JSON object: %r", content[:200])
        return None
    try:
        payload: dict[str, Any] = json.loads(match.group(0))
        eval_text = str(payload.get("why", "")).strip()
        score = int(payload["score"])
        return DealEvaluation(score=score, why=eval_text)
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("LLM reply not a valid evaluation JSON: %s (%r)", exc, content[:200])
        return None


class LLMProvider:
    """Thin async client over the OpenRouter chat completions API."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://openrouter.ai/api/v1",
        model: str = "openai/gpt-4o-mini",
        temperature: float = 0.2,
        max_tokens: int = 400,
    ) -> None:
        if not api_key:
            raise LlmError("OPENROUTER_API_KEY is empty")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=httpx.Timeout(60.0, connect=10.0),
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> LLMProvider:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def evaluate(self, listing: Listing) -> DealEvaluation | None:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _listing_text(listing)},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
        }
        try:
            resp = await self._client.post(f"{self.base_url}/chat/completions", json=payload)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except Exception as exc:
            raise LlmError(f"OpenRouter request failed for ad {listing.ad_id}: {exc}") from exc
        evaluation = parse_evaluation(content)
        if evaluation is not None:
            evaluation.model = self.model
        return evaluation
