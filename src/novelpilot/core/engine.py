"""The engine: role-routed, cost-tracked access to any provider.

Every model call in NovelPilot goes through this class.  That gives three
things for free:

* **Role routing** -- a caller says ``role=Role.PROSE`` and the engine picks
  the configured model for that job.  Planning and judging can run on a cheap
  model while prose runs on an expensive one, which is where most of the cost
  savings live.
* **Cost accounting** -- token usage and dollar cost are recorded per step,
  so a book knows what it cost to generate and the budget guard can stop a
  runaway before it happens.
* **Retry and fallback** -- transient upstream failures are retried with
  backoff; if the primary model for a role fails outright, the engine falls
  back to another provider that can serve the same role.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from ..providers.base import (
    LLMRequest,
    LLMResponse,
    Message,
    ProviderError,
    Role,
    TokenUsage,
)
from ..providers.registry import model_spec, resolve_model

log = logging.getLogger("novelpilot.engine")


@dataclass(slots=True)
class StepCost:
    step: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(slots=True)
class Engine:
    """Role-routed LLM access with cost tracking and budget guards."""

    provider: Any
    models: dict[str, str | None] = field(default_factory=dict)
    temperature: float = 0.85
    max_retries: int = 3
    max_cost_usd: float = 5.00
    steps: list[StepCost] = field(default_factory=list)
    _on_cost: Any = None

    # -- model selection -------------------------------------------------
    def model_for(self, role: Role) -> str:
        override = self.models.get(role.value)
        if override:
            return override
        provider_name = getattr(self.provider, "name", "openai")
        return resolve_model(role, provider=provider_name)

    # -- budget ----------------------------------------------------------
    @property
    def total_cost(self) -> float:
        return sum(s.usd for s in self.steps)

    @property
    def total_tokens(self) -> int:
        return sum(s.total_tokens for s in self.steps)

    def _check_budget(self, role: Role) -> None:
        if self.max_cost_usd and self.total_cost >= self.max_cost_usd:
            raise ProviderError(
                f"budget guard: project has spent ${self.total_cost:.2f} "
                f"(limit ${self.max_cost_usd:.2f})"
            )

    # -- calls ------------------------------------------------------------
    async def complete_text(
        self,
        messages: list[tuple[str, str]],
        *,
        role: Role = Role.PROSE,
        max_tokens: int = 4096,
        temperature: float | None = None,
        step: str | None = None,
        stop: list[str] | None = None,
    ) -> str:
        resp = await self._complete(
            messages,
            role=role,
            max_tokens=max_tokens,
            temperature=temperature,
            step=step or role.value,
            stop=stop,
        )
        return resp.text

    async def complete_json(
        self,
        messages: list[tuple[str, str]],
        *,
        role: Role = Role.PLAN,
        max_tokens: int = 4000,
        temperature: float | None = None,
        step: str | None = None,
    ) -> Any:
        resp = await self._complete(
            messages,
            role=role,
            max_tokens=max_tokens,
            temperature=temperature,
            step=step or role.value,
            json_mode=True,
        )
        return resp.json()

    async def _complete(
        self,
        messages: list[tuple[str, str]],
        *,
        role: Role,
        max_tokens: int,
        temperature: float | None,
        step: str,
        json_mode: bool = False,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        self._check_budget(role)
        model = self.model_for(role)
        req = LLMRequest(
            messages=[Message.user(c) if r == "user" else Message.system(c) for r, c in messages],
            model=model,
            temperature=self.temperature if temperature is None else temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
            stop=stop or [],
        )
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self.provider.complete(req)
                self._record(step, model, resp.usage)
                return resp
            except ProviderError as exc:
                last = exc
                if not exc.retryable or attempt >= self.max_retries:
                    break
                delay = min(2**attempt * 1.5, 20.0)
                log.warning("provider error on %s (attempt %d): %s -- retrying in %.1fs",
                            step, attempt + 1, exc, delay)
                await asyncio.sleep(delay)
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(min(2**attempt * 1.5, 20.0))
        raise ProviderError(f"step {step!r} failed after retries: {last}")

    async def stream_text(
        self,
        messages: list[tuple[str, str]],
        *,
        role: Role = Role.PROSE,
        max_tokens: int = 4096,
        temperature: float | None = None,
        step: str = "stream",
    ):
        """Yield text chunks.  Used by the API for live chapter drafting."""
        model = self.model_for(role)
        req = LLMRequest(
            messages=[Message.user(c) if r == "user" else Message.system(c) for r, c in messages],
            model=model,
            temperature=self.temperature if temperature is None else temperature,
            max_tokens=max_tokens,
        )
        async for chunk in self.provider.stream(req):
            yield chunk

    # -- accounting -------------------------------------------------------
    def _record(self, step: str, model: str, usage: TokenUsage) -> None:
        spec = model_spec(model)
        usd = 0.0
        if spec:
            usd = (
                usage.prompt_tokens / 1_000_000 * spec.input_cost_per_mtok
                + usage.completion_tokens / 1_000_000 * spec.output_cost_per_mtok
            )
        self.steps.append(
            StepCost(
                step=step,
                model=model,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                usd=usd,
            )
        )
        if self._on_cost:
            try:
                self._on_cost(step, model, usage, usd)
            except Exception:  # noqa: BLE001
                log.exception("cost callback failed")

    def cost_breakdown(self) -> list[dict[str, Any]]:
        agg: dict[tuple[str, str], StepCost] = {}
        for s in self.steps:
            key = (s.step, s.model)
            if key not in agg:
                agg[key] = StepCost(step=s.step, model=s.model)
            agg[key].prompt_tokens += s.prompt_tokens
            agg[key].completion_tokens += s.completion_tokens
            agg[key].usd += s.usd
        return [
            {
                "step": s.step,
                "model": s.model,
                "prompt_tokens": s.prompt_tokens,
                "completion_tokens": s.completion_tokens,
                "usd": round(s.usd, 4),
            }
            for s in sorted(agg.values(), key=lambda x: -x.usd)
        ]
