"""Layout validator: programmatic checks for layout defects.

Runs a suite of checks against a laid-out MetroGraph and returns
a list of Violation objects describing any problems found.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum

from nf_metro.layout.constants import Y_SPACING
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import RoutedPath, is_orthogonal_turn
from nf_metro.parser.model import MetroGraph, PortSide
from nf_metro.render.svg import apply_route_offsets


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass
class Violation:
    check: str
    severity: Severity
    message: str
    context: dict = field(default_factory=dict)


def shared_same_line_turn_vertices(
    routes: list[RoutedPath],
) -> set[tuple[str, int, int]]:
    """``(line, x, y)`` turn vertices where two or more same-line legs coincide.

    Mirrors the bucketing of ``_unify_coincident_corner_radii``: these are the
    corners a fused same-line stroke shares, where every leg must draw one radius.
    """
    counts: dict[tuple[str, int, int], int] = defaultdict(int)
    for rp in routes:
        pts = rp.points
        for i in range(1, len(pts) - 1):
            if is_orthogonal_turn(pts[i - 1], pts[i], pts[i + 1]):
                counts[(rp.line_id, round(pts[i][0]), round(pts[i][1]))] += 1
    return {key for key, n in counts.items() if n >= 2}


def _compute_routes(
    graph: MetroGraph,
) -> tuple[dict[tuple[str, str], float], list[RoutedPath]] | None:
    """Compute offsets and routes once for all routing-dependent checks."""
    try:
        offsets = compute_station_offsets(graph)
        routes = route_edges(graph, station_offsets=offsets)
        return offsets, routes
    except Exception:
        return None


def validate_layout(graph: MetroGraph) -> list[Violation]:
    """Run all layout checks and return violations."""
    violations: list[Violation] = []
    violations.extend(check_section_overlap(graph))
    violations.extend(check_station_containment(graph))
    violations.extend(check_port_boundary(graph))
    violations.extend(check_coordinate_sanity(graph))
    violations.extend(check_coincident_stations(graph))
    violations.extend(check_minimum_section_spacing(graph))

    # Compute offsets + routes once for all routing-dependent checks.
    precomputed = _compute_routes(graph)
    violations.extend(check_edge_waypoints(graph, _precomputed=precomputed))
    if precomputed is not None:
        violations.extend(check_label_overlap(graph, _precomputed=precomputed))
        violations.extend(check_edge_section_crossing(graph, _precomputed=precomputed))
        violations.extend(
            check_bypass_section_clearance(graph, _precomputed=precomputed)
        )
        violations.extend(
            check_almost_horizontal_edges(graph, _precomputed=precomputed)
        )
    violations.extend(check_single_layer_centering(graph))
    violations.extend(check_station_as_elbow(graph))
    violations.extend(check_intra_section_chain_alignment(graph))
    violations.extend(check_exit_port_feeder_alignment(graph))
    violations.extend(check_inter_section_line_crossings(graph))
    violations.extend(check_excessive_column_gaps(graph))
    if precomputed is not None:
        violations.extend(
            check_single_segment_diagonals(graph, _precomputed=precomputed)
        )
        violations.extend(
            check_route_segment_crossings(graph, _precomputed=precomputed)
        )
        violations.extend(
            check_serpentine_no_backtrack(graph, _precomputed=precomputed)
        )
    return violations


def check_section_overlap(
    graph: MetroGraph, tolerance: float = -1.0
) -> list[Violation]:
    """Check that no two section bounding boxes overlap.

    A small negative tolerance allows sections to be flush (touching)
    but not overlapping. Positive tolerance would require a gap.
    """
    violations: list[Violation] = []
    sections = [
        (sid, s) for sid, s in graph.sections.items() if s.bbox_w > 0 and s.bbox_h > 0
    ]

    for i in range(len(sections)):
        sid_a, a = sections[i]
        ax1, ay1 = a.bbox_x, a.bbox_y
        ax2, ay2 = ax1 + a.bbox_w, ay1 + a.bbox_h

        for j in range(i + 1, len(sections)):
            sid_b, b = sections[j]
            bx1, by1 = b.bbox_x, b.bbox_y
            bx2, by2 = bx1 + b.bbox_w, by1 + b.bbox_h

            # AABB overlap test with tolerance
            overlap_x = ax2 - tolerance > bx1 and bx2 - tolerance > ax1
            overlap_y = ay2 - tolerance > by1 and by2 - tolerance > ay1
            if overlap_x and overlap_y:
                violations.append(
                    Violation(
                        check="section_overlap",
                        severity=Severity.ERROR,
                        message=(
                            f"Sections '{sid_a}' and '{sid_b}' overlap: "
                            f"A=({ax1:.0f},{ay1:.0f},{ax2:.0f},{ay2:.0f}) "
                            f"B=({bx1:.0f},{by1:.0f},{bx2:.0f},{by2:.0f})"
                        ),
                        context={"section_a": sid_a, "section_b": sid_b},
                    )
                )

    return violations


def check_station_containment(
    graph: MetroGraph, margin: float = 5.0
) -> list[Violation]:
    """Check that non-port stations are within their section bbox (with margin)."""
    violations: list[Violation] = []

    for sid, station in graph.stations.items():
        if station.is_port or station.section_id is None:
            continue

        section = graph.sections.get(station.section_id)
        if not section or section.bbox_w == 0:
            continue

        sx1 = section.bbox_x - margin
        sy1 = section.bbox_y - margin
        sx2 = section.bbox_x + section.bbox_w + margin
        sy2 = section.bbox_y + section.bbox_h + margin

        if not (sx1 <= station.x <= sx2 and sy1 <= station.y <= sy2):
            violations.append(
                Violation(
                    check="station_containment",
                    severity=Severity.ERROR,
                    message=(
                        f"Station '{sid}' at ({station.x:.1f},{station.y:.1f}) "
                        f"is outside section '{station.section_id}' "
                        f"bbox ({section.bbox_x:.0f},{section.bbox_y:.0f},"
                        f"{section.bbox_x + section.bbox_w:.0f},"
                        f"{section.bbox_y + section.bbox_h:.0f})"
                    ),
                    context={"station": sid, "section": station.section_id},
                )
            )

    return violations


def check_port_boundary(graph: MetroGraph, tolerance: float = 5.0) -> list[Violation]:
    """Check that port stations are on their section's boundary edge."""
    violations: list[Violation] = []

    for pid, port in graph.ports.items():
        station = graph.stations.get(pid)
        if not station:
            continue

        section = graph.sections.get(port.section_id)
        if not section or section.bbox_w == 0:
            continue

        left = section.bbox_x
        right = section.bbox_x + section.bbox_w
        top = section.bbox_y
        bottom = section.bbox_y + section.bbox_h

        on_boundary = False
        if port.side == PortSide.LEFT:
            on_boundary = abs(station.x - left) <= tolerance
        elif port.side == PortSide.RIGHT:
            on_boundary = abs(station.x - right) <= tolerance
        elif port.side == PortSide.TOP:
            on_boundary = abs(station.y - top) <= tolerance
        elif port.side == PortSide.BOTTOM:
            on_boundary = abs(station.y - bottom) <= tolerance

        if not on_boundary:
            violations.append(
                Violation(
                    check="port_boundary",
                    severity=Severity.WARNING,
                    message=(
                        f"Port '{pid}' (side={port.side.value}) at "
                        f"({station.x:.1f},{station.y:.1f}) is not on "
                        f"section '{port.section_id}' boundary "
                        f"(L={left:.0f},R={right:.0f},T={top:.0f},B={bottom:.0f})"
                    ),
                    context={"port": pid, "section": port.section_id},
                )
            )

    return violations


