"""Persistence and retrieval for the Story Bible.

Storage strategy
----------------
The Bible is stored **twice on purpose**:

1. As a single canonical JSON document (``bible_json``) -- lossless, versioned,
   trivially exportable, and the thing that gets checkpointed.
2. As indexed rows in ``characters`` / ``threads`` / ``events`` / ``chapters``
   -- so retrieval is a SQL query instead of a full document scan.

A naive implementation keeps the novel in prose and asks a vector store for
"relevant" context.  That fails in a specific, reproducible way: semantic
similarity does not know that a character is dead, that a promise was made, or
that a door was locked in chapter three.  NovelPilot retrieves *structured
state* (facts, threads, character knowledge) and lets the model handle the
prose.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import (
    ChapterSummary,
    Character,
    FactualEvent,
    Location,
    PlotThread,
    StoryBible,
    ThreadStatus,
)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    slug        TEXT UNIQUE NOT NULL,
    title       TEXT NOT NULL,
    stage       TEXT NOT NULL DEFAULT 'brief',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bible_json (
    project_id  TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    revision    INTEGER NOT NULL DEFAULT 1,
    payload     TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS characters (
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    id          TEXT NOT NULL,
    name        TEXT NOT NULL,
    role        TEXT,
    aliases     TEXT NOT NULL DEFAULT '[]',
    physical    TEXT NOT NULL DEFAULT '{}',
    voice       TEXT NOT NULL DEFAULT '{}',
    goal        TEXT,
    arc         TEXT,
    knowledge   TEXT NOT NULL DEFAULT '[]',
    relationships TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (project_id, id)
);

CREATE TABLE IF NOT EXISTS locations (
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    id          TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT,
    sensory     TEXT,
    PRIMARY KEY (project_id, id)
);

CREATE TABLE IF NOT EXISTS threads (
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    id           TEXT NOT NULL,
    title        TEXT NOT NULL,
    status       TEXT NOT NULL,
    stakes       TEXT,
    introduced_in INTEGER,
    resolved_in  INTEGER,
    related_characters TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (project_id, id)
);
CREATE INDEX IF NOT EXISTS idx_threads_status ON threads(project_id, status);

CREATE TABLE IF NOT EXISTS events (
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    id          TEXT NOT NULL,
    chapter     INTEGER NOT NULL,
    scene       INTEGER,
    summary     TEXT NOT NULL,
    participants TEXT NOT NULL DEFAULT '[]',
    irreversible INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, id)
);
CREATE INDEX IF NOT EXISTS idx_events_chapter ON events(project_id, chapter);

CREATE TABLE IF NOT EXISTS chapters (
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    chapter     INTEGER NOT NULL,
    title       TEXT,
    summary     TEXT,
    pov_character_id TEXT,
    emotional_valence REAL,
    tension     REAL,
    word_count  INTEGER,
    opened_threads   TEXT NOT NULL DEFAULT '[]',
    resolved_threads TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (project_id, chapter)
);

CREATE TABLE IF NOT EXISTS drafts (
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    chapter     INTEGER NOT NULL,
    title       TEXT,
    text        TEXT NOT NULL,
    word_count  INTEGER,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (project_id, chapter)
);

CREATE TABLE IF NOT EXISTS costs (
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    step        TEXT NOT NULL,
    model       TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    usd         REAL NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
"""


@dataclass(slots=True)
class RetrievedFact:
    """One piece of state handed to the model for a scene."""

    kind: str
    text: str
    chapter: int | None = None
    irreversible: bool = False


