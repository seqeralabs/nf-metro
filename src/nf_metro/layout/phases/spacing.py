"""Global y-spacing widening search that clears residual label overlaps."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nf_metro.parser.model import MetroGraph

if TYPE_CHECKING:
    from nf_metro.layout.labels import LabelOverlap, LabelPlacement

# Cap on spread-loop passes.  Each pass strictly widens the binding axis,
# so a handful suffices to clear any realistic crowding before giving up.
_MAX_SPREAD_ITERS = 6

# Extra clearance (px) added on top of the measured intrusion when widening
# spacing, so the re-laid-out labels land with a small gap, not flush.
_SPREAD_SLACK = 4.0


def _probe_label_placements(
    graph: MetroGraph, *, allow_hyphenation: bool
) -> tuple[dict[tuple[str, str], float], list[LabelPlacement]] | None:
    """Run the renderer's offset/route/label pipeline at the current layout.

    Returns the computed ``(station_offsets, placements)`` so callers can
    inspect the settled labelling the renderer would draw, or ``None`` if
    routing/placement raises (a transient failure never blocks layout).

    Routing and placement mutate the graph (route_edges nudges station X for
    bundle separation; place_labels expands section bboxes to fit labels).
    This probe snapshots station coordinates and section bboxes and restores
    them, so it never leaks those mutations into the positioned state.
    """
    from nf_metro.layout.labels import place_labels
    from nf_metro.layout.routing import compute_station_offsets, route_edges

    # Resolve the effective label angle so the spread search sizes column
    # spacing for the same (possibly rotated, hence narrower) footprint the
    # renderer will draw.  graph.label_angle is None when no directive set it;
    # the theme default is horizontal (0), so None -> 0 here (#527).
    label_angle = graph.label_angle or 0.0

    pos_snapshot = {sid: (s.x, s.y) for sid, s in graph.stations.items()}
    bbox_snapshot = {
        sid: (s.bbox_x, s.bbox_y, s.bbox_w, s.bbox_h)
        for sid, s in graph.sections.items()
    }
    try:
        offsets = compute_station_offsets(graph)
        routes = route_edges(graph, station_offsets=offsets)
        placements = place_labels(
            graph,
            station_offsets=offsets,
            routes=routes,
            allow_hyphenation=allow_hyphenation,
            label_angle=label_angle,
        )
        return offsets, placements
    except Exception:
        return None
    finally:
        for sid, (x, y) in pos_snapshot.items():
            st = graph.stations.get(sid)
            if st is not None:
                st.x, st.y = x, y
        for sid, (bx, by, bw, bh) in bbox_snapshot.items():
            s = graph.sections.get(sid)
            if s is not None:
                s.bbox_x, s.bbox_y, s.bbox_w, s.bbox_h = bx, by, bw, bh


def _residual_label_overlaps(
    graph: MetroGraph, *, allow_hyphenation: bool
) -> list[LabelOverlap]:
    """Place labels at the current layout and report leftover overlaps.

    Returns the overlaps that wrapping could not resolve, or an empty list if
    routing/placement raises.

    The spread loop calls this with ``allow_hyphenation=False`` so residual
    overlaps surface (to be cleared by widening spacing rather than by
    hard-breaking words); the final guard calls it with True to validate the
    settled, fully wrapped state the renderer will draw.

    Overlaps involving a rail-section station are dropped: rail sections run a
    dedicated layout with their own column pitch, so widening the normal global
    X spacing cannot fix them and would only needlessly bloat the normal
    sections.
    """
    from nf_metro.layout.labels import find_label_overlaps

    probe = _probe_label_placements(graph, allow_hyphenation=allow_hyphenation)
    if probe is None:
        return []
    offsets, placements = probe
    overlaps = find_label_overlaps(graph, placements, offsets)
    if not graph.has_rail_sections:
        return overlaps

    def _in_rail(station_id: str) -> bool:
        st = graph.stations.get(station_id)
        return bool(st and st.section_id and graph.is_rail_section(st.section_id))

    return [o for o in overlaps if not (_in_rail(o.a) or _in_rail(o.b))]


def _placed_name_label_station_ids(graph: MetroGraph) -> set[str]:
    """Return station ids that ``place_labels`` emits a name label for.

    Probes with the renderer-faithful ``allow_hyphenation=True`` so the result
    reflects the settled labelling that will actually be drawn.  Returns an
    empty set if routing/placement raises.
    """
    probe = _probe_label_placements(graph, allow_hyphenation=True)
    if probe is None:
        return set()
    _offsets, placements = probe
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
