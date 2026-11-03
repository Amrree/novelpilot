"""Continuity pack assembly and contradiction detection."""

from __future__ import annotations

from novelpilot.core.continuity import build_continuity_pack, deterministic_checks
from novelpilot.core.models import ChapterDraft, SceneCard, StoryBible


def test_pack_contains_pov_physical_details(bible: StoryBible, scene: SceneCard) -> None:
    pack = build_continuity_pack(bible, scene)
    assert "grey" in pack.text
    assert "Mara" in pack.text


def test_pack_contains_location_sensory_detail(bible: StoryBible, scene: SceneCard) -> None:
    pack = build_continuity_pack(bible, scene)
    assert "Salt Road" in pack.text
    assert "crunches" in pack.text


def test_pack_contains_banned_phrases(bible: StoryBible, scene: SceneCard) -> None:
    pack = build_continuity_pack(bible, scene)
    assert "delve into" in pack.text


def test_pack_contains_earlier_irreversible_facts(bible: StoryBible, scene: SceneCard) -> None:
    pack = build_continuity_pack(bible, scene)
    assert "left hand was broken" in pack.text
    assert "IRREVERSIBLE" in pack.text


def test_pack_does_not_leak_future_state(bible: StoryBible, scene: SceneCard) -> None:
    """The pack is built for chapter 3, so chapter-4+ facts must not appear."""
    from novelpilot.core.models import FactualEvent

    bible.events.append(FactualEvent(chapter=9, summary="Mara died in the desert."))
    pack = build_continuity_pack(bible, scene)
    assert "Mara died" not in pack.text


def test_pack_includes_open_threads_for_present_characters(bible: StoryBible,
                                                          scene: SceneCard) -> None:
    from novelpilot.core.models import PlotThread

    bible.threads = [PlotThread(id="t1", title="The forged message", stakes="critical",
                                related_characters=["c-mara"])]
    pack = build_continuity_pack(bible, scene)
    assert "The forged message" in pack.text
    assert "critical" in pack.text


def test_pack_excludes_threads_for_absent_characters(bible: StoryBible,
                                                     scene: SceneCard) -> None:
    from novelpilot.core.models import PlotThread

    bible.threads = [PlotThread(id="t1", title="Unrelated subplot",
                                related_characters=["c-someone-else"])]
    pack = build_continuity_pack(bible, scene)
    assert "Unrelated subplot" not in pack.text


def test_pack_states_pov_knowledge_boundary(bible: StoryBible, scene: SceneCard) -> None:
    pack = build_continuity_pack(bible, scene)
    assert "The message is a forgery" in pack.text


# --------------------------------------------------------------------------
# Deterministic checks
# --------------------------------------------------------------------------


def test_detects_banned_phrase(bible: StoryBible, scene: SceneCard) -> None:
    draft = ChapterDraft(chapter=3, text="She had to delve into the salt.", word_count=8)
    issues = deterministic_checks(bible, draft, scene)
    assert any(i.kind == "banned_phrase" for i in issues)


def test_detects_missing_required_element(bible: StoryBible, scene: SceneCard) -> None:
    draft = ChapterDraft(chapter=3, text="They walked on.", word_count=4)
    issues = deterministic_checks(bible, draft, scene)
    assert any(i.kind == "missing_beat" for i in issues)


def test_detects_unknown_character_name(bible: StoryBible, scene: SceneCard) -> None:
    # Mid-sentence capitals are the signal; sentence-initial ones are ignored.
    draft = ChapterDraft(chapter=3, text="She saw Bartholomew at the gate.", word_count=8)
    issues = deterministic_checks(bible, draft, scene)
    assert any(i.kind == "unknown_character" for i in issues)


def test_ignores_sentence_initial_capitals(bible: StoryBible, scene: SceneCard) -> None:
    """A capitalised first word could be a name or just a word -- do not guess."""
    draft = ChapterDraft(chapter=3, text="Bartholomew waited by the gate.", word_count=6)
    issues = deterministic_checks(bible, draft, scene)
    assert not [i for i in issues if i.kind == "unknown_character"]


def test_no_false_positive_on_known_names(bible: StoryBible, scene: SceneCard) -> None:
    draft = ChapterDraft(chapter=3, text="Mara and Elias walked on.", word_count=6)
    issues = deterministic_checks(bible, draft, scene)
    assert not [i for i in issues if i.kind == "unknown_character"]


def test_flags_chapter_that_is_far_too_short(bible: StoryBible, scene: SceneCard) -> None:
    draft = ChapterDraft(chapter=3, text="Short.", word_count=1)
    issues = deterministic_checks(bible, draft, scene)
    assert any(i.kind == "too_short" for i in issues)
