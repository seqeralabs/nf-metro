"""Label placement for station names.

Uses horizontal labels (like the reference nf-core metro maps) with
above/below alternation and collision avoidance.
"""

from __future__ import annotations

__all__ = [
    "LabelOverlap",
    "LabelPlacement",
    "active_font_scale",
    "find_label_overlaps",
    "font_scale_context",
    "label_glyph_ink_bbox",
    "label_text_width",
    "place_labels",
]

import math
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator, Literal, NamedTuple

from nf_metro.layout.constants import (
    CHAR_WIDTH,
    COLLISION_MULTIPLIER,
    DESCENDER_CLEARANCE,
    DIAGONAL_LABEL_OFFSET,
    FONT_HEIGHT,
    LABEL_BBOX_MARGIN,
    LABEL_GLYPH_INK_RATIO,
    LABEL_LINE_HEIGHT,
    LABEL_MARGIN,
    LABEL_NUDGE_MAX,
    LABEL_OFFSET,
    LABEL_OVERLAP_TOL,
    LABEL_WRAP_MIN_LINE_CHARS,
    PORT_LABEL_MAX_DX,
    TB_LABEL_H_SPACING,
    TB_LINE_Y_OFFSET,
    TB_PILL_EDGE_OFFSET,
    X_SPACING,
)
from nf_metro.layout.geometry import segment_intersects_bbox
from nf_metro.parser.model import MetroGraph

if TYPE_CHECKING:
    from nf_metro.layout.routing.common import RoutedPath
    from nf_metro.parser.model import Section, Station


_ACTIVE_FONT_SCALE: float = 1.0


def active_font_scale() -> float:
    """Font-size multiplier in effect for the current layout/render pass.

    Set via ``font_scale_context`` around ``compute_layout`` and
    ``render_svg`` so the label-width metrics below reserve room that
    matches the scaled rendered text.  Defaults to 1.0 (a no-op).
    """
    return _ACTIVE_FONT_SCALE


@contextmanager
def font_scale_context(scale: float) -> Iterator[None]:
    """Apply ``scale`` to the label metrics for the duration of the block."""
    global _ACTIVE_FONT_SCALE
    previous = _ACTIVE_FONT_SCALE
    _ACTIVE_FONT_SCALE = scale
    try:
        yield
    finally:
        _ACTIVE_FONT_SCALE = previous


def label_text_width(label: str) -> float:
    """Pixel width of the widest line in a (possibly multi-line) label."""
    char_width = CHAR_WIDTH * _ACTIVE_FONT_SCALE
    if "\n" not in label:
        return len(label) * char_width
    return max(len(line) for line in label.split("\n")) * char_width


def _label_text_height(label: str) -> float:
    """Pixel height of a (possibly multi-line) label."""
    font_height = FONT_HEIGHT * _ACTIVE_FONT_SCALE
    n = label.count("\n") + 1
    if n == 1:
        return font_height
    return font_height + (n - 1) * font_height * LABEL_LINE_HEIGHT


def diagonal_label_pitch(
    graph: "MetroGraph",
    fallback: float,
    section_ids: "set[str] | None" = None,
) -> float:
    """Column pitch for diagonal (angled) station labels.

    All diagonal labels are drawn at the same angle, so adjacent columns'
    labels are PARALLEL and collide only by their PERPENDICULAR separation, not
    along their length.  Giving adjacent labels a clear text-row of space
    between them means a perpendicular separation of ~2x the label height (the
    row itself plus an equal gap); the column pitch that yields that is
    ``2 * height / sin(angle)``, floored at a marker-collision minimum.

    The tallest qualifying label drives the pitch so every column in scope
    shares one consistent pitch rather than each column sizing its own.  With
    *section_ids* the scope is restricted to stations in those sections, so a
    disconnected component's tall labels do not inflate another component's
    pitch; ``None`` scopes the whole graph.  Returns *fallback* when no label
    angle is set or no qualifying label exists in scope.
    """
    from nf_metro.layout.constants import STATION_RADIUS_APPROX

    angle = graph.label_angle or 0.0
    if not angle:
        return fallback
    sin_a = abs(math.sin(math.radians(angle)))
    if sin_a < 1e-6:
        return fallback
    tallest = 0.0
    for st in graph.stations.values():
        if section_ids is not None and st.section_id not in section_ids:
            continue
        if st.is_port or st.is_hidden or st.off_track:
            continue
        if st.is_blank_terminus:
            continue
        if not st.label.strip():
            continue
        tallest = max(tallest, _label_text_height(st.label))
    if tallest <= 0.0:
        return fallback
    marker_floor = STATION_RADIUS_APPROX * 3.0
    return max(marker_floor, (tallest * 2.0) / sin_a)


def diagonal_label_pitch_by_section(
    graph: "MetroGraph", fallback: float
) -> dict[str, float]:
    """Per-section diagonal-label column pitch, scoped per component.

    A diagonal label angle drives the column pitch off the tallest label in
    scope.  Scoping that to each weakly-connected component of the section
    meta-graph stops one component's tall labels (e.g. a disconnected rail
    panel) inflating another component's columns.  Each section maps to its
    component's pitch.

    Returns an empty map -- the signal for callers to keep the single
    graph-wide pitch -- unless the graph both carries a label angle and splits
    into two or more components.
    """
    if not graph.label_angle or graph.section_dag is None:
        return {}
    from nf_metro.layout.section_placement import _weakly_connected_components

    components = _weakly_connected_components(graph, graph.section_dag.section_edges)
    if len(components) <= 1:
        return {}
    per_section: dict[str, float] = {}
    for comp in components:
        pitch = diagonal_label_pitch(graph, fallback, section_ids=comp)
        for sid in comp:
            per_section[sid] = pitch
    return per_section


@dataclass
class LabelPlacement:
    """Placement information for a station label."""

    station_id: str
    text: str
    x: float
    y: float
    above: bool
    angle: float = 0.0  # Horizontal by default
    text_anchor: str = "middle"
    dominant_baseline: str = ""  # Empty means use above/below logic
    obstacle_bbox: tuple[float, float, float, float] | None = None


def _rail_panel_extents(graph: "MetroGraph") -> dict[str, tuple[float, float]]:
    """Per-rail-section (top_rail_y, bottom_rail_y) from the rail Y map.

    Used to offset rail-mode station labels to the whole panel's outer edge
    so a label always clears every rail rather than landing between two of
    them.  Returns an empty map when the graph has no rail sections.
    """
    rail_y = graph._rail_y
    extents: dict[str, tuple[float, float]] = {}
    if not rail_y:
        return extents
    for sec_id, per_line in rail_y.items():
        if not per_line:
            continue
        ys = list(per_line.values())
        extents[sec_id] = (min(ys), max(ys))
    return extents


def _rail_above_threshold(per_line: dict[str, float] | None) -> float | None:
    """Y below which a single-rail station counts as sitting on the top rail.

    ``per_line`` maps a section's line ids to their rail Y.  The threshold sits
    midway between the topmost rail and the next rail down, so only stations on
    the top rail fall above it.  ``None`` when there are fewer than two distinct
    rails (no meaningful top/non-top split).  Shared by ``_rail_label_side`` and
    ``rail_mode._rail_above_label_stations`` so both apply one definition.
    """
    if not per_line:
        return None
    ys = sorted(set(per_line.values()))
    if len(ys) < 2:
        return None
    return (ys[0] + ys[1]) / 2


def _rail_label_side(
    graph: "MetroGraph",
    station: "Station",
) -> bool | None:
    """Forced above/below side for a rail-mode single-rail station label.

    A label beside an inner rail reads as noise in the bundle, so each
    single-rail station's label is pushed *outward* to the panel edge (see
    ``_rail_panel_label_offsets``): a station on the topmost rail labels above
    (clearing the panel's top), every other single-rail station labels below
    (clearing the bottommost rail).  This keeps top-rail names out of the
    bundle and reads like a transit map where the outer line's stops are named
    on the outside.  Returns True (above) / False (below), or None when the
    rule doesn't apply (non-rail section, or a multi-rail spanning station
    which keeps layer alternation).
    """
    if not station.section_id:
        return None
    if station.rail_top_y is not None and station.rail_bottom_y is not None:
        return None  # spanning pill: keeps layer alternation
    threshold = _rail_above_threshold(graph._rail_y.get(station.section_id))
    if threshold is None:
        return None
    return station.y < threshold


def _rail_panel_label_offsets(
    station: "Station",
    panel_extents: dict[str, tuple[float, float]],
) -> tuple[float, float] | None:
    """Label offsets (min_off, max_off) clearing the WHOLE rail panel.

    A rail-mode label must never sit beside a middle rail: an above label has
    to clear the panel's topmost rail and a below label its bottommost rail,
    whatever rail(s) the station itself occupies.  Returns offsets measured
    from ``station.y`` to the panel's top/bottom rail (so ``_try_place`` lands
    the label outside the rail stack), or None when the station is not in a
    known rail panel.
    """
    if not station.section_id:
        return None
    extent = panel_extents.get(station.section_id)
    if extent is None:
        return None
    top_y, bot_y = extent
    return (top_y - station.y, bot_y - station.y)


