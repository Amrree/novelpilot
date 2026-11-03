"""Command-line interface.

    novelpilot new "The Salt Road" --genre fantasy --premise "..."
    novelpilot plan my-book
    novelpilot write my-book --chapters 3
    novelpilot export my-book --author "Jane Doe"
    novelpilot doctor
    novelpilot serve

Uses argparse rather than a CLI framework so the tool has no dependency
beyond the engine itself.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .core.bible import BibleStore
from .core.config import Config
from .core.engine import Engine
from .core.models import StoryBible
from .core.voice import StyleMetrics, build_voice_profile
from .graph.pipeline import Pipeline
from .plugins.manager import PluginManager
from .providers.registry import available_providers, build_provider, resolve_model
from .providers.base import Role


def _slugify(text: str) -> str:
    out = "".join(ch if ch.isalnum() else "-" for ch in text.lower()).strip("-")
    return "-".join(p for p in out.split("-") if p) or "untitled"


def _make_engine(cfg: Config) -> Engine:
    kwargs: dict[str, Any] = {}
    if cfg.base_url:
        kwargs["base_url"] = cfg.base_url
    provider = build_provider(cfg.provider, **kwargs)
    return Engine(
        provider=provider,
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


def _progress(step: str, payload: dict[str, Any]) -> None:
    extras = " ".join(f"{k}={v}" for k, v in payload.items() if k != "step")
    print(f"[{step}] {extras}", flush=True)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_new(args: argparse.Namespace, cfg: Config) -> int:
    slug = args.slug or _slugify(args.title)
    store = BibleStore(cfg.db_path)
    store.create_project(slug, args.title)

    bible = StoryBible(
        title=args.title,
        genre=args.genre or "",
        subgenre=args.subgenre or "",
        logline=args.logline or "",
        premise=args.premise or "",
        audience=args.audience or "",
        target_word_count=args.words,
        pov=args.pov,  # type: ignore[arg-type]
        tense=args.tense,
        style_directives=args.style or [],
        banned_phrases=args.banned or [],
    )
    problems = bible.validate()
    if problems:
        print("Validation problems:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2
    store.save_bible(slug, bible)
    print(f"Created project '{slug}' -> {cfg.db_path}")
    print(f"Next: novelpilot plan {slug}")
    return 0


def cmd_plan(args: argparse.Namespace, cfg: Config) -> int:
    store = BibleStore(cfg.db_path)
    engine = _make_engine(cfg)
    pipe = Pipeline(store, engine, progress=_progress)
    plans = asyncio.run(
        pipe.step_chapters(
            slug=args.slug,
            scenes_per_chapter=args.scenes,
            words_per_chapter=args.words,
        )
    )
    if args.json:
        print(json.dumps([p.model_dump() for p in plans], indent=2, default=str))
    else:
        for p in plans:
            print(f"Ch{p.chapter:>3}  {p.title or '(untitled)'}  [{len(p.scenes)} scenes]")
    print(f"\nCost so far: ${engine.total_cost:.4f}")
    return 0


def cmd_write(args: argparse.Namespace, cfg: Config) -> int:
    store = BibleStore(cfg.db_path)
    engine = _make_engine(cfg)
    pipe = Pipeline(store, engine, progress=_progress)
    result = asyncio.run(
        pipe.run(
            slug=args.slug,
            chapters=args.chapters,
            scenes_per_chapter=args.scenes,
            words_per_chapter=args.words,
            outdir=cfg.exports_dir if args.export else None,
            author=args.author,
            verify=not args.no_verify,
        )
    )
    print(f"\nWrote chapters: {result.chapters_written}")
    print(f"Cost: ${result.cost_usd:.4f}")
    if result.issues:
        print(f"Continuity issues: {len(result.issues)}")
        for issue in result.issues[:10]:
            print(f"  [{issue.get('severity')}] {issue.get('message')}")
    for kind, path in result.exports.items():
        print(f"  {kind}: {path}")
    return 0


def cmd_export(args: argparse.Namespace, cfg: Config) -> int:
    store = BibleStore(cfg.db_path)
    engine = _make_engine(cfg)
    pipe = Pipeline(store, engine, progress=_progress)
    paths = asyncio.run(
        pipe.step_export(args.slug, args.outdir, author=args.author, cover=args.cover)
    )
    for kind, path in paths.items():
        print(f"{kind}: {path}")
    return 0


def cmd_cost(args: argparse.Namespace, cfg: Config) -> int:
    store = BibleStore(cfg.db_path)
    print(f"Total: ${store.total_cost(args.slug):.4f}")
    for row in store.cost_breakdown(args.slug):
        print(
            f"  {row['step']:<18} {row['model']:<28} "
            f"{row['prompt_tokens']:>9,}p {row['completion_tokens']:>9,}c  ${row['usd']:.4f}"
        )
    return 0


def cmd_doctor(args: argparse.Namespace, cfg: Config) -> int:
    print(f"provider (configured): {cfg.provider}")
    print("\nproviders:")
    for name, ok in available_providers().items():
        print(f"  {'OK ' if ok else '-- '} {name}")
    print("\nrole routing:")
    for role in (Role.PLAN, Role.PROSE, Role.SUMMARY, Role.JUDGE):
        print(f"  {role.value:<8} -> {resolve_model(role, provider=cfg.provider)}")
    print("\nplugins:")
    pm = PluginManager()
    for info in pm.discover():
        print(f"  {info.name:<12} {info.version:<8} {info.description}")
    return 0


def cmd_serve(args: argparse.Namespace, cfg: Config) -> int:
    try:
        import uvicorn
    except ImportError:
        print("uvicorn is required: pip install 'novelpilot[server]'", file=sys.stderr)
        return 2
    uvicorn.run(
        "novelpilot.server.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="novelpilot", description="The AI novel engine that actually writes books.")
    p.add_argument("--config", help="Path to a YAML config file")
    p.add_argument("--db", help="Path to the SQLite database")
    sub = p.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="Create a new project from a brief")
    new.add_argument("title")
    new.add_argument("--slug")
    new.add_argument("--genre", default="")
    new.add_argument("--subgenre", default="")
    new.add_argument("--logline", default="")
    new.add_argument("--premise", default="")
    new.add_argument("--audience", default="")
    new.add_argument("--words", type=int, default=80_000)
    new.add_argument("--pov", default="third_person_past",
                     choices=["first_person", "first_person_present", "third_person_past",
                              "third_person_present", "multi_pov"])
    new.add_argument("--tense", default="past")
    new.add_argument("--style", action="append", help="Style directive (repeatable)")
    new.add_argument("--banned", action="append", help="Banned phrase (repeatable)")
    new.set_defaults(func=cmd_new)

    plan = sub.add_parser("plan", help="Generate the outline and chapter/scene plan")
    plan.add_argument("slug")
    plan.add_argument("--scenes", type=int, default=3)
    plan.add_argument("--words", type=int, default=2500)
    plan.add_argument("--json", action="store_true")
    plan.set_defaults(func=cmd_plan)

    write = sub.add_parser("write", help="Generate chapters")
    write.add_argument("slug")
    write.add_argument("--chapters", type=int, help="Stop after N chapters")
    write.add_argument("--scenes", type=int, default=3)
    write.add_argument("--words", type=int, default=2500)
    write.add_argument("--author", default="Anonymous")
    write.add_argument("--export", action="store_true", help="Export when finished")
    write.add_argument("--no-verify", action="store_true", help="Skip the continuity judge")
    write.set_defaults(func=cmd_write)

    export = sub.add_parser("export", help="Export the manuscript to EPUB/DOCX/TXT")
    export.add_argument("slug")
    export.add_argument("--outdir", default="exports")
    export.add_argument("--author", default="Anonymous")
    export.add_argument("--cover")
    export.set_defaults(func=cmd_export)

    cost = sub.add_parser("cost", help="Show the cost breakdown for a project")
    cost.add_argument("slug")
    cost.set_defaults(func=cmd_cost)

    doctor = sub.add_parser("doctor", help="Check provider and plugin availability")
    doctor.set_defaults(func=cmd_doctor)

    serve = sub.add_parser("serve", help="Run the API server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = Config.load(args.config)
    if args.db:
        cfg.db_path = Path(args.db)
    cfg.ensure_dirs()
    try:
        return int(args.func(args, cfg) or 0)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
