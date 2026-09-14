"""Passive layout-quality metrics for the CI render-diff.

Computes a small scorecard of geometric quality scores from a laid-out
``MetroGraph`` so the render-diff page can report per-render *deltas* alongside
the visual comparison.

Strictly an instrument: nothing in the layout engine reads these scores and CI
never fails on them.  The defect counts reuse the same detectors as the layout
validator (crossings, near-horizontal segments, single-segment diagonals,
excessive column gaps) and the label-strike count reuses the engine's own
strike definition (``iter_line_label_strikes``), so a score only moves when a
real geometric property of the render moves.

The bend, corner, turn-angle and marker-clearance scores have no engine-side
detector to borrow -- the engine's non-consumer guard answers a boolean, not a
distance -- so they are read straight off the drawn polylines.  Their
definitions are the four measured against human layout judgement by the
frozen preference dataset, whose record is
``datasets/layout_preferences/README.md``.

When a ``RenderPlan`` is available, the scorecard reads the exact routes used
for the SVG. See ``measured_geometry``.

Module-level imports are kept stdlib-only so the spec and formatting helpers
can be imported by ``build_render_diff.py`` without pulling in the layout
engine; the heavy ``nf_metro`` imports live inside ``compute_metrics``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from nf_metro.layout.routing.common import RoutedPath
    from nf_metro.parser.model import MetroGraph
    from nf_metro.render.plan import RenderPlan

MetricKind = Literal["count", "decimal", "ratio"]


@dataclass(frozen=True)
class MetricSpec:
    """One layout-quality score: its JSON key, display label, and value kind."""

    key: str
    label: str
    kind: MetricKind
    higher_is_better: bool = False


# Most metrics count a defect, so lower is better; ``higher_is_better`` marks
# the ones measuring room rather than damage.  This list is the canonical schema
# for ``metrics.json`` and the column order of the render-diff metrics table.
METRICS: list[MetricSpec] = [
    MetricSpec("crossings", "Crossings", "count"),
    MetricSpec("near_horizontal", "Near-horiz.", "count"),
    MetricSpec("single_diagonals", "Lone diag.", "count"),
    MetricSpec("bends_per_route", "Bends/route", "decimal"),
    MetricSpec("corners_total", "Corners", "count"),
    MetricSpec("turn_angle_per_route", "Turn/route (rad)", "decimal"),
    MetricSpec("label_strikes", "Label strikes", "count"),
    MetricSpec("marker_clearance", "Marker gap ↑", "decimal", higher_is_better=True),
    MetricSpec("excessive_gaps", "Excess gaps", "count"),
    MetricSpec("wasted_canvas", "Wasted canvas", "ratio"),
]

TURN_FLOOR = math.radians(5.0)
"""Smallest direction change counted as a bend.

