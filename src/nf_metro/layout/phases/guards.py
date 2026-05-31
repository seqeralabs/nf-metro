"""Stage-boundary invariant guards run by ``compute_layout(validate=True)``."""

from __future__ import annotations

import math
from collections import defaultdict

from nf_metro.layout.constants import (
    COORD_TOLERANCE,
    EDGE_TO_BUNDLE_CLEARANCE,
    GUARD_TOLERANCE,
    ICON_HALF_HEIGHT,
    OFFSET_STEP,
    SECTION_Y_GAP,
    STATION_RADIUS_APPROX,
    X_SPACING,
    Y_SPACING,
)
from nf_metro.layout.geometry import BBoxXIndex, segment_intersects_bbox
from nf_metro.layout.phases._common import (
    _bbox_cols_overlap,
    _canvas_width,
    _route_crosses_section_boundary,
    _section_bundle_lines,
    _station_marker_bbox,
    first_vertical_leg_x,
    is_loop_side_branch_station,
)
from nf_metro.layout.phases.bbox import _section_fit_top
from nf_metro.layout.phases.single_section import _terminus_y_overhang
from nf_metro.layout.phases.spacing import _residual_label_overlaps
from nf_metro.parser.model import MetroGraph, PortSide, Section


class PhaseInvariantError(Exception):
    """Raised when a layout phase produces invalid intermediate state."""


def _guard_coordinates_finite(graph: MetroGraph, phase: str) -> None:
    """After Stage 2.1+: all laid-out stations must have finite coordinates."""
    junction_ids = graph.junction_ids
    for sid, st in graph.stations.items():
        if st.section_id and not st.is_port and sid not in junction_ids:
            if math.isnan(st.x) or math.isnan(st.y):
                raise PhaseInvariantError(
                    f"{phase}: station {sid!r} has NaN coordinates (x={st.x}, y={st.y})"
                )
            if math.isinf(st.x) or math.isinf(st.y):
                raise PhaseInvariantError(
                    f"{phase}: station {sid!r} has infinite coordinates "
                    f"(x={st.x}, y={st.y})"
                )


def _bbox_guarded_stations(graph: MetroGraph):
    """Yield ``(sid, station, section)`` for each rendered station that a
    bbox-containment guard should check: skips ports, junctions, and
    stations whose section has no sized bbox.  Shared by the marker-edge
    and centre-containment guards so they can't drift on what they exempt.
    """
    junction_ids = graph.junction_ids
    for sid, st in graph.stations.items():
        sec = graph.sections.get(st.section_id or "")
        if not sec or st.is_port or sid in junction_ids or sec.bbox_w == 0:
            continue
        yield sid, st, sec


def _guard_stations_in_sections(graph: MetroGraph, phase: str) -> None:
    """After Stage 2.1+: rendered station markers (and terminus icons) must
    be fully within their section bbox.

    Tightened from station-centre containment to marker-edge containment:
    we expand the station's render-time footprint by ``STATION_RADIUS_APPROX``
    (regular markers) or ``ICON_HALF_HEIGHT`` (terminus / off-track icons)
    and require the expanded box to stay inside the section's bbox.  Centre
    containment alone hides regressions where off-track icons (~16 px half
    height) spill above the bbox top while still being technically "in" the
    section.
    """
    tol = GUARD_TOLERANCE
    for sid, st, sec in _bbox_guarded_stations(graph):
        # Off-track inputs and terminus icons render at icon scale; on-track
        # markers render at station-pill scale.  Use the wider reach so the
        # guard catches icon spill-over above the bbox top.
        half_h = (
            ICON_HALF_HEIGHT
            if (st.off_track or st.is_terminus)
            else STATION_RADIUS_APPROX
        )
        top = st.y - half_h
        bottom = st.y + half_h
        if not (
            sec.bbox_x - tol <= st.x <= sec.bbox_x + sec.bbox_w + tol
            and sec.bbox_y - tol <= top
            and bottom <= sec.bbox_y + sec.bbox_h + tol
        ):
            raise PhaseInvariantError(
                f"{phase}: station {sid!r} marker bbox "
                f"(x={st.x:.1f}, y={top:.1f}..{bottom:.1f}, "
                f"half_h={half_h:.1f}) "
                f"outside section {st.section_id!r} bbox "
                f"({sec.bbox_x:.1f}, {sec.bbox_y:.1f}, "
                f"w={sec.bbox_w:.1f}, h={sec.bbox_h:.1f})"
            )


# Sides that lie along a section's internal flow axis: an LR/RL section
# flows horizontally, so its flow-aligned ports are on the left/right; a
# TB/BT section flows vertically, so its flow-aligned ports are on the
# top/bottom.  A section with at least one flow-aligned port has an edge
# to anchor the start (or end) of its run to the bbox boundary.
_FLOW_ALIGNED_SIDES = {
    "LR": {PortSide.LEFT, PortSide.RIGHT},
    "RL": {PortSide.LEFT, PortSide.RIGHT},
    "TB": {PortSide.TOP, PortSide.BOTTOM},
    "BT": {PortSide.TOP, PortSide.BOTTOM},
}


def _section_lacks_flow_aligned_port(graph: MetroGraph, section: Section) -> bool:
    """True when *section* has ports but none on its flow axis.

    An internally-horizontal (LR/RL) section whose only ports are on the
    top/bottom (or a vertical section with only left/right ports) has no
    flow-aligned edge to pin its run to the bbox, so the engine lays the
    run out past the box.  The bbox-containment guard uses this to emit an
    actionable error for that unsupported directive combination.
    """
    flow_sides = _FLOW_ALIGNED_SIDES.get(section.direction)
    if flow_sides is None:
        return False
    sides = [graph.ports[pid].side for pid in section.port_ids if pid in graph.ports]
    return bool(sides) and not any(s in flow_sides for s in sides)


def _guard_stations_within_bbox(graph: MetroGraph, phase: str) -> None:
    """Always-on postcondition: every station centre must lie within its
    section's bbox (plus a small tolerance).

    Unlike :func:`_guard_stations_in_sections` (which runs only under
    ``validate`` mid-layout and checks marker-edge containment on the Y
    axis), this guard runs on every layout -- including the default render
    path -- and checks the *settled* bbox on both axes.  Forcing
    perpendicular ports on a horizontal section lays its stations out past
    the right of its own bbox, and the engine must reject that loudly
    rather than render it silently.
    """
    tol = GUARD_TOLERANCE
    for sid, st, sec in _bbox_guarded_stations(graph):
        inside_x = sec.bbox_x - tol <= st.x <= sec.bbox_x + sec.bbox_w + tol
        inside_y = sec.bbox_y - tol <= st.y <= sec.bbox_y + sec.bbox_h + tol
        if inside_x and inside_y:
            continue
        detail = (
            f"{phase}: station {sid!r} centre ({st.x:.1f}, {st.y:.1f}) "
            f"outside section {st.section_id!r} bbox "
            f"({sec.bbox_x:.1f}, {sec.bbox_y:.1f}, "
            f"w={sec.bbox_w:.1f}, h={sec.bbox_h:.1f})"
        )
        if _section_lacks_flow_aligned_port(graph, sec):
            detail += (
                f"; section {st.section_id!r} is internally {sec.direction} "
                f"but its only ports are perpendicular to that flow, so the "
                f"run has no flow-aligned port to anchor it to the bbox. "
                f"Give the section a flow-aligned entry/exit port "
                f"(left/right for LR/RL, top/bottom for TB/BT) or change "
                f"its '%%metro direction:'."
            )
        raise PhaseInvariantError(detail)


