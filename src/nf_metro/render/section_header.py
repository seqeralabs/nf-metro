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

import re
from dataclasses import dataclass
from typing import Literal

from nf_metro.layout.constants import LABEL_WRAP_MIN_LINE_CHARS
from nf_metro.parser.model import MetroGraph, Section
from nf_metro.render.constants import (
    SECTION_HEADER_ROUTE_PAD,
    SECTION_HEADER_SIDE_GAP,
    SECTION_LABEL_CHAR_WIDTH_RATIO,
    SECTION_LABEL_HALF_HEIGHT_RATIO,
    SECTION_LABEL_LINE_HEIGHT_RATIO,
    SECTION_LABEL_TEXT_OFFSET,
    SECTION_NUM_CIRCLE_R_LARGE,
    SECTION_NUM_Y_OFFSET,
    STATION_LABEL_CLEARANCE,
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
    (a rotated header is never split), stacked from ``label_y`` at
    :func:`header_line_height` spacing.  ``keepout`` is the union bbox of badge
    and title (all lines) used by the render-time guard.  ``height_capped`` is
    True when the wrap wanted more lines than fit above the section's own
    content and was forced onto fewer, wider lines instead (see
    :func:`_wrapped_header_geometry`); such a header is exempt from
    :func:`check_section_headers_fit_box_width`, since a bounded width
    overhang is preferable to colliding with the section's content.
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


def _hyphenate_balanced(word: str, budget: int) -> list[str]:
    """Split ``word`` into evenly-sized, hyphenated pieces of at most ``budget`` chars.

    Balancing the piece length - rather than greedily filling each piece to
    the budget - avoids a short orphan trailing piece, e.g. "Quantif-" /
    "ication" rather than "Quantificati-" / "on".
    """
    if len(word) <= budget:
        return [word]
    n_pieces = -(-len(word) // budget)
    piece_len = max(LABEL_WRAP_MIN_LINE_CHARS, -(-len(word) // n_pieces))
    pieces = [word[i : i + piece_len] for i in range(0, len(word), piece_len)]
    return [p + "-" for p in pieces[:-1]] + [pieces[-1]]


def _pack_lines(name: str, font_size: float, max_width: float) -> list[str]:
    """Word-wrap ``name`` so each line's estimated width fits ``max_width``.

    Greedily packs whitespace/hyphen-delimited tokens (see
    :func:`_header_wrap_tokens`) onto lines; a single token too wide to fit on
    its own falls back to :func:`_hyphenate_balanced`.
    """
    char_width = font_size * SECTION_LABEL_CHAR_WIDTH_RATIO
    budget = max(LABEL_WRAP_MIN_LINE_CHARS, int(max_width / char_width))

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

    wrapped: list[str] = []
    for line in lines:
        fits = estimate_section_label_width(line, font_size) <= max_width
        if " " in line or fits:
            wrapped.append(line)
        else:
            wrapped.extend(_hyphenate_balanced(line, budget))
    return wrapped


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


def _content_clear_height(graph: MetroGraph, section: Section) -> float | None:
    """Vertical room between ``section.bbox_y`` and its topmost station's own
    label, or ``None`` when the section has no real station to bound against
    (grow unrestricted)."""
    tops = [
        st.y
        for sid in section.station_ids
        if (st := graph.stations.get(sid)) is not None
        and not st.is_port
        and not st.is_hidden
    ]
    if not tops:
        return None
    return min(tops) - section.bbox_y - STATION_LABEL_CLEARANCE


_UNBOUNDED_WRAP_LINES = 1_000_000
"""Sentinel returned by :func:`_max_wrap_lines` for a section with no station
to bound against - larger than any real title could ever wrap onto."""


def _max_wrap_lines(clear_height: float | None, font_size: float) -> int:
    """Most lines a wrapped header can grow to without reaching section content."""
    if clear_height is None:
        return _UNBOUNDED_WRAP_LINES
    if clear_height <= 0:
        return 1
    return 1 + int(clear_height / header_line_height(font_size))


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
    ``bbox_w`` when the title needs more lines than the section's content
    leaves room for.
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
) -> SectionHeaderPlacement:
    """Pick a clash-free position for ``section``'s header (see module docstring).

    Each candidate position is tested against the actual routed ``polylines`` so
    a line crossing the header band - whether it enters through an edge port or
    merely skirts the box - forces a relocation.  With no polylines supplied the
    default above-left position is returned (used only where routes are not yet
    available)."""
    circle_r = SECTION_NUM_CIRCLE_R_LARGE
    num_y = SECTION_NUM_Y_OFFSET
    gap = SECTION_HEADER_SIDE_GAP

    x0 = section.bbox_x
    y0 = section.bbox_y
    box_bottom = section.bbox_y + section.bbox_h
    box_right = section.bbox_x + section.bbox_w
    half_text = SECTION_LABEL_HALF_HEIGHT_RATIO * label_font_size

    # A rotated side header runs down the box height and is never wrapped; a
    # horizontal (above/below/nudge) header wraps onto extra lines instead of
    # overhanging bbox_w, capped at however many lines fit above the
    # section's own content.
    side_length = _header_length(section.name, label_font_size)
    max_lines = _max_wrap_lines(_content_clear_height(graph, section), label_font_size)
    lines, length, extra_height, height_capped = _wrapped_header_geometry(
        section.name, label_font_size, section.bbox_w, side_length, max_lines
    )

    above = _above(
        x0, y0, circle_r, num_y, length, half_text, lines, extra_height, height_capped
    )
    if polylines is None:
        return above

    # The left column additionally needs room between the box and the canvas origin.
    side_fits = side_length <= section.bbox_h
    candidates = [
        above,
        _below(
            x0,
            box_bottom,
            circle_r,
            num_y,
            length,
            half_text,
            lines,
            extra_height,
            height_capped,
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
) -> dict[str, SectionHeaderPlacement]:
    """Resolve every drawn section's header placement once, keyed by section id."""
    return {
        section.id: resolve_section_header_placement(
            graph, section, label_font_size, polylines
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
        badge_cy=cy,
        label_x=cx + circle_r + SECTION_LABEL_TEXT_OFFSET,
        label_y=cy,
        label_rotation=0.0,
        label_lines=tuple(lines),
        keepout=(x0, cy - half_text, x0 + length, y0 + extra_height),
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
        badge_cy=cy,
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
    overhang the box to the right."""
    pad = SECTION_HEADER_ROUTE_PAD
    bx0, by0, bx1, by1 = above.keepout
    band = (bx0 - pad, by0 - pad, bx1 + pad, by1 + pad)
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
        badge_cy=cy,
        label_x=cx + circle_r + SECTION_LABEL_TEXT_OFFSET,
        label_y=cy,
        label_rotation=0.0,
        label_lines=tuple(lines),
        keepout=(start, cy - half_text, start + length, y0 + extra_height),
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
    width for fewer lines specifically to stay clear of the section's own
    content (see :func:`_wrapped_header_geometry`).
    """
    overflowing: list[str] = []
    for section_id, placement in placements.items():
        if placement.label_rotation or placement.mode == "nudge":
            continue
        if placement.height_capped:
            continue
        section = graph.sections.get(section_id)
        if section is None:
            continue
        header_width = placement.keepout[2] - placement.keepout[0]
        if header_width > section.bbox_w + tolerance:
            overflowing.append(section_id)
    return overflowing
