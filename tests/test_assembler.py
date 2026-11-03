"""Export formats: EPUB structure, DOCX output, KDP metadata."""

from __future__ import annotations

import zipfile

from novelpilot.core.assembler import (
    Manuscript,
    kdp_metadata,
    to_docx,
    to_epub,
    to_plain_text,
)
from novelpilot.core.models import ChapterDraft


def _manuscript() -> Manuscript:
    return Manuscript(
        title="The Salt Road",
        author="Jane Doe",
        subtitle="A Courier's Tale",
        genre="fantasy",
        logline="A courier crosses a dissolving empire.",
        chapters=[
            ChapterDraft(chapter=1, title="Chapter One", text="The salt was white.\n\nShe walked.",
                         word_count=6),
            ChapterDraft(chapter=2, title="Chapter Two", text="The checkpoint knew her face.",
                         word_count=6),
        ],
    )


# --------------------------------------------------------------------------
# EPUB
# --------------------------------------------------------------------------


def test_epub_mimetype_is_first_and_stored(tmp_path) -> None:
    """KDP's two hard rules: mimetype first, and stored not deflated."""
    path = to_epub(_manuscript(), tmp_path / "book.epub")
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        assert names[0] == "mimetype"
        info = zf.getinfo("mimetype")
        assert info.compress_type == zipfile.ZIP_STORED
        assert zf.read("mimetype") == b"application/epub+zip"


def test_epub_has_required_structure(tmp_path) -> None:
    path = to_epub(_manuscript(), tmp_path / "book.epub")
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        assert "META-INF/container.xml" in names
        assert "OEBPS/content.opf" in names
        assert "OEBPS/nav.xhtml" in names
        assert "OEBPS/text/chapter-1.xhtml" in names
        assert "OEBPS/text/chapter-2.xhtml" in names


def test_epub_container_points_at_the_package(tmp_path) -> None:
    path = to_epub(_manuscript(), tmp_path / "book.epub")
    with zipfile.ZipFile(path) as zf:
        container = zf.read("META-INF/container.xml").decode()
        assert "OEBPS/content.opf" in container


def test_epub_package_declares_nav_property(tmp_path) -> None:
    path = to_epub(_manuscript(), tmp_path / "book.epub")
    with zipfile.ZipFile(path) as zf:
        opf = zf.read("OEBPS/content.opf").decode()
        assert 'properties="nav"' in opf
        assert "<dc:title>The Salt Road</dc:title>" in opf
        assert "<dc:creator>Jane Doe</dc:creator>" in opf


def test_epub_spine_lists_every_chapter(tmp_path) -> None:
    path = to_epub(_manuscript(), tmp_path / "book.epub")
    with zipfile.ZipFile(path) as zf:
        opf = zf.read("OEBPS/content.opf").decode()
        assert opf.count("<itemref") == 2


def test_epub_escapes_xml_special_characters(tmp_path) -> None:
    ms = _manuscript()
    ms.title = "Salt & Stone <Empire>"
    path = to_epub(ms, tmp_path / "book.epub")
    with zipfile.ZipFile(path) as zf:
        opf = zf.read("OEBPS/content.opf").decode()
        assert "Salt &amp; Stone &lt;Empire&gt;" in opf


def test_epub_refuses_empty_manuscript(tmp_path) -> None:
    ms = _manuscript()
    ms.chapters = []
    try:
        to_epub(ms, tmp_path / "empty.epub")
    except ValueError:
        return
    raise AssertionError("expected ValueError for a manuscript with no chapters")


# --------------------------------------------------------------------------
# DOCX
# --------------------------------------------------------------------------


def test_docx_is_written_and_readable(tmp_path) -> None:
    path = to_docx(_manuscript(), tmp_path / "book.docx")
    assert path.exists() and path.stat().st_size > 0
    from docx import Document

    doc = Document(str(path))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "THE SALT ROAD" in text.upper()
    assert "Jane Doe" in text
    assert "The salt was white." in text


def test_docx_uses_six_by_nine_trim(tmp_path) -> None:
    from docx import Document
    from docx.shared import Inches

    path = to_docx(_manuscript(), tmp_path / "book.docx")
    doc = Document(str(path))
    section = doc.sections[0]
    assert abs(section.page_width - Inches(6)) < 1000
    assert abs(section.page_height - Inches(9)) < 1000


# --------------------------------------------------------------------------
# Text and metadata
# --------------------------------------------------------------------------


def test_plain_text_includes_chapters(tmp_path) -> None:
    text = to_plain_text(_manuscript())
    assert "THE SALT ROAD" in text
    assert "Chapter One" in text
    assert "The salt was white." in text


def test_kdp_metadata_maps_genre_to_bisac() -> None:
    meta = kdp_metadata(_manuscript())
    assert meta["bisac"].startswith("Science Fiction & Fantasy")
    assert meta["trim_size"] == "6x9 in"
    assert meta["chapter_count"] == 2


def test_kdp_metadata_maps_romance() -> None:
    ms = _manuscript()
    ms.genre = "romance"
    assert kdp_metadata(ms)["bisac"] == "Romance > Contemporary"
