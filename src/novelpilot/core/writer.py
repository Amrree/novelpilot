"""The writer: turning a scene card into prose.

The writer is deliberately dumb.  It is handed a scene card, a continuity pack
and a voice profile, and it writes.  All creative decisions were already made
by the planner; the writer's only jobs are to execute them faithfully, to obey
the voice directives, and to not contradict the pack.

After writing, the chapter is summarised and the Story Bible is updated with
*deltas* -- new facts, new threads, character knowledge changes.  That update
is what makes chapter N+1 better than chapter N, and it is the step most
"AI novel writer" implementations skip.
"""

from __future__ import annotations

import json
from typing import Any

from .continuity import ContinuityPack, build_continuity_pack, deterministic_checks
from .models import (
    ChapterDraft,
    FactualEvent,
    SceneCard,
    StoryBible,
    ThreadStatus,
)
from .voice import VoiceProfile

# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

SCENE_PROMPT = """You are writing one scene of a {genre} novel.

=== SCENE CARD ===
Chapter {chapter}, scene {scene_index}
Heading: {heading}
POV character: {pov_name}
Location: {location}
Participants: {participants}

Goal: {goal}
Conflict: {conflict}
Outcome (must happen by the end of the scene): {outcome}

MUST INCLUDE:
{must_include}

MUST AVOID:
{must_avoid}

=== STORY STATE (authoritative -- never contradict it) ===
{continuity_pack}

=== STYLE DIRECTIVES (follow exactly) ===
{style_directives}

=== TASK ===
Write the scene. Target approximately {target_words} words.

Requirements:
- Write ONLY the scene. No chapter number, no title, no author notes, no
  meta-commentary, no "Here is the scene".
- Open in the middle of the action or tension. No throat-clearing.
- The outcome stated above must be visible in the prose by the end.
- Every physical detail of a character must match the STORY STATE.
- Show the conflict; do not narrate that a conflict happened.
- Use dialogue to reveal character and advance the scene. Do not write
  "they discussed" -- write the exchange.
- Never use the banned phrases.

Return the scene text only."""

CHAPTER_SYNC_PROMPT = """You are the continuity editor for a {genre} novel.

You are given the CHAPTER TEXT that was just written, and the current STORY STATE.

Your job is to extract what CHANGED, so the Story Bible can be updated.

Return JSON only:
{{
  "chapter_title": "A title for this chapter",
  "summary": "2-4 sentence summary of what happened, written for someone who has not read the chapter",
  "events": [
    {{"summary": "One concrete fact that happened, phrased as a fact",
      "participants": ["character names"], "irreversible": false}}
  ],
  "opened_threads": ["Titles of NEW plot threads started in this chapter"],
  "resolved_threads": ["Titles of EXISTING plot threads that were resolved in this chapter"],
  "character_updates": [
    {{"name": "Character name", "learned": "Something new they now know",
      "relationship_changes": ["Who they are now closer to / further from"]}}
  ],
  "emotional_valence": -1.0,
  "tension": 0.5
}}

Only list events that actually occurred in the text. Do not invent facts.
Only list resolved_threads that were actually open before.
"""

# --------------------------------------------------------------------------
# Writer
# --------------------------------------------------------------------------


