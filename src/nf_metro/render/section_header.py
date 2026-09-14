"""Section header (number badge + title) placement.

The header is drawn at a section box's top-left corner by default.  When a route
enters the box through an edge under the header it would cross the title text.
:func:`resolve_section_header_placement` picks a position that keeps the header
clear of routes, never moving a route to do so:

1. ``above`` - the default top-left position - wins outright whenever it is
   available.  It is the position a reader looks for, so an uncontested default
   is never traded away and the band above the box needs no other candidate
   scored against it.
2. Otherwise the caption has already left that corner, so neither upright
   position has a claim to priority over the other.  What is left to compare is
   how much air each keeps, so both are scored by :func:`_route_clearance` and
   the roomiest wins:

   - ``nudge`` - the header shifted along the band above the box to the middle
     of one of the route-clear slots that band offers, bounded to the section's
     own box width, which is what keeps it readable as that box's label.
   - ``below`` - the mirror position at the bottom-left.

   Exact ties go to a band slot, so a band as roomy as the bottom edge keeps the
   caption above its box.  A slot is scored at the middle of the gap it sits in
   rather than at the first position that clears, because the leftmost clear
   shift keeps exactly ``SECTION_HEADER_ROUTE_PAD`` from the stroke it stepped
   past and would lose every comparison before it was made.
3. ``left`` / ``right`` - the title rotated to run down a vertical edge, when
   neither upright position is available.  A sideways title is harder to read
   than either of them, so it is not their peer to be scored against and stays a
   lower tier.  ``left`` anchors the badge at the bottom of the edge and reads
   upward; ``right`` anchors it at the top and reads downward.  A title longer
   than the box height overhangs past the box ends rather than being ruled out,
   since a rotated header is never wrapped.
4. ``nudge`` unbounded - the last resort when nothing else is available; it
   always clears, at the cost of overhanging the box to the right.

A candidate is available when it clears every route by
``SECTION_HEADER_ROUTE_PAD`` and its ink fits :func:`header_band_room`, the room
the layout leaves free on the side it hangs off.  That second condition is what
makes the caption's position accountable wherever it lands:
:func:`check_section_headers_hold_the_reserved_band` reads the band back off the
placement, so the band stated for a caption is the one on the side the caption
took rather than a fixed band above ``bbox_y``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, NamedTuple

from nf_metro.layout.constants import SAME_COORD_TOLERANCE, SECTION_HEADER_PROTRUSION
from nf_metro.layout.geometry import point_to_polyline_distance
from nf_metro.parser.model import MetroGraph, Section
from nf_metro.render.constants import (
    HEADER_WRAP_CLEARANCE,
    SECTION_HEADER_ROUTE_PAD,
    SECTION_HEADER_SIDE_GAP,
    SECTION_LABEL_HALF_HEIGHT_RATIO,
    SECTION_LABEL_TEXT_OFFSET,
    SECTION_NUM_CIRCLE_R_LARGE,
    SECTION_NUM_Y_OFFSET,
    title_baseline_y,
)
from nf_metro.text_metrics import DEFAULT_TEXT_METRICS, TextRole, text_style

Rect = tuple[float, float, float, float]
Polyline = list[tuple[float, float]]
HeaderMode = Literal["above", "below", "left", "right", "nudge"]


class _BandBlock(NamedTuple):
    """The badge and wrapped-title measurements a band placement is built from."""

    circle_r: float
    num_y: float
    length: float
    half_text: float
    lines: list[str]
    extra_height: float
    height_capped: bool


@dataclass(frozen=True)
class SectionHeaderPlacement:
    """Resolved drawing geometry for one section's header.

    ``badge_*`` locate the numbered circle; ``label_*`` locate the title text.
    ``label_rotation`` is 0 for the horizontal positions, 90 for ``right``
    (title reads top-to-bottom) and 270 for ``left`` (title reads bottom-to-top,
    badge at the bottom of the edge).  ``label_lines`` is the title
    split onto separate lines when it would otherwise overhang the section box
    (a rotated header is never split); ``label_y`` is the topmost line's
    position, with each later line drawn :func:`header_line_height` further
    down.  The builder that resolves ``label_y`` (see ``_above``/``_below``/
    ``_band_shift``) places the block so the extra lines always grow away from the
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


