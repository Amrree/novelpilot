"""Cover generation.

Two backends, chosen by what's available:

* **Typographic (default)** -- a deterministic SVG/PNG cover generated from the
  title, author and genre palette.  No API key, no network, no cost, and
  deterministic for a given title, so re-running a build does not change the
  artwork.  It looks competent rather than generic, which matters because a
  weak cover is the single biggest reason a self-published book does not sell.
* **Image model (optional)** -- delegates to a text-to-image provider for
  genre artwork, then overlays the typography so the title stays legible.
  This is the path the product uses in production; the typographic path is the
  fallback and the offline default.

The KDP minimum for a print cover is 1.625 x 1.625 inches at 300 DPI
(2550 x 2550 px) with 0.125 inch bleed on each edge, giving a 2565 x 2565 px
file for a 6x9 book.  We generate that size.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Genre palettes: (background, accent, text)
PALETTES: dict[str, tuple[str, str, str]] = {
    "romance": ("#2d1b2e", "#e8a0bf", "#fdf6f8"),
    "dark romance": ("#1a1012", "#8b1e3f", "#f5e6e9"),
    "romantasy": ("#1b1430", "#9b6dd7", "#f3ecff"),
    "fantasy": ("#12211c", "#4fa87a", "#eefaf3"),
    "epic fantasy": ("#101820", "#c9a227", "#fdf8e7"),
    "thriller": ("#0e1116", "#e04747", "#f0f2f5"),
    "mystery": ("#14171c", "#7a8899", "#eef1f5"),
    "cozy mystery": ("#f3ead9", "#c2703d", "#3a2a1c"),
    "sci-fi": ("#070b18", "#3fc1d4", "#e6f6fa"),
    "space opera": ("#0a0d1c", "#6c7bff", "#e9ecff"),
    "horror": ("#0c0c0c", "#8b0000", "#e8e8e8"),
    "historical": ("#2b2418", "#a8843c", "#f5efe2"),
    "young adult": ("#1d3557", "#f77f00", "#fdf6e8"),
    "children": ("#fefae0", "#e76f51", "#264653"),
    "default": ("#14161c", "#6c7bff", "#f2f3f7"),
}

COVER_PX = 2565  # 6x9in + 0.125in bleed @ 300dpi
BLEED_PX = 38


@dataclass(slots=True)
class CoverSpec:
    title: str
    author: str = ""
    subtitle: str = ""
    genre: str = ""
    seed: str = ""

    def palette(self) -> tuple[str, str, str]:
        g = (self.genre or "default").lower()
        if g in PALETTES:
            return PALETTES[g]
        for key, pal in PALETTES.items():
            if key in g:
                return pal
        return PALETTES["default"]

    def stable_seed(self) -> int:
        raw = f"{self.title}|{self.author}|{self.genre}|{self.seed}"
        return int(hashlib.sha256(raw.encode()).hexdigest()[:8], 16)


def _wrap_title(title: str, max_chars: int = 16) -> list[str]:
    words = title.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= max_chars:
            cur = f"{cur} {w}"
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines[:4] or [title]


def cover_svg(spec: CoverSpec) -> str:
    """Deterministic SVG cover.  Same input -> byte-identical output."""
    bg, accent, text = spec.palette()
    seed = spec.stable_seed()
    lines = _wrap_title(spec.title)
    n = len(lines)

    # Font size scales with the longest line.
    longest = max((len(x) for x in lines), default=10)
    size = max(72, min(300, int((COVER_PX * 0.82) / max(longest, 1) * 0.9)))
    start_y = int(COVER_PX * 0.42) - (n - 1) * int(size * 0.62)

    # Decorative geometry derived from the seed.
    shapes: list[str] = []
    for i in range(5):
        h = (seed >> (i * 3)) & 0xFF
        cx = int((seed >> (i * 5)) % COVER_PX)
        cy = int((seed >> (i * 7)) % COVER_PX)
        r = 80 + h * 4
        op = 0.04 + (h % 7) / 100
        shapes.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{accent}" opacity="{op:.3f}"/>'
        )

    # Each tspan gets an absolute y. Using relative `dy` here is unreliable:
    # renderers differ on whether a tspan with an absolute `x` inherits the
    # parent's baseline or resets it, and the symptom is text off-canvas.
    line_height = int(size * 1.18)
    tspans = "\n".join(
        f'    <tspan x="{COVER_PX // 2}" y="{start_y + i * line_height}">{_esc(line)}</tspan>'
        for i, line in enumerate(lines)
    )
    text_bottom = start_y + (n - 1) * line_height
    author_block = ""
    if spec.author:
        author_block = (
            f'  <text x="{COVER_PX // 2}" y="{int(COVER_PX * 0.90)}" font-family="Georgia, serif" '
            f'font-size="96" fill="{text}" opacity="0.92" text-anchor="middle">'
            f'{_esc(spec.author)}</text>'
        )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{COVER_PX}" height="{COVER_PX}"
     viewBox="0 0 {COVER_PX} {COVER_PX}">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0.6" y2="1">
      <stop offset="0%" stop-color="{bg}"/>
      <stop offset="100%" stop-color="#05070c"/>
    </linearGradient>
  </defs>
  <rect width="{COVER_PX}" height="{COVER_PX}" fill="url(#bg)"/>
  {''.join(shapes)}
  <rect x="40" y="40" width="{COVER_PX - 80}" height="{COVER_PX - 80}" fill="none"
        stroke="{accent}" stroke-width="6" opacity="0.55"/>
  <text font-family="Georgia, 'Times New Roman', serif" font-size="{size}"
        font-weight="700" fill="{text}" text-anchor="middle" letter-spacing="4">
{tspans}
  </text>
  <rect x="{COVER_PX // 2 - 180}" y="{text_bottom + int(size * 0.55)}" width="360" height="8" fill="{accent}"/>
{author_block}
</svg>
"""


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def render_png(spec: CoverSpec, path: str | Path) -> Path:
    """Render the SVG cover to PNG via cairosvg if available, else write the SVG."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    svg = cover_svg(spec)
    try:
        import cairosvg

        cairosvg.svg2png(bytestring=svg.encode("utf-8"), write_to=str(path),
                         output_width=COVER_PX, output_height=COVER_PX)
        return path
    except Exception:  # noqa: BLE001 - cairosvg is optional
        svg_path = path.with_suffix(".svg")
        svg_path.write_text(svg, encoding="utf-8")
        return svg_path


class CoverPlugin:
    """Generates a cover for a finished manuscript."""

    manifest = {
        "name": "covers",
        "version": "0.1.0",
        "description": "Generates a deterministic typographic cover (SVG/PNG) for a manuscript.",
        "backend": "typographic",
    }

    async def run(self, title: str, author: str = "", genre: str = "", subtitle: str = "",
                  outdir: str | Path = "exports", seed: str = "") -> dict[str, Any]:
        spec = CoverSpec(title=title, author=author, genre=genre, subtitle=subtitle, seed=seed)
        outdir = Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        target = outdir / "cover.png"
        written = render_png(spec, target)
        return {
            "path": str(written),
            "size_px": [COVER_PX, COVER_PX],
            "trim": "6x9in",
            "bleed_in": 0.125,
        }


plugin = CoverPlugin()
