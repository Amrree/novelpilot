"""Assembly: turning finished chapters into publishable files.

The output targets Amazon KDP, which is fussy about EPUB internals: the
``mimetype`` entry must be the first file in the archive and must be stored
uncompressed, the container and package documents must be well-formed, and the
navigation document must be present.  Those rules are implemented literally
here rather than delegated to a library, because the libraries mostly ignore
them and you find out at upload time.

Also produces DOCX (for beta readers and print-layout workflows) and plain
text (for the cheap "read it on your phone" path).
"""

from __future__ import annotations

import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .models import ChapterDraft, StoryBible

EPUB_NS = "http://www.idpf.org/2007/ops"
DC_NS = "http://purl.org/dc/elements/1.1/"


@dataclass(slots=True)
class Manuscript:
    """A book ready for export."""

    title: str
    author: str = "Anonymous"
    subtitle: str = ""
    chapters: list[ChapterDraft] = None  # type: ignore[assignment]
    bible: StoryBible | None = None
    genre: str = ""
    description: str = ""
    logline: str = ""

    def __post_init__(self) -> None:
        if self.chapters is None:
            self.chapters = []
        if not self.description and self.logline:
            self.description = self.logline

    @property
    def word_count(self) -> int:
        return sum(c.word_count for c in self.chapters)


# --------------------------------------------------------------------------
# Plain text
# --------------------------------------------------------------------------


def to_plain_text(ms: Manuscript) -> str:
    out: list[str] = [ms.title.upper(), ""]
    if ms.subtitle:
        out += [ms.subtitle, ""]
    out += [f"by {ms.author}", ""]
    for ch in sorted(ms.chapters, key=lambda c: c.chapter):
        title = ch.title or f"Chapter {ch.chapter}"
        out += ["", "", title, "", ""]
        out.append(ch.text.strip())
    return "\n".join(out).strip() + "\n"


# --------------------------------------------------------------------------
# DOCX
# --------------------------------------------------------------------------


def to_docx(ms: Manuscript, path: str | Path) -> Path:
    """Write a DOCX with a title page and chapter headings."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
    from docx.shared import Inches, Pt

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()

    # Page setup for a 6x9 trade paperback (KDP default).
    for section in doc.sections:
        section.page_width = Inches(6)
        section.page_height = Inches(9)
        section.top_margin = Inches(0.75)
        section.bottom_margin = Inches(0.75)
        section.left_margin = Inches(0.75)
        section.right_margin = Inches(0.75)

    normal = doc.styles["Normal"]
    normal.font.name = "Garamond"
    normal.font.size = Pt(11)
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.line_spacing = 1.15

    # Title page.
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(180)
    run = p.add_run(ms.title)
    run.bold = True
    run.font.size = Pt(28)

    if ms.subtitle:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run(ms.subtitle).font.size = Pt(14)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(36)
    p.add_run(ms.author).font.size = Pt(14)

    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    for ch in sorted(ms.chapters, key=lambda c: c.chapter):
        heading = doc.add_heading(ch.title or f"Chapter {ch.chapter}", level=1)
        heading.style = doc.styles["Heading 1"]
        heading.paragraph_format.page_break_before = True
        for para in _split_paragraphs(ch.text):
            doc.add_paragraph(para)
        # Scene break.
        doc.add_paragraph("* * *")

    doc.save(str(path))
    return path


# --------------------------------------------------------------------------
# EPUB
# --------------------------------------------------------------------------


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _chapter_xhtml(title: str, text: str, idx: int) -> str:
    paras = "\n".join(f"    <p>{_xml_escape(p)}</p>" for p in _split_paragraphs(text))
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">
  <head>
    <meta charset="utf-8"/>
    <title>{_xml_escape(title)}</title>
    <link rel="stylesheet" type="text/css" href="../styles/book.css"/>
  </head>
  <body>
    <section epub:type="chapter" id="chapter-{idx}">
      <h1>{_xml_escape(title)}</h1>
{paras}
    </section>
  </body>
</html>
"""


def to_epub(ms: Manuscript, path: str | Path, *, cover: str | Path | None = None) -> Path:
    """Write a KDP-safe EPUB 3 file.

    The archive is assembled by hand because the ordering and compression rules
    (``mimetype`` first, stored not deflated) are mandatory for EPUB validity
    and are not enforced by generic zip helpers.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    chapters = sorted(ms.chapters, key=lambda c: c.chapter)
    if not chapters:
        raise ValueError("cannot build an EPUB with no chapters")

    items: list[tuple[str, str, str]] = []  # (id, href, media-type)
    spine: list[tuple[str, str]] = []  # (idref, href)
    nav_items: list[tuple[str, str]] = []

    with zipfile.ZipFile(path, "w") as zf:
        # 1. mimetype -- MUST be first and MUST be stored uncompressed.
        zf.writestr(
            zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0)),
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )

        # 2. META-INF/container.xml
        zf.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
""",
            compress_type=zipfile.ZIP_DEFLATED,
        )

        # 3. Stylesheet
        zf.writestr(
            "OEBPS/styles/book.css",
            """body { margin: 0 5%; font-family: Georgia, 'Times New Roman', serif; line-height: 1.5; }
h1 { font-size: 1.4em; margin: 1.6em 0 1em; page-break-before: always; text-align: center; }
p { margin: 0; text-indent: 1.2em; }
p.first { text-indent: 0; }
""",
            compress_type=zipfile.ZIP_DEFLATED,
        )

        # 4. Cover (optional)
        cover_id = None
        if cover and Path(cover).exists():
            ext = Path(cover).suffix.lower().lstrip(".")
            cover_id = "cover-image"
            zf.write(cover, f"OEBPS/images/cover.{ext}", compress_type=zipfile.ZIP_DEFLATED)
            items.append((cover_id, f"images/cover.{ext}", f"image/{'jpeg' if ext in ('jpg','jpeg') else ext}"))

        # 5. Chapters
        for i, ch in enumerate(chapters, start=1):
            title = ch.title or f"Chapter {ch.chapter}"
            href = f"text/chapter-{i}.xhtml"
            cid = f"chapter-{i}"
            zf.writestr(
                f"OEBPS/{href}",
                _chapter_xhtml(title, ch.text, i),
                compress_type=zipfile.ZIP_DEFLATED,
            )
            items.append((cid, href, "application/xhtml+xml"))
            spine.append((cid, href))
            nav_items.append((title, href))

        # 6. Navigation document
        nav_html = _nav_xhtml(ms, nav_items)
        zf.writestr("OEBPS/nav.xhtml", nav_html, compress_type=zipfile.ZIP_DEFLATED)
        items.append(("nav", "nav.xhtml", "application/xhtml+xml"))

        # 7. Package document
        zf.writestr("OEBPS/content.opf", _content_opf(ms, items, spine, cover_id),
                    compress_type=zipfile.ZIP_DEFLATED)

    return path


