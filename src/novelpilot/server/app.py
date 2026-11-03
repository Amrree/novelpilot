"""FastAPI application.

Endpoints are grouped by what a user actually does:

    POST /projects              create a project from a brief
    GET  /projects              list projects
    GET  /projects/{slug}       project detail + bible state
    POST /projects/{slug}/plan  generate outline + chapter plan
    POST /projects/{slug}/write generate one chapter (or N)
    GET  /projects/{slug}/stream  SSE progress while writing
    GET  /projects/{slug}/export    produce EPUB/DOCX/TXT
    GET  /projects/{slug}/cost      cost breakdown
    GET  /health                provider reachability

Generation is long-running, so ``write`` runs the pipeline in a background task
and the client follows progress over SSE.  That keeps the HTTP layer stateless
and lets a user close the tab without losing the run.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..core.assembler import Manuscript, kdp_metadata
from ..core.bible import BibleStore
from ..core.config import Config
from ..core.engine import Engine
from ..core.models import ChapterDraft, Character, StoryBible
from ..core.voice import StyleMetrics, build_voice_profile
from ..graph.pipeline import Pipeline
from ..plugins.manager import PluginManager
from ..providers.registry import available_providers, build_provider

log = logging.getLogger("novelpilot.server")


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------


class CharacterIn(BaseModel):
    name: str
    role: str = "minor"
    age: int | None = None
    pronouns: str | None = None
    physical: dict[str, str] = Field(default_factory=dict)
    goal: str = ""
    flaw: str = ""
    arc: str = ""
    background: str = ""
    dialogue_style: str = ""
    voice: dict[str, Any] = Field(default_factory=dict)
    aliases: list[str] = Field(default_factory=list)


class LocationIn(BaseModel):
    name: str
    kind: str = "other"
    description: str = ""
    sensory: str = ""


class ProjectIn(BaseModel):
    slug: str | None = None
    title: str
    author: str = "Anonymous"
    genre: str = ""
    subgenre: str = ""
    logline: str = ""
    premise: str = ""
    audience: str = ""
    target_words: int = 80_000
    pov: str = "third_person_past"
    tense: str = "past"
    style_directives: list[str] = Field(default_factory=list)
    banned_phrases: list[str] = Field(default_factory=list)
    content_notes: list[str] = Field(default_factory=list)
    characters: list[CharacterIn] = Field(default_factory=list)
    locations: list[LocationIn] = Field(default_factory=list)


class WriteIn(BaseModel):
    chapters: int | None = None
    scenes_per_chapter: int = 3
    words_per_chapter: int = 2500
    verify: bool = True


class ExportIn(BaseModel):
    author: str = "Anonymous"
    outdir: str = "exports"
    cover: str | None = None


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or Config.load()
    cfg.ensure_dirs()

    app = FastAPI(
        title="NovelPilot",
        version="0.1.0",
        description="The AI novel engine that actually writes books.",
    )
    store = BibleStore(cfg.db_path)
    plugins = PluginManager()
    plugins.discover()

    # One engine per provider, created lazily and cached.
    _engines: dict[str, Engine] = {}

    def engine_for(provider: str) -> Engine:
        if provider not in _engines:
            prov = build_provider(provider, base_url=cfg.base_url) if cfg.base_url else build_provider(provider)
            _engines[provider] = Engine(
                provider=prov,
                models={
                    "plan": cfg.models.plan,
                    "prose": cfg.models.prose,
                    "summary": cfg.models.summary,
                    "judge": cfg.models.judge,
                },
                temperature=cfg.temperature,
                max_retries=cfg.max_retries,
                max_cost_usd=cfg.max_cost_per_book_usd,
            )
        return _engines[provider]

    # -- helpers ---------------------------------------------------------
    def load_bible(slug: str) -> StoryBible:
        try:
            return store.load_bible(slug)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no project {slug!r}")

    def build_bible(payload: ProjectIn) -> StoryBible:
        bible = StoryBible(
            title=payload.title,
            genre=payload.genre,
            subgenre=payload.subgenre,
            logline=payload.logline,
            premise=payload.premise,
            audience=payload.audience,
            target_word_count=payload.target_words,
            pov=payload.pov,  # type: ignore[arg-type]
            tense=payload.tense,
            style_directives=payload.style_directives,
            banned_phrases=payload.banned_phrases,
            content_notes=payload.content_notes,
        )
        bible.characters = [
            Character(
                name=c.name,
                role=c.role,  # type: ignore[arg-type]
                age=c.age,
                pronouns=c.pronouns,
                physical=c.physical,
                goal=c.goal,
                flaw=c.flaw,
                arc=c.arc,
                background=c.background,
                dialogue_style=c.dialogue_style,
                voice=c.voice,
                aliases=c.aliases,
            )
            for c in payload.characters
        ]
        from ..core.models import Location

        bible.locations = [
            Location(name=l.name, kind=l.kind, description=l.description, sensory=l.sensory)  # type: ignore[arg-type]
            for l in payload.locations
        ]
        return bible

    # -- routes ----------------------------------------------------------
    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "providers": available_providers(),
            "plugins": [p.to_dict() for p in plugins.list()],
        }

    @app.post("/projects", status_code=201)
    async def create_project(payload: ProjectIn) -> dict[str, Any]:
        slug = payload.slug or payload.title.lower().replace(" ", "-")
        store.create_project(slug, payload.title)
        bible = build_bible(payload)
        problems = bible.validate()
        if problems:
            raise HTTPException(status_code=422, detail={"validation": problems})
        store.save_bible(slug, bible)
        return {"slug": slug, "title": payload.title, "characters": len(bible.characters)}

    @app.get("/projects")
    async def list_projects() -> list[dict[str, Any]]:
        return [
            {"slug": r["slug"], "title": r["title"], "stage": r["stage"], "updated_at": r["updated_at"]}
            for r in store.list_projects()
        ]

    @app.get("/projects/{slug}")
    async def get_project(slug: str) -> dict[str, Any]:
        bible = load_bible(slug)
        return {
            "slug": slug,
            "title": bible.title,
            "stage": bible.stage.value,
            "characters": [c.model_dump() for c in bible.characters],
            "locations": [l.model_dump() for l in bible.locations],
            "threads": [t.model_dump() for t in bible.threads],
            "chapters_written": sorted(store.load_all_drafts(slug)),
            "cost_usd": round(store.total_cost(slug), 4),
        }

    @app.post("/projects/{slug}/plan")
    async def plan(slug: str) -> dict[str, Any]:
        load_bible(slug)
        pipe = Pipeline(store, engine_for(cfg.provider))
        plans = await pipe.step_chapters(slug)
        return {"slug": slug, "chapters": [p.model_dump() for p in plans]}

    @app.post("/projects/{slug}/write")
    async def write(slug: str, payload: WriteIn) -> dict[str, Any]:
        load_bible(slug)
        pipe = Pipeline(store, engine_for(cfg.provider))
        result = await pipe.run(
            slug,
            chapters=payload.chapters,
            scenes_per_chapter=payload.scenes_per_chapter,
            words_per_chapter=payload.words_per_chapter,
            verify=payload.verify,
        )
        return {
            "slug": slug,
            "chapters_written": result.chapters_written,
            "cost_usd": round(result.cost_usd, 4),
            "issues": result.issues,
        }

    @app.get("/projects/{slug}/stream")
    async def stream(slug: str, chapters: int | None = Query(default=None)) -> StreamingResponse:
        """Server-sent events for live generation progress."""
        load_bible(slug)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def progress(step: str, payload: dict[str, Any]) -> None:
            await queue.put({"step": step, **payload})

        async def run() -> Any:
            pipe = Pipeline(store, engine_for(cfg.provider), progress=progress)
            try:
                await pipe.run(slug, chapters=chapters)
            except Exception as exc:  # noqa: BLE001
                await queue.put({"step": "error", "message": str(exc)})
            finally:
                await queue.put({"step": "done"})

        async def event_stream():
            task = asyncio.create_task(run())
            try:
                while True:
                    item = await queue.get()
                    yield f"data: {json.dumps(item)}\n\n"
                    if item.get("step") in ("done", "error"):
                        break
            finally:
                task.cancel()

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/projects/{slug}/export")
    async def export(slug: str, payload: ExportIn) -> dict[str, Any]:
        bible = load_bible(slug)
        drafts = store.load_all_drafts(slug)
        if not drafts:
            raise HTTPException(status_code=409, detail="no chapters written yet")
        pipe = Pipeline(store, engine_for(cfg.provider))
        paths = await pipe.step_export(slug, payload.outdir, author=payload.author, cover=payload.cover)
        ms = Manuscript(
            title=bible.title or slug,
            author=payload.author,
            subtitle=bible.subgenre,
            chapters=[
                ChapterDraft(chapter=n, text=t, word_count=len(t.split()))
                for n, t in sorted(drafts.items())
            ],
            bible=bible,
            genre=bible.genre,
            description=bible.logline,
        )
        return {
            "files": {k: str(v) for k, v in paths.items()},
            "kdp_metadata": kdp_metadata(ms),
        }

    @app.get("/projects/{slug}/cost")
    async def cost(slug: str) -> dict[str, Any]:
        load_bible(slug)
        return {"total_usd": round(store.total_cost(slug), 4), "breakdown": store.cost_breakdown(slug)}

    @app.get("/projects/{slug}/chapter/{chapter}")
    async def get_chapter(slug: str, chapter: int) -> dict[str, Any]:
        load_bible(slug)
        text = store.load_draft(slug, chapter)
        if text is None:
            raise HTTPException(status_code=404, detail="chapter not written")
        return {"chapter": chapter, "text": text, "word_count": len(text.split())}

    return app


app = create_app()
