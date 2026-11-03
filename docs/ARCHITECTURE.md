# Architecture

This document describes how NovelPilot is put together: the module map, the
state model, the data flow, and the extension points.

For the *reasoning* behind the stack choices, see
[TECH_STACK.md](TECH_STACK.md).

---

## The one idea

Everything in this codebase follows from a single observation:

> A 90,000-word novel does not fit in a context window, and "write chapter 12"
> is not a plan.

So the system separates **state** from **prose**:

- **State** — the Story Bible: characters, locations, plot threads, facts,
  events, chapter summaries. Structured, queryable, persisted.
- **Prose** — the generated chapters. An output artifact, disposable and
  regenerable.

Every operation reads from state and writes back to state. The manuscript is
never read to decide what happens next; it is *summarised into* state, and
state is what gets retrieved.

---

## Module map

```
src/novelpilot/
│
├── core/
│   ├── models.py        Domain types. The Story Bible and everything hanging
│   │                    off it. Pure pydantic, no I/O, no logic.
│   │
│   ├── bible.py         BibleStore: SQLite persistence + structured
│   │                    retrieval (open threads, facts through chapter N,
│   │                    character knowledge).
│   │
│   ├── planner.py       premise -> Outline -> ChapterPlan -> SceneCard.
│   │                    Pure functions over an LLM.
│   │
│   ├── writer.py        SceneCard -> prose, and prose -> state deltas.
│   │                    The Writer owns the round trip.
│   │
│   ├── continuity.py    build_continuity_pack() and verify_chapter().
│   │                    The context-assembly and contradiction-detection
│   │                    halves of the coherence problem.
│   │
│   ├── voice.py         StyleMetrics: measure a sample, emit numeric
│   │                    directives, detect drift afterwards.
│   │
│   ├── assembler.py     Manuscript -> EPUB / DOCX / TXT + KDP metadata.
│   │
│   ├── engine.py        Role-routed, cost-tracked LLM access. Everything
│   │                    that talks to a model goes through here.
│   │
│   └── config.py        YAML + env configuration with documented precedence.
│
├── providers/
│   ├── base.py          LLMProvider protocol, Message/LLMRequest/LLMResponse.
│   ├── openai_compat.py One adapter for the OpenAI chat-completions dialect.
│   ├── anthropic.py     Anthropic Messages API.
│   └── registry.py      Model catalogue (context, pricing, role affinity)
│                        and role -> model resolution.
│
├── graph/
│   └── pipeline.py      The resumable DAG: plan, write, sync, verify, export.
│
├── plugins/
│   ├── manager.py       Discovery + registration.
│   ├── covers.py        Deterministic typographic cover generator.
│   └── audit.py         Content Rights Certificate + Generation Audit Log.
│
├── server/
│   └── app.py           FastAPI application.
│
└── cli.py               Command-line interface.
```

### Dependency direction

```
cli.py / server/app.py
        |
        v
   graph/pipeline.py  --------->  core/writer.py
        |                              |
        v                              v
   core/planner.py  --------->  core/continuity.py
        |                              |
        +-------------> core/bible.py <-+
                             |
                             v
                       core/models.py

   everything that calls a model ----> core/engine.py ----> providers/*
```

`core/models.py` depends on nothing internal. `providers/*` depends on nothing
internal. Everything else may depend on both. There are no cycles.

---

## The state model

### Entities

| Entity | Purpose |
|---|---|
| `StoryBible` | The root container. Everything else hangs off it. |
| `Character` | Identity, physical traits, voice, goals, arc, relationships, **knowledge**. |
| `Location` | Name, kind, description, **sensory signature**. |
| `PlotThread` | A narrative thread with a status, stakes, and a payoff note. |
| `FactualEvent` | Something that happened. `irreversible` flags must-not-contradict facts. |
| `ChapterSummary` | Rolling per-chapter summary plus tension and emotional valence. |
| `Outline` / `ActPlan` / `BeatSheet` | Structure, with a tension value per beat. |
| `ChapterPlan` / `SceneCard` | The executable unit of generation. |
| `ChapterDraft` | Prose plus any continuity issues found in it. |

### Why `knowledge` lives on the character

A continuity failure that readers actually notice is a **knowledge leak**: a
character reacting to information they could not have. It is not caught by
tracking facts — the fact is correct, the *holder* is wrong.

So each character carries an explicit list of what they know. When a scene is
assembled, the pack states the boundary:

```
## WHAT THE POV KNOWS (the POV must not know anything else yet)
- The message is a forgery
```

That is a far stronger instruction than "be consistent".

### Why `PlotThread` is a table, not a list in the outline

Threads outlive chapters. A thread opened in chapter 4 and paid off in
chapter 30 needs to be *tracked*, not remembered. Explicit status means the
engine can report dangling threads instead of shipping a book where the
central conflict evaporates.

---

## Persistence

### The double-write

`BibleStore.save_bible()` writes the Bible **twice**:

1. **Canonical JSON** (`bible_json` table) — the complete `StoryBible`
   serialised. Lossless, versioned via a `revision` counter, trivially
   exported, and what gets checkpointed.
2. **Indexed rows** (`characters`, `locations`, `threads`, `events`,
   `chapters`) — the queryable projections.

