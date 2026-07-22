"""Section header (number badge + title) placement.

The header is drawn at a section box's top-left corner by default.  When a route
enters the box through an edge under the header it would cross the title text.
:func:`resolve_section_header_placement` picks a position that keeps the header
clear of routes, never moving a route to do so, following the priority chain:

1. ``above`` - the default top-left position, when the top edge is clear.
2. ``below`` - mirror at the bottom-left, when the top is blocked but the bottom
   is clear.
3. ``left`` / ``right`` - the title rotated to read down a vertical edge, when
   both horizontal edges are blocked but a side is clear and the title fits.
4. ``nudge`` - the top-left header shifted right past the clashing routes, as a
   last resort that always clears.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

from nf_metro.layout.constants import SAME_COORD_TOLERANCE
from nf_metro.parser.model import MetroGraph, Section
from nf_metro.render.constants import (
    HEADER_WRAP_CLEARANCE,
    SECTION_HEADER_ROUTE_PAD,
    SECTION_HEADER_SIDE_GAP,
    SECTION_LABEL_CHAR_WIDTH_RATIO,
    SECTION_LABEL_HALF_HEIGHT_RATIO,
    SECTION_LABEL_LINE_HEIGHT_RATIO,
    SECTION_LABEL_TEXT_OFFSET,
    SECTION_NUM_CIRCLE_R_LARGE,
    SECTION_NUM_Y_OFFSET,
    TITLE_Y_OFFSET,
)

Rect = tuple[float, float, float, float]
Polyline = list[tuple[float, float]]
HeaderMode = Literal["above", "below", "left", "right", "nudge"]


@dataclass(frozen=True)
class SectionHeaderPlacement:
    """Resolved drawing geometry for one section's header.

    ``badge_*`` locate the numbered circle; ``label_*`` locate the title text.
    ``label_rotation`` is 0 for the horizontal positions and 90 for the rotated
    side positions (title reads top-to-bottom).  ``label_lines`` is the title
    split onto separate lines when it would otherwise overhang the section box
    (a rotated header is never split); ``label_y`` is the topmost line's
    position, with each later line drawn :func:`header_line_height` further
    down.  The builder that resolves ``label_y`` (see ``_above``/``_below``/
    ``_nudge``) places the block so the extra lines always grow away from the
    section box rather than toward it, never crossing the box's own border.
    ``keepout`` is the union bbox of badge and title (all lines) used by the
    render-time guard.  ``height_capped`` is True when the wrap wanted more
    lines than fit before the nearest obstruction in its growth direction (the
    map title, another section's box, or the canvas edge) and was forced onto
    fewer, wider lines instead (see :func:`_wrapped_header_geometry`); such a
    header is exempt from :func:`check_section_headers_fit_box_width`, since a
    bounded width overhang is preferable to overlapping something else.
    """

    mode: HeaderMode
    badge_cx: float
    badge_cy: float
    label_x: float
    label_y: float
    label_rotation: float
    label_lines: tuple[str, ...]
    keepout: Rect
    height_capped: bool = False


def estimate_section_label_width(name: str, font_size: float) -> float:
    """Estimate the rendered width of a section title in pixels."""
    return len(name) * font_size * SECTION_LABEL_CHAR_WIDTH_RATIO


def _badge_span() -> float:
    """Horizontal room the number badge and its text gap occupy."""
    return 2.0 * SECTION_NUM_CIRCLE_R_LARGE + SECTION_LABEL_TEXT_OFFSET


def _header_length(name: str, font_size: float) -> float:
    """Length of the header (badge + gap + title) along its reading axis."""
    if not name:
        return 2.0 * SECTION_NUM_CIRCLE_R_LARGE
    return _badge_span() + estimate_section_label_width(name, font_size)


def header_line_height(font_size: float) -> float:
    """Pixel spacing between stacked lines of a wrapped section title."""
    return font_size * SECTION_LABEL_LINE_HEIGHT_RATIO


_HEADER_HYPHEN_BREAK_RE = re.compile(r"(?<=-)(?!$)")


def _header_wrap_tokens(name: str) -> list[tuple[str, bool]]:
    """Split ``name`` into ``(text, needs_space_before)`` pairs for line-wrapping.

    A run of whitespace is a break with a leading space on the next piece; an
    existing hyphen is a break with no leading space, since the hyphen itself
    already joins the two halves visually - splitting "Pre-processing" at its
    own hyphen rather than mid-syllable.
    """
    tokens: list[tuple[str, bool]] = []
    for i, word in enumerate(name.split()):
        pieces = [p for p in _HEADER_HYPHEN_BREAK_RE.split(word) if p]
        for j, piece in enumerate(pieces):
            tokens.append((piece, j == 0 and i > 0))
    return tokens


def _pack_lines(name: str, font_size: float, max_width: float) -> list[str]:
    """Word-wrap ``name`` so each line's estimated width fits ``max_width``.

    Greedily packs whitespace/hyphen-delimited tokens (see
    :func:`_header_wrap_tokens`) onto lines.  Never splits a token mid-word: a
    single token wider than ``max_width`` gets a line to itself, left whole,
    rather than being hyphenated to fit.
    """
    lines: list[str] = []
    current = ""
    for text, needs_space in _header_wrap_tokens(name):
        sep = " " if needs_space and current else ""
        candidate = f"{current}{sep}{text}"
        if current and estimate_section_label_width(candidate, font_size) > max_width:
            lines.append(current)
            current = text
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _wrap_header_lines(
    name: str, font_size: float, max_width: float, max_lines: int
) -> tuple[list[str], bool]:
    """Pack ``name`` onto lines fitting ``max_width``, capped at ``max_lines``.

    Widens the packing width in steps until the result fits within
    ``max_lines`` when the natural wrap needs more (trading line width for
    line count).  Returns ``(lines, was_widened)``; a widened result may
    overhang ``max_width``, which is why the caller marks it ``height_capped``
    rather than letting :func:`check_section_headers_fit_box_width` treat it
    as an authoring mistake.
    """
    lines = _pack_lines(name, font_size, max_width)
    if len(lines) <= max_lines:
        return lines, False

    # A single line at the title's own full width never needs a break, so this
    # loop is bounded: doubling the step whenever an attempt fails to shrink
    # the line count guarantees it reaches ``full_width`` (1 line) eventually.
    full_width = estimate_section_label_width(name, font_size)
    width = max_width
    step = max(font_size, 8.0)
    while len(lines) > max_lines and width < full_width:
        width += step
        candidate = _pack_lines(name, font_size, width)
        if len(candidate) < len(lines):
            lines = candidate
        else:
            step *= 2
    return lines, True


def _bbox_cols_overlap(a: Section, b: Section) -> bool:
    """True if ``a`` and ``b``'s bounding boxes overlap horizontally."""
    a_left, a_right = a.bbox_x, a.bbox_x + a.bbox_w
    b_left, b_right = b.bbox_x, b.bbox_x + b.bbox_w
    return a_left < b_right and b_left < a_right


def _nearest_section_above(graph: MetroGraph, section: Section) -> float | None:
    """bbox_bottom of the closest other (x-overlapping) section whose box sits
    above ``section``, or None when there is no such section."""
    best: float | None = None
    for other in graph.sections.values():
        if other.id == section.id or other.bbox_w <= 0 or other.bbox_h <= 0:
            continue
        other_bottom = other.bbox_y + other.bbox_h
        if other_bottom > section.bbox_y + SAME_COORD_TOLERANCE:
            continue
        if not _bbox_cols_overlap(other, section):
            continue
        best = other_bottom if best is None else max(best, other_bottom)
    return best


def _nearest_section_below(graph: MetroGraph, section: Section) -> float | None:
    """bbox_y of the closest other (x-overlapping) section whose box sits
    below ``section``, or None when there is no such section."""
    box_bottom = section.bbox_y + section.bbox_h
    best: float | None = None
    for other in graph.sections.values():
        if other.id == section.id or other.bbox_w <= 0 or other.bbox_h <= 0:
            continue
        if other.bbox_y < box_bottom - SAME_COORD_TOLERANCE:
            continue
        if not _bbox_cols_overlap(other, section):
            continue
        best = other.bbox_y if best is None else min(best, other.bbox_y)
    return best


_UNBOUNDED_WRAP_LINES = 1_000_000
"""Sentinel meaning a header's growth direction has no obstruction to bound
against - larger than any real title could ever wrap onto."""


def _lines_for_room(available: float, font_size: float) -> int:
    """Most lines that fit within ``available`` px of :func:`header_line_height`."""
    if available <= 0:
        return 1
    return 1 + int(available / header_line_height(font_size))


def _single_line_protrusion(font_size: float) -> float:
    """Vertical room a single-line header already occupies past the box edge
    (badge radius + gap + half the text's own height), before any wrapping."""
    return (
        SECTION_NUM_CIRCLE_R_LARGE
        + SECTION_NUM_Y_OFFSET
        + SECTION_LABEL_HALF_HEIGHT_RATIO * font_size
    )


def _max_lines_upward(
    graph: MetroGraph,
    section: Section,
    title_font_size: float | None,
    font_size: float,
) -> int:
    """Most lines an ``above``/``nudge`` header can grow to before reaching
    the map title, another section's box, or the canvas top."""
    ceiling = 0.0
    if title_font_size is not None and graph.title:
        # A quarter of the title's font size approximates its descender depth
        # below the baseline at ``TITLE_Y_OFFSET`` (mirrors the layout side's
        # TITLE_BAND_BOTTOM, calibrated the same way for a fixed title size).
        ceiling = max(ceiling, TITLE_Y_OFFSET + title_font_size * 0.25)
    above_bottom = _nearest_section_above(graph, section)
    if above_bottom is not None:
        ceiling = max(ceiling, above_bottom)
    available = (
        section.bbox_y
        - _single_line_protrusion(font_size)
        - ceiling
        - HEADER_WRAP_CLEARANCE
    )
    return _lines_for_room(available, font_size)


def _max_lines_downward(graph: MetroGraph, section: Section, font_size: float) -> int:
    """Most lines a ``below`` header can grow to before reaching another
    section's box below it; unbounded when there is none, since the canvas
    grows to fit."""
    below_top = _nearest_section_below(graph, section)
    if below_top is None:
        return _UNBOUNDED_WRAP_LINES
    box_bottom = section.bbox_y + section.bbox_h
    available = (
        below_top
        - box_bottom
        - _single_line_protrusion(font_size)
        - HEADER_WRAP_CLEARANCE
    )
    return _lines_for_room(available, font_size)


def _wrapped_header_geometry(
    name: str,
    font_size: float,
    bbox_w: float,
    single_line_length: float,
    max_lines: int,
) -> tuple[list[str], float, float, bool]:
    """Header lines, horizontal length, extra block height, and height-capped flag.

    Wraps the title onto additional lines only when the single-line header
    would overhang ``bbox_w``; an unwrapped header returns one line with no
    added height.  The horizontal length shrinks to whatever the widest
    wrapped line actually measures, except when the wrap is capped at
    ``max_lines`` (see :func:`_wrap_header_lines`): a capped wrap can overhang
    ``bbox_w`` when the title needs more lines than fit before the nearest
    obstruction in its growth direction.
    """
    if not name or single_line_length <= bbox_w:
        return [name], single_line_length, 0.0, False
    badge_span = _badge_span()
    available_width = max(bbox_w - badge_span, 1.0)
    lines, height_capped = _wrap_header_lines(
        name, font_size, available_width, max_lines
    )
    text_width = max(estimate_section_label_width(line, font_size) for line in lines)
    extra_height = (len(lines) - 1) * header_line_height(font_size)
    return lines, badge_span + text_width, extra_height, height_capped


def resolve_section_header_placement(
    graph: MetroGraph,
    section: Section,
    label_font_size: float,
    polylines: list[Polyline] | None = None,
    title_font_size: float | None = None,
) -> SectionHeaderPlacement:
    """Pick a clash-free position for ``section``'s header (see module docstring).

    Each candidate position is tested against the actual routed ``polylines`` so
    a line crossing the header band - whether it enters through an edge port or
    merely skirts the box - forces a relocation.  With no polylines supplied the
    default above-left position is returned (used only where routes are not yet
    available).  ``title_font_size`` sizes the map title's clearance band for an
    ``above``/``nudge`` header wrapping upward; omit it for an untitled map or
    when the caller doesn't know the theme yet."""
    circle_r = SECTION_NUM_CIRCLE_R_LARGE
    num_y = SECTION_NUM_Y_OFFSET
    gap = SECTION_HEADER_SIDE_GAP

    x0 = section.bbox_x
    y0 = section.bbox_y
    box_bottom = section.bbox_y + section.bbox_h
    box_right = section.bbox_x + section.bbox_w
    half_text = SECTION_LABEL_HALF_HEIGHT_RATIO * label_font_size

    # A rotated side header runs down the box height and is never wrapped; a
    # horizontal header wraps onto extra lines instead of overhanging bbox_w,
    # growing away from the box - upward for above/nudge, downward for below -
    # capped at however many lines fit before whatever is nearest that way.
    side_length = _header_length(section.name, label_font_size)
    up_max_lines = _max_lines_upward(graph, section, title_font_size, label_font_size)
    lines, length, extra_height, height_capped = _wrapped_header_geometry(
        section.name, label_font_size, section.bbox_w, side_length, up_max_lines
    )

    above = _above(
        x0, y0, circle_r, num_y, length, half_text, lines, extra_height, height_capped
    )
    if polylines is None:
        return above

    down_max_lines = _max_lines_downward(graph, section, label_font_size)
    lines_dn, length_dn, extra_dn, capped_dn = _wrapped_header_geometry(
        section.name, label_font_size, section.bbox_w, side_length, down_max_lines
    )

    # The left column additionally needs room between the box and the canvas origin.
    side_fits = side_length <= section.bbox_h
    candidates = [
        above,
        _below(
            x0,
            box_bottom,
            circle_r,
            num_y,
            length_dn,
            half_text,
            lines_dn,
            extra_dn,
            capped_dn,
        ),
    ]
    if side_fits and x0 - gap - 2.0 * circle_r >= 0.0:
        candidates.append(_left(x0, y0, circle_r, gap, side_length, section.name))
    if side_fits:
        candidates.append(
            _right(box_right, y0, circle_r, gap, side_length, section.name)
        )

    for candidate in candidates:
        if _placement_clear(candidate, polylines):
            return candidate
    return _nudge(
        x0,
        y0,
        circle_r,
        num_y,
        length,
        half_text,
        lines,
        extra_height,
        height_capped,
        above,
        polylines,
    )


def resolve_all_section_headers(
    graph: MetroGraph,
    label_font_size: float,
    polylines: list[Polyline],
    title_font_size: float | None = None,
) -> dict[str, SectionHeaderPlacement]:
    """Resolve every drawn section's header placement once, keyed by section id."""
    return {
        section.id: resolve_section_header_placement(
            graph, section, label_font_size, polylines, title_font_size
        )
        for section in graph.sections.values()
        if section.bbox_w > 0 and section.bbox_h > 0 and not section.is_implicit
    }


def _placement_clear(
    placement: SectionHeaderPlacement, polylines: list[Polyline]
) -> bool:
    """True if no routed line comes within ``SECTION_HEADER_ROUTE_PAD`` of the
    placement's header region."""
    pad = SECTION_HEADER_ROUTE_PAD
    return not any(
        _segment_hits_rect(poly[i], poly[i + 1], placement.keepout, -pad)
        for poly in polylines
        for i in range(len(poly) - 1)
    )


def _above(
    x0: float,
    y0: float,
    circle_r: float,
    num_y: float,
    length: float,
    half_text: float,
    lines: list[str],
    extra_height: float,
    height_capped: bool,
) -> SectionHeaderPlacement:
    cx = x0 + circle_r
    cy = y0 - circle_r - num_y
    return SectionHeaderPlacement(
        mode="above",
        badge_cx=cx,
        badge_cy=cy - extra_height / 2.0,
        label_x=cx + circle_r + SECTION_LABEL_TEXT_OFFSET,
        label_y=cy - extra_height,
        label_rotation=0.0,
        label_lines=tuple(lines),
        keepout=(x0, cy - half_text - extra_height, x0 + length, y0),
        height_capped=height_capped,
    )


def _below(
    x0: float,
    box_bottom: float,
    circle_r: float,
    num_y: float,
    length: float,
    half_text: float,
    lines: list[str],
    extra_height: float,
    height_capped: bool,
) -> SectionHeaderPlacement:
    cx = x0 + circle_r
    cy = box_bottom + circle_r + num_y
    return SectionHeaderPlacement(
        mode="below",
        badge_cx=cx,
        badge_cy=cy + extra_height / 2.0,
        label_x=cx + circle_r + SECTION_LABEL_TEXT_OFFSET,
        label_y=cy,
        label_rotation=0.0,
        label_lines=tuple(lines),
        keepout=(x0, box_bottom, x0 + length, cy + half_text + extra_height),
        height_capped=height_capped,
    )


def _left(
    x0: float,
    y0: float,
    circle_r: float,
    gap: float,
    length: float,
    name: str,
) -> SectionHeaderPlacement:
    col_x = x0 - gap - circle_r
    cy = y0 + circle_r
    return SectionHeaderPlacement(
        mode="left",
        badge_cx=col_x,
        badge_cy=cy,
        label_x=col_x,
        label_y=cy + circle_r + SECTION_LABEL_TEXT_OFFSET,
        label_rotation=90.0,
        label_lines=(name,),
        keepout=(col_x - circle_r, y0, x0, y0 + length),
    )


def _right(
    box_right: float,
    y0: float,
    circle_r: float,
    gap: float,
    length: float,
    name: str,
) -> SectionHeaderPlacement:
    col_x = box_right + gap + circle_r
    cy = y0 + circle_r
    return SectionHeaderPlacement(
        mode="right",
        badge_cx=col_x,
        badge_cy=cy,
        label_x=col_x,
        label_y=cy + circle_r + SECTION_LABEL_TEXT_OFFSET,
        label_rotation=90.0,
        label_lines=(name,),
        keepout=(box_right, y0, col_x + circle_r, y0 + length),
    )


def _nudge(
    x0: float,
    y0: float,
    circle_r: float,
    num_y: float,
    length: float,
    half_text: float,
    lines: list[str],
    extra_height: float,
    height_capped: bool,
    above: SectionHeaderPlacement,
    polylines: list[Polyline],
) -> SectionHeaderPlacement:
    """Shift the above-left header right until it clears every route crossing
    the band it would occupy.  Always clears, at the cost of a header that may
    overhang the box to the right.

    The band is unbounded to the right, not clipped to the un-nudged header's
    box-width extent: the shifted header occupies ``[start, start + length]``,
    so a route crossing the header's vertical band anywhere to the right of the
    box must be stepped past.  Sweeping the band unbounded to the right
    guarantees the placed header sits past every route, which is what makes
    nudge a true last-resort clear."""
    pad = SECTION_HEADER_ROUTE_PAD
    bx0, by0, _, by1 = above.keepout
    band = (bx0 - pad, by0 - pad, math.inf, by1 + pad)
    start = x0
    for poly in polylines:
        for i in range(len(poly) - 1):
            span = _segment_rect_xspan(poly[i], poly[i + 1], band)
            if span is not None:
                start = max(start, span + pad)
    cx = start + circle_r
    cy = y0 - circle_r - num_y
    return SectionHeaderPlacement(
        mode="nudge",
        badge_cx=cx,
        badge_cy=cy - extra_height / 2.0,
        label_x=cx + circle_r + SECTION_LABEL_TEXT_OFFSET,
        label_y=cy - extra_height,
        label_rotation=0.0,
        label_lines=tuple(lines),
        keepout=(start, cy - half_text - extra_height, start + length, y0),
        height_capped=height_capped,
    )


class SectionHeaderClashError(RuntimeError):
    """A section header was placed over a routed line.

    Raised on the render path so the placement chain can never silently draw a
    title across a metro line, independent of ``compute_layout``'s validation.
    """


class SectionHeaderOverflowError(RuntimeError):
    """A section header's wrapped title overhangs its box width.

    Wrapping (see :func:`_wrapped_header_geometry`) keeps a horizontal
    header's rendered width within ``bbox_w`` except when a single word can't
    be broken further than the wrap floor; this is the render-time safety net
    for that residual case, independent of ``compute_layout``'s validation.
    """


@dataclass(frozen=True)
class HeaderRouteClash:
    """A routed line crosses a section header's text/badge region."""

    section_id: str
    mode: str
    keepout: Rect

    def message(self) -> str:
        return (
            f"section '{self.section_id}' header (placed '{self.mode}') overlaps a "
            f"route inside {tuple(round(c, 1) for c in self.keepout)}"
        )


def _clip_segment(
    p0: tuple[float, float], p1: tuple[float, float], rect: Rect
) -> tuple[float, float] | None:
    """Liang-Barsky clip of segment ``p0``-``p1`` against ``rect``; returns the
    ``(t_lo, t_hi)`` parameter range inside the rect, or ``None`` if it misses."""
    rx0, ry0, rx1, ry1 = rect
    if rx1 <= rx0 or ry1 <= ry0:
        return None
    x0, y0 = p0
    dx = p1[0] - x0
    dy = p1[1] - y0
    t_lo, t_hi = 0.0, 1.0
    for p, q in ((-dx, x0 - rx0), (dx, rx1 - x0), (-dy, y0 - ry0), (dy, ry1 - y0)):
        if p == 0:
            if q < 0:
                return None
            continue
        t = q / p
        if p < 0:
            t_lo = max(t_lo, t)
        else:
            t_hi = min(t_hi, t)
        if t_lo > t_hi:
            return None
    return t_lo, t_hi


def _segment_hits_rect(
    p0: tuple[float, float],
    p1: tuple[float, float],
    rect: Rect,
    margin: float,
) -> bool:
    """True if segment ``p0``-``p1`` enters ``rect`` inset by ``margin`` on every
    side (negative ``margin`` expands), so a route merely tangent to the keepout
    boundary does not count."""
    inset = (
        rect[0] + margin,
        rect[1] + margin,
        rect[2] - margin,
        rect[3] - margin,
    )
    return _clip_segment(p0, p1, inset) is not None


def _segment_rect_xspan(
    p0: tuple[float, float], p1: tuple[float, float], rect: Rect
) -> float | None:
    """Largest X at which segment ``p0``-``p1`` lies inside ``rect``, or ``None``."""
    clip = _clip_segment(p0, p1, rect)
    if clip is None:
        return None
    t_lo, t_hi = clip
    return max(p0[0] + t_lo * (p1[0] - p0[0]), p0[0] + t_hi * (p1[0] - p0[0]))


def check_section_headers_clear_routes(
    placements: dict[str, SectionHeaderPlacement],
    polylines: list[Polyline],
    margin: float = 2.0,
) -> list[HeaderRouteClash]:
    """Report every section whose resolved header region a routed line crosses."""
    clashes: list[HeaderRouteClash] = []
    for section_id, placement in placements.items():
        rect = placement.keepout
        for poly in polylines:
            if any(
                _segment_hits_rect(poly[i], poly[i + 1], rect, margin)
                for i in range(len(poly) - 1)
            ):
                clashes.append(HeaderRouteClash(section_id, placement.mode, rect))
                break
    return clashes


def check_section_headers_fit_box_width(
    graph: MetroGraph,
    placements: dict[str, SectionHeaderPlacement],
    tolerance: float = 0.5,
) -> list[str]:
    """Report every section whose horizontal header overhangs its box width.

    A ``nudge`` header is exempt: it deliberately overhangs the box to clear a
    route ahead of it (see :func:`_nudge`).  A rotated (``left``/``right``)
    header reads down the box height rather than across its width, so it is
    exempt too.  A ``height_capped`` header is exempt as well: it traded extra
    width for fewer lines to stay clear of whatever bounded its growth
    direction (see :func:`_wrapped_header_geometry`).  A single line with no
    space to break at (one word, or a word joined by an existing hyphen the
    wrap already used) is exempt too: the title is never split mid-word (see
    :func:`_pack_lines`), so a lone long word has no further way to narrow.
    """
    overflowing: list[str] = []
    for section_id, placement in placements.items():
        if placement.label_rotation or placement.mode == "nudge":
            continue
        if placement.height_capped:
            continue
        if len(placement.label_lines) == 1 and " " not in placement.label_lines[0]:
            continue
        section = graph.sections.get(section_id)
        if section is None:
            continue
        header_width = placement.keepout[2] - placement.keepout[0]
        if header_width > section.bbox_w + tolerance:
            overflowing.append(section_id)
    return overflowing