def check_coordinate_sanity(
    graph: MetroGraph, max_coord: float = 10000.0
) -> list[Violation]:
    """Check for NaN, Inf, or extreme coordinates."""
    violations: list[Violation] = []

    for sid, station in graph.stations.items():
        for coord_name, value in [("x", station.x), ("y", station.y)]:
            if value != value:  # NaN check
                violations.append(
                    Violation(
                        check="coordinate_sanity",
                        severity=Severity.ERROR,
                        message=f"Station '{sid}' has NaN {coord_name}",
                        context={"station": sid, "coordinate": coord_name},
                    )
                )
            elif abs(value) == float("inf"):
                violations.append(
                    Violation(
                        check="coordinate_sanity",
                        severity=Severity.ERROR,
                        message=f"Station '{sid}' has Inf {coord_name}",
                        context={"station": sid, "coordinate": coord_name},
                    )
                )
            elif abs(value) > max_coord:
                violations.append(
                    Violation(
                        check="coordinate_sanity",
                        severity=Severity.WARNING,
                        message=(
                            f"Station '{sid}' has extreme {coord_name}={value:.0f} "
                            f"(>{max_coord})"
                        ),
                        context={"station": sid, "coordinate": coord_name},
                    )
                )

    return violations


def check_coincident_stations(
    graph: MetroGraph, tolerance: float = 1.0
) -> list[Violation]:
    """Check that no two distinct visible stations share a coordinate.

    Two real (non-port, non-hidden) stations placed within *tolerance* of
    the same ``(x, y)`` render their pill markers on top of each other.
    Rail-mode stations are exempt: their markers render as per-rail knobs
    distributed across the rail bundle, so a shared station centre is not a
    visual collision there.
    """
    violations: list[Violation] = []

    placed: list[tuple[str, float, float]] = []
    for sid, station in graph.stations.items():
        if station.is_port or station.is_hidden:
            continue
        if graph.station_is_rail(sid):
            continue
        for other, ox, oy in placed:
            if abs(station.x - ox) <= tolerance and abs(station.y - oy) <= tolerance:
                violations.append(
                    Violation(
                        check="coincident_stations",
                        severity=Severity.ERROR,
                        message=(
                            f"Stations '{other}' and '{sid}' share coordinate "
                            f"({station.x:.1f}, {station.y:.1f})"
                        ),
                        context={"stations": [other, sid]},
                    )
                )
                break
        placed.append((sid, station.x, station.y))

    return violations


def check_minimum_section_spacing(
    graph: MetroGraph, min_gap: float = 5.0
) -> list[Violation]:
    """Check that adjacent sections have a minimum gap between them."""
    violations: list[Violation] = []
    sections = [
        (sid, s) for sid, s in graph.sections.items() if s.bbox_w > 0 and s.bbox_h > 0
    ]

    for i in range(len(sections)):
        sid_a, a = sections[i]
        ax1, ay1 = a.bbox_x, a.bbox_y
        ax2, ay2 = ax1 + a.bbox_w, ay1 + a.bbox_h

        for j in range(i + 1, len(sections)):
            sid_b, b = sections[j]
            bx1, by1 = b.bbox_x, b.bbox_y
            bx2, by2 = bx1 + b.bbox_w, by1 + b.bbox_h

            # Compute gap on each axis
            gap_x = max(bx1 - ax2, ax1 - bx2)
            gap_y = max(by1 - ay2, ay1 - by2)

            # If they're separated on one axis, no adjacency concern
            if gap_x > min_gap or gap_y > min_gap:
                continue

            # They're near each other - check the minimum gap
            actual_gap = max(gap_x, gap_y)
            if actual_gap < min_gap and actual_gap >= -1.0:
                # Close but not deeply overlapping (overlap is caught elsewhere)
                violations.append(
                    Violation(
                        check="minimum_section_spacing",
                        severity=Severity.WARNING,
                        message=(
                            f"Sections '{sid_a}' and '{sid_b}' are only "
                            f"{actual_gap:.1f}px apart (min={min_gap}px)"
                        ),
                        context={
                            "section_a": sid_a,
                            "section_b": sid_b,
                            "gap": actual_gap,
                        },
                    )
                )

    return violations


def check_label_overlap(
    graph: MetroGraph,
    _precomputed: tuple[dict[tuple[str, str], float], list[RoutedPath]] | None = None,
) -> list[Violation]:
    """Check that no station label overlaps another label or a marker.

    Mirrors the runtime guard: label/label overlap is never allowed;
    label/marker grazes within ``LABEL_OVERLAP_TOL`` are tolerated.  Uses the
    same detector the engine and wrapping pass use, so this reports exactly
    what the final render would draw.
    """
    from nf_metro.layout.labels import find_label_overlaps, place_labels

    violations: list[Violation] = []
    if _precomputed is not None:
        offsets, routes = _precomputed
    else:
        precomputed = _compute_routes(graph)
        if precomputed is None:
            return violations
        offsets, routes = precomputed

    try:
        placements = place_labels(
            graph,
            station_offsets=offsets,
            routes=routes,
            label_angle=graph.label_angle or 0.0,
        )
    except Exception as e:
        return [
            Violation(
                check="label_overlap",
                severity=Severity.ERROR,
                message=f"Label placement failed: {e}",
            )
        ]

    for ov in find_label_overlaps(graph, placements, offsets):
        target = "label" if ov.kind == "label" else "marker"
        violations.append(
            Violation(
                check="label_overlap",
                severity=Severity.ERROR,
                message=(
                    f"Label {ov.a!r} overlaps {target} {ov.b!r} by "
                    f"({ov.ox:.1f}, {ov.oy:.1f})px"
                ),
                context={"a": ov.a, "b": ov.b, "kind": ov.kind},
            )
        )
    return violations


