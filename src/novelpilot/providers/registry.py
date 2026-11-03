"""Model catalogue and role-based routing.

This table is the single place where "which model writes what" is decided.
Adding a model is a data change; the pipeline asks for a *role* and the
registry picks the best available model for it.

Pricing figures are indicative (USD per million tokens, input/output) and
should be re-verified against each provider's pricing page before quoting
costs to customers.  ``docs/COST_MODEL.md`` explains how they are used.
"""

from __future__ import annotations

from .base import LLMProvider, ModelSpec, Role
from .openai_compat import OpenAICompatProvider

# --------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------

MODELS: list[ModelSpec] = [
    # --- OpenAI -------------------------------------------------------
    ModelSpec("gpt-4o", "openai", 128_000, 2.50, 10.00, role_affinity=("plan", "judge")),
    ModelSpec("gpt-4o-mini", "openai", 128_000, 0.15, 0.60, role_affinity=("summary", "judge")),
    ModelSpec("gpt-4.1", "openai", 1_047_576, 2.00, 8.00, role_affinity=("plan", "prose")),
    ModelSpec("gpt-4.1-mini", "openai", 1_047_576, 0.40, 1.60, role_affinity=("summary", "judge")),
    ModelSpec("o4-mini", "openai", 200_000, 1.10, 4.40, role_affinity=("plan", "judge")),
    # --- Anthropic ----------------------------------------------------
    ModelSpec("claude-sonnet-4-5", "anthropic", 200_000, 3.00, 15.00, role_affinity=("plan", "prose", "judge")),
    ModelSpec("claude-opus-4-1", "anthropic", 200_000, 15.00, 75.00, role_affinity=("plan", "judge")),
    ModelSpec("claude-haiku-4-5", "anthropic", 200_000, 1.00, 5.00, role_affinity=("summary", "judge")),
    # --- Groq / fast inference ----------------------------------------
    ModelSpec("llama-3.3-70b-versatile", "groq", 128_000, 0.59, 0.79, role_affinity=("prose", "summary")),
    ModelSpec("llama-3.1-8b-instant", "groq", 128_000, 0.05, 0.08, role_affinity=("summary",)),
    # --- DeepSeek -----------------------------------------------------
    ModelSpec("deepseek-chat", "deepseek", 128_000, 0.27, 1.10, role_affinity=("prose", "plan")),
    ModelSpec("deepseek-reasoner", "deepseek", 128_000, 0.55, 2.19, role_affinity=("plan", "judge")),
    # --- OpenRouter (aggregator) --------------------------------------
    ModelSpec("openrouter/auto", "openrouter", 128_000, 1.00, 3.00, role_affinity=("plan", "prose", "judge")),
    # --- Local (Ollama / LM Studio / vLLM) ---------------------------
    ModelSpec("llama3.1:70b", "ollama", 131_072, 0.0, 0.0, role_affinity=("prose", "plan")),
    ModelSpec("qwen2.5:14b", "ollama", 32_768, 0.0, 0.0, role_affinity=("prose", "summary")),
    ModelSpec("llama3.1:8b", "ollama", 131_072, 0.0, 0.0, role_affinity=("summary",)),
    ModelSpec("mistral-nemo:12b", "ollama", 131_072, 0.0, 0.0, role_affinity=("prose",)),
]


def model_spec(model_id: str) -> ModelSpec | None:
    for m in MODELS:
        if m.id == model_id:
            return m
    # Try a provider-qualified match, then a bare-name match.
    tail = model_id.split("/")[-1]
    for m in MODELS:
        if m.id == tail:
            return m
    return None


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


def resolve_model(role: Role, provider: str | None = None, override: str | None = None) -> str:
    """Pick the model for a role.

    ``override`` always wins (this is how the CLI/config pins a model).
    Otherwise we look for the best model on ``provider`` whose role_affinity
    contains ``role``; falling back to any model on that provider.
    """
    if override:
        return override
    if provider:
        candidates = [m for m in MODELS if m.provider == provider]
        if candidates:
            for m in candidates:
                if role.value in m.role_affinity:
                    return m.id
            return candidates[0].id
    for m in MODELS:
        if role.value in m.role_affinity:
            return m.id
    return MODELS[0].id


# --------------------------------------------------------------------------
# Provider construction
# --------------------------------------------------------------------------


def build_provider(provider: str = "openai", **kwargs) -> LLMProvider:
    """Instantiate a provider by name.

    ``anthropic`` gets its native adapter; everything else goes through the
    OpenAI-compatible adapter (Ollama included).
    """
    provider = provider.lower()
    if provider == "anthropic":
        from .anthropic import AnthropicProvider

        return AnthropicProvider(**kwargs)
    return OpenAICompatProvider(provider=provider, **kwargs)


def available_providers() -> dict[str, bool]:
    """Which providers look usable right now (key present or local)."""
    import os

    from .openai_compat import KEY_ENV, LOCAL_PROVIDERS

    out: dict[str, bool] = {}
    for m in MODELS:
        p = m.provider
        if p in out:
            continue
        if p in LOCAL_PROVIDERS:
            out[p] = True
        elif p == "anthropic":
            out[p] = bool(os.getenv("ANTHROPIC_API_KEY"))
        else:
            out[p] = bool(os.getenv(KEY_ENV.get(p, "")))
    return out