class BibleStore:
    """SQLite-backed store for one or more projects."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- project lifecycle --------------------------------------------
    def create_project(self, slug: str, title: str) -> str:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO projects (id, slug, title, stage, created_at, updated_at) "
            "VALUES (?, ?, ?, 'brief', ?, ?)",
            (slug, slug, title, now, now),
        )
        if cur.rowcount == 0:
            row = self.conn.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()
            return str(row["id"])
        self.conn.commit()
        row = self.conn.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()
        return str(row["id"])

    def list_projects(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM projects ORDER BY updated_at DESC"))

    def _project_id(self, slug: str) -> str:
        row = self.conn.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            raise KeyError(f"no project named {slug!r}")
        return str(row["id"])

    # -- bible ---------------------------------------------------------
    def save_bible(self, slug: str, bible: StoryBible) -> None:
        pid = self._project_id(slug)
        now = _now_iso()
        row = self.conn.execute(
            "SELECT revision FROM bible_json WHERE project_id = ?", (pid,)
        ).fetchone()
        rev = (int(row["revision"]) + 1) if row else 1
        self.conn.execute(
            "INSERT INTO bible_json (project_id, revision, payload, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(project_id) DO UPDATE SET revision=excluded.revision, "
            "payload=excluded.payload, updated_at=excluded.updated_at",
            (pid, rev, bible.model_dump_json(), now),
        )
        self._reindex(pid, bible)
        self.conn.execute(
            "UPDATE projects SET updated_at = ?, stage = ? WHERE id = ?",
            (now, bible.stage.value, pid),
        )
        self.conn.commit()

    def load_bible(self, slug: str) -> StoryBible:
        pid = self._project_id(slug)
        row = self.conn.execute(
            "SELECT payload FROM bible_json WHERE project_id = ?", (pid,)
        ).fetchone()
        if row is None:
            return StoryBible()
        return StoryBible.model_validate_json(row["payload"])

    def bible_revision(self, slug: str) -> int:
        pid = self._project_id(slug)
        row = self.conn.execute(
            "SELECT revision FROM bible_json WHERE project_id = ?", (pid,)
        ).fetchone()
        return int(row["revision"]) if row else 0

    def _reindex(self, pid: str, bible: StoryBible) -> None:
        for table in ("characters", "locations", "threads", "events", "chapters"):
            self.conn.execute(f"DELETE FROM {table} WHERE project_id = ?", (pid,))
        for c in bible.characters:
            self.conn.execute(
                "INSERT INTO characters (project_id,id,name,role,aliases,physical,voice,goal,arc,knowledge,relationships) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    pid, c.id, c.name, c.role, json.dumps(c.aliases), json.dumps(c.physical),
                    json.dumps(c.voice, default=str), c.goal, c.arc,
                    json.dumps(c.knowledge), json.dumps(c.relationships),
                ),
            )
        for loc in bible.locations:
            self.conn.execute(
                "INSERT INTO locations (project_id,id,name,kind,sensory) VALUES (?,?,?,?,?)",
                (pid, loc.id, loc.name, loc.kind, loc.sensory),
            )
        for t in bible.threads:
            self.conn.execute(
                "INSERT INTO threads (project_id,id,title,status,stakes,introduced_in,resolved_in,related_characters) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (pid, t.id, t.title, t.status.value, t.stakes, t.introduced_in,
                 t.resolved_in, json.dumps(t.related_characters)),
            )
        for e in bible.events:
            self.conn.execute(
                "INSERT INTO events (project_id,id,chapter,scene,summary,participants,irreversible) "
                "VALUES (?,?,?,?,?,?,?)",
                (pid, e.id, e.chapter, e.scene, e.summary, json.dumps(e.participants),
                 int(e.irreversible)),
            )
        for s in bible.chapter_summaries:
            self.conn.execute(
                "INSERT INTO chapters (project_id,chapter,title,summary,pov_character_id,"
                "emotional_valence,tension,word_count,opened_threads,resolved_threads) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pid, s.chapter, s.title, s.summary, s.pov_character_id, s.emotional_valence,
                 s.tension, s.word_count, json.dumps(s.opened_threads), json.dumps(s.resolved_threads)),
            )

    # -- retrieval ------------------------------------------------------
    def open_threads(self, slug: str) -> list[PlotThread]:
        pid = self._project_id(slug)
        bible = self.load_bible(slug)
        rows = self.conn.execute(
            "SELECT * FROM threads WHERE project_id = ? AND status IN ('open','advancing')",
            (pid,),
        ).fetchall()
        by_id = {t.id: t for t in bible.threads}
        out: list[PlotThread] = []
        for r in rows:
            t = by_id.get(str(r["id"]))
            if t:
                out.append(t)
        return out

    def facts_through(self, slug: str, chapter: int) -> list[RetrievedFact]:
        """Facts known as of (and including) a chapter."""
        pid = self._project_id(slug)
        rows = self.conn.execute(
            "SELECT summary, chapter, irreversible FROM events "
            "WHERE project_id = ? AND chapter <= ? ORDER BY chapter ASC, id ASC",
            (pid, chapter),
        ).fetchall()
        return [
            RetrievedFact(
                kind="event",
                text=str(r["summary"]),
                chapter=int(r["chapter"]),
                irreversible=bool(r["irreversible"]),
            )
            for r in rows
        ]

    def character_knowledge(self, slug: str, char_id: str, through_chapter: int) -> list[str]:
        """What a character knows by a given point -- used to prevent leaks."""
        bible = self.load_bible(slug)
        char = bible.character(char_id)
        if not char:
            return []
        known = list(char.knowledge)
        for e in bible.events:
            if e.chapter <= through_chapter and char_id in e.participants:
                known.append(e.summary)
        return known

    def chapter_summaries(self, slug: str, through_chapter: int) -> list[ChapterSummary]:
        bible = self.load_bible(slug)
        return bible.summaries_up_to(through_chapter)

    # -- drafts ---------------------------------------------------------
    def save_draft(self, slug: str, chapter: int, title: str, text: str) -> None:
        pid = self._project_id(slug)
        self.conn.execute(
            "INSERT INTO drafts (project_id,chapter,title,text,word_count,created_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(project_id,chapter) DO UPDATE SET "
            "title=excluded.title, text=excluded.text, word_count=excluded.word_count",
            (pid, chapter, title, text, len(text.split()), _now_iso()),
        )
        self.conn.commit()

    def load_draft(self, slug: str, chapter: int) -> str | None:
        pid = self._project_id(slug)
        row = self.conn.execute(
            "SELECT text FROM drafts WHERE project_id = ? AND chapter = ?", (pid, chapter)
        ).fetchone()
        return str(row["text"]) if row else None

    def load_all_drafts(self, slug: str) -> dict[int, str]:
        pid = self._project_id(slug)
        rows = self.conn.execute(
            "SELECT chapter, text FROM drafts WHERE project_id = ? ORDER BY chapter", (pid,)
        ).fetchall()
        return {int(r["chapter"]): str(r["text"]) for r in rows}

    # -- cost ledger ----------------------------------------------------
    def record_cost(
        self, slug: str, step: str, model: str, prompt_tokens: int, completion_tokens: int, usd: float
    ) -> None:
        pid = self._project_id(slug)
        self.conn.execute(
            "INSERT INTO costs (project_id,step,model,prompt_tokens,completion_tokens,usd,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (pid, step, model, prompt_tokens, completion_tokens, usd, _now_iso()),
        )
        self.conn.commit()

    def total_cost(self, slug: str) -> float:
        pid = self._project_id(slug)
        row = self.conn.execute(
            "SELECT COALESCE(SUM(usd),0) AS c FROM costs WHERE project_id = ?", (pid,)
        ).fetchone()
        return float(row["c"])

    def cost_breakdown(self, slug: str) -> list[dict[str, Any]]:
        pid = self._project_id(slug)
        rows = self.conn.execute(
            "SELECT step, model, SUM(prompt_tokens) p, SUM(completion_tokens) c, SUM(usd) u "
            "FROM costs WHERE project_id = ? GROUP BY step, model ORDER BY u DESC",
            (pid,),
        ).fetchall()
        return [
            {
                "step": r["step"],
                "model": r["model"],
                "prompt_tokens": int(r["p"]),
                "completion_tokens": int(r["c"]),
                "usd": round(float(r["u"]), 4),
            }
            for r in rows
        ]


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
