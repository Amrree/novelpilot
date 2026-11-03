"""End-to-end pipeline behaviour, driven by a fake provider.

These tests exercise the real orchestration, state-sync and export code paths
without touching the network.  They are the tests that would catch a regression
in "does a chapter actually end up in the Bible".
"""

from __future__ import annotations

import json

from novelpilot.core.engine import Engine
from novelpilot.core.models import ChapterPlan, SceneCard, Stage, StoryBible
from novelpilot.graph.pipeline import Pipeline

from conftest import FakeProvider

OUTLINE_JSON = json.dumps(
    {
        "title_candidates": ["The Salt Road", "Salt and Ash", "The Long White Causeway"],
        "ending": "Mara delivers the message and the empire falls.",
        "acts": [
            {"number": 1, "name": "Acceptance", "goal": "Take the contract",
             "turns": ["The forgery is revealed"], "chapters": [1, 2]},
            {"number": 2, "name": "Crossing", "goal": "Reach the border",
             "turns": ["Mara loses the satchel"], "chapters": [3, 4]},
            {"number": 3, "name": "Delivery", "goal": "End the war",
             "turns": ["The message is delivered"], "chapters": [5]},
        ],
        "beats": [
            {"label": "Inciting incident", "description": "Mara takes the contract.",
             "chapter": 1, "tension": 0.4, "participants": ["Mara"]},
            {"label": "Midpoint reversal", "description": "The message is a forgery.",
             "chapter": 3, "tension": 0.7, "participants": ["Mara"]},
            {"label": "Climax", "description": "Mara delivers the real message.",
             "chapter": 5, "tension": 1.0, "participants": ["Mara"]},
        ],
    }
)

CHAPTERS_JSON = json.dumps(
    {
        "chapters": [
            {
                "chapter": 1,
                "title": "The Contract",
                "purpose": "Establish the world and the deal.",
                "pov": "Mara",
                "location": "The Salt Road",
                "scenes": [
                    {
                        "heading": "An Offer",
                        "pov": "Mara",
                        "location": "The Salt Road",
                        "participants": ["Mara", "Elias"],
                        "goal": "Take the contract",
                        "conflict": "The price is too high",
                        "outcome": "Mara accepts anyway",
                        "must_include": ["the contract"],
                        "must_avoid": [],
                        "threads": [],
                        "words": 400,
                    }
                ],
            }
        ]
    }
)

SYNC_JSON = json.dumps(
    {
        "chapter_title": "The Contract",
        "summary": "Mara accepts a dangerous contract to carry a message across the salt.",
        "events": [
            {"summary": "Mara accepted the contract to carry a message.",
             "participants": ["Mara"], "irreversible": True}
        ],
        "opened_threads": ["The forged message"],
        "resolved_threads": [],
        "character_updates": [{"name": "Mara", "learned": "The message must reach the border."}],
        "emotional_valence": -0.2,
        "tension": 0.6,
    }
)


def _provider() -> FakeProvider:
    return FakeProvider(
        {
            "three-act structure": OUTLINE_JSON,
            "scene planner": CHAPTERS_JSON,
            "continuity editor": SYNC_JSON,
        }
    )


def _bible() -> StoryBible:
    from novelpilot.core.models import Character, Location

    b = StoryBible(
        title="The Salt Road",
        genre="fantasy",
        logline="A courier crosses a dissolving empire.",
        premise="Salt, empire, a message.",
        target_word_count=20_000,
    )
    b.characters = [
        Character(id="c-mara", name="Mara", role="protagonist",
                  physical={"eyes": "grey"}, goal="Deliver the message"),
        Character(id="c-elias", name="Elias", role="deuteragonist"),
    ]
    b.locations = [Location(id="l-road", name="The Salt Road")]
    return b


async def test_full_pipeline_writes_chapters_and_exports(store, tmp_path) -> None:
    store.create_project("salt-road", "The Salt Road")
    store.save_bible("salt-road", _bible())

    engine = Engine(provider=_provider(), models={"plan": "m", "prose": "m", "summary": "m",
                                                  "judge": "m"}, max_cost_usd=100.0)
    pipe = Pipeline(store, engine)
    result = await pipe.run("salt-road", chapters=1, outdir=tmp_path / "out", verify=False)

    assert result.chapters_written == [1]
    assert (tmp_path / "out").exists()

    drafts = store.load_all_drafts("salt-road")
    assert 1 in drafts


