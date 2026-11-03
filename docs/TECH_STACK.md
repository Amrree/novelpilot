# The Tech Stack

This document states every significant decision, the reasoning, and the alternatives
that were considered and rejected. It is written for the person who will maintain
this code in a year and needs to know *why* it is shaped this way.

**Summary of the stack:**

| Layer | Choice | One-line reason |
|---|---|---|
| Language | Python 3.11+ | The AI ecosystem is Python; the awkward parts are all model-shaped. |
| API surface | FastAPI + Uvicorn | Async, typed, OpenAPI for free, SSE for live generation. |
| Model access | Own thin protocol + OpenAI-compatible adapter | One adapter covers OpenAI, Anthropic gateways, Ollama, LM Studio, vLLM, Groq, OpenRouter. |
| State | SQLite (WAL) + explicit relational schema | Continuity is a *query* problem, not a similarity problem. |
| Retrieval | Structured SQL only (no vector DB) | Semantic similarity does not know that a character is dead. |
| Orchestration | Hand-written resumable DAG | The graph is fixed; a framework adds indirection, not capability. |
| Export | Hand-written EPUB writer + python-docx | KDP's rules are specific and libraries ignore them. |
| Frontend | Next.js (planned) | Not in the engine's critical path. |
| Validation | pydantic v2 | The Story Bible is the product; it must be a typed contract. |

---

## 1. Language: Python, not TypeScript

**Decision: Python 3.11+ for the entire engine.**

The tempting alternative is TypeScript end-to-end, so the frontend and backend share
types and one toolchain. That is a real advantage, and for a CRUD-ish SaaS it is
usually the right call. It is the wrong call *here*, for three specific reasons.

**a) The hard parts are model-shaped, not UI-shaped.** The interesting work in this
product is prompt construction, token accounting, structured state reconciliation, and
evaluation. The Python ecosystem is where those libraries live (inspect-ai, lm-eval,
pydantic, sentence-transformers, vLLM's own client code). In TypeScript you are
reimplementing or re-binding all of it.

**b) Local inference is a Python-first ecosystem.** The Ollama, vLLM and llama.cpp
server ecosystems are Python-native. The product's most attractive cost story — "run
a 70B on your own hardware" — is much easier to support when the backend can import
the same tooling the user runs locally.

**c) The frontend is not on the critical path.** The API is consumed by a Next.js app
that can be typed separately via generated OpenAPI clients. Sharing types across the
wire boundary is nearly as good as sharing them in-process.

**Where this decision could be wrong:** if the product grows to be primarily
real-time collaborative editing with heavy client-side state, a TypeScript backend
would win. That is not this product.

**Cost of the decision:** one extra serialization boundary. Mitigated by generating
TypeScript clients from the OpenAPI schema.

---

## 2. The provider layer: one protocol, two adapters

**Decision: define our own `LLMProvider` protocol (≈120 lines). Support OpenAI-compatible
endpoints with a single adapter, plus a native Anthropic adapter.**

This is the single highest-leverage decision in the codebase, so it is worth being
explicit about why we did *not* use LangChain or LlamaIndex.

**Why not LangChain?** The generation pipeline is a *known, fixed* DAG. We know
exactly what happens: plan → write → sync → verify → export. LangChain's value
proposition is composing arbitrary chains of tools and agents — precisely the part we
do not want. Adopting it would mean:

- a large transitive dependency surface (`langchain-core`, `langchain-community`,
  `pydantic` v1 shims, `tenacity`, `jsonpatch`, …);
- an abstraction layer between us and `httpx`, making timeouts and retries our
  problem anyway;
- a serialization format for prompts that we would then have to escape when calling a
  provider directly.

We already depend on `pydantic`, `httpx` and `PyYAML`. The whole provider layer is
`providers/base.py` + two adapters, and it is fully unit-testable without a network.

**Why not LlamaIndex?** It is a retrieval library. Our retrieval is SQL against a
schema we designed. LlamaIndex would want a vector store we have deliberately
decided not to use (see §4).

