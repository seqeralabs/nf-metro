"""Layout phase: _common (extracted from engine.py, see #451)."""

from __future__ import annotations

from collections import defaultdict

from nf_metro.layout.constants import (
    COORD_TOLERANCE,
    SECTION_Y_PADDING,
    STATION_RADIUS_APPROX,
)
from nf_metro.parser.model import Edge, MetroGraph, PortSide, Section, Station


def _bbox_cols_overlap(a: Section, b: Section) -> bool:
    """True when two sections' bboxes overlap in X (share horizontal extent)."""
    return a.bbox_x < b.bbox_x + b.bbox_w and b.bbox_x < a.bbox_x + a.bbox_w


def _station_marker_bbox(
    graph: MetroGraph,
    sid: str,
    offsets: dict | None = None,
    radius: float = STATION_RADIUS_APPROX,
) -> tuple[float, float, float, float] | None:
    """Rendered marker / icon bbox for ``sid``, or ``None`` for ports,
    hidden stations, and junctions.

    Mirrors the pill geometry used by ``nf_metro.render.svg``: width
    ``2 * radius``, height ``(max_off - min_off) + 2 * radius``, centred
    at ``(station.x, station.y + (min_off + max_off) / 2)``.
    """
    from nf_metro.layout.routing import compute_station_offsets

    st = graph.stations.get(sid)
    if st is None or st.is_port or st.is_hidden or sid in graph.junctions:
        return None
    if offsets is None:
        offsets = compute_station_offsets(graph)
    line_offs = [offsets.get((sid, lid), 0.0) for lid in graph.station_lines(sid)] or [
        0.0
    ]
    min_off, max_off = min(line_offs), max(line_offs)
    cy = st.y + (min_off + max_off) / 2
    half_h = (max_off - min_off) / 2 + radius
    return (st.x - radius, cy - half_h, st.x + radius, cy + half_h)


def first_vertical_leg_x(points) -> float | None:
    """X of the first (near-)vertical leg of *points*.

    The source-side vertical channel ("V1") of an inter-section route;
    ``None`` when no vertical leg exists.
    """
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if abs(x1 - x0) < COORD_TOLERANCE and abs(y1 - y0) > COORD_TOLERANCE:
            return x1
    return None


def _canvas_width(graph: MetroGraph) -> float:
    """Horizontal extent of all positioned sections (rightmost - leftmost)."""
    rights = [s.bbox_x + s.bbox_w for s in graph.sections.values() if s.bbox_w > 0]
    lefts = [s.bbox_x for s in graph.sections.values() if s.bbox_w > 0]
    if not rights or not lefts:
        return 0.0
    return max(rights) - min(lefts)


def _route_crosses_section_boundary(
    graph: MetroGraph,
    routes: list,
    *,
    port_tol: float = 24.0,
    inset: float = 4.0,
    axis_tol: float = 1.0,
) -> tuple | None:
    """Return the first ``(route, section_id, x, y)`` where an axis-aligned
    routed segment crosses a section bbox edge away from any declared port,
    else None.

    A segment p1->p2 "crosses" a section edge when it passes strictly
    through one of the four bbox sides within the perpendicular extent of
    that side.  Crossings within *port_tol* of one of the section's ports
    are permitted (that is how a line legitimately enters/leaves).  Any
    other crossing means a horizontal or vertical run is cutting through a
    section box where no port invites it -- the symptom this guard forbids
    (e.g. an entry inferred on the wrong side so the connector slices the
    box,).

    Two classes are intentionally out of scope:

    * **Diagonal transition segments** (45-degree corner curves) clip box
      corners while legitimately approaching a side port; only axis-aligned
      runs (within *axis_tol*) are inspected.
    * **Fan-in/-out bundle routes** through ``__junction_*`` / ``__merge_*``
      / ``__bypass_*`` virtual nodes route around or through neighbouring
      sections by a separate mechanism with its own guards; long-range
      multi-row merge bundles are a known, tracked
      limitation and are excluded here.
    """
    ports_by_sec: dict[str, list] = {}
    for port in graph.ports.values():
        ports_by_sec.setdefault(port.section_id, []).append(port)

    def near_port(sec_id: str, x: float, y: float) -> bool:
        return any(
            abs(p.x - x) <= port_tol and abs(p.y - y) <= port_tol
            for p in ports_by_sec.get(sec_id, [])
        )

    def edge_crossings(p1, p2, x0, y0, x1, y1):
        ax, ay = p1
        bx, by = p2
        hits = []
        for ex in (x0, x1):
            if (ax - ex) * (bx - ex) < 0:
                t = (ex - ax) / (bx - ax)
                yy = ay + t * (by - ay)
                if y0 - inset <= yy <= y1 + inset:
                    hits.append((ex, yy))
        for ey in (y0, y1):
            if (ay - ey) * (by - ey) < 0:
                t = (ey - ay) / (by - ay)
                xx = ax + t * (bx - ax)
                if x0 - inset <= xx <= x1 + inset:
                    hits.append((xx, ey))
        return hits

    def is_bundle_node(node_id: str) -> bool:
        return node_id.startswith(("__junction", "__merge", "__bypass"))

    boxes = [
        (
            sid,
            sec.bbox_x,
            sec.bbox_y,
            sec.bbox_x + sec.bbox_w,
            sec.bbox_y + sec.bbox_h,
        )
        for sid, sec in graph.sections.items()
        if sec.bbox_w > 0 and sec.bbox_h > 0
    ]
    for rp in routes:
        if is_bundle_node(rp.edge.source) or is_bundle_node(rp.edge.target):
            continue
        pts = rp.points
        for i in range(len(pts) - 1):
            p1, p2 = pts[i], pts[i + 1]
            # Only axis-aligned runs cut a box; diagonal corner curves clip
            # edges while approaching side ports legitimately.
            if abs(p1[0] - p2[0]) > axis_tol and abs(p1[1] - p2[1]) > axis_tol:
                continue
            for sid, x0, y0, x1, y1 in boxes:
                for bx, by in edge_crossings(p1, p2, x0, y0, x1, y1):
                    if not near_port(sid, bx, by):
                        return (rp, sid, bx, by)
    return None