def check_edge_waypoints(
    graph: MetroGraph,
    _precomputed: tuple[dict[tuple[str, str], float], list[RoutedPath]] | None = None,
) -> list[Violation]:
    """Check that routed edges have valid waypoints."""
    violations: list[Violation] = []

    if _precomputed is not None:
        offsets, routes = _precomputed
    else:
        try:
            offsets = compute_station_offsets(graph)
            routes = route_edges(graph, station_offsets=offsets)
        except Exception as e:
            violations.append(
                Violation(
                    check="edge_waypoints",
                    severity=Severity.ERROR,
                    message=f"Edge routing failed: {e}",
                )
            )
            return violations

    for route in routes:
        if len(route.points) < 2:
            violations.append(
                Violation(
                    check="edge_waypoints",
                    severity=Severity.ERROR,
                    message=(
                        f"Edge {route.edge.source}->{route.edge.target} "
                        f"(line={route.line_id}) has only {len(route.points)} "
                        f"waypoint(s), need >= 2"
                    ),
                    context={
                        "source": route.edge.source,
                        "target": route.edge.target,
                        "line": route.line_id,
                    },
                )
            )

        for k, (px, py) in enumerate(route.points):
            if px != px or py != py:  # NaN
                violations.append(
                    Violation(
                        check="edge_waypoints",
                        severity=Severity.ERROR,
                        message=(
                            f"Edge {route.edge.source}->{route.edge.target} "
                            f"waypoint {k} has NaN: ({px}, {py})"
                        ),
                        context={
                            "source": route.edge.source,
                            "target": route.edge.target,
                        },
                    )
                )

    return violations


def _segment_crosses_bbox(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    bx: float,
    by: float,
    bw: float,
    bh: float,
    margin: float = 2.0,
) -> bool:
    """Test if a line segment passes through (not just touches) an AABB.

    Uses an inset margin so segments running along a bbox edge don't
    trigger false positives.  Handles axis-aligned segments (common in
    metro routing) as simple range overlaps.
    """
    # Inset the bbox by margin so edge-touching doesn't count
    bx1 = bx + margin
    by1 = by + margin
    bx2 = bx + bw - margin
    by2 = by + bh - margin

    if bx1 >= bx2 or by1 >= by2:
        return False  # Section too small after inset

    # Normalise segment endpoints
    seg_x_min, seg_x_max = min(x1, x2), max(x1, x2)
    seg_y_min, seg_y_max = min(y1, y2), max(y1, y2)

    # Quick reject: no overlap on either axis
    if seg_x_max <= bx1 or seg_x_min >= bx2:
        return False
    if seg_y_max <= by1 or seg_y_min >= by2:
        return False

    # Horizontal segment
    if abs(y1 - y2) < 0.5:
        return by1 < y1 < by2

    # Vertical segment
    if abs(x1 - x2) < 0.5:
        return bx1 < x1 < bx2

    # Diagonal / general segment: use parametric clipping (Liang-Barsky)
    dx = x2 - x1
    dy = y2 - y1
    t_min, t_max = 0.0, 1.0

    for p, q in [
        (-dx, x1 - bx1),
        (dx, bx2 - x1),
        (-dy, y1 - by1),
        (dy, by2 - y1),
    ]:
        if abs(p) < 1e-9:
            if q < 0:
                return False
        else:
            t = q / p
            if p < 0:
                t_min = max(t_min, t)
            else:
                t_max = min(t_max, t)
            if t_min > t_max:
                return False

    return t_min < t_max


def _edge_home_sections(graph: MetroGraph, source_id: str, target_id: str) -> set[str]:
    """Return section IDs that own the source and target of an edge."""
    home: set[str] = set()
    for station_id in (source_id, target_id):
        station = graph.stations.get(station_id)
        if not station:
            continue
        if station.section_id:
            home.add(station.section_id)
        elif station_id in graph.ports:
            home.add(graph.ports[station_id].section_id)
        else:
            # Junction: trace through edges to find connected port sections
            for e in graph.edges:
                other_id = None
                if e.source == station_id:
                    other_id = e.target
                elif e.target == station_id:
                    other_id = e.source
                if other_id:
                    other = graph.stations.get(other_id)
                    if other and other.section_id:
                        home.add(other.section_id)
                    elif other_id in graph.ports:
                        home.add(graph.ports[other_id].section_id)
    return home


def check_edge_section_crossing(
    graph: MetroGraph,
    margin: float = 2.0,
    _precomputed: tuple[dict[tuple[str, str], float], list[RoutedPath]] | None = None,
) -> list[Violation]:
    """Check no routed edge segment passes through a non-home section."""
    violations: list[Violation] = []

    if _precomputed is not None:
        offsets, routes = _precomputed
    else:
        try:
            offsets = compute_station_offsets(graph)
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:
            return violations

    # Pre-collect sections with valid bboxes
    sections = [
        (sid, s) for sid, s in graph.sections.items() if s.bbox_w > 0 and s.bbox_h > 0
    ]

    for route in routes:
        if not route.is_inter_section:
            continue

        home = _edge_home_sections(graph, route.edge.source, route.edge.target)

        for k in range(len(route.points) - 1):
            x1, y1 = route.points[k]
            x2, y2 = route.points[k + 1]

            for sid, sec in sections:
                if sid in home:
                    continue

                if _segment_crosses_bbox(
                    x1,
                    y1,
                    x2,
                    y2,
                    sec.bbox_x,
                    sec.bbox_y,
                    sec.bbox_w,
                    sec.bbox_h,
                    margin=margin,
                ):
                    violations.append(
                        Violation(
                            check="edge_section_crossing",
                            severity=Severity.ERROR,
                            message=(
                                f"Edge {route.edge.source}->{route.edge.target} "
                                f"(line={route.line_id}) segment {k} "
                                f"({x1:.0f},{y1:.0f})->({x2:.0f},{y2:.0f}) "
                                f"crosses section '{sid}' "
                                f"bbox ({sec.bbox_x:.0f},{sec.bbox_y:.0f},"
                                f"{sec.bbox_x + sec.bbox_w:.0f},"
                                f"{sec.bbox_y + sec.bbox_h:.0f})"
                            ),
                            context={
                                "source": route.edge.source,
                                "target": route.edge.target,
                                "line": route.line_id,
                                "segment": k,
                                "crossed_section": sid,
                            },
                        )
                    )

    return violations


