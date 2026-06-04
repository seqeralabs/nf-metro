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

from nf_metro.layout.constants import DIAGONAL_RUN, MIN_STRAIGHT_EDGE
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

    # Ports carry an averaged Y; resolve to the line's own rail in the port's
    # section so inter-section legs stay on the correct rail per line.
    port = graph.ports.get(station_id)
    if port is not None:
        section_rails = graph._rail_y.get(port.section_id, {})
        if line_id in section_rails:
            return section_rails[line_id]

    if st.rail_top_y is None or st.rail_bottom_y is None:
        return st.y

    # Each served line's rail Y is recorded directly in rail_used_ys, parallel
    # to the station's served-line order, so look it up rather than assuming
    # the used rails evenly fill the span (a station may span a rail it does
    # not use, which would break an interpolated estimate).
    served = _station_lines_in_order(graph, station_id)
    if line_id in served and len(st.rail_used_ys) == len(served):
        return st.rail_used_ys[served.index(line_id)]
    return st.y


def _off_track_drop_stagger(
    graph: MetroGraph,
    feeder: object,
    edge: object,
    rail_y: float,
) -> float:
    """Horizontal offset for an off-track feeder line's vertical drop.

    Several lines feeding the same consumer from one off-track input would
    otherwise drop on the same X and overlap in the vertical leg (merging into
    one fat line).  Each feeding line instead drops on its own X, ordered by
    its target rail Y (top rail leftmost), one OFFSET_STEP apart and centred on
    the feeder, so the bundle stays as parallel lines and the elbows form a
    tidy staircase into the rails.
    """
    from nf_metro.layout.constants import OFFSET_STEP

    feeder_id = feeder.id  # type: ignore[attr-defined]
    consumer_id = (
        edge.target if edge.source == feeder_id else edge.source  # type: ignore[attr-defined]
    )
    # Sibling feeder lines: every line carried by an edge between this feeder
    # and the same consumer, ordered by their target rail Y.
    sib_rails: list[tuple[float, str]] = []
    for e in graph.edges:
        if {e.source, e.target} != {feeder_id, consumer_id}:
            continue
        on_rail_id = e.target if e.source == feeder_id else e.source
        sib_rails.append((_line_rail_y(graph, on_rail_id, e.line_id), e.line_id))
    # Order the drop Xs so the bundle does NOT twist through the drop->rail
    # elbow: the line landing on the LOWER rail (larger Y) drops on the LEFT
    # (smaller X), matching the left/right ordering the rightward outgoing run
    # expects (a D->R corner maps the down run's left side to the run's bottom).
    sib_rails.sort(reverse=True)
    order = [lid for _y, lid in sib_rails]
    if edge.line_id not in order or len(order) <= 1:  # type: ignore[attr-defined]
        return 0.0
    k = order.index(edge.line_id)  # type: ignore[attr-defined]
    return (k - (len(order) - 1) / 2.0) * OFFSET_STEP


def _diagonal_placement(
    sx: float,
    tx: float,
    sy: float,
    ty: float,
    is_fork: bool,
    is_join: bool,
) -> tuple[float, float]:
    """X coordinates of a 45-degree diagonal joining two rails along an X run.

    The diagonal climbs/falls between rail Ys at 45 degrees, so its horizontal
    span equals the vertical rail separation ``|ty - sy|`` (clamped to the
    available run so it never inverts).  The flat lead-in/out keeps a minimum
    straight stub at each end; the diagonal is biased toward the fan's shared
    convergence point (the fork source or the join target) so a rail eases off
    the fan early and runs flat the rest of the column.
    """
    dx = tx - sx
    sign = 1.0 if dx >= 0 else -1.0
    span = abs(dx)
    diag = min(abs(ty - sy), DIAGONAL_RUN)
    # Keep a straight stub at each end, shrinking the diagonal if the column is
    # too narrow to seat both stubs plus the diagonal.
    stub = MIN_STRAIGHT_EDGE
    if diag + 2 * stub > span:
        diag = max(0.0, span - 2 * stub)
        if diag <= 0.0:
            stub = span / 2
    if is_fork and not is_join:
        diag_start = sx + sign * stub
    elif is_join and not is_fork:
        diag_start = tx - sign * (stub + diag)
    else:
        mid = (sx + tx) / 2
        diag_start = mid - sign * diag / 2
    diag_end = diag_start + sign * diag
    return diag_start, diag_end


def route_rail_edges(
    graph: MetroGraph,
    edges: list | None = None,
) -> list[RoutedPath]:
    """Route edges as straight horizontal runs along their line's rail.

    The two endpoints of an edge are on the same line, hence the same rail,
    so each route is two points at a common Y.  When the endpoints' resolved
    rail Ys differ slightly (e.g. a port at a section's mid rail vs. a pill's
    own rail), a short vertical jog at the source X joins them so the run
    stays axis-aligned rather than diagonal.

    When *edges* is None every edge in the graph is routed (legacy global
    rail mode).  In per-section rail mode the caller passes just that
    section's internal edges so the normal router handles the rest.
    """
    routes: list[RoutedPath] = []
    for edge in edges if edges is not None else graph.edges:
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if src is None or tgt is None:
            continue

        # Off-track input: the source sits above the rails and feeds into the
        # target's rail.  Drop straight down (a clean perpendicular crossing of
        # any rails above the target reads far better than a steep diagonal
        # merge), then turn onto the rail with a rounded elbow and run flat into
        # the consumer.  Sibling feeder lines (a bundle feeding the same
        # consumer) drop on staggered Xs - one per rail, lower rails turning
        # later - so the bundle stays two parallel lines and never merges.
        off_src = src.off_track and not tgt.off_track
        off_tgt = tgt.off_track and not src.off_track
        if off_src or off_tgt:
            feeder = src if off_src else tgt
            on_rail = tgt if off_src else src
            rail_y = _line_rail_y(graph, on_rail.id, edge.line_id)
            drop_x = feeder.x + _off_track_drop_stagger(graph, feeder, edge, rail_y)
            l_points = [
                (drop_x, feeder.y),
                (drop_x, rail_y),
                (on_rail.x, rail_y),
            ]
            if off_tgt:
                l_points.reverse()
            routes.append(
                RoutedPath(
                    edge=edge,
                    line_id=edge.line_id,
                    points=l_points,
                    offsets_applied=True,
                )
            )
            continue

        y_src = _line_rail_y(graph, edge.source, edge.line_id)
        y_tgt = _line_rail_y(graph, edge.target, edge.line_id)

        if abs(y_src - y_tgt) < 0.5:
            points = [(src.x, y_src), (tgt.x, y_src)]
        else:
            # The endpoints sit on different rails: this is a fan-out (the
            # source is a shared convergence point, e.g. the CRAM input) or a
            # fan-in (the target is a shared collector, e.g. the VCF output).
            # Ease between the two rails with a 45-degree diagonal transition -
            # the metro-map convention used by the normal router - rather than
            # a square right-angle jog.  Bias the diagonal toward whichever
            # endpoint is the shared convergence point so the rail leaves/joins
            # the fan on a diagonal and runs flat the rest of the way.
            is_fork = len(graph.edges_from(edge.source)) > 1
            is_join = len(graph.edges_to(edge.target)) > 1
            diag_start_x, diag_end_x = _diagonal_placement(
                src.x, tgt.x, y_src, y_tgt, is_fork, is_join
            )
            points = [
                (src.x, y_src),
                (diag_start_x, y_src),
                (diag_end_x, y_tgt),
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
