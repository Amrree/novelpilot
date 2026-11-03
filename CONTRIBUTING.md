# Contributing to NovelPilot

Thanks for looking. This document covers how to get set up, what the house
style is, and the one rule that matters most.

---

## The one rule

**A change to a prompt or to the continuity pack format is a behaviour change,
not a refactor.**

If you touch `core/planner.py`, `core/writer.py` or `core/continuity.py`, you
have changed what the engine produces. That needs:

1. An eval run against the corpus in `evals/` (see `docs/EVALS.md`).
2. A note in the PR describing what changed in the *output*, not just the code.
3. A minor version bump if the output changes noticeably.

This exists because the failure mode of this project is silent: a prompt edit
makes every book slightly worse and nothing errors.

---

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[server,dev]"
pre-commit install          # optional
```

Run the tests:

```bash
pytest -q
```

Run the linters:

```bash
ruff check src tests
ruff format src tests
mypy src
```

---

## House style

The codebase follows the conventions already established in
[BookOfShadows](https://github.com/Amrree/BookOfShadows) and
[pipecat-ollama](https://github.com/Amrree/pipecat-ollama):

- Python 3.11+, type hints on public functions.
- `core/` holds domain logic and must not import from `graph/`, `server/` or
  `plugins/`.
- `providers/` must not import from `core/`.
- `plugins/` are optional and discovered at runtime.
- Docstrings explain *why*, not *what*. The code already says what.
- Comments earn their place by explaining a decision, a gotcha, or a non-obvious
  constraint. Not restating the line below.

Line length is 100 (ruff config in `pyproject.toml`).

---

## Commit messages

Conventional-ish:

```
feat(writer): carry physical state into scene cards
fix(continuity): include recent events in the pack, not just summaries
docs(tech-stack): explain the no-vector-db decision
test(bible): cover revision increments
```

---

## Pull requests

1. **Tests pass.** `pytest -q` green.
2. **New behaviour has a test.** A bug fix gets a test that fails without the fix.
3. **Prompts are argued for.** If you change a prompt, say what it was doing
   wrong and why the new version is better.
4. **Docs updated.** If you changed an API, an architecture decision or a
   configuration key, the docs move with it.
5. **One concern per PR.** A prompt change bundled with a refactor is
   unreviewable.

---

## Adding a provider

1. Implement `LLMProvider` in `src/novelpilot/providers/`.
2. If it speaks the OpenAI chat-completions dialect, add it to `ENDPOINTS` and
   `KEY_ENV` in `openai_compat.py` — you probably do not need a new adapter.
3. Otherwise write an adapter and add it to `build_provider()`.
4. Add the model to `MODELS` in `registry.py` with its context window, pricing
   and role affinity.
5. Add a test asserting the request shape.

---

## Adding a plugin

Drop a module in `src/novelpilot/plugins/` exposing a module-level `plugin`
object with a `manifest` dict and an async `run()`:

```python
manifest = {"name": "myplugin", "version": "0.1.0", "description": "..."}

class MyPlugin:
    async def run(self, **kwargs): ...

plugin = MyPlugin()
```

`PluginManager.discover()` picks it up automatically.

---

## Style of prose in prompts

The prompts in `core/` are the product. When you edit one:

- Keep the instruction **concrete**. "Show the conflict; do not narrate that a
  conflict happened" works. "Write engagingly" does not.
- Prefer **numbers over adjectives** — "3.2 sentences per paragraph" beats
  "varied paragraph length".
- Keep the **negative constraints**. `must_avoid` is where physical-state
  continuity is enforced.
- Never let the writer see the raw outline. It sees a scene card.
