"""Font portability utilities for SVG output.

Two opt-in strategies for making SVG output font-portable:

``embed_font(svg)``
    Inlines a subset of Inter as a base64 ``@font-face`` declaration in the
    SVG ``<style>`` block.  The SVG is self-contained: it renders identically
    on any host regardless of whether Helvetica Neue or Inter are installed.
    Keeps text selectable and ``data-*`` interactivity on labels intact.

``text_to_paths(svg)``
    Converts every ``<text>`` element to ``<path>`` elements using
    ``fontTools``.  The result has zero font dependency.  Loses selectable
    text and label-level ``data-*`` attributes.  Requires ``fontTools[woff]``
    (``pip install "fonttools[woff]"``).
"""

from __future__ import annotations

import base64
import functools
import html
import re
from collections import namedtuple
from pathlib import Path

__all__ = [
    "embed_font",
    "text_to_paths",
    "EMBEDDED_FONT_FAMILY",
    "EMBEDDED_FONT_STACK",
]

_FONTS_DIR = Path(__file__).parent.parent / "fonts"

# Face name declared by the injected @font-face rule.
EMBEDDED_FONT_FAMILY = "Inter"

# font-family value written onto text elements: the embedded face first, a
# generic sans-serif fallback last.  Where the @font-face is honoured Inter
# wins; where the <style> is stripped (e.g. GitHub's SVG sanitiser) the value
# degrades to sans-serif instead of the browser's serif default for an
# unknown family.
EMBEDDED_FONT_STACK = (
    f"{EMBEDDED_FONT_FAMILY}, 'Helvetica Neue', Helvetica, Arial, sans-serif"
)

# Attributes to drop from text elements when converting to paths
# (they become meaningless once text is gone).
_DROP_ATTRS = frozenset(
    {
        "font-family",
        "font-weight",
        "font-size",
        "text-anchor",
        "dy",
        "dominant-baseline",
        "text-decoration",
        "aria-hidden",
        "aria-label",
    }
)

# Match a complete <text …>content</text> element (single-line content).
# drawsvg never emits <tspan> for nf-metro output.
_TEXT_ELEM_RE = re.compile(
    r"<text\b([^>]*)>(.*?)</text>",
    re.DOTALL,
)

# Match individual attribute name="value" or name='value' pairs.
_ATTR_RE = re.compile(r'([\w:.-]+)=["\']([^"\']*)["\']')

# Match an existing <style> block in the SVG.
_STYLE_BLOCK_RE = re.compile(r"(<style>)(.*?)(</style>)", re.DOTALL)

# Sentinel after </defs> for inserting a new <style> block.
_DEFS_END = "</defs>"

# Holds a pair of per-weight values (regular, bold).
_FontPair = namedtuple("_FontPair", ["regular", "bold"])


# ── helpers ──────────────────────────────────────────────────────────────────


@functools.lru_cache(maxsize=None)
def _font_data_uri(weight: str) -> str:
    """Return a base64 data URI for the bundled Inter WOFF2 subset."""
    path = _FONTS_DIR / f"Inter-{weight}.woff2"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:font/woff2;base64,{data}"


def _font_face_css() -> str:
    """Build @font-face rules for Inter Regular (400) and Bold (700)."""

    def _face(weight_name: str, weight_num: int) -> str:
        return (
            f"@font-face {{ font-family: '{EMBEDDED_FONT_FAMILY}';"
            f" font-style: normal; font-weight: {weight_num};"
            f" src: url('{_font_data_uri(weight_name)}') format('woff2'); }}"
        )

    return " ".join(_face(n, w) for n, w in (("Regular", 400), ("Bold", 700)))


def _parse_attrs(attrs_str: str) -> dict[str, str]:
    return dict(_ATTR_RE.findall(attrs_str))


def _parse_dy(dy_str: str, font_size: float) -> float:
    """Convert a dy value (e.g. '0.3em', '5', '5px') to pixels."""
    dy_str = dy_str.strip()
    if dy_str.endswith("em"):
        return float(dy_str[:-2]) * font_size
    if dy_str.endswith("px"):
        return float(dy_str[:-2])
    try:
        return float(dy_str)
    except ValueError:
        return 0.0


# ── public API ───────────────────────────────────────────────────────────────


def embed_font(svg: str) -> str:
    """Inject an Inter @font-face block and update font-family to the embedded stack.

    The bundled WOFF2 files cover Latin Basic and Latin-1 Supplement
    (U+0020-U+00FF), which is sufficient for typical nf-metro pipeline names.

    Parameters
    ----------
    svg:
        The raw SVG string produced by ``render_svg``.

    Returns
    -------
    str
        Modified SVG with the font embedded.
    """
    css = _font_face_css()

    if _STYLE_BLOCK_RE.search(svg):
        # Prepend to the existing <style> block.
        svg = _STYLE_BLOCK_RE.sub(
            lambda m: m.group(1) + css + "\n" + m.group(2) + m.group(3),
            svg,
            count=1,
        )
    else:
        # Insert a new <style> block immediately after </defs>.
        style_block = f"<style>{css}</style>"
        if _DEFS_END in svg:
            svg = svg.replace(_DEFS_END, _DEFS_END + style_block, 1)
        else:
            # Fallback: insert right after the opening <svg …> tag.
            svg = re.sub(r"(<svg\b[^>]*>)", r"\1" + style_block, svg, count=1)

    # Replace all font-family attributes on text elements with the embedded
    # stack (Inter first, generic sans-serif fallback last).
    svg = re.sub(
        r'(font-family=")[^"]*(")',
        rf"\g<1>{EMBEDDED_FONT_STACK}\g<2>",
        svg,
    )
    return svg