def _guard_ports_on_boundaries(graph: MetroGraph, phase: str) -> None:
    """After Stage 3.1+: ports must sit on their section's bounding box edge."""
    tolerance = GUARD_TOLERANCE
    for pid, port in graph.ports.items():
        st = graph.stations.get(pid)
        sec = graph.sections.get(st.section_id or "") if st else None
        if not st or not sec or sec.bbox_w == 0:
            continue
        on_left = abs(st.x - sec.bbox_x) <= tolerance
        on_right = abs(st.x - (sec.bbox_x + sec.bbox_w)) <= tolerance
        on_top = abs(st.y - sec.bbox_y) <= tolerance
        on_bottom = abs(st.y - (sec.bbox_y + sec.bbox_h)) <= tolerance
        if not (on_left or on_right or on_top or on_bottom):
            raise PhaseInvariantError(
                f"{phase}: port {pid!r} at ({st.x:.1f}, {st.y:.1f}) "
                f"not on any edge of section {st.section_id!r} bbox "
                f"({sec.bbox_x:.1f}, {sec.bbox_y:.1f}, "
                f"w={sec.bbox_w:.1f}, h={sec.bbox_h:.1f})"
            )


def _guard_section_bboxes_positive(graph: MetroGraph, phase: str) -> None:
    """After Stage 1.1+: non-empty sections must have positive-size bboxes."""
    for sid, sec in graph.sections.items():
        if not sec.station_ids:
            continue
        if sec.bbox_w < 0 or sec.bbox_h < 0:
            raise PhaseInvariantError(
                f"{phase}: section {sid!r} has negative bbox "
                f"(w={sec.bbox_w:.1f}, h={sec.bbox_h:.1f})"
            )


def _guard_no_negative_grid_columns(graph: MetroGraph, phase: str) -> None:
    """After Stage 1.1+: no section may sit at a negative grid column.

    The auto-layout serpentine packer steps a return row leftward from
    its fold bridge; without normalization that walk can run off the left
    edge into negative columns (issue #256), which renders the section's
    badge left of everything and snakes the inter-section trunk down the
    left margin. ``infer_section_layout`` normalizes the grid so the
    leftmost column is 0; this guard fails loudly if that ever regresses.
    """
    # Read from grid_overrides (populated with an explicit column for every
    # placed section) rather than Section.grid_col, whose -1 sentinel for
    # "auto" is indistinguishable from a genuine column -1.
    offenders = {
        sid: override[0]
        for sid, override in graph.grid_overrides.items()
        if override[0] < 0
    }
    if offenders:
        raise PhaseInvariantError(
            f"{phase}: sections at negative grid columns "
            f"(serpentine packer ran off the left edge): {offenders}"
        )


def _guard_explicit_grid_directions(graph: MetroGraph, phase: str) -> None:
    """Explicit-grid sections keep the LR default unless they carry an
    explicit %%metro direction.

    A section's grid position is the author's manual layout intent, not a
    flow-direction signal (issue #446). Direction inference therefore skips
    explicit-grid sections; this guard fails loudly if a future change ever
    lets inference reorient one (e.g. by reading override-aware positions),
    which would silently elongate serpentine-stacked maps vertically.
    """
    offenders = {
        sid: graph.sections[sid].direction
        for sid in graph._explicit_grid - graph._explicit_directions
        if sid in graph.sections and graph.sections[sid].direction != "LR"
    }
    if offenders:
        raise PhaseInvariantError(
            f"{phase}: explicit-grid sections with no %%metro direction were "
            f"inferred to a non-LR direction: {offenders}"
        )


def _guard_row_gaps(graph: MetroGraph, phase: str, *, section_y_gap: float) -> None:
    """Final phase: column-overlapping adjacent-row section pairs must
    keep at least ``section_y_gap`` between the upper section's bbox
    bottom and the lower section's bbox top.

    Sections that don't share horizontal extent are unconstrained --
    their vertical proximity has no visual impact.
    """
    tol = 0.5
    sections_by_row_start: dict[int, list[tuple[str, Section]]] = defaultdict(list)
    for sid, sec in graph.sections.items():
        if sec.bbox_w <= 0 or sec.bbox_h <= 0:
            continue
        sections_by_row_start[sec.grid_row].append((sid, sec))
    if not sections_by_row_start:
        return

    deepest: tuple[float, float, str, str] | None = None
    for usid, us in graph.sections.items():
        if us.bbox_w <= 0 or us.bbox_h <= 0:
            continue
        next_row = us.grid_row + us.grid_row_span
        for lsid, ls in sections_by_row_start.get(next_row, []):
            if not _bbox_cols_overlap(us, ls):
                continue
            gap = ls.bbox_y - (us.bbox_y + us.bbox_h)
            deficit = section_y_gap - gap
            if deficit > tol and (deepest is None or deficit > deepest[0]):
                deepest = (deficit, gap, usid, lsid)
    if deepest is None:
        return
    deficit, gap, usid, lsid = deepest
    raise PhaseInvariantError(
        f"{phase}: row gap below required: sections {usid!r} (bottom) "
        f"and {lsid!r} (top) overlap horizontally and are {gap:.1f}px "
        f"apart, expected >= {section_y_gap:.1f}px "
        f"(deficit {deficit:.1f}px)"
    )


def _guard_section_top_padding(
    graph: MetroGraph,
    phase: str,
    *,
    section_y_padding: float,
    section_y_gap: float,
) -> None:
    """Final phase: each section's bbox top must clear its highest marker.

    The mirror of the bottom-padding contract.  After
    :func:`_grow_bboxes_to_content_top` runs, every section's bbox top
    should sit at its content-anchored target (a full ``section_y_padding``
    above the highest marker, unless gap-bounded by the row above).  A
    bbox top below that target means a later pass crowded the topmost
    marker against the box edge (issue #406).
    """
    tol = 1.0
    for section in graph.sections.values():
        if section.bbox_h <= 0:
            continue
        target = _section_fit_top(graph, section, section_y_padding, section_y_gap)
        if target is None:
            continue
        if section.bbox_y > target + tol:
            raise PhaseInvariantError(
                f"{phase}: section {section.id!r} bbox top {section.bbox_y:.1f} "
                f"sits below its content-anchored target {target:.1f} "
                f"(highest marker crowds the bbox top edge)"
            )