async def test_chapter_sync_folds_deltas_into_the_bible(store) -> None:
    store.create_project("salt-road", "The Salt Road")
    store.save_bible("salt-road", _bible())

    engine = Engine(provider=_provider(), models={"plan": "m", "prose": "m", "summary": "m",
                                                  "judge": "m"}, max_cost_usd=100.0)
    pipe = Pipeline(store, engine)
    await pipe.run("salt-road", chapters=1, verify=False)

    bible = store.load_bible("salt-road")
    assert len(bible.chapter_summaries) == 1
    assert bible.chapter_summaries[0].chapter == 1
    assert any("accepted the contract" in e.summary for e in bible.events)
    assert any(t.title == "The forged message" for t in bible.threads)
    mara = bible.character_by_name("Mara")
    assert mara is not None
    assert any("reach the border" in k for k in mara.knowledge)


async def test_pipeline_is_resumable_and_skips_written_chapters(store) -> None:
    store.create_project("salt-road", "The Salt Road")
    store.save_bible("salt-road", _bible())

    engine = Engine(provider=_provider(), models={"plan": "m", "prose": "m", "summary": "m",
                                                  "judge": "m"}, max_cost_usd=100.0)
    pipe = Pipeline(store, engine)
    await pipe.run("salt-road", chapters=1, verify=False)
    calls_after_first = len(engine.provider.calls)

    # Second run must not rewrite chapter 1.
    engine2 = Engine(provider=_provider(), models={"plan": "m", "prose": "m", "summary": "m",
                                                   "judge": "m"}, max_cost_usd=100.0)
    pipe2 = Pipeline(store, engine2)
    result = await pipe2.run("salt-road", chapters=1, verify=False)
    assert result.chapters_written == []
    assert len(engine2.provider.calls) < calls_after_first


async def test_planning_produces_scene_cards(store) -> None:
    store.create_project("salt-road", "The Salt Road")
    store.save_bible("salt-road", _bible())

    engine = Engine(provider=_provider(), models={"plan": "m", "prose": "m", "summary": "m",
                                                  "judge": "m"}, max_cost_usd=100.0)
    pipe = Pipeline(store, engine)
    plans = await pipe.step_chapters("salt-road")

    assert len(plans) == 1
    plan = plans[0]
    assert plan.chapter == 1
    assert len(plan.scenes) == 1
    assert plan.scenes[0].pov_character_id == "c-mara"
    assert plan.scenes[0].location_id == "l-road"
    assert "c-elias" in plan.scenes[0].participants


async def test_outline_tension_is_shaped_not_flat(store) -> None:
    store.create_project("salt-road", "The Salt Road")
    store.save_bible("salt-road", _bible())

    engine = Engine(provider=_provider(), models={"plan": "m", "prose": "m", "summary": "m",
                                                  "judge": "m"}, max_cost_usd=100.0)
    pipe = Pipeline(store, engine)
    outline = await pipe.step_outline("salt-road")

    assert len(outline.acts) == 3
    assert len(outline.beats) == 3
    tensions = [b.tension for b in outline.beats]
    assert max(tensions) > min(tensions)


async def test_export_produces_epub_docx_and_txt(store, tmp_path) -> None:
    store.create_project("salt-road", "The Salt Road")
    store.save_bible("salt-road", _bible())

    engine = Engine(provider=_provider(), models={"plan": "m", "prose": "m", "summary": "m",
                                                  "judge": "m"}, max_cost_usd=100.0)
    pipe = Pipeline(store, engine)
    await pipe.run("salt-road", chapters=1, verify=False)

    paths = await pipe.step_export("salt-road", tmp_path / "out", author="Jane Doe")
    assert paths["epub"].exists()
    assert paths["docx"].exists()
    assert paths["txt"].exists()

    bible = store.load_bible("salt-road")
    assert bible.stage == Stage.COMPLETE
