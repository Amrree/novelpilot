"""The generation pipeline: a resumable, explicit DAG.

Every step is idempotent and its output is persisted, so a run can be
interrupted at any point (crash, token exhaustion, a human changing their
mind about the plot) and resumed without re-paying for work already done.

We deliberately do **not** use LangChain/LangGraph here.  The graph is fixed
and known; a general agent framework would add a large dependency surface and
a layer of indirection over what is really a dozen functions.  The whole
orchestrator is this file.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..core.assembler import Manuscript, export_all
from ..core.bible import BibleStore
from ..core.engine import Engine
from ..core.models import (
    ChapterDraft,
    ChapterPlan,
    Outline,
    Project,
    Stage,
    StoryBible,
)
from ..core.planner import plan_chapters, plan_outline
from ..core.voice import VoiceProfile, build_voice_profile
from ..core.writer import Writer

log = logging.getLogger("novelpilot.pipeline")

ProgressFn = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class PipelineResult:
    slug: str
    title: str
    chapters_written: list[int]
    cost_usd: float
    exports: dict[str, str]
    issues: list[dict[str, Any]]


class Pipeline:
    """Drives a project from brief to export."""

    def __init__(
        self,
        store: BibleStore,
        engine: Engine,
        *,
        voice: VoiceProfile | None = None,
        progress: ProgressFn | None = None,
    ) -> None:
        self.store = store
        self.engine = engine
        self.voice = voice or build_voice_profile()
        self.progress = progress
        self.writer = Writer(engine, self.voice)

    # -- helpers ---------------------------------------------------------
    async def _emit(self, step: str, payload: dict[str, Any]) -> None:
        if self.progress:
            try:
                result = self.progress(step, payload)
                # The callback may be sync or async; support both.
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001
                log.exception("progress callback failed")

    def _bible(self, slug: str) -> StoryBible:
        return self.store.load_bible(slug)

    def _save(self, slug: str, bible: StoryBible) -> None:
        self.store.save_bible(slug, bible)

    # -- steps ------------------------------------------------------------
    async def step_outline(self, slug: str) -> Outline:
        """Produce the act/beat structure."""
        bible = self._bible(slug)
        await self._emit("outline.start", {"slug": slug, "genre": bible.genre})
        outline = await plan_outline(bible, self.engine)
        self._save(slug, bible)
        await self._emit(
            "outline.done",
            {
                "acts": len(outline.acts),
                "beats": len(outline.beats),
                "titles": outline.title_candidates[:5],
            },
        )
        return outline

    async def step_chapters(self, slug: str, *, scenes_per_chapter: int = 3,
                            words_per_chapter: int = 2500) -> list[ChapterPlan]:
        """Turn the outline into per-chapter scene cards."""
        bible = self._bible(slug)
        outline = await self.step_outline(slug)
        await self._emit("chapters.start", {"slug": slug})
        plans = await plan_chapters(
            bible,
            outline,
            self.engine,
            scenes_per_chapter=scenes_per_chapter,
            words_per_chapter=words_per_chapter,
        )
        await self._emit("chapters.done", {"count": len(plans)})
        return plans

    async def step_write_chapter(self, slug: str, chapter: int, plans: list[ChapterPlan]) -> ChapterDraft:
        """Write one chapter and fold the resulting state changes back into the Bible."""
        bible = self._bible(slug)
        plan = next((p for p in plans if p.chapter == chapter), None)
        if plan is None:
            raise ValueError(f"no plan for chapter {chapter}")

        await self._emit(
            "chapter.start", {"chapter": chapter, "title": plan.title, "scenes": len(plan.scenes)}
        )
        draft = await self.writer.write_chapter(bible, chapter, plan.scenes)

        await self._emit("chapter.sync", {"chapter": chapter})
        await self.writer.sync_bible(bible, draft)

        self.store.save_draft(slug, draft.chapter, draft.title, draft.text)
        bible.stage = Stage.DRAFTING
        self._save(slug, bible)

        await self._emit(
            "chapter.done",
            {"chapter": chapter, "words": draft.word_count, "issues": len(draft.issues)},
        )
        return draft

    async def step_verify(self, slug: str, plans: list[ChapterPlan]) -> list[dict[str, Any]]:
        """Run the continuity judge across the finished manuscript."""
        from ..core.continuity import llm_judge

        bible = self._bible(slug)
        drafts = self.store.load_all_drafts(slug)
        issues: list[dict[str, Any]] = []
        await self._emit("verify.start", {"chapters": len(drafts)})
        for chapter, text in sorted(drafts.items()):
            draft = ChapterDraft(chapter=chapter, text=text, word_count=len(text.split()))
            plan = next((p for p in plans if p.chapter == chapter), None)
            scene = plan.scenes[0] if plan and plan.scenes else None
            from ..core.continuity import build_continuity_pack

            pack = build_continuity_pack(bible, scene) if scene else None
            found = []
            if pack:
                found = await llm_judge(bible, draft, pack.text, self.engine)
            for issue in found:
                issues.append(issue.model_dump())
        await self._emit("verify.done", {"issues": len(issues)})
        return issues

    async def step_export(
        self,
        slug: str,
        outdir: str | Path,
        *,
        author: str = "Anonymous",
        cover: str | Path | None = None,
    ) -> dict[str, Path]:
        """Assemble the finished book into publishable files."""
        bible = self._bible(slug)
        drafts = self.store.load_all_drafts(slug)
        chapters = [
            ChapterDraft(
                chapter=num,
                title=_chapter_title(bible, num),
                text=text,
                word_count=len(text.split()),
            )
            for num, text in sorted(drafts.items())
        ]
        ms = Manuscript(
            title=bible.title or slug,
            author=author,
            subtitle=bible.subgenre,
            chapters=chapters,
            bible=bible,
            genre=bible.genre,
            description=bible.logline,
        )
        await self._emit("export.start", {"chapters": len(chapters)})
        paths = export_all(ms, outdir, cover=cover)
        bible.stage = Stage.COMPLETE
        self._save(slug, bible)
        await self._emit("export.done", {"files": {k: str(v) for k, v in paths.items()}})
        return paths

    # -- orchestration ----------------------------------------------------
    async def run(
        self,
        slug: str,
        *,
        chapters: int | None = None,
        scenes_per_chapter: int = 3,
        words_per_chapter: int = 2500,
        outdir: str | Path | None = None,
        author: str = "Anonymous",
        verify: bool = True,
    ) -> PipelineResult:
        """Run the full pipeline, skipping work already done."""
        bible = self._bible(slug)
        plans = await self.step_chapters(
            slug, scenes_per_chapter=scenes_per_chapter, words_per_chapter=words_per_chapter
        )
        if chapters is not None:
            plans = [p for p in plans if p.chapter <= chapters]

        written: list[int] = []
        existing = self.store.load_all_drafts(slug)
        for plan in sorted(plans, key=lambda p: p.chapter):
            if plan.chapter in existing:
                await self._emit("chapter.skip", {"chapter": plan.chapter})
                continue
            draft = await self.step_write_chapter(slug, plan.chapter, plans)
            written.append(draft.chapter)

        issues: list[dict[str, Any]] = []
        if verify:
            issues = await self.step_verify(slug, plans)

        exports: dict[str, Path] = {}
        if outdir:
            exports = await self.step_export(slug, outdir, author=author)

        return PipelineResult(
            slug=slug,
            title=bible.title or slug,
            chapters_written=written,
            cost_usd=self.engine.total_cost,
            exports={k: str(v) for k, v in exports.items()},
            issues=issues,
        )


def _chapter_title(bible: StoryBible, chapter: int) -> str:
    for s in bible.chapter_summaries:
        if s.chapter == chapter:
            return s.title
    return f"Chapter {chapter}"
