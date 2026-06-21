"""Shared leaf helpers used across layout phases (bbox math, section queries)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from nf_metro.layout.constants import (
    COORD_TOLERANCE,
    SAME_COORD_TOLERANCE,
    SECTION_Y_PADDING,
    STATION_RADIUS_APPROX,
)
from nf_metro.layout.geometry import lanes_run_along_y
from nf_metro.parser.model import (
    Edge,
    MetroGraph,
    Port,
    PortSide,
    Section,
    Station,
    is_bypass_v,
)

if TYPE_CHECKING:
    from nf_metro.layout.routing.common import RoutedPath


def _is_fold_section(section: Section) -> bool:
    """``True`` for a section the row-fold logic produced.

    A fold either spans more than one grid row or runs its flow vertically
    (TB/BT).  Its exit ports are placed by the fold exit-port path
    (``_align_exit_ports``) rather than the row-level exit passes, which expect
    a single-row horizontal-flow section.
    """
    return section.grid_row_span > 1 or not lanes_run_along_y(section.direction)


@contextmanager
def _scoped_sections(graph: MetroGraph, section_ids: list[str]) -> Iterator[None]:
    """Temporarily restrict ``graph.sections`` to ``section_ids``.

    Row-local content phases iterate ``graph.sections`` and read only
    coordinates within each section, so restricting the view to one grid
    row's sections lets a whole-graph phase run row-by-row.  Station and
    edge data on ``graph`` are untouched, so the per-station caches stay
    valid.  Restores the original mapping on exit, including on error.
    """
    original = graph.sections
    graph.sections = {sid: original[sid] for sid in section_ids if sid in original}
    try:
        yield
    finally:
        graph.sections = original


@contextmanager
def _restoring_layout_geometry(graph: MetroGraph) -> Iterator[None]:
    """Restore station coords and section bboxes on exit.

    route_edges' diagonal-centring nudges Station.x and place_labels expands
    section bboxes to fit labels, so a probe or guard that re-routes and
    re-places to inspect the drawn geometry must undo those mutations:
    inspecting the settled layout must not perturb it.
    """
    pos = {sid: (s.x, s.y) for sid, s in graph.stations.items()}
    bbox = {
        sid: (s.bbox_x, s.bbox_y, s.bbox_w, s.bbox_h)
        for sid, s in graph.sections.items()
    }
    try:
        yield
    finally:
        for sid, (x, y) in pos.items():
            st = graph.stations.get(sid)
            if st is not None:
                st.x, st.y = x, y
        for sid, (bx, by, bw, bh) in bbox.items():
            s = graph.sections.get(sid)
            if s is not None:
                s.bbox_x, s.bbox_y, s.bbox_w, s.bbox_h = bx, by, bw, bh


def _grid_rows_top_to_bottom(graph: MetroGraph) -> list[list[str]]:
    """Section ids grouped by grid row, rows ordered top-to-bottom.

    Sections with no bbox or an unassigned row are dropped, matching the
    precondition the row-local content phases already apply when they skip
    such sections.  Ids within a row keep ascending-column order.
    """
    by_row: dict[int, list[Section]] = defaultdict(list)
    for section in graph.sections.values():
        if section.bbox_h > 0 and section.grid_row >= 0:
            by_row[section.grid_row].append(section)
    return [
        [s.id for s in sorted(by_row[row], key=lambda s: s.grid_col)]
        for row in sorted(by_row)
    ]


def _bbox_cols_overlap(a: Section, b: Section) -> bool:
    """True when two sections' bboxes overlap in X (share horizontal extent)."""
    return a.bbox_x < b.bbox_x + b.bbox_w and b.bbox_x < a.bbox_x + a.bbox_w


def _content_station_ys(graph: MetroGraph, section: Section) -> list[float]:
    """Y of every content marker in ``section``.

    Content = non-port stations excluding the ``__bypass_`` helpers; hidden
    phantoms are kept.  The single definition of the content set the
    top-fit helpers (:func:`...bbox._section_content_hug_top`,
    :func:`...bbox._section_fit_top`,
    :func:`...off_track._off_track_fit_top`) anchor on, so the set cannot
    drift between them -- e.g. a switch to ``is_hidden``, a superset that
    would drop the phantoms.
    """
    return [
        graph.stations[sid].y
        for sid in section.station_ids
        if (
            sid in graph.stations
            and not graph.stations[sid].is_port
            and not is_bypass_v(sid)
        )
    ]


