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

# How long a cached OpenRouter /models response stays valid (once a day).
MODELS_CACHE_TTL_S = 86400

SYSTEM_PROMPT = (
    "Si analytik ojazdených áut pre slovenský trh (Bazoš.sk). Na základe ceny, "
    "roku výroby, najazdených km, názvu a popisu ohodnoť, aká dobrá kúpa je "
    "daný inzerát 0–100 (100 = vynikajúci obchod), podľa kritérií používateľa "
    "(ak nie sú, hodnotiť všeobecne). V poli risk uveď NAJVÄČŠÍ možný problém "
    "alebo prečo by kúpa mohla byť riziková (1 veta, po slovensky; ak žiadne "
    "riziko, krátke 'najazdené' alebo 'neznáme' ). V poli condition zhrň "
    "predpokladaný stav vozidla podľa popisu (2-4 slová, po slovensky, napr. "
    "'zachovalé, servisované'). V poli target_offer navrhni primeranú prvú "
    "ponuku v EUR na vyjednávanie (celé číslo, nižšia než požadovaná cena; "
    "null ak cena nie je známa). Odpovedaj LEN jedným JSON "
    "objektom (žiadny text pred ani za ním): "
    "{\"score\": <int 0-100>, \"why\": \"<1-2 viet po slovensky: dôvod>\", "
    "\"risk\": \"<najväčší možný problém po slovensky>\", "
    "\"is_car\": <true ak inzerát predáva celé auto, inak false — svetlá/disky/pn/a sedaky>, "
    "\"condition\": \"<predpokladaný stav, 2-4 slová>\", "
    "\"target_offer\": <int EUR alebo null>}."
)

# Named evaluation prompts. Every variant keeps the exact same JSON output
# contract (keys + Slovak values) so parsing never depends on the preset —
# only the scoring emphasis changes.
PROMPTS: dict[str, str] = {
    "standard": SYSTEM_PROMPT,
    "bargain": (
        "Si lovec výhodných kúb ojazdených áut na slovenskom trhu (Bazoš.sk). "
        "Cenu porovnaj s trhom a ohodnoť inzerát 0–100 podľa toho, ako VÝHODNÁ je kúpa "
        "(100 = výrazne pod cenou trhu). Vyšší nájazd km alebo kozmetické vady toleruj, "
        "ak je cena výrazne pod trhom; skryté technické riziká penalizuj. "
        "V poli risk uveď NAJVÄČŠÍ možný problém (1 veta, po slovensky; ak žiadne "
        "riziko, krátke 'najazdené' alebo 'neznáme'). V poli condition zhrň "
        "predpokladaný stav (2-4 slová, po slovensky). V poli target_offer navrhni "
        "primeranú prvú ponuku v EUR (celé číslo, nižšia než požadovaná cena; "
        "null ak cena nie je známa). Odpovedaj LEN jedným JSON "
        "objektom (žiadny text pred ani za ním): "
        "{\"score\": <int 0-100>, \"why\": \"<1-2 viet po slovensky: dôvod>\", "
        "\"risk\": \"<najväčší možný problém po slovensky>\", "
        "\"is_car\": <true ak inzerát predáva celé auto, inak false>, "
        "\"condition\": \"<predpokladaný stav, 2-4 slová>\", "
        "\"target_offer\": <int EUR alebo null>}."
    ),
    "family": (
        "Si poradca pre rodinné auto na slovenskom trhu (Bazoš.sk). Ohodnoť inzerát "
        "0–100 podľa vhodnosti pre rodinu s deťmi: bezpečnosť, spoľahlivosť, priestor "
        "a nízke prevádzkové náklady vážia najviac; akékoľvek bezpečnostné alebo "
        "spoľahlivostné riziko výrazne penalizuj. V poli risk uveď NAJVÄČŠÍ možný "
        "problém (1 veta, po slovensky; ak žiadne riziko, krátke 'najazdené' alebo "
        "'neznáme'). V poli condition zhrň predpokladaný stav (2-4 slová, po slovensky). "
        "V poli target_offer navrhni primeranú prvú ponuku v EUR (celé číslo, nižšia "
        "než požadovaná cena; null ak cena nie je známa). Odpovedaj LEN jedným JSON "
        "objektom (žiadny text pred ani za ním): "
        "{\"score\": <int 0-100>, \"why\": \"<1-2 viet po slovensky: dôvod>\", "
        "\"risk\": \"<najväčší možný problém po slovensky>\", "
        "\"is_car\": <true ak inzerát predáva celé auto, inak false>, "
        "\"condition\": \"<predpokladaný stav, 2-4 slová>\", "
        "\"target_offer\": <int EUR alebo null>}."
    ),
}