**What the OpenAI-compatible adapter buys us.** The `/v1/chat/completions` dialect is
the closest thing the industry has to a standard, and it is spoken by:

| Provider | Endpoint | Notes |
|---|---|---|
| OpenAI | `api.openai.com/v1` | |
| Anthropic via gateway | varies | |
| Groq | `api.groq.com/openai/v1` | Very fast; good price/perf for prose |
| OpenRouter | `openrouter.ai/api/v1` | Aggregator; useful as a fallback |
| DeepSeek | `api.deepseek.com/v1` | Cheap, strong at fiction |
| Mistral, xAI, Together, Fireworks, NVIDIA NIM | | |
| **Ollama** | `localhost:11434/v1` | **Local, free, no key** |
| **LM Studio** | `localhost:1234/v1` | |
| **vLLM / llama.cpp / TGI** | `localhost:8000/v1` | |

That is one adapter and a URL, versus ten bespoke SDKs with ten dependency trees and
ten sets of behavioural quirks. The adapter is ~200 lines.

**Why a separate Anthropic adapter?** Two reasons: the wire format genuinely differs
(the system prompt is a top-level field, not a message), and Anthropic's prompt
caching is worth exposing explicitly rather than losing behind a generic shim.

**Cost of the decision:** providers with non-standard features (Anthropic's cache
control, Gemini's safety settings) need explicit support rather than arriving free.

---

## 3. Model routing: roles, not one model

**Decision: every model call declares a *role* — `PLAN`, `PROSE`, `SUMMARY`, `JUDGE` —
and the registry resolves the role to a concrete model.**

A single "the model" setting is wrong for this workload because the cost profile is
extremely skewed:

- **Prose** is ~95% of the token bill. It gets the expensive model.
- **Planning** is maybe 3% of tokens but the highest-leverage 3%: a bad outline
  produces an expensive bad book.
- **Summarising** (chapter sync, rollups) is mechanical and cheap.
- **Judging** (continuity) needs reasoning but not creative quality, and benefits
  from a low temperature.

`providers/registry.py` holds one table. Adding a model is a data change:

```python
ModelSpec("claude-sonnet-4-5", "anthropic", 200_000, 3.00, 15.00,
          role_affinity=("plan", "prose", "judge"))
```

This is also what makes a *cheap* configuration possible: pin a 7B local model for
`summary` and `judge`, and only `prose` goes to a frontier model. Most books spend
most of their money on prose, so most of the saving comes from routing prose to a
mid-tier model — but that is a quality decision, not an engineering one, and the
registry lets us A/B it.

**Cost of the decision:** a layer of indirection between callers and models. Mitigated
because `Engine.model_for(role)` is one function and the resolved model is recorded
in the cost ledger for every call.

---

## 4. State: SQLite, and deliberately **no vector database**

This is the decision most likely to be argued with, so here is the reasoning in full.

### The problem

To write chapter 40 consistently, the engine needs to know what happened in chapter
3. The naive approaches:

**(a) Put the whole manuscript in the prompt.** Fails immediately: a 90k-word novel
is ~120k tokens, and no context window comfortably holds it plus the prompt plus the
output.

**(b) Summarize the manuscript and put the summary in the prompt.** This is the
standard trick, and it works — until it doesn't. Summaries lose exactly the details
that matter for continuity: the precise colour of a character's eyes, whether a wound
has healed, who was in the room when a thing was said, whether a promise was made.

**(c) Use a vector store and retrieve "relevant" chunks by similarity.** This is what
most RAG-over-novels systems do. It fails in a specific, reproducible way.
Embeddings encode *topical* similarity. They do not encode *truth*. A passage about
"Mara's left eye" will retrieve alongside "Mara's right eye" with high similarity,
because they are lexically and semantically near-identical. And a sentence stating
"the salt road is impassable" retrieves alongside "the salt road is passable".

Semantic similarity is the wrong instrument for state. It has no notion of
"contradicts".

### The decision

Store the narrative state as **relational rows** and retrieve with SQL:

```sql
SELECT summary FROM events
 WHERE project_id = ? AND chapter <= ? AND irreversible = 1
 ORDER BY chapter;
```

```sql
SELECT title FROM threads
 WHERE project_id = ? AND status IN ('open','advancing');
```

The Bible is stored twice on purpose: as one canonical JSON document (lossless,
versioned, trivially exported, what gets checkpointed) *and* as indexed rows (so
retrieval is a query, not a scan). See `core/bible.py`.

The pack assembled for each scene is a few hundred to a couple of thousand tokens —
small enough to include in every prompt without the bill exploding, which is the
actual constraint that makes long-form generation affordable.

### When a vector store *would* be right

If the product needs "find me every scene where Mara and the salt road co-occur",
across 30 books, for an author trying to reuse a motif — that is a similarity
problem, and a vector index over chapter summaries is the right tool. That is a
*search* feature layered on top, not the continuity substrate. `sentence-transformers`
is already an optional extra in `pyproject.toml` for exactly this.

**Cost of the decision:** we must maintain a schema, and a model that invents a new
kind of fact needs a new column. That is a real tax, paid in exchange for
correctness. The mitigation is the `Bible.validate()` method, which checks
referential integrity across characters, threads and events on every write.

---

## 5. Orchestration: a hand-written resumable DAG

**Decision: `graph/pipeline.py` is ~250 lines of explicit async code.**

Steps are functions. State transitions are explicit. Every step persists its output,
so an interrupted run resumes where it stopped.

**Why not LangGraph?** LangGraph is genuinely good — at *agentic* graphs with cycles,
conditional branching decided at runtime, and human-in-the-loop interrupts. Our graph
is a straight line with a loop over chapters. Adopting it would mean modelling our
pipeline in someone else's node/edge vocabulary, inheriting their checkpoint format,
and depending on their execution model, in exchange for features we do not use.

**Why not Celery / RQ / arq?** For the first production shape, an in-process asyncio
worker with a persisted job table is enough, and it removes Redis from the
critical path. `docs/DEPLOYMENT.md` documents the upgrade path to a real queue when
generation volume justifies it.

**Cost of the decision:** we hand-roll progress reporting, cancellation and retries.
These are ~150 lines and are exactly the code most likely to have subtle bugs. The
mitigation is that the pipeline is small enough to read in one sitting, and that the
persistence layer means a bug in progress reporting cannot corrupt a book.

---

## 6. Export: hand-written EPUB writer

**Decision: write the EPUB by hand (`core/assembler.py`), use `python-docx` for DOCX.**

Amazon KDP's EPUB validator is strict about things that generic ZIP helpers do not
care about:

- `mimetype` must be the **first** entry in the archive;
- it must be **stored** (uncompressed), not deflated;
- `META-INF/container.xml` must point at a valid OPF package;
- an EPUB 3 navigation document must exist and be declared with `properties="nav"`.

Libraries like `ebooklib` get these wrong often enough that you find out at upload
time, when you have a queue of books waiting. Writing the archive by hand is ~150
lines and removes a dependency and a class of support tickets.

DOCX is different — `python-docx` is stable, and the formatting requirements
(6×9 trim, margins, chapter page breaks) are a few API calls. KDP's DOCX path is
also more forgiving.

**Cost of the decision:** we own EPUB conformance testing. Mitigated by
`tests/test_assembler.py`, which asserts the mimetype-first ordering and the stored
compression method — the two rules that are easiest to break silently.

---

## 7. Configuration: YAML + environment

**Decision: `config/default.yaml` for settings, environment variables for secrets.**

The rule that matters: **the config file never contains a secret.** It contains the
*name* of the environment variable holding the key (`OPENAI_API_KEY`). This makes
`config/default.yaml` safe to commit, and it means a deployment is configured by
environment, which is what every hosting platform expects.

Precedence: constructor arguments > environment > YAML > defaults.

**Cost of the decision:** two sources of truth. Mitigated by documenting the
precedence order in `core/config.py` and keeping the override list short.

---

## 8. Validation: pydantic v2

**Decision: every domain object is a pydantic model.**

The Story Bible *is* the product. If a character's ID is dangling, or a thread is
marked resolved without a `resolved_in` chapter, the book is broken in a way that
shows up 30,000 words later. `StoryBible.validate()` runs referential-integrity
checks on every save, and the API rejects invalid projects with a 422 listing every
problem at once rather than one per round-trip.

**Cost of the decision:** pydantic v2's validator machinery is not free to learn, and
its serialization performance is worse than dataclasses for very large payloads. At
our scale (a few thousand records per book) that is irrelevant.

---

## 9. Testing strategy

| Layer | What is tested | How |
|---|---|---|
| Unit | Bible retrieval, continuity packing, voice metrics, tension shaping | Pure functions, no network |
| Contract | EPUB structure, DOCX output, provider request shapes | Assert on bytes and JSON |
| Continuity eval | Does the engine keep the book consistent? | A golden-corpus suite — see `docs/EVALS.md` |
| End-to-end | Full pipeline against a recorded fake provider | Deterministic replay, no API cost |

The end-to-end tests run against a **fake provider** that returns recorded responses.
That keeps CI free, deterministic and fast, while a separate, opt-in suite runs the
real models and is the only thing that can tell you whether the *writing* is good.

---

## 10. Deliberately not used

| Not used | Why |
|---|---|
| LangChain / LangGraph | Indirection over a fixed, known DAG. See §5. |
| LlamaIndex | A retrieval library for a problem we solved with SQL. See §4. |
| Vector database | Wrong instrument for state. See §4. |
| Celery / Redis | Not needed at current volume; documented upgrade path. See §5. |
| LangSmith / Langfuse | Useful later for tracing; adds a vendor dependency and a network hop. We already keep our own cost ledger. |
| Pydantic-AI / instructor | Nice ergonomics, but our structured outputs are few (outline, scenes, sync) and the pattern is ~30 lines with `json_mode`. |
| Docker / Kubernetes | Premature. See `docs/DEPLOYMENT.md`. |
| MongoDB | We want relational constraints. |

---

## 11. Dependency policy

**Core runtime dependencies: five.**

```
pydantic   httpx   PyYAML   python-docx   cairosvg
```

That is the whole tree for the engine. `fastapi`/`uvicorn` are an extra for the API
server; `pytest`/`ruff`/`mypy` are dev extras.

The reason for keeping this tight is not asceticism. This project will be maintained
by one or two people, and the dominant risk is not "we cannot use a library" — it is
"an upgrade breaks something in a pipeline that takes 40 minutes to run and costs real
money". A small dependency tree means the blast radius of a bad release is small and
the debugging loop is short.

`cairosvg` is the only dependency with a system-level requirement (libcairo); the
cover generator degrades gracefully to writing SVG if it is missing.

---

## 12. The stack, end to end

```
                  ┌──────────────────────────────┐
   author brief → │  FastAPI  /projects/{slug}   │
                  └──────────────┬───────────────┘
                                 │
                  ┌──────────────▼───────────────┐
                  │  Pipeline (resumable DAG)     │
                  │  plan → write → verify → export│
                  └──────────────┬───────────────┘
                                 │
        ┌────────────────────────┼────────────────────────┐
        ▼                        ▼                        ▼
┌───────────────┐      ┌──────────────────┐      ┌────────────────┐
│ Story Bible   │◄─────┤  Writer          │─────►│ LLM provider   │
│ (SQLite)      │ sync │  + continuity    │      │ (OpenAI-compat │
│ structured    │      │  + voice         │      │  / Anthropic)  │
│ state         │      └──────────────────┘      └────────────────┘
└───────────────┘
        │
        ▼
┌───────────────┐      ┌────────────────┐      ┌────────────────┐
│ EPUB / DOCX / │      │ cover (SVG→PNG)│      │ rights cert +  │
│ TXT + KDP meta│      │                │      │ audit log      │
└───────────────┘      └────────────────┘      └────────────────┘
```