def _guard_terminus_icons_within_bbox(graph: MetroGraph, phase: str) -> None:
    """Final phase: TB/BT terminus file icons must fit inside the section bbox.

    Vertical-flow termini stack their file icon (and caption) below or
    above the station marker; the section bbox must reserve that extent so
    the icon doesn't spill past the box edge (issue #254).
    """
    tol = 1.0
    for section in graph.sections.values():
        if section.bbox_h <= 0 or section.direction not in ("TB", "BT"):
            continue
        top = section.bbox_y
        bottom = section.bbox_y + section.bbox_h
        for sid in section.station_ids:
            st = graph.stations.get(sid)
            if st is None or not st.is_terminus:
                continue
            above, below = _terminus_y_overhang(st, section.direction, graph)
            if st.y + below > bottom + tol:
                raise PhaseInvariantError(
                    f"{phase}: terminus {sid!r} icons extend to "
                    f"{st.y + below:.1f}, past section {section.id!r} bbox "
                    f"bottom {bottom:.1f}"
                )
            if st.y - above < top - tol:
                raise PhaseInvariantError(
                    f"{phase}: terminus {sid!r} icons extend to "
                    f"{st.y - above:.1f}, above section {section.id!r} bbox "
                    f"top {top:.1f}"
                )


def _guard_no_station_overlap(
    graph: MetroGraph, phase: str, *, offsets: dict | None = None
) -> None:
    """Final-phase: no two station marker bboxes may overlap at render
    time, else one station hides another in the SVG.

    Sweep-line: bboxes are sorted by left edge, and the inner loop breaks
    once a candidate's left edge passes the current bbox's right edge.
    """
    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)
    boxes: list[tuple[str, tuple[float, float, float, float]]] = []
    for sid in graph.stations:
        b = _station_marker_bbox(graph, sid, offsets=offsets)
        if b is not None:
            boxes.append((sid, b))
    boxes.sort(key=lambda item: item[1][0])
    tol = 0.5
    n = len(boxes)
    for i in range(n):
        s1, (x1, y1, X1, Y1) = boxes[i]
        for j in range(i + 1, n):
            s2, (x2, y2, X2, Y2) = boxes[j]
            if x2 >= X1 - tol:
                break  # Sorted by left edge; no further X-overlap possible.
            if y1 < Y2 - tol and y2 < Y1 - tol:
                raise PhaseInvariantError(
                    f"{phase}: position clash: {s1!r} at "
                    f"({(x1 + X1) / 2:.1f},{(y1 + Y1) / 2:.1f}) overlaps "
                    f"{s2!r} at ({(x2 + X2) / 2:.1f},{(y2 + Y2) / 2:.1f})"
                )


def _guard_no_line_crosses_non_consumer(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict | None = None,
    routes: list | None = None,
) -> None:
    """Final-phase: no rendered line segment may pass through a
    station marker whose station neither consumes nor produces that
    line.

    Complements ``_guard_no_station_overlap``: station/station marker
    overlap catches one class of clash; this catches the other --
    a line bundle routed at a Y that crosses an off-trunk station's
    marker bbox while bypassing it (the "breeze-past" pattern).
    A common trigger is a sparse single-line consumer (e.g. ``grea``
    in the differential-functional section, consuming only rnaseq)
    sharing its trunk-Y row with a busier sibling whose inbound
    bundle traverses the sparse consumer's column.
    """
    from nf_metro.render.svg import apply_route_offsets

    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)
    if routes is None:
        from nf_metro.layout.routing import route_edges

        try:
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:  # noqa: BLE001 - routing failure surfaces elsewhere
            return

    boxes: list[tuple[str, tuple[float, float, float, float]]] = []
    station_lines_cache: dict[str, set[str]] = {}
    for sid in graph.stations:
        bbox = _station_marker_bbox(graph, sid, offsets=offsets)
        if bbox is None:
            continue
        boxes.append((sid, bbox))
        station_lines_cache[sid] = set(graph.station_lines(sid))
    if not boxes:
        return
    index = BBoxXIndex(boxes)

    for r in routes:
        pts = apply_route_offsets(r, offsets)
        src, tgt, line_id = r.edge.source, r.edge.target, r.line_id
        for k in range(len(pts) - 1):
            p1, p2 = pts[k], pts[k + 1]
            for sid, bbox in index.query_x_range(min(p1[0], p2[0]), max(p1[0], p2[0])):
                if line_id in station_lines_cache[sid]:
                    continue
                if src == sid or tgt == sid:
                    continue
                if segment_intersects_bbox(p1[0], p1[1], p2[0], p2[1], bbox):
                    raise PhaseInvariantError(
                        f"{phase}: line {line_id!r} on edge "
                        f"{src!r} -> {tgt!r} "
                        f"crosses non-consumer station {sid!r} "
                        f"marker bbox ({bbox[0]:.1f},{bbox[1]:.1f})-"
                        f"({bbox[2]:.1f},{bbox[3]:.1f}); segment "
                        f"({p1[0]:.1f},{p1[1]:.1f})->"
                        f"({p2[0]:.1f},{p2[1]:.1f})"
                    )


def _guard_row_trunk_cy_consistent(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict | None = None,
) -> None:
    """Final-phase: same-row LR sections that share the same line bundle
    AND whose trunk Y-ranges overlap must render their trunk marker at
    the same cy within ``GUARD_TOLERANCE``.

    The bundle-overlap filter means same-row sections carrying disjoint
    line sets (e.g. parallel sub-rows on a row-spanner) don't trigger.
    """
    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)

    def _section_trunk_info(sec) -> tuple[float, float, float, set[str]] | None:
        bundle = _section_bundle_lines(graph, sec)
        if not bundle:
            return None
        port_ys: list[float] = []
        for pid in list(sec.entry_ports) + list(sec.exit_ports):
            pst = graph.stations.get(pid)
            pport = graph.ports.get(pid)
            if (
                pst is not None
                and pport is not None
                and pport.side in (PortSide.LEFT, PortSide.RIGHT)
            ):
                port_ys.append(pst.y)
        if not port_ys:
            return None
        port_y = port_ys[0]
        port_set = sec.port_ids
        best: tuple[float, float, float, float] | None = None
        for sid in sec.station_ids:
            if sid in port_set:
                continue
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.is_hidden:
                continue
            lines = graph.station_lines(sid)
            if set(lines) != bundle:
                continue
            line_offs = [offsets.get((sid, lid), 0.0) for lid in lines]
            if not line_offs:
                continue
            y_min = st.y + min(line_offs)
            y_max = st.y + max(line_offs)
            cy = st.y + (min(line_offs) + max(line_offs)) / 2
            dist = abs(cy - port_y)
            if best is None or dist < best[0]:
                best = (dist, cy, y_min, y_max)
        if best is None:
            return None
        return (best[1], best[2], best[3], bundle)

    rows: dict[int, list] = {}
    for sec in graph.sections.values():
        if (
            sec.bbox_h <= 0
            or sec.grid_row < 0
            or sec.direction not in ("LR", "RL")
            or sec.grid_row_span > 1
        ):
            continue
        rows.setdefault(sec.grid_row, []).append(sec)

    for row, sections in rows.items():
        info: dict[str, tuple[float, float, float, set[str]]] = {}
        for sec in sections:
            t = _section_trunk_info(sec)
            if t is not None:
                info[sec.id] = t
        if len(info) < 2:
            continue
        parent = {sid: sid for sid in info}

        def _find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        ids = list(info)
        for i, a in enumerate(ids):
            cy_a, lo_a, hi_a, bun_a = info[a]
            for b in ids[i + 1 :]:
                cy_b, lo_b, hi_b, bun_b = info[b]
                bands_overlap = min(hi_a, hi_b) - max(lo_a, lo_b) >= -GUARD_TOLERANCE
                if bands_overlap and bun_a == bun_b:
                    ra, rb = _find(a), _find(b)
                    if ra != rb:
                        parent[ra] = rb

        groups: dict[str, list[str]] = {}
        for sid in ids:
            groups.setdefault(_find(sid), []).append(sid)

        for members in groups.values():
            if len(members) < 2:
                continue
            anchor = members[0]
            anchor_cy = info[anchor][0]
            for sid in members[1:]:
                cy = info[sid][0]
                if abs(cy - anchor_cy) > GUARD_TOLERANCE:
                    raise PhaseInvariantError(
                        f"{phase}: row {row} trunk cy drift: "
                        f"section {sid!r} cy={cy:.1f} vs "
                        f"section {anchor!r} cy={anchor_cy:.1f}"
                    )


