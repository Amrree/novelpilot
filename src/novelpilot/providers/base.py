"""Provider-agnostic LLM interface.

NovelPilot deliberately defines its own small protocol rather than depending on
a framework like LangChain.  The generation pipeline is a *known, fixed* DAG;
a general agent framework would add abstraction, indirection and a large
dependency surface without removing any real complexity.

What we do need is a clean seam so the same engine runs against a frontier API
and against a local model on a laptop.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, runtime_checkable

# --------------------------------------------------------------------------
# Message / request primitives
# --------------------------------------------------------------------------


class MessageRole(str, enum.Enum):
    """The role of a single message in a chat conversation."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(slots=True)
class Message:
    role: MessageRole
    content: str

    @classmethod
    def system(cls, content: str) -> "Message":
        return cls(MessageRole.SYSTEM, content)

    @classmethod
    def user(cls, content: str) -> "Message":
        return cls(MessageRole.USER, content)

    @classmethod
    def assistant(cls, content: str) -> "Message":
        return cls(MessageRole.ASSISTANT, content)


@dataclass(slots=True)
class LLMRequest:
    messages: list[Message]
    model: str
    temperature: float = 0.8
    max_tokens: int = 4096
    top_p: float = 0.95
    stop: list[str] = field(default_factory=list)
    json_mode: bool = False
    seed: int | None = None
    timeout: float = 180.0
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role.value, "content": m.content} for m in self.messages],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": self.top_p,
        }
        if self.stop:
            d["stop"] = self.stop
        if self.json_mode:
            d["response_format"] = {"type": "json_object"}
        if self.seed is not None:
            d["seed"] = self.seed
        d.update(self.extra)
        return d


@dataclass(slots=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(slots=True)
class LLMResponse:
    text: str
    model: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    raw: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None

    def json(self) -> Any:
        """Parse the response as JSON, tolerating markdown fences.

        Local models (especially 7B-13B) love wrapping JSON in ```json fences
        or adding a trailing sentence, so this is deliberately forgiving.
        """
        text = self.text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.lstrip().lower().startswith("json"):
                text = text.lstrip()[4:]
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Salvage the outermost JSON object/array.
            for opener, closer in (("{", "}"), ("[", "]")):
                start = text.find(opener)
                end = text.rfind(closer)
                if start != -1 and end > start:
                    try:
                        return json.loads(text[start : end + 1])
                    except json.JSONDecodeError:
                        continue
            raise


class ProviderError(RuntimeError):
    """Raised for upstream failures that are worth retrying."""

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


# --------------------------------------------------------------------------
# Model catalogue
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ModelSpec:
    """Static metadata about a model: context window, pricing, role affinity.

    ``role_affinity`` drives model routing inside the pipeline: planning wants
    reasoning, prose wants a good writing model.  Keeping this in one table
    means adding a model is a data change, not a code change.
    """

    id: str
    provider: str
    context_window: int = 8192
    input_cost_per_mtok: float = 0.0
    output_cost_per_mtok: float = 0.0
    supports_json: bool = True
    supports_streaming: bool = True
    role_affinity: tuple[str, ...] = ("prose",)
    notes: str = ""


class Role(str, enum.Enum):
    """What a given call needs from the model."""

    PLAN = "plan"
    PROSE = "prose"
    SUMMARY = "summary"
    JUDGE = "judge"
    COVER = "cover"


# --------------------------------------------------------------------------
# Provider protocol
# --------------------------------------------------------------------------


@runtime_checkable
class LLMProvider(Protocol):
    """Every provider implements this.  The engine only ever sees this."""

    name: str

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Single-shot completion."""
        ...

    async def stream(self, request: LLMRequest) -> Iterable[str]:
        """Yield text chunks as they arrive (used for live chapter drafting)."""
        ...

    async def health(self) -> bool:
        """True when the provider is reachable and configured."""
        ...

    def models(self) -> list[ModelSpec]:
        """Models this provider can serve."""
        ...
