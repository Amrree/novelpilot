# Cost Model

What it actually costs to generate a book, where the money goes, and how to
make it cheaper without making it worse.

> Pricing in this document is **indicative**. Provider prices change. Check
> `src/novelpilot/providers/registry.py` against each provider's live pricing
> page before quoting a number to a customer. The registry is the source of
> truth for the engine; this document explains how those numbers are used.

---

## The shape of the cost

For an **80,000-word novel** (32 chapters × 2,500 words, 3 scenes per chapter,
≈ 1,200 words per scene → **~67 scenes**):

### Input tokens

| Component | Per call | Calls | Total |
|---|---:|---:|---:|
| Continuity pack | ~3,000 | 67 | ~201,000 |
| Scene card | ~400 | 67 | ~27,000 |
| Style directives + instructions | ~700 | 67 | ~47,000 |
| Planning prompts | ~3,000 | ~12 | ~36,000 |
| Chapter sync | ~5,000 | 32 | ~160,000 |
| Continuity judge | ~4,000 | 32 | ~128,000 |
| | | | **≈ 600,000 input** |

### Output tokens

| Component | Per call | Calls | Total |
|---|---:|---:|---:|
| Prose | ~1,600 | 67 | ~108,000 |
| Outline | ~4,000 | 1 | ~4,000 |
| Chapter plans | ~2,500 | 12 | ~30,000 |
| Chapter sync | ~800 | 32 | ~26,000 |
| Judge | ~500 | 32 | ~16,000 |
| | | | **≈ 184,000 output** |

### The ratio that matters

**Input is ~3× output.**

This is unusual for a generation product and it is a direct consequence of the
architecture. The continuity pack is re-sent on every scene, because it is
recomputed from state rather than cached from prose.

That is a deliberate trade:

- **Cost of re-sending:** ~600k input tokens per book.
- **Cost of the alternative** (caching prose context, or using a vector store):
  a much larger context per call, worse coherence, and — for a vector store —
  a dependency and an infrastructure bill.

600k input tokens is affordable. A 90k-token context window on every one of 67
calls is not, and would not fix the coherence problem.

---

## Cost per book

Using the indicative prices in the registry:

| Configuration | Prose model | Input | Output | **Per book** |
|---|---|---:|---:|---:|
| Frontier | claude-sonnet-4-5 ($3/$15) | ~600k | ~184k | **≈ $3.60** |
| Balanced | gpt-4o ($2.50/$10) | ~600k | ~184k | **≈ $2.55** |
| Mid-tier | deepseek-chat ($0.27/$1.10) | ~600k | ~184k | **≈ $0.31** |
| Fast | llama-3.3-70b on Groq ($0.59/$0.79) | ~600k | ~184k | **≈ $0.50** |
| Local | llama3.1:70b on Ollama | 600k | 184k | **≈ $0.00** |

A frontier-configured book lands around **$3.60**, comfortably inside the
default `max_cost_per_book_usd: 5.00` guard.

### Worked example — frontier configuration

| Step | Model | Input | Output | Cost |
|---|---|---:|---:|---:|
| Prose (67 scenes) | claude-sonnet-4-5 | 275,000 | 108,000 | $2.44 |
| Planning | claude-sonnet-4-5 | 36,000 | 34,000 | $0.62 |
| Chapter sync | gpt-4o-mini | 160,000 | 26,000 | $0.04 |
| Continuity judge | gpt-4o-mini | 128,000 | 16,000 | $0.03 |
| | | | | **≈ $3.13** |

The two cheap steps do 45% of the calls but cost 2% of the money. **That is
exactly why the engine routes by role.**

---

## The three levers

### 1. Role routing (biggest, free)

Put a small model on `summary` and `judge` and an expensive one on `prose`.
Because prose is ~85% of the output tokens, this is where nearly all the
saving is — but it is also the only lever that affects quality, so it needs
measuring (see [EVALS.md](EVALS.md)).

```yaml
models:
  plan: claude-sonnet-4-5
  prose: claude-sonnet-4-5
  summary: gpt-4o-mini     # mechanical work
  judge: gpt-4o-mini       # pattern matching
```

### 2. Reduce the pack (medium, needs care)

The pack is ~3,000 tokens per scene. Levers:

- `RECENT_CHAPTER_WINDOW` — fewer recent chapters summarised verbatim.
- `MAX_LEGACY_FACTS` — fewer older facts carried forward.
- `PACK_CHAR_BUDGET` — the hard cap.

**The trap:** cutting the pack makes the book cheaper and *worse*. The pack is
the mechanism. If the pack gets thin, the model starts inventing things, and
the cost moves from generation to repair (re-running chapters) — which is more
expensive than what you saved.

The honest rule: **shrink the pack only after the book is provably coherent.**
Optimise cost second, quality first.

### 3. Cheaper prose models (large, risky)

Dropping prose from a frontier model to a cheap one cuts cost by ~90%. It also
changes the output's quality in ways that are hard to predict without
measuring — cheap models tend to produce flatter prose, more repetition, and
more reliable violations of the style directives.

The right way to evaluate this is the eval harness in
[EVALS.md](EVALS.md), not intuition.

---

## Guard rails

### Per-book budget

```yaml
max_cost_per_book_usd: 5.00
```

`Engine._check_budget()` refuses to start a new model call once the project has
spent this much. It is a hard stop, not a warning — the alternative is a
runaway loop on a book with a pathological plan.

Set it deliberately. A 120,000-word epic with 5 scenes per chapter needs more
headroom than a 40,000-word novella.

### Daily budget

```yaml
daily_token_budget: 5000000
```

A platform-level guard. Not enforced by the engine yet — it needs a
shared/central counter rather than a per-process one, which means a database
or Redis. Tracked in `ROADMAP.md`.

### Cost tracking

Every model call is recorded in a `costs` table with the step name, model,
prompt tokens, completion tokens and computed USD. `novelpilot cost <slug>`
prints the breakdown; `/projects/{slug}/cost` returns it as JSON.

Unknown models (anything not in the registry) are tracked for **tokens but not
for dollars** — they cost `$0.00` in the ledger, which is the correct
behaviour for a model we have no price for, but worth knowing if you are
comparing runs.

---

## Scaling economics

A fiction-generation service is unusual in that the revenue per book is
bounded by the *selling* of the book, not by the number of books generated.

A rough model at the frontier configuration:

| Quantity | Value |
|---|---|
| Cost per book | $3.13 |
| Cost per 100 books | $313 |
| Cost per 1,000 books | $3,130 |

At the mid-tier configuration ($0.31/book) that drops to **$310 per 1,000
books**.

This is why the product needs to be sold on the *outcome* — a finished,
publishable, coherent manuscript — rather than on generation volume. The
margin lives in the author's time saved, not in the tokens.

---

## What the audit log is for

Every book emits `generation-audit-log.json` and a Content Rights Certificate.
They are not decoration:

- The certificate is the artefact an author shows a publisher or a royalty
  auditor.
- The log is the artefact *we* use to answer "what did this book cost, and
  why".
- Together they make the generation auditable, which is what turns "an AI
  wrote this" into "an author directed this, and here is the record".

---

## Summary

- One frontier-configured 80k-word book: **≈ $3.13**.
- Role routing is the free lever; put small models on `summary` and `judge`.
- The pack is the expensive part and the quality part — do not cut it before
  the book is coherent.
- The budget guards are hard stops. Raise them deliberately.
- Measure quality before chasing cost. See [EVALS.md](EVALS.md).