def _guard_inter_section_routes_in_row_band(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict | None = None,
    routes: list | None = None,
) -> None:
    """After routing: inter-section routes whose endpoints both sit in
    grid row R must keep all waypoint Ys within a one-row band centered
    on R, plus ``Y_SPACING`` slack for diagonal corner approach.
    """
    if offsets is None or routes is None:
        from nf_metro.layout.routing import compute_station_offsets, route_edges

        if offsets is None:
            offsets = compute_station_offsets(graph)
        if routes is None:
            routes = route_edges(graph, station_offsets=offsets)

    row_band: dict[int, tuple[float, float]] = {}
    for sec in graph.sections.values():
        if sec.bbox_h <= 0 or sec.grid_row_span != 1:
            continue
        cur = row_band.get(sec.grid_row)
        top = sec.bbox_y
        bot = sec.bbox_y + sec.bbox_h
        if cur is None:
            row_band[sec.grid_row] = (top, bot)
        else:
            row_band[sec.grid_row] = (min(cur[0], top), max(cur[1], bot))

    slack = Y_SPACING
    for r in routes:
        src = graph.stations.get(r.edge.source)
        tgt = graph.stations.get(r.edge.target)
        if src is None or tgt is None:
            continue
        if src.section_id is None or tgt.section_id is None:
            continue
        if src.section_id == tgt.section_id:
            continue
        sec_a = graph.sections.get(src.section_id)
        sec_b = graph.sections.get(tgt.section_id)
        if sec_a is None or sec_b is None:
            continue
        if sec_a.grid_row != sec_b.grid_row:
            continue
        if sec_a.grid_row_span != 1 or sec_b.grid_row_span != 1:
            continue
        band = row_band.get(sec_a.grid_row)
        if band is None:
            continue
        lo, hi = band[0] - slack, band[1] + slack
        for _x, y in r.points:
            if y < lo or y > hi:
                raise PhaseInvariantError(
                    f"{phase}: route {r.edge.source!r}->{r.edge.target!r} "
                    f"line {r.line_id!r} waypoint y={y:.1f} outside "
                    f"row-{sec_a.grid_row} band [{lo:.1f}..{hi:.1f}]"
                )


def _ensure_routes(graph: MetroGraph, routes: list | None) -> list:
    """Return *routes*, routing all edges first if the caller didn't supply them."""
    if routes is not None:
        return routes
    from nf_metro.layout.routing import route_edges

    return route_edges(graph)


def _route_exit_side(graph: MetroGraph, rp) -> PortSide | None:
    """Side of the port a route exits through (directly or via its feeder)."""
    port = graph.ports.get(rp.edge.source)
    if port is not None:
        return port.side
    for e in graph.edges_to(rp.edge.source):
        port = graph.ports.get(e.source)
        if port is not None:
            return port.side
    return None


def _inter_section_backtrack_legs(
    graph: MetroGraph,
    routes: list,
    *,
    reference: str = "grid",
    tolerance: float = 0.0,
    include_exempt: bool = False,
):
    """Yield ``(rp, x1, x2)`` for each horizontal leg of a forward LR
    inter-section route that reverses against its flow.

    *reference* selects how "forward" is defined:

    * ``"grid"`` - flow points toward the target grid column (strict;
      used by the monotonic guard, which assumes grid order matches X).
    * ``"endpoint"`` - flow points toward the route's own endpoint X
      (tolerant of a nested-column approach where the target column sits
      left of its source; used by the full-width dog-leg guard).

    *tolerance* widens the reversal threshold; *include_exempt* keeps
    ``normalize_exempt`` wrap routes (needed to measure around-section
    dog-legs).  Routes exiting a port that faces away from their target
    column legitimately wrap and are skipped, as are TB folds and
    same-column routes.
    """
    from nf_metro.layout.routing.common import resolve_section

    for rp in routes:
        if not rp.is_inter_section:
            continue
        if rp.normalize_exempt and not include_exempt:
            continue
        src_sec = resolve_section(graph, graph.stations[rp.edge.source])
        tgt_sec = resolve_section(graph, graph.stations[rp.edge.target])
        if src_sec is None or tgt_sec is None:
            continue
        if src_sec.direction != "LR" or tgt_sec.direction != "LR":
            continue
        if src_sec.grid_col == tgt_sec.grid_col:
            continue
        xs = [p[0] for p in rp.points]
        if len(xs) < 2:
            continue
        rightward_cols = tgt_sec.grid_col > src_sec.grid_col
        side = _route_exit_side(graph, rp)
        if rightward_cols and side != PortSide.RIGHT:
            continue
        if not rightward_cols and side != PortSide.LEFT:
            continue
        forward_is_right = rightward_cols if reference == "grid" else xs[-1] > xs[0]
        for x1, x2 in zip(xs, xs[1:]):
            backtracks = (
                (x2 < x1 - tolerance) if forward_is_right else (x2 > x1 + tolerance)
            )
            if backtracks:
                yield rp, x1, x2