def check_bypass_section_clearance(
    graph: MetroGraph,
    min_clearance: float = 5.0,
    _precomputed: tuple[dict[tuple[str, str], float], list[RoutedPath]] | None = None,
) -> list[Violation]:
    """Check that vertical bypass segments maintain clearance from section edges.

    Bypass routes should not run right along a section bbox boundary.
    Vertical segments of inter-section routes must be at least
    *min_clearance* pixels away from the left/right edge of any
    non-home section they pass alongside.
    """
    violations: list[Violation] = []

    if _precomputed is not None:
        offsets, routes = _precomputed
    else:
        try:
            offsets = compute_station_offsets(graph)
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:
            return violations

    sections = [
        (sid, s) for sid, s in graph.sections.items() if s.bbox_w > 0 and s.bbox_h > 0
    ]

    for route in routes:
        if not route.is_inter_section:
            continue

        home = _edge_home_sections(graph, route.edge.source, route.edge.target)

        for k in range(len(route.points) - 1):
            x1, y1 = route.points[k]
            x2, y2 = route.points[k + 1]

            # Only check vertical segments (same X, different Y)
            if abs(x1 - x2) > 0.5:
                continue

            seg_x = (x1 + x2) / 2
            seg_y_min = min(y1, y2)
            seg_y_max = max(y1, y2)

            for sid, sec in sections:
                if sid in home:
                    continue

                # Check Y overlap: segment must span some vertical
                # range that overlaps the section's Y extent
                sec_y_top = sec.bbox_y
                sec_y_bot = sec.bbox_y + sec.bbox_h
                if seg_y_max <= sec_y_top or seg_y_min >= sec_y_bot:
                    continue

                # Check X proximity to left/right bbox edges
                left_dist = abs(seg_x - sec.bbox_x)
                right_dist = abs(seg_x - (sec.bbox_x + sec.bbox_w))

                near_dist = min(left_dist, right_dist)
                if near_dist < min_clearance:
                    violations.append(
                        Violation(
                            check="bypass_section_clearance",
                            severity=Severity.ERROR,
                            message=(
                                f"Edge {route.edge.source}->"
                                f"{route.edge.target} "
                                f"(line={route.line_id}) "
                                f"vertical segment {k} at "
                                f"x={seg_x:.0f} is only "
                                f"{near_dist:.1f}px from "
                                f"section '{sid}' edge "
                                f"(min {min_clearance}px)"
                            ),
                            context={
                                "source": route.edge.source,
                                "target": route.edge.target,
                                "line": route.line_id,
                                "segment": k,
                                "near_section": sid,
                                "clearance": near_dist,
                            },
                        )
                    )

    return violations


def check_single_layer_centering(
    graph: MetroGraph, tolerance: float = 5.0
) -> list[Violation]:
    """Check that single-layer sections have stations centered in bbox.

    For LR/RL sections where all internal stations share the same layer
    (single column of stations), the station X should be approximately
    at the horizontal center of the section bounding box.  A drift
    indicates asymmetric bbox expansion without recentering.
    """
    violations: list[Violation] = []
    junction_ids = set(graph.junctions)

    for sec_id, section in graph.sections.items():
        if section.bbox_w == 0 or section.direction not in ("LR", "RL"):
            continue

        # Collect internal (non-port, non-junction) stations
        internals = [
            st
            for sid, st in graph.stations.items()
            if st.section_id == sec_id and not st.is_port and sid not in junction_ids
        ]
        if not internals:
            continue

        # Only check single-layer sections (all stations at same X)
        xs = [st.x for st in internals]
        if max(xs) - min(xs) > tolerance:
            continue

        station_x = sum(xs) / len(xs)
        bbox_center = section.bbox_x + section.bbox_w / 2
        offset = abs(station_x - bbox_center)

        if offset > tolerance:
            violations.append(
                Violation(
                    check="single_layer_centering",
                    severity=Severity.WARNING,
                    message=(
                        f"Section {sec_id!r}: single-layer stations at "
                        f"x={station_x:.1f} are {offset:.1f}px from "
                        f"bbox center ({bbox_center:.1f})"
                    ),
                    context={
                        "section": sec_id,
                        "station_x": station_x,
                        "bbox_center": bbox_center,
                        "offset": offset,
                    },
                )
            )

    return violations


def check_station_as_elbow(
    graph: MetroGraph, tolerance: float = 10.0
) -> list[Violation]:
    """Check that perpendicular ports don't align with internal stations.

    Only flags ports that are perpendicular to the section's flow direction,
    where the line must bend and could pass through a station:
    - LEFT/RIGHT ports on TB sections (horizontal entry into vertical flow)
    - TOP/BOTTOM ports on LR/RL sections (vertical entry into horizontal flow)

    Ports along the flow direction (e.g. LEFT entry on LR section) naturally
    share coordinates with stations on the main track and are not checked.

    The default tolerance of 10px accounts for station marker diameter
    (station_radius is typically 5-6px).
    """
    violations: list[Violation] = []

    # Group internal (non-port) stations by section
    section_stations: dict[str, list[tuple[str, float, float]]] = {}
    for sid, station in graph.stations.items():
        if station.is_port or station.section_id is None:
            continue
        sec_id = station.section_id
        if sec_id not in section_stations:
            section_stations[sec_id] = []
        section_stations[sec_id].append((sid, station.x, station.y))

    for pid, port in graph.ports.items():
        port_station = graph.stations.get(pid)
        if not port_station:
            continue

        section = graph.sections.get(port.section_id)
        if not section:
            continue
        direction = section.direction

        internals = section_stations.get(port.section_id, [])
        if not internals:
            continue

        # Only check perpendicular ports (where a bend is required)
        is_perpendicular = False
        if direction == "TB" and port.side in (PortSide.LEFT, PortSide.RIGHT):
            is_perpendicular = True
        elif direction in ("LR", "RL") and port.side in (
            PortSide.TOP,
            PortSide.BOTTOM,
        ):
            is_perpendicular = True

        if not is_perpendicular:
            continue

        # LEFT/RIGHT ports: line runs horizontally at port.y, check Y
        # TOP/BOTTOM ports: line runs vertically at port.x, check X
        if port.side in (PortSide.LEFT, PortSide.RIGHT):
            for st_id, st_x, st_y in internals:
                if abs(port_station.y - st_y) <= tolerance:
                    violations.append(
                        Violation(
                            check="station_as_elbow",
                            severity=Severity.ERROR,
                            message=(
                                f"Port '{pid}' ({port.side.value}, "
                                f"y={port_station.y:.1f}) aligns with "
                                f"station '{st_id}' (y={st_y:.1f}) in "
                                f"section '{port.section_id}' - line "
                                f"would route through the station"
                            ),
                            context={
                                "port": pid,
                                "station": st_id,
                                "section": port.section_id,
                                "axis": "y",
                                "port_coord": port_station.y,
                                "station_coord": st_y,
                            },
                        )
                    )
        else:  # TOP / BOTTOM
            for st_id, st_x, st_y in internals:
                if abs(port_station.x - st_x) <= tolerance:
                    violations.append(
                        Violation(
                            check="station_as_elbow",
                            severity=Severity.ERROR,
                            message=(
                                f"Port '{pid}' ({port.side.value}, "
                                f"x={port_station.x:.1f}) aligns with "
                                f"station '{st_id}' (x={st_x:.1f}) in "
                                f"section '{port.section_id}' - line "
                                f"would route through the station"
                            ),
                            context={
                                "port": pid,
                                "station": st_id,
                                "section": port.section_id,
                                "axis": "x",
                                "port_coord": port_station.x,
                                "station_coord": st_x,
                            },
                        )
                    )

    return violations


