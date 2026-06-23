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

from dataclasses import dataclass
from typing import Literal

from nf_metro.parser.model import MetroGraph, Section
from nf_metro.render.constants import (
    SECTION_HEADER_ROUTE_PAD,
    SECTION_HEADER_SIDE_GAP,
    SECTION_LABEL_CHAR_WIDTH_RATIO,
    SECTION_LABEL_HALF_HEIGHT_RATIO,
    SECTION_LABEL_TEXT_OFFSET,
    SECTION_NUM_CIRCLE_R_LARGE,
    SECTION_NUM_Y_OFFSET,
)

Rect = tuple[float, float, float, float]
Polyline = list[tuple[float, float]]
HeaderMode = Literal["above", "below", "left", "right", "nudge"]


@dataclass(frozen=True)
class SectionHeaderPlacement:
    """Resolved drawing geometry for one section's header.

    ``badge_*`` locate the numbered circle; ``label_*`` locate the title text.
    ``label_rotation`` is 0 for the horizontal positions and 90 for the rotated
    side positions (title reads top-to-bottom).  ``keepout`` is the union bbox
    of badge and title used by the render-time guard.
    """

    mode: HeaderMode
    badge_cx: float
    badge_cy: float
    label_x: float
    label_y: float
    label_rotation: float
    keepout: Rect


def estimate_section_label_width(name: str, font_size: float) -> float:
    """Estimate the rendered width of a section title in pixels."""
    return len(name) * font_size * SECTION_LABEL_CHAR_WIDTH_RATIO


def _header_length(name: str, font_size: float) -> float:
    """Length of the header (badge + gap + title) along its reading axis."""
    circle_r = SECTION_NUM_CIRCLE_R_LARGE
    if not name:
        return 2.0 * circle_r
    return (
        2.0 * circle_r
        + SECTION_LABEL_TEXT_OFFSET
        + estimate_section_label_width(name, font_size)
    )


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
    length = _header_length(section.name, label_font_size)
    half_text = SECTION_LABEL_HALF_HEIGHT_RATIO * label_font_size

    above = _above(x0, y0, circle_r, num_y, length, half_text)
    if polylines is None:
        return above

    # A rotated side header runs down the edge and must fit the box height; the
    # left column additionally needs room between the box and the canvas origin.
    side_fits = length <= section.bbox_h
    candidates = [above, _below(x0, box_bottom, circle_r, num_y, length, half_text)]
    if side_fits and x0 - gap - 2.0 * circle_r >= 0.0:
        candidates.append(_left(x0, y0, circle_r, gap, length))
    if side_fits:
        candidates.append(_right(box_right, y0, circle_r, gap, length))

    for candidate in candidates:
        if _placement_clear(candidate, polylines):
            return candidate
    return _nudge(x0, y0, circle_r, num_y, length, half_text, above, polylines)


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
        keepout=(x0, cy - half_text, x0 + length, y0),
    )


def _below(
    x0: float,
    box_bottom: float,
    circle_r: float,
    num_y: float,
    length: float,
    half_text: float,
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
        keepout=(x0, box_bottom, x0 + length, cy + half_text),
    )


def _left(
    x0: float,
    y0: float,
    circle_r: float,
    gap: float,
    length: float,
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
        keepout=(col_x - circle_r, y0, x0, y0 + length),
    )


def _right(
    box_right: float,
    y0: float,
    circle_r: float,
    gap: float,
    length: float,
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
        keepout=(box_right, y0, col_x + circle_r, y0 + length),
    )


def _nudge(
    x0: float,
    y0: float,
    circle_r: float,
    num_y: float,
    length: float,
    half_text: float,
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
        keepout=(start, cy - half_text, start + length, y0),
    )


class SectionHeaderClashError(RuntimeError):
    """A section header was placed over a routed line.

    Raised on the render path so the placement chain can never silently draw a
    title across a metro line, independent of ``compute_layout``'s validation.
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
