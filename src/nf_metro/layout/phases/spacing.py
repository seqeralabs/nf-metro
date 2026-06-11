"""Global y-spacing widening search that clears residual label overlaps."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nf_metro.layout.constants import DIAGONAL_SLOPE_RATIO
from nf_metro.layout.phases._common import _restoring_layout_geometry
from nf_metro.parser.model import MetroGraph

if TYPE_CHECKING:
    from nf_metro.layout.labels import LabelOverlap, LabelPlacement
    from nf_metro.layout.routing.common import RoutedPath

# Cap on spread-loop passes.  Each pass strictly widens the binding axis,
# so a handful suffices to clear any realistic crowding before giving up.
_MAX_SPREAD_ITERS = 6

# Extra clearance (px) added on top of the measured intrusion when widening
# spacing, so the re-laid-out labels land with a small gap, not flush.
_SPREAD_SLACK = 4.0

_Probe = tuple[
    dict[tuple[str, str], float],
    "list[RoutedPath]",
    "list[LabelPlacement]",
]


def _probe_label_placements(
    graph: MetroGraph, *, allow_hyphenation: bool
) -> _Probe | None:
    """Run the renderer's offset/route/label pipeline at the current layout.

    Returns the computed ``(station_offsets, routes, placements)`` so callers
    can inspect the settled geometry and labelling the renderer would draw, or
    ``None`` if routing/placement raises (a transient failure never blocks
    layout).  Snapshots and restores the in-place mutations route/place make.

    The render-time wrapped-label trunk lift is held off here so the spacing
    search and the label-overlap guard reason about the unlifted geometry; the
    lift only moves a label closer to its own pill and never changes which
    labels overlap their neighbours in a way the search should react to.
    """
    from nf_metro.layout.labels import place_labels
    from nf_metro.layout.routing import compute_station_offsets, route_edges

    with _restoring_layout_geometry(graph):
        try:
            offsets = compute_station_offsets(graph)
            routes = route_edges(graph, station_offsets=offsets)
            placements = place_labels(
                graph,
                station_offsets=offsets,
                routes=routes,
                allow_hyphenation=allow_hyphenation,
                label_angle=graph.label_angle or 0.0,
                lift_wrapped_off_trunks=False,
            )
            return offsets, routes, placements
        except Exception:
            return None


def _overlaps_from(
    graph: MetroGraph,
    offsets: dict[tuple[str, str], float],
    placements: list[LabelPlacement],
) -> list[LabelOverlap]:
    """Leftover label overlaps in a probed placement set.

    Overlaps involving a rail-section station are dropped: rail sections run a
    dedicated layout with their own column pitch, so widening the normal global
    X spacing cannot fix them and would only needlessly bloat the normal
    sections.
    """
    from nf_metro.layout.labels import find_label_overlaps

    overlaps = find_label_overlaps(graph, placements, offsets)
    if not graph.has_rail_sections:
        return overlaps

    def _in_rail(station_id: str) -> bool:
        st = graph.stations.get(station_id)
        return bool(st and st.section_id and graph.is_rail_section(st.section_id))

    return [o for o in overlaps if not (_in_rail(o.a) or _in_rail(o.b))]


def _struck_label_station_ids(
    graph: MetroGraph,
    offsets: dict[tuple[str, str], float],
    routes: list[RoutedPath],
    placements: list[LabelPlacement],
) -> set[str]:
    """Stations whose horizontal name label a diagonal route crosses.

    The visual goal: a fan-in/fan-out, convergence, or descent diagonal
    transitioning through a station's drawn name reads as a strike-through --
    whether or not the station carries that line.  A wide label sits in the path
    of its own line's sweep just as it sits in a foreign line's, so ownership is
    not an exemption; the flat run must lengthen until the transition clears the
    glyphs either way.

    Segments a longer flat run cannot relocate are excluded: flat (near-
    horizontal) trunk runs, bypass-V crossings, off-track output sweeps (placed
    by the off-track machinery, not the in-grid runway), and angled labels
    (handled by their rotated footprint).
    """
    from nf_metro.layout.labels import segment_strikes_label
    from nf_metro.render.svg import apply_route_offsets

    def _off_track(node_id: str) -> bool:
        st = graph.stations.get(node_id)
        return bool(st and st.off_track)

    seg_lists = [
        (
            apply_route_offsets(r, offsets),
            r.edge.source.startswith("__bypass_")
            or r.edge.target.startswith("__bypass_")
            or _off_track(r.edge.source)
            or _off_track(r.edge.target),
        )
        for r in routes
    ]
    struck: set[str] = set()
    for p in placements:
        station = graph.stations.get(p.station_id)
        if station is None or not station.label.strip() or p.angle:
            continue
        for pts, skip in seg_lists:
            if skip:
                continue
            if any(
                segment_strikes_label(x1, y1, x2, y2, p)
                and abs(y2 - y1) >= max(abs(x2 - x1), 1.0) * DIAGONAL_SLOPE_RATIO
                for (x1, y1), (x2, y2) in zip(pts, pts[1:])
            ):
                struck.add(p.station_id)
                break
    return struck


def _collinear_from(
    graph: MetroGraph,
    offsets: dict[tuple[str, str], float],
    routes: list[RoutedPath],
) -> bool:
    """Whether probed routes draw two distinct lines on top of each other.

    Runs the inter- and intra-section collinear-overlay checks the final-phase
    guards enforce, so the strike-clearance loop can step back a widening that
    would overshoot into a collinear defect rather than ship it.
    """
    from nf_metro.layout.routing.invariants import (
        check_intra_section_collinear_distinct_lines,
        check_no_collinear_distinct_lines,
    )

    return bool(
        check_no_collinear_distinct_lines(graph, routes, offsets)
        or check_intra_section_collinear_distinct_lines(graph, routes, offsets)
    )


def _residual_label_overlaps(
    graph: MetroGraph, *, allow_hyphenation: bool
) -> list[LabelOverlap]:
    """Place labels at the current layout and report leftover overlaps.

    The spread loop calls this with ``allow_hyphenation=False`` so residual
    overlaps surface (to be cleared by widening spacing rather than by
    hard-breaking words); the final guard calls it with True to validate the
    settled, fully wrapped state the renderer will draw.  Returns an empty list
    if routing/placement raises.
    """
    probe = _probe_label_placements(graph, allow_hyphenation=allow_hyphenation)
    if probe is None:
        return []
    offsets, _routes, placements = probe
    return _overlaps_from(graph, offsets, placements)


def _struck_stations_and_collinear(graph: MetroGraph) -> tuple[set[str], bool]:
    """One probe: stations whose label a diagonal crosses, and a collinear flag.

    Returns ``(station_ids, has_collinear)`` where ``station_ids`` are stations
    whose horizontal label a diagonal route rakes, and ``has_collinear`` is
    whether the routes draw two distinct lines on top of each other.  Both read
    the same probed routes, so the strike-clearance loop decides growth and
    step-back from a single route+place pass.

    Probes with ``allow_hyphenation=True`` -- the renderer-faithful wrapping --
    so a strike is judged against the label the renderer draws; the collinear
    check ignores placements, so the hyphenation flag does not affect it.
    Returns ``(set(), False)`` on probe failure.  See
    :func:`_struck_label_station_ids` and :func:`_collinear_from`.
    """
    probe = _probe_label_placements(graph, allow_hyphenation=True)
    if probe is None:
        return set(), False
    offsets, routes, placements = probe
    struck = _struck_label_station_ids(graph, offsets, routes, placements)
    return struck, _collinear_from(graph, offsets, routes)


def _placed_name_label_station_ids(graph: MetroGraph) -> set[str]:
    """Return station ids that ``place_labels`` emits a name label for.

    Probes with the renderer-faithful ``allow_hyphenation=True`` so the result
    reflects the settled labelling that will actually be drawn.  Returns an
    empty set if routing/placement raises.
    """
    probe = _probe_label_placements(graph, allow_hyphenation=True)
    if probe is None:
        return set()
    _offsets, _routes, placements = probe
    return {p.station_id for p in placements if p.station_id}


def _spread_bump(
    graph: MetroGraph,
    residual: list[LabelOverlap],
    x_spacing: float,
    y_spacing: float,
    auto_x: bool,
    auto_y: bool,
) -> tuple[float, float]:
    """Compute widened (x, y) spacing to clear the residual label overlaps.

    Each overlap is attributed to the axis along which its two stations are
    separated (columns -> x, rows -> y).  The required extra pitch is the
    intrusion depth shared across the columns/rows between them, plus slack.
    Only auto-resolved axes are widened; a pinned axis is left untouched.
    """
    extra_x = 0.0
    extra_y = 0.0
    for ov in residual:
        a = graph.stations.get(ov.a)
        b = graph.stations.get(ov.b)
        if a is None or b is None:
            continue
        dx = abs(a.x - b.x)
        dy = abs(a.y - b.y)
        if dx >= dy:
            cols = max(round(dx / x_spacing), 1)
            extra_x = max(extra_x, (ov.ox + _SPREAD_SLACK) / cols)
        else:
            rows = max(round(dy / y_spacing), 1)
            extra_y = max(extra_y, (ov.oy + _SPREAD_SLACK) / rows)
    new_x = x_spacing + extra_x if auto_x else x_spacing
    new_y = y_spacing + extra_y if auto_y else y_spacing
    return new_x, new_y


def _bypass_label_obstacles(
    graph: MetroGraph,
) -> dict[str, tuple[float, float, float, float]]:
    """Glyph-ink bbox of each bypassed station's label, keyed by its bypass V.

    Probes the renderer's settled labelling and, for every hidden bypass-V
    helper station, records the drawn glyph-ink box of the station the V routes
    around.  The router consumes this to seat the V's flat-run corners clear of
    that label.  Boxes are in rendered (offset-applied) coordinates and only
    cover Vs whose bypassed station carries a drawn name; the result is empty
    when no bypass V exists or the probe fails.
    """
    from nf_metro.layout.labels import label_glyph_ink_bbox

    vs = [
        st
        for st in graph.stations.values()
        if st.is_hidden and st.bypasses_station_id is not None
    ]
    if not vs:
        return {}
    probe = _probe_label_placements(graph, allow_hyphenation=True)
    if probe is None:
        return {}
    _offsets, _routes, placements = probe
    ink_by_sid = {
        p.station_id: label_glyph_ink_bbox(p)
        for p in placements
        if p.station_id and not p.angle
    }
    obstacles: dict[str, tuple[float, float, float, float]] = {}
    for st in vs:
        sid = st.bypasses_station_id
        box = ink_by_sid.get(sid) if sid is not None else None
        if box is not None:
            obstacles[st.id] = box
    return obstacles
