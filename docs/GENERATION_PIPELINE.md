# The Generation Pipeline

Stage by stage: what happens, why, and what the model is actually asked for.

The prompts quoted here are the real ones from `core/planner.py`,
`core/writer.py` and `core/continuity.py`, lightly trimmed for length.

---

## Stage 0 — The brief

The user supplies a premise and, optionally, a cast. The system builds a
`StoryBible` and validates it.

**What gets validated:** duplicate character names, relationships pointing at
nobody, resolved threads with no payoff chapter, events referencing unknown
characters. This is a 422, not a warning. A broken Bible produces a broken
book 30,000 words later, and finding out then is expensive.

**Why the cast is explicit:** the planner invents characters when none are
given, and invented characters are shallow. If the user supplies Mara, Mara has
grey eyes for the whole book.

---

## Stage 1 — Outline (`role=PLAN`)

The model is asked for a three-act structure with beats.

```
Produce a commercial, publishable three-act structure.

Rules:
- Act I is roughly 25% of the book, Act II 50%, Act III 25%.
- The midpoint must contain a real reversal, not just a complication.
- Every act needs at least one turn that changes the protagonist's situation.
- Tension must vary across the book: it should dip around the start of Act II
  and peak in Act III. Do not produce a flat tension line.
- The ending must resolve the primary thread. Do not leave the central
  conflict dangling.
```

### Tension shaping

Local models (and frontier models on a lazy day) return a flat tension line —
everything at 0.5. `_normalize_tension()` blends the model's own values with a
structural shape:

```
position < 25%   →  ramp 0.15 → 0.35
position < 50%   →  dip  to 0.30 at the midpoint
position < 80%   →  climb to 0.78
position ≥ 80%   →  peak at 1.0
```

Blended as `0.35 × model_value + 0.65 × shape`. The model still chooses *where*
the tension goes; the shape guarantees the overall curve reads like a novel.
The model gets the smaller weight deliberately — the structural guarantee is
worth more than the model's opinion about tension, which is unreliable.

---

## Stage 2 — Chapter and scene plan (`role=PLAN`)

The outline becomes **scene cards**. This is where planning stops being
narrative and becomes executable.

```
A scene card must be a complete, self-contained instruction for one scene:
  - pov: the character whose eyes we read from (use exact names from the cast)
  - location: where the scene happens
  - participants: who is present
  - goal: what the POV wants in this scene
  - conflict: what resists that goal
  - outcome: what changes by the end of the scene (state it concretely)
  - must_include: concrete beats or details that MUST appear
  - must_avoid: things that must NOT happen
  - threads: plot threads this scene touches
  - words: target word count for the scene
```

Names are resolved against the Bible at parse time. A scene naming a character
who does not exist gets that participant **dropped** rather than silently
inventing a stranger, and the scene's POV falls back to the chapter's POV.

### Planning in batches

Planning is done for a range of chapters at a time rather than all at once,
so the plan can be revised between sections and so a single enormous JSON
response is not required.

---

## Stage 3 — Continuity pack assembly (no LLM)

Before any prose is written, the engine assembles exactly what the scene needs
to know. This is pure SQL and string formatting — no model call.

```
## NON-NEGOTIABLE RULES
- [style directives]
- Never use the phrase: "delve into"
- Narration point of view: third person past.
- Use past tense throughout.

## POV CHARACTER
Mara (protagonist) | age 34 | she | eyes: grey; hair: black, cropped |
  goal: Deliver the message | voice: diction=clipped; rhythm=staccato

## OTHER CHARACTERS PRESENT
- Elias (deuteragonist) | eyes: brown
  Relationship to POV: former apprentice

## LOCATION
The Salt Road: A white causeway across a dead sea.
  Signature detail: Salt crust crunches underfoot.

## ACTIVE PLOT THREADS
- [critical stakes] The forged message: Mara's commission is a forgery
  (payoff target: revealed at the border gate)

## RECENT CHAPTERS (must be consistent with these)
- Ch2 (Mara POV): Mara accepted the contract.
- Ch2: Mara's left hand was broken. [IRREVERSIBLE - must not contradict]

## EARLIER FACTS
- Ch1: Mara took the salt road at dawn. [IRREVERSIBLE - must not contradict]

## WHAT THE POV KNOWS (the POV must not know anything else yet)
- The message is a forgery
```

### The size constraint is the whole trick

The pack is capped at ~12,000 characters (≈3,000 tokens). That cap is not a
technical limitation — it is the reason the product is affordable. If the pack
were the entire manuscript, a 90k-word novel would cost more than it is worth
to generate. Because the pack is small and *relevant*, every scene prompt
costs about the same whether it is chapter 2 or chapter 32.

### Facts and summaries are complementary

The pack includes both:

- **Chapter summaries** — "Ch2 (Mara POV): Mara accepted the contract." What the
  chapter was *about*.
- **Factual events** — "Ch2: Mara's left hand was broken." What actually
  *happened*.

A summary alone loses the details that matter. A fact alone loses the shape.
Shipping only summaries was a real bug caught by the test suite; the pack now
carries both, with recent events (the last `RECENT_CHAPTER_WINDOW` chapters)
in the fine-grained section and older events compacted into bullets.

### Anti-leak

The POV's knowledge list is stated explicitly, and the prompt says the POV must
not know anything else *yet*. This catches the failure that matters most in
long-form fiction: a character reacting to information they could not have.

---

## Stage 4 — Writing the scene (`role=PROSE`)

