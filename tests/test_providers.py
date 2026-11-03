"""Provider adapters, request shaping and role routing."""

from __future__ import annotations

import json

import pytest

from novelpilot.core.engine import Engine
from novelpilot.providers.base import (
    LLMRequest,
    LLMResponse,
    Message,
    ProviderError,
    Role,
    TokenUsage,
)
from novelpilot.providers.openai_compat import ENDPOINTS, KEY_ENV, OpenAICompatProvider
from novelpilot.providers.registry import MODELS, model_spec, resolve_model


def test_local_providers_are_reachable_without_a_key() -> None:
    p = OpenAICompatProvider(provider="ollama")
    assert p.is_local
    assert p._needs_key() is False


def test_hosted_providers_require_a_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    p = OpenAICompatProvider(provider="openai")
    assert p._needs_key() is True


def test_request_serialisation_is_openai_shaped() -> None:
    req = LLMRequest(
        messages=[Message.system("you are a writer"), Message.user("write")],
        model="gpt-4o",
        temperature=0.7,
        max_tokens=100,
        json_mode=True,
    )
    d = req.as_dict()
    assert d["model"] == "gpt-4o"
    assert d["messages"][0] == {"role": "system", "content": "you are a writer"}
    assert d["response_format"] == {"type": "json_object"}


def test_response_parses_fenced_json() -> None:
    resp = LLMResponse(text='```json\n{"a": 1}\n```', model="m")
    assert resp.json() == {"a": 1}


def test_response_parses_json_with_trailing_prose() -> None:
    resp = LLMResponse(text='Here you go: {"a": 1} Hope that helps!', model="m")
    assert resp.json() == {"a": 1}


def test_response_parses_bare_array() -> None:
    resp = LLMResponse(text='[1, 2, 3]', model="m")
    assert resp.json() == [1, 2, 3]


def test_response_raises_on_unparseable_json() -> None:
    resp = LLMResponse(text="not json at all", model="m")
    with pytest.raises(json.JSONDecodeError):
        resp.json()


def test_endpoints_cover_local_and_hosted() -> None:
    assert ENDPOINTS["ollama"] == "http://localhost:11434/v1"
    assert ENDPOINTS["openai"].startswith("https://api.openai.com")
    assert "anthropic" not in ENDPOINTS  # it has its own adapter


def test_key_env_names_are_defined_for_every_hosted_provider() -> None:
    for provider in ("openai", "groq", "openrouter", "deepseek", "mistral"):
        assert provider in KEY_ENV


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


def test_every_model_has_a_valid_spec() -> None:
    for m in MODELS:
        assert m.id and m.provider
        assert m.context_window > 0
        assert m.input_cost_per_mtok >= 0
        assert m.output_cost_per_mtok >= 0


def test_role_routing_prefers_a_role_affinity() -> None:
    assert resolve_model(Role.PROSE, provider="anthropic") == "claude-sonnet-4-5"
    assert resolve_model(Role.SUMMARY, provider="openai") == "gpt-4o-mini"


def test_role_routing_falls_back_when_no_affinity() -> None:
    model = resolve_model(Role.COVER, provider="anthropic")
    assert model in [m.id for m in MODELS if m.provider == "anthropic"]


def test_override_always_wins() -> None:
    assert resolve_model(Role.PROSE, provider="openai", override="my-tuned-model") == "my-tuned-model"


def test_model_spec_lookup_handles_provider_qualified_names() -> None:
    assert model_spec("claude-sonnet-4-5").provider == "anthropic"
    assert model_spec("openrouter/auto").provider == "openrouter"
    assert model_spec("does-not-exist") is None


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


class _StubProvider:
    name = "stub"

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.seen.append(request.model)
        return LLMResponse(text='{"ok": true}', model=request.model,
                           usage=TokenUsage(prompt_tokens=100, completion_tokens=50))

    async def stream(self, request: LLMRequest):
        yield "x"

    async def health(self) -> bool:
        return True

    def models(self) -> list:
        return []


async def test_engine_records_cost() -> None:
    eng = Engine(provider=_StubProvider(), models={"plan": "gpt-4o"}, max_cost_usd=10.0)
    await eng.complete_text([("user", "hi")], role=Role.PLAN)
    assert eng.total_tokens == 150
    assert eng.total_cost > 0


async def test_engine_budget_guard_blocks_further_calls() -> None:
    eng = Engine(provider=_StubProvider(), models={"plan": "gpt-4o"}, max_cost_usd=0.0000001)
    await eng.complete_text([("user", "hi")], role=Role.PLAN)
    with pytest.raises(ProviderError, match="budget guard"):
        await eng.complete_text([("user", "hi again")], role=Role.PLAN)


async def test_engine_uses_the_role_specific_model() -> None:
    prov = _StubProvider()
    eng = Engine(provider=prov, models={"plan": "gpt-4o", "prose": "claude-sonnet-4-5"},
                 max_cost_usd=10.0)
    await eng.complete_text([("user", "x")], role=Role.PLAN)
    await eng.complete_text([("user", "y")], role=Role.PROSE)
    assert prov.seen == ["gpt-4o", "claude-sonnet-4-5"]


def test_cost_breakdown_is_sorted_by_cost() -> None:
    from novelpilot.core.engine import StepCost

    eng = Engine(provider=_StubProvider(), max_cost_usd=10.0)
    eng.steps.append(StepCost(step="a", model="m", usd=0.1))
    eng.steps.append(StepCost(step="b", model="m", usd=0.9))
    rows = eng.cost_breakdown()
    assert rows[0]["step"] == "b"