def _nav_xhtml(ms: Manuscript, nav_items: list[tuple[str, str]]) -> str:
    lis = "\n".join(
        f'      <li><a href="{href}">{_xml_escape(title)}</a></li>' for title, href in nav_items
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en">
  <head><meta charset="utf-8"/><title>Contents</title></head>
  <body>
    <nav epub:type="toc" id="toc">
      <h1>Contents</h1>
      <ol>
{lis}
      </ol>
    </nav>
  </body>
</html>
"""


def _content_opf(
    ms: Manuscript,
    items: list[tuple[str, str, str]],
    spine: list[tuple[str, str]],
    cover_id: str | None,
) -> str:
    manifest_lines = "\n".join(
        f'    <item id="{iid}" href="{href}" media-type="{mime}"/>' for iid, href, mime in items
    )
    spine_lines = "\n".join(f'    <itemref idref="{iid}"/>' for iid, _ in spine)
    cover_meta = (
        f'    <meta name="cover" content="{cover_id}"/>\n' if cover_id else ""
    )
    desc = ms.description or f"{ms.genre} novel."
    return f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid"
         xml:lang="en">
  <metadata xmlns:dc="{DC_NS}">
    <dc:identifier id="bookid">urn:uuid:novelpilot-{abs(hash(ms.title))}</dc:identifier>
    <dc:title>{_xml_escape(ms.title)}</dc:title>
    <dc:creator>{_xml_escape(ms.author)}</dc:creator>
    <dc:language>en</dc:language>
    <dc:description>{_xml_escape(desc)}</dc:description>
    <meta property="dcterms:modified">2024-01-01T00:00:00Z</meta>
{cover_meta}  </metadata>
  <manifest>
{manifest_lines}
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
  </manifest>
  <spine>
{spine_lines}
  </spine>
</package>
"""


# --------------------------------------------------------------------------
# Metadata / KDP
# --------------------------------------------------------------------------


def kdp_metadata(ms: Manuscript) -> dict[str, Any]:
    """Title, subtitle, description and BISAC-style categories for a KDP listing."""
    categories = {
        "romance": ("Romance", "Contemporary"),
        "romantasy": ("Romance", "Fantasy"),
        "fantasy": ("Science Fiction & Fantasy", "Fantasy"),
        "thriller": ("Thrillers & Suspense", "Thriller"),
        "mystery": ("Mystery & Thriller", "Mystery"),
        "cozy": ("Mystery & Thriller", "Cozy"),
        "scifi": ("Science Fiction & Fantasy", "Science Fiction"),
        "space opera": ("Science Fiction & Fantasy", "Space Opera"),
        "horror": ("Horror", "Horror"),
        "historical": ("Romance", "Historical"),
        "young adult": ("Teen & Young Adult", "Coming of Age"),
        "children": ("Children's Books", "Growing Up & Facts of Life"),
    }
    key = (ms.genre or "").lower()
    category, subcategory = ("Fiction", "General")
    for k, v in categories.items():
        if k in key:
            category, subcategory = v
            break

    description = ms.description or (
        f"{ms.title} — a {ms.genre or 'fiction'} novel of {ms.word_count:,} words."
    )
    return {
        "title": ms.title,
        "subtitle": ms.subtitle,
        "description": description,
        "bisac": f"{category} > {subcategory}",
        "word_count": ms.word_count,
        "chapter_count": len(ms.chapters),
        "isbn": None,  # KDP assigns these for Kindle; paperbacks need one purchased
        "trim_size": "6x9 in",
        "bleed": "no bleed",
    }


def export_all(
    ms: Manuscript, outdir: str | Path, *, cover: str | Path | None = None, stem: str | None = None
) -> dict[str, Path]:
    """Produce every export format at once."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    stem = stem or re.sub(r"[^a-z0-9]+", "-", ms.title.lower()).strip("-") or "manuscript"

    out: dict[str, Path] = {}
    out["txt"] = outdir / f"{stem}.txt"
    out["txt"].write_text(to_plain_text(ms), encoding="utf-8")

    try:
        out["docx"] = to_docx(ms, outdir / f"{stem}.docx")
    except ImportError:
        out["docx"] = outdir / f"{stem}.docx"  # recorded but not written
        out.pop("docx")

    out["epub"] = to_epub(ms, outdir / f"{stem}.epub", cover=cover)
    return out