# English labels for the prompt picker (UI strings stay English; the prompt
# bodies above must stay Slovak — the listings are Slovak).
PROMPT_META: dict[str, str] = {
    "standard": "Standard — balanced deal score",
    "bargain": "Bargain hunter — rewards underpriced cars",
    "family": "Family — rewards safety and reliability",
}


def prompt_names() -> list[str]:
    """Available evaluation prompt presets, in picker order."""
    return list(PROMPTS)


def resolve_prompt(name: str | None) -> str:
    """Prompt body for ``name`` (unknown names fall back to standard)."""
    return PROMPTS.get((name or "").strip().lower(), PROMPTS["standard"])


def normalize_prompt_name(name: str | None) -> str:
    """Canonical preset name (unknown/empty -> standard)."""
    key = (name or "").strip().lower()
    return key if key in PROMPTS else "standard"


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


def _fmt_price(per_token: str) -> str:
    """Render per-token USD as a readable per-1M-token figure."""
    try:
        cost = float(per_token) * 1_000_000
    except (TypeError, ValueError):
        return "?"
    if cost < 0.001:
        return "<0.001"
    return f"{cost:g}"


async def list_models(api_key: str | None = None) -> list[dict]:
    """OpenRouter models that can output strict JSON, with key parameters.

    Each entry: ``id``, ``name``, ``free``, ``price_in``/``price_out``
    (per 1M tokens), ``rpm`` (requests/min limit — None = unlimited),
    ``ctx`` (context length). Only models advertising ``response_format`` or
    ``structured_outputs`` are included (our prompt requires JSON).
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(
            f"{DEFAULT_BASE_URL.rstrip('/')}/models", headers=headers
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
    models: list[dict] = []
    for model in data:
        supports_json = bool(
            {"response_format", "structured_outputs"}
            & set(model.get("supported_parameters") or [])
        )
        if not supports_json:
            continue
        pricing = model.get("pricing") or {}
        prompt_cost = pricing.get("prompt", "1")
        limits = model.get("per_request_limits") or {}
        models.append(
            {
                "id": model.get("id", ""),
                "name": model.get("name") or model.get("id", ""),
                "free": prompt_cost == "0",
                "price_in": _fmt_price(prompt_cost),
                "price_out": _fmt_price(pricing.get("completion", "1")),
                "rpm": limits.get("requests_per_minute"),
                "ctx": model.get("context_length"),
            }
        )
    return models


async def list_models_cached(
    api_key: str | None = None,
    repo=None,
    max_age_s: int = MODELS_CACHE_TTL_S,
) -> tuple[list[dict], object | None]:
    """JSON-capable models, refreshed from OpenRouter at most once a day.

    Returns ``(models, fetched_at)`` — ``fetched_at`` is None when nothing
    could be loaded (no cache and no network). Without a ``repo`` this is a
    plain live request (serverless-safe fallback).
    """
    import datetime

    if repo is not None:
        try:
            cached = await repo.get_model_cache()
        except Exception:
            cached = None
        if cached and cached.get("models"):
            fetched_at = cached.get("fetched_at")
            try:
                age = (
                    datetime.datetime.now(datetime.UTC) - fetched_at
                ).total_seconds() if fetched_at else float("inf")
            except TypeError:
                age = float("inf")
            if age < max_age_s:
                return _filter_json_models(cached["models"]), fetched_at
    data = await _fetch_models_raw(api_key)
    if repo is not None and data:
        try:
            await repo.save_model_cache(data)
        except Exception:
            logger.warning("model cache write failed", exc_info=True)
    fetched_at = await _cache_stamp(repo)
    return _filter_json_models(data), fetched_at


async def _fetch_models_raw(api_key: str | None) -> list[dict]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(
            f"{DEFAULT_BASE_URL.rstrip('/')}/models", headers=headers
        )
        resp.raise_for_status()
        return resp.json().get("data", [])


async def _cache_stamp(repo) -> object | None:
    if repo is None:
        return None
    try:
        cached = await repo.get_model_cache()
        return cached.get("fetched_at") if cached else None
    except Exception:
        return None


def _filter_json_models(data: list[dict]) -> list[dict]:
    """Keep the JSON-capable entries of a raw OpenRouter /models payload."""
    models: list[dict] = []
    for model in data:
        supports_json = bool(
            {"response_format", "structured_outputs"}
            & set(model.get("supported_parameters") or [])
        )
        if not supports_json:
            continue
        pricing = model.get("pricing") or {}
        prompt_cost = pricing.get("prompt", "1")
        limits = model.get("per_request_limits") or {}
        models.append(
            {
                "id": model.get("id", ""),
                "name": model.get("name") or model.get("id", ""),
                "free": prompt_cost == "0",
                "price_in": _fmt_price(prompt_cost),
                "price_out": _fmt_price(pricing.get("completion", "1")),
                "rpm": limits.get("requests_per_minute"),
                "ctx": model.get("context_length"),
            }
        )
    return models


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
            condition = str(payload.get("condition", "") or "").strip()
            offer_raw = payload.get("target_offer")
            target_offer = None
            if offer_raw is not None:
                try:
                    cleaned = (
                        str(offer_raw)
                        .replace("\u00a0", "")
                        .replace(" ", "")
                        .replace("€", "")
                        .replace("EUR", "")
                        .replace("eur", "")
                    )
                    target_offer = int(cleaned)
                    if target_offer <= 0:
                        target_offer = None
                except (ValueError, TypeError):
                    target_offer = None
            return DealEvaluation(
                score=score,
                why=why,
                risk=risk,
                is_car=is_car,
                condition=condition,
                target_offer=target_offer,
            )
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

    async def evaluate(
        self,
        listing: Listing,
        *,
        criteria: str | None = None,
        prompt_name: str = "standard",
    ) -> DealEvaluation | None:
        system_prompt = resolve_prompt(prompt_name)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
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
        usage: dict[str, Any] | None = None
        try:
            content, usage = await self._completion(payload, listing)
        except LlmError as exc:
            rate_limited = "rate limited" in str(exc)
            if rate_limited:
                # Free tier: 429 persists for ~a minute — wait it out, retry once.
                logger.warning("LLM rate limited for ad %s — backing off 30s", listing.ad_id)
                await asyncio.sleep(30)
            else:
                logger.warning("LLM call failed (%s), retrying once", exc)
                await asyncio.sleep(1.5)
            try:
                content, usage = await self._completion(payload, listing)
            except LlmError as exc2:
                if rate_limited:
                    logger.warning("LLM still rate limited for ad %s after backoff", listing.ad_id)
                    # give the limiter one long breath, then a final attempt
                    await asyncio.sleep(30)
                    try:
                        content, usage = await self._completion(payload, listing)
                    except LlmError as exc3:
                        logger.warning("LLM final attempt failed for ad %s: %s", listing.ad_id, exc3)
                        return None
                else:
                    logger.warning("LLM retry also failed: %s", exc2)
                    return None
        if content is None:
            return None
        evaluation = parse_evaluation(content)
        if evaluation is None:
            # Free-tier models are flaky — one retry with slightly higher temperature.
            payload["temperature"] = min(float(self.temperature or 0.0) + 0.6, 1.0)
            content, usage = await self._completion(payload, listing)
            if content is None:
                return None
            evaluation = parse_evaluation(content)
        if evaluation is not None:
            evaluation.model = self.model
            self._attach_usage(evaluation, usage)
        return evaluation

    @staticmethod
    def _attach_usage(evaluation: DealEvaluation, usage: dict[str, Any] | None) -> None:
        """Copy token counts (+ cost when the gateway reports it) onto the score."""
        if not usage:
            return
        try:
            prompt = usage.get("prompt_tokens")
            completion = usage.get("completion_tokens") or usage.get("completionTokens")
            evaluation.prompt_tokens = int(prompt) if prompt is not None else None
            evaluation.completion_tokens = (
                int(completion) if completion is not None else None
            )
            cost = usage.get("cost") or usage.get("total_cost")
            evaluation.cost_usd = float(cost) if cost is not None else None
        except (TypeError, ValueError):
            logger.debug("unparseable LLM usage payload: %r", usage)

    async def _completion(
        self, payload: dict[str, Any], listing: Listing
    ) -> tuple[str | None, dict[str, Any] | None]:
        try:
            resp = await self._client.post(f"{self.base_url}/chat/completions", json=payload)
            resp.raise_for_status()
            data = resp.json()
            usage = data.get("usage")
            return data["choices"][0]["message"].get("content"), (
                dict(usage) if isinstance(usage, dict) else None
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                raise LlmError(f"rate limited (429) for ad {listing.ad_id}") from exc
            raise LlmError(f"OpenRouter request failed for ad {listing.ad_id}: {exc}") from exc
        except Exception as exc:
            raise LlmError(f"OpenRouter request failed for ad {listing.ad_id}: {exc}") from exc
