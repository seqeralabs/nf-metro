"""Dedicated edge router for opt-in rail mode.

In rail mode each metro line runs along a single fixed horizontal rail Y
(see ``layout/rail_mode.py``).  An edge therefore connects two points that
both sit on *this line's* rail, so the route is a straight horizontal run at
that rail Y from the source X to the target X.  The station pills (drawn by
the renderer) bridge across rails; the rails themselves never converge.

A line that uses a station meets that station's pill at its own rail Y.  A
line that does not use a station simply passes straight along its rail; if
that rail falls between two rails the station spans, the rail visually
crosses the pill (acceptable for v1 - see the feature notes).
"""

from __future__ import annotations

__all__ = ["route_rail_edges"]

from nf_metro.layout.routing.common import RoutedPath
from nf_metro.parser.model import MetroGraph


def _line_rail_y(graph: MetroGraph, station_id: str, line_id: str) -> float:
    """Return the Y at which *line_id* meets *station_id* in rail mode.

    A single-rail station sits at its ``y``.  A multi-rail (spanning) station
    stores its rail range on ``rail_top_y``/``rail_bottom_y``; the line meets
    the pill at the rail Y for that line, which is recoverable from the
    station's served lines and the even rail spacing.  When the rail span is
    unknown the station's own ``y`` is used (single-rail fallback).
    """
    from nf_metro.layout.rail_mode import _station_lines_in_order

    st = graph.stations.get(station_id)
    if st is None:
        return 0.0
    if st.rail_top_y is None or st.rail_bottom_y is None:
        return st.y

    # Recover this line's rail within the station's span.  The station's
    # served lines are evenly spaced from rail_top_y to rail_bottom_y in
    # line-definition order, so interpolate the line's index.
    served = _station_lines_in_order(graph, station_id)
    if line_id not in served:
        return st.y
    n = len(served)
    if n <= 1:
        return st.y
    idx = served.index(line_id)
    frac = idx / (n - 1)
    return st.rail_top_y + frac * (st.rail_bottom_y - st.rail_top_y)


def route_rail_edges(graph: MetroGraph) -> list[RoutedPath]:
    """Route every edge as a straight horizontal run along its line's rail.

    The two endpoints of an edge are on the same line, hence the same rail,
    so each route is two points at a common Y.  When the endpoints' resolved
    rail Ys differ slightly (e.g. a port at a section's mid rail vs. a pill's
    own rail), a short vertical jog at the source X joins them so the run
    stays axis-aligned rather than diagonal.
    """
    routes: list[RoutedPath] = []
    for edge in graph.edges:
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if src is None or tgt is None:
            continue

        y_src = _line_rail_y(graph, edge.source, edge.line_id)
        y_tgt = _line_rail_y(graph, edge.target, edge.line_id)

        if abs(y_src - y_tgt) < 0.5:
            points = [(src.x, y_src), (tgt.x, y_src)]
        else:
            # Run horizontally at the source rail, then a short vertical jog
            # near the target, then into the target.  Keeps every leg
            # axis-aligned (no diagonal) so the rail look is preserved.
            mid_x = (src.x + tgt.x) / 2
            points = [
                (src.x, y_src),
                (mid_x, y_src),
                (mid_x, y_tgt),
                (tgt.x, y_tgt),
            ]

        routes.append(
            RoutedPath(
                edge=edge,
                line_id=edge.line_id,
                points=points,
                offsets_applied=True,
            )
        )
    return routes