def _guard_inter_section_route_no_backtrack(
    graph: MetroGraph,
    phase: str,
    *,
    routes: list | None = None,
) -> None:
    """After routing: a forward-flowing inter-section route between two LR
    columns must be X-monotonic.

    A route that exits a port toward its target column (rightward exit, target
    to the right) must not contain a horizontal segment that reverses; such a
    backtrack renders as a turn-back toward the section just behind the exit
    (#386).  Routes that exit AWAY from their target legitimately wrap and are
    skipped, as are ``normalize_exempt`` wrap legs, TB folds, and same-column
    routes.
    """
    from nf_metro.layout.routing.common import resolve_section

    routes = _ensure_routes(graph, routes)

    for rp, x1, x2 in _inter_section_backtrack_legs(
        graph, routes, reference="grid", tolerance=GUARD_TOLERANCE
    ):
        src_sec = resolve_section(graph, graph.stations[rp.edge.source])
        tgt_sec = resolve_section(graph, graph.stations[rp.edge.target])
        rightward = tgt_sec.grid_col > src_sec.grid_col
        raise PhaseInvariantError(
            f"{phase}: route {rp.edge.source!r}->{rp.edge.target!r} "
            f"line {rp.line_id!r} backtracks x={x1:.1f}->{x2:.1f} "
            f"against its {'rightward' if rightward else 'leftward'} flow"
        )


def _guard_fan_bundles_coincide_or_separate(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict | None = None,
    routes: list | None = None,
) -> None:
    """After routing: two routes carrying the SAME line out of a unified-fan
    junction must coincide on their source-side vertical channel or separate
    clearly - never smear a few px apart.

    A unified-fan junction (one the router assigns shared
    ``junction_fan_info`` positions) fans the same line to multiple targets
    that are MEANT to pivot through one channel.  When two such routes' first
    vertical legs sit between ``OFFSET_STEP`` (the legitimate per-bundle
    stagger) and ``SECTION_Y_GAP`` (a clean column split) apart, they render
    as a smeared partial overlap rather than one bundle or two separated
    bundles.
    """
    from nf_metro.layout.routing import compute_station_offsets, route_edges
    from nf_metro.layout.routing.core import compute_junction_fan_info

    if offsets is None:
        offsets = compute_station_offsets(graph)
    if routes is None:
        routes = route_edges(graph, station_offsets=offsets)
    fan_sources = {key[0] for key in compute_junction_fan_info(graph)}
    if not fan_sources:
        return

    by_src_line: dict[tuple[str, str], list[float]] = {}
    for rp in routes:
        if not rp.is_inter_section or rp.edge.source not in fan_sources:
            continue
        vx = first_vertical_leg_x(rp.points)
        if vx is None:
            continue
        by_src_line.setdefault((rp.edge.source, rp.line_id), []).append(vx)

    # Coincide within the per-bundle stagger plus a 1px rounding epsilon;
    # GUARD_TOLERANCE (5px) would swallow the 6px smear this guards against.
    coincide_tol = OFFSET_STEP + 1.0
    for (src, line), xs in by_src_line.items():
        if len(xs) < 2:
            continue
        ordered = sorted(xs)
        for lo, hi in zip(ordered, ordered[1:]):
            gap = hi - lo
            if coincide_tol < gap < SECTION_Y_GAP:
                raise PhaseInvariantError(
                    f"{phase}: junction {src!r} line {line!r} fans two routes "
                    f"whose first vertical channels are {gap:.1f}px apart "
                    f"(x={lo:.1f} vs {hi:.1f}) - neither coincident "
                    f"(<= {coincide_tol:.1f}) nor clearly separated "
                    f"(>= {SECTION_Y_GAP:.1f}); a smeared partial overlap"
                )


def inter_section_route_backtrack_legs(graph: MetroGraph, routes: list):
    """Yield ``(rp, x1, x2)`` for each horizontal leg that moves *away* from
    the route's own endpoint X - a genuine out-and-back dog-leg.

    A backtrack is reverse-direction travel: the line heads away from where
    it is going, then has to come back.  This is measured against the
    route's actual endpoint X (its last waypoint), not the grid-column
    order.  Grid columns can disagree with X order when a narrow target
    column nests inside a wide row-span sibling: there the target column is
    "higher" yet sits to the *left*, so a single long leftward traverse is a
    monotonic approach toward the target - not a dog-leg - and is not
    yielded.  A true dog-leg (right past the target, then back left)
    still moves away from the endpoint on its outward leg and is yielded.

    Routes that exit a port facing away from their endpoint legitimately
    wrap and are skipped, as are TB folds and same-column routes.  Unlike
    the strict :func:`_guard_inter_section_route_no_backtrack`, exempt
    (``normalize_exempt``) wrap routes are *included* so a multi-corner
    around-section dog-leg is still measured.
    """
    yield from _inter_section_backtrack_legs(
        graph, routes, reference="endpoint", tolerance=0.0, include_exempt=True
    )


def _guard_inter_section_route_no_full_width_backtrack(
    graph: MetroGraph,
    phase: str,
    *,
    routes: list | None = None,
    fraction: float = 0.4,
) -> None:
    """After routing: a forward inter-section route may reverse in X (when a
    narrow target column nests inside an oversized sibling) but no single
    backtrack leg may exceed *fraction* of the canvas width.

    The strict :func:`_guard_inter_section_route_no_backtrack` forbids *any*
    reversal on a forward LR route, assuming grid-column order matches X
    order.  When a column is geometrically nested inside an oversized
    sibling, reaching it requires a legitimate X
    reversal, so such routes are made ``normalize_exempt`` and the strict
    guard skips them.  This guard still bounds those reversals: a
    right-then-left dog-leg sweeping the whole diagram is forbidden
    even when exempt.
    """
    routes = _ensure_routes(graph, routes)

    canvas_width = _canvas_width(graph)
    if canvas_width <= 0:
        return
    limit = fraction * canvas_width

    for rp, x1, x2 in inter_section_route_backtrack_legs(graph, routes):
        span = abs(x2 - x1)
        if span > limit + GUARD_TOLERANCE:
            raise PhaseInvariantError(
                f"{phase}: route {rp.edge.source!r}->{rp.edge.target!r} "
                f"line {rp.line_id!r} backtracks {span:.1f}px in one leg "
                f"(x={x1:.1f}->{x2:.1f}), exceeding {fraction:.0%} of canvas "
                f"width {canvas_width:.1f} - a full-width dog-leg"
            )


def _guard_routes_enter_sections_at_ports(
    graph: MetroGraph,
    phase: str,
    *,
    routes: list | None = None,
) -> None:
    """After routing: no routed segment may cross a section bbox boundary
    except within tolerance of a declared port on that section.

    A line that cuts through a section box anywhere other than a port is
    visually entering/leaving the section where nothing invites it (e.g. a
    fan-in merge bundle ploughing into a section through its right edge, or
    an entry inferred on the wrong side so the connector slices the box).
    """
    routes = _ensure_routes(graph, routes)

    hit = _route_crosses_section_boundary(graph, routes)
    if hit is not None:
        rp, sid, bx, by = hit
        raise PhaseInvariantError(
            f"{phase}: route {rp.edge.source!r}->{rp.edge.target!r} "
            f"line {rp.line_id!r} crosses section {sid!r} boundary at "
            f"({bx:.1f}, {by:.1f}) away from any declared port"
        )


