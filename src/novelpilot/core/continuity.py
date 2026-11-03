"""Continuity: context assembly and contradiction detection.

Two halves, and both matter:

* **Assembly** (``build_continuity_pack``) decides what the writer is allowed
  to see.  It is deliberately *small* -- a few hundred to a couple of thousand
  tokens -- because that is what can be pasted into every scene prompt without
  the cost exploding.  The pack is recomputed per scene from structured state,
  never from "whatever prose happened to be nearby".

* **Verification** (``verify_chapter``) checks the produced prose back against
  the state.  Deterministic checks (banned phrases, name usage, length) run
  first because they are free; an LLM judge runs second for the semantic
  contradictions that only a reader would notice.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .models import (
    ChapterDraft,
    ContinuityIssue,
    SceneCard,
    StoryBible,
    ThreadStatus,
)

# How many recent chapters of summary to include verbatim.
RECENT_CHAPTER_WINDOW = 3
# How many older facts to include as bullet points.
MAX_LEGACY_FACTS = 24
# Soft cap on the assembled pack, in characters.
PACK_CHAR_BUDGET = 12_000


@dataclass(slots=True)
class ContinuityPack:
    text: str
    included_facts: list[str]
    included_threads: list[str]
    token_estimate: int

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.text


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def build_continuity_pack(bible: StoryBible, scene: SceneCard) -> ContinuityPack:
    """Assemble the exact context a scene needs, and nothing more."""
    chapter = scene.chapter
    parts: list[str] = []
    facts: list[str] = []
    threads: list[str] = []

    # -- 1. Non-negotiable style rules ---------------------------------
    rules: list[str] = []
    if bible.style_directives:
        rules += [f"- {d}" for d in bible.style_directives]
    if bible.banned_phrases:
        rules += [f"- Never use the phrase: \"{p}\"" for p in bible.banned_phrases]
    rules.append(f"- Narration point of view: {bible.pov.value.replace('_', ' ')}.")
    if bible.tense:
        rules.append(f"- Use {bible.tense} tense throughout.")
    if rules:
        parts.append("## NON-NEGOTIABLE RULES\n" + "\n".join(rules))

    # -- 2. POV character ----------------------------------------------
    pov = bible.character(scene.pov_character_id) if scene.pov_character_id else None
    if pov:
        parts.append(f"## POV CHARACTER\n{pov.summary_for_prompt()}")
        if pov.background:
            parts.append(f"Background: {pov.background}")
        if pov.flaw:
            parts.append(f"Flaw: {pov.flaw}")
        if pov.internal_conflict:
            parts.append(f"Internal conflict: {pov.internal_conflict}")
        if pov.arc:
            parts.append(f"Arc (do not complete this yet): {pov.arc}")

    # -- 3. Other participants -----------------------------------------
    others = [bible.character(cid) for cid in scene.participants if cid != scene.pov_character_id]
    others = [c for c in others if c]
    if others:
        lines = []
        for c in others:
            lines.append(f"- {c.summary_for_prompt()}")
            if c.relationships.get(scene.pov_character_id or ""):
                lines.append(f"  Relationship to POV: {c.relationships[scene.pov_character_id]}")
        parts.append("## OTHER CHARACTERS PRESENT\n" + "\n".join(lines))

    # -- 4. Location ---------------------------------------------------
    if scene.location_id:
        loc = next((l for l in bible.locations if l.id == scene.location_id), None)
        if loc:
            detail = loc.description
            if loc.sensory:
                detail += f" Signature detail: {loc.sensory}"
            parts.append(f"## LOCATION\n{loc.name}: {detail}")

    # -- 5. Plot threads in play ---------------------------------------
    relevant_ids = set(scene.thread_ids)
    thread_lines: list[str] = []
    for t in bible.threads:
        if t.status in (ThreadStatus.OPEN, ThreadStatus.ADVANCING):
            if t.id in relevant_ids or set(t.related_characters) & set(scene.participants):
                threads.append(t.title)
                thread_lines.append(
                    f"- [{t.stakes} stakes] {t.title}: {t.description}"
                    + (f" (payoff target: {t.payoff_note})" if t.payoff_note else "")
                )
    if thread_lines:
        parts.append("## ACTIVE PLOT THREADS\n" + "\n".join(thread_lines))

    # -- 6. Recent history (fine-grained) ------------------------------
    # Facts and summaries are complementary: a summary says what the chapter
    # was about, a fact says what actually happened. Both must be present or
    # the model is left to invent what it was told moments ago.
    recent_summaries = bible.summaries_up_to(chapter - 1)[-RECENT_CHAPTER_WINDOW:]
    recent_facts = [
        e for e in bible.events
        if chapter - RECENT_CHAPTER_WINDOW < e.chapter <= chapter - 1
    ]
    if recent_summaries or recent_facts:
        lines: list[str] = []
        for s in recent_summaries:
            who = (
                bible.character(s.pov_character_id).name
                if s.pov_character_id and bible.character(s.pov_character_id)
                else ""
            )
            head = f"Ch{s.chapter}" + (f" ({who} POV)" if who else "")
            lines.append(f"- {head}: {s.summary}")
        for e in recent_facts:
            mark = " [IRREVERSIBLE - must not contradict]" if e.irreversible else ""
            lines.append(f"- Ch{e.chapter}: {e.summary}{mark}")
        parts.append("## RECENT CHAPTERS (must be consistent with these)\n" + "\n".join(lines))

    # -- 7. Older facts (coarse) ---------------------------------------
    legacy = [e for e in bible.events if e.chapter <= chapter - RECENT_CHAPTER_WINDOW]
    if legacy:
        legacy = legacy[-MAX_LEGACY_FACTS:]
        lines = []
        for e in legacy:
            mark = " [IRREVERSIBLE - must not contradict]" if e.irreversible else ""
            lines.append(f"- Ch{e.chapter}: {e.summary}{mark}")
            facts.append(e.summary)
        parts.append("## EARLIER FACTS\n" + "\n".join(lines))

    # -- 8. POV knowledge guard (anti-leak) ----------------------------
    if pov and pov.knowledge:
        parts.append(
            "## WHAT THE POV KNOWS (the POV must not know anything else yet)\n"
            + "\n".join(f"- {k}" for k in pov.knowledge)
        )

    text = "\n\n".join(p for p in parts if p.strip())
    if len(text) > PACK_CHAR_BUDGET:
        text = text[:PACK_CHAR_BUDGET] + "\n\n[...truncated for length...]"

    return ContinuityPack(
        text=text,
        included_facts=facts,
        included_threads=threads,
        token_estimate=len(text) // 4,
    )


# --------------------------------------------------------------------------
# Deterministic checks
# --------------------------------------------------------------------------


def deterministic_checks(bible: StoryBible, draft: ChapterDraft, scene: SceneCard | None) -> list[ContinuityIssue]:
    """Cheap, exact checks.  These catch a surprising share of real problems."""
    issues: list[ContinuityIssue] = []
    text = draft.text

    # Banned phrases.
    for phrase in bible.banned_phrases:
        if phrase and phrase.lower() in text.lower():
            issues.append(
                ContinuityIssue(
                    severity="error",
                    kind="banned_phrase",
                    message=f"Contains banned phrase: {phrase!r}",
                    chapter=draft.chapter,
                    suggestion="Rewrite the sentence using different wording.",
                )
            )

    # Characters in the Bible but absent from the scene are fine; the reverse
    # is not -- a name appears that the Bible has never heard of.
    #
    # Heuristic: a capitalised word that appears *mid-sentence* is almost
    # certainly a proper noun. Words that only ever appear at the start of a
    # sentence are ignored, because there is no way to tell a name from an
    # ordinary capitalised word without a dictionary.
    known = set()
    for c in bible.characters:
        known.add(c.name.lower())
        known.update(a.lower() for a in c.aliases)

    for name in _mid_sentence_capitals(text):
        low = name.lower()
        if low in known or low in _COMMON_WORDS:
            continue
        issues.append(
            ContinuityIssue(
                severity="warning",
                kind="unknown_character",
                message=f"Name {name!r} is not in the Story Bible.",
                chapter=draft.chapter,
                suggestion="Add the character to the Bible or change the name.",
            )
        )

    # Scene requirements.
    if scene:
        for required in scene.must_include:
            if required.lower() not in text.lower():
                issues.append(
                    ContinuityIssue(
                        severity="warning",
                        kind="missing_beat",
                        message=f"Scene required element missing: {required!r}",
                        chapter=draft.chapter,
                        suggestion="Include the required element or adjust the scene card.",
                    )
                )

    # Length sanity.
    if draft.word_count and scene and scene.approx_words:
        ratio = draft.word_count / max(scene.approx_words, 1)
        if ratio < 0.4:
            issues.append(
                ContinuityIssue(
                    severity="warning",
                    kind="too_short",
                    message=f"Chapter is {draft.word_count} words, target ~{scene.approx_words}.",
                    chapter=draft.chapter,
                )
            )

    return issues


# --------------------------------------------------------------------------
# LLM judge
# --------------------------------------------------------------------------

JUDGE_PROMPT = """You are a strict continuity editor for a {genre} novel.

