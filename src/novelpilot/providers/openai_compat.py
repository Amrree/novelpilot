"""OpenAI-compatible chat-completions adapter.

This single class covers the large majority of inference backends because they
all speak the OpenAI ``/v1/chat/completions`` dialect:

    OpenAI, Azure OpenAI, Groq, OpenRouter, Together, Fireworks, DeepSeek,
    Mistral, xAI, NVIDIA NIM, and -- crucially for local development --
    Ollama, LM Studio, llama.cpp's server, vLLM and Text Generation Inference.

Only the URL and API-key handling differ, so those are the only knobs.  This
is the single most important cost/complexity decision in the provider layer:
one adapter instead of ten.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterable
from typing import Any

import httpx

from .base import LLMProvider, LLMRequest, LLMResponse, ModelSpec, ProviderError, TokenUsage

# Well-known endpoints for the OpenAI-compatible providers we support.
ENDPOINTS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "ollama": "http://localhost:11434/v1",
    "lmstudio": "http://localhost:1234/v1",
    "vllm": "http://localhost:8000/v1",
    "llamacpp": "http://localhost:8080/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "together": "https://api.together.xyz/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "mistral": "https://api.mistral.ai/v1",
    "xai": "https://api.x.ai/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
}

# Env var that holds the key for each provider.
KEY_ENV: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "together": "TOGETHER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "xai": "XAI_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "ollama": "OLLAMA_API_KEY",
    "lmstudio": "LMSTUDIO_API_KEY",
    "vllm": "VLLM_API_KEY",
    "llamacpp": "LLAMACPP_API_KEY",
}

LOCAL_PROVIDERS = {"ollama", "lmstudio", "vllm", "llamacpp"}


class OpenAICompatProvider:
    """Async client for any OpenAI-compatible endpoint."""

    def __init__(
        self,
        provider: str = "openai",
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float = 180.0,
        max_retries: int = 3,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.name = provider
        self.base_url = (base_url or ENDPOINTS.get(provider, ENDPOINTS["openai"])).rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv(KEY_ENV.get(provider, ""), "")
        self.timeout = timeout
        self.max_retries = max_retries
        self.extra_headers = extra_headers or {}
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=15.0),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
                **self.extra_headers,
            },
        )

    # -- capability -----------------------------------------------------
    @property
    def is_local(self) -> bool:
        return self.name in LOCAL_PROVIDERS

    def _needs_key(self) -> bool:
        # Local servers usually run keyless; hosted ones do not.
        return not self.is_local and not self.api_key

    # -- request --------------------------------------------------------
    async def complete(self, request: LLMRequest) -> LLMResponse:
        if self._needs_key():
            raise ProviderError(
                f"provider {self.name!r} needs an API key (set {KEY_ENV.get(self.name, 'API_KEY')})"
            )
        payload = request.as_dict()
        payload["stream"] = False
        data = await self._post("/chat/completions", payload, request.timeout)
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
            finish = choice.get("finish_reason")
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"malformed response from {self.name}: {data!r}") from exc
        usage_raw = data.get("usage") or {}
        usage = TokenUsage(
            prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
            completion_tokens=int(usage_raw.get("completion_tokens") or 0),
        )
        return LLMResponse(
            text=text,
            model=data.get("model") or request.model,
            usage=usage,
            raw=data,
            finish_reason=finish,
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[str]:
        """Yield raw text chunks.  Falls back to a single chunk if the server
        does not implement streaming (some TGI/llama.cpp builds do not)."""
        if self._needs_key():
            raise ProviderError(f"provider {self.name!r} needs an API key")
        payload = request.as_dict()
        payload["stream"] = True
        async with self._client.stream(
            "POST", "/chat/completions", json=payload, timeout=request.timeout
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise ProviderError(
                    f"{self.name} returned {resp.status_code}: {body[:300]!r}",
                    retryable=resp.status_code >= 500 or resp.status_code == 429,
                    status=resp.status_code,
                )
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    import json as _json

                    obj = _json.loads(chunk)
                    delta = obj["choices"][0].get("delta", {})
                    piece = delta.get("content")
                    if piece:
                        yield piece
                except Exception:  # noqa: BLE001 - a malformed frame must not kill the stream
                    continue

    # -- helpers --------------------------------------------------------
    async def _post(self, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._client.post(path, json=payload, timeout=timeout)
                if resp.status_code >= 400:
                    body = resp.text[:300]
                    retryable = resp.status_code >= 500 or resp.status_code == 429
                    err = ProviderError(
                        f"{self.name} returned {resp.status_code}: {body}",
                        retryable=retryable,
                        status=resp.status_code,
                    )
                    if retryable and attempt < self.max_retries:
                        last_exc = err
                        await asyncio.sleep(min(2**attempt * 1.5, 20.0))
                        continue
                    raise err
                return resp.json()
            except ProviderError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(min(2**attempt * 1.5, 20.0))
        raise ProviderError(
            f"{self.name} request failed after {self.max_retries + 1} attempts: {last_exc}",
            retryable=True,
        )

    async def health(self) -> bool:
        try:
            resp = await self._client.get("/models", timeout=10.0)
            return resp.status_code < 500
        except Exception:  # noqa: BLE001
            return False

    def models(self) -> list[ModelSpec]:
        from .registry import MODELS

        return [m for m in MODELS if m.provider == self.name]

    async def close(self) -> None:
        await self._client.aclose()

    def list_models_remote(self) -> list[str]:
        """Synchronously-unfriendly helper used by the CLI; returns [] on failure."""
        return []
