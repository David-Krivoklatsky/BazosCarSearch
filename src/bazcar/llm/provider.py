"""OpenRouter-backed LLM provider for deal evaluation (Phase 3)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from bazcar.core.exceptions import LlmError
from bazcar.core.models import DealEvaluation, Listing

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

SYSTEM_PROMPT = (
    "Si analytik ojazdených áut pre slovenský trh (Bazoš.sk). Na základe ceny, "
    "roku výroby, najazdených km, názvu a popisu ohodnoť, aká dobrá kúpa je "
    "daný inzerát 0–100 (100 = vynikajúci obchod), podľa kritérií používateľa "
    "(ak nie sú, hodnotiť všeobecne). V poli risk uveď NAJVÄČŠÍ možný problém "
    "alebo prečo by kúpa mohla byť riziková (1 veta, po slovensky; ak žiadne "
    "riziko, krátke 'najazdené' alebo 'neznáme' ). Odpovedaj LEN jedným JSON "
    "objestom (žiadny text pred ani za ním): "
    "{\"score\": <int 0-100>, \"why\": \"<1-2 viet po slovensky: dôvod>\", "
    "\"risk\": \"<najväčší možný problém po slovensky>\", "
    "\"is_car\": <true ak inzerát predáva celé auto, inak false — svetlá/diskía/pn/a sedaky>}."
)


def _criteria_instruction(criteria: str | None) -> str:
    if not criteria:
        return "Kritériá používateľa: žiadne špecifické — hodnoti všeobecne."
    return f"Užívateľ hľadá vozidlo: {criteria}. Hodnoť bodovo podľa zhody s nimi."


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


async def list_models() -> tuple[list[str], list[str]]:
    """OpenRouter models that can output strict JSON, split (free, paid).

    Only models advertising ``response_format`` or ``structured_outputs`` in
    ``supported_parameters`` are listed — our evaluation prompt requires JSON.
    """
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(f"{DEFAULT_BASE_URL.rstrip('/')}/models")
        resp.raise_for_status()
        data = resp.json().get("data", [])
    free: list[str] = []
    paid: list[str] = []
    for model in data:
        model_id = model.get("id", "")
        supports_json = bool(
            {"response_format", "structured_outputs"}
            & set(model.get("supported_parameters") or [])
        )
        if not supports_json:
            continue
        pricing = model.get("pricing") or {}
        prompt_cost = pricing.get("prompt", "1")
        (free if prompt_cost == "0" else paid).append(model_id)
    return sorted(free), sorted(paid)


def parse_evaluation(content: str | None) -> DealEvaluation | None:
    """Parse the model's reply into a DealEvaluation (None on failure).

    Robust against free-tier models that wrap the JSON in prose or reasoning:
    scans for flat JSON objects and accepts the first valid one with a 0..100
    ``score``. ``None``/empty replies (safety refusals) also yield None.
    """
    if not content or not isinstance(content, str):
        logger.warning("LLM reply was empty (refusal): %r", content)
        return None

    def try_object(candidate: str) -> DealEvaluation | None:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        try:
            score = int(payload["score"])
            if not 0 <= score <= 100:
                return None
            why = str(payload.get("why", "")).strip()
            risk = str(payload.get("risk", "")).strip()
            is_car = bool(payload.get("is_car", True))
            return DealEvaluation(score=score, why=why, risk=risk, is_car=is_car)
        except (KeyError, ValueError, TypeError):
            return None

    for candidate in re.finditer(r"\{[^{}]*\}", content):
        evaluation = try_object(candidate.group(0))
        if evaluation is not None:
            return evaluation
    # Fallback: greedy brace-pair in case the JSON is nested.
    for match in re.finditer(r"\{.*\}", content, re.DOTALL):
        evaluation = try_object(match.group(0))
        if evaluation is not None:
            return evaluation
    logger.warning("LLM reply contained no parseable evaluation JSON: %r", content[:200])
    return None


class LLMProvider:
    """Thin async client over the OpenRouter chat completions API."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://openrouter.ai/api/v1",
        model: str = "openrouter/free",
        temperature: float = 0.0,
        max_tokens: int = 300,
        top_p: float | None = None,
        frequency_penalty: float | None = None,
        presence_penalty: float | None = None,
        response_format: str | None = None,
        stream: bool = False,
    ) -> None:
        if not api_key:
            raise LlmError("OPENROUTER_API_KEY is empty")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.top_p = top_p
        self.frequency_penalty = frequency_penalty
        self.presence_penalty = presence_penalty
        self.response_format = response_format
        self.stream = stream
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

    async def evaluate(self, listing: Listing, *, criteria: str | None = None) -> DealEvaluation | None:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "system", "content": _criteria_instruction(criteria)},
                {"role": "user", "content": _listing_text(listing)},
            ],
            "stream": self.stream,
        }
        optional = {
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": self.top_p,
            "frequency_penalty": self.frequency_penalty,
            "presence_penalty": self.presence_penalty,
            "response_format": {"type": self.response_format} if self.response_format else None,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        content: str | None = None
        try:
            content = await self._completion(payload, listing)
        except LlmError as exc:
            logger.warning("LLM call failed (%s), retrying once", exc)
            await asyncio.sleep(1.5)
            try:
                content = await self._completion(payload, listing)
            except LlmError as exc2:
                logger.warning("LLM retry also failed: %s", exc2)
                return None
        if content is None:
            return None
        evaluation = parse_evaluation(content)
        if evaluation is None:
            # Free-tier models are flaky — one retry with slightly higher temperature.
            payload["temperature"] = min(float(self.temperature or 0.0) + 0.6, 1.0)
            content = await self._completion(payload, listing)
            if content is None:
                return None
            evaluation = parse_evaluation(content)
        if evaluation is not None:
            evaluation.model = self.model
        return evaluation

    async def _completion(self, payload: dict[str, Any], listing: Listing) -> str | None:
        try:
            resp = await self._client.post(f"{self.base_url}/chat/completions", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"].get("content")
        except Exception as exc:
            raise LlmError(f"OpenRouter request failed for ad {listing.ad_id}: {exc}") from exc
