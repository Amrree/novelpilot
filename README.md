# NovelPilot

**The AI novel engine that actually writes books.**

NovelPilot generates a complete, publishable novel — outline, characters, chapter
prose, cover and export files — from a short brief. It is built around one idea that
most AI writing tools get wrong:

> A 90,000-word novel does not fit in a context window, and "write chapter 12" is not
> a plan. Coherence comes from *structured state*, not from a longer prompt.

So the Story Bible — characters, plot threads, facts, relationships — is the source of
truth, stored as queryable rows. Every scene retrieves exactly the slice of state it
needs. Prose is the output; the Bible is the book.

---

## Quick start

```bash
git clone https://github.com/Amrree/novelpilot.git
cd novelpilot
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[server,dev]"

export OPENAI_API_KEY=sk-...        # or ANTHROPIC_API_KEY, or run a local Ollama

novelpilot new "The Salt Road" \
    --genre fantasy \
    --premise "A salt courier must cross a dissolving empire to deliver a message that will end the war." \
    --words 80000

novelpilot plan the-salt-road       # outline -> beats -> scene cards
novelpilot write the-salt-road --chapters 3
novelpilot export the-salt-road --author "Jane Doe"
```

Local model, no API key:

```bash
ollama pull llama3.1:70b
export NOVELPILOT_PROVIDER=ollama
export NOVELPILOT_BASE_URL=http://localhost:11434/v1
```

---

## How it works

```
brief
  │
  ├─▶ PLAN ────────▶ Outline: 3 acts + beats with a tension curve
  │                   ChapterPlans: scene cards (POV, goal, conflict, outcome)
  │
  ├─▶ WRITE ───────▶ For each scene:
  │                     1. build continuity pack from the Bible  (structured retrieval)
  │                     2. write prose under voice directives     (style control)
  │                     3. fold the result back into the Bible   (state deltas)
  │
  ├─▶ VERIFY ──────▶ deterministic checks + LLM judge for contradictions
  │
  └─▶ EXPORT ──────▶ EPUB (KDP-safe), DOCX (6x9 paperback), TXT, cover, audit log
```

Every step is persisted, so a run can be interrupted at any point and resumed without
re-paying for work already done. `novelpilot write` is idempotent per chapter.

---

## The three problems this solves

### 1. Coherence across a long book

The engine never re-reads the manuscript. It re-reads *state*:

| Question | Answered by |
|---|---|
| "What does Mara know at chapter 12?" | `characters.knowledge` + events up to ch12 |
| "Is the salt-road thread still open?" | `threads.status` |
| "Did anyone actually reach the border?" | `events` (with `irreversible` flags) |
| "What did this character look like?" | `characters.physical` |

The assembled **continuity pack** is small (a few thousand tokens), so it can be
pasted into every scene prompt without the bill exploding. That is the whole trick:
structured retrieval instead of brute-force context stuffing.

### 2. Style drift and the "AI tell"

`core/voice.py` *measures* the target style — average sentence length and its variance,
paragraph length, em-dash density, dialogue ratio — and converts those measurements into
numeric directives. Models follow numbers far more reliably than they follow "write in
the style of...". It also bans the specific vocabulary that reads as machine-written
(*delve*, *tapestry*, *poignant*, …) and detects drift after the fact.

### 3. Plot that actually pays off

Every plot beat is a tracked **thread** with a status and an optional payoff note.
Threads are opened and resolved explicitly by the chapter-sync step. The engine reports
dangling threads at the end instead of quietly shipping a book where the central
conflict never resolves.

---

## Documentation

| Document | What it covers |
|---|---|
| [docs/TECH_STACK.md](docs/TECH_STACK.md) | **Every stack decision and why.** Python vs TS, provider layer, state store, no-vector-DB rationale, deployment. |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Module map, data flow, the Story Bible schema, extension points. |
| [docs/GENERATION_PIPELINE.md](docs/GENERATION_PIPELINE.md) | Each pipeline stage in detail, with the actual prompts. |
| [docs/COST_MODEL.md](docs/COST_MODEL.md) | What a book costs to generate, per model, and how to make it cheaper. |
| [docs/EVALS.md](docs/EVALS.md) | How to measure whether the output is actually good. |
| [docs/ROADMAP.md](docs/ROADMAP.md) | What is built, what is next, what is deliberately not being built. |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Running it locally, on a server, and behind a queue. |

---

## Architecture at a glance

```
src/novelpilot/
├── core/
│   ├── models.py       Story Bible, scene cards, outline, drafts  (pydantic)
│   ├── bible.py        SQLite persistence + structured retrieval
│   ├── planner.py      outline -> beats -> scene cards
│   ├── writer.py       scene -> prose, and prose -> state deltas
│   ├── continuity.py   context assembly + contradiction detection
│   ├── voice.py        style measurement and directives
│   ├── assembler.py    EPUB / DOCX / TXT / KDP metadata
│   ├── engine.py       role-routed LLM access with cost accounting
│   └── config.py       YAML + env configuration
├── providers/
│   ├── base.py         LLMProvider protocol
│   ├── openai_compat.py  OpenAI + Ollama + LM Studio + vLLM + Groq + ...
│   ├── anthropic.py    Anthropic Messages API
│   └── registry.py     model catalogue and role routing
├── graph/pipeline.py   the resumable DAG
├── plugins/            covers, audit, and your own
└── server/app.py       FastAPI application
```

---

## Plugin system

Plugins are optional and discovered at runtime, following the same pattern as
`BookOfShadows`:

```python
# src/novelpilot/plugins/myplugin.py
manifest = {"name": "myplugin", "version": "0.1.0", "description": "..."}

class MyPlugin:
    async def run(self, **kwargs): ...

plugin = MyPlugin()
```

Built in: `covers` (deterministic typographic cover generator) and `audit`
(Content Rights Certificate + Generation Audit Log).

---

## Configuration

`config/default.yaml` holds the defaults; environment variables override it.
Secrets are never stored in the file — only the *name* of the environment variable
that holds the key.

```yaml
provider: openai
models:
  plan: null       # planning is a small share of the bill
  prose: null      # prose is almost all of it
  summary: null
  judge: null
max_cost_per_book_usd: 5.00
```

See [docs/TECH_STACK.md](docs/TECH_STACK.md) for the full rationale behind each
setting.

---

## License

MIT. See [LICENSE](LICENSE).
