"""Planning: turn a premise into a story that can actually be executed.

The pipeline is **plan first, prose second**.  A scene card is a complete
instruction -- POV, location, participants, goal, conflict, outcome, required
elements, threads touched -- and the writer never sees the raw outline.  That
separation is why the output stays coherent: the creative decisions are made
once, in a context that fits in a prompt, and the prose step only has to
execute them.

The planner also enforces *pacing arithmetic*: beats carry a tension value and
must be distributed so the book has peaks and troughs rather than a flat
middle.
"""

from __future__ import annotations

import json
import math
from typing import Any

from .models import (
    ActPlan,
    BeatSheet,
    ChapterPlan,
    Outline,
    SceneCard,
    StoryBible,
)

# --------------------------------------------------------------------------
# Outline
# --------------------------------------------------------------------------

OUTLINE_PROMPT = """You are a structural editor for a {genre} novel.

PREMISE: {premise}
LOGLINE: {logline}
TARGET LENGTH: {words} words (~{chapters} chapters)
POV: {pov}

Produce a commercial, publishable three-act structure.

Rules:
- Act I is roughly 25% of the book, Act II 50%, Act III 25%.
- The midpoint must contain a real reversal, not just a complication.
- Every act needs at least one turn that changes the protagonist's situation.
- Tension must vary across the book: it should dip around the start of Act II
  and peak in Act III. Do not produce a flat tension line.
- The ending must resolve the primary thread. Do not leave the central
  conflict dangling.
- Offer 5 candidate titles that fit the genre and do not contain "The" more
  than once.

Return JSON only:
{{
  "title_candidates": ["..."],
  "ending": "How the book ends, in 2-4 sentences",
  "acts": [
    {{"number": 1, "name": "Act name", "goal": "What the protagonist wants in this act",
      "turns": ["turn 1", "turn 2"], "chapters": [1,2,3]}}
  ],
  "beats": [
    {{"label": "Inciting incident", "description": "What happens", "chapter": 1,
      "tension": 0.4, "participants": ["character names present"]}}
  ]
}}
"""