def check_almost_horizontal_edges(
    graph: MetroGraph,
    slope_threshold: float = 0.1,
    min_dx: float = 10.0,
    _precomputed: tuple[dict[tuple[str, str], float], list[RoutedPath]] | None = None,
) -> list[Violation]:
    """Check for almost-horizontal edge segments after offset application.

    Checks both intra-section and inter-section edges. Intra-section
    slopes indicate offset mismatches that should be flat. Inter-section
    slopes (outside the L-shaped vertical segments) indicate offset
    reordering regressions.

    Flags segments where abs(dy) > 0.5 AND abs(dx) >= abs(dy) / slope_threshold,
    i.e. a shallow slope that should be perfectly flat.
    """
    violations: list[Violation] = []

    if _precomputed is not None:
        offsets, routes = _precomputed
    else:
        try:
            offsets = compute_station_offsets(graph)
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:
            return violations

    for route in routes:
        pts = apply_route_offsets(route, offsets)
        for k in range(len(pts) - 1):
            x1, y1 = pts[k]
            x2, y2 = pts[k + 1]
            dx = abs(x2 - x1)
            dy = abs(y2 - y1)
            if dy > 0.5 and dx >= min_dx and dx >= dy / slope_threshold:
                violations.append(
                    Violation(
                        check="almost_horizontal_edge",
                        severity=Severity.WARNING,
                        message=(
                            f"Edge {route.edge.source}->{route.edge.target} "
                            f"(line={route.line_id}) segment {k} is almost "
                            f"horizontal: dx={dx:.1f}, dy={dy:.1f} "
                            f"(slope={dy / dx:.4f})"
                        ),
                        context={
                            "source": route.edge.source,
                            "target": route.edge.target,
                            "line": route.line_id,
                            "segment": k,
                            "dx": dx,
                            "dy": dy,
                        },
                    )
                )

    return violations


def check_excessive_column_gaps(
    graph: MetroGraph,
    max_unit_gap: float = 1.5,
) -> list[Violation]:
    """Flag consecutive same-column stations separated by an empty row.

    Within a section, two interior stations sharing an X coordinate (i.e.
    the same column / layer) should sit on adjacent grid rows when nothing
    else occupies the rows between them. A vertical gap larger than
    `max_unit_gap` x `Y_SPACING` indicates wasted vertical space - e.g.
    variant_calling's GATK HaplotypeCaller and DeepVariant placed two
    grid units apart with an empty row between, when they could be
    adjacent.

    Multi-line hubs and ports are excluded; only non-port stations sharing
    a column within the same section are compared.
    """
    violations: list[Violation] = []
    threshold = Y_SPACING * max_unit_gap

    from collections import defaultdict

    # Group stations by (section_id, rounded x).
    columns: dict[tuple[str, int], list] = defaultdict(list)
    for st in graph.stations.values():
        if st.is_port or st.section_id is None:
            continue
        columns[(st.section_id, round(st.x))].append(st)

    for (sec_id, col_x), col_stations in columns.items():
        if len(col_stations) < 2:
            continue
        col_stations.sort(key=lambda s: s.y)
        for i in range(len(col_stations) - 1):
            a = col_stations[i]
            b = col_stations[i + 1]
            gap = b.y - a.y
            if gap > threshold:
                violations.append(
                    Violation(
                        check="excessive_column_gap",
                        severity=Severity.WARNING,
                        message=(
                            f"Stations '{a.id}' (y={a.y:.0f}) and '{b.id}' "
                            f"(y={b.y:.0f}) share column x={col_x} in "
                            f"section '{sec_id}' but are {gap:.0f}px apart "
                            f"({gap / Y_SPACING:.1f} grid units); empty "
                            f"row(s) between them waste vertical space"
                        ),
                        context={
                            "station_a": a.id,
                            "station_b": b.id,
                            "section": sec_id,
                            "column_x": col_x,
                            "gap": gap,
                            "gap_units": gap / Y_SPACING,
                        },
                    )
                )

    return violations


def _segments_cross(
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    p4: tuple[float, float],
    eps: float = 0.5,
) -> tuple[float, float] | None:
    """Return intersection point if segments p1-p2 and p3-p4 cross properly.

    A "proper" crossing means the segments intersect at a point that is
    strictly interior to both - i.e. the intersection is not at any
    segment endpoint (within tolerance eps). Returns None for parallel,
    collinear, non-intersecting, or endpoint-touching cases.
    """
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    x4, y4 = p4
    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denom) < 1e-9:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
    u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / denom
    if not (
        eps / max(abs(x1 - x2) + abs(y1 - y2), 1.0)
        < t
        < 1 - eps / max(abs(x1 - x2) + abs(y1 - y2), 1.0)
    ):
        return None
    if not (
        eps / max(abs(x3 - x4) + abs(y3 - y4), 1.0)
        < u
        < 1 - eps / max(abs(x3 - x4) + abs(y3 - y4), 1.0)
    ):
        return None
    return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))


def check_route_segment_crossings(
    graph: MetroGraph,
    _precomputed: tuple[dict[tuple[str, str], float], list[RoutedPath]] | None = None,
) -> list[Violation]:
    """Flag pairs of routed edges whose paths visually cross.

    Two metro lines crossing at any point that is not a junction or shared
    station indicates an avoidable visual ambiguity. The variant_calling
    Main / QC Reporting case is the canonical example: both lines exit
    Section 1's right edge, but Main descends through QC Reporting's track
    on the way to Section 2 - they swap order and cross.

    Implementation: pairwise segment intersection on post-offset routed
    paths. Intersections at station coordinates (within Y_SPACING / 4)
    are treated as legitimate hub crossings and excluded.
    """
    violations: list[Violation] = []

    if _precomputed is not None:
        offsets, routes = _precomputed
    else:
        try:
            offsets = compute_station_offsets(graph)
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:
            return violations

    # Pre-compute station coordinates for hub-crossing exclusion.
    station_xy = [(st.x, st.y) for st in graph.stations.values() if not st.is_port]
    hub_tol = Y_SPACING / 4.0

    # Precompute post-offset point lists once per route.
    paths = [(r, apply_route_offsets(r, offsets)) for r in routes]

    seen: set[tuple] = set()
    for i in range(len(paths)):
        ra, pa = paths[i]
        for j in range(i + 1, len(paths)):
            rb, pb = paths[j]
            pt = _route_pair_crossing(graph, ra, pa, rb, pb, station_xy, hub_tol)
            if pt is None:
                continue
            key = tuple(
                sorted(
                    [
                        (ra.edge.source, ra.edge.target, ra.line_id),
                        (rb.edge.source, rb.edge.target, rb.line_id),
                    ]
                )
            )
            if key in seen:
                continue
            seen.add(key)
            violations.append(
                Violation(
                    check="route_segment_crossing",
                    severity=Severity.WARNING,
                    message=(
                        f"Lines '{ra.line_id}' "
                        f"({ra.edge.source}->{ra.edge.target}) and "
                        f"'{rb.line_id}' "
                        f"({rb.edge.source}->{rb.edge.target}) "
                        f"cross at ({pt[0]:.0f},{pt[1]:.0f}) - not "
                        f"at a station, so the crossing is avoidable"
                    ),
                    context={
                        "line_a": ra.line_id,
                        "line_b": rb.line_id,
                        "edge_a": (ra.edge.source, ra.edge.target),
                        "edge_b": (rb.edge.source, rb.edge.target),
                        "intersection": pt,
                    },
                )
            )

    return violations