```
You are writing one scene of a {genre} novel.

=== SCENE CARD ===
Chapter {chapter}, scene {scene_index}
...

=== STORY STATE (authoritative -- never contradict it) ===
{continuity_pack}

=== STYLE DIRECTIVES (follow exactly) ===
{style_directives}

=== TASK ===
Write the scene. Target approximately {target_words} words.

Requirements:
- Write ONLY the scene. No chapter number, no title, no author notes.
- Open in the middle of the action or tension. No throat-clearing.
- The outcome stated above must be visible in the prose by the end.
- Every physical detail of a character must match the STORY STATE.
- Show the conflict; do not narrate that a conflict happened.
- Use dialogue to reveal character and advance the scene.
- Never use the banned phrases.
```

### Style directives are numbers

`core/voice.py` measures a reference sample and converts it into directives:

```
Average sentence length: ~12 words, but vary it -- include sentences from
8 to 16 words.
Aim for 3.2 sentences per paragraph on average; deliberately vary paragraph
length.
Em-dash density target: about 1.0 per 1000 words. Do not overuse dashes.
Dialogue should be roughly 35% of the text for conversation-led scenes.
Vary sentence openings. Do not begin consecutive sentences with the same word.
```

Models follow numbers far more reliably than they follow "write like this
author". The measurements come from `StyleMetrics.measure()`, which computes
sentence-length mean *and standard deviation*, paragraph length, em-dash
density, dialogue ratio and AI-tell vocabulary hits.

### Output cleanup

Models sometimes prefix the scene with a label or wrap it in markdown fences.
`_strip_preamble()` removes those. Without it, the first line of every chapter
reads `Here is the scene:` — which then appears in the EPUB.

---

## Stage 5 — Bible sync (`role=SUMMARY`)

After the chapter is written, the model is asked what changed:

```
Return JSON only:
{
  "chapter_title": "...",
  "summary": "2-4 sentence summary...",
  "events": [{"summary": "...", "participants": [...], "irreversible": false}],
  "opened_threads": [...],
  "resolved_threads": [...],
  "character_updates": [{"name": "...", "learned": "...", "relationship_changes": [...]}],
  "emotional_valence": -1.0,
  "tension": 0.5
}
```

The response is applied to the Bible:

- `events` → new `FactualEvent` rows
- `opened_threads` → new `PlotThread` rows (status `open`, `introduced_in` = current chapter)
- `resolved_threads` → existing threads marked resolved
- `character_updates` → appended to each character's `knowledge`
- the summary → a `ChapterSummary` row

**This step is what makes chapter N+1 better than chapter N.** Without it,
the engine has no idea what just happened and the next scene starts from
nothing. With it, chapter 12 opens with the pack containing chapter 11's
events, threads and character knowledge.

A failed sync is caught and swallowed — the prose is never lost because a
JSON parse failed.

---

## Stage 6 — Verification (`role=JUDGE`)

Two halves.

### Deterministic (free, exact)

- Banned phrases present
- Required scene elements missing
- Capitalised names mid-sentence that are not in the Bible
- Chapter length far below target

The name check deliberately ignores sentence-initial capitals. A capitalised
word at the start of a sentence is indistinguishable from an ordinary word
without a lexicon, so flagging it would produce pure noise. Mid-sentence
capitals are a far stronger signal.

### LLM judge (semantic)

```
You are a strict continuity editor for a {genre} novel.

Report ONLY real contradictions between the written text and the story state
or plan. Do not report style opinions...

Contradictions include:
- A character having a physical trait they do not have (or losing one).
- A character knowing something they could not know yet.
- An event contradicting an earlier event.
- A required plot beat being absent.
- A resolved plot thread being treated as still open.
```

The judge runs at low temperature and returns structured issues. A judge
failure is caught and returns `[]` — a failed judge must never fail the
chapter.

---

## Stage 7 — Export

`core/assembler.py` produces:

| Output | Notes |
|---|---|
| `.epub` | Hand-written EPUB 3. `mimetype` first and stored; valid container, OPF and nav documents. |
| `.docx` | 6×9in trade paperback via `python-docx`, chapter page breaks, scene-break markers. |
| `.txt` | For reading on a phone. |
| `cover.png` | Deterministic typographic cover, 2565×2565 px (6×9 + 0.125in bleed @ 300dpi). |
| `content-rights-certificate.md` | Rights statement + generation record. |
| `generation-audit-log.json` | Machine-readable provenance: every model call, token count and cost. |

The certificate and the audit log are derived from the same cost ledger, so
they cannot drift apart.

---

## Failure modes and how they are handled

| Failure | Handling |
|---|---|
| Upstream 5xx / 429 | Retried with exponential backoff (3 attempts) in `Engine._complete` |
| Malformed JSON from the model | `LLMResponse.json()` salvages the outermost object/array, tolerates markdown fences |
| Model ignores JSON mode | Same salvage path; `parse` failure raises only after salvage |
| Chapter sync fails | Caught, logged, prose preserved |
| Continuity judge fails | Caught, returns no issues, chapter stands |
| Run interrupted | Every step persisted; `Pipeline.run()` resumes and skips written chapters |
| Budget exceeded | `Engine._check_budget` refuses the next call |
| Model returns empty text | Caught by the deterministic length check, flagged as `too_short` |

---

## Tuning knobs

| Config | Effect |
|---|---|
| `words_per_chapter` | 2,500 is a solid middle. 4,000 gives fewer, longer chapters — a very different book from the same target length. |
| `scenes_per_chapter` | 3 is the default. 2 gives longer scenes; 5 gives more frequent POV changes. |
| `models.prose` | The single biggest quality and cost lever. |
| `models.summary` | Cheap and mechanical — put a small model here. |
| `max_cost_per_book_usd` | A hard stop, not a warning. |
| `continuity_check` | Turn off for draft-quality output. |
