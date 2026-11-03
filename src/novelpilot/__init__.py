"""NovelPilot -- the AI novel engine that actually writes books."""

__version__ = "0.1.0"

from .core.models import (  # noqa: F401
    ChapterDraft,
    ChapterPlan,
    Character,
    FactualEvent,
    Location,
    Outline,
    PlotThread,
    SceneCard,
    StoryBible,
)

__all__ = [
    "__version__",
    "Character",
    "ChapterDraft",
    "ChapterPlan",
    "FactualEvent",
    "Location",
    "Outline",
    "PlotThread",
    "SceneCard",
    "StoryBible",
]