def _pt_near(pt, points, tol) -> bool:
    """True if *pt* is within ``tol`` of any ``(x, y)`` in *points* on both axes."""
    for px, py in points:
        if abs(px - pt[0]) <= tol and abs(py - pt[1]) <= tol:
            return True
    return False


def _edges_share_real_hub(graph, ra_edge, rb_edge) -> bool:
    """True if the edges share an endpoint at a multi-line real-station hub.

    Such a shared endpoint means the two edges fork or join at a station that
    genuinely carries multiple lines, so crossings near it are natural
    divergence.  Junction endpoints (synthetic, is_port=True) do NOT count.
    """
    shared = {ra_edge.source, ra_edge.target} & {rb_edge.source, rb_edge.target}
    for sid in shared:
        st = graph.stations.get(sid)
        if st is not None and not st.is_port and len(graph.station_lines(sid)) > 1:
            return True
    return False


def _edge_involves_hidden(graph, edge) -> bool:
    """True if either endpoint of *edge* is a hidden helper station."""
    for sid in (edge.source, edge.target):
        st = graph.stations.get(sid)
        if st is not None and st.is_hidden:
            return True
    return False


def _shared_port_endpoints(graph, ra_edge, rb_edge) -> list[tuple[float, float]]:
    """Coordinates of port stations shared as endpoints by both edges."""
    shared = {ra_edge.source, ra_edge.target} & {rb_edge.source, rb_edge.target}
    result: list[tuple[float, float]] = []
    for sid in shared:
        st = graph.stations.get(sid)
        if st is not None and st.is_port:
            result.append((st.x, st.y))
    return result


def _seg_near_vertical(p0, p1, vertical_dx_tol) -> bool:
    """True if the segment is near-vertical (small dx, non-zero dy)."""
    return abs(p1[0] - p0[0]) <= vertical_dx_tol and abs(p1[1] - p0[1]) > 0


def _route_pair_crossing(graph, ra, pa, rb, pb, station_xy, hub_tol):
    """First avoidable crossing point between two routed paths, or None.

    A crossing near a shared port endpoint can be a benign fan-out/fan-in
    divergence artefact (lines split or merge at the port and may intersect
    within a few pixels of it).  Such crossings are excluded ONLY when both
    crossing segments transition via a diagonal; if either is near-vertical,
    one line is plunging past its source Y to detour, which is the awkward
    case worth flagging.
    """
    if ra.line_id == rb.line_id:
        return None
    if _edges_share_real_hub(graph, ra.edge, rb.edge):
        return None
    # Hidden helper stations are inserted by the layout to extend terminus or
    # branch geometry; their crossings are intentional.
    if _edge_involves_hidden(graph, ra.edge) or _edge_involves_hidden(graph, rb.edge):
        return None
    shared_ports = _shared_port_endpoints(graph, ra.edge, rb.edge)
    fan_tol = Y_SPACING
    vertical_dx_tol = 2.0
    for ai in range(len(pa) - 1):
        for bi in range(len(pb) - 1):
            pt = _segments_cross(pa[ai], pa[ai + 1], pb[bi], pb[bi + 1])
            if pt is None:
                continue
            if _pt_near(pt, station_xy, hub_tol):
                continue
            if shared_ports and _pt_near(pt, shared_ports, fan_tol):
                seg_a = (pa[ai], pa[ai + 1])
                seg_b = (pb[bi], pb[bi + 1])
                if not (
                    _seg_near_vertical(*seg_a, vertical_dx_tol)
                    or _seg_near_vertical(*seg_b, vertical_dx_tol)
                ):
                    continue
            return pt
    return None


def check_inter_section_line_crossings(
    graph: MetroGraph, tolerance: float = 2.0
) -> list[Violation]:
    """Flag pairs of lines that cross on the curve between two sections.

    Lines leaving the same source section bound for the same target
    section should preserve their top-to-bottom order: the line that
    exits higher should also enter higher. When two lines swap order
    between exit and entry, their connecting curves cross each other
    visually for no structural reason - one or both lines were placed
    sub-optimally on a side of the gap.

    Compares pairs of inter-section edges that share (source_section,
    target_section). For each pair (a, b), if `a` exits above `b` but
    enters below `b` (or vice versa), the two curves cross and a
    violation is emitted.

    The check works on edges before port resolution would mask the
    issue: it uses the source station's Y for "exit" and the target
    station's Y for "entry", ignoring intermediate ports / junctions.
    """
    violations: list[Violation] = []

    # Group inter-section edges by (source_section, target_section).
    groups: dict[tuple[str, str], list] = {}
    for edge in graph.edges:
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if src is None or tgt is None:
            continue
        if src.is_port or tgt.is_port:
            continue
        if src.section_id is None or tgt.section_id is None:
            continue
        if src.section_id == tgt.section_id:
            continue
        groups.setdefault((src.section_id, tgt.section_id), []).append((edge, src, tgt))

    seen: set[tuple[str, str, str, str]] = set()
    for (src_sec, tgt_sec), edges in groups.items():
        if len(edges) < 2:
            continue
        for i in range(len(edges)):
            for j in range(i + 1, len(edges)):
                a_edge, a_src, a_tgt = edges[i]
                b_edge, b_src, b_tgt = edges[j]
                if a_edge.line_id == b_edge.line_id:
                    continue
                d_src = a_src.y - b_src.y
                d_tgt = a_tgt.y - b_tgt.y
                if abs(d_src) <= tolerance or abs(d_tgt) <= tolerance:
                    continue
                if (d_src > 0) == (d_tgt > 0):
                    continue
                key = (a_edge.line_id, b_edge.line_id, src_sec, tgt_sec)
                if key in seen or key[:2][::-1] + key[2:] in seen:
                    continue
                seen.add(key)
                violations.append(
                    Violation(
                        check="inter_section_line_crossing",
                        severity=Severity.WARNING,
                        message=(
                            f"Lines '{a_edge.line_id}' and '{b_edge.line_id}' "
                            f"swap top-bottom order between sections "
                            f"'{src_sec}' and '{tgt_sec}': "
                            f"{a_edge.line_id} exits at y={a_src.y:.0f} "
                            f"(vs {b_src.y:.0f}) but enters at y={a_tgt.y:.0f} "
                            f"(vs {b_tgt.y:.0f}); curves cross unnecessarily"
                        ),
                        context={
                            "line_a": a_edge.line_id,
                            "line_b": b_edge.line_id,
                            "src_section": src_sec,
                            "tgt_section": tgt_sec,
                            "a_src_y": a_src.y,
                            "a_tgt_y": a_tgt.y,
                            "b_src_y": b_src.y,
                            "b_tgt_y": b_tgt.y,
                        },
                    )
                )

    return violations