class Writer:
    """Writes scenes and keeps the Bible in sync."""

    def __init__(self, llm, voice: VoiceProfile | None = None) -> None:
        self.llm = llm
        self.voice = voice or VoiceProfile()

    # -- scene ----------------------------------------------------------
    async def write_scene(self, bible: StoryBible, scene: SceneCard) -> str:
        pack = build_continuity_pack(bible, scene)
        scene.continuity_pack = pack.text

        pov = bible.character(scene.pov_character_id) if scene.pov_character_id else None
        location = next((l for l in bible.locations if l.id == scene.location_id), None)
        names = []
        for cid in scene.participants:
            c = bible.character(cid)
            if c:
                names.append(c.name)

        must_include = "\n".join(f"- {x}" for x in scene.must_include) or "- (none)"
        must_avoid = "\n".join(f"- {x}" for x in scene.must_avoid) or "- (none)"

        prompt = SCENE_PROMPT.format(
            genre=bible.genre or "fiction",
            chapter=scene.chapter,
            scene_index=scene.index + 1,
            heading=scene.heading or "(untitled)",
            pov_name=pov.name if pov else "(unspecified)",
            location=location.name if location else "(unspecified)",
            participants=", ".join(names) or "(none)",
            goal=scene.goal,
            conflict=scene.conflict,
            outcome=scene.outcome,
            must_include=must_include,
            must_avoid=must_avoid,
            continuity_pack=pack.text,
            style_directives="\n".join(f"- {d}" for d in self.voice.directives_for(scene.pov_character_id)),
            target_words=scene.approx_words,
        )
        resp = await self.llm.complete_text(
            messages=[("user", prompt)],
            max_tokens=max(1024, int(scene.approx_words * 2.2)),
            temperature=self.llm.temperature,
        )
        return _strip_preamble(resp)

    # -- chapter --------------------------------------------------------
    async def write_chapter(
        self, bible: StoryBible, chapter: SceneCard | Any, scenes: list[SceneCard]
    ) -> ChapterDraft:
        """Write all scenes of a chapter, then sync the Bible."""
        texts: list[str] = []
        for scene in scenes:
            texts.append(await self.write_scene(bible, scene))
        body = "\n\n".join(texts).strip()

        draft = ChapterDraft(
            chapter=scenes[0].chapter if scenes else 1,
            title="",
            text=body,
            word_count=len(body.split()),
            scenes=[s.heading for s in scenes],
        )
        draft.issues = deterministic_checks(bible, draft, scenes[0] if scenes else None)
        return draft

    # -- bible sync -----------------------------------------------------
    async def sync_bible(self, bible: StoryBible, draft: ChapterDraft) -> dict[str, Any]:
        """Extract deltas from a finished chapter and apply them to the Bible."""
        prompt = CHAPTER_SYNC_PROMPT.format(genre=bible.genre or "fiction") + (
            f"\n\n=== STORY STATE ===\n{_state_digest(bible, draft.chapter)}\n"
            f"\n=== CHAPTER {draft.chapter} TEXT ===\n{draft.text}\n"
        )
        try:
            data = await self.llm.complete_json(
                messages=[("user", prompt)], max_tokens=2500, temperature=0.2
            )
        except Exception:  # noqa: BLE001 - a failed sync must not lose the prose
            return {}

        self._apply_deltas(bible, draft, data)
        return data

    def _apply_deltas(self, bible: StoryBible, draft: ChapterDraft, data: dict[str, Any]) -> None:
        name_to_char = {c.name.lower(): c for c in bible.characters}
        for c in bible.characters:
            for a in c.aliases:
                name_to_char.setdefault(a.lower(), c)

        title_to_thread = {t.title.lower(): t for t in bible.threads}

        # New events.
        for e in data.get("events", []) or []:
            if not isinstance(e, dict):
                continue
            summary = str(e.get("summary", "")).strip()
            if not summary:
                continue
            participants = []
            for p in e.get("participants", []) or []:
                ch = name_to_char.get(str(p).strip().lower())
                if ch:
                    participants.append(ch.id)
            bible.events.append(
                FactualEvent(
                    chapter=draft.chapter,
                    summary=summary,
                    participants=participants,
                    irreversible=bool(e.get("irreversible", False)),
                )
            )

        # New threads.
        for title in data.get("opened_threads", []) or []:
            t = str(title).strip()
            if not t or t.lower() in title_to_thread:
                continue
            thread = PlotThreadFactory.create(t, bible, draft.chapter)
            bible.threads.append(thread)
            title_to_thread[t.lower()] = thread

        # Resolved threads.
        for title in data.get("resolved_threads", []) or []:
            t = title_to_thread.get(str(title).strip().lower())
            if t and t.status is not ThreadStatus.RESOLVED:
                t.status = ThreadStatus.RESOLVED
                t.resolved_in = draft.chapter

        # Character knowledge.
        for upd in data.get("character_updates", []) or []:
            if not isinstance(upd, dict):
                continue
            ch = name_to_char.get(str(upd.get("name", "")).strip().lower())
            if not ch:
                continue
            learned = str(upd.get("learned", "")).strip()
            if learned and learned not in ch.knowledge:
                ch.knowledge.append(learned)

        # Chapter summary.
        from .models import ChapterSummary

        bible.chapter_summaries.append(
            ChapterSummary(
                chapter=draft.chapter,
                title=str(data.get("chapter_title", "")),
                summary=str(data.get("summary", "")),
                events=[str(e.get("summary", "")) for e in (data.get("events") or []) if isinstance(e, dict)],
                opened_threads=[str(t) for t in (data.get("opened_threads") or [])],
                resolved_threads=[str(t) for t in (data.get("resolved_threads") or [])],
                word_count=draft.word_count,
                emotional_valence=_f(data.get("emotional_valence"), 0.0, -1.0, 1.0),
                tension=_f(data.get("tension"), 0.5, 0.0, 1.0),
            )
        )
        draft.title = str(data.get("chapter_title", ""))


class PlotThreadFactory:
    """Tiny helper so thread creation keeps a single definition of defaults."""

    @staticmethod
    def create(title: str, bible: StoryBible, chapter: int):
        from .models import PlotThread

        return PlotThread(
            title=title,
            description="",
            status=ThreadStatus.OPEN,
            introduced_in=chapter,
            stakes="medium",
        )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _state_digest(bible: StoryBible, chapter: int) -> str:
    lines: list[str] = []
    for c in bible.characters:
        lines.append(f"- {c.name} ({c.role})")
    lines.append("\nOpen threads:")
    for t in bible.open_threads():
        lines.append(f"- {t.title} (introduced ch{t.introduced_in})")
    lines.append("\nRecent summaries:")
    for s in bible.summaries_up_to(chapter - 1)[-3:]:
        lines.append(f"- Ch{s.chapter}: {s.summary}")
    return "\n".join(lines)


_PREAMBLE_PATTERNS = (
    "here is the scene",
    "here's the scene",
    "scene:",
    "chapter:",
    "---",
    "***",
)


def _strip_preamble(text: str) -> str:
    """Models sometimes prepend a label or a meta-commentary line."""
    t = text.strip()
    low = t.lower()
    for pat in _PREAMBLE_PATTERNS:
        if low.startswith(pat):
            lines = t.split("\n", 1)
            if len(lines) == 2:
                t = lines[1].strip()
            break
    # Strip surrounding markdown fences if present.
    if t.startswith("```"):
        t = t.split("```")[1] if "```" in t[3:] else t
        t = t.strip()
        if t.lower().startswith("markdown"):
            t = t[7:].strip()
    return t


def _f(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default
