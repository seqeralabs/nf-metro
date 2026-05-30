"""Global y-spacing widening search that clears residual label overlaps."""

from __future__ import annotations

from nf_metro.parser.model import MetroGraph

# Cap on spread-loop passes.  Each pass strictly widens the binding axis,
# so a handful suffices to clear any realistic crowding before giving up.
_MAX_SPREAD_ITERS = 6

# Extra clearance (px) added on top of the measured intrusion when widening
# spacing, so the re-laid-out labels land with a small gap, not flush.
_SPREAD_SLACK = 4.0


def _residual_label_overlaps(graph: MetroGraph, *, allow_hyphenation: bool):
    """Place labels at the current layout and report leftover overlaps.

    Runs the same offset/route/label pipeline the renderer uses (so the
    wrapping pass has already fired) and returns the overlaps that wrapping
    could not resolve.  Returns an empty list if routing/placement raises,
    so a transient routing failure never blocks layout.

    The spread loop calls this with ``allow_hyphenation=False`` so residual
    overlaps surface (to be cleared by widening spacing rather than by
    hard-breaking words); the final guard calls it with True to validate the
    settled, fully wrapped state the renderer will draw.
    """
    from nf_metro.layout.labels import find_label_overlaps, place_labels
    from nf_metro.layout.routing import compute_station_offsets, route_edges

    # Routing and placement mutate the graph (route_edges nudges station X
    # for bundle separation; place_labels expands section bboxes to fit
    # labels).  This probe must not leak those mutations, or a clean graph
    # would drift from its positioned state.  Snapshot station coordinates
    # and section bboxes, and restore them after measuring.
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
        )
        return find_label_overlaps(graph, placements, offsets)
    except Exception:
        return []
    finally:
        for sid, (x, y) in pos_snapshot.items():
            st = graph.stations.get(sid)
            if st is not None:
                st.x, st.y = x, y
        for sid, (bx, by, bw, bh) in bbox_snapshot.items():
            s = graph.sections.get(sid)
            if s is not None:
                s.bbox_x, s.bbox_y, s.bbox_w, s.bbox_h = bx, by, bw, bh


def _spread_bump(graph, residual, x_spacing, y_spacing, auto_x, auto_y):
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