def check_single_segment_diagonals(
    graph: MetroGraph,
    min_dx: float = 5.0,
    min_dy: float = 5.0,
    _precomputed: tuple[dict[tuple[str, str], float], list[RoutedPath]] | None = None,
) -> list[Violation]:
    """Flag routed edges that are a single straight diagonal segment.

    A 1:1 chain edge whose post-offset path consists of a single point pair
    with non-trivial dx and dy renders as one straight slope between the
    source and target pills. The line should instead step between tracks
    via an L-shape or S-curve: a horizontal exit segment, a 45 degree
    corner transition, then a horizontal entry segment.

    Multi-segment paths are exempt because their endpoint segments may
    legitimately be 45 degree corners as part of an L-shape, with the
    surrounding axis-aligned segments providing the visual cleanness.
    Sub-threshold deltas (default 5px) are ignored to filter out
    sub-pixel offset jitter that the renderer rounds away.

    This complements check_almost_horizontal_edges, which targets shallow
    slopes that *should* be flat; this check targets visibly diagonal
    single segments that should be broken into S-shapes.
    """
    violations: list[Violation] = []

    if _precomputed is not None:
        offsets, routes = _precomputed
    else:
        try:
            offsets = compute_station_offsets(graph)
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:
            return violations

    for route in routes:
        pts = apply_route_offsets(route, offsets)
        if len(pts) != 2:
            continue
        x1, y1 = pts[0]
        x2, y2 = pts[1]
        dx = abs(x2 - x1)
        dy = abs(y2 - y1)
        if dx > min_dx and dy > min_dy:
            violations.append(
                Violation(
                    check="single_segment_diagonal",
                    severity=Severity.WARNING,
                    message=(
                        f"Edge {route.edge.source}->{route.edge.target} "
                        f"(line={route.line_id}) renders as a single straight "
                        f"diagonal: ({x1:.0f},{y1:.0f})->({x2:.0f},{y2:.0f}), "
                        f"dx={dx:.0f}, dy={dy:.0f}. Should be an L-shape or "
                        f"S-curve with axis-aligned endpoint segments"
                    ),
                    context={
                        "source": route.edge.source,
                        "target": route.edge.target,
                        "line": route.line_id,
                        "dx": dx,
                        "dy": dy,
                    },
                )
            )

    return violations


def check_intra_section_chain_alignment(
    graph: MetroGraph, tolerance: float = 2.0
) -> list[Violation]:
    """Check that consecutive same-line stations within a section share a track.

    Within an LR section, two non-port stations connected by an edge on the
    same metro line should sit at the same Y (the line's track), so the
    intra-section edge runs horizontally. Within a TB section the same
    invariant applies on the X axis.

    Diagonal intra-section edges typically arise when one endpoint is a
    multi-line hub centred across many tracks while the other sits on a
    single track. Routing produces an L-shape with corners that often
    looks fine, so this is a WARNING rather than an ERROR - it surfaces
    candidates for layout improvement (e.g. funcprofiler MERGE_RUNS
    pulling above the concat track) without blocking renders that absorb
    the offset cleanly.

    Multi-line hubs (stations carrying more than one metro line, e.g.
    variantbenchmarking's Liftover where test and truth converge) sit at
    a centroid Y dictated by the multiple lines they carry, so a 1:1
    chain edge into such a hub legitimately has a Y mismatch.
    Cross-line fan-in / fan-out hubs (umi_dedup-style convergence
    points) likewise sit at centroid Y and are excluded by the distinct-
    neighbour count.

    Terminus stations (file icons for inputs / outputs) are skipped
    because they conventionally sit at section edges, not aligned with
    interior tracks; routing absorbs the icon-to-station Y delta as a
    smooth S-curve.

    The variant_calling bwa_index -> bwa_mem case is the canonical
    target: both stations carry only the Main line in a 4-station
    zigzag where the layout could simply place them on the same row.
    The flagged edge has no structural excuse for the Y delta.

    Inter-section edges, port endpoints, and the perpendicular axis are
    not checked here (other validators cover those cases).
    """
    violations: list[Violation] = []

    # Per-station distinct *non-port* neighbour sets across all lines.
    # Ports are routing artefacts that shouldn't excuse a structural
    # misalignment between two real stations.
    out_neighbours: dict[str, set[str]] = {}
    in_neighbours: dict[str, set[str]] = {}
    # Targets fed by an entry/exit port: a station that merges an internal
    # branch with the line's external entry is a genuine fan-in hub, so the
    # internal edge into it is legitimately diagonal and must not be flagged.
    port_fed: set[str] = set()
    for e in graph.edges:
        src_st = graph.stations.get(e.source)
        tgt_st = graph.stations.get(e.target)
        if tgt_st is not None and not tgt_st.is_port:
            out_neighbours.setdefault(e.source, set()).add(e.target)
        if src_st is not None and not src_st.is_port:
            in_neighbours.setdefault(e.target, set()).add(e.source)
        if src_st is not None and src_st.is_port:
            port_fed.add(e.target)

    for edge in graph.edges:
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if src is None or tgt is None:
            continue
        if src.is_port or tgt.is_port:
            continue
        if src.is_hidden or tgt.is_hidden:
            # A hidden bypass V dips off the trunk to route a line around a
            # station marker, so an edge into or out of it is diagonal by
            # design, not a chain misalignment.
            continue
        if src.is_terminus or tgt.is_terminus:
            continue
        if src.section_id is None or src.section_id != tgt.section_id:
            continue
        section = graph.sections.get(src.section_id)
        if section is None:
            continue

        # Skip multi-line hubs: the endpoint's Y is dictated by the
        # other lines passing through it, not by this edge.
        if len(graph.station_lines(edge.source)) > 1:
            continue
        if len(graph.station_lines(edge.target)) > 1:
            continue

        # Skip cross-line fan-in / fan-out hubs: same logic at the
        # neighbour level (multiple sources / targets across all lines).
        if len(out_neighbours.get(edge.source, ())) > 1:
            continue
        if len(in_neighbours.get(edge.target, ())) > 1:
            continue

        # Skip entry-port fan-in hubs: the target merges this internal
        # branch with the line's external entry, so the branch edge is a
        # legitimate diagonal merge, not a chain misalignment.
        if edge.target in port_fed:
            continue

        # Skip pre-terminus targets: a station whose only follow-on is
        # a terminus (file icon) is at the tail of the chain and
        # conventionally sits at the section's output edge.
        tgt_outs = out_neighbours.get(edge.target, set())
        if len(tgt_outs) == 1:
            next_st = graph.stations.get(next(iter(tgt_outs)))
            if next_st is not None and next_st.is_terminus:
                continue

        if section.direction in ("LR", "RL"):
            delta = abs(src.y - tgt.y)
            axis = "y"
            src_coord, tgt_coord = src.y, tgt.y
        elif section.direction == "TB":
            delta = abs(src.x - tgt.x)
            axis = "x"
            src_coord, tgt_coord = src.x, tgt.x
        else:
            continue

        if delta > tolerance:
            violations.append(
                Violation(
                    check="intra_section_chain_alignment",
                    severity=Severity.WARNING,
                    message=(
                        f"Edge {edge.source}->{edge.target} "
                        f"(line={edge.line_id}) within section "
                        f"'{src.section_id}' ({section.direction}) is "
                        f"diagonal: |d{axis}|={delta:.1f}px "
                        f"(src={src_coord:.1f}, tgt={tgt_coord:.1f}); "
                        f"both endpoints share a line and a section so the "
                        f"edge should be axis-aligned"
                    ),
                    context={
                        "source": edge.source,
                        "target": edge.target,
                        "line": edge.line_id,
                        "section": src.section_id,
                        "direction": section.direction,
                        "axis": axis,
                        "delta": delta,
                    },
                )
            )

    return violations