class _Scored(NamedTuple):
    """One available candidate with the air it keeps and its listed position."""

    clearance: float
    rank: int
    placement: SectionHeaderPlacement


def estimate_section_label_width(name: str, font_size: float) -> float:
    """Estimate the rendered width of a section title in pixels."""
    style = text_style(font_size, "bold")
    return DEFAULT_TEXT_METRICS.reserve_width(name, style, TextRole.SECTION_HEADER)


def _widest_label_line(lines: Sequence[str], font_size: float) -> str:
    """The wrapped title line with the greatest estimated width."""
    return max(
        lines,
        key=lambda line: estimate_section_label_width(line, font_size),
        default="",
    )


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
    return DEFAULT_TEXT_METRICS.line_height(
        text_style(font_size, "bold"), TextRole.SECTION_HEADER
    )


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


def _bbox_rows_overlap(a: Section, b: Section) -> bool:
    """True if ``a`` and ``b``'s bounding boxes overlap vertically."""
    a_top, a_bottom = a.bbox_y, a.bbox_y + a.bbox_h
    b_top, b_bottom = b.bbox_y, b.bbox_y + b.bbox_h
    return a_top < b_bottom and b_top < a_bottom


def _nearest_section_beside(
    graph: MetroGraph, section: Section, sign: float
) -> float | None:
    """Facing edge X of the closest other (y-overlapping) section whose box sits
    on ``section``'s *sign* side, or None when there is no such section."""
    here = section.bbox_x + section.bbox_w if sign > 0 else section.bbox_x
    best: float | None = None
    for other in graph.sections.values():
        if other.id == section.id or other.bbox_w <= 0 or other.bbox_h <= 0:
            continue
        facing = other.bbox_x if sign > 0 else other.bbox_x + other.bbox_w
        if (facing - here) * sign < -SAME_COORD_TOLERANCE:
            continue
        if not _bbox_rows_overlap(other, section):
            continue
        best = facing if best is None else (max if sign < 0 else min)(best, facing)
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


def _upward_ceiling(
    graph: MetroGraph, section: Section, title_font_size: float | None
) -> float:
    """Y of the lowest thing standing above ``section``'s box: the map title,
    another section's box, or the canvas top."""
    ceiling = 0.0
    if title_font_size is not None and graph.title:
        # A quarter of the title's font size approximates its descender depth
        # below the baseline (mirrors the layout side's TITLE_BAND_BOTTOM,
        # calibrated the same way for a fixed title size).
        ceiling = max(
            ceiling, title_baseline_y(title_font_size) + title_font_size * 0.25
        )
    above_bottom = _nearest_section_above(graph, section)
    if above_bottom is not None:
        ceiling = max(ceiling, above_bottom)
    return ceiling


class _BandQuery(NamedTuple):
    """The placed geometry one side's free room is read from."""

    graph: MetroGraph
    section: Section
    label_font_size: float
    title_font_size: float | None


def _top_edge_room(query: _BandQuery) -> float:
    return max(
        SECTION_HEADER_PROTRUSION,
        _single_line_protrusion(query.label_font_size),
        query.section.bbox_y
        - _upward_ceiling(query.graph, query.section, query.title_font_size),
    )


def _top_edge_protrusion(section: Section, keepout: Rect) -> float:
    return section.bbox_y - keepout[1]


def _bottom_edge_room(query: _BandQuery) -> float:
    below_top = _nearest_section_below(query.graph, query.section)
    if below_top is None:
        return float("inf")
    section = query.section
    return below_top - SECTION_HEADER_PROTRUSION - (section.bbox_y + section.bbox_h)


def _bottom_edge_protrusion(section: Section, keepout: Rect) -> float:
    return keepout[3] - (section.bbox_y + section.bbox_h)


def _left_edge_room(query: _BandQuery) -> float:
    beside = _nearest_section_beside(query.graph, query.section, -1.0)
    left = query.section.bbox_x
    return left if beside is None else left - beside


def _left_edge_protrusion(section: Section, keepout: Rect) -> float:
    return section.bbox_x - keepout[0]


