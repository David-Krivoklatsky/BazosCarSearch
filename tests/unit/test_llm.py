"""Unit tests for the LLM evaluation (Phase 3) — mocked OpenRouter."""

from __future__ import annotations

import asyncio
import json

import pytest
import respx
from httpx import Response

from bazcar.core.exceptions import ConfigError, LlmError
from bazcar.core.models import DealEvaluation, Listing
from bazcar.llm.provider import LLMProvider, parse_evaluation


def _listing(**overrides) -> Listing:
    base = dict(
        ad_id=195357798,
        url="https://auto.bazos.sk/inzerat/195357798/kia-sportage.php",
        title="Kia Sportage 1.6 T-GDI Platinum 2022",
        price_eur=22500,
        description_preview="Kia Sportage 2022, 33 650 km",
        year=2022,
        mileage_km=33650,
    )
    base.update(overrides)
    return Listing(**base)


def test_parse_evaluation_valid() -> None:
    eval_ = parse_evaluation('{"score": 88, "why": "Veľmi dobrá cena za rok 2022."}')
    assert eval_ == DealEvaluation(score=88, why="Veľmi dobrá cena za rok 2022.")


def test_parse_evaluation_handles_code_block_wrapper() -> None:
    raw = 'Here is your answer:\n```json\n{"score": 42, "why": "Priemerná ponuka"}\n```'
    eval_ = parse_evaluation(raw)
    assert eval_ is not None
    assert eval_.score == 42
    assert eval_.why == "Priemerná ponuka"


def test_parse_evaluation_rejects_missing_score() -> None:
    assert parse_evaluation('{"why": "no score"}') is None


def test_parse_evaluation_rejects_out_of_range() -> None:
    assert parse_evaluation('{"score": 150, "why": "prehnané"}') is None


def test_parse_evaluation_rejects_plain_text() -> None:
    assert parse_evaluation("Toto nie je JSON.") is None


@respx.mock
@pytest.mark.asyncio
async def test_provider_evaluates_listing() -> None:
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '{"score": 91, "why": "Skvelá cena pod trhom."}',
                        }
                    }
                ]
            },
        )
    )
    async with LLMProvider("test-key", model="test/model") as llm:
        eval_ = await llm.evaluate(_listing())
    assert eval_ is not None
    assert eval_.score == 91
    assert eval_.model == "test/model"
    assert route.called
    request = route.calls[0].request
    body = json.loads(request.content)
    assert body["model"] == "test/model"
    assert body["response_format"] == {"type": "json_object"}


@respx.mock
@pytest.mark.asyncio
async def test_provider_raises_llm_error_on_http_error() -> None:
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=Response(401, json={"error": {"message": "User not found."}})
    )
    async with LLMProvider("bad-key") as provider:
        with pytest.raises(LlmError, match="OpenRouter request failed"):
            await provider.evaluate(_listing())


def test_provider_requires_key() -> None:
    with pytest.raises(LlmError, match="OPENROUTER_API_KEY is empty"):
        LLMProvider("")


def test_runner_skips_evaluation_without_key() -> None:
    from bazcar.config.settings import get_settings
    from bazcar.pipeline.runner import _evaluate

    settings = get_settings()
    if settings.openrouter_api_key:
        pytest.skip("OPENROUTER_API_KEY is set — cannot test no-key path")
    result = asyncio.run(_evaluate([_listing()], enabled=None))
    assert result == 0


def test_runner_raises_when_eval_explicit_but_no_key() -> None:
    from bazcar.config.settings import get_settings
    from bazcar.pipeline.runner import _evaluate

    settings = get_settings()
    if settings.openrouter_api_key:
        pytest.skip("OPENROUTER_API_KEY is set — cannot test no-key path")
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        asyncio.run(_evaluate([_listing()], enabled=True))
