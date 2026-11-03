"""Planner helpers and the HTTP API surface."""

from __future__ import annotations

import json

import pytest

from novelpilot.core.models import BeatSheet, StoryBible
from novelpilot.core.planner import _normalize_tension, plan_chapter_count


def test_chapter_count_scales_with_length() -> None:
    assert plan_chapter_count(80_000, 2_500) == 32
    assert plan_chapter_count(80_000, 4_000) == 20
    assert plan_chapter_count(10_000, 2_500) == 4  # floor of 4


def test_chapter_count_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        plan_chapter_count(80_000, 0)


def test_tension_curve_is_not_flat() -> None:
    beats = [BeatSheet(label=f"b{i}", description="", chapter=i, tension=0.5) for i in range(1, 13)]
    shaped = _normalize_tension(beats, 12)
    tensions = [b.tension for b in shaped]
    assert max(tensions) > 0.7
    assert min(tensions) < 0.45
    assert tensions[-1] > tensions[0]


def test_tension_is_clamped_to_unit_range() -> None:
    beats = [BeatSheet(label="x", description="", chapter=1, tension=1.0)]
    shaped = _normalize_tension(beats, 1)
    assert 0.0 <= shaped[0].tension <= 1.0


def test_tension_rejects_out_of_range_input() -> None:
    """The model refuses to store an impossible tension value."""
    import pytest as _pytest
    from pydantic import ValidationError

    with _pytest.raises(ValidationError):
        BeatSheet(label="x", description="", chapter=1, tension=5.0)


def test_tension_handles_empty_input() -> None:
    assert _normalize_tension([], 10) == []


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELPILOT_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("NOVELPILOT_DATA_DIR", str(tmp_path / ".novelpilot"))
    from fastapi.testclient import TestClient

    from novelpilot.server.app import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c


def test_health_endpoint(client) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_create_and_read_project(client) -> None:
    payload = {
        "title": "The Salt Road",
        "genre": "fantasy",
        "premise": "A courier crosses an empire.",
        "characters": [{"name": "Mara", "role": "protagonist"}],
    }
    r = client.post("/projects", json=payload)
    assert r.status_code == 201
    assert r.json()["slug"] == "the-salt-road"

    r = client.get("/projects/the-salt-road")
    assert r.status_code == 200
    assert r.json()["title"] == "The Salt Road"
    assert r.json()["characters"][0]["name"] == "Mara"


def test_missing_project_returns_404(client) -> None:
    assert client.get("/projects/nope").status_code == 404


def test_list_projects(client) -> None:
    client.post("/projects", json={"title": "Book One"})
    client.post("/projects", json={"title": "Book Two"})
    r = client.get("/projects")
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_export_without_chapters_is_a_conflict(client) -> None:
    client.post("/projects", json={"title": "Empty Book"})
    r = client.post("/projects/empty-book/export", json={"author": "Jane"})
    assert r.status_code == 409


def test_openapi_schema_is_generated(client) -> None:
    r = client.get("/openapi.json")
    assert r.status_code == 200
    schema = r.json()
    assert "/projects" in schema["paths"]
    assert "/projects/{slug}/write" in schema["paths"]
