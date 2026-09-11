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

import resvg_py

from nf_metro.render.font_embed import _FONTS_DIR

__all__ = ["svg_to_png"]

# fontdb reads ttf/otf but not woff2, so these are the uncompressed twins of
# the Inter-*.woff2 subsets beside them (tests/test_png_output.py keeps them in step).
_FONT_FILES = [
    str(_FONTS_DIR / f"Inter-{weight}.ttf") for weight in ("Regular", "Bold")
]


def svg_to_png(svg: str, *, scale: float = 2.0, width: int | None = None) -> bytes:
    """Rasterise *svg* to PNG bytes.

    ``scale`` multiplies the SVG's own pixel size.  ``width`` instead pins the
    output width and scales the height with it, overriding ``scale``.  Both
    resize the picture; the SVG's own ``--width`` grows the canvas around a
    map drawn at its natural size, which is a different thing entirely.
    """
    sizing = {"width": width} if width is not None else {"zoom": scale}
    return bytes(
        resvg_py.svg_to_bytes(
            svg_string=svg,
            skip_system_fonts=True,
            font_files=_FONT_FILES,
            **sizing,
        )
    )