Reads that need structure go to SQL. Reads that need everything go to the JSON.
The two are written in the same transaction, so they cannot diverge.

### Schema highlights

```sql
CREATE INDEX idx_threads_status ON threads(project_id, status);
CREATE INDEX idx_events_chapter ON events(project_id, chapter);
```

The two queries that run on every scene:
- open threads for a project
- facts for a project up to chapter N

### Integrity

`StoryBible.validate()` runs on every save and checks:

- duplicate character ids or names
- relationships pointing at unknown characters
- threads referencing unknown characters
- resolved threads with no `resolved_in` chapter
- events referencing unknown characters

The API returns all problems at once with a 422, rather than one per
round-trip.

---

## Data flow

```
                    ┌─────────────────────────┐
                    │  POST /projects         │
                    │  (brief + cast)         │
                    └────────────┬────────────┘
                                 v
                    ┌─────────────────────────┐
                    │  StoryBible created     │  validate() -> 422 on problems
                    │  characters, locations  │
                    └────────────┬────────────┘
                                 v
                    ┌─────────────────────────┐
                    │  plan_outline()         │  role=PLAN
                    │  -> Outline (acts+beats)│  tension curve shaped
                    └────────────┬────────────┘
                                 v
                    ┌─────────────────────────┐
                    │  plan_chapters()        │  role=PLAN
                    │  -> SceneCards          │
                    └────────────┬────────────┘
                                 v
              ┌──────────────────────────────────────┐
              │  for each scene:                     │
              │    build_continuity_pack(bible,scene)│  structured retrieval
              │    write_scene(pack, voice)          │  role=PROSE
              └──────────────────┬───────────────────┘
                                 v
              ┌──────────────────────────────────────┐
              │  sync_bible(draft)                   │  role=SUMMARY
              │    -> events, threads, knowledge,     │
              │       chapter summaries               │
              └──────────────────┬───────────────────┘
                                 v
                    ┌─────────────────────────┐
                    │  verify_chapter()       │  role=JUDGE
                    │  deterministic + LLM     │
                    └────────────┬────────────┘
                                 v
                    ┌─────────────────────────┐
                    │  export: EPUB/DOCX/TXT  │
                    │  + cover + audit log    │
                    └─────────────────────────┘
```

---

## The scene card is the contract

Everything interesting about the design is in `SceneCard`:

```python
class SceneCard(BaseModel):
    chapter: int
    index: int
    pov_character_id: str | None
    location_id: str | None
    participants: list[str]
    goal: str
    conflict: str
    outcome: str
    must_include: list[str]
    must_avoid: list[str]
    thread_ids: list[str]
    approx_words: int
```

The writer **never sees the raw outline**. It sees a scene card. The outline is
the author's creative intent; the scene card is a complete, self-contained
instruction for one scene.

This is the load-bearing separation. It means:

- the prose step cannot drift from the plan, because the plan *is* the input;
- a scene card can be edited by a human without touching the outline;
- the continuity pack can be assembled deterministically from the card;
- `must_avoid` gives the author negative constraints ("the broken hand is not
  healed yet"), which is how physical-state continuity is enforced.

---

## Extension points

### A new provider

Implement `LLMProvider` (four methods) and register it in
`providers/registry.py`. If it speaks the OpenAI dialect, you probably do not
need to write anything — just add the endpoint to `ENDPOINTS`.

### A new plugin

Drop a module in `src/novelpilot/plugins/`:

```python
manifest = {"name": "myplugin", "version": "0.1.0", "description": "..."}

class MyPlugin:
    async def run(self, **kwargs): ...

plugin = MyPlugin()
```

`PluginManager.discover()` imports every module in the package and registers
anything exposing a module-level `plugin`.

### A new export format

Add a function to `core/assembler.py` returning a `Path`, and register it in
`export_all()`.

### A new state field

Add it to the model in `core/models.py`, add a column in `SCHEMA`, and extend
`_reindex()`. Existing projects migrate because the Bible is stored as JSON —
old payloads simply lack the new field and get its default.

---

## Concurrency and resumability

The pipeline is `async` end to end, but generation is **sequential by
design**: chapter N+1 needs the state that chapter N produced. Parallelising
across chapters would break the very coherence the design depends on.

What *is* parallelisable:

- **Chapters in different books** — separate projects, separate pipelines.
- **Scenes within a chapter** — in principle, but they share a pack and a
  voice, and sequential scenes keep the prose flow continuous. Not worth it.

Resumability comes from persistence, not from scheduling: every step writes its
result, and `Pipeline.run()` skips chapters that already have a draft. Kill
the process mid-book, restart, and it picks up where it stopped without
re-paying for finished chapters.

---

## Security notes

- Secrets are read from the environment, never from the config file.
- The config file records only the *name* of the environment variable.
- No user input reaches a shell. File writes go through `Path` operations with
  sanitised slugs (`Project.slug` is validated by a pydantic field validator).
- The provider layer sends only the prompt content to the configured endpoint.
  If that endpoint is a third-party API, the prompt — which contains story
  state — leaves the machine. This is worth being explicit about, and it is the
  strongest practical argument for the local-model configuration.