def text_to_paths(svg: str) -> str:
    """Convert all <text> elements to <path> elements.

    Uses the bundled Inter WOFF2 font for glyph outlines.  Characters outside
    the bundled Latin subset are silently dropped; their advance-width is
    consumed so subsequent glyphs remain correctly positioned.

    Parameters
    ----------
    svg:
        The raw SVG string produced by ``render_svg`` (with or without prior
        ``embed_font`` processing).

    Returns
    -------
    str
        Modified SVG with text converted to paths.

    Raises
    ------
    ImportError
        If ``fontTools`` is not installed.
    """
    try:
        from fontTools.ttLib import TTFont  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "fontTools is required for --text-to-paths. "
            'Install it with: pip install "fonttools[woff]"'
        ) from exc

    regular = TTFont(str(_FONTS_DIR / "Inter-Regular.woff2"))
    bold = TTFont(str(_FONTS_DIR / "Inter-Bold.woff2"))

    glyphsets = _FontPair(regular.getGlyphSet(), bold.getGlyphSet())
    cmaps = _FontPair(regular.getBestCmap(), bold.getBestCmap())
    upems = _FontPair(regular["head"].unitsPerEm, bold["head"].unitsPerEm)

    def _replace(match: re.Match[str]) -> str:
        return _text_elem_to_paths(match, glyphsets, cmaps, upems)

    return _TEXT_ELEM_RE.sub(_replace, svg)


# ── text-to-paths internals ──────────────────────────────────────────────────


def _text_elem_to_paths(
    match: re.Match[str],
    glyphsets: _FontPair,
    cmaps: _FontPair,
    upems: _FontPair,
) -> str:
    """Convert one matched <text …>content</text> to a <g> of <path> elements."""
    from fontTools.pens.svgPathPen import SVGPathPen  # type: ignore[import]

    attrs = _parse_attrs(match.group(1))
    raw_content = match.group(2)

    # Decode XML entities so we can look up actual Unicode code-points.
    text_content = html.unescape(raw_content)
    if not text_content:
        return ""

    x = float(attrs.get("x", "0"))
    y = float(attrs.get("y", "0"))
    font_size_str = attrs.get("font-size", "14")
    font_size = float(font_size_str.rstrip("px"))
    font_weight = attrs.get("font-weight", "400")
    fill = attrs.get("fill", "#000000")
    text_anchor = attrs.get("text-anchor", "start")
    dy_str = attrs.get("dy", "0")
    transform = attrs.get("transform", "")

    dy = _parse_dy(dy_str, font_size)
    baseline_y = y + dy

    is_bold = font_weight in ("bold", "700", "600")
    glyphset = glyphsets.bold if is_bold else glyphsets.regular
    cmap = cmaps.bold if is_bold else cmaps.regular
    upem = upems.bold if is_bold else upems.regular
    scale = font_size / upem

    # Compute per-character advance widths (needed for text-anchor offsets).
    glyphs: list[tuple[str | None, float]] = []
    for ch in text_content:
        gn = cmap.get(ord(ch)) if cmap else None
        if gn is not None and gn in glyphset:
            glyphs.append((gn, glyphset[gn].width * scale))
        else:
            # Missing glyph: use a per-em estimate for advance, no path.
            glyphs.append((None, font_size * 0.55))

    total_width = sum(adv for _, adv in glyphs)

    if text_anchor == "middle":
        start_x = x - total_width / 2.0
    elif text_anchor == "end":
        start_x = x - total_width
    else:
        start_x = x

    path_parts: list[str] = []
    cx = start_x
    for glyph_name, advance in glyphs:
        if glyph_name is not None:
            pen = SVGPathPen(glyphset)
            glyphset[glyph_name].draw(pen)
            d = pen.getCommands()
            if d:
                # Font coords: y-up.  SVG coords: y-down.
                # translate(cx, baseline_y) scale(scale, -scale) maps
                # font origin (0,0) to (cx, baseline_y) with y-axis flipped.
                t = (
                    f"translate({cx:.3f},{baseline_y:.3f})"
                    f" scale({scale:.6f},{-scale:.6f})"
                )
                path_parts.append(f'<path d="{d}" transform="{t}"/>')
        cx += advance

    if not path_parts:
        return ""

    group_attrs = f'fill="{fill}"'
    if transform:
        group_attrs += f' transform="{transform}"'

    # Preserve non-font, non-text-layout data-* and class attributes for
    # JavaScript interactivity that keys on them.
    passthrough_attrs = _passthrough_attrs(attrs)
    if passthrough_attrs:
        group_attrs += " " + passthrough_attrs

    return f"<g {group_attrs}>{''.join(path_parts)}</g>"


def _passthrough_attrs(attrs: dict[str, str]) -> str:
    """Return a string of attributes to preserve on the replacement <g>."""
    keep: list[str] = []
    for name, value in attrs.items():
        if name in _DROP_ATTRS or name in ("x", "y", "fill", "transform"):
            continue
        keep.append(f'{name}="{value}"')
    return " ".join(keep)