def _guard_serpentine_no_backtrack(
    graph: MetroGraph,
    phase: str,
    *,
    routes: list | None = None,
) -> None:
    """After routing: stacked same-direction sections must not backtrack.

    Same-direction sections stacked in one grid column and chained
    serpentine their effective flow row by row so consecutive sections meet
    on a shared side joined by a short vertical drop.  A section that fails
    to alternate enters on the wrong side and folds its internal route back
    across the section width.  For every section in a detected serpentine
    run, the wrong-way horizontal travel of its internal segments must stay
    below half the section width.
    """
    from nf_metro.layout.auto_layout import detect_serpentine_runs

    routes = _ensure_routes(graph, routes)

    dag = graph.section_dag
    if dag is None:
        return
    runs = detect_serpentine_runs(graph, dag.successors, dag.predecessors)
    serpentine_sections = {sid for run in runs for sid in run}
    if not serpentine_sections:
        return

    wrong_way: dict[str, float] = {sid: 0.0 for sid in serpentine_sections}
    for rp in routes:
        src_sec = graph.section_for_station(rp.edge.source)
        if src_sec != graph.section_for_station(rp.edge.target):
            continue
        if src_sec not in serpentine_sections:
            continue
        forward = 1.0 if graph.sections[src_sec].direction != "RL" else -1.0
        xs = [p[0] for p in rp.points]
        for x1, x2 in zip(xs, xs[1:]):
            dx = x2 - x1
            if dx * forward < 0:
                wrong_way[src_sec] += abs(dx)

    for sid, against in wrong_way.items():
        section = graph.sections[sid]
        limit = 0.5 * max(section.bbox_w, 1.0)
        if against > limit + GUARD_TOLERANCE:
            raise PhaseInvariantError(
                f"{phase}: stacked section {sid!r} (dir={section.direction}) "
                f"backtracks {against:.1f}px against its flow (>{limit:.1f}px); "
                f"the serpentine chain is kinking instead of dropping vertically"
            )


def _guard_inter_row_run_clearance(
    graph: MetroGraph,
    phase: str,
    *,
    routes: list | None = None,
) -> None:
    """After routing: a horizontal leg of an inter-*row* route must keep
    ``EDGE_TO_BUNDLE_CLEARANCE`` from its source section's near bbox edge.

    An inter-section bundle that crosses grid rows (e.g. a right-exit
    wrapping down to a left-entry below) lands its horizontal run in the
    inter-row gap.  A run grazing the source bbox reads as "running along
    under the box".  The placement-side widening
    (``_wrap_bundle_row_minimums``) reserves the space; this guard fails
    loudly if a layout change ever lets the run creep back against the box.
    """
    from nf_metro.layout.routing.common import resolve_section

    routes = _ensure_routes(graph, routes)

    tol = GUARD_TOLERANCE
    for rp in routes:
        if not rp.is_inter_section:
            continue
        src_sec = resolve_section(graph, graph.stations.get(rp.edge.source))
        tgt_sec = resolve_section(graph, graph.stations.get(rp.edge.target))
        if src_sec is None or tgt_sec is None:
            continue
        if src_sec.grid_row == tgt_sec.grid_row:
            continue
        left = src_sec.bbox_x
        right = left + src_sec.bbox_w
        top = src_sec.bbox_y
        bottom = top + src_sec.bbox_h
        pts = rp.points
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if abs(y1 - y0) > tol or abs(x1 - x0) < tol:
                continue  # horizontal runs only
            xlo, xhi = sorted((x0, x1))
            if xhi <= left + tol or xlo >= right - tol:
                continue  # run doesn't overlap the source section in X
            y = y0
            if bottom + tol < y < bottom + EDGE_TO_BUNDLE_CLEARANCE - tol:
                raise PhaseInvariantError(
                    f"{phase}: inter-row run of {rp.edge.source!r}->"
                    f"{rp.edge.target!r} line {rp.line_id!r} at y={y:.1f} sits "
                    f"{y - bottom:.1f}px below source section {src_sec.id!r} "
                    f"bottom={bottom:.1f} (< {EDGE_TO_BUNDLE_CLEARANCE})"
                )
            if top - EDGE_TO_BUNDLE_CLEARANCE + tol < y < top - tol:
                raise PhaseInvariantError(
                    f"{phase}: inter-row run of {rp.edge.source!r}->"
                    f"{rp.edge.target!r} line {rp.line_id!r} at y={y:.1f} sits "
                    f"{top - y:.1f}px above source section {src_sec.id!r} "
                    f"top={top:.1f} (< {EDGE_TO_BUNDLE_CLEARANCE})"
                )


def _guard_inter_section_descent_edge_clearance(
    graph: MetroGraph,
    phase: str,
    *,
    routes: list | None = None,
) -> None:
    """After routing: a vertical descent channel of an inter-section route
    must not *incidentally* graze a section bbox edge.

    A descent legitimately sits on a section edge when its X coincides
    with a port at one of the route's endpoints (a port-to-port drop).
    When the channel instead lands within ``EDGE_TO_BUNDLE_CLEARANCE`` of
    a section edge, on the interior side, with no endpoint port at that
    X, the lines visibly cross the border.  The channel-x selection
    in :func:`_route_l_shape` pushes such channels outward; this guard
    fails loudly if a future change lets one creep back against an edge.
    """
    from nf_metro.layout.routing.common import endpoint_port_xs

    routes = _ensure_routes(graph, routes)

    tol = GUARD_TOLERANCE
    for rp in routes:
        if not rp.is_inter_section:
            continue
        port_xs = endpoint_port_xs(graph, rp.edge)
        pts = rp.points
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if abs(x1 - x0) > tol:
                continue  # vertical segments only
            vx = (x0 + x1) / 2
            if any(abs(vx - px) <= COORD_TOLERANCE for px in port_xs):
                continue  # legitimate port-to-port drop
            ylo, yhi = sorted((y0, y1))
            for sec in graph.sections.values():
                if sec.bbox_w <= 0:
                    continue
                if yhi < sec.bbox_y or ylo > sec.bbox_y + sec.bbox_h:
                    continue
                left = sec.bbox_x
                right = left + sec.bbox_w
                from_left = vx - left
                from_right = right - vx
                grazes = (-tol <= from_left < EDGE_TO_BUNDLE_CLEARANCE - tol) or (
                    -tol <= from_right < EDGE_TO_BUNDLE_CLEARANCE - tol
                )
                if grazes:
                    edge_x = left if from_left < from_right else right
                    raise PhaseInvariantError(
                        f"{phase}: descent of {rp.edge.source!r}->"
                        f"{rp.edge.target!r} line {rp.line_id!r} at x={vx:.1f} "
                        f"grazes section {sec.id!r} edge x={edge_x:.1f} "
                        f"(< {EDGE_TO_BUNDLE_CLEARANCE})"
                    )