async def plan_outline(bible: StoryBible, llm) -> Outline:
    chapters = max(4, bible.target_word_count // 2500)
    prompt = OUTLINE_PROMPT.format(
        genre=bible.genre or "fiction",
        premise=bible.premise,
        logline=bible.logline,
        words=bible.target_word_count,
        chapters=chapters,
        pov=bible.pov.value.replace("_", " "),
    )
    data = await llm.complete_json(
        messages=[("user", prompt)], max_tokens=4000, temperature=0.75
    )
    outline = _outline_from_json(data, chapters)
    outline.beats = _normalize_tension(outline.beats, chapters)
    return outline


def _outline_from_json(data: dict[str, Any], chapters: int) -> Outline:
    acts: list[ActPlan] = []
    for a in data.get("acts", []) or []:
        if not isinstance(a, dict):
            continue
        try:
            acts.append(
                ActPlan(
                    number=int(a.get("number", len(acts) + 1)),
                    name=str(a.get("name", "")),
                    goal=str(a.get("goal", "")),
                    turns=[str(t) for t in (a.get("turns") or [])],
                    chapters=[int(c) for c in (a.get("chapters") or [])],
                )
            )
        except (TypeError, ValueError):
            continue

    beats: list[BeatSheet] = []
    for b in data.get("beats", []) or []:
        if not isinstance(b, dict):
            continue
        try:
            beats.append(
                BeatSheet(
                    label=str(b.get("label", "")),
                    description=str(b.get("description", "")),
                    chapter=max(1, int(b.get("chapter", 1))),
                    tension=_clamp(float(b.get("tension", 0.5))),
                    participants=[str(p) for p in (b.get("participants") or [])],
                )
            )
        except (TypeError, ValueError):
            continue

    return Outline(
        acts=acts,
        beats=sorted(beats, key=lambda x: x.chapter),
        ending=str(data.get("ending", "")),
        title_candidates=[str(t) for t in (data.get("title_candidates") or [])][:5],
    )


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _normalize_tension(beats: list[BeatSheet], chapters: int) -> list[BeatSheet]:
    """Smooth the tension curve.

    Local models produce flat tension (everything 0.5).  We apply a light
    structural shape -- ramp in Act I, a deliberate dip entering Act II, rising
    to a peak in Act III -- and blend it with what the model proposed, so the
    model still chooses *where* the tension goes but the overall shape is
    guaranteed to read like a novel rather than a list.
    """
    if not beats:
        return beats
    for b in beats:
        pos = (b.chapter - 1) / max(chapters - 1, 1)
        # Shape: rise to 0.35 by 25%, dip to 0.28 at the midpoint, climb to 1.0.
        if pos < 0.25:
            shape = 0.15 + pos * 0.8
        elif pos < 0.5:
            shape = 0.42 - (pos - 0.25) * 0.55
        elif pos < 0.8:
            shape = 0.30 + (pos - 0.5) * 1.6
        else:
            shape = 0.78 + (pos - 0.8) * 1.1
        # Blend the model's own tension with a structural shape. The shape gets
        # the larger weight so that a model which reports a flat 0.5 everywhere
        # still yields a book that reads like a novel rather than a list.
        b.tension = round(_clamp(0.35 * b.tension + 0.65 * _clamp(shape)), 3)
    return beats


# --------------------------------------------------------------------------
# Chapters and scenes
# --------------------------------------------------------------------------

CHAPTER_PLAN_PROMPT = """You are the scene planner for a {genre} novel.

STORY: {logline}
PREMISE: {premise}
ENDING: {ending}

Act structure:
{acts}

Beats in order:
{beats}

Plan chapters {start} to {end} of {total}.

For each chapter produce {scenes_per_chapter} scene cards.

A scene card must be a complete, self-contained instruction for one scene:
  - pov: the character whose eyes we read from (use exact names from the cast)
  - location: where the scene happens
  - participants: who is present
  - goal: what the POV wants in this scene
  - conflict: what resists that goal
  - outcome: what changes by the end of the scene (state it concretely)
  - must_include: concrete beats or details that MUST appear
  - must_avoid: things that must NOT happen (e.g. "the wound is not healed yet")
  - threads: plot threads this scene touches
  - words: target word count for the scene

CAST (use these exact names):
{cast}

Return JSON only:
{{"chapters": [
  {{"chapter": {start}, "title": "Chapter title",
    "purpose": "What this chapter accomplishes for the story",
    "pov": "character name", "location": "location name",
    "scenes": [
      {{"heading": "...", "pov": "...", "location": "...", "participants": ["..."],
        "goal": "...", "conflict": "...", "outcome": "...", "must_include": ["..."],
        "must_avoid": ["..."], "threads": ["..."], "words": 1200}}
    ]}}
]}}
"""


async def plan_chapters(
    bible: StoryBible,
    outline: Outline,
    llm,
    *,
    start: int = 1,
    end: int | None = None,
    scenes_per_chapter: int = 3,
    words_per_chapter: int = 2500,
) -> list[ChapterPlan]:
    chapters = max(4, bible.target_word_count // max(words_per_chapter, 500))
    end = end or chapters

    cast = "\n".join(f"- {c.name} ({c.role})" for c in bible.characters) or "- (no cast defined yet)"
    acts = "\n".join(f"- Act {a.number}: {a.name} -- {a.goal}" for a in outline.acts) or "- (none)"
    beats = "\n".join(
        f"- Ch{b.chapter} [{b.label}] (tension {b.tension:.2f}): {b.description}" for b in outline.beats
    ) or "- (none)"

    prompt = CHAPTER_PLAN_PROMPT.format(
        genre=bible.genre or "fiction",
        logline=bible.logline,
        premise=bible.premise,
        ending=outline.ending,
        acts=acts,
        beats=beats,
        start=start,
        end=end,
        total=chapters,
        scenes_per_chapter=scenes_per_chapter,
        cast=cast,
    )
    data = await llm.complete_json(
        messages=[("user", prompt)], max_tokens=6000, temperature=0.7
    )
    return _chapters_from_json(
        data, bible, start=start, words_per_chapter=words_per_chapter, scenes_per_chapter=scenes_per_chapter
    )


def _chapters_from_json(
    data: dict[str, Any],
    bible: StoryBible,
    *,
    start: int,
    words_per_chapter: int,
    scenes_per_chapter: int,
) -> list[ChapterPlan]:
    name_to_id = {c.name.lower(): c.id for c in bible.characters}
    for c in bible.characters:
        for a in c.aliases:
            name_to_id.setdefault(a.lower(), c.id)
    loc_name_to_id = {l.name.lower(): l.id for l in bible.locations}
    thread_title_to_id = {t.title.lower(): t.id for t in bible.threads}

    def cid(name: str | None) -> str | None:
        if not name:
            return None
        return name_to_id.get(name.strip().lower())

    def lid(name: str | None) -> str | None:
        if not name:
            return None
        return loc_name_to_id.get(name.strip().lower())

    out: list[ChapterPlan] = []
    for ch in data.get("chapters", []) or []:
        if not isinstance(ch, dict):
            continue
        try:
            num = int(ch.get("chapter", start))
        except (TypeError, ValueError):
            continue
        scenes: list[SceneCard] = []
        for i, sc in enumerate(ch.get("scenes", []) or []):
            if not isinstance(sc, dict):
                continue
            participants = []
            for p in (sc.get("participants") or []):
                resolved = cid(str(p))
                if resolved:
                    participants.append(resolved)
            threads = []
            for tname in sc.get("threads", []) or []:
                tid = thread_title_to_id.get(str(tname).strip().lower())
                if tid:
                    threads.append(tid)
            scenes.append(
                SceneCard(
                    chapter=num,
                    index=i,
                    heading=str(sc.get("heading", "")),
                    pov_character_id=cid(sc.get("pov")) or cid(ch.get("pov")),
                    location_id=lid(sc.get("location")) or lid(ch.get("location")),
                    participants=participants,
                    goal=str(sc.get("goal", "")),
                    conflict=str(sc.get("conflict", "")),
                    outcome=str(sc.get("outcome", "")),
                    must_include=[str(x) for x in (sc.get("must_include") or [])],
                    must_avoid=[str(x) for x in (sc.get("must_avoid") or [])],
                    thread_ids=threads,
                    approx_words=int(sc.get("words") or (words_per_chapter // max(scenes_per_chapter, 1))),
                )
            )
        out.append(
            ChapterPlan(
                chapter=num,
                title=str(ch.get("title", "")),
                purpose=str(ch.get("purpose", "")),
                pov_character_id=cid(ch.get("pov")),
                location_id=lid(ch.get("location")),
                scenes=scenes,
                target_words=words_per_chapter,
            )
        )
    out.sort(key=lambda c: c.chapter)
    return out


def plan_chapter_count(target_words: int, words_per_chapter: int = 2500) -> int:
    """How many chapters a target length implies.

    Kept as an explicit function because pacing depends on it: 90k words at
    2.5k per chapter is 36 chapters, but 90k words at 4k per chapter is 22 --
    and the latter is a very different book.
    """
    if words_per_chapter <= 0:
        raise ValueError("words_per_chapter must be positive")
    return max(4, int(math.ceil(target_words / words_per_chapter)))
