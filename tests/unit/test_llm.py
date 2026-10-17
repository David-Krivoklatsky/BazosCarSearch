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


def test_parse_evaluation_handles_none_content() -> None:
    assert parse_evaluation(None) is None


def test_parse_evaluation_v2_fields() -> None:
    raw = (
        '{"score": 81, "why": "Solídna ponuka.", "risk": "vysoké km", '
        '"is_car": true, "condition": "zachovalé, servisované", "target_offer": 7300}'
    )
    eval_ = parse_evaluation(raw)
    assert eval_ is not None
    assert eval_.score == 81
    assert eval_.condition == "zachovalé, servisované"
    assert eval_.target_offer == 7300


def test_parse_evaluation_v1_still_parses() -> None:
    eval_ = parse_evaluation('{"score": 70, "why": "ok", "risk": "km"}')
    assert eval_ is not None
    assert eval_.condition == ""
    assert eval_.target_offer is None


def test_parse_evaluation_offer_edge_cases() -> None:
    assert parse_evaluation('{"score": 50, "target_offer": null}').target_offer is None
    assert parse_evaluation('{"score": 50, "target_offer": "7 300"}').target_offer == 7300
    assert parse_evaluation('{"score": 50, "target_offer": "7\u00a0500 €"}').target_offer == 7500
    assert parse_evaluation('{"score": 50, "target_offer": "6200 EUR"}').target_offer == 6200
    assert parse_evaluation('{"score": 50, "target_offer": -100}').target_offer is None
    assert parse_evaluation('{"score": 50, "target_offer": "abc"}').target_offer is None


def test_parse_evaluation_finds_json_inside_reasoning_prose() -> None:
    content = (
        "Here's a thinking process:\n"
        "1. **Analyze the car**: it's cheap for its year.\n"
        "2. Final answer:\n"
        '{"score": 73, "why": "Primeraná cena za rok a km."}\n'
        "That completes the evaluation."
    )
    eval_ = parse_evaluation(content)
    assert eval_ is not None
    assert eval_.score == 73


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
    async with LLMProvider(
        "test-key",
        model="test/model",
        temperature=0.5,
        top_p=0.9,
        frequency_penalty=0.0,
        presence_penalty=0.1,
        response_format="json_object",
    ) as llm:
        eval_ = await llm.evaluate(_listing())
    assert eval_ is not None
    assert eval_.score == 91
    assert eval_.model == "test/model"
    assert route.called
    request = route.calls[0].request
    body = json.loads(request.content)
    assert body["model"] == "test/model"
    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == 0.5
    assert body["top_p"] == 0.9
    assert body["frequency_penalty"] == 0.0
    assert body["presence_penalty"] == 0.1
    assert body["stream"] is False
    assert "seed" not in body


@respx.mock
@pytest.mark.asyncio
async def test_provider_returns_none_on_http_error() -> None:
    """Free-tier flakiness must not raise — one retry, then None."""
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=Response(502, json={"error": {"message": "upstream error"}})
    )
    async with LLMProvider("bad-key") as provider:
        result = await provider.evaluate(_listing())
    assert result is None
    assert route.call_count == 2  # initial attempt + one retry


@respx.mock
@pytest.mark.asyncio
async def test_provider_honors_retry_after(monkeypatch) -> None:
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=Response(
            429,
            headers={"retry-after": "2"},
            json={"error": {"message": "rate limited"}},
        )
    )
    delays: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr("bazcar.llm.provider.asyncio.sleep", _fake_sleep)
    async with LLMProvider("test-key") as llm:
        result = await llm.evaluate(_listing())
    assert result is None
    assert route.call_count == 3  # initial + two retries
    assert delays == [2.0, 2.0]  # Retry-After honoured, no jitter