def _guard_bundle_order_preserved(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict | None = None,
    routes: list | None = None,
) -> None:
    """Final-phase: at every shared-xy corner where 2 or more bundled
    lines meet, the lines' relative left/right ordering must be
    preserved between incoming and outgoing tangents.

    See ``src/nf_metro/layout/routing/invariants.py`` for the
    semantic definition.  The guard is a thin wrapper: it routes the
    edges (if not provided), invokes
    :func:`check_bundle_order_preserved`, and raises
    :class:`PhaseInvariantError` with the first violation's
    self-describing message (the full violation list is summarised in
    the count).

    The check operates on the final ``route_edges`` output, so it can
    only run at the final guard block where the routing is stable.
    """
    from nf_metro.layout.routing.invariants import check_bundle_order_preserved

    if routes is None:
        from nf_metro.layout.routing import compute_station_offsets, route_edges

        if offsets is None:
            offsets = compute_station_offsets(graph)
        try:
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:  # noqa: BLE001 - routing failure surfaces elsewhere
            return

    violations = check_bundle_order_preserved(routes)
    if not violations:
        return
    first = violations[0]
    extra = f" (+{len(violations) - 1} more)" if len(violations) > 1 else ""
    raise PhaseInvariantError(f"{phase}: {first.message()}{extra}")


def _guard_merge_port_approach_side(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict | None = None,
) -> None:
    """Final-phase: at every multi-feeder reconvergence merge port, a
    line that re-joins the bundle perpendicular (rising from a section
    below, descending from one above) must take the bundle slot nearest
    its approach side, so its riser does not cross over the lines that
    arrive horizontally.

    See
    :func:`nf_metro.layout.routing.invariants.check_merge_port_approach_side`
    for the semantic definition.
    """
    from nf_metro.layout.routing.invariants import check_merge_port_approach_side

    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)

    violations = check_merge_port_approach_side(graph, offsets)
    if not violations:
        return
    first = violations[0]
    extra = f" (+{len(violations) - 1} more)" if len(violations) > 1 else ""
    raise PhaseInvariantError(f"{phase}: {first.message()}{extra}")


def _guard_partial_branch_offset_gaps(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict | None = None,
) -> None:
    """Final-phase: under ``compact_offsets``, an independent fan branch
    that carries only a subset of a bundle's lines must place them on
    consecutive offset slots, not reserve an empty interior slot for the
    lines it omits (which parks its marker off-centre with a gap).

    See
    :func:`nf_metro.layout.routing.invariants.check_partial_branch_offset_gaps`
    for the semantic definition.
    """
    from nf_metro.layout.routing.invariants import check_partial_branch_offset_gaps

    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)

    violations = check_partial_branch_offset_gaps(graph, offsets)
    if not violations:
        return
    first = violations[0]
    extra = f" (+{len(violations) - 1} more)" if len(violations) > 1 else ""
    raise PhaseInvariantError(f"{phase}: {first.message()}{extra}")


def _guard_fanout_tail_join(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict | None = None,
    routes: list | None = None,
) -> None:
    """Final-phase: at every single-source fan-out junction, each
    upstream ``port -> junction`` route must hand off to its same-line
    downstream ``junction -> target`` route with no gap along the line's
    travel direction (no visible apex notch).

    See :func:`nf_metro.layout.routing.invariants.check_fanout_tail_join`
    for the semantic definition.
    """
    from nf_metro.layout.routing.invariants import check_fanout_tail_join

    if routes is None:
        from nf_metro.layout.routing import compute_station_offsets, route_edges

        if offsets is None:
            offsets = compute_station_offsets(graph)
        try:
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:  # noqa: BLE001 - routing failure surfaces elsewhere
            return

    gaps = check_fanout_tail_join(routes, graph)
    if not gaps:
        return
    first = gaps[0]
    extra = f" (+{len(gaps) - 1} more)" if len(gaps) > 1 else ""
    raise PhaseInvariantError(f"{phase}: {first.message()}{extra}")


def _guard_off_track_inputs_above_consumer(graph: MetroGraph, phase: str) -> None:
    """After Stage 4.5 and final: off-track input stations must sit at
    least ``GUARD_TOLERANCE`` above (smaller Y than) their on-track
    consumer.
    """
    junction_ids = graph.junction_ids
    consumer_of: dict[str, str] = {}
    for edge in graph.edges:
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if (
            src is None
            or tgt is None
            or not src.off_track
            or src.is_port
            or src.id in junction_ids
            or tgt.is_port
            or tgt.id in junction_ids
            or tgt.off_track
        ):
            continue
        consumer_of.setdefault(src.id, tgt.id)

    for off_id, consumer_id in consumer_of.items():
        off_st = graph.stations.get(off_id)
        cons_st = graph.stations.get(consumer_id)
        if off_st is None or cons_st is None:
            continue
        if not (off_st.y < cons_st.y - GUARD_TOLERANCE):
            raise PhaseInvariantError(
                f"{phase}: off-track {off_id!r} y={off_st.y:.1f} "
                f"not above consumer {consumer_id!r} y={cons_st.y:.1f}"
            )


def _guard_fanout_junction_shares_exit_port_y(graph: MetroGraph, phase: str) -> None:
    """A fan-out junction fed by an LR/RL exit port must share that port's Y.

    ``_position_junctions`` anchors such a junction at the exit port's Y so the
    bundle runs straight from exit to junction.  When a late settling pass
    moves the exit port without re-running junction positioning, the junction
    is stranded above/below the port and the fanned routes dip to the stale
    junction Y and back (#386).  BOTTOM/TOP exit ports are intentionally offset
    from their junction, so only LEFT/RIGHT exits are checked.
    """
    for jid in graph.junction_ids:
        junction = graph.stations.get(jid)
        if junction is None:
            continue
        port_preds = {
            e.source
            for e in graph.edges_to(jid)
            if (src := graph.stations.get(e.source)) and src.is_port
        }
        entry_succs = {
            e.target
            for e in graph.edges_from(jid)
            if (tgt := graph.stations.get(e.target)) and tgt.is_port
        }
        if len(port_preds) != 1 or len(entry_succs) <= 1:
            continue
        exit_port = graph.stations[next(iter(port_preds))]
        port_obj = graph.ports.get(exit_port.id)
        if port_obj is None or port_obj.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        if abs(junction.y - exit_port.y) > GUARD_TOLERANCE:
            raise PhaseInvariantError(
                f"{phase}: fan-out junction {jid!r} y={junction.y:.1f} "
                f"stranded from exit port {exit_port.id!r} y={exit_port.y:.1f}"
            )


