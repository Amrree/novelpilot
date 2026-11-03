"""Anthropic Messages API adapter.

Kept separate from the OpenAI-compatible adapter because the wire format
differs (system prompt is a top-level field, not a message) and because
Anthropic's prompt-caching support is worth exposing explicitly.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Message,
    MessageRole,
    ModelSpec,
    ProviderError,
    TokenUsage,
)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class AnthropicProvider:
    def __init__(
        self,
        model: str = "claude-sonnet-4-5",
        api_key: str | None = None,
        *,
        base_url: str = API_URL,
        timeout: float = 180.0,
        max_retries: int = 3,
    ) -> None:
        self.name = "anthropic"
        self.model = model
        self.api_key = api_key if api_key is not None else os.getenv("ANTHROPIC_API_KEY", "")
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=15.0),
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": API_VERSION,
            },
        )

    def _payload(self, request: LLMRequest) -> dict[str, Any]:
        system_parts = [m.content for m in request.messages if m.role is MessageRole.SYSTEM]
        convo = [{"role": m.role.value, "content": m.content} for m in request.messages if m.role is not MessageRole.SYSTEM]
        payload: dict[str, Any] = {
            "model": request.model or self.model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": convo or [{"role": "user", "content": "..."}],
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.stop:
            payload["stop_sequences"] = request.stop
        if request.seed is not None:
            payload["metadata"] = {"user_id": str(request.seed)}
        return payload

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if not self.api_key:
            raise ProviderError("anthropic needs an API key (set ANTHROPIC_API_KEY)")
        payload = self._payload(request)
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._client.post(self.base_url, json=payload, timeout=request.timeout)
                if resp.status_code >= 400:
                    body = resp.text[:300]
                    retryable = resp.status_code >= 500 or resp.status_code == 429
                    err = ProviderError(
                        f"anthropic returned {resp.status_code}: {body}",
                        retryable=retryable,
                        status=resp.status_code,
                    )
                    if retryable and attempt < self.max_retries:
                        last_exc = err
                        await asyncio.sleep(min(2**attempt * 1.5, 20.0))
                        continue
                    raise err
                data = resp.json()
                text = "".join(
                    blk.get("text", "") for blk in data.get("content", []) if blk.get("type") == "text"
                )
                usage_raw = data.get("usage") or {}
                return LLMResponse(
                    text=text,
                    model=data.get("model", request.model),
                    usage=TokenUsage(
                        prompt_tokens=int(usage_raw.get("input_tokens") or 0),
                        completion_tokens=int(usage_raw.get("output_tokens") or 0),
                    ),
                    raw=data,
                    finish_reason=data.get("stop_reason"),
                )
            except ProviderError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(min(2**attempt * 1.5, 20.0))
        raise ProviderError(f"anthropic request failed: {last_exc}", retryable=True)

    async def stream(self, request: LLMRequest) -> AsyncIterator[str]:
        if not self.api_key:
            raise ProviderError("anthropic needs an API key (set ANTHROPIC_API_KEY)")
        payload = self._payload(request)
        payload["stream"] = True
        async with self._client.stream("POST", self.base_url, json=payload, timeout=request.timeout) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise ProviderError(f"anthropic returned {resp.status_code}: {body[:300]!r}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    import json as _json

                    obj = _json.loads(line[5:].strip())
                except Exception:  # noqa: BLE001
                    continue
                if obj.get("type") == "content_block_delta":
                    delta = obj.get("delta") or {}
                    if delta.get("text"):
                        yield delta["text"]

    async def health(self) -> bool:
        if not self.api_key:
            return False
        try:
            resp = await self._client.post(
                self.base_url,
                json={"model": self.model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
                timeout=20.0,
            )
            return resp.status_code < 500
        except Exception:  # noqa: BLE001
            return False

    def models(self) -> list[ModelSpec]:
        from .registry import MODELS

        return [m for m in MODELS if m.provider == "anthropic"]

    async def close(self) -> None:
        await self._client.aclose()
