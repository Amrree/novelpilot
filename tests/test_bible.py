"""Story Bible persistence, retrieval and integrity."""

from __future__ import annotations

import pytest

from novelpilot.core.bible import BibleStore
from novelpilot.core.models import StoryBible, ThreadStatus


def test_create_and_load_roundtrip(store: BibleStore, bible: StoryBible) -> None:
    store.create_project("salt-road", "The Salt Road")
    store.save_bible("salt-road", bible)
    loaded = store.load_bible("salt-road")
    assert loaded.title == "The Salt Road"
    assert len(loaded.characters) == 2
    assert loaded.characters[0].name == "Mara"
    assert loaded.characters[0].physical["eyes"] == "grey"


def test_revision_increments_on_save(store: BibleStore, bible: StoryBible) -> None:
    store.create_project("salt-road", "The Salt Road")
    store.save_bible("salt-road", bible)
    assert store.bible_revision("salt-road") == 1
    store.save_bible("salt-road", bible)
    assert store.bible_revision("salt-road") == 2


def test_open_threads_only_returns_unresolved(store: BibleStore, bible: StoryBible) -> None:
    from novelpilot.core.models import PlotThread

    bible.threads = [
        PlotThread(id="t1", title="Open one", status=ThreadStatus.OPEN),
        PlotThread(id="t2", title="Resolved one", status=ThreadStatus.RESOLVED, resolved_in=4),
    ]
    store.create_project("salt-road", "The Salt Road")
    store.save_bible("salt-road", bible)
    open_threads = store.open_threads("salt-road")
    assert [t.title for t in open_threads] == ["Open one"]


def test_facts_through_is_chapter_scoped(store: BibleStore, bible: StoryBible) -> None:
    store.create_project("salt-road", "The Salt Road")
    store.save_bible("salt-road", bible)
    assert len(store.facts_through("salt-road", 1)) == 1
    assert len(store.facts_through("salt-road", 2)) == 2
    assert len(store.facts_through("salt-road", 99)) == 2


def test_irreversible_facts_are_marked(store: BibleStore, bible: StoryBible) -> None:
    store.create_project("salt-road", "The Salt Road")
    store.save_bible("salt-road", bible)
    facts = store.facts_through("salt-road", 2)
    assert any(f.irreversible for f in facts)


def test_drafts_roundtrip(store: BibleStore) -> None:
    store.create_project("salt-road", "The Salt Road")
    store.save_draft("salt-road", 1, "Chapter One", "The road was white.")
    assert store.load_draft("salt-road", 1) == "The road was white."
    assert store.load_all_drafts("salt-road") == {1: "The road was white."}


def test_cost_ledger_accumulates(store: BibleStore) -> None:
    store.create_project("salt-road", "The Salt Road")
    store.record_cost("salt-road", "prose", "gpt-4o", 1000, 500, 0.25)
    store.record_cost("salt-road", "prose", "gpt-4o", 1000, 500, 0.25)
    store.record_cost("salt-road", "plan", "gpt-4o-mini", 200, 100, 0.01)
    assert store.total_cost("salt-road") == pytest.approx(0.51)
    breakdown = store.cost_breakdown("salt-road")
    assert len(breakdown) == 2
    assert breakdown[0]["step"] == "prose"
    assert breakdown[0]["prompt_tokens"] == 2000


def test_unknown_project_raises(store: BibleStore) -> None:
    with pytest.raises(KeyError):
        store.load_bible("does-not-exist")


# --------------------------------------------------------------------------
# Integrity
# --------------------------------------------------------------------------


def test_validate_catches_dangling_relationship() -> None:
    b = StoryBible()
    from novelpilot.core.models import Character

    b.characters = [Character(name="Mara", relationships={"nobody": "friend"})]
    problems = b.validate()
    assert any("unknown character" in p for p in problems)


def test_validate_catches_resolved_thread_without_chapter() -> None:
    from novelpilot.core.models import PlotThread

    b = StoryBible()
    b.threads = [PlotThread(title="T", status=ThreadStatus.RESOLVED)]
    problems = b.validate()
    assert any("resolved without resolved_in" in p for p in problems)


def test_validate_catches_duplicate_names() -> None:
    from novelpilot.core.models import Character

    b = StoryBible()
    b.characters = [Character(name="Mara"), Character(name="Mara")]
    assert any("duplicate character names" in p for p in b.validate())


def test_validate_accepts_a_sane_bible(bible: StoryBible) -> None:
    assert bible.validate() == []