def is_loop_side_branch_station(graph: MetroGraph, sid: str) -> bool:
    """Mirror ``_recenter_loop_side_stations``'s precondition: a station
    with exactly one in-edge and one out-edge, whose predecessor and
    successor share Y, sitting off the trunk Y between them in X.

    The engine moves such stations to the midpoint of their loop's
    diagonal corners; that move legitimately decouples their X from the
    section's column grid, so column-X consistency checks must exempt
    them.  Both ``_guard_station_x_column_drift`` here and the
    ``test_station_x_within_column_tolerance`` invariant call this.
    """
    st = graph.stations.get(sid)
    if st is None:
        return False
    ins = graph.edges_to(sid)
    outs = graph.edges_from(sid)
    if len(ins) != 1 or len(outs) != 1:
        return False
    src = graph.stations.get(ins[0].source)
    tgt = graph.stations.get(outs[0].target)
    if src is None or tgt is None:
        return False
    if abs(src.y - tgt.y) > 0.5:
        return False
    if abs(st.y - src.y) < 0.5:
        return False
    if not ((src.x < st.x < tgt.x) or (tgt.x < st.x < src.x)):
        return False
    return True


def _grid_group_section_ids(graph: MetroGraph) -> set[str]:
    """Return the set of section IDs that participated in grid alignment."""
    grid_info = graph._row_y_grid_info
    result: set[str] = set()
    for info in grid_info.values():
        result.update(info["section_ids"])
    return result


def _classify_multi_station_ys(
    sub: MetroGraph,
) -> tuple[dict[int, list[float]], set[float]]:
    """Classify Y values by layer and identify multi-station-layer Ys.

    Returns (layer_stations, multi_layer_ys) where layer_stations maps
    layer -> list of Y values, and multi_layer_ys is the set of Y values
    that appear in layers with >1 station.
    """
    layer_stations: dict[int, list[float]] = defaultdict(list)
    for s in sub.stations.values():
        layer_stations[s.layer].append(s.y)
    multi_layer_ys: set[float] = set()
    for ys_at_layer in layer_stations.values():
        if len(ys_at_layer) > 1:
            multi_layer_ys.update(ys_at_layer)
    return layer_stations, multi_layer_ys


def _max_stations_per_layer(sub: MetroGraph) -> int:
    """Return the maximum number of distinct Y positions at any single layer.

    Bypass V helpers (ids starting with ``__bypass_``) are excluded -
    they exist only for routing and must not inflate the row Y grid.
    """
    layer_ys: dict[int, set[float]] = defaultdict(set)
    for s in sub.stations.values():
        if s.id.startswith("__bypass_"):
            continue
        layer_ys[s.layer].add(s.y)
    return max((len(ys) for ys in layer_ys.values()), default=1)


def _row_contiguous_column_groups(
    graph: MetroGraph,
) -> list[list[Section]]:
    """Group laid-out sections by grid row into contiguous column runs.

    Each returned group has at least 2 sections sitting in adjacent
    grid columns (gap <= 1) within the same row.  Sections with no
    bbox or unassigned row are skipped, matching the precondition
    used by the row-alignment callers in this module.
    """
    by_row: dict[int, list[Section]] = defaultdict(list)
    for section in graph.sections.values():
        if section.bbox_h > 0 and section.grid_row >= 0:
            by_row[section.grid_row].append(section)

    result: list[list[Section]] = []
    for row in by_row.values():
        if len(row) < 2:
            continue
        row_sorted = sorted(row, key=lambda s: s.grid_col)
        group = [row_sorted[0]]
        for s in row_sorted[1:]:
            if s.grid_col - group[-1].grid_col <= 1:
                group.append(s)
            else:
                if len(group) >= 2:
                    result.append(group)
                group = [s]
        if len(group) >= 2:
            result.append(group)
    return result


