"""PNG rasterisation of a rendered SVG, via resvg.

resvg resolves fonts through fontdb, which matches a ``font-family`` against
whatever the machine happens to have installed.  Left alone that makes a PNG
depend on its author's font set.  :func:`svg_to_png` instead skips system fonts
and loads only the bundled Inter faces, so the same SVG rasterises to the same
bytes anywhere.

That pairs with the ``"embed"`` font-portability mode, which puts Inter at the
head of every ``font-family`` and measures the layout with Inter metrics: the
raster then draws the face the geometry was computed for.  Callers that ask for
a PNG without it would lay out against fallback metrics and draw in Inter.
"""

from __future__ import annotations

from pathlib import Path

import resvg_py

__all__ = ["svg_to_png"]

# fontdb reads ttf/otf but not woff2, so these are the uncompressed twins of
# the Inter-*.woff2 subsets beside them (tests/test_fonts.py keeps them in step).
_FONTS_DIR = Path(__file__).parent.parent / "fonts"
_FONT_FILES = [
    str(_FONTS_DIR / f"Inter-{weight}.ttf") for weight in ("Regular", "Bold")
]


def svg_to_png(svg: str, *, scale: float = 2.0) -> bytes:
    """Rasterise *svg* to PNG bytes, at *scale* times its own pixel size."""
    return bytes(
        resvg_py.svg_to_bytes(
            svg_string=svg,
            skip_system_fonts=True,
            font_files=_FONT_FILES,
            zoom=scale,
        )
    )