def _station_marker_bbox(
    graph: MetroGraph,
    sid: str,
    offsets: dict[tuple[str, str], float] | None = None,
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


def first_vertical_leg_x(points: list[tuple[float, float]]) -> float | None:
    """X of the first (near-)vertical leg of *points*.

    The source-side vertical channel ("V1") of an inter-section route;
    ``None`` when no vertical leg exists.
    """
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if abs(x1 - x0) < COORD_TOLERANCE and abs(y1 - y0) > COORD_TOLERANCE:
            return x1
    return None


def first_vertical_leg_sign(points: list[tuple[float, float]]) -> int | None:
    """Sign of the first (near-)vertical leg of *points*.

    ``-1`` when the source-side vertical channel ("V1") heads up
    (toward smaller Y), ``+1`` when it heads down, ``None`` when no
    vertical leg exists.
    """
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if abs(x1 - x0) < COORD_TOLERANCE and abs(y1 - y0) > COORD_TOLERANCE:
            return -1 if y1 < y0 else 1
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
    routes: list[RoutedPath],
    *,
    port_tol: float = 24.0,
    inset: float = 4.0,
    axis_tol: float = 1.0,
) -> tuple[RoutedPath, str, float, float] | None:
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
    ports_by_sec: dict[str | None, list[Port]] = {}
    for port in graph.ports.values():
        ports_by_sec.setdefault(port.section_id, []).append(port)

    def near_port(sec_id: str, x: float, y: float) -> bool:
        return any(
            abs(p.x - x) <= port_tol and abs(p.y - y) <= port_tol
            for p in ports_by_sec.get(sec_id, [])
        )

    def edge_crossings(
        p1: tuple[float, float],
        p2: tuple[float, float],
        x0: float,
        y0: float,
        x1: float,
        y1: float,
    ) -> list[tuple[float, float]]:
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


def routes_through_unrelated_sections(
    graph: MetroGraph,
    *,
    inset: float = 2.0,
    routes: list[RoutedPath] | None = None,
    offsets: dict[tuple[str, str], float] | None = None,
) -> list[tuple[RoutedPath, str]]:
    """Return ``(route, section_id)`` for every routed segment that passes
    through the interior of a section box the route does not belong to.

    A metro line may only occupy a section's bbox coordinates where it
    connects to a station there: that is, the section must hold the route
    edge's source (the line starts there) or its target (the line enters
    via that section's port).  Any other section whose box a routed
    segment intersects is a pass-through error -- the line is plotted over
    a section it never interacts with (issue #484).

    Unlike :func:`_route_crosses_section_boundary`, this works on the final
    rendered geometry (route offsets applied) and inspects *every* route,
    including fan-in/-out bundle routes through ``__junction_*`` /
    ``__merge_*`` nodes, which the boundary guard intentionally excludes.
    Section membership is resolved via ``section_for_station`` (so a merge
    node assigned to its target section is correctly treated as belonging
    there).
    """
    from nf_metro.layout.geometry import segment_intersects_bbox
    from nf_metro.layout.routing import compute_station_offsets, route_edges
    from nf_metro.layout.routing.common import apply_route_offsets

    if offsets is None:
        offsets = compute_station_offsets(graph)
    if routes is None:
        try:
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:  # noqa: BLE001 - routing failures surface elsewhere
            return []

    boxes = [
        (
            sid,
            sec.bbox_x + inset,
            sec.bbox_y + inset,
            sec.bbox_x + sec.bbox_w - inset,
            sec.bbox_y + sec.bbox_h - inset,
        )
        for sid, sec in graph.sections.items()
        if sec.bbox_w > 2 * inset and sec.bbox_h > 2 * inset
    ]

    out: list[tuple[RoutedPath, str]] = []
    for rp in routes:
        own = {
            graph.section_for_station(rp.edge.source),
            graph.section_for_station(rp.edge.target),
        }
        pts = apply_route_offsets(rp, offsets)
        for sid, x0, y0, x1, y1 in boxes:
            if sid in own:
                continue
            if any(
                segment_intersects_bbox(
                    pts[i][0],
                    pts[i][1],
                    pts[i + 1][0],
                    pts[i + 1][1],
                    (x0, y0, x1, y1),
                )
                for i in range(len(pts) - 1)
            ):
                out.append((rp, sid))
    return out


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
    if abs(src.y - tgt.y) > SAME_COORD_TOLERANCE:
        return False
    if abs(st.y - src.y) < SAME_COORD_TOLERANCE:
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
        if is_bypass_v(s.id):
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


def _section_lr_port_anchor_y(graph: MetroGraph, section: Section) -> float | None:
    """The section's frozen trunk anchor: its LR/RL entry port Y, or the
    exit port Y when there is no LR/RL entry port.

    Unlike :func:`_section_trunk_y` (which reads an internal station's
    current Y), this returns a port station's Y -- a frozen inter-section
    anchor held fixed through content placement -- so content phases can
    centre on the trunk without depending on mutable station positions.
    Returns ``None`` when the section has no LR/RL port.
    """
    for ports in (section.entry_ports, section.exit_ports):
        for pid in ports:
            port = graph.ports.get(pid)
            st = graph.stations.get(pid)
            if port and st and port.side in (PortSide.LEFT, PortSide.RIGHT):
                return st.y
    return None