You are given the authoritative STORY STATE for the book so far, the PLAN for the
chapter that was just written, and the WRITTEN TEXT.

Report ONLY real contradictions between the written text and the story state or
plan. Do not report style opinions, do not report suggestions for improvement,
and do not report things that are merely unspecified.

Contradictions include:
- A character having a physical trait they do not have (or losing one).
- A character doing something impossible given an established injury, location, or state.
- A character knowing something they could not know yet.
- An event contradicting an earlier event.
- A required plot beat being absent.
- A resolved plot thread being treated as still open, or an open thread being
  silently resolved without the payoff.

Return JSON:
{{"issues": [{{"severity": "error|warning|info", "kind": "...", "message": "...", "suggestion": "..."}}]}}
If there are no contradictions, return {{"issues": []}}.
"""


async def llm_judge(bible: StoryBible, draft: ChapterDraft, pack: str, llm) -> list[ContinuityIssue]:
    """Ask the model to find semantic contradictions.  Returns [] on any failure."""
    prompt = (
        JUDGE_PROMPT.format(genre=bible.genre or "fiction")
        + "\n\n=== STORY STATE (authoritative) ===\n"
        + pack
        + f"\n\n=== CHAPTER {draft.chapter} TEXT ===\n{draft.text}\n"
    )
    try:
        resp = await llm.complete_json(
            messages=[("user", prompt)],
            max_tokens=1500,
            temperature=0.1,
        )
    except Exception:  # noqa: BLE001 - a failed judge must not fail the chapter
        return []
    data = resp if isinstance(resp, dict) else {}
    raw_issues = data.get("issues") or []
    out: list[ContinuityIssue] = []
    for item in raw_issues:
        if not isinstance(item, dict):
            continue
        try:
            out.append(
                ContinuityIssue(
                    severity=item.get("severity", "warning"),
                    kind=str(item.get("kind", "")),
                    message=str(item.get("message", "")),
                    suggestion=str(item.get("suggestion", "")),
                    chapter=draft.chapter,
                )
            )
        except Exception:  # noqa: BLE001
            continue
    return out


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _mid_sentence_capitals(text: str) -> list[str]:
    """Capitalised words that appear somewhere other than sentence-initial.

    Sentence-initial capitals are excluded because a capitalised word there is
    indistinguishable from an ordinary word without a lexicon; mid-sentence
    capitals are a far stronger signal of a proper noun.
    """
    found: list[str] = []
    seen: set[str] = set()
    for sentence in re.split(r"(?<=[.!?…])\s+", text):
        words = sentence.split()
        for i, word in enumerate(words):
            if i == 0:
                continue
            bare = word.strip(".,;:!?\"'()[]")
            if len(bare) < 3 or not bare[0].isupper() or not bare[1:].islower():
                continue
            if bare in seen:
                continue
            seen.add(bare)
            found.append(bare)
    return found


_COMMON_WORDS = {
    "the", "she", "he", "they", "and", "but", "for", "with", "that", "this", "then",
    "when", "what", "her", "his", "its", "their", "there", "here", "not", "from",
    "into", "out", "down", "over", "under", "after", "before", "because", "chapter",
    "book", "one", "two", "three", "first", "second", "third", "had", "was", "were",
    "been", "have", "has", "did", "does", "said", "told", "asked", "looked", "saw",
    "walked", "turned", "made", "knew", "thought", "felt", "stood", "waited", "went",
    "came", "took", "gave", "seemed", "began", "still", "just", "only", "even",
    "also", "back", "away", "down", "well", "now", "more", "most", "some", "such",
    "very", "much", "many", "every", "other", "another", "again", "around", "though",
    "while", "where", "before", "without", "through", "during", "against", "between",
}


def format_pack_for_prompt(pack: ContinuityPack) -> str:
    return pack.text