def _label_bbox(
    placement: LabelPlacement,
) -> tuple[float, float, float, float]:
    """Return (x_min, y_min, x_max, y_max) bounding box for a label."""
    if placement.obstacle_bbox is not None:
        return placement.obstacle_bbox
    if placement.angle:
        return _rotated_label_bbox(placement)
    w = label_text_width(placement.text)
    text_h = _label_text_height(placement.text)

    # Horizontal bounds depend on text_anchor
    if placement.text_anchor == "end":
        x_min = placement.x - w
        x_max = placement.x
    elif placement.text_anchor == "start":
        x_min = placement.x
        x_max = placement.x + w
    else:  # "middle" (default)
        x_min = placement.x - w / 2
        x_max = placement.x + w / 2

    # Vertical bounds: "central" baseline centers text at y
    if placement.dominant_baseline == "central":
        y_min = placement.y - text_h / 2
        y_max = placement.y + text_h / 2
    elif placement.above:
        y_min = placement.y - text_h
        y_max = placement.y
    else:
        y_min = placement.y
        y_max = placement.y + text_h

    return (x_min, y_min, x_max, y_max)


# Fraction of the font height the alphabetic baseline sits below the glyph
# top.  Matches the renderer's dominant-baseline auto/hanging placement
# closely enough to tell a line striking the name's ink from one clearing it.
_GLYPH_ASCENT_RATIO = 0.8


def _label_ink_y_band(placement: LabelPlacement) -> tuple[float, float]:
    """Approximate ``(y_top, y_bottom)`` of a label's drawn glyph ink.

    Mirrors the renderer: a multi-line *above* label stacks downward from a
    raised first-line baseline (dominant-baseline auto), so the block grows
    upward from the pill; a *below* label hangs its first line at ``y`` and
    stacks downward.  Tighter and lower than ``_label_bbox``, which models an
    above block as ``[y - text_h, y]`` (the full height above the anchor) and
    so reads ~one line-height too high for a wrapped label.
    """
    font_height = FONT_HEIGHT * _ACTIVE_FONT_SCALE
    n = placement.text.count("\n") + 1
    line_spacing = font_height * LABEL_LINE_HEIGHT
    ascent = font_height * _GLYPH_ASCENT_RATIO
    if placement.above:
        first_baseline = placement.y - (n - 1) * line_spacing
        return first_baseline - ascent, placement.y + (font_height - ascent)
    return placement.y, placement.y + (n - 1) * line_spacing + font_height


def label_glyph_ink_bbox(
    placement: LabelPlacement,
) -> tuple[float, float, float, float]:
    """Return the label's drawn-glyph box, tightened to where text is inked.

    The reserved box from :func:`_label_bbox` budgets ``CHAR_WIDTH`` per
    character so labels claim ample collision room, but the rendered text is
    narrower than that.  This shrinks the horizontal span to
    ``LABEL_GLYPH_INK_RATIO`` of the reserved width about the box centre, and
    (for a standard above/below label) replaces the vertical span with the
    renderer's true ink band (:func:`_label_ink_y_band`), which stacks wrapped
    lines downward rather than growing upward from the anchor.  A route grazing
    only the empty reserved margin clears this box; a route striking through
    the text intersects it.
    """
    x0, y0, x1, y1 = _label_bbox(placement)
    center = (x0 + x1) / 2
    ink_half = (x1 - x0) / 2 * LABEL_GLYPH_INK_RATIO
    if not placement.angle and not placement.dominant_baseline:
        y0, y1 = _label_ink_y_band(placement)
    return (center - ink_half, y0, center + ink_half, y1)


def _rotated_label_bbox(
    placement: LabelPlacement,
) -> tuple[float, float, float, float]:
    """Axis-aligned bounding box of a label rotated about its anchor.

    The label text starts at the anchor ``(x, y)`` (``text_anchor="start"``)
    and reads horizontally before being rotated clockwise by ``angle``
    degrees about that anchor -- matching the SVG ``rotate(angle, x, y)``
    transform the renderer emits.  The enclosing rectangle of the rotated
    corners approximates the diagonal footprint for collision purposes.
    """
    corners = _label_corners(placement)
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return (min(xs), min(ys), max(xs), max(ys))


def _label_corners(
    placement: LabelPlacement,
) -> list[tuple[float, float]]:
    """Return the four corners of a label's (possibly rotated) box.

    For an angled label the corners are the upright text box rotated about
    its anchor, matching the renderer's ``rotate(angle, x, y)`` transform.
    For a horizontal label (or one carrying an explicit ``obstacle_bbox``)
    the corners are those of its axis-aligned bounding box.  Used by the
    oriented overlap test so densely-spaced parallel diagonal labels pack
    by their true (narrow) footprint rather than their enclosing AABB.
    """
    if placement.obstacle_bbox is not None or not placement.angle:
        x0, y0, x1, y1 = _label_bbox(placement)
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    w = label_text_width(placement.text)
    text_h = _label_text_height(placement.text)
    if placement.text_anchor == "end":
        left, right = placement.x - w, placement.x
    elif placement.text_anchor == "middle":
        left, right = placement.x - w / 2, placement.x + w / 2
    else:
        left, right = placement.x, placement.x + w
    corners = [
        (left, placement.y - text_h),
        (right, placement.y - text_h),
        (right, placement.y),
        (left, placement.y),
    ]
    rad = math.radians(placement.angle)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    ax, ay = placement.x, placement.y
    out: list[tuple[float, float]] = []
    for cx, cy in corners:
        dx, dy = cx - ax, cy - ay
        out.append((ax + dx * cos_a - dy * sin_a, ay + dx * sin_a + dy * cos_a))
    return out


def _obb_separation(
    a: list[tuple[float, float]],
    b: list[tuple[float, float]],
) -> float:
    """Minimum separating gap between two convex quads via SAT.

    Returns the largest gap found over all candidate separating axes
    (positive when the quads are apart, <= 0 when they overlap).  Both
    quads are convex (label rectangles), so the separating-axis theorem
    using each quad's edge normals is exact.
    """

    def axes(quad: list[tuple[float, float]]) -> list[tuple[float, float]]:
        res = []
        for i in range(len(quad)):
            x1, y1 = quad[i]
            x2, y2 = quad[(i + 1) % len(quad)]
            nx, ny = -(y2 - y1), (x2 - x1)
            length = math.hypot(nx, ny)
            if length > 1e-9:
                res.append((nx / length, ny / length))
        return res

    best_gap = -math.inf
    for ax, ay in axes(a) + axes(b):
        a_proj = [px * ax + py * ay for px, py in a]
        b_proj = [px * ax + py * ay for px, py in b]
        gap = max(min(b_proj) - max(a_proj), min(a_proj) - max(b_proj))
        best_gap = max(best_gap, gap)
    return best_gap


def _boxes_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    margin: float = LABEL_MARGIN,
) -> bool:
    """Check if two axis-aligned bounding boxes overlap."""
    return not (
        a[2] + margin < b[0]
        or b[2] + margin < a[0]
        or a[3] + margin < b[1]
        or b[3] + margin < a[1]
    )


def _placements_overlap(
    a: LabelPlacement,
    b: LabelPlacement,
    margin: float = LABEL_MARGIN,
) -> bool:
    """Overlap test honouring label rotation.

    When either label is angled, an oriented (separating-axis) test packs
    parallel diagonal labels by their true footprint; otherwise it falls
    back to the cheap AABB test so horizontal layouts are unchanged.
    """
    if a.angle or b.angle:
        return _obb_separation(_label_corners(a), _label_corners(b)) < margin
    return _boxes_overlap(_label_bbox(a), _label_bbox(b), margin)


class LabelOverlap(NamedTuple):
    """A detected overlap involving a station label.

    ``kind`` is ``"label"`` (label box over another label box) or
    ``"marker"`` (label box over a non-owner station marker).  ``a`` is the
    owning station of the label; ``b`` is the other label's station (for
    ``"label"``) or the intruded marker's station (for ``"marker"``).
    ``ox``/``oy`` are the per-axis intrusion depths in px.
    """

    kind: Literal["label", "marker"]
    a: str
    b: str
    ox: float
    oy: float