Applying a bundle's line separation can leave two collinear segments meeting at
a fraction of a degree; below this floor there is no corner a reader sees.
"""

METRIC_KEYS: list[str] = [m.key for m in METRICS]


def measured_geometry(
    graph: MetroGraph, plan: RenderPlan | None = None
) -> tuple[dict[tuple[str, str], float], list[RoutedPath]]:
    """Return the station offsets and routes used by the scorecard.

    A supplied plan provides the exact geometry used for the SVG. Without a
    plan, the function routes the laid-out graph for pre-render callers.
    """
    from nf_metro.layout.routing import compute_station_offsets, route_edges

    if plan is not None:
        return plan.station_offsets, list(plan.routes)
    try:
        offsets = compute_station_offsets(graph)
        return offsets, route_edges(graph, station_offsets=offsets)
    except Exception:  # noqa: BLE001 - routing failure surfaces in the validator
        return {}, []


def drawn_polylines(
    routes: list[RoutedPath], offsets: dict[tuple[str, str], float]
) -> list[tuple[RoutedPath, list[tuple[float, float]]]]:
    """Each route paired with the polyline the renderer draws for it.

    ``apply_route_offsets`` is the one place a route's stored points become
    drawable coordinates, so a deferred-offset route only reveals its drawn
    shape here.  Repeated waypoints are dropped: a zero-length step has no
    direction, so it would otherwise register as a turn against due east.
    """
    from nf_metro.layout.routing.common import apply_route_offsets

    out: list[tuple[RoutedPath, list[tuple[float, float]]]] = []
    for route in routes:
        pts: list[tuple[float, float]] = []
        for point in apply_route_offsets(route, offsets):
            if not pts or point != pts[-1]:
                pts.append(point)
        if len(pts) > 1:
            out.append((route, pts))
    return out


def _turn_angle(
    before: tuple[float, float],
    at: tuple[float, float],
    after: tuple[float, float],
) -> float:
    """Absolute direction change at ``at``, in radians (0 = straight on)."""
    incoming = math.atan2(at[1] - before[1], at[0] - before[0])
    outgoing = math.atan2(after[1] - at[1], after[0] - at[0])
    delta = abs(outgoing - incoming) % (2 * math.pi)
    return min(delta, 2 * math.pi - delta)


def _bend_scores(
    polylines: list[tuple[RoutedPath, list[tuple[float, float]]]],
) -> tuple[float, float, float]:
    """``(corners, corners per route, radians turned per route)``.

    The per-route forms are the scale-free ones: a bigger map draws more
    corners without each of its routes being any more tortuous.
    """
    corners = 0
    turned = 0.0
    for _route, pts in polylines:
        for i in range(1, len(pts) - 1):
            angle = _turn_angle(pts[i - 1], pts[i], pts[i + 1])
            if angle > TURN_FLOOR:
                corners += 1
                turned += angle
    routed = max(len(polylines), 1)
    return float(corners), corners / routed, turned / routed


def _min_non_consumer_clearance(
    graph: MetroGraph,
    polylines: list[tuple[RoutedPath, list[tuple[float, float]]]],
) -> float | None:
    """How close the nearest line gets to a station marker it does not serve.

    A line running just past a marker reads as stopping there, so the room it
    leaves is a quality score in its own right -- the engine's non-consumer
    guard only catches the endpoint of the same spectrum, where the line
    reaches the marker's box.  Ports and hidden stations draw no marker, so
    nothing can crowd them, and the guard's own exemption is honoured so the
    score does not report the deliberate rail idiom as crowding.

    ``None`` when every line serves every station it passes, which leaves the
    clearance undefined rather than arbitrarily large.

    Measured to the marker centre, so a station carrying a wide bundle -- whose
    drawn pill extends well past its centre -- reports more room than the
    picture shows.
    """
    from nf_metro.layout.geometry import point_to_polyline_distance
    from nf_metro.layout.phases._common import marker_cross_exempt

    markers = [
        ((float(s.x), float(s.y)), set(graph.station_lines(sid)))
        for sid, s in graph.stations.items()
        if not (s.is_port or s.is_hidden)
        and s.x is not None
        and s.y is not None
        and not marker_cross_exempt(graph, sid)
    ]

    closest: float | None = None
    for route, pts in polylines:
        for centre, own_lines in markers:
            if route.line_id in own_lines:
                continue
            gap = point_to_polyline_distance(centre, pts)
            if closest is None or gap < closest:
                closest = gap
    return closest


# ``min_marker_gap``'s "no foreign line exists at all" value, which is the
# cleanest state rather than the worst. Masked to zero crowding.
MARKER_GAP_UNDEFINED = -1.0

CROWDING_PITCH = 40.0
"""One lane pitch, mirroring ``nf_metro.layout.constants.Y_SPACING``.

Clearance beyond one pitch is room enough, so this is where
:func:`marker_crowding` saturates. Hardcoded to match the literal the frozen
preference dataset measured with (``datasets/layout_preferences/README.md``):
a feature's meaning must not track a constant the engine may retune, or a
vector measured today would not be comparable with one measured at an older
revision.
"""


def marker_crowding(gap: float | None) -> float:
    """How far the nearest foreign line intrudes on a marker, as a 0..1 fraction.

    One-sided and saturating, so the term penalises tight clearance without ever
    paying for loose clearance: a term that kept paying would be minimised by
    spreading the map out, which is the failure mode
    :data:`ADMISSIBILITY` exists to exclude.

    An absent measurement scores **zero** crowding. No foreign line coming near
    any marker is the cleanest state a map can be in, not the worst: 104 of the
    278 fixtures carrying a vector in the committed corpus are in it, and
    reading their ``-1.0`` as a gap would score every one of them as maximally
    crowded and drive a non-negative weight to penalise the fixtures with the
    best clearance in the corpus.
    """
    if gap is None or gap == MARKER_GAP_UNDEFINED:
        return 0.0
    return min(1.0, max(0.0, (CROWDING_PITCH - gap) / CROWDING_PITCH))


DERIVED = {"marker_crowding": ("min_marker_gap", marker_crowding)}
"""Admissible terms computed from an inadmissible feature.

