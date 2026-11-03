# Roadmap

What is built, what is next, and — just as importantly — what is deliberately
not being built.

---

## Shipped (v0.1.0)

### Core engine
- [x] Story Bible domain model with referential-integrity validation
- [x] SQLite persistence: canonical JSON + indexed relational projections
- [x] Structured retrieval: open threads, facts through chapter N, character knowledge
- [x] Continuity pack assembly (context retrieval, anti-leak, size-capped)
- [x] Three-act outline with tension-curve shaping
- [x] Chapter planning into executable scene cards
- [x] Scene writer with style directives and output cleanup
- [x] Chapter sync: prose → state deltas → Bible
- [x] Deterministic continuity checks
- [x] LLM-judged continuity checks
- [x] Voice: style measurement, numeric directives, drift detection
- [x] Role-routed engine with cost accounting and budget guards

### Providers
- [x] `LLMProvider` protocol
- [x] OpenAI-compatible adapter (OpenAI, Ollama, LM Studio, vLLM, Groq, OpenRouter, DeepSeek, Mistral, xAI, NVIDIA NIM, Together, Fireworks)
- [x] Anthropic Messages adapter
- [x] Model catalogue with context windows, pricing and role affinity
- [x] Role → model resolution with override support

### Output
- [x] EPUB 3 writer (KDP-safe: mimetype first, stored)
- [x] DOCX writer (6×9in trade paperback)
- [x] Plain text export
- [x] KDP metadata (BISAC categories, trim, description)
- [x] Deterministic typographic cover generator (2565×2565, bleed-aware)
- [x] Content Rights Certificate + Generation Audit Log

### Interfaces
- [x] CLI: `new`, `plan`, `write`, `export`, `cost`, `doctor`, `serve`
- [x] FastAPI server: 14 routes including SSE progress streaming
- [x] Plugin manager with auto-discovery

### Quality
- [x] 84 passing tests
- [x] End-to-end CLI verification against a mock OpenAI-compatible server

---

## Next (v0.2.0) — *make it measurably good*

The engine generates books. The next milestone is proving the books are good.

- [ ] **Repetition metric** — 4-gram diversity per chapter (see [EVALS.md](EVALS.md))
- [ ] **Eval harness** — `evals/run.py`, corpus of briefs, automated scoring
- [ ] **Human-edited reference chapters** — 3 books' worth. This is the
      bottleneck; it needs human time, not engineering
- [ ] **Per-chapter quality report** — a markdown scorecard per book
- [ ] **Repetition-triggered regeneration** — regenerate a scene when
      repetition exceeds threshold, with a cost guard
- [ ] **Mid-book revision** — the ability to change a beat in chapter 12 and
      have chapters 13+ regenerate against the new state

## Later (v0.3.0) — *make it a product*

- [ ] **Next.js frontend** — a real workspace UI: brief editor, live generation,
      chapter reader with continuity warnings, export panel
- [ ] **Series support** — books that share a cast and continuity across volumes
- [ ] **Illustration generation** — chapter illustrations via an image model
- [ ] **Cover image models** — generative artwork behind the typographic fallback
- [ ] **Auth + multi-tenancy** — projects belong to users
- [ ] **Billing** — per-book or subscription, metered on actual model cost
- [ ] **Interactive editing** — edit a scene card, regenerate just that scene

## Infrastructure (as needed, not before)

- [ ] **Queue-backed workers** — Redis + arq when generation volume justifies
      it. Currently an in-process async worker with a persisted job table.
- [ ] **Daily token budget** — needs a central counter (DB or Redis)
- [ ] **Per-user rate limiting**
- [ ] **Object storage for exports** — S3-compatible
- [ ] **Observability** — OpenTelemetry traces, structured logs

---

## Deliberately not building

Things a reasonable person might expect, and the reasons.

### A vector database for continuity

The single most-argued decision. See [TECH_STACK.md §4](TECH_STACK.md). Semantic
similarity does not know that a character is dead or that a promise was made.
Continuity is a query problem, not a similarity problem.

A vector index over chapter summaries may still be added — for *search*
("find me every scene where Mara and the salt road co-occur"), which is a
different feature from continuity. `sentence-transformers` is already an
optional dependency.

### An agent framework

LangChain / LangGraph / Pydantic-AI. The pipeline is a known, fixed DAG.
These tools pay off with dynamic, tool-using, branching graphs. Ours is a
straight line. The reasoning is in [TECH_STACK.md §5](TECH_STACK.md).

### Full prose regeneration for every edit

When an author changes the plan, regenerating the whole book is expensive and
throws away good prose. The design supports targeted scene regeneration from
the start (`SceneCard` is the unit), so this is a matter of wiring, not
architecture.

### A full writing environment

NovelPilot generates books. It is not a replacement for Scrivener, and it
should not try to be. The target user is someone with a story they cannot
find the time to write, not a professional with a toolchain.

### Multi-language generation

The model layer supports it; the prompts are English-specific and the voice
engine measures English text. Adding a language means a localisation of the
prompts and the voice metrics, which is a real project, not a flag.

### Training or fine-tuning a model

Every model in the catalogue is a hosted or local general-purpose model.
Fine-tuning on a specific author's corpus is a genuine product feature, but it
is a separate product with its own evaluation and licensing questions.

---

## Versioning

Semantic versioning, with one addition: **a change to a prompt or to the
continuity pack format is a minor version bump**, because it changes output
quality in ways users will notice. If you upgrade NovelPilot and regenerate
your book, the book will be different. That should be visible in the version
number.