def _right_edge_room(query: _BandQuery) -> float:
    beside = _nearest_section_beside(query.graph, query.section, 1.0)
    right = query.section.bbox_x + query.section.bbox_w
    return float("inf") if beside is None else beside - right


def _right_edge_protrusion(section: Section, keepout: Rect) -> float:
    return keepout[2] - (section.bbox_x + section.bbox_w)


class _HeaderSide(NamedTuple):
    """One box edge a header hangs off, read from both directions.

    Pairing the two readings is what keeps them describing the same edge: the
    room the layout leaves beside it and the reach of the ink placed there.
    """

    room: Callable[[_BandQuery], float]
    protrusion: Callable[[Section, Rect], float]


_TOP_EDGE = _HeaderSide(_top_edge_room, _top_edge_protrusion)

_HEADER_SIDES: dict[HeaderMode, _HeaderSide] = {
    "above": _TOP_EDGE,
    "nudge": _TOP_EDGE,
    "below": _HeaderSide(_bottom_edge_room, _bottom_edge_protrusion),
    "left": _HeaderSide(_left_edge_room, _left_edge_protrusion),
    "right": _HeaderSide(_right_edge_room, _right_edge_protrusion),
}
"""The box edge each mode hangs its header off.

``above`` and ``nudge`` share the top edge: a nudged caption is the default one
slid along that same band."""


def header_band_room(
    graph: MetroGraph,
    section: Section,
    mode: HeaderMode,
    label_font_size: float,
    title_font_size: float | None = None,
) -> float:
    """Depth of the band the layout leaves free on the side ``mode`` hangs off.

    The band a caption occupies is not a fixed strip above ``bbox_y``: a wrapped
    title grows away from the box until it meets whatever stands that way, and a
    caption on another side hangs into that side's gap instead.  This states the
    room on each side from the placed geometry, so a caption's claim can be read
    off its own placement:

    ``above``/``nudge``
        down from whatever stands above the box (see :func:`_upward_ceiling`) to
        ``bbox_y``, but never less than the default position's own reach.  The top
        side is the one the layout keeps a caption's room on unconditionally -
        ``SECTION_HEADER_PROTRUSION``, which ``assert_render_header_clearance``
        gates - so it holds what is drawn there whatever else the geometry says: a
        title band calibrated for a fixed font can land below a box top placed
        against other content, and the reservation itself is calibrated for the
        default label size rather than the scaled one actually drawn.
    ``below``
        the inter-row gap under the box, less the ``SECTION_HEADER_PROTRUSION``
        the section below reserves for its own badge.  Unbounded with nothing
        below, since the canvas grows downward to fit.
    ``left``/``right``
        the inter-column gap beside the box.  Unbounded to the right for the same
        reason; to the left it stops at the canvas edge.
    """
    return _HEADER_SIDES[mode].room(
        _BandQuery(graph, section, label_font_size, title_font_size)
    )


def header_band_protrusion(
    section: Section, placement: SectionHeaderPlacement
) -> float:
    """How far ``placement``'s ink reaches past the box edge it hangs off."""
    return _HEADER_SIDES[placement.mode].protrusion(section, placement.keepout)


def _fits_its_band(
    graph: MetroGraph,
    section: Section,
    placement: SectionHeaderPlacement,
    label_font_size: float,
    title_font_size: float | None,
) -> bool:
    """True when the caption's protrusion is within the room its own side has."""
    room = header_band_room(
        graph, section, placement.mode, label_font_size, title_font_size
    )
    return header_band_protrusion(section, placement) <= room + SAME_COORD_TOLERANCE