def _section_trunk_y(graph: MetroGraph, section: Section) -> float | None:
    """Topmost Y of a full-bundle internal station connected to an LR port.

    This Y is what neighbouring sections must line up with for the row
    bundle to flow horizontally.  Returns ``None`` when no full-bundle
    internal station is directly connected to any LR port.  Bypass V
    helpers (ids starting with ``__bypass_``) are skipped - they exist
    only for routing and must not anchor the row's trunk.
    """
    if section.direction not in ("LR", "RL"):
        return None
    bundle = _section_bundle_lines(graph, section)
    if not bundle:
        return None
    port_ids = section.port_ids
    internal_ids = set(section.station_ids) - port_ids
    trunk_ys: set[float] = set()
    for pid in port_ids:
        p = graph.ports.get(pid)
        if p is None or p.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        candidates: list[str] = []
        for edge in graph.edges_from(pid):
            if edge.target in internal_ids:
                candidates.append(edge.target)
        for edge in graph.edges_to(pid):
            if edge.source in internal_ids:
                candidates.append(edge.source)
        for other_id in candidates:
            st = graph.stations.get(other_id)
            if (
                st
                and not st.is_port
                and not other_id.startswith("__bypass_")
                and set(graph.station_lines(other_id)) == bundle
            ):
                trunk_ys.add(round(st.y, 3))
    return min(trunk_ys) if trunk_ys else None


def _classify_section_station_ys(
    graph: MetroGraph, section: Section
) -> tuple[list[float], list[float], list[float]]:
    """Return (on_track_ys, off_track_ys, port_ys) for a section's stations."""
    on_track: list[float] = []
    off_track: list[float] = []
    ports: list[float] = []
    for sid in section.station_ids:
        st = graph.stations.get(sid)
        if st is None:
            continue
        if st.is_port:
            ports.append(st.y)
        elif st.off_track:
            off_track.append(st.y)
        else:
            on_track.append(st.y)
    return on_track, off_track, ports


def _section_bundle_lines(graph: MetroGraph, section: Section) -> set[str]:
    """Return the set of line IDs crossing a section's LEFT/RIGHT ports."""
    bundle: set[str] = set()
    for pid in list(section.entry_ports) + list(section.exit_ports):
        port = graph.ports.get(pid)
        if port is None or port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        bundle.update(graph.station_lines(pid))
    return bundle


def _fan_offsets(n: int) -> list[int]:
    """Symmetric vertical slot offsets for ``n`` stations fanned about a
    trunk Y: even ``n`` leaves the trunk row empty (-n//2..-1, 1..n//2),
    odd ``n`` keeps a middle station on the trunk (-(n//2)..n//2).
    """
    if n % 2 == 0:
        return list(range(-(n // 2), 0)) + list(range(1, n // 2 + 1))
    return list(range(-(n // 2), n // 2 + 1))


def _expand_bbox_for_y(section: Section, y: float) -> None:
    """Expand *section*'s bbox so *y* sits inside with padding."""
    pad = SECTION_Y_PADDING
    top = section.bbox_y
    bot = section.bbox_y + section.bbox_h
    if y - pad < top:
        section.bbox_h += top - (y - pad)
        section.bbox_y = y - pad
    elif y + pad > bot:
        section.bbox_h = (y + pad) - section.bbox_y


def _build_section_subgraph(graph: MetroGraph, section: Section) -> MetroGraph:
    """Build a temporary MetroGraph containing only a section's real stations and edges.

    Excludes port stations and any edges that touch ports. Ports are positioned
    separately on section boundaries after the internal layout is computed.
    """
    sub = MetroGraph()
    sub.lines = graph.lines  # Share line definitions
    sub.diamond_style = graph.diamond_style

    # Collect port IDs for this section
    port_ids = section.port_ids

    # Add only real (non-port) stations belonging to this section
    real_station_ids: set[str] = set()
    for sid in section.station_ids:
        if sid in port_ids:
            continue
        if sid in graph.stations:
            station = graph.stations[sid]
            if station.is_port:
                continue
            sub.add_station(
                Station(
                    id=station.id,
                    label=station.label,
                    section_id=station.section_id,
                    is_port=False,
                    off_track=station.off_track,
                    terminus_labels=list(station.terminus_labels),
                    terminus_icon_types=list(station.terminus_icon_types),
                    terminus_names=list(station.terminus_names),
                )
            )
            real_station_ids.add(sid)

    # Add only edges between real stations (no port-touching edges)
    for edge in graph.edges:
        if edge.source in real_station_ids and edge.target in real_station_ids:
            sub.add_edge(
                Edge(
                    source=edge.source,
                    target=edge.target,
                    line_id=edge.line_id,
                )
            )

    return sub


def _grow_section_bbox_upward(graph: MetroGraph, section, new_bbox_top: float) -> None:
    """Expand a section's bbox upward to *new_bbox_top* and pull TOP ports.

    BOTTOM ports stay put because the bbox only grows upward.
    """
    section.bbox_h += section.bbox_y - new_bbox_top
    section.bbox_y = new_bbox_top
    for pid in section.entry_ports + section.exit_ports:
        port = graph.ports.get(pid)
        port_st = graph.stations.get(pid)
        if not port or not port_st:
            continue
        if port.side == PortSide.TOP:
            port_st.y = section.bbox_y
            port.y = port_st.y
