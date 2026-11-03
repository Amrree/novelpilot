"""Domain model for NovelPilot.

The central design decision in this file: a novel is represented as *structured
state* (the Story Bible) plus a sequence of generated artifacts (plans, scenes,
chapters).  Prose is the output; the Bible is the source of truth.

That inversion is what makes long-form generation tractable.  A 90k-word novel
does not fit in any context window, and naive "write chapter N" prompting fails
because the model has no reliable memory of what happened in chapter N-7.
NovelPilot instead keeps every narrative fact in queryable tables and
reconstructs a small, targeted "continuity pack" for each scene.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------
# Identity helpers
# --------------------------------------------------------------------------


def _uuid() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Stage(str, enum.Enum):
    """Where a project sits in the generation pipeline."""

    BRIEF = "brief"
    OUTLINE = "outline"
    BEATS = "beats"
    CHAPTERS = "chapters"
    DRAFTING = "drafting"
    COMPLETE = "complete"


class PovType(str, enum.Enum):
    FIRST_PERSON = "first_person"
    THIRD_PERSON_PAST = "third_person_past"
    THIRD_PERSON_PRESENT = "third_person_present"
    FIRST_PERSON_PRESENT = "first_person_present"
    MULTI_POV = "multi_pov"


class ThreadStatus(str, enum.Enum):
    OPEN = "open"
    ADVANCING = "advancing"
    RESOLVED = "resolved"
    ABANDONED = "abandoned"


# --------------------------------------------------------------------------
# Story Bible entities
# --------------------------------------------------------------------------


class Character(BaseModel):
    """A character with the attributes that must stay stable across the book.

    Everything in ``voice`` and ``physical`` is load-bearing: these are exactly
    the facts that language models most often contradict when generating
    chapter by chapter (eye colour changing, a scar appearing in chapter 20).
    """

    id: str = Field(default_factory=_uuid)
    name: str
    aliases: list[str] = Field(default_factory=list)
    role: Literal["protagonist", "antagonist", "deuteragonist", "mentor", "foil", "minor"] = "minor"
    age: int | None = None
    gender: str | None = None
    pronouns: str | None = None

    # --- identity that must never drift -------------------------------
    physical: dict[str, str] = Field(
        default_factory=dict,
        description="Stable physical traits: hair, eyes, height, distinguishing marks.",
    )
    background: str = ""
    goal: str = ""
    motivation: str = ""
    internal_conflict: str = ""
    flaw: str = ""
    arc: str = Field(default="", description="Where this character ends up vs. where they start.")

    # --- voice --------------------------------------------------------
    voice: dict[str, Any] = Field(
        default_factory=dict,
        description="Speech fingerprint: diction, rhythm, register, verbal tics, signature words.",
    )
    dialogue_style: str = ""
    silence_style: str = Field(default="", description="How they behave when they do not speak.")

    # --- relational state ----------------------------------------------
    relationships: dict[str, str] = Field(
        default_factory=dict, description="character_id -> current relationship description"
    )
    knowledge: list[str] = Field(
        default_factory=list, description="Things this character knows. Used to prevent leaks."
    )

    def summary_for_prompt(self) -> str:
        """Compact rendering injected into scene prompts."""
        bits = [f"{self.name} ({self.role})"]
        if self.age:
            bits.append(f"age {self.age}")
        if self.pronouns:
            bits.append(self.pronouns)
        if self.physical:
            bits.append("; ".join(f"{k}: {v}" for k, v in self.physical.items()))
        if self.goal:
            bits.append(f"goal: {self.goal}")
        if self.voice:
            bits.append("voice: " + "; ".join(f"{k}={v}" for k, v in self.voice.items()))
        return " | ".join(bits)


class Location(BaseModel):
    id: str = Field(default_factory=_uuid)
    name: str
    kind: Literal["room", "building", "city", "region", "world", "other"] = "other"
    description: str = ""
    details: dict[str, str] = Field(default_factory=dict)
    sensory: str = Field(default="", description="Signature sensory detail for this place.")


class PlotThread(BaseModel):
    """A thread that may open in one chapter and close in another.

    Threads are the main source of "dangling plot" bugs; the engine keeps an
    explicit ledger of them and refuses to finish a book with open threads
    unless the author opts out.
    """

    id: str = Field(default_factory=_uuid)
    title: str
    description: str = ""
    status: ThreadStatus = ThreadStatus.OPEN
    introduced_in: int | None = None
    resolved_in: int | None = None
    stakes: Literal["low", "medium", "high", "critical"] = "medium"
    related_characters: list[str] = Field(default_factory=list)
    payoff_note: str = Field(default="", description="How/where this thread is meant to resolve.")


class FactualEvent(BaseModel):
    """An immutable fact that happened. The continuity checker defends these."""

    id: str = Field(default_factory=_uuid)
    chapter: int
    scene: int | None = None
    summary: str
    participants: list[str] = Field(default_factory=list)
    irreversible: bool = Field(
        default=False, description="True if later prose must not contradict or undo this."
    )


class ChapterSummary(BaseModel):
    """Rolling summary per chapter; the coarse level of the memory hierarchy."""

    chapter: int
    title: str = ""
    summary: str = ""
    pov_character_id: str | None = None
    events: list[str] = Field(default_factory=list)
    opened_threads: list[str] = Field(default_factory=list)
    resolved_threads: list[str] = Field(default_factory=list)
    word_count: int = 0
    emotional_valence: float = Field(
        default=0.0, ge=-1.0, le=1.0, description="Perceived mood shift, -1 dark to +1 light."
    )
    tension: float = Field(default=0.0, ge=0.0, le=1.0)


class StoryBible(BaseModel):
    """The single source of truth for a novel.

    Stored as structured rows so that a scene can retrieve exactly the slice it
    needs ("what does Mara know?", "which threads are open?") instead of
    re-reading the whole manuscript.
    """

    # validate_assignment means a caller can assign plain dicts and get real
    # model objects back, instead of a runtime error three steps later.
    model_config = ConfigDict(validate_assignment=True)

    title: str = ""
    genre: str = ""
    subgenre: str = ""
    logline: str = ""
    premise: str = ""
    target_word_count: int = 80_000
    pov: PovType = PovType.THIRD_PERSON_PAST
    tense: str = "past"
    audience: str = ""
    content_notes: list[str] = Field(default_factory=list)
    style_directives: list[str] = Field(default_factory=list)
    banned_phrases: list[str] = Field(default_factory=list)

    characters: list[Character] = Field(default_factory=list)
    locations: list[Location] = Field(default_factory=list)
    threads: list[PlotThread] = Field(default_factory=list)
    events: list[FactualEvent] = Field(default_factory=list)
    chapter_summaries: list[ChapterSummary] = Field(default_factory=list)

    # --- generation bookkeeping ---------------------------------------
    stage: Stage = Stage.BRIEF
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    # -- lookups ------------------------------------------------------
    def character(self, char_id: str) -> Character | None:
        return next((c for c in self.characters if c.id == char_id), None)

    def character_by_name(self, name: str) -> Character | None:
        low = name.strip().lower()
        for c in self.characters:
            names = {c.name.lower(), *(a.lower() for a in c.aliases)}
            if low in names:
                return c
        return None

    def open_threads(self) -> list[PlotThread]:
        return [t for t in self.threads if t.status in (ThreadStatus.OPEN, ThreadStatus.ADVANCING)]

    def facts_up_to(self, chapter: int) -> list[FactualEvent]:
        return [e for e in self.events if e.chapter <= chapter]

    def summaries_up_to(self, chapter: int) -> list[ChapterSummary]:
        return [s for s in self.chapter_summaries if s.chapter <= chapter]

    def validate(self) -> list[str]:
        """Structural integrity checks run before and after each generation step."""
        problems: list[str] = []
        ids = [c.id for c in self.characters]
        if len(ids) != len(set(ids)):
            problems.append("duplicate character ids")
        names = [c.name.lower() for c in self.characters]
        if len(names) != len(set(names)):
            problems.append("duplicate character names")
        for c in self.characters:
            for rel_id in c.relationships:
                if rel_id not in ids:
                    problems.append(f"character {c.name!r} references unknown character {rel_id}")
        known_thread_ids = {t.id for t in self.threads}
        for t in self.threads:
            for cid in t.related_characters:
                if cid not in ids:
                    problems.append(f"thread {t.title!r} references unknown character {cid}")
            if t.status is ThreadStatus.RESOLVED and t.resolved_in is None:
                problems.append(f"thread {t.title!r} resolved without resolved_in")
        for e in self.events:
            for cid in e.participants:
                if cid not in ids:
                    problems.append(f"event in ch{e.chapter} references unknown character {cid}")
        return problems


# --------------------------------------------------------------------------
# Planning artifacts
# --------------------------------------------------------------------------


class ActPlan(BaseModel):
    number: int
    name: str
    goal: str
    turns: list[str] = Field(default_factory=list, description="Plot turns that end this act.")
    chapters: list[int] = Field(default_factory=list)


class BeatSheet(BaseModel):
    """Story beats mapped to chapter positions.

    Beats are the load-bearing unit of pacing: they say what *happens* and
    where tension should sit, independent of prose.
    """

    id: str = Field(default_factory=_uuid)
    label: str
    description: str
    chapter: int
    tension: float = Field(default=0.5, ge=0.0, le=1.0)
    pov_character_id: str | None = None
    participants: list[str] = Field(default_factory=list)


class Outline(BaseModel):
    acts: list[ActPlan] = Field(default_factory=list)
    beats: list[BeatSheet] = Field(default_factory=list)
    ending: str = ""
    title_candidates: list[str] = Field(default_factory=list)


class SceneCard(BaseModel):
    """The unit that is actually written.

    A scene card is a complete, self-contained instruction for one scene:
    who is present, where, what changes, what the reader must feel.  The
    writer never sees the raw outline -- it sees a scene card.  This is what
    keeps prose on-rails.
    """

    id: str = Field(default_factory=_uuid)
    chapter: int
    index: int
    heading: str = ""
    pov_character_id: str | None = None
    location_id: str | None = None
    participants: list[str] = Field(default_factory=list)
    goal: str = ""
    conflict: str = ""
    outcome: str = ""
    emotional_valence: float = 0.0
    tension: float = 0.5
    must_include: list[str] = Field(default_factory=list)
    must_avoid: list[str] = Field(default_factory=list)
    thread_ids: list[str] = Field(default_factory=list)
    approx_words: int = 1200
    continuity_pack: str = Field(
        default="", description="Assembled context injected into the writing prompt."
    )


class ChapterPlan(BaseModel):
    chapter: int
    title: str = ""
    summary: str = ""
    pov_character_id: str | None = None
    location_id: str | None = None
    scenes: list[SceneCard] = Field(default_factory=list)
    target_words: int = 2500
    purpose: str = ""


class ContinuityIssue(BaseModel):
    severity: Literal["info", "warning", "error"] = "warning"
    kind: str = ""
    message: str = ""
    character_id: str | None = None
    chapter: int | None = None
    suggestion: str = ""


class ChapterDraft(BaseModel):
    chapter: int
    title: str = ""
    text: str = ""
    word_count: int = 0
    scenes: list[str] = Field(default_factory=list)
    issues: list[ContinuityIssue] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)


# --------------------------------------------------------------------------
# Job envelope
# --------------------------------------------------------------------------


class StepStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class StepResult(BaseModel):
    name: str
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    output: dict[str, Any] = Field(default_factory=dict)


class Project(BaseModel):
    """Top-level project record tying a Bible, a plan, and drafts together."""

    id: str = Field(default_factory=_uuid)
    slug: str
    title: str
    bible: StoryBible = Field(default_factory=StoryBible)
    outline: Outline = Field(default_factory=Outline)
    chapter_plans: list[ChapterPlan] = Field(default_factory=list)
    drafts: dict[int, ChapterDraft] = Field(default_factory=dict)
    steps: dict[str, StepResult] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @field_validator("slug")
    @classmethod
    def _slugify(cls, v: str) -> str:
        v = v.strip().lower().replace(" ", "-")
        return "".join(ch for ch in v if ch.isalnum() or ch in "-_") or "untitled"
