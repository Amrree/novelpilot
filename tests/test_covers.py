"""Cover generation.

Covers are a real deliverable, and the renderer had a bug where the title
rendered off-canvas (the SVG <text> element had no baseline, and relative
`dy` positioning on tspans is not portable across renderers). These tests
guard against that class of regression.
"""

from __future__ import annotations

import re

from novelpilot.plugins.covers import (
    BLEED_PX,
    COVER_PX,
    PALETTES,
    CoverSpec,
    cover_svg,
    render_png,
)


def test_cover_is_kdp_sized() -> None:
    """6x9in + 0.125in bleed on each edge at 300 DPI."""
    assert COVER_PX == 2565
    assert BLEED_PX == 38
    svg = cover_svg(CoverSpec(title="The Salt Road"))
    assert f'width="{COVER_PX}"' in svg
    assert f'height="{COVER_PX}"' in svg


def test_title_is_present_and_on_canvas() -> None:
    svg = cover_svg(CoverSpec(title="The Salt Road"))
    assert "The Salt Road" in svg

    # Every tspan must carry an absolute y inside the canvas.
    ys = [int(y) for y in re.findall(r'<tspan x="\d+" y="(\d+)"', svg)]
    assert ys, "no tspan with an absolute y was found"
    assert all(0 < y < COVER_PX for y in ys), f"tspan baseline off-canvas: {ys}"


def test_long_titles_wrap_onto_multiple_lines() -> None:
    svg = cover_svg(CoverSpec(title="A Very Long Title That Will Certainly Need To Wrap"))
    assert svg.count("<tspan") >= 2


def test_cover_is_deterministic() -> None:
    """Same input must give byte-identical output, or rebuilds churn artwork."""
    a = cover_svg(CoverSpec(title="The Salt Road", author="Jane Doe", genre="fantasy"))
    b = cover_svg(CoverSpec(title="The Salt Road", author="Jane Doe", genre="fantasy"))
    assert a == b


def test_different_titles_give_different_covers() -> None:
    a = cover_svg(CoverSpec(title="The Salt Road"))
    b = cover_svg(CoverSpec(title="Salt and Ash"))
    assert a != b


def test_xml_special_characters_are_escaped() -> None:
    svg = cover_svg(CoverSpec(title="Salt & Stone <Empire>"))
    # The title wraps across lines, so check the escaped fragments.
    assert "Salt &amp; Stone" in svg
    assert "&lt;Empire&gt;" in svg
    assert "<Empire>" not in svg


def test_genre_selects_a_palette() -> None:
    fantasy = cover_svg(CoverSpec(title="X", genre="fantasy"))
    romance = cover_svg(CoverSpec(title="X", genre="romance"))
    assert PALETTES["fantasy"][0] in fantasy
    assert PALETTES["romance"][0] in romance
    assert fantasy != romance


def test_unknown_genre_falls_back_to_default() -> None:
    svg = cover_svg(CoverSpec(title="X", genre="kafkaesque-bureaucracy"))
    assert PALETTES["default"][0] in svg


def test_render_png_writes_a_file(tmp_path) -> None:
    path = render_png(CoverSpec(title="The Salt Road"), tmp_path / "cover.png")
    assert path.exists()
    assert path.stat().st_size > 0