@respx.mock
@pytest.mark.asyncio
async def test_provider_backoff_is_exponential_without_retry_after(monkeypatch) -> None:
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=Response(429, json={"error": {"message": "rate limited"}})
    )
    delays: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr("bazcar.llm.provider.asyncio.sleep", _fake_sleep)
    async with LLMProvider("test-key") as llm:
        await llm.evaluate(_listing())
    assert len(delays) == 2
    assert 15.0 <= delays[0] <= 16.0  # RATE_LIMIT_BASE_S + up to 1s jitter
    assert 30.0 <= delays[1] <= 31.0  # doubled + up to 1s jitter


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

def test_prompt_presets_share_output_contract() -> None:
    from bazcar.llm.provider import PROMPT_META, normalize_prompt_name, prompt_names, resolve_prompt

    assert set(prompt_names()) == set(PROMPT_META)
    for name in prompt_names():
        body = resolve_prompt(name)
        assert "score" in body and "target_offer" in body and "is_car" in body
    assert normalize_prompt_name(None) == "standard"
    assert normalize_prompt_name("  BARGAIN ") == "bargain"
    assert normalize_prompt_name("nope") == "standard"
    assert resolve_prompt("nope") == resolve_prompt("standard")


@respx.mock
@pytest.mark.asyncio
async def test_evaluate_sends_named_prompt() -> None:
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "{\"score\": 77}"}}]},
        )
    )
    async with LLMProvider("test-key", model="test/model") as llm:
        eval_ = await llm.evaluate(_listing(), prompt_name="bargain")
    assert eval_ is not None and eval_.score == 77
    body = json.loads(route.calls[0].request.content)
    system_text = " ".join(m["content"] for m in body["messages"] if m["role"] == "system")
    assert "lovec" in system_text  # bargain preset, not the standard prompt


@respx.mock
@pytest.mark.asyncio
async def test_list_models_cached_serves_fresh_cache() -> None:
    import datetime

    from bazcar.llm.provider import list_models_cached

    raw = [{"id": "x/model", "name": "X", "supported_parameters": ["response_format"],
            "pricing": {"prompt": "0", "completion": "0"},
            "per_request_limits": {}, "context_length": 64000},
           {"id": "x/chat", "name": "Y", "supported_parameters": ["tools"],
            "pricing": {"prompt": "1", "completion": "1"},
            "per_request_limits": {}, "context_length": 8000}]

    class _Repo:
        def __init__(self):
            self.saved = None

        async def get_model_cache(self):
            return {"models": raw,
                    "fetched_at": datetime.datetime.now(datetime.UTC)}

        async def save_model_cache(self, data):
            self.saved = data

    route = respx.get("https://openrouter.ai/api/v1/models").mock(
        return_value=Response(200, json={"data": raw})
    )
    repo = _Repo()
    models, stamp = await list_models_cached("K", repo)
    assert [m["id"] for m in models] == ["x/model"]  # only JSON-capable
    assert models[0]["free"] is True
    assert stamp is not None
    assert not route.called  # fresh cache => no network
    assert repo.saved is None


@respx.mock
@pytest.mark.asyncio
async def test_list_models_cached_refreshes_stale_cache() -> None:
    import datetime

    from bazcar.llm.provider import list_models_cached

    raw = [{"id": "x/model", "name": "X", "supported_parameters": ["structured_outputs"],
            "pricing": {"prompt": "0.5", "completion": "1"},
            "per_request_limits": {"requests_per_minute": 30},
            "context_length": 128000}]

    class _Repo:
        def __init__(self):
            self.saved = None

        async def get_model_cache(self):
            return {"models": [],
                    "fetched_at": datetime.datetime(2020, 1, 1,
                                                    tzinfo=datetime.UTC)}

        async def save_model_cache(self, data):
            self.saved = data

    route = respx.get("https://openrouter.ai/api/v1/models").mock(
        return_value=Response(200, json={"data": raw})
    )
    repo = _Repo()
    models, _ = await list_models_cached("K", repo, max_age_s=60)
    assert route.called  # stale cache => live refresh
    assert repo.saved == raw
    assert models[0]["rpm"] == 30
    assert models[0]["free"] is False