Keyed by the derived name, valued by the source feature and the transform. A
consumer that wants the source's signal in a minimisable score reaches for the
derived term instead of repairing the raw feature locally.
"""


def compute_metrics(
    graph: MetroGraph,
    *,
    plan: RenderPlan | None = None,
    canvas: tuple[float, float] | None = None,
) -> dict[str, float | None]:
    """Compute the layout-quality scorecard for one laid-out graph.

    ``canvas`` is the rendered SVG ``(width, height)`` in user units; when
    omitted (unit tests) the canvas extent is estimated from the laid-out
    geometry.
    """
    from collections import Counter

    from layout_validator import validate_layout

    counts = Counter(v.check for v in validate_layout(graph))

    geometry_graph = plan.graph if plan is not None else graph
    offsets, routes = measured_geometry(graph, plan)
    polylines = drawn_polylines(routes, offsets)
    corners, bends_per_route, turn_per_route = _bend_scores(polylines)

    # Distinct (line, station) strikes: one visual mark per line crossing a
    # label, not one per route segment that happens to clip it.
    if plan is None:
        from nf_metro.layout.phases.guards import iter_line_label_strikes

        strikes = {
            (strike.line_id, strike.station_id)
            for strike in iter_line_label_strikes(graph, offsets=offsets, routes=routes)
        }
    else:
        strikes = _plan_label_strikes(plan)

    return {
        "crossings": float(
            counts["route_segment_crossing"] + counts["inter_section_line_crossing"]
        ),
        "near_horizontal": float(counts["almost_horizontal_edge"]),
        "single_diagonals": float(counts["single_segment_diagonal"]),
        "bends_per_route": bends_per_route,
        "corners_total": corners,
        "turn_angle_per_route": turn_per_route,
        "label_strikes": float(len(strikes)),
        "marker_clearance": _min_non_consumer_clearance(geometry_graph, polylines),
        "excessive_gaps": float(counts["excessive_column_gap"]),
        "wasted_canvas": _wasted_canvas_ratio(geometry_graph, routes, canvas),
    }


def _plan_label_strikes(plan: RenderPlan) -> set[tuple[str, str]]:
    """Distinct line/station label strikes in settled plan geometry."""
    from nf_metro.layout.labels import segment_strikes_label

    placements = {
        placement.station_id: placement
        for placement in plan.labels
        if placement.station_id
    }
    strikes: set[tuple[str, str]] = set()
    for route, (_line_id, points) in zip(plan.routes, plan.offset_polylines()):
        for station_id, placement in placements.items():
            if station_id in (route.edge.source, route.edge.target):
                continue
            if route.line_id in plan.graph.station_lines(station_id):
                continue
            if any(
                segment_strikes_label(*start, *end, placement)
                for start, end in zip(points, points[1:])
            ):
                strikes.add((route.line_id, station_id))
    return strikes


def _wasted_canvas_ratio(
    graph: MetroGraph,
    routes: list[RoutedPath],
    canvas: tuple[float, float] | None,
) -> float:
    """Fraction of the canvas area not enclosed by the content bounding box.

    Content spans the visible stations, section boxes, and routed waypoints;
    the canvas is the rendered ``(width, height)``.  A diagram whose content
    fills the canvas scores ~0; one stranded in a corner of a large canvas
    scores high.
    """
    xs: list[float] = []
    ys: list[float] = []
    for s in graph.stations.values():
        if s.is_port or s.is_hidden:
            continue
        xs.append(s.x)
        ys.append(s.y)
    for sec in graph.sections.values():
        if sec.bbox_w > 0:
            xs.extend((sec.bbox_x, sec.bbox_x + sec.bbox_w))
            ys.extend((sec.bbox_y, sec.bbox_y + sec.bbox_h))
    for r in routes:
        for px, py in r.points:
            xs.append(px)
            ys.append(py)
    if not xs or not ys:
        return 0.0

    content_w = max(xs) - min(xs)
    content_h = max(ys) - min(ys)
    if canvas is not None:
        canvas_w, canvas_h = canvas
    else:
        from nf_metro.render.constants import CANVAS_PADDING

        canvas_w = max(xs) + CANVAS_PADDING
        canvas_h = max(ys) + CANVAS_PADDING
    if canvas_w <= 0 or canvas_h <= 0:
        return 0.0

    used = (content_w * content_h) / (canvas_w * canvas_h)
    return round(max(0.0, min(1.0, 1.0 - used)), 3)


def _format_magnitude(kind: MetricKind, value: float) -> str:
    """Format a non-negative magnitude for display, by value kind."""
    if kind == "ratio":
        return f"{value:.0%}"
    if kind == "decimal":
        return f"{value:.1f}"
    return f"{value:.0f}"


def format_value(spec: MetricSpec, value: float | None) -> str:
    """Render a metric value for display (``n/a`` when missing)."""
    if value is None:
        return "n/a"
    return _format_magnitude(spec.kind, value)


def format_delta(spec: MetricSpec, base: float | None, pr: float | None) -> str:
    """Render a base->PR delta as a signed magnitude, or ``""`` when undefined."""
    if base is None or pr is None:
        return ""
    delta = pr - base
    if abs(delta) < 1e-9:
        return "0"
    sign = "+" if delta > 0 else "−"
    return f"{sign}{_format_magnitude(spec.kind, abs(delta))}"


def delta_direction(spec: MetricSpec, base: float | None, pr: float | None) -> int:
    """Sign of a base->PR change: ``-1`` better, ``+1`` worse, ``0`` flat/undefined."""
    if base is None or pr is None:
        return 0
    delta = pr - base
    if abs(delta) < 1e-9:
        return 0
    grew = delta > 0
    return -1 if grew == spec.higher_is_better else 1