def _snapshot_placement_refs(graph: MetroGraph) -> None:
    """Freeze station Ys and section bbox tops as the placement reference.

    Captured once right before Stage 6.1 and read by Stages 6.1 / 6.2 via
    :func:`_ref_y` / :func:`_ref_bbox_top` for their slack and arrangement
    decisions, so re-applying or perturbing the live geometry between the
    snapshot and those phases can't change where they place content.  Sibling
    of :func:`...bbox._snapshot_struct_heights_below_top` (#485).
    """
    graph._placement_ref_y = {sid: st.y for sid, st in graph.stations.items()}
    graph._placement_ref_bbox_top = {
        sec.id: sec.bbox_y for sec in graph.sections.values()
    }


def _ref_y(graph: MetroGraph, sid: str) -> float:
    """Frozen reference Y for ``sid`` (see :func:`_snapshot_placement_refs`),
    falling back to the live Y when no snapshot covers it."""
    ref = graph._placement_ref_y.get(sid)
    return ref if ref is not None else graph.stations[sid].y


def _ref_bbox_top(graph: MetroGraph, section: Section) -> float:
    """Frozen reference bbox top for ``section`` (see
    :func:`_snapshot_placement_refs`), falling back to the live top."""
    ref = graph._placement_ref_bbox_top.get(section.id)
    return ref if ref is not None else section.bbox_y


def _section_trunk_y(graph: MetroGraph, section: Section) -> float | None:
    """Topmost Y of a full-bundle internal station connected to an LR port.

    This Y is what neighbouring sections must line up with for the row
    bundle to flow horizontally.  Returns ``None`` when no full-bundle
    internal station is directly connected to any LR port.  Bypass V
    helpers (ids starting with ``__bypass_``) are skipped - they exist
    only for routing and must not anchor the row's trunk.
    """
    if not lanes_run_along_y(section.direction):
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
                and not is_bypass_v(other_id)
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
    sub.line_spread = graph.section_line_spread(section.id)

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


def _pull_section_ports_to_edge(
    graph: MetroGraph, section: Section, side: PortSide, edge_y: float
) -> None:
    """Move every port on *side* of *section* to *edge_y*."""
    for pid in section.entry_ports + section.exit_ports:
        port = graph.ports.get(pid)
        port_st = graph.stations.get(pid)
        if not port or not port_st:
            continue
        if port.side == side:
            port_st.y = edge_y
            port.y = edge_y


def _set_section_bbox_top(
    graph: MetroGraph, section: Section, new_bbox_top: float
) -> None:
    """Move a section's bbox top to *new_bbox_top* and pull TOP ports along.

    Works in both directions: a smaller *new_bbox_top* grows the box
    upward, a larger one shrinks it.  BOTTOM ports stay put because only
    the top edge moves.
    """
    section.bbox_h += section.bbox_y - new_bbox_top
    section.bbox_y = new_bbox_top
    _pull_section_ports_to_edge(graph, section, PortSide.TOP, section.bbox_y)


def _grow_section_bbox_upward(
    graph: MetroGraph, section: Section, new_bbox_top: float
) -> None:
    """Expand a section's bbox upward to *new_bbox_top* and pull TOP ports.

    Grow-only convenience wrapper: callers guard on ``new_bbox_top <
    section.bbox_y`` so the top is never lowered.  See
    :func:`_set_section_bbox_top` for the bidirectional primitive.
    """
    _set_section_bbox_top(graph, section, new_bbox_top)


def _grow_section_bbox_downward(
    graph: MetroGraph, section: Section, new_bbox_bottom: float
) -> None:
    """Expand a section's bbox downward to *new_bbox_bottom* and pull BOTTOM
    ports along.  Grow-only: a *new_bbox_bottom* at or above the current
    bottom edge is a no-op, so the box is never raised.
    """
    if new_bbox_bottom <= section.bbox_y + section.bbox_h:
        return
    section.bbox_h = new_bbox_bottom - section.bbox_y
    _pull_section_ports_to_edge(
        graph, section, PortSide.BOTTOM, section.bbox_y + section.bbox_h
    )


def exit_run_corridor_clear(
    graph: MetroGraph,
    exit_port_id: str,
    section: Section,
    carrier_ids: list[str],
) -> bool:
    """Whether the X span between the carrier(s) and the exit port is free of
    other section stations.

    Anchoring a flow-aligned exit to its carrier row only helps when the
    straight run from the carrier to the port stays clear; a station seated
    in that span (e.g. an off-track output hung off the carrier) would be
    ploughed through, so the exit keeps its downstream-aligned placement.
    """
    port_st = graph.stations.get(exit_port_id)
    carrier_xs = [graph.stations[c].x for c in carrier_ids if c in graph.stations]
    if port_st is None or not carrier_xs:
        return False
    inner_x = max(carrier_xs) if section.direction == "LR" else min(carrier_xs)
    lo, hi = sorted((inner_x, port_st.x))
    carrier_set = set(carrier_ids)
    for sid in section.station_ids:
        if sid == exit_port_id or sid in carrier_set:
            continue
        st = graph.stations.get(sid)
        if st is None or st.is_port:
            continue
        if lo + SAME_COORD_TOLERANCE < st.x < hi - SAME_COORD_TOLERANCE:
            return False
    return True