def _intrusion(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> tuple[float, float]:
    """Return per-axis overlap depth (negative when separated on that axis)."""
    ox = min(a[2], b[2]) - max(a[0], b[0])
    oy = min(a[3], b[3]) - max(a[1], b[1])
    return ox, oy


def find_label_overlaps(
    graph: MetroGraph,
    placements: list[LabelPlacement],
    station_offsets: dict[tuple[str, str], float] | None = None,
    marker_tol: float = LABEL_OVERLAP_TOL,
) -> list[LabelOverlap]:
    """Find label/label and label/marker overlaps in a set of placements.

    Two *label* boxes count as overlapping when they intrude on both axes
    (beyond a sub-pixel epsilon): text-on-text is never acceptable.  A label
    over a *marker* is reported only when it intrudes by more than
    ``marker_tol`` on both axes, so a label whose edge merely grazes a pill
    (the 1px touch between tightly stacked parallel lines) is tolerated.

    Shared by the wrapping pass, the runtime guard, and the layout validator
    so all three agree on what "overlap" means.
    """
    eps = 0.5
    labels = [
        p
        for p in placements
        if p.station_id
        and not p.station_id.startswith("__")
        and p.obstacle_bbox is None
    ]
    boxes = [(p, _label_bbox(p)) for p in labels]
    overlaps: list[LabelOverlap] = []

    for i in range(len(boxes)):
        pa, ba = boxes[i]
        for j in range(i + 1, len(boxes)):
            pb, bb = boxes[j]
            ox, oy = _intrusion(ba, bb)
            if ox <= eps or oy <= eps:
                continue
            # Angled labels pack as parallel diagonal strips: the AABBs
            # overlap long before the tilted text does, so confirm with the
            # oriented test before reporting (and spreading).  The reported
            # ox/oy stay the AABB intrusion -- a safe upper bound for the
            # spread bump.
            if (pa.angle or pb.angle) and _obb_separation(
                _label_corners(pa), _label_corners(pb)
            ) >= LABEL_MARGIN:
                continue
            overlaps.append(LabelOverlap("label", pa.station_id, pb.station_id, ox, oy))

    # Reuse the engine's pill geometry (returns None for ports, hidden
    # stations, and junctions, none of which render a marker to collide with).
    from nf_metro.layout.engine import _station_marker_bbox

    markers = {
        sid: bbox
        for sid in graph.stations
        if (bbox := _station_marker_bbox(graph, sid, station_offsets)) is not None
    }
    for p, lb in boxes:
        for sid, mb in markers.items():
            if sid == p.station_id:
                continue
            ox, oy = _intrusion(lb, mb)
            if ox > marker_tol and oy > marker_tol:
                overlaps.append(LabelOverlap("marker", p.station_id, sid, ox, oy))

    return overlaps


def find_wrapped_label_trunk_strikes(
    graph: MetroGraph,
    placements: list[LabelPlacement],
    routes: list["RoutedPath"] | None,
    station_offsets: dict[tuple[str, str], float] | None = None,
    tol: float = 0.75,
) -> list[tuple[str, float, str]]:
    """Find multi-line labels whose ink overruns a foreign horizontal trunk.

    A wrapped label stacks its extra lines toward the row above (an above
    label) or below, so its block can grow across a metro line the station
    does not serve when that line runs horizontally over the label's width.
    Single-line labels and a label's own served lines never count: a station
    sits on its own line by construction and its name is offset clear of it.

    Routes are measured at their drawn Y (``station_offsets`` applied) so this
    sees the same geometry as ``_guard_no_line_strikes_label``.  Returns
    ``(station_id, trunk_y, line_id)`` for each strike, shared by the
    render-time lift, that guard, and the layout invariant test so all agree
    on what "a line strikes a wrapped label" means.
    """
    if not routes:
        return []
    from nf_metro.render.svg import apply_route_offsets

    # Every line's horizontal runs at their drawn Y: (line_id, x_lo, x_hi, y).
    trunks: list[tuple[str, float, float, float]] = []
    for route in routes:
        pts = (
            apply_route_offsets(route, station_offsets)
            if station_offsets is not None
            else route.points
        )
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            if abs(by - ay) <= tol:
                trunks.append((route.line_id, min(ax, bx), max(ax, bx), ay))

    strikes: list[tuple[str, float, str]] = []
    for p in placements:
        if p.obstacle_bbox is not None or p.angle or "\n" not in p.text:
            continue
        st = graph.stations.get(p.station_id)
        if st is None or st.is_port:
            continue
        served = set(graph.station_lines(st.id))
        x0, ytop, x1, ybot = label_glyph_ink_bbox(p)
        for line_id, seg_lo, seg_hi, y in trunks:
            if line_id in served or seg_hi < x0 - tol or seg_lo > x1 + tol:
                continue
            if ytop - tol <= y <= ybot + tol:
                strikes.append((p.station_id, y, line_id))
                break
    return strikes


def _pill_offsets(
    graph: MetroGraph,
    station: "Station",
    station_offsets: dict[tuple[str, str], float] | None,
) -> tuple[float, float]:
    """``(min_off, max_off)``: the pill's top/bottom edge offsets from
    ``station.y``.

    A multi-line station spreads its served lines around ``station.y``; a
    label measures from the outermost line so it clears the whole pill.
    Mirrors the per-station offset the placement loop computes.
    """
    if not station_offsets:
        return 0.0, 0.0
    offs = [
        station_offsets.get((station.id, lid), 0.0)
        for lid in graph.station_lines(station.id)
    ]
    if not offs:
        return 0.0, 0.0
    return min(offs), max(offs)


def _lift_wrapped_labels_off_foreign_trunks(
    placements: list[LabelPlacement],
    graph: MetroGraph,
    routes: list["RoutedPath"] | None,
    station_offsets: dict[tuple[str, str], float] | None,
    safe_offsets: dict[str, tuple[float, float]],
    label_offset: float,
) -> None:
    """Pull a wrapped label back to its un-pushed anchor when its block
    overruns a foreign trunk.

    place_labels settles each label's side and any both-sides-collision
    push-out from single-line geometry; ``_wrap_overlapping_labels`` then
    stacks crowded labels onto extra lines that grow the block toward the row
    above (above placement) or below.  A label the push-out drove toward a
    neighbouring track can then overrun a metro line the station does not
    serve.  Restoring the label to its un-pushed anchor (the closest it
    naturally sits to its own pill) keeps the grown block clear; a graze with a
    neighbouring label is preferable to a line drawn through the name.
    """
    struck = {
        sid
        for sid, _y, _lid in find_wrapped_label_trunk_strikes(
            graph, placements, routes, station_offsets
        )
    }
    if not struck:
        return
    for p in placements:
        if p.station_id not in struck:
            continue
        st = graph.stations.get(p.station_id)
        if st is None:
            continue
        min_off, max_off = _pill_offsets(graph, st, station_offsets)
        safe_above, safe_below = safe_offsets.get(
            p.station_id, (label_offset, label_offset)
        )
        if p.above:
            base_y = st.y + min_off - safe_above - DESCENDER_CLEARANCE
            if base_y > p.y:  # base sits closer to the pill than the push-out
                p.y = base_y
        else:
            base_y = st.y + max_off + safe_below
            if base_y < p.y:
                p.y = base_y


def _wrap_text_to_chars(text: str, max_chars: int) -> str:
    """Word-wrap ``text`` so no line exceeds ``max_chars`` characters.

    Words longer than the budget are hard-broken with a trailing hyphen as a
    last resort (so a single long token like "Quantification" can still be
    narrowed).  ``max_chars`` is floored at ``LABEL_WRAP_MIN_LINE_CHARS``.
    """
    budget = max(max_chars, LABEL_WRAP_MIN_LINE_CHARS)
    lines: list[str] = []
    cur = ""
    for word in text.split():
        w = word
        # Hard-break tokens that can't fit the budget on their own.
        while len(w) > budget:
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(w[: budget - 1] + "-")
            w = w[budget - 1 :]
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= budget:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return "\n".join(lines)


def _nudge_to_clear(
    candidate: LabelPlacement,
    existing: list[LabelPlacement],
    max_nudge: float = LABEL_NUDGE_MAX,
) -> LabelPlacement | None:
    """Try a small horizontal shift to clear collisions on the preferred side.

    When a label collides with already-placed labels, a tiny X nudge can
    often resolve it without flipping above/below (which breaks
    alternation).  Returns a nudged copy if successful, None otherwise.
    """
    cbox = _label_bbox(candidate)

    # Accumulate the minimum shift needed in each direction.
    need_right = 0.0  # shift right to clear left-side collisions
    need_left = 0.0  # shift left to clear right-side collisions

    for placed in existing:
        pbox = _label_bbox(placed)
        if not _boxes_overlap(cbox, pbox):
            continue
        if placed.x <= candidate.x:
            # Collider is to the left: need to shift our left edge rightward.
            gap = pbox[2] + LABEL_MARGIN - cbox[0]
            if gap > 0:
                need_right = max(need_right, gap)
        else:
            # Collider is to the right: need to shift our right edge leftward.
            gap = cbox[2] + LABEL_MARGIN - pbox[0]
            if gap > 0:
                need_left = max(need_left, gap)

    # If squeezed from both sides, nudging can't help.
    if need_right > 0 and need_left > 0:
        return None

    # Add a tiny epsilon so the shifted edge clears the strict-less-than
    # overlap check rather than landing exactly on the boundary.
    _EPS = 0.1
    shift = need_right - need_left
    if shift > 0:
        shift += _EPS
    elif shift < 0:
        shift -= _EPS
    if abs(shift) > max_nudge:
        return None

    nudged = LabelPlacement(
        station_id=candidate.station_id,
        text=candidate.text,
        x=candidate.x + shift,
        y=candidate.y,
        above=candidate.above,
    )
    if _has_collision(nudged, existing):
        return None
    return nudged


def _edge_solo(
    stations: list[Station],
    section_y_range: dict[str, tuple[float, float]],
) -> dict[str, tuple[bool, bool]]:
    """Determine whether each section's Y extremes have a sole station.

    Returns a dict mapping section_id -> (lo_solo, hi_solo).  The edge
    station override (prefer outward labels) should only apply when the
    extreme Y has a single station; otherwise it kills alternation on
    crowded tracks.
    """
    from collections import Counter

    result: dict[str, tuple[bool, bool]] = {}
    sec_ys: dict[str, list[float]] = {}
    for s in stations:
        if s.section_id and s.section_id in section_y_range:
            sec_ys.setdefault(s.section_id, []).append(s.y)

    for sec_id, ys in sec_ys.items():
        y_lo, y_hi = section_y_range[sec_id]
        counts = Counter(ys)
        result[sec_id] = (counts.get(y_lo, 0) == 1, counts.get(y_hi, 0) == 1)

    return result


def _compute_port_label_preference(
    graph: MetroGraph,
    max_dx: float = 0.0,
) -> dict[str, bool]:
    """Determine preferred label side for stations connected to nearby off-Y ports.

    When a station connects to a port at a different Y **and** the port is
    horizontally close, the diagonal route between them occupies the space
    on that side.  The label should go on the opposite side to avoid
    overlapping the route.

    *max_dx* is the maximum horizontal distance between station and port
    for the override to apply.  Stations far from their port have enough
    horizontal room for the diagonal to clear the label.

    Returns a dict mapping station_id -> preferred_above (True = above).
    Only stations with a clear preference are included.
    """
    result: dict[str, bool] = {}
    for edge in graph.edges:
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if not src or not tgt:
            continue

        # Only consider station -> exit port edges.  Exit routes depart
        # from the station toward the section boundary and can pass
        # through the label area.  Entry routes arrive via L-shaped
        # inter-section routing and rarely conflict with labels.
        if not (tgt.is_port and not src.is_port):
            continue
        port_info = graph.ports.get(tgt.id)
        if not port_info or port_info.is_entry:
            continue

        station, port = src, tgt
        dy = port.y - station.y
        if abs(dy) < 1:
            continue

        # Only override when the port is close enough that the
        # diagonal route would visually clash with the label.
        if max_dx > 0 and abs(port.x - station.x) > max_dx:
            continue

        # Port is below station -> route goes down -> prefer label above
        # Port is above station -> route goes up -> prefer label below
        preferred_above = dy > 0
        if station.id in result and result[station.id] != preferred_above:
            # Conflicting ports on both sides; no preference
            del result[station.id]
        else:
            result[station.id] = preferred_above

    return result


def _diagonal_prefer_above(graph: MetroGraph, station: Station) -> bool:
    """Whether a tilted label should flip above the trunk for *station*.

    Diagonal labels otherwise always hang below the trunk.  When a fork
    sibling sits directly below a station (same column, a lower track) and
    nothing sits above it, the below label would cross that branch's route, so
    flip it above where it is clear.  Conservative: fires only for a station
    that is the top of a same-column stack in an LR/RL section.
    """
    if not station.section_id:
        return False
    sec = graph.sections.get(station.section_id)
    if sec is None or sec.direction not in ("LR", "RL"):
        return False
    below = above = False
    for other in graph.stations.values():
        if (
            other is station
            or other.is_port
            or other.is_hidden
            or other.section_id != station.section_id
        ):
            continue
        if abs(other.x - station.x) > X_SPACING * 0.5:
            continue
        if other.y > station.y + 1:
            below = True
        elif other.y < station.y - 1:
            above = True
    return below and not above


def _apply_edge_override(
    station: Station,
    start_above: bool,
    section_y_range: dict[str, tuple[float, float]],
    sections_with_multiline: set[str],
    edge_solo: dict[str, tuple[bool, bool]],
) -> bool:
    """Apply the edge-station outward-label override when appropriate."""
    if (
        not station.section_id
        or station.section_id in sections_with_multiline
        or station.section_id not in section_y_range
    ):
        return start_above

    y_lo, y_hi = section_y_range[station.section_id]
    lo_solo, hi_solo = edge_solo.get(station.section_id, (True, True))
    if y_lo < y_hi:
        if station.y == y_lo and lo_solo:
            return True
        if station.y == y_hi and hi_solo:
            return False
    return start_above


def _make_obstacle_placements(
    obstacles: list[tuple[float, float, float, float]] | None,
) -> list[LabelPlacement]:
    """Create phantom LabelPlacement entries for obstacle bounding boxes.

    These participate in collision detection but are filtered out before
    the final placement list is returned.
    """
    if not obstacles:
        return []
    return [
        LabelPlacement(
            station_id=f"__obstacle_{i}",
            text="",
            x=(bbox[0] + bbox[2]) / 2,
            y=bbox[1],
            above=False,
            obstacle_bbox=bbox,
        )
        for i, bbox in enumerate(obstacles)
    ]


def _trial_cost(
    stations: list[Station],
    graph: MetroGraph,
    label_offset: float,
    station_offsets: dict[tuple[str, str], float] | None,
    section_y_range: dict[str, tuple[float, float]],
    sections_with_multiline: set[str],
    flip: bool,
    icon_obstacles: list[tuple[float, float, float, float]] | None = None,
    port_pref: dict[str, bool] | None = None,
    panel_extents: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Count label collision cost for a section using the given alternation.

    Returns a score where lower is better: each collision-resolution flip
    costs 1, each push (still colliding after flip) costs 2.  A small
    fractional penalty is added for labels that face inward (toward the
    section's Y center, i.e. into the route-line bundle), weighted by
    label width so the longest labels dominate the tiebreaker.
    """
    solo = _edge_solo(stations, section_y_range)

    # Compute section Y midpoint for inward-facing penalty
    sec_id = stations[0].section_id if stations else None
    if sec_id and sec_id in section_y_range:
        y_lo, y_hi = section_y_range[sec_id]
        y_mid = (y_lo + y_hi) / 2
    else:
        y_mid = None

    placements: list[LabelPlacement] = _make_obstacle_placements(icon_obstacles)
    cost: float = 0
    for station in stations:
        if station_offsets:
            line_offs = [
                station_offsets.get((station.id, lid), 0.0)
                for lid in graph.station_lines(station.id)
            ]
            min_off = min(line_offs) if line_offs else 0.0
            max_off = max(line_offs) if line_offs else 0.0
        else:
            min_off = max_off = 0.0
        rail_panel = (
            _rail_panel_label_offsets(station, panel_extents)
            if panel_extents is not None
            else None
        )
        if rail_panel is not None:
            min_off, max_off = rail_panel

        start_above = station.layer % 2 == 1
        if flip:
            start_above = not start_above

        rail_side = _rail_label_side(graph, station)

        start_above = _apply_edge_override(
            station,
            start_above,
            section_y_range,
            sections_with_multiline,
            solo,
        )

        if port_pref and station.id in port_pref:
            start_above = port_pref[station.id]

        if rail_side is not None:
            start_above = rail_side

        candidate = _try_place(
            station, label_offset, start_above, placements, min_off, max_off
        )

        if _has_collision(candidate, placements):
            # Try a small horizontal nudge before flipping sides.
            nudged = _nudge_to_clear(candidate, placements)
            if nudged is not None:
                cost += 0.1  # small penalty for nudge (better than flip)
                candidate = nudged
            else:
                cost += 1
                candidate = _try_place(
                    station,
                    label_offset,
                    not start_above,
                    placements,
                    min_off,
                    max_off,
                )
                if _has_collision(candidate, placements):
                    cost += 2
                    direction = -1 if not start_above else 1
                    if direction < 0:
                        y = station.y + min_off - label_offset * COLLISION_MULTIPLIER
                    else:
                        y = station.y + max_off + label_offset * COLLISION_MULTIPLIER
                    candidate = LabelPlacement(
                        station_id=station.id,
                        text=station.label,
                        x=station.x,
                        y=y,
                        above=(direction < 0),
                    )

        # Small tiebreaker: penalize labels that face inward (toward
        # the section Y center, i.e. into the route-line bundle),
        # weighted by label width so longer labels drive the decision.
        if y_mid is not None and y_lo < y_hi:  # type: ignore[possibly-undefined]
            if not candidate.above and station.y < y_mid:
                cost += label_text_width(station.label) * 0.001
            elif candidate.above and station.y > y_mid:
                cost += label_text_width(station.label) * 0.001

        placements.append(candidate)

    return cost


def place_labels(
    graph: MetroGraph,
    label_offset: float = LABEL_OFFSET,
    station_offsets: dict[tuple[str, str], float] | None = None,
    icon_obstacles: list[tuple[float, float, float, float]] | None = None,
    routes: list["RoutedPath"] | None = None,
    allow_hyphenation: bool = True,
    label_angle: float = 0.0,
    lift_wrapped_off_trunks: bool = True,
) -> list[LabelPlacement]:
    """Place station labels, alternating above/below stations.

    Strategy:
    1. Default: alternate above/below based on layer index.
    2. If it collides with an existing label, try the other side.
    3. If still colliding, push further away.

    Per-section trial: for each LR/RL section with multiple stations,
    both alternation patterns are tested and the one with fewer
    collisions is used.

    When ``label_angle`` is non-zero, LR/RL section labels are instead
    anchored at the pill and tilted (transit-map style); they hang on one
    side and pack by their narrow rotated footprint.

    ``lift_wrapped_off_trunks`` runs a final render-time pass that pulls a
    wrapped label back to its un-pushed anchor when its grown block overruns a
    foreign trunk.  Callers measuring overlaps for the layout (the spacing
    search probe) pass False so the engine sees the pre-lift geometry and the
    deliberate neighbour graze never trips the label-overlap guard.
    """
    sorted_stations = sorted(
        (
            s
            for s in graph.stations.values()
            if not s.is_port
            and not s.is_hidden
            and not s.is_terminus
            and s.label.strip()
        ),
        key=lambda s: (s.layer, s.track),
    )

    # Pre-compute per-section Y extremes for LR/RL sections so edge
    # stations prefer outward-facing labels, centering visual content.
    # Include port station Y positions so routes entering/exiting at a
    # different Y than the station track inform the outward preference
    # (prevents labels facing into an entry/exit route).
    # Skip sections that contain multi-line labels: consistent layer
    # alternation avoids cascading collisions between the taller labels.
    section_y_range: dict[str, tuple[float, float]] = {}
    sections_with_multiline: set[str] = set()
    for s in sorted_stations:
        if not s.section_id:
            continue
        if "\n" in s.label:
            sections_with_multiline.add(s.section_id)
        sec = graph.sections.get(s.section_id)
        if not sec or sec.direction not in ("LR", "RL"):
            continue
        if s.section_id not in section_y_range:
            section_y_range[s.section_id] = (s.y, s.y)
        else:
            lo, hi = section_y_range[s.section_id]
            section_y_range[s.section_id] = (min(lo, s.y), max(hi, s.y))

    # Extend section Y ranges with port station positions so single-track
    # sections with off-track ports get outward-facing label preference.
    for s in graph.stations.values():
        if not s.is_port or not s.section_id:
            continue
        if s.section_id not in section_y_range:
            continue
        lo, hi = section_y_range[s.section_id]
        section_y_range[s.section_id] = (min(lo, s.y), max(hi, s.y))

    # Pre-compute which section edges have a sole station, so the
    # outward-label override only fires when it won't kill alternation.
    solo = _edge_solo(sorted_stations, section_y_range)

    # Pre-compute label side preference for stations connected to
    # off-Y ports, so labels avoid overlapping diagonal port routes.
    port_pref = _compute_port_label_preference(graph, max_dx=PORT_LABEL_MAX_DX)

    # Rail-mode panels: offset labels to the whole panel's outer edge so they
    # alternate above the top rail / below the bottom rail (never between rails).
    panel_extents = _rail_panel_extents(graph)

    # Trial both alternation patterns per section, pick the better one.
    section_flip: dict[str, bool] = {}
    sec_groups: dict[str, list[Station]] = {}
    for s in sorted_stations:
        if s.section_id and s.section_id in section_y_range:
            sec_groups.setdefault(s.section_id, []).append(s)
    for sec_id, sec_stations in sec_groups.items():
        if len(sec_stations) < 2:
            continue
        args = (
            sec_stations,
            graph,
            label_offset,
            station_offsets,
            section_y_range,
            sections_with_multiline,
        )
        cost_default = _trial_cost(
            *args,
            flip=False,
            icon_obstacles=icon_obstacles,
            port_pref=port_pref,
            panel_extents=panel_extents,
        )
        cost_flipped = _trial_cost(
            *args,
            flip=True,
            icon_obstacles=icon_obstacles,
            port_pref=port_pref,
            panel_extents=panel_extents,
        )
        if cost_flipped < cost_default:
            section_flip[sec_id] = True

    # Pre-compute per-station safe label offsets so labels between
    # vertically stacked stations stay closer to their own pill.
    safe_offsets = _compute_safe_offsets(
        sorted_stations, label_offset, station_offsets, graph
    )

    placements: list[LabelPlacement] = _make_obstacle_placements(icon_obstacles)

    for i, station in enumerate(sorted_stations):
        # Offset labels from the pill edge (outermost served line), not station.y.
        min_off, max_off = _pill_offsets(graph, station, station_offsets)
        rail_panel = _rail_panel_label_offsets(station, panel_extents)
        if rail_panel is not None:
            min_off, max_off = rail_panel
        rail_side = _rail_label_side(graph, station)

        # Check if this is a TB section station (horizontal pill)
        is_tb_vert = False
        if station.section_id:
            sec = graph.sections.get(station.section_id)
            if sec and sec.direction == "TB":
                is_tb_vert = True

        if is_tb_vert:
            # Place label to the left of the horizontal pill
            n_lines = len(graph.station_lines(station.id))
            offset_span = (n_lines - 1) * TB_LINE_Y_OFFSET
            pill_left = station.x - offset_span / 2 - TB_PILL_EDGE_OFFSET
            pill_right = station.x + offset_span / 2 + TB_PILL_EDGE_OFFSET
            candidate = LabelPlacement(
                station_id=station.id,
                text=station.label,
                x=pill_left - TB_LABEL_H_SPACING,
                y=station.y,
                above=True,
                text_anchor="end",
                dominant_baseline="central",
            )
            if _has_collision(candidate, placements):
                # Try right side of the pill
                candidate = LabelPlacement(
                    station_id=station.id,
                    text=station.label,
                    x=pill_right + TB_LABEL_H_SPACING,
                    y=station.y,
                    above=True,
                    text_anchor="start",
                    dominant_baseline="central",
                )
            # Expand section bbox to contain the label
            if station.section_id:
                tb_sec = graph.sections.get(station.section_id)
                if tb_sec and tb_sec.bbox_w > 0:
                    margin = LABEL_BBOX_MARGIN
                    lx_min, _, lx_max, _ = _label_bbox(candidate)
                    lx_min -= margin
                    lx_max += margin
                    if lx_min < tb_sec.bbox_x:
                        old_right = tb_sec.bbox_x + tb_sec.bbox_w
                        tb_sec.bbox_x = lx_min
                        tb_sec.bbox_w = old_right - lx_min
                    if lx_max > tb_sec.bbox_x + tb_sec.bbox_w:
                        tb_sec.bbox_w = lx_max - tb_sec.bbox_x
            placements.append(candidate)
            continue

        # Opt-in diagonal labels (#527): for non-TB sections, anchor the
        # label at the bottom of the pill and tilt it.  All angled labels
        # sit below the trunk on the same side (like real transit maps),
        # which lets densely-spaced stations share a compact horizontal
        # line without their long names colliding.
        if label_angle:
            # A single-rail station labels outward: the topmost rail above,
            # every other rail below, so names sit outside the bundle.  The
            # above anchor measures from ``min_off`` (the panel's top rail), so
            # the above-labels share a baseline.  Spanning and non-rail stations
            # keep the default tilt -- below the trunk, flipping above only to
            # clear a fork sibling sitting directly below.
            rail_side = _rail_label_side(graph, station)
            if rail_side is not None:
                diag_above = rail_side
            else:
                diag_above = _diagonal_prefer_above(graph, station)
            if diag_above:
                # Mirror the tilt across the trunk: anchor at the pill top and
                # read up-and-to-the-right, clearing a fork sibling below.
                candidate = LabelPlacement(
                    station_id=station.id,
                    text=station.label,
                    x=station.x,
                    y=station.y + min_off - label_offset - DIAGONAL_LABEL_OFFSET,
                    above=True,
                    angle=-label_angle,
                    text_anchor="start",
                )
            else:
                candidate = LabelPlacement(
                    station_id=station.id,
                    text=station.label,
                    x=station.x,
                    y=station.y + max_off + label_offset + DIAGONAL_LABEL_OFFSET,
                    above=False,
                    angle=label_angle,
                    text_anchor="start",
                )
            if station.section_id:
                sec = graph.sections.get(station.section_id)
                if sec is not None and sec.bbox_w > 0:
                    _grow_section_for_box(sec, _label_bbox(candidate))
            placements.append(candidate)
            continue

        # Alternate by layer (column): even layers below, odd layers above
        start_above = station.layer % 2 == 1
        if station.section_id and section_flip.get(station.section_id, False):
            start_above = not start_above

        # For isolated edge stations in LR/RL sections, prefer labels
        # extending outward (away from center).  Only applies when the
        # station is the sole occupant at the section's Y extreme;
        # crowded edges keep alternation to avoid horizontal collisions.
        start_above = _apply_edge_override(
            station,
            start_above,
            section_y_range,
            sections_with_multiline,
            solo,
        )

        # Override when a port route would clash with the label.
        if station.id in port_pref:
            start_above = port_pref[station.id]

        # Rail mode: force single-rail labels outward (above their own rail in
        # the top half, below in the bottom half) so they never sit between
        # rails and a column of branch stations spreads its labels vertically.
        if rail_side is not None:
            start_above = rail_side

        safe_above, safe_below = safe_offsets.get(
            station.id, (label_offset, label_offset)
        )
        eff_offset = safe_above if start_above else safe_below

        candidate = _try_place(
            station, eff_offset, start_above, placements, min_off, max_off
        )

        if _has_collision(candidate, placements):
            # Try a small horizontal nudge before flipping sides.
            nudged = _nudge_to_clear(candidate, placements)
            if nudged is not None:
                candidate = nudged
            else:
                # Try the other side
                alt_offset = safe_below if start_above else safe_above
                candidate = _try_place(
                    station,
                    alt_offset,
                    not start_above,
                    placements,
                    min_off,
                    max_off,
                )

                if _has_collision(candidate, placements):
                    # Push further in the non-default direction
                    direction = -1 if not start_above else 1
                    if direction < 0:
                        y = station.y + min_off - safe_above * COLLISION_MULTIPLIER
                    else:
                        y = station.y + max_off + safe_below * COLLISION_MULTIPLIER
                    candidate = LabelPlacement(
                        station_id=station.id,
                        text=station.label,
                        x=station.x,
                        y=y,
                        above=(direction < 0),
                    )

        # Final obstacle clearance: if the label still overlaps an
        # obstacle (e.g. a terminus file icon), try flipping to the
        # opposite side of the station first (keeps label close).
        # Only push past the obstacle as a last resort.
        if _has_collision(candidate, placements):
            cbox = _label_bbox(candidate)
            for p in placements:
                if p.obstacle_bbox is None:
                    continue
                obox = _label_bbox(p)
                if not _boxes_overlap(cbox, obox):
                    continue

                # Try flipping to the opposite side of the station.
                flip_above = not candidate.above
                flip_off = safe_above if flip_above else safe_below
                flipped = _try_place(
                    station,
                    flip_off,
                    flip_above,
                    placements,
                    min_off,
                    max_off,
                )
                if not _has_collision(flipped, placements):
                    candidate = flipped
                    break

                # Flipping also collides.  Pick whichever side keeps the
                # label closer to the station.  When both sides have
                # obstacles (e.g. tight grid between two terminus
                # icons), prefer closeness over clearance - a slight
                # overlap is better than pushing the label into an
                # adjacent row.
                flip_dist = abs(flipped.y - station.y)
                orig_dist = abs(candidate.y - station.y)
                if flip_dist < orig_dist:
                    candidate = flipped
                break

        # Clamp labels so they stay within section bbox
        if station.section_id:
            sec = graph.sections.get(station.section_id)
            if sec and sec.bbox_w > 0:
                text_half_w = label_text_width(candidate.text) / 2
                margin = LABEL_BBOX_MARGIN
                # Horizontal: expand section bbox if needed, keeping
                # the label centered on its station.
                label_left = candidate.x - text_half_w - margin
                label_right = candidate.x + text_half_w + margin
                if label_left < sec.bbox_x:
                    old_right = sec.bbox_x + sec.bbox_w
                    sec.bbox_x = label_left
                    sec.bbox_w = old_right - label_left
                if label_right > sec.bbox_x + sec.bbox_w:
                    sec.bbox_w = label_right - sec.bbox_x
                # Vertical clamping (with flip/expand on overlap)
                candidate = _clamp_label_vertical(
                    candidate,
                    sec,
                    station,
                    label_offset,
                    min_off,
                    max_off,
                    margin,
                    placements,
                )

        placements.append(candidate)

    if routes:
        _avoid_diagonal_routes(placements, graph, routes, station_offsets)
        _avoid_horizontal_strikethrough(placements, graph, routes, station_offsets)

    _wrap_overlapping_labels(
        placements, graph, station_offsets, allow_hyphenation=allow_hyphenation
    )

    if lift_wrapped_off_trunks:
        _lift_wrapped_labels_off_foreign_trunks(
            placements, graph, routes, station_offsets, safe_offsets, label_offset
        )

    if graph.label_angle:
        _apply_rail_label_angle(
            placements, graph, float(graph.label_angle), panel_extents
        )

    return [p for p in placements if p.obstacle_bbox is None]


_RAIL_LABEL_PANEL_CLEARANCE = 4.0


def _apply_rail_label_angle(
    placements: list[LabelPlacement],
    graph: MetroGraph,
    label_angle: float,
    panel_extents: dict[str, tuple[float, float]] | None = None,
) -> None:
    """Tilt rail-section station labels by *label_angle* degrees.

    Only rail-mode panels are affected (the normal layout path has its own
    diagonal-label machinery).  A below-bundle label trails down-and-right from
    the pill (``text-anchor=start``); an above-bundle label trails up-and-left
    with its bottom-right corner at the pill (``text-anchor=end``), so each
    name leads directly into its station marker.  The rail column step is sized
    for the rotated label's horizontal projection by ``_label_aware_x_spacing``
    so the panel packs tighter.
    """
    for p in placements:
        if p.obstacle_bbox is not None or not p.station_id:
            continue
        st = graph.stations.get(p.station_id)
        if st is None or st.is_port:
            continue
        if not (st.section_id and graph.is_rail_section(st.section_id)):
            continue
        # Blank termini render as icons, not text; leave them alone.
        if st.is_blank_terminus:
            continue
        p.angle = label_angle
        p.x = st.x
        # An above-bundle label rises up-and-to-the-left with its bottom-right
        # corner seated at the pill; a below-bundle label trails down-and-right
        # from the pill.
        p.text_anchor = "end" if p.above else "start"
        # Tilting adds vertical extent; nudge the anchor so the rotated label
        # clears the whole rail panel (above its top rail / below its bottom
        # rail) rather than dipping back beside a middle rail.
        extent = panel_extents.get(st.section_id) if panel_extents else None
        if extent is not None:
            top_y, bot_y = extent
            _, by0, _, by1 = _label_bbox(p)
            if p.above and by1 > top_y - _RAIL_LABEL_PANEL_CLEARANCE:
                p.y -= by1 - (top_y - _RAIL_LABEL_PANEL_CLEARANCE)
            elif not p.above and by0 < bot_y + _RAIL_LABEL_PANEL_CLEARANCE:
                p.y += (bot_y + _RAIL_LABEL_PANEL_CLEARANCE) - by0


# Upper bound on wrapping rounds.  Each round narrows one offending label by
# at least one line-width step, so this comfortably exceeds the steps any
# realistic label needs to shrink from full width to the legibility floor.
_WRAP_MAX_ROUNDS = 200


def _wrap_overlapping_labels(
    placements: list[LabelPlacement],
    graph: MetroGraph,
    station_offsets: dict[tuple[str, str], float] | None,
    allow_hyphenation: bool = True,
) -> None:
    """Narrow colliding labels by wrapping them onto multiple lines.

    Conditional: only labels that actually overlap a neighbour (per
    :func:`find_label_overlaps`) are wrapped, so a clean layout is left
    byte-identical.  Each round picks the widest wrappable offender and
    shrinks its line budget by one character, re-wrapping the *original*
    label (never the already-wrapped text, which would compound hyphens).
    The label grows away from its station so the extra height never intrudes
    on the pill.  Author-specified multi-line labels are left untouched --
    the author already chose the breaks.

    When ``allow_hyphenation`` is False the budget stops at the longest word
    (word-boundary wrapping only), leaving any residual overlap for the
    engine's spread loop to clear by widening spacing.  When True (the final
    render, where spacing is settled) a word may be hard-broken with a hyphen
    down to ``LABEL_WRAP_MIN_LINE_CHARS`` as a last resort.
    """
    by_id = {
        p.station_id: p
        for p in placements
        if p.station_id
        and not p.station_id.startswith("__")
        and p.obstacle_bbox is None
        and graph.stations.get(p.station_id) is not None
    }
    # Only re-flow single-line, un-rotated labels; respect any author-chosen
    # breaks and leave angled labels (#527) to spread rather than wrap.
    wrappable = {sid for sid, p in by_id.items() if "\n" not in p.text and not p.angle}
    if not wrappable:
        return

    originals = {sid: graph.stations[sid].label for sid in by_id}
    budgets = {sid: len(by_id[sid].text) for sid in wrappable}

    def min_budget(sid: str) -> int:
        if allow_hyphenation:
            return LABEL_WRAP_MIN_LINE_CHARS
        longest_word = max((len(w) for w in originals[sid].split()), default=1)
        return max(LABEL_WRAP_MIN_LINE_CHARS, longest_word)

    for _ in range(_WRAP_MAX_ROUNDS):
        overlaps = find_label_overlaps(graph, placements, station_offsets)
        offender = _choose_wrap_offender(overlaps, by_id, wrappable)
        if offender is None:
            break
        new_budget = budgets[offender] - 1
        if new_budget < min_budget(offender):
            wrappable.discard(offender)
            continue
        budgets[offender] = new_budget
        by_id[offender].text = _wrap_text_to_chars(originals[offender], new_budget)

    _expand_sections_for_wrapped_labels(placements, graph)


def _choose_wrap_offender(
    overlaps: list[LabelOverlap],
    by_id: dict[str, LabelPlacement],
    wrappable: set[str],
) -> str | None:
    """Pick the widest still-wrappable label involved in any overlap.

    For ``"marker"`` overlaps only the label owner can be narrowed; for
    ``"label"`` overlaps either side is a candidate.  Narrowing the wider
    label first does the most to clear the collision with the least wrapping.
    """
    best: str | None = None
    best_w = -1.0
    for ov in overlaps:
        candidates = (ov.a,) if ov.kind == "marker" else (ov.a, ov.b)
        for sid in candidates:
            if sid not in wrappable:
                continue
            w = label_text_width(by_id[sid].text)
            if w > best_w:
                best_w = w
                best = sid
    return best


def _expand_sections_for_wrapped_labels(
    placements: list[LabelPlacement],
    graph: MetroGraph,
) -> None:
    """Grow each section's bbox to contain its (now taller) wrapped labels."""
    margin = LABEL_BBOX_MARGIN
    for p in placements:
        if not p.station_id or p.station_id.startswith("__") or p.obstacle_bbox:
            continue
        station = graph.stations.get(p.station_id)
        if station is None or not station.section_id:
            continue
        sec = graph.sections.get(station.section_id)
        if sec is None or sec.bbox_w <= 0:
            continue
        x_min, y_min, x_max, y_max = _label_bbox(p)
        if y_min - margin < sec.bbox_y:
            sec.bbox_h += sec.bbox_y - (y_min - margin)
            sec.bbox_y = y_min - margin
        if y_max + margin > sec.bbox_y + sec.bbox_h:
            sec.bbox_h = (y_max + margin) - sec.bbox_y


def _grow_section_for_box(
    sec: Section,
    box: tuple[float, float, float, float],
    margin: float = LABEL_BBOX_MARGIN,
) -> None:
    """Grow a section bbox so it contains the given label box (plus margin)."""
    x_min, y_min, x_max, y_max = box
    if x_min - margin < sec.bbox_x:
        old_right = sec.bbox_x + sec.bbox_w
        sec.bbox_x = x_min - margin
        sec.bbox_w = old_right - sec.bbox_x
    if x_max + margin > sec.bbox_x + sec.bbox_w:
        sec.bbox_w = (x_max + margin) - sec.bbox_x
    if y_min - margin < sec.bbox_y:
        old_bottom = sec.bbox_y + sec.bbox_h
        sec.bbox_y = y_min - margin
        sec.bbox_h = old_bottom - sec.bbox_y
    if y_max + margin > sec.bbox_y + sec.bbox_h:
        sec.bbox_h = (y_max + margin) - sec.bbox_y


def _avoid_diagonal_routes(
    placements: list[LabelPlacement],
    graph: MetroGraph,
    routes: list["RoutedPath"],
    station_offsets: dict[tuple[str, str], float] | None,
) -> None:
    """Flip labels overlapping a non-horizontal route segment.

    Trunk segments are horizontal by design so labels offset above or
    below them clear naturally.  Diagonal segments (trunk-to-fan
    transitions, off-track entries) can land on top of a label and need
    explicit avoidance: flip the label to the opposite side of its
    station when the flipped position is free of label and route
    collisions; otherwise leave it.
    """
    diag: list[tuple[float, float, float, float]] = []
    for route in routes:
        pts = list(route.points)
        if station_offsets and not route.offsets_applied and pts:
            so = station_offsets.get((route.edge.source, route.line_id), 0.0)
            to = station_offsets.get((route.edge.target, route.line_id), 0.0)
            pts[0] = (pts[0][0], pts[0][1] + so)
            pts[-1] = (pts[-1][0], pts[-1][1] + to)
        for i in range(len(pts) - 1):
            (x1, y1), (x2, y2) = pts[i], pts[i + 1]
            dx, dy = x2 - x1, y2 - y1
            if abs(dy) < max(abs(dx), 1.0) * 0.05:
                continue  # ~horizontal; not a label obstacle.
            diag.append((x1, y1, x2, y2))
    if not diag:
        return

    m = LABEL_MARGIN

    def hits(box: tuple[float, float, float, float]) -> bool:
        padded = (box[0] - m, box[1] - m, box[2] + m, box[3] + m)
        return any(segment_intersects_bbox(*s, padded) for s in diag)

    for placement in [p for p in placements if p.obstacle_bbox is None]:
        if placement.dominant_baseline or placement.angle:
            continue
        if not hits(_label_bbox(placement)):
            continue
        station = graph.stations.get(placement.station_id)
        if station is None:
            continue
        if station_offsets:
            offs = [
                station_offsets.get((station.id, lid), 0.0)
                for lid in graph.station_lines(station.id)
            ]
            min_off, max_off = (min(offs), max(offs)) if offs else (0.0, 0.0)
        else:
            min_off = max_off = 0.0
        if placement.above:
            gap = max((station.y + min_off) - placement.y, LABEL_OFFSET)
            trial = LabelPlacement(
                station_id=placement.station_id,
                text=placement.text,
                x=placement.x,
                y=station.y + max_off + gap,
                above=False,
            )
        else:
            gap = max(placement.y - (station.y + max_off), LABEL_OFFSET)
            trial = LabelPlacement(
                station_id=placement.station_id,
                text=placement.text,
                x=placement.x,
                y=station.y + min_off - gap,
                above=True,
            )
        siblings = [p for p in placements if p is not placement]
        if _has_collision(trial, siblings) or hits(_label_bbox(trial)):
            continue
        placement.x, placement.y, placement.above = trial.x, trial.y, trial.above


def _avoid_horizontal_strikethrough(
    placements: list[LabelPlacement],
    graph: MetroGraph,
    routes: list["RoutedPath"],
    station_offsets: dict[tuple[str, str], float] | None,
) -> None:
    """Flip a label off a foreign horizontal trunk that strikes its glyph ink.

    A label hanging on the side of its station where another line's horizontal
    run passes reads as struck through.  ``_avoid_diagonal_routes`` ignores
    horizontal segments because labels normally clear the trunk they sit on;
    this catches the case a *foreign* trunk (a line the station does not carry,
    not an edge endpoint) crosses the label's glyph ink, and flips the label to
    the opposite side when that side is free of label and ink collisions.
    """
    foreign: list[tuple[str, tuple[float, float, float, float]]] = []
    for route in routes:
        pts = list(route.points)
        if station_offsets and not route.offsets_applied and pts:
            so = station_offsets.get((route.edge.source, route.line_id), 0.0)
            to = station_offsets.get((route.edge.target, route.line_id), 0.0)
            pts[0] = (pts[0][0], pts[0][1] + so)
            pts[-1] = (pts[-1][0], pts[-1][1] + to)
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            if abs(y2 - y1) >= max(abs(x2 - x1), 1.0) * 0.05:
                continue
            foreign.append((route.line_id, (min(x1, x2), y1, max(x1, x2), y2)))
    if not foreign:
        return

    for placement in [p for p in placements if p.obstacle_bbox is None]:
        if placement.dominant_baseline or placement.angle:
            continue
        station = graph.stations.get(placement.station_id)
        if station is None:
            continue
        carried = set(graph.station_lines(station.id))
        ink = label_glyph_ink_bbox(placement)

        def strikes(box: tuple[float, float, float, float]) -> bool:
            for line_id, (sx1, sy1, sx2, sy2) in foreign:
                if line_id in carried:
                    continue
                if segment_intersects_bbox(sx1, sy1, sx2, sy2, box):
                    return True
            return False

        if not strikes(ink):
            continue
        if station_offsets:
            offs = [
                station_offsets.get((station.id, lid), 0.0)
                for lid in graph.station_lines(station.id)
            ]
            min_off, max_off = (min(offs), max(offs)) if offs else (0.0, 0.0)
        else:
            min_off = max_off = 0.0
        if placement.above:
            gap = max((station.y + min_off) - placement.y, LABEL_OFFSET)
            trial = LabelPlacement(
                station_id=placement.station_id,
                text=placement.text,
                x=placement.x,
                y=station.y + max_off + gap,
                above=False,
            )
        else:
            gap = max(placement.y - (station.y + max_off), LABEL_OFFSET)
            trial = LabelPlacement(
                station_id=placement.station_id,
                text=placement.text,
                x=placement.x,
                y=station.y + min_off - gap,
                above=True,
            )
        siblings = [p for p in placements if p is not placement]
        if _has_collision(trial, siblings) or strikes(label_glyph_ink_bbox(trial)):
            continue
        placement.x, placement.y, placement.above = trial.x, trial.y, trial.above


def _clamp_label_vertical(
    candidate: LabelPlacement,
    sec: Section,
    station: Station,
    label_offset: float,
    min_off: float,
    max_off: float,
    margin: float,
    existing: list[LabelPlacement] | None = None,
) -> LabelPlacement:
    """Clamp label vertically within section bbox.

    If clamping would push the label into the station pill, flip it to the
    opposite side (provided the flipped position doesn't collide with an
    existing label).  If both sides would overlap, expand the section bbox
    so the label fits at its ideal position.
    """
    pill_top = station.y + min_off
    pill_bottom = station.y + max_off
    sec_top = sec.bbox_y
    sec_bottom = sec.bbox_y + sec.bbox_h

    text_h = _label_text_height(candidate.text)

    if candidate.above:
        # Label text occupies [candidate.y - text_h, candidate.y].
        min_y = sec_top + text_h + margin
        if candidate.y >= min_y:
            return candidate  # fits without clamping

        overshoot = min_y - candidate.y

        # Clamping needed - would the clamped position overlap the pill?
        if min_y <= pill_top - label_offset:
            # Still enough gap after clamping
            candidate.y = min_y
            return candidate

        # Small overshoot: prefer expanding the bbox over flipping, so
        # the label keeps its intended above/below side (preserving
        # alternation).
        if overshoot <= margin:
            sec.bbox_y -= overshoot + margin
            sec.bbox_h += overshoot + margin
            return candidate

        # Clamped position too close to pill - try flipping to below
        below_y = pill_bottom + label_offset
        max_y = sec_bottom - text_h - margin
        if below_y <= max_y:
            flipped = LabelPlacement(
                station_id=candidate.station_id,
                text=candidate.text,
                x=candidate.x,
                y=below_y,
                above=False,
            )
            if existing is None or not _has_collision(flipped, existing):
                candidate.y = below_y
                candidate.above = False
                return candidate

        # Neither side fits (or flip collides) - expand bbox upward
        expand = overshoot + margin
        sec.bbox_y -= expand
        sec.bbox_h += expand
        return candidate

    else:
        # Label text occupies [candidate.y, candidate.y + text_h].
        max_y = sec_bottom - text_h - margin
        if candidate.y <= max_y:
            return candidate  # fits without clamping

        overshoot = candidate.y - max_y

        # Clamping needed - would the clamped position overlap the pill?
        if max_y >= pill_bottom + label_offset:
            # Still enough gap after clamping
            candidate.y = max_y
            return candidate

        # Small overshoot: prefer expanding the bbox over flipping, so
        # the label keeps its intended above/below side (preserving
        # alternation).
        if overshoot <= margin:
            sec.bbox_h += overshoot + margin
            return candidate

        # Clamped position too close to pill - try flipping to above
        above_y = pill_top - label_offset
        min_y = sec_top + text_h + margin
        if above_y >= min_y:
            flipped = LabelPlacement(
                station_id=candidate.station_id,
                text=candidate.text,
                x=candidate.x,
                y=above_y,
                above=True,
            )
            if existing is None or not _has_collision(flipped, existing):
                candidate.y = above_y
                candidate.above = True
                return candidate

        # Neither side fits (or flip collides) - expand bbox downward
        expand = overshoot + margin
        sec.bbox_h += expand
        return candidate


def _compute_safe_offsets(
    sorted_stations: list[Station],
    label_offset: float,
    station_offsets: dict[tuple[str, str], float] | None,
    graph: MetroGraph,
) -> dict[str, tuple[float, float]]:
    """Compute per-station safe label offsets (above, below).

    For vertically stacked stations at the same X, the label offset is
    reduced so a label stays closer to its own pill than to the
    neighboring pill.  We use 40% of the available gap (instead of 50%)
    so the label is visibly biased toward its own station.

    Returns a dict mapping station_id -> (safe_above, safe_below).
    """
    # Group stations by (section_id, rounded X) to find vertical neighbors.
    col_groups: dict[tuple[str | None, float], list[Station]] = {}
    for s in sorted_stations:
        key = (s.section_id, round(s.x, 1))
        col_groups.setdefault(key, []).append(s)

    result: dict[str, tuple[float, float]] = {}

    for _key, group in col_groups.items():
        if len(group) < 2:
            for s in group:
                result[s.id] = (label_offset, label_offset)
            continue

        group.sort(key=lambda s: s.y)

        for idx, s in enumerate(group):
            if station_offsets:
                offs = [
                    station_offsets.get((s.id, lid), 0.0)
                    for lid in graph.station_lines(s.id)
                ]
                s_min = min(offs) if offs else 0.0
                s_max = max(offs) if offs else 0.0
            else:
                s_min = s_max = 0.0

            text_h = _label_text_height(s.label)
            safe_above = label_offset
            safe_below = label_offset

            # Check neighbor above
            if idx > 0:
                nb = group[idx - 1]
                if station_offsets:
                    nb_offs = [
                        station_offsets.get((nb.id, lid), 0.0)
                        for lid in graph.station_lines(nb.id)
                    ]
                    nb_max = max(nb_offs) if nb_offs else 0.0
                else:
                    nb_max = 0.0
                pill_gap = (s.y + s_min) - (nb.y + nb_max)
                if pill_gap > 0:
                    safe_above = min(label_offset, (pill_gap - text_h) * 0.4)
                    safe_above = max(safe_above, 2.0)  # floor

            # Check neighbor below
            if idx < len(group) - 1:
                nb = group[idx + 1]
                if station_offsets:
                    nb_offs = [
                        station_offsets.get((nb.id, lid), 0.0)
                        for lid in graph.station_lines(nb.id)
                    ]
                    nb_min = min(nb_offs) if nb_offs else 0.0
                else:
                    nb_min = 0.0
                pill_gap = (nb.y + nb_min) - (s.y + s_max)
                if pill_gap > 0:
                    safe_below = min(label_offset, (pill_gap - text_h) * 0.4)
                    safe_below = max(safe_below, 2.0)  # floor

            result[s.id] = (safe_above, safe_below)

    return result


def _try_place(
    station: Station,
    label_offset: float,
    above: bool,
    existing: list[LabelPlacement],
    min_off: float = 0.0,
    max_off: float = 0.0,
) -> LabelPlacement:
    """Create a label placement above or below a station.

    Offsets are measured from the pill edge: above labels use min_off
    (top of the pill) and below labels use max_off (bottom of the pill).

    Above labels include DESCENDER_CLEARANCE so that letter descenders
    (g, p, y ...) don't visually touch the pill.  The SVG renderer
    uses ``dominant-baseline: auto`` which places the alphabetic
    baseline at label.y -- descenders extend below that point.
    """
    if above:
        return LabelPlacement(
            station_id=station.id,
            text=station.label,
            x=station.x,
            y=station.y + min_off - label_offset - DESCENDER_CLEARANCE,
            above=True,
        )
    else:
        return LabelPlacement(
            station_id=station.id,
            text=station.label,
            x=station.x,
            y=station.y + max_off + label_offset,
            above=False,
        )


def _has_collision(
    candidate: LabelPlacement,
    existing: list[LabelPlacement],
) -> bool:
    """Check if a candidate label collides with any existing placement."""
    for placed in existing:
        if _placements_overlap(candidate, placed):
            return True
    return False
