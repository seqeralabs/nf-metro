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

Module-level imports are kept stdlib-only so the spec and formatting helpers
can be imported by ``build_render_diff.py`` without pulling in the layout
engine; the heavy ``nf_metro`` imports live inside ``compute_metrics``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nf_metro.layout.routing.common import RoutedPath
    from nf_metro.parser.model import MetroGraph


@dataclass(frozen=True)
class MetricSpec:
    """One layout-quality score: its JSON key, display label, and value kind."""

    key: str
    label: str
    kind: str  # "count" or "ratio"


# Every metric is oriented so LOWER IS BETTER: a negative delta is an
# improvement.  This list is the canonical schema for ``metrics.json`` and the
# column order of the render-diff metrics table.
METRICS: list[MetricSpec] = [
    MetricSpec("crossings", "Crossings", "count"),
    MetricSpec("near_horizontal", "Near-horiz.", "count"),
    MetricSpec("single_diagonals", "Lone diag.", "count"),
    MetricSpec("label_strikes", "Label strikes", "count"),
    MetricSpec("excessive_gaps", "Excess gaps", "count"),
    MetricSpec("wasted_canvas", "Wasted canvas", "ratio"),
]

METRIC_KEYS: list[str] = [m.key for m in METRICS]


def compute_metrics(
    graph: MetroGraph, *, canvas: tuple[float, float] | None = None
) -> dict[str, float]:
    """Compute the layout-quality scorecard for one laid-out graph.

    ``canvas`` is the rendered SVG ``(width, height)`` in user units; when
    omitted (unit tests) the canvas extent is estimated from the laid-out
    geometry.
    """
    from collections import Counter

    from layout_validator import validate_layout

    from nf_metro.layout.phases.guards import iter_line_label_strikes
    from nf_metro.layout.routing import compute_station_offsets, route_edges

    counts = Counter(v.check for v in validate_layout(graph))

    try:
        offsets = compute_station_offsets(graph)
        routes = route_edges(graph, station_offsets=offsets)
    except Exception:  # noqa: BLE001 - routing failure surfaces in the validator
        offsets, routes = {}, []

    # Distinct (line, station) strikes: one visual mark per line crossing a
    # label, not one per route segment that happens to clip it.
    strikes = {
        (s.line_id, s.station_id)
        for s in iter_line_label_strikes(graph, offsets=offsets, routes=routes)
    }

    return {
        "crossings": float(
            counts["route_segment_crossing"] + counts["inter_section_line_crossing"]
        ),
        "near_horizontal": float(counts["almost_horizontal_edge"]),
        "single_diagonals": float(counts["single_segment_diagonal"]),
        "label_strikes": float(len(strikes)),
        "excessive_gaps": float(counts["excessive_column_gap"]),
        "wasted_canvas": _wasted_canvas_ratio(graph, routes, canvas),
    }


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


def _format_magnitude(kind: str, value: float) -> str:
    """Format a non-negative magnitude as a percentage (ratio) or integer (count)."""
    if kind == "ratio":
        return f"{value:.0%}"
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


def delta_direction(base: float | None, pr: float | None) -> int:
    """Sign of a base->PR change: ``-1`` better, ``+1`` worse, ``0`` flat/undefined.

    Every metric is lower-is-better, so a drop is an improvement.
    """
    if base is None or pr is None:
        return 0
    delta = pr - base
    if abs(delta) < 1e-9:
        return 0
    return 1 if delta > 0 else -1
