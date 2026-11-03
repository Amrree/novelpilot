# Evaluation

How to tell whether the output is actually good.

This is the hardest problem in the product and the one most likely to be
hand-waved. A book can be structurally perfect, technically correct, and still
unreadable. Worse, *the model cannot tell you* — asked to evaluate its own
prose, a language model will happily explain why it is good.

So evaluation has to be built deliberately, with three layers that do not
depend on the model's opinion.

---

## Layer 1 — Deterministic checks (free, always on)

These run on every chapter with no model call.

| Check | Catches |
|---|---|
| Banned phrase present | The exact word patterns the author banned |
| Required element missing | A scene beat the plan promised |
| Mid-sentence name not in the Bible | An invented character |
| Length far below target | A truncated or refused generation |
| Em-dash density over target | The mechanical AI-tell |
| AI-tell vocabulary | *delve*, *tapestry*, *poignant*, … |
| Sentence-length drift | Style collapse into uniformity |
| Open threads at completion | A central conflict that never resolved |

**These are assertions, not opinions.** They are exact and they are cheap, and
they catch a surprising share of real problems. `tests/test_continuity.py`
covers each one.

---

## Layer 2 — Model-judged continuity (`role=JUDGE`)

The judge is a *different* model from the writer, at low temperature, given
the story state and the prose, asked to report contradictions only.

```
Report ONLY real contradictions between the written text and the story state
or plan. Do not report style opinions, do not report suggestions for
improvement, and do not report things that are merely unspecified.
```

The prompt is deliberately narrow. A judge asked to "evaluate the chapter"
produces a list of complaints; a judge asked to find *contradictions* produces
a short, actionable list — or nothing, which is the correct answer most of
the time.

**Limitation, stated plainly:** the judge is still a language model. It can
miss contradictions and, less often, invent them. It is a filter, not proof.

---

## Layer 3 — The eval harness (what you actually need)

Layers 1 and 2 tell you the book is *consistent*. They do not tell you it is
*good*. For that you need a corpus and a rubric.

### The corpus

Maintain a set of briefs with known-good reference outcomes:

```
evals/
├── briefs/
│   ├── 01-romance-60k.json
│   ├── 02-thriller-80k.json
│   └── 03-cozy-mystery-40k.json
├── references/
│   └── 02-thriller-80k/
│       ├── human-edited-chapters/
│       └── notes.md
└── run.py
```

Ten briefs is enough to start. Twenty is enough to trust a change.

### The rubric

Score each generated book on five axes, 1–5:

| Axis | The question |
|---|---|
| **Coherence** | Does anything contradict anything else? |
| **Character integrity** | Are people consistent in voice, knowledge, and physical detail? |
| **Prose quality** | Does it read as written by a person, and not like a template? |
| **Structure** | Does it build, turn, and pay off? |
| **Readability** | Would you finish it? |

**The human-edited reference is the point.** Take a machine chapter, have a
human edit it into the chapter it should have been, and you now have a target.
Scoring "how close is the raw output to the edited version" is far more
reliable than scoring "is this good".

### The regression loop

```
1. Change something (a prompt, a model, a temperature).
2. Run the corpus.
3. Score against the rubric.
4. Compare to the baseline.
5. Keep it only if coherence held and prose did not regress.
```

This is the only way to make model and prompt changes safely. Without it, every
change is a gamble, and the failure mode — a book that is subtly worse —
produces no error message.

---

## What to measure on every run

These go in the cost/eval ledger automatically:

| Metric | Why |
|---|---|
| Cost per book | The business constraint |
| Tokens per scene | Efficiency of the pack |
| Continuity issues per chapter | The coherence trend over a book |
| Deterministic issues per chapter | Same, but exact |
| Words per scene vs target | Whether the model is hitting length |
| Open threads at completion | Whether the plot pays off |
| Repetition rate | Cheap proxy for "flat prose" |

**Repetition rate** deserves a note. It is computable without a model: the
ratio of unique n-grams (n=4) to total n-grams. A chapter whose 4-gram
diversity is below ~0.55 is repetitive. It is a rough signal, it is free, and
it correlates with the single most common complaint about AI prose.

---

## The trap to avoid

> "The judge said there were no issues, so the book is fine."

No. The judge confirms consistency, not quality. It cannot tell you the prose
is dull, the middle sags, or the ending is obvious. Those need a human and a
rubric.

The layering is the point: deterministic checks for exactness, the judge for
semantic consistency, and humans for whether the book is *worth reading*.
Only the first two can be automated.

---

## Status

| Layer | Status |
|---|---|
| Deterministic checks | **Implemented** — `core/continuity.py`, `core/voice.py` |
| Model-judged continuity | **Implemented** — `core/continuity.py::llm_judge` |
| Repetition metric | **Not implemented** — trivial to add, see below |
| Eval harness + corpus | **Not implemented** — next in the roadmap |
| Human-edited references | **Not started** — needs 3 books' worth of human editing |

### Adding the repetition metric

```python
def repetition_rate(text: str, n: int = 4) -> float:
    """Ratio of unique n-grams to total n-grams. Below ~0.55 reads repetitive."""
    words = re.findall(r"\b\w+\b", text.lower())
    if len(words) < n:
        return 1.0
    grams = [tuple(words[i:i + n]) for i in range(len(words) - n + 1)]
    return len(set(grams)) / len(grams)
```

Worth adding to `core/voice.py` next to `StyleMetrics.measure()`.