def _guard_station_x_column_drift(graph: MetroGraph, phase: str) -> None:
    """Final-phase: within each LR/RL section, stations sharing a layer
    must agree on X within one ``X_SPACING`` of the layer's median X.

    Excludes loop-side-branch stations: ``_recenter_loop_side_stations``
    deliberately moves single in/out stations whose endpoints share Y to
    the midpoint of their loop's diagonal corners, legitimately decoupling
    their X from the column grid.
    """
    import statistics

    for sec in graph.sections.values():
        if sec.bbox_h <= 0 or sec.direction not in ("LR", "RL"):
            continue
        port_ids = sec.port_ids
        layer_xs: dict[int, list[tuple[str, float]]] = {}
        for sid in sec.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.is_hidden:
                continue
            if st.off_track:
                continue
            if is_loop_side_branch_station(graph, sid):
                continue
            layer_xs.setdefault(st.layer, []).append((sid, st.x))
        for layer, members in layer_xs.items():
            if len(members) < 2:
                continue
            xs = [x for _, x in members]
            median_x = statistics.median(xs)
            for sid, x in members:
                if abs(x - median_x) > X_SPACING:
                    raise PhaseInvariantError(
                        f"{phase}: section {sec.id!r} layer {layer} "
                        f"{sid!r} x={x:.1f} drifts "
                        f"{abs(x - median_x):.1f} > X_SPACING={X_SPACING:.1f} "
                        f"from median={median_x:.1f}"
                    )


_PASS_C_BISECTION_ORDER: tuple[str, ...] = (
    "after Stage 5.2",
    "after Stage 5.3",
    "after Stage 5.4",
    "after Stage 5.5",
    "after Stage 6.1",
    "after Stage 6.2",
    "after Stage 6.3",
    "after Stage 6.4",
    "after Stage 6.5",
    "after Stage 6.6",
    "after Stage 6.9",
    "after Stage 6.10",
    "after Stage 6.11",
    "after Stage 6.12",
    "after Stage 6.13",
    "after Stage 6.14",
    "after Stage 6.15",
)
"""Ordered Pass C bisection checkpoints, used by
``_run_pass_c_guards`` to gate guards whose invariants only become
valid mid-pipeline.  Update when adding or removing a Pass C
checkpoint in ``_compute_section_layout``.
"""

# Each entry: bisection-runnable guard -> first checkpoint at which its
# invariant must hold.  Before that checkpoint, the guard is skipped in
# bisection mode; the final guard block (phase ``"after Stage 5.1
# (final)"``, which is not in ``_PASS_C_BISECTION_ORDER``) always runs it.
#
# - stations_in_sections: Stage 5.2 lifts off-track stations above their
#   section's pre-grow bbox top; Stage 5.3's row top-align grows the
#   bbox upward to enclose them.
# - no_station_overlap: Stage 6.4's snap-to-grid can place an off-track
#   terminus icon at the same coordinates as an on-track column-mate;
#   Stage 6.6's re-anchor lifts the off-track back above its consumer.
# - no_line_crosses_non_consumer: a sparse loop-side station (single
#   line in, single line out, full-bundle row-mates) sits on the trunk
#   Y until Stage 6.14 shifts it to a half-grid offset; before that,
#   the sibling line bundle's route passes through its marker bbox.
_BISECTION_FIRST_VALID: dict[str, str] = {
    "_guard_stations_in_sections": "after Stage 5.3",
    "_guard_no_station_overlap": "after Stage 6.6",
    "_guard_no_line_crosses_non_consumer": "after Stage 6.14",
}


def _bisection_should_run(guard_name: str, phase: str) -> bool:
    """True if ``guard_name`` should run at bisection checkpoint ``phase``.

    Returns True for the final guard block (``phase`` outside
    ``_PASS_C_BISECTION_ORDER``) so the final invariant set stays complete.
    """
    threshold = _BISECTION_FIRST_VALID.get(guard_name)
    if threshold is None:
        return True
    try:
        phase_idx = _PASS_C_BISECTION_ORDER.index(phase)
        threshold_idx = _PASS_C_BISECTION_ORDER.index(threshold)
        return phase_idx >= threshold_idx
    except ValueError:
        return True


def _run_pass_c_guards(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict | None = None,
    routes: list | None = None,
) -> tuple[dict, list | None]:
    """Bisection guards run after every Pass C sub-phase boundary in
    ``validate=True`` mode.

    The Pass C tidy-up pipeline is a sequence of ~20 mutating passes
    over a shared graph; before this helper, ``validate=True`` only
    sampled the final state, so a regression introduced at e.g.
    Stage 6.7 surfaced as ``after final: ...`` with no way to
    bisect.  Running the same overlap / breeze-past / column-drift
    checks at each boundary localises the culprit to a single phase.

    Guards transient through specific Pass C sub-phases are gated
    by ``_BISECTION_FIRST_VALID`` and skipped before they're valid.
    See that table for the per-guard transient windows.

    Always excluded from the bisection set (only meaningful at the
    final boundary):

    * ``_guard_off_track_inputs_above_consumer`` -- Stage 6.4's snap-
      to-grid shifts the on-track consumer Y by up to half a pitch
      before Stage 6.6 re-anchors the off-track input.
    * ``_guard_row_trunk_cy_consistent`` -- the row trunk Y is only
      finalised once Stage 6.7 has re-centred ``center_ports`` graphs.
    * ``_guard_inter_section_routes_in_row_band`` -- row-band height
      tolerance assumes final bboxes, which Stages 6.13 / 6.14 may still be
      shrinking.

    The final guard block (``after final``) composes this
    helper with the three excluded guards above, sharing
    ``offsets``/``routes`` for a single computation per checkpoint.
    Returns the computed ``(offsets, routes)`` so callers (e.g. the
    final block) can pass them on to the remaining guards.
    """
    from nf_metro.layout.routing import compute_station_offsets, route_edges

    if offsets is None:
        offsets = compute_station_offsets(graph)
    if routes is None:
        try:
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:  # noqa: BLE001 - routing failure surfaces elsewhere
            routes = None

    _guard_coordinates_finite(graph, phase)
    _guard_section_bboxes_positive(graph, phase)
    if _bisection_should_run("_guard_stations_in_sections", phase):
        _guard_stations_in_sections(graph, phase)
    _guard_ports_on_boundaries(graph, phase)
    if _bisection_should_run("_guard_no_station_overlap", phase):
        _guard_no_station_overlap(graph, phase, offsets=offsets)
    if routes is not None and _bisection_should_run(
        "_guard_no_line_crosses_non_consumer", phase
    ):
        _guard_no_line_crosses_non_consumer(
            graph, phase, offsets=offsets, routes=routes
        )
    _guard_station_x_column_drift(graph, phase)
    return offsets, routes


def _guard_no_label_overlap(graph: MetroGraph, phase: str) -> None:
    """Raise if any station label overlaps another label or a marker.

    Runs after the spread loop has settled, so it asserts the final, fully
    wrapped-and-spread state.  Label/label overlap is never tolerated;
    label/marker grazes within ``LABEL_OVERLAP_TOL`` are allowed (see
    :func:`nf_metro.layout.labels.find_label_overlaps`).
    """
    residual = _residual_label_overlaps(graph, allow_hyphenation=True)
    if not residual:
        return
    ov = residual[0]
    kind = "label" if ov.kind == "label" else "marker"
    raise PhaseInvariantError(
        f"{phase}: station label {ov.a!r} overlaps "
        f"{kind} {ov.b!r} by ({ov.ox:.1f}, {ov.oy:.1f})px after wrapping and "
        f"spreading; {len(residual)} overlap(s) total"
    )
