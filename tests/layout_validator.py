"""Layout validator: programmatic checks for layout defects.

Runs a suite of checks against a laid-out MetroGraph and returns
a list of Violation objects describing any problems found.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.parser.model import MetroGraph, PortSide


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass
class Violation:
    check: str
    severity: Severity
    message: str
    context: dict = field(default_factory=dict)


def validate_layout(graph: MetroGraph) -> list[Violation]:
    """Run all layout checks and return violations."""
    violations: list[Violation] = []
    violations.extend(check_section_overlap(graph))
    violations.extend(check_station_containment(graph))
    violations.extend(check_port_boundary(graph))
    violations.extend(check_coordinate_sanity(graph))
    violations.extend(check_minimum_section_spacing(graph))
    violations.extend(check_edge_waypoints(graph))
    violations.extend(check_edge_section_crossing(graph))
    violations.extend(check_station_as_elbow(graph))
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


def check_edge_waypoints(graph: MetroGraph) -> list[Violation]:
    """Check that routed edges have valid waypoints."""
    violations: list[Violation] = []

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
    graph: MetroGraph, margin: float = 2.0
) -> list[Violation]:
    """Check no routed edge segment passes through a non-home section."""
    violations: list[Violation] = []

    try:
        offsets = compute_station_offsets(graph)
        routes = route_edges(graph, station_offsets=offsets)
    except Exception:
        return violations  # Edge routing failures are caught by check_edge_waypoints

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