def _max_lines_upward(
    graph: MetroGraph,
    section: Section,
    title_font_size: float | None,
    font_size: float,
) -> int:
    """Most lines an ``above``/``nudge`` header can grow to before reaching
    the map title, another section's box, or the canvas top."""
    available = (
        section.bbox_y
        - _single_line_protrusion(font_size)
        - _upward_ceiling(graph, section, title_font_size)
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
    text_width = estimate_section_label_width(
        _widest_label_line(lines, font_size), font_size
    )
    extra_height = (len(lines) - 1) * header_line_height(font_size)
    return lines, badge_span + text_width, extra_height, height_capped


def _band_slot_placements(
    section: Section,
    above: SectionHeaderPlacement,
    block: _BandBlock,
    polylines: list[Polyline],
) -> list[SectionHeaderPlacement]:
    """One header per route-clear slot the band above ``section`` offers, each
    centred in its slot and wholly inside the box width.

    Bounding a slot to the box width is what lets the band be ranked without
    consulting neighbours: two headers so bounded cannot meet, since boxes in a
    grid row do not overlap horizontally and boxes in different rows hang off
    different edges.
    """
    pad = SECTION_HEADER_ROUTE_PAD
    length = block.length
    x_lo = section.bbox_x
    x_hi = section.bbox_x + section.bbox_w
    _, band_top, _, band_bottom = above.keepout
    band = (x_lo - pad, band_top - pad, x_hi + pad, band_bottom + pad)
    blocked = sorted(
        span
        for poly in polylines
        for i in range(len(poly) - 1)
        if (span := _segment_rect_xspan(poly[i], poly[i + 1], band)) is not None
    )

    slots: list[SectionHeaderPlacement] = []
    free_from = x_lo
    for lo, hi in [*blocked, (x_hi + pad, x_hi + pad)]:
        free_to = lo - pad
        if free_to - free_from >= length:
            start = free_from + (free_to - free_from - length) / 2.0
            slots.append(_band_shift(start, section, block))
        free_from = max(free_from, hi + pad)
    return [slot for slot in slots if _placement_clear(slot, polylines)]


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

    # A rotated side header runs down a vertical edge and is never wrapped; a
    # horizontal header wraps onto extra lines instead of overhanging bbox_w,
    # growing away from the box - upward for above/nudge, downward for below -
    # capped at however many lines fit before whatever is nearest that way.
    side_length = _header_length(section.name, label_font_size)
    up_max_lines = _max_lines_upward(graph, section, title_font_size, label_font_size)
    lines, length, extra_height, height_capped = _wrapped_header_geometry(
        section.name, label_font_size, section.bbox_w, side_length, up_max_lines
    )
    block = _BandBlock(
        circle_r, num_y, length, half_text, lines, extra_height, height_capped
    )
    above = _above(x0, y0, block)
    if polylines is None or _placement_clear(above, polylines):
        return above

    down_max_lines = _max_lines_downward(graph, section, label_font_size)
    lines_dn, length_dn, extra_dn, capped_dn = _wrapped_header_geometry(
        section.name, label_font_size, section.bbox_w, side_length, down_max_lines
    )
    down_block = _BandBlock(
        circle_r, num_y, length_dn, half_text, lines_dn, extra_dn, capped_dn
    )

    # A rotated side header needs the box only as tall as its badge; an overlong
    # title overhangs past the box ends rather than being ruled out (see module
    # docstring). ``_left`` reads upward, so it additionally needs room to the
    # left of the box and enough canvas above the box bottom for that upward
    # overhang (the canvas grows for a downward ``_right`` overhang but not an
    # upward one). ``_right`` reads downward into always-growable canvas.
    badge_diameter = 2.0 * circle_r
    side_room = section.bbox_h >= badge_diameter
    upright = [
        *_band_slot_placements(section, above, block, polylines),
        _below(x0, box_bottom, down_block),
    ]
    rotated = []
    if (
        side_room
        and x0 - gap - badge_diameter >= 0.0
        and box_bottom - side_length >= 0.0
    ):
        rotated.append(_left(x0, box_bottom, circle_r, gap, side_length, section.name))
    if side_room:
        rotated.append(_right(box_right, y0, circle_r, gap, side_length, section.name))

    def _available(candidates: list[SectionHeaderPlacement]) -> list[_Scored]:
        return [
            _Scored(_route_clearance(candidate, polylines), rank, candidate)
            for rank, candidate in enumerate(candidates)
            if _placement_clear(candidate, polylines)
            and _fits_its_band(
                graph, section, candidate, label_font_size, title_font_size
            )
        ]

    # The roomiest of the upright positions, and only then a rotated one: a
    # sideways title is harder to read than either horizontal position, so it is
    # not a peer of them to be scored against and stays a lower tier.
    scored = _available(upright) or _available(rotated)
    if scored:
        return max(scored, key=lambda item: (item.clearance, -item.rank)).placement
    return _band_shift(
        _leftmost_clear_band_start(section, above, length, polylines), section, block
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


def _above(x0: float, y0: float, block: _BandBlock) -> SectionHeaderPlacement:
    circle_r, num_y, length, half_text, lines, extra_height, height_capped = block
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


def _below(x0: float, box_bottom: float, block: _BandBlock) -> SectionHeaderPlacement:
    circle_r, num_y, length, half_text, lines, extra_height, height_capped = block
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
    box_bottom: float,
    circle_r: float,
    gap: float,
    length: float,
    name: str,
) -> SectionHeaderPlacement:
    col_x = x0 - gap - circle_r
    cy = box_bottom - circle_r
    return SectionHeaderPlacement(
        mode="left",
        badge_cx=col_x,
        badge_cy=cy,
        label_x=col_x,
        label_y=cy - circle_r - SECTION_LABEL_TEXT_OFFSET,
        label_rotation=270.0,
        label_lines=(name,),
        keepout=(col_x - circle_r, box_bottom - length, x0, box_bottom),
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


def _band_shift(
    start: float, section: Section, block: _BandBlock
) -> SectionHeaderPlacement:
    """The above-left header moved along the band to begin at ``start``."""
    circle_r, num_y, length, half_text, lines, extra_height, height_capped = block
    cx = start + circle_r
    cy = section.bbox_y - circle_r - num_y
    return SectionHeaderPlacement(
        mode="nudge",
        badge_cx=cx,
        badge_cy=cy - extra_height / 2.0,
        label_x=cx + circle_r + SECTION_LABEL_TEXT_OFFSET,
        label_y=cy - extra_height,
        label_rotation=0.0,
        label_lines=tuple(lines),
        keepout=(
            start,
            cy - half_text - extra_height,
            start + length,
            section.bbox_y,
        ),
        height_capped=height_capped,
    )


def _leftmost_clear_band_start(
    section: Section,
    above: SectionHeaderPlacement,
    length: float,
    polylines: list[Polyline],
) -> float:
    """Leftmost X along the band at which a header of ``length`` clears every
    route crossing it, however far right of the box that lands.

    The search is a fixpoint over the header's own footprint ``[start, start +
    length]``, not a single pass over the un-shifted box-width extent: stepping
    right to clear a route can slide a route that was beyond the old footprint
    into the new one, so the step is repeated against the shifted footprint
    until nothing crosses it.  That is what makes this a guaranteed clear, while
    stopping at the leftmost such position rather than sweeping past routes the
    finite-width header would never reach.
    """
    pad = SECTION_HEADER_ROUTE_PAD
    _, band_top, _, band_bottom = above.keepout
    y_lo, y_hi = band_top - pad, band_bottom + pad
    start = section.bbox_x
    while True:
        band = (start - pad, y_lo, start + length + pad, y_hi)
        spans = (
            _segment_rect_xspan(poly[i], poly[i + 1], band)
            for poly in polylines
            for i in range(len(poly) - 1)
        )
        rightmost = max((s[1] for s in spans if s is not None), default=None)
        if rightmost is None or rightmost + pad <= start:
            return start
        start = rightmost + pad


def check_section_headers_hold_the_reserved_band(
    graph: MetroGraph,
    placements: dict[str, SectionHeaderPlacement],
    label_font_size: float,
    title_font_size: float | None = None,
) -> list[str]:
    """Report every section whose caption reaches past the band its own side has.

    The band a caption claims is read off the caption: whichever side it hangs
    off, :func:`header_band_room` states the room the layout leaves there and
    :func:`header_band_protrusion` states how far the ink reaches into it.  A
    caption below or beside its box is therefore accounted for by the gap it
    actually occupies rather than measured against a band above ``bbox_y`` it was
    never going to hold.

    Reaching past that room is what would put caption ink where the layout left
    none - over the box below, or over the badge that box reserves its own room
    for - so it is the condition worth refusing a render over.  Horizontal
    overhang along the band is :func:`check_section_headers_fit_box_width`'s.
    """
    return sorted(
        section_id
        for section_id, placement in placements.items()
        if (section := graph.sections.get(section_id)) is not None
        and section.bbox_w > 0
        and section.bbox_h > 0
        and not _fits_its_band(
            graph, section, placement, label_font_size, title_font_size
        )
    )


class SectionHeaderBandError(RuntimeError):
    """A section caption reaches past the band the layout leaves on its own side.

    Raised on the render path so caption ink can never silently land where the
    layout left no room for it - over the box below, or over the badge that box
    reserves its own room for - independent of ``compute_layout``'s validation.
    """


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
) -> tuple[float, float] | None:
    """X interval over which segment ``p0``-``p1`` lies inside ``rect``, or ``None``."""
    clip = _clip_segment(p0, p1, rect)
    if clip is None:
        return None
    t_lo, t_hi = clip
    xs = (p0[0] + t_lo * (p1[0] - p0[0]), p0[0] + t_hi * (p1[0] - p0[0]))
    return min(xs), max(xs)


def _point_rect_distance(point: tuple[float, float], rect: Rect) -> float:
    """Distance from ``point`` to axis-aligned ``rect``; 0 when inside it."""
    dx = max(rect[0] - point[0], 0.0, point[0] - rect[2])
    dy = max(rect[1] - point[1], 0.0, point[1] - rect[3])
    return (dx * dx + dy * dy) ** 0.5


def _segment_rect_distance(
    p0: tuple[float, float], p1: tuple[float, float], rect: Rect
) -> float:
    """Distance between segment ``p0``-``p1`` and axis-aligned ``rect``.

    Zero when they meet.  Both are convex, so the closest pair always involves a
    vertex of one of them: checking each segment end against the rect and each
    rect corner against the segment covers every case exactly.
    """
    if _clip_segment(p0, p1, rect) is not None:
        return 0.0
    x0, y0, x1, y1 = rect
    corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    return min(
        min(_point_rect_distance(p, rect) for p in (p0, p1)),
        min(point_to_polyline_distance(c, (p0, p1)) for c in corners),
    )


def _route_clearance(
    placement: SectionHeaderPlacement, polylines: list[Polyline]
) -> float:
    """Least distance from any routed line to ``placement``'s header region.

    Infinite where no route is drawn at all, which leaves the ranking defined on
    a map whose sections carry no edges between them.
    """
    return min(
        (
            _segment_rect_distance(poly[i], poly[i + 1], placement.keepout)
            for poly in polylines
            for i in range(len(poly) - 1)
        ),
        default=float("inf"),
    )


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

    A ``nudge`` header is exempt: every slot the resolver ranks is inside the box
    width, so one that overhangs is the last resort taken when no position at all
    was available (see :func:`_leftmost_clear_band_start`).  A rotated
    (``left``/``right``) header reads down the box height rather than across its
    width, so it is exempt too.  A ``height_capped`` header is exempt as well: it
    traded extra width for fewer lines to stay clear of whatever bounded its
    growth direction (see :func:`_wrapped_header_geometry`).

    A header whose widest wrapped line is a single unbreakable token is exempt:
    :func:`_pack_lines` never splits mid-word, so such a line has no further way
    to narrow, and it is the only shape :func:`_pack_lines` can leave wider than
    the available width.  A line that carries an unused break point (a space, or
    an unsplit hyphen) is reported, since a correct wrap would have used it.

    This guard therefore does not verify that a permitted unbreakable-token
    overhang stays clear of a neighbouring section's box or header: it only
    measures against the section's own ``bbox_w``.  Lateral overlap with a
    neighbour is not checked anywhere - only route crossings
    (:func:`check_section_headers_clear_routes`) and the vertical reserved band
    (:func:`check_section_headers_hold_the_reserved_band`) are.
    """
    overflowing: list[str] = []
    for section_id, placement in placements.items():
        if placement.label_rotation or placement.mode == "nudge":
            continue
        if placement.height_capped:
            continue
        # Header width is linear in font size, so any fixed size ranks the lines
        # by relative width identically; the placement's own font size is not
        # available here.
        widest_line = _widest_label_line(placement.label_lines, 1.0)
        if len(_header_wrap_tokens(widest_line)) <= 1:
            continue
        section = graph.sections.get(section_id)
        if section is None:
            continue
        header_width = placement.keepout[2] - placement.keepout[0]
        if header_width > section.bbox_w + tolerance:
            overflowing.append(section_id)
    return overflowing
