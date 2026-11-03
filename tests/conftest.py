"""Shared fixtures, including a deterministic fake provider.

The fake provider lets the whole pipeline run in tests without a network or an
API key, and without spending money.  It returns canned but *structurally
valid* responses, so the pipeline's parsing and state-sync logic is genuinely
exercised.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from novelpilot.core.bible import BibleStore
from novelpilot.core.engine import Engine
from novelpilot.core.models import ChapterPlan, Character, Location, SceneCard, StoryBible
from novelpilot.providers.base import LLMResponse, TokenUsage


class FakeProvider:
    """Returns recorded responses keyed by a marker in the prompt."""

    name = "fake"

    def __init__(self, responses: dict[str, str] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[Any] = []
        self.default = json.dumps({"issues": [], "events": [], "opened_threads": [],
                                   "resolved_threads": [], "character_updates": []})

    async def complete(self, request: Any) -> LLMResponse:
        self.calls.append(request)
        text = self.default
        prompt = "\n".join(m.content for m in request.messages)
        for marker, canned in self.responses.items():
            if marker in prompt:
                text = canned
                break
        return LLMResponse(
            text=text,
            model=request.model,
            usage=TokenUsage(prompt_tokens=len(prompt) // 4, completion_tokens=len(text) // 4),
        )

    async def stream(self, request: Any):
        yield "streamed "

    async def health(self) -> bool:
        return True

    def models(self) -> list:
        return []


@pytest.fixture
def store(tmp_path) -> BibleStore:
    s = BibleStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def bible() -> StoryBible:
    b = StoryBible(
        title="The Salt Road",
        genre="fantasy",
        logline="A courier crosses a dissolving empire.",
        premise="Salt, empire, a message that ends a war.",
        target_word_count=80_000,
        style_directives=["No purple prose."],
        banned_phrases=["delve into"],
    )
    b.characters = [
        Character(
            id="c-mara",
            name="Mara",
            role="protagonist",
            physical={"eyes": "grey", "hair": "black, cropped"},
            goal="Deliver the message",
            flaw="Cannot let go of control",
            arc="Learns to trust someone else",
            knowledge=["The message is a forgery"],
            relationships={"c-elias": "former apprentice"},
        ),
        Character(
            id="c-elias",
            name="Elias",
            role="deuteragonist",
            physical={"eyes": "brown"},
            goal="Survive the crossing",
        ),
    ]
    b.locations = [Location(id="l-road", name="The Salt Road", kind="region",
                            description="A white causeway across a dead sea.",
                            sensory="Salt crust crunches underfoot.")]
    b.events = [
        {"chapter": 1, "summary": "Mara accepted the contract.", "participants": ["c-mara"]},
        {"chapter": 2, "summary": "Mara's left hand was broken.", "participants": ["c-mara"],
         "irreversible": True},
    ]
    return b


@pytest.fixture
def scene(bible: StoryBible) -> SceneCard:
    return SceneCard(
        chapter=3,
        index=0,
        heading="The Crossing",
        pov_character_id="c-mara",
        location_id="l-road",
        participants=["c-mara", "c-elias"],
        goal="Get past the checkpoint",
        conflict="The checkpoint knows her face",
        outcome="Mara crosses but loses her satchel",
        must_include=["the checkpoint"],
        must_avoid=["the broken hand is healed"],
        thread_ids=[],
        approx_words=400,
    )


@pytest.fixture
def fake_engine() -> Engine:
    return Engine(
        provider=FakeProvider(),
        models={"plan": "test-model", "prose": "test-model", "summary": "test-model",
                "judge": "test-model"},
        temperature=0.5,
        max_cost_usd=100.0,
    )