def check_exit_port_feeder_alignment(
    graph: MetroGraph, tolerance: float = 2.0
) -> list[Violation]:
    """Check that section exit ports align with their internal feeder station.

    For each exit port P, every internal (non-port, non-junction) station
    inside the same section that has an edge directly into P should share
    P's perpendicular coordinate (Y for LR/RL sections, X for TB). This
    keeps the connecting segment axis-aligned and avoids kinks in the
    horizontal exit run.

    Multi-feeder fan-ins inherently misalign all but one feeder, so this
    check fires only when the port matches *none* of its feeders - i.e.
    the engine missed a free alignment, not when geometry forced a fan-in
    L-shape. When it fires, every misaligned feeder is reported so the
    failure mode is fully visible.

    Junction stations (auto-inserted for fan-outs/fan-ins) are excluded
    because they are routing artefacts that adopt the port's coordinate
    by construction.
    """
    violations: list[Violation] = []
    junction_ids = set(graph.junctions)

    # Build feeder mapping: port_id -> [(station_id, station)]
    feeders: dict[str, list[tuple[str, object]]] = {}
    for edge in graph.edges:
        if edge.target not in graph.ports:
            continue
        src = graph.stations.get(edge.source)
        if src is None or src.is_port or edge.source in junction_ids:
            continue
        port = graph.ports[edge.target]
        if port.is_entry:
            continue
        if src.section_id != port.section_id:
            continue
        feeders.setdefault(edge.target, []).append((edge.source, src))

    for port_id, feeder_list in feeders.items():
        port = graph.ports[port_id]
        port_station = graph.stations.get(port_id)
        if port_station is None:
            continue
        section = graph.sections.get(port.section_id)
        if section is None:
            continue

        if section.direction in ("LR", "RL"):
            axis = "y"
            port_coord = port_station.y
        elif section.direction == "TB":
            axis = "x"
            port_coord = port_station.x
        else:
            continue

        # Compute deltas; only emit if no feeder aligns within tolerance.
        deltas: list[tuple[str, object, float]] = []
        any_aligned = False
        for st_id, st in feeder_list:
            st_coord = st.y if axis == "y" else st.x
            delta = abs(port_coord - st_coord)
            deltas.append((st_id, st, delta))
            if delta <= tolerance:
                any_aligned = True

        if any_aligned:
            continue

        for st_id, st, delta in deltas:
            st_coord = st.y if axis == "y" else st.x
            violations.append(
                Violation(
                    check="exit_port_feeder_alignment",
                    severity=Severity.WARNING,
                    message=(
                        f"Exit port '{port_id}' ({port.side.value}, "
                        f"{axis}={port_coord:.1f}) is misaligned with "
                        f"feeder station '{st_id}' ({axis}={st_coord:.1f}) "
                        f"in section '{port.section_id}': "
                        f"|d{axis}|={delta:.1f}px - port aligns with no "
                        f"feeder, every exit segment will kink"
                    ),
                    context={
                        "port": port_id,
                        "station": st_id,
                        "section": port.section_id,
                        "axis": axis,
                        "delta": delta,
                        "port_coord": port_coord,
                        "station_coord": st_coord,
                    },
                )
            )

    return violations


def check_serpentine_no_backtrack(
    graph: MetroGraph,
    backtrack_frac: float = 0.5,
    _precomputed: tuple[dict[tuple[str, str], float], list[RoutedPath]] | None = None,
) -> list[Violation]:
    """Stacked same-direction sections must not backtrack horizontally.

    When same-direction sections are stacked in one grid column and chained
    (issue #421), the engine serpentines their effective flow so consecutive
    sections meet on a shared side joined by a short vertical drop.  A section
    that fails to serpentine instead enters on the wrong side and folds its
    internal route back across (nearly) the full section width.

    For every section in a detected serpentine run, this sums the horizontal
    travel of its internal routed segments that runs *against* the section's
    flow direction.  If that wrong-way travel exceeds ``backtrack_frac`` of the
    section width, the section is kinking the chain.
    """
    from nf_metro.layout.auto_layout import detect_serpentine_runs

    violations: list[Violation] = []

    if _precomputed is not None:
        offsets, routes = _precomputed
    else:
        try:
            offsets = compute_station_offsets(graph)
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:
            return violations

    dag = graph.section_dag
    if dag is None:
        return violations
    runs = detect_serpentine_runs(graph, dag.successors, dag.predecessors)
    serpentine_sections = {sid for run in runs for sid in run}
    if not serpentine_sections:
        return violations

    # Internal route segments grouped by home section.
    wrong_way: dict[str, float] = {sid: 0.0 for sid in serpentine_sections}
    for route in routes:
        src_sec = graph.section_for_station(route.edge.source)
        tgt_sec = graph.section_for_station(route.edge.target)
        if src_sec != tgt_sec or src_sec not in serpentine_sections:
            continue
        port = graph.ports.get(route.edge.source)
        if port and port.is_entry and port.side in (PortSide.LEFT, PortSide.RIGHT):
            # A LEFT/RIGHT entry port's turn-in to the trunk is the entry, one
            # leg perpendicular to flow, not a serpentine fold-back.
            continue
        section = graph.sections[src_sec]
        forward = 1.0 if section.direction != "RL" else -1.0
        pts = apply_route_offsets(route, offsets)
        for k in range(len(pts) - 1):
            dx = pts[k + 1][0] - pts[k][0]
            if dx * forward < 0:
                wrong_way[src_sec] += abs(dx)

    for sid, against in wrong_way.items():
        section = graph.sections[sid]
        limit = backtrack_frac * max(section.bbox_w, 1.0)
        if against > limit:
            violations.append(
                Violation(
                    check="serpentine_no_backtrack",
                    severity=Severity.ERROR,
                    message=(
                        f"Stacked section '{sid}' (dir={section.direction}) "
                        f"backtracks {against:.0f}px against its flow "
                        f"(>{limit:.0f}px = {backtrack_frac:.0%} of width "
                        f"{section.bbox_w:.0f}); the serpentine chain is "
                        f"kinking instead of dropping vertically"
                    ),
                    context={
                        "section": sid,
                        "direction": section.direction,
                        "against": against,
                        "limit": limit,
                    },
                )
            )

    return violations
