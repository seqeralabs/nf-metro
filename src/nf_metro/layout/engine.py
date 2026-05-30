"""Layout coordinator: combines layer assignment, ordering, and coordinate mapping.

Section-first layout: sections are laid out independently, then placed on a meta-graph.
"""

from __future__ import annotations

__all__ = ["PhaseInvariantError", "compute_layout", "compute_min_y_spacing"]

import math
import warnings
from collections import Counter, defaultdict

from nf_metro.layout.constants import (
    BYPASS_CLEARANCE,
    CANVAS_GRID_SHIFT_THRESHOLD,
    COORD_TOLERANCE,
    CURVE_RADIUS,
    DESCENDER_CLEARANCE,
    DIAGONAL_RUN,
    EDGE_TO_BUNDLE_CLEARANCE,
    ENTRY_SHIFT_LR,
    ENTRY_SHIFT_TB,
    ENTRY_SHIFT_TB_CROSS,
    EXIT_GAP_MULTIPLIER,
    FONT_HEIGHT,
    GUARD_TOLERANCE,
    ICON_CAPTION_FONT_HEIGHT,
    ICON_CAPTION_GAP,
    ICON_HALF_HEIGHT,
    ICON_INTER_GAP,
    ICON_STACK_LABEL_CLEARANCE,
    JUNCTION_MARGIN,
    LABEL_BBOX_MARGIN,
    LABEL_LINE_HEIGHT,
    LABEL_MARGIN,
    LABEL_OFFSET,
    LABEL_PAD,
    LINE_GAP,
    MAX_PORT_ALIGN_BBOX_EXPANSION_FRAC,
    MIN_PORT_STATION_GAP,
    MIN_STATION_FLAT_LENGTH,
    MIN_STRAIGHT_EDGE,
    MIN_STRAIGHT_PORT,
    MIN_Y_SPACING_FLOOR,
    OFFSET_STEP,
    ROW_GAP,
    SECTION_GAP,
    SECTION_HEADER_PROTRUSION,
    SECTION_X_GAP,
    SECTION_X_PADDING,
    SECTION_Y_GAP,
    SECTION_Y_PADDING,
    STATION_ELBOW_TOLERANCE,
    STATION_RADIUS_APPROX,
    TB_LINE_Y_OFFSET,
    TERMINUS_ICON_CLEARANCE,
    TERMINUS_ICON_CLEARANCE_V,
    TERMINUS_WIDTH,
    X_OFFSET,
    X_SPACING,
    Y_OFFSET,
    Y_SPACING,
)
from nf_metro.layout.geometry import BBoxXIndex, segment_intersects_bbox
from nf_metro.layout.labels import label_text_width
from nf_metro.layout.layers import assign_layers
from nf_metro.layout.ordering import assign_tracks
from nf_metro.parser.model import Edge, MetroGraph, Port, PortSide, Section, Station

# ---------------------------------------------------------------------------
# Stage-boundary guards
# ---------------------------------------------------------------------------

_VALIDATE_DEFAULT = False
"""Set to True to enable stage-boundary invariant checks.

Controlled by the ``validate`` parameter on ``compute_layout``.
Tests pass ``validate=True`` to catch cross-phase corruption that would
otherwise only surface as subtle visual defects.
"""


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
    junction_ids = graph.junction_ids
    tol = GUARD_TOLERANCE
    for sid, st in graph.stations.items():
        sec = graph.sections.get(st.section_id or "")
        if not sec or st.is_port or sid in junction_ids or sec.bbox_w == 0:
            continue
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


def _bbox_cols_overlap(a: Section, b: Section) -> bool:
    """True when two sections' bboxes overlap in X (share horizontal extent)."""
    return a.bbox_x < b.bbox_x + b.bbox_w and b.bbox_x < a.bbox_x + a.bbox_w


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
        target = _section_content_top_target(
            graph, section, section_y_padding, section_y_gap
        )
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

    if routes is None:
        from nf_metro.layout.routing import route_edges

        routes = route_edges(graph)

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


def first_vertical_leg_x(points) -> float | None:
    """X of the first (near-)vertical leg of *points*.

    The source-side vertical channel ("V1") of an inter-section route;
    ``None`` when no vertical leg exists.
    """
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if abs(x1 - x0) < 1.0 and abs(y1 - y0) > 1.0:
            return x1
    return None


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
    from nf_metro.layout.routing.core import _build_routing_context

    if offsets is None:
        offsets = compute_station_offsets(graph)
    if routes is None:
        routes = route_edges(graph, station_offsets=offsets)
    ctx = _build_routing_context(graph, DIAGONAL_RUN, CURVE_RADIUS, offsets)
    fan_sources = {key[0] for key in ctx.junction_fan_info}
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


def _canvas_width(graph: MetroGraph) -> float:
    """Horizontal extent of all positioned sections (rightmost - leftmost)."""
    rights = [s.bbox_x + s.bbox_w for s in graph.sections.values() if s.bbox_w > 0]
    lefts = [s.bbox_x for s in graph.sections.values() if s.bbox_w > 0]
    if not rights or not lefts:
        return 0.0
    return max(rights) - min(lefts)


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
    if routes is None:
        from nf_metro.layout.routing import route_edges

        routes = route_edges(graph)

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
    if routes is None:
        from nf_metro.layout.routing import route_edges

        routes = route_edges(graph)

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

    if routes is None:
        from nf_metro.layout.routing import route_edges

        routes = route_edges(graph)

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

    if routes is None:
        from nf_metro.layout.routing import route_edges

        routes = route_edges(graph)

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

    if routes is None:
        from nf_metro.layout.routing import route_edges

        routes = route_edges(graph)

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


def compute_min_y_spacing(
    graph: MetroGraph, floor: float = MIN_Y_SPACING_FLOOR
) -> float:
    """Return the minimum global ``y_spacing`` the graph's content needs.

    Scans every LR/RL section and asks, for any pair of stations that
    could land in vertically-adjacent grid slots in the same column:
    what centre-to-centre pitch is needed for their labels / captioned
    file icons not to collide?

    The four worst-case vertical extents considered are:

    * captioned file-icon below the marker: ``ICON_HALF_HEIGHT +
      ICON_CAPTION_GAP + ICON_CAPTION_FONT_HEIGHT``
    * captioned file-icon above the marker: ``ICON_HALF_HEIGHT``
    * labeled station, label below: ``LABEL_OFFSET + FONT_HEIGHT +
      DESCENDER_CLEARANCE``
    * labeled station, label above: ``LABEL_OFFSET + FONT_HEIGHT +
      DESCENDER_CLEARANCE``

    Required pitch for two stacked elements is
    ``upper.below_extent + lower.above_extent +
    ICON_STACK_LABEL_CLEARANCE``.  We take the worst case across all
    candidate pairs in every LR/RL section, then clamp to ``floor`` so
    a label-light graph stays at the historical default pitch.

    Label-only stations alternate above/below within a column at the
    default pitch, so they're not the binding constraint on their own.
    Captioned file icons can't alternate (caption placement is fixed
    under the icon), so the widening fires when icons enter the mix.

    The result is applied uniformly to the whole render -- the grid
    stays global, no per-section overrides.
    """
    icon_below = ICON_HALF_HEIGHT + ICON_CAPTION_GAP + ICON_CAPTION_FONT_HEIGHT
    icon_above = ICON_HALF_HEIGHT
    label_extent = LABEL_OFFSET + FONT_HEIGHT + DESCENDER_CLEARANCE
    clearance = ICON_STACK_LABEL_CLEARANCE

    pitch_icon_icon = icon_above + icon_below + clearance
    # icon_over_label uses icon_below (the larger extent), so it
    # subsumes the label-over-icon case which uses icon_above.
    pitch_icon_over_label = icon_below + label_extent + clearance

    required = floor
    if not graph.sections:
        return required

    for section in graph.sections.values():
        if section.direction not in ("LR", "RL"):
            continue
        captioned = 0
        labeled = 0
        for sid in section.station_ids:
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.is_hidden:
                continue
            has_caption = st.is_terminus and any(
                bool(n) for n in (st.terminus_names or [])
            )
            has_label = bool(st.label) and not st.is_terminus
            if has_caption:
                captioned += 1
            elif has_label:
                labeled += 1
        if captioned >= 2:
            required = max(required, pitch_icon_icon)
        if captioned >= 1 and labeled >= 1:
            required = max(required, pitch_icon_over_label)

    return required


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


def compute_layout(
    graph: MetroGraph,
    x_spacing: float | None = None,
    y_spacing: float | None = None,
    x_offset: float = X_OFFSET,
    y_offset: float = Y_OFFSET,
    row_gap: float = ROW_GAP,
    section_gap: float = SECTION_GAP,
    section_x_padding: float = SECTION_X_PADDING,
    section_y_padding: float = SECTION_Y_PADDING,
    section_x_gap: float = SECTION_X_GAP,
    section_y_gap: float = SECTION_Y_GAP,
    validate: bool = _VALIDATE_DEFAULT,
) -> None:
    """Compute layout positions for all stations in the graph.

    When ``y_spacing`` is ``None`` (the default) it is derived from the
    graph's content via ``compute_min_y_spacing`` so renders adapt to
    captioned icons and labelled stations automatically.  Pass an
    explicit numeric value to override.

    If the explicit value is below the minimum the content needs, a
    ``UserWarning`` is emitted: the render is honoured at the requested
    pitch, but labels and captioned file-icons may collide.  Omit
    ``y_spacing`` to let the engine pick a safe value.

    When *validate* is True, stage-boundary invariant checks run after
    key phases.  Violations raise ``PhaseInvariantError`` instead of
    silently producing broken layouts.
    """
    auto_x = x_spacing is None
    auto_y = y_spacing is None
    if auto_y:
        y_spacing = compute_min_y_spacing(graph)
    else:
        min_required = compute_min_y_spacing(graph)
        if y_spacing < min_required - 1e-6:
            warnings.warn(
                f"explicit y_spacing={y_spacing!r} is below the minimum "
                f"({min_required:.1f}) this graph's content requires; "
                f"labels and captioned file-icons may collide. "
                f"Omit --y-spacing to let the engine pick a safe value.",
                UserWarning,
                stacklevel=2,
            )
    if auto_x:
        x_spacing = X_SPACING

    # Optionally reorder lines by section span before layout.
    # Must happen here (on the full graph) before section subgraphs are
    # built, since subgraphs share graph.lines via reference.  Done once;
    # the reorder is order-stable across the spread loop below.
    if graph.line_order == "span" and graph.lines:
        from nf_metro.layout.ordering import _reorder_by_span

        new_order = _reorder_by_span(graph, list(graph.lines.keys()))
        graph.lines = {lid: graph.lines[lid] for lid in new_order}

    # Spread loop: lay out, then if labels still collide at this pitch
    # (after wrapping has done what it can), widen the auto-resolved
    # spacing and lay out again.  A clean layout clears on the first pass
    # so nothing is widened; only crowded wide-label graphs iterate.  When
    # the caller pins both spacings explicitly there is nothing to widen,
    # so a single pass runs.
    max_iters = _MAX_SPREAD_ITERS if (auto_x or auto_y) else 1
    for attempt in range(max_iters):
        _layout_once(
            graph,
            x_spacing=x_spacing,
            y_spacing=y_spacing,
            x_offset=x_offset,
            y_offset=y_offset,
            section_x_padding=section_x_padding,
            section_y_padding=section_y_padding,
            section_x_gap=section_x_gap,
            section_y_gap=section_y_gap,
            validate=validate,
        )
        if attempt == max_iters - 1:
            break
        residual = _residual_label_overlaps(graph, allow_hyphenation=False)
        if not residual:
            break
        new_x, new_y = _spread_bump(
            graph, residual, x_spacing, y_spacing, auto_x, auto_y
        )
        if new_x <= x_spacing + 1e-6 and new_y <= y_spacing + 1e-6:
            break  # can't widen the binding axis (e.g. pinned) -- give up
        x_spacing, y_spacing = new_x, new_y

    if validate:
        _guard_no_label_overlap(graph, "final")


def _layout_once(
    graph: MetroGraph,
    *,
    x_spacing: float,
    y_spacing: float,
    x_offset: float,
    y_offset: float,
    section_x_padding: float,
    section_y_padding: float,
    section_x_gap: float,
    section_y_gap: float,
    validate: bool,
) -> None:
    """Run one full positioning pass at the given spacing (idempotent)."""
    if not graph.sections:
        _compute_flat_layout(
            graph,
            x_spacing=x_spacing,
            y_spacing=y_spacing,
            x_offset=x_offset,
            y_offset=y_offset,
        )
        return

    _compute_section_layout(
        graph,
        x_spacing=x_spacing,
        y_spacing=y_spacing,
        x_offset=x_offset,
        y_offset=y_offset,
        section_x_padding=section_x_padding,
        section_y_padding=section_y_padding,
        section_x_gap=section_x_gap,
        section_y_gap=section_y_gap,
        validate=validate,
    )


def _compute_flat_layout(
    graph: MetroGraph,
    x_spacing: float = X_SPACING,
    y_spacing: float = Y_SPACING,
    x_offset: float = X_OFFSET,
    y_offset: float = Y_OFFSET,
) -> None:
    """Flat layout for sectionless pipelines.

    Runs layer/track assignment directly on the full graph and maps
    to coordinates without section boxes or port routing.
    """
    layers = assign_layers(graph)
    tracks = assign_tracks(graph, layers)

    if not layers:
        return

    # When tracks is empty (e.g. no named lines), default all to track 0.
    if not tracks:
        tracks = {sid: 0 for sid in layers}

    unique_tracks = sorted(set(tracks.values()))
    track_rank = {t: i for i, t in enumerate(unique_tracks)}

    layer_extra = _compute_fork_join_gaps(graph, layers, tracks, x_spacing)

    for sid, station in graph.stations.items():
        station.layer = layers.get(sid, 0)
        station.track = tracks.get(sid, 0)
        station.x = (
            x_offset + station.layer * x_spacing + layer_extra.get(station.layer, 0)
        )
        station.y = y_offset + track_rank[station.track] * y_spacing


def _compute_section_layout(
    graph: MetroGraph,
    x_spacing: float = X_SPACING,
    y_spacing: float = Y_SPACING,
    x_offset: float = X_OFFSET,
    y_offset: float = Y_OFFSET,
    section_x_padding: float = SECTION_X_PADDING,
    section_y_padding: float = SECTION_Y_PADDING,
    section_x_gap: float = SECTION_X_GAP,
    section_y_gap: float = SECTION_Y_GAP,
    validate: bool = False,
) -> None:
    """Section-first layout pipeline.

    Quick map of the pipeline's structure.  See ``CONTRACT.md`` for the
    full per-sub-stage table with pre/postconditions and invariant
    coverage, and ``docs/dev/layout_pipeline.md`` for the human-facing
    overview.

    Parsing & partition is already done by the parser.  Six stages
    follow:

    1. **Section construction** (Stages 1.1 to 1.5).  Lay out each
       section internally, snap row Y grids, place on the canvas grid,
       renumber by reading order, correct left/top overshoot.  Coords
       stay local.
    2. **Globalise** (Stage 2.1).  Single-stage coord-regime
       transition: translate stations and bboxes to canvas coordinates.
    3. **Pass A - port positioning** (Stages 3.1 to 3.5).  Place ports
       on bbox edges, align entry ports, shift LR/RL perp-entry
       stations, align fold-section exit ports, top-align rows.
    4. **Pass B - downstream alignment & trunk-Y consolidation**
       (Stages 4.1 to 4.10).  Pull ports toward downstream content,
       snap to grid-group stations, space from termini, recompute
       bboxes, align trunk Ys, redistribute fan-out and full-bundle
       columns.
    5. **Pass C - junctions & off-track lift** (Stages 5.1 to 5.5).
       Position junctions, lift off-track stations, re-align row bbox
       tops, compact, snap inter-section port pairs.
    6. **Pass C - vertical settling & finishing** (Stages 6.1 to 6.15).
       Fan content upward, snap to grid, re-anchor off-track, recenter
       full-bundle columns and restore their invariants, balance content
       around trunk, loop-side X recenter, bbox shrink + row tighten /
       push, captioned-icon pad.

    Inline ``# ---- Stage N - ... ----`` dividers below mark each
    stage's start; ``# Stage X.Y:`` comments above each helper call
    name the sub-stage.
    """
    from nf_metro.layout.section_placement import place_sections, position_ports

    # ---- Stage 1 - Section construction (local coords) ------------------
    # Lay out each section internally, snap row Y grids, place sections on
    # the canvas grid, renumber by reading order, correct left/top
    # overshoot.  All work in section-local coordinates.

    # Stage 1.1: Lay out each section independently (real stations only, no ports)
    section_subgraphs: dict[str, MetroGraph] = {}
    for sec_id, section in graph.sections.items():
        sub = _layout_single_section(
            graph, section, x_spacing, y_spacing, section_x_padding, section_y_padding
        )
        if sub is not None:
            section_subgraphs[sec_id] = sub

    if validate:
        _guard_section_bboxes_positive(graph, "after Stage 1.1")
        _guard_no_negative_grid_columns(graph, "after Stage 1.1")

    # Stage 1.2: Align Y grids across same-row, same-direction sections
    _align_row_y_grids(graph, section_subgraphs, y_spacing, section_y_padding)

    # Stage 1.3: Place sections on the canvas
    place_sections(graph, section_x_gap, section_y_gap)

    # Stage 1.4: Renumber sections by visual reading order (row, col)
    _renumber_sections_by_grid(graph)

    # Stage 1.5: Adapt x/y_offset for left/top overshoot.
    # Section bboxes extend left of the local origin by at least
    # section_x_padding; x_offset normally absorbs this with margin to
    # spare (standard margin = x_offset - section_x_padding).  When
    # terminus-icon clearance expands bbox_x far enough that
    # offset_x + bbox_x + x_offset < 0, content clips off the canvas.
    # Increase x_offset to restore the standard margin and let the canvas
    # grow on the right (via auto_width = max_x + CANVAS_PADDING in
    # render).  Same logic for y_offset.
    local_lefts = [
        section.offset_x + section.bbox_x
        for section in graph.sections.values()
        if section.bbox_w > 0
    ]
    if local_lefts:
        min_local_left = min(local_lefts)
        global_left = min_local_left + x_offset
        if global_left < 0:
            standard_margin = x_offset - section_x_padding
            x_offset += standard_margin - global_left

    local_tops = [
        section.offset_y + section.bbox_y
        for section in graph.sections.values()
        if section.bbox_h > 0
    ]
    if local_tops:
        min_local_top = min(local_tops)
        global_top = min_local_top + y_offset
        if global_top < 0:
            standard_margin = y_offset - section_y_padding
            y_offset += standard_margin - global_top

    # ---- Stage 2 - Globalise (local -> global coords) ------------------
    # The coord-regime transition.  Owns the post-Stage-2.1 guard
    # checkpoint (finite coords, stations-in-sections, bboxes-positive).

    # Stage 2.1: Translate local coords to global coords (real stations)
    for sec_id, section in graph.sections.items():
        sub = section_subgraphs.get(sec_id)
        if not sub:
            continue

        for sid, local_station in sub.stations.items():
            if sid in graph.stations:
                graph.stations[sid].layer = local_station.layer
                graph.stations[sid].track = local_station.track
                graph.stations[sid].x = local_station.x + section.offset_x + x_offset
                graph.stations[sid].y = local_station.y + section.offset_y + y_offset

        # Update section bbox to global coords
        section.bbox_x += section.offset_x + x_offset
        section.bbox_y += section.offset_y + y_offset

    if validate:
        _guard_coordinates_finite(graph, "after Stage 2.1")
        _guard_stations_in_sections(graph, "after Stage 2.1")
        _guard_section_bboxes_positive(graph, "after Stage 2.1")

    # ---- Stage 3 - Pass A: Port initialisation & section geometry --------
    # Position ports on bbox edges, align entry ports, shift internal
    # stations for perp entries, align fold exits, then top-align.
    # Top-align runs last so it corrects any bbox shifts from fold-exit
    # alignment.

    # Stage 3.1: Position ports on section boundaries (after bbox is in global coords)
    for sec_id, section in graph.sections.items():
        position_ports(section, graph)

    if validate:
        _guard_ports_on_boundaries(graph, "after Stage 3.1")

    # Stage 3.2: Align LEFT/RIGHT entry ports with their incoming
    # connection's Y so inter-section horizontal runs are straight.
    # Uses _resolve_source_xy() to derive junction coordinates
    # on-the-fly, removing the dependency on pre-positioned junctions.
    _align_entry_ports(graph)

    # Stage 3.3: Shift internal stations in LR/RL sections with
    # perpendicular (TOP/BOTTOM) entry away from the port.  Needs the
    # aligned port X from Stage 3.2; only moves internal station X, not
    # ports or bboxes.
    _shift_lr_perp_entry_stations(graph, x_spacing)

    # Stage 3.4: Align LEFT/RIGHT exit ports on row-spanning (fold)
    # sections with their target's Y so the exit is at the return row.
    # May push target sections down (via _resolve_tb_exit_y), which
    # top-align in the next step corrects.
    _align_exit_ports(graph)

    # Stage 3.5: Top-align sections within each grid row.
    # Runs after fold-exit alignment so it corrects any bbox_y shifts
    # from Stage 3.4's target-section push.  Same-row port pairs shift
    # by the same delta, preserving entry-port alignment.
    _top_align_row_sections(graph)

    if validate:
        _guard_ports_on_boundaries(graph, "after top-align")

    # ---- Stage 4 - Pass B: Downstream alignment & trunk-Y consolidation -
    # Stage 4.5's port-terminus spacing can expand bboxes via
    # ``_expand_bbox_for_y``; Stage 4.7 re-runs row top-align to undo
    # the resulting bbox-top drift.  Stages 4.9 / 4.10 run only on
    # ``center_ports`` graphs.

    # Stage 4.1: For non-fold LR/RL sections, pull exit-entry port pairs
    # toward the downstream section's stations so lines flow directly.
    _align_ports_to_downstream(graph)

    # Stage 4.2: When a port-connected station is the sole occupant of its
    # layer, snap it to the port Y so the connection is horizontal.
    _snap_sole_layer_stations_to_ports(graph)

    # Stage 4.3: For grid-group sections (where Stage 4.2 is skipped), snap
    # entry ports to the Y of their first connected internal station.
    # This produces a straight horizontal port-to-station connection
    # instead of a diagonal from the upstream junction Y.
    _snap_grid_group_entry_ports(graph)

    # Stage 4.4: Mirror of Stage 4.3 for exit ports.  Move exit ports of
    # grid-group sections to the Y of the downstream entry port (which
    # Stage 4.3 already snapped to a grid station).  This eliminates detours
    # where lines leave at the section midpoint then route back.
    _snap_grid_group_exit_ports(graph)

    # Stage 4.5: Ensure ports maintain at least y_spacing from terminus
    # stations in their section so file icons don't overlap routed lines.
    _space_ports_from_termini(graph, y_spacing)

    # Stage 4.6: Recompute bboxes for grid-aligned sections.  Earlier
    # stages (3.2, 3.4, 4.5) may have expanded bboxes for temporary port
    # positions that were later corrected (e.g. Stage 4.1 pulls ports
    # back toward downstream stations).  Recompute with symmetric
    # padding around the final non-port station range.
    _recompute_grid_group_bboxes(graph)

    # Stage 4.7: Re-run top-align after Stage 4.5 may have shifted
    # individual section bbox_y values (via _expand_bbox_for_y) so
    # bbox tops within each row stay flush after port-terminus spacing.
    _top_align_row_sections(graph)

    # Stage 4.8: Align trunk Ys across same-row sections.  Shifts
    # content downward in shallower sections so the inter-section bundle
    # passes through at a single Y per row.  Bbox tops are preserved.
    _align_row_trunk_ys(graph)

    # Stage 4.9: When --center-ports is on, redistribute fan-out siblings
    # of a section's trunk junction symmetrically around the trunk Y.
    # Scoped to fan-out side branches only: linear chains, fan-in
    # structures, and file inputs are left in place.
    _redistribute_fanout_siblings(graph, y_spacing)

    # Stage 4.10: Symmetrically fan a column of full-bundle stations
    # around the trunk Y when no unique trunk exists (e.g. Reporting's
    # Shiny app + Quarto report, both carrying the full bundle).
    #
    # Why both Stage 4.10 and Stage 6.7's recenter: Stage 4.10's
    # symmetric layout is read by Stage 5.2's bbox-growth, compaction,
    # and snap-to-grid passes (an empty trunk row in fanned columns
    # lets Stages 5.4 / 6.13 shrink the section bbox to the compact
    # extent).  Stage 6.7 then re-fans the same columns using the
    # post-row-alignment trunk Y, which can have drifted from Stage
    # 4.10's port-Y anchor.  Skipping Stage 4.10 changes intermediate
    # bbox sizes and is not empty-render-diff; the two passes are
    # load-bearing in combination.
    _redistribute_full_bundle_columns(graph, y_spacing)

    # ---- Stage 5 - Pass C: Junctions & off-track lift ------------------
    # All port positions are now final; Stage 5.1 positions junctions
    # once.  Stage 5.2 lifts off-track stations; Stages 5.3 to 5.5
    # then re-align row bbox tops, compact, and re-snap inter-section
    # port pairs (re-running Stage 5.1 for the junctions that move
    # with them).  Stage 6 below handles the rest of Pass C.

    # Stage 5.1: Position junction stations in the inter-section gap.
    _position_junctions(graph)

    # Stage 5.2: Lift off_track stations above their section's top track.
    # Runs last so it operates on finalised station Ys and bboxes.
    _lift_off_track_stations(graph, y_spacing, section_y_padding)
    # The upward bbox growth above can push the topmost section above
    # the canvas top margin set by Stage 1.5; shift the whole graph
    # down to restore the margin.  No-op when no section overflowed.
    _shift_graph_into_canvas(graph, section_y_padding)
    if validate:
        _run_pass_c_guards(graph, "after Stage 5.2")

    # Stage 5.3: Re-align bbox tops within each grid row after off-track
    # lifting expanded some sections upward.  Unlike Stages 3.5 / 4.7 which
    # shifts stations with the bbox, this only grows the bbox upward so
    # the empty input-band space lines up across the row.  Station Ys
    # in unlifted sections are preserved.
    _top_align_row_bboxes_only(graph)
    if validate:
        _run_pass_c_guards(graph, "after Stage 5.3")

    # Stage 5.4: Compact row-mate sections so content sits just inside
    # the bbox top edge.  Shifts an entire row's column group up by the
    # smallest above-content slack, preserving trunk alignment.  Bbox
    # heights shrink correspondingly so the empty top space disappears.
    _compact_row_content_to_bbox_top(graph, section_y_padding, y_spacing)
    if validate:
        _run_pass_c_guards(graph, "after Stage 5.4")

    # Stage 5.5: Snap inter-section LR/RL port pairs to a common Y so
    # the trunk bundle stays perfectly horizontal across boundaries.
    # Picks the downstream entry port's Y as the anchor since it sits
    # on the row's aligned trunk grid.  Junctions are re-positioned
    # afterwards: Stage 5.1 fixed their Y to the pre-compaction exit
    # port Y, and either the snap pass or ``_compact_row_content_to_bbox_top``
    # may have moved the exit port since then.
    _snap_inter_section_port_pairs(graph)
    _position_junctions(graph)
    if validate:
        _run_pass_c_guards(graph, "after Stage 5.5")

    # ---- Stage 6 - Pass C: Vertical settling & finishing ---------------
    # The long settle: fan free / source content upward, half-grid
    # symfan, snap to grid, bbox-bottom and off-track-reanchor
    # post-snap fixups, full-bundle recenter + its invariant-restore
    # sub-phases, terminus pin / auto-balance, loop-side X recenter,
    # bbox shrink + row tighten / push, captioned-icon pad.  Most
    # phases here run unconditionally; a few are gated on
    # ``center_ports`` or on a specific topology (see each comment).

    # Stage 6.1: Fan a section's free content upward when the row's
    # compaction left visible empty space at the bbox top.  Only fires
    # for sections whose internal stations have no upward dependency
    # (no off-track band) and whose trunk Y sits below the bbox top
    # padding by more than one ``y_spacing`` slot.
    _fan_free_content_upward(graph, section_y_padding, y_spacing)
    if validate:
        _run_pass_c_guards(graph, "after Stage 6.1")

    # Stage 6.2: Companion to Stage 6.1 for source-stack sections.  When the
    # entry column has a single full-bundle trunk plus subset-bundle
    # source inputs (file icons with no inbound edges), lift the
    # nearest-to-trunk sources into the empty top band so the section
    # is bottom- and top-weighted instead of stacked below the trunk.
    _fan_source_inputs_upward(graph, y_spacing)
    if validate:
        _run_pass_c_guards(graph, "after Stage 6.2")

    # Stage 6.3: For sections that contain exactly a 2-branch
    # symmetric fan (and no off-track or other constraining content),
    # collapse the fan onto half-pitch offsets so the section consumes
    # one vertical grid unit instead of two.  Marks the branch stations
    # in ``graph.half_grid_station_ids`` so the next snap pass leaves
    # them alone.  Runs before ``_snap_all_y_to_grid`` so the snap-to-
    # row-grid pass doesn't immediately undo the compaction.
    if graph.center_ports:
        _apply_half_grid_2branch_symfan(graph, y_spacing, section_y_padding)
        if validate:
            _run_pass_c_guards(graph, "after Stage 6.3")

    # Stage 6.4: Snap all station/port Ys to a per-section y_spacing
    # grid.  Trunk-Y align, port-snap, and the row compaction/fan
    # phases compute shifts that don't respect the grid pitch, leaving
    # coordinates at fractional Ys (e.g. 298.785 when the pitch is 55).
    # This final pass restores clean grid positions before validation.
    # Junctions sit in the inter-section gap with no section_id and are
    # skipped by the snap pass; re-running _position_junctions after the
    # snap re-anchors them to the moved exit ports so the L-shape route
    # doesn't U-turn through a stale junction Y.
    _snap_all_y_to_grid(graph, y_spacing)
    _position_junctions(graph)
    if validate:
        _run_pass_c_guards(graph, "after Stage 6.4")

    # Stage 6.5: Grow TB-section bbox bottoms so they align with the
    # downstream LR section's bbox bottom.  Without this the TB
    # section's bbox ends right at the inter-section exit port Y,
    # making the line look pinned to the section edge.
    _align_tb_section_bbox_bottoms(graph)
    if validate:
        _run_pass_c_guards(graph, "after Stage 6.5")

    # Stage 6.6: Re-anchor off-track inputs to their consumer's final
    # (snapped) Y.  Stage 5.2's lift placed them relative to pre-snap
    # consumer Ys; snapping the consumer to the grid can shift it by
    # up to half a pitch, which would collapse the y_spacing gap above
    # off-track.  Recomputing here pins each off-track at
    # consumer.y - n*y_spacing on the final grid and grows the bbox
    # upward if the new position rises above the padding zone.
    _reanchor_off_track_to_consumer(graph, y_spacing, section_y_padding)
    # Same canvas-fit safeguard as after Stage 5.2: a reanchor-driven
    # bbox grow can push the topmost section above the canvas top.
    _shift_graph_into_canvas(graph, section_y_padding)
    if validate:
        _run_pass_c_guards(graph, "after Stage 6.6")

    # Stage 6.7: Re-center full-bundle columns around the row's final
    # trunk Y.  ``_redistribute_full_bundle_columns`` runs early when
    # only local port Ys are available; for terminal sections whose
    # port Y differs from the row's eventual trunk Y, the symmetric
    # fan ends up offset from the trunk row (e.g. Reporting's Shiny at
    # the trunk row, Quarto two slots below, instead of one above and
    # one below).  This re-center uses the final inter-section bundle
    # Y as the anchor so the trunk row stays empty in each fanned
    # column.
    #
    # Stage 6.8 and Stage 6.9 below restore invariants the recenter
    # breaks.
    if graph.center_ports:
        _recenter_full_bundle_columns(graph, y_spacing)

        # Stage 6.8: Re-anchor off-track inputs after the recenter.
        # The recenter moves consumers to the final trunk-anchored Y,
        # which can leave the off-track icon stranded at the old
        # consumer Y (overlapping the consumer station instead of
        # sitting one row above it).  Uses each consumer's post-
        # recenter Y as the new anchor and grows the section bbox
        # upward when the lifted band moves above its current top.
        _reanchor_off_track_to_consumer(graph, y_spacing, section_y_padding)
        # Same canvas-fit safeguard as Stage 5.2 / Stage 6.6: a
        # reanchor-driven bbox grow can push the topmost section
        # above the canvas top.
        _shift_graph_into_canvas(graph, section_y_padding)

        # Stage 6.9: Re-run row top-align.  A Stage 6.8 reanchor-
        # driven bbox grow leaves the section's bbox above its row
        # mates'; pull row mates' bbox tops up to match so the section
        # row stays flush along its top edge.
        _top_align_row_bboxes_only(graph)
        if validate:
            _run_pass_c_guards(graph, "after Stage 6.9")

    # Stage 6.10: After fan-re-centering, single-station downstream
    # columns (e.g. terminus file icons) may have stayed at their
    # pre-fan Y while their sole upstream moved to the trunk.  Pin
    # them back onto the source Y so the connection stays horizontal.
    _align_terminus_to_upstream(graph)
    if validate:
        _run_pass_c_guards(graph, "after Stage 6.10")

    # Stage 6.11: Auto-balance pass.  For sections whose final layout
    # still leaves an empty band above the trunk while more siblings
    # sit below than above, lift bottommost movable siblings into the
    # empty top band so content sits symmetrically around the trunk.
    # Runs after re-centering and terminus-Y pinning so it sees the
    # final trunk Y.  U-turn-safe and bbox-bounded.
    _balance_section_content_around_trunk(graph, section_y_padding, y_spacing)
    if validate:
        _run_pass_c_guards(graph, "after Stage 6.11")

    # Stage 6.12: Recenter fan-out side stations on their loop midpoint.
    # The layer-based X assignment places off-trunk siblings (e.g. propd,
    # dream, DESeq2 fanned off limma between section entry and annotate
    # results) at a fixed offset from the section entry that ignores how
    # far the join's diagonal-back corner reaches.  Asymmetric corners
    # leave the station visibly off-centre on its horizontal loop run.
    # Reposition each side station to the midpoint of the two diagonal
    # corner Xs derived from the actual routing geometry.
    _recenter_loop_side_stations(graph)
    if validate:
        _run_pass_c_guards(graph, "after Stage 6.12")

    # Stage 6.13: Shrink rowspan / row-mate bboxes whose content moved
    # up after compact (e.g. ``_fan_source_inputs_upward`` lifted the
    # bottom rows away from the bbox bottom), then pull lower rows up
    # to close the slack the shrink revealed.  Bottom-only shrink, so
    # trunk alignment is unaffected; tighten only fires where a rowspan
    # section's content fell short of its row claim.  Junctions live
    # in inter-section space and aren't moved by the tighten pass, so
    # re-run ``_position_junctions`` afterwards to re-anchor them to
    # the now-shifted exit/entry port Ys (otherwise the trunk dips to
    # the junction's pre-shift Y and produces an S-kink).
    _shrink_and_tighten_rows(graph, section_y_padding, section_y_gap)
    _position_junctions(graph)
    if validate:
        _run_pass_c_guards(graph, "after Stage 6.13")

    # Stage 6.14: Shift sparse loop-side stations (e.g. ``grea`` -- one
    # incoming, one outgoing, single-line consumer) onto a half-grid Y
    # when sharing the full-row Y with a busier sibling whose inbound
    # bundle would otherwise cross the sparse station's marker bbox.
    # When the shift grows a section's bbox downward, the helper also
    # pushes lower-row sections down internally to restore
    # ``section_y_gap``.
    _shift_and_propagate_loop_stations(
        graph, y_spacing, section_y_padding, section_y_gap
    )
    # The shift can move an exit port off the Y it held when junctions were
    # last positioned (Stage 6.13).  Re-anchor junctions to the settled port
    # Ys so a fan-out bundle runs straight from exit to junction instead of
    # dipping to a stale junction Y and back (#386).
    _position_junctions(graph)
    if validate:
        _run_pass_c_guards(graph, "after Stage 6.14")

    # Stage 6.15a: Restore top padding symmetric with the bottom.  Fan
    # re-distribution (Stages 4.9 / 4.10 / 6.7 / 6.11) can lift a branch
    # above the content-top line the bbox was sized for, crowding the
    # topmost marker against the bbox top while the bottom keeps its full
    # band.  Grow each bbox top to a full ``section_y_padding`` above the
    # highest marker (bounded by the row above) so content fanning above
    # the trunk sits centred in its box.  The upward growth can push the
    # topmost section above the canvas top margin, so re-fit; the
    # re-fit's non-grid shift is then cleaned up by the Stage 6.15
    # canvas snap below.
    _grow_bboxes_to_content_top(graph, section_y_padding, section_y_gap)
    _shift_graph_into_canvas(graph, section_y_padding)

    # Stage 6.15: Restore canvas-wide grid alignment after all settling.
    # Stage 6.4 snaps to a per-row grid; later helpers (notably
    # ``_shift_graph_into_canvas`` shifting by ``section_y_padding -
    # min_top``, which is not a multiple of ``y_spacing`` when padding
    # is not a grid multiple) can introduce a uniform half-grid drift.
    # When every real station shares a single non-zero residue, shift
    # the whole canvas by the smallest signed amount that returns them
    # to integer multiples of ``y_spacing``.  No-op when residues are
    # mixed.
    _snap_canvas_y_to_grid(graph, y_spacing, section_y_padding)
    if validate:
        _run_pass_c_guards(graph, "after Stage 6.15")

    # Stage 6.16: Re-align LEFT/RIGHT entry ports with their feeders.  A
    # TB section's perpendicular entry port is pinned a fixed offset above
    # its first internal station, so the late vertical settling (Stages
    # 6.13-6.15) that shifts the section's content also drags the entry
    # port off the upstream feeder Y it was snapped to in Stage 3.2,
    # re-introducing an inter-section S-kink.  Re-running the alignment
    # (TB/BT sections only, to leave settled LR/RL geometry untouched)
    # re-snaps the port to its now-settled feeder.  Junctions are
    # re-anchored afterwards for the same reason as Stages 6.13/6.14.
    _align_entry_ports(graph, tb_only=True)
    _position_junctions(graph)
    if validate:
        _run_pass_c_guards(graph, "after Stage 6.16")

    if validate:
        phase = "after final"
        offsets, routes = _run_pass_c_guards(graph, phase)
        _guard_row_trunk_cy_consistent(graph, phase, offsets=offsets)
        _guard_off_track_inputs_above_consumer(graph, phase)
        _guard_fanout_junction_shares_exit_port_y(graph, phase)
        _guard_merge_port_approach_side(graph, phase, offsets=offsets)
        _guard_partial_branch_offset_gaps(graph, phase, offsets=offsets)
        _guard_row_gaps(graph, phase, section_y_gap=section_y_gap)
        _guard_section_top_padding(
            graph,
            phase,
            section_y_padding=section_y_padding,
            section_y_gap=section_y_gap,
        )
        _guard_terminus_icons_within_bbox(graph, phase)
        if routes is not None:
            _guard_inter_section_routes_in_row_band(
                graph, phase, offsets=offsets, routes=routes
            )
            _guard_bundle_order_preserved(graph, phase, offsets=offsets, routes=routes)
            _guard_fanout_tail_join(graph, phase, offsets=offsets, routes=routes)
            _guard_inter_section_route_no_backtrack(graph, phase, routes=routes)
            _guard_inter_section_route_no_full_width_backtrack(
                graph, phase, routes=routes
            )
            _guard_routes_enter_sections_at_ports(graph, phase, routes=routes)
            _guard_serpentine_no_backtrack(graph, phase, routes=routes)
            _guard_inter_row_run_clearance(graph, phase, routes=routes)
            _guard_inter_section_descent_edge_clearance(graph, phase, routes=routes)
            _guard_fan_bundles_coincide_or_separate(
                graph, phase, offsets=offsets, routes=routes
            )


def _renumber_sections_by_grid(graph: MetroGraph) -> None:
    """Renumber sections by visual reading order.

    Groups sections into flow sweeps separated by fold boundaries:
    each left-to-right (or right-to-left) run is one sweep, with
    TB fold sections belonging to the sweep they terminate.  Within
    each sweep, sections are numbered by (grid_col, grid_row) so
    columns go left-to-right and stacked sections go top-to-bottom.
    All numbers in sweep N+1 are greater than those in sweep N.
    """
    from collections import deque

    import networkx as nx

    dag = nx.DiGraph()
    for sid in graph.sections:
        dag.add_node(sid)
    if graph.section_dag:
        for src, tgt in graph.section_dag.section_edges:
            if src in graph.sections and tgt in graph.sections:
                dag.add_edge(src, tgt)

    secs = graph.sections

    def _is_direction_change(src: str, tgt: str) -> bool:
        """True when flow direction reverses between two sections."""
        sd, td = secs[src].direction, secs[tgt].direction
        # TB->LR/RL: only counts if the TB's predecessors flowed
        # the opposite way (i.e. TB is a fold boundary).
        if sd == "TB" and td in ("LR", "RL"):
            for pred in dag.predecessors(src):
                pd = secs[pred].direction
                if pd in ("LR", "RL") and pd != td:
                    return True
            return False
        if sd in ("LR", "RL") and td in ("LR", "RL") and sd != td:
            return True
        return False

    sweep: dict[str, int] = {}
    roots = [n for n in dag.nodes() if dag.in_degree(n) == 0]
    q: deque[str] = deque()
    for r in roots:
        sweep[r] = 0
        q.append(r)

    while q:
        node = q.popleft()
        for succ in dag.successors(node):
            new_depth = sweep[node]
            if _is_direction_change(node, succ):
                new_depth = sweep[node] + 1
            if succ not in sweep or new_depth < sweep[succ]:
                sweep[succ] = new_depth
                q.append(succ)

    for sid in graph.sections:
        if sid not in sweep:
            sweep[sid] = 0

    # Disconnected components: number each flow fully before the next,
    # ordered by the root's grid_row so top flows come first.
    comp_idx: dict[str, int] = {}
    for rank, comp in enumerate(
        sorted(
            nx.weakly_connected_components(dag),
            key=lambda c: min(graph.sections[sid].grid_row for sid in c),
        )
    ):
        for sid in comp:
            comp_idx[sid] = rank

    # Determine flow direction for each sweep: RL sweeps number
    # columns right-to-left (descending grid_col) to match the flow.
    sweep_is_rl: dict[int, bool] = {}
    for sid, s in graph.sections.items():
        sw = sweep[sid]
        if sw not in sweep_is_rl and s.direction == "RL":
            sweep_is_rl[sw] = True
        elif sw not in sweep_is_rl and s.direction == "LR":
            sweep_is_rl[sw] = False

    def _sort_key(s):
        sw = sweep[s.id]
        col = -s.grid_col if sweep_is_rl.get(sw, False) else s.grid_col
        return (comp_idx.get(s.id, 0), sw, col, s.grid_row)

    sorted_sections = sorted(graph.sections.values(), key=_sort_key)
    for i, section in enumerate(sorted_sections, start=1):
        section.number = i


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


def _align_row_y_grids(
    graph: MetroGraph,
    section_subgraphs: dict[str, MetroGraph],
    y_spacing: float,
    section_y_padding: float,
) -> None:
    """Snap station Y coordinates to a shared grid within each row.

    For same-direction sections in the same grid row, determines the
    maximum stations-per-layer across all sections and builds a shared
    Y grid with that many slots at *y_spacing* pitch.

    Constraints preserved:

    1. **Isolated stations** (sole occupant of their layer, with a Y
       value not found at any multi-station layer) keep their original Y.
       This preserves hub-station centering (e.g. bench_hub).
    2. **Bbox dimensions** (bbox_w, bbox_h) are unchanged.  Stations may
       shift within their bbox but the box itself keeps its Stage-1.1 size.
    3. **y_pad compensation**: a uniform shift of ``max_y_pad - y_pad``
       is applied to every station so that after Stage 3.5 top-aligns
       bbox_y, the first-station Y matches across sections despite
       differing ``_multiline_label_padding``.

    Stores grid metadata on ``graph._row_y_grid_info`` for the debug
    overlay to render shared Y grid lines.

    Runs between Stage 1.1 and Stage 1.3, operating in local coordinates.
    """
    from nf_metro.layout.section_placement import _assign_grid_layout

    # Pre-compute grid rows (not yet set on Section objects at this point)
    section_edges = graph.section_dag.section_edges if graph.section_dag else set()
    _, row_assign = _assign_grid_layout(graph, section_edges)

    # Group sections by (row, direction), skipping TB sections
    groups: dict[tuple[int, str], list[str]] = defaultdict(list)
    for sec_id in section_subgraphs:
        section = graph.sections[sec_id]
        row = row_assign.get(sec_id, -1)
        if row < 0 or section.direction == "TB":
            continue
        groups[(row, section.direction)].append(sec_id)

    # Store grid info for debug overlay
    grid_info: dict[int, dict] = {}

    for (row, _direction), sec_ids in groups.items():
        if len(sec_ids) < 2:
            continue

        # Grid size = max stations stacked at any single layer across
        # all sections in the group.
        grid_slots = 0
        for sec_id in sec_ids:
            sub = section_subgraphs[sec_id]
            grid_slots = max(grid_slots, _max_stations_per_layer(sub))

        if grid_slots <= 1:
            continue

        # Compute max y_pad across group for compensation
        max_y_pad = 0.0
        for sec_id in sec_ids:
            sub = section_subgraphs[sec_id]
            y_pad = section_y_padding + _multiline_label_padding(sub)
            max_y_pad = max(max_y_pad, y_pad)

        # Scale effective y_spacing when stations carry many lines.
        # Per-line offsets spread the rendered line bundle vertically;
        # when the spread + label height exceeds the base y_spacing,
        # labels on adjacent tracks overlap.  We inflate the grid
        # pitch just enough to guarantee clearance.
        #
        # Only count stations at multi-station layers (i.e. Y values
        # in remap_ys).  Isolated hub stations (sole layer occupant,
        # e.g. bench_hub with 6 lines) don't represent inter-track
        # crowding and should not inflate spacing for the entire row.
        #
        max_lines = 0
        section_class: dict[str, tuple[dict[int, list[float]], set[float]]] = {
            sec_id: _classify_multi_station_ys(section_subgraphs[sec_id])
            for sec_id in sec_ids
        }
        for sec_id in sec_ids:
            sub = section_subgraphs[sec_id]
            _multi_ys = section_class[sec_id][1]
            for st in sub.stations.values():
                if not st.is_port and st.y in _multi_ys:
                    max_lines = max(max_lines, len(graph.station_lines(st.id)))
        min_track_gap = (
            (max_lines - 1) * OFFSET_STEP
            + 2 * STATION_RADIUS_APPROX
            + LABEL_OFFSET
            + FONT_HEIGHT
        )
        effective_y_spacing = max(y_spacing, min_track_gap)

        grid_info[row] = {
            "section_ids": list(sec_ids),
            "slot_count": grid_slots,
            "slot_spacing": effective_y_spacing,
            "max_y_pad": max_y_pad,
        }

        # Remap each section's stations to the shared grid
        for sec_id in sec_ids:
            sub = section_subgraphs[sec_id]
            section = graph.sections[sec_id]

            layer_stations, multi_layer_ys = section_class[sec_id]

            # Remap multi-station-layer Y values first.
            if multi_layer_ys:
                remap_ys = sorted(multi_layer_ys)
            else:
                remap_ys = sorted(set(s.y for s in sub.stations.values()))

            # Check for diamond patterns: isolated Y values that sit
            # between adjacent remap_ys indicate a join/fork hub.
            # These need at least 2 grid slots between tracks so the
            # hub has visual room.  Only for small fan-outs; large
            # fan-outs (>3 per layer) keep their original spacing.
            max_layer_size = max((len(ys) for ys in layer_stations.values()), default=0)
            all_ys = sorted(set(s.y for s in sub.stations.values()))
            isolated_ys = set(all_ys) - set(remap_ys)
            has_diamond = False
            if max_layer_size <= 3 and len(remap_ys) >= 2 and isolated_ys:
                for iso_y in isolated_ys:
                    if remap_ys[0] < iso_y < remap_ys[-1]:
                        has_diamond = True
                        break

            # Map Y values to grid slots.
            #
            # Uniform spacing preservation: when all input gaps are
            # equal (e.g. [0, 68.8, 137.6] with gap 68.8), map to
            # equally-spaced slots so the output gaps stay uniform.
            # This avoids asymmetric compression that causes label
            # clashes (e.g. floor gives [0,40,120] = gaps 40,80).
            #
            # Diamond gap enforcement: when a fork/join hub sits
            # between tracks, ensure at least 2-slot gap so the hub
            # has visual room.
            y_map: dict[float, float] = {}

            # Detect uniform input spacing
            gaps = [remap_ys[i + 1] - remap_ys[i] for i in range(len(remap_ys) - 1)]
            uniform_gap = len(gaps) >= 1 and all(abs(g - gaps[0]) < 1.0 for g in gaps)

            if uniform_gap and len(gaps) >= 1:
                # Floor the uniform gap to a whole number of grid
                # slots (minimum 1).  Using floor instead of round
                # prevents inflation (e.g. a 68.8px gap with 40px
                # spacing stays at 1 slot, not 2).
                slot_gap = max(1, int(math.floor(gaps[0] / effective_y_spacing)))
                if has_diamond and slot_gap < 2:
                    slot_gap = 2
                for i, old_y in enumerate(remap_ys):
                    y_map[old_y] = i * slot_gap * effective_y_spacing
            else:
                # Build set of Y-value pairs that MUST occupy different
                # grid slots because they co-occur at the same layer.
                # Unlike the previous "check only previous remap_y"
                # approach, we check against ALL values already
                # assigned to the candidate slot, so non-adjacent
                # pairs (e.g. sortmerna and ribodetector separated by
                # trimgalore in remap_ys) are still caught.
                must_separate: set[tuple[float, float]] = set()
                for ys_at_layer in layer_stations.values():
                    unique_ys = sorted(
                        set(y for y in ys_at_layer if y in multi_layer_ys)
                    )
                    for a_idx in range(len(unique_ys)):
                        for b_idx in range(a_idx + 1, len(unique_ys)):
                            must_separate.add((unique_ys[a_idx], unique_ys[b_idx]))

                slot_for_y: dict[float, int] = {}
                prev_slot = 0
                for i, old_y in enumerate(remap_ys):
                    raw_slot = int(math.floor(old_y / effective_y_spacing))
                    slot = max(raw_slot, prev_slot)
                    # Check if this Y must be separated from any value
                    # already assigned to the same slot.  Re-check after
                    # each bump in case the new slot also conflicts.
                    _changed = True
                    while _changed:
                        _changed = False
                        for other_y, other_slot in slot_for_y.items():
                            if other_slot != slot:
                                continue
                            pair = (min(other_y, old_y), max(other_y, old_y))
                            if pair in must_separate:
                                slot += 1
                                _changed = True
                                break
                    # Diamond: ensure at least 2-slot gap from previous
                    if has_diamond and prev_slot > 0 and slot - prev_slot < 2:
                        slot = prev_slot + 2
                    elif has_diamond and prev_slot == 0 and slot < 2 and old_y > 0:
                        slot = 2
                    y_map[old_y] = slot * effective_y_spacing
                    slot_for_y[old_y] = slot
                    prev_slot = slot

            # Multi-line label clearance: when a multi-line label station
            # is sandwiched between same-layer neighbors above AND below,
            # the label must fit within the gap.  Enforce a 2-slot gap
            # from the preceding Y so the label text doesn't overlap.
            # Stations at the top or bottom of their column can extend
            # outward and don't need the extra gap.
            layer_at_y: dict[tuple[int, float], bool] = {}
            for st in sub.stations.values():
                if not st.is_port:
                    layer_at_y[(st.layer, st.y)] = True
            _needs_gap_ys: set[float] = set()
            for st in sub.stations.values():
                if st.is_port or not st.label or "\n" not in st.label:
                    continue
                if st.y not in y_map:
                    continue
                has_above = any(
                    (st.layer, ry) in layer_at_y for ry in remap_ys if ry < st.y - 0.5
                )
                has_below = any(
                    (st.layer, ry) in layer_at_y for ry in remap_ys if ry > st.y + 0.5
                )
                if has_above and has_below:
                    _needs_gap_ys.add(st.y)
            if _needs_gap_ys:
                sorted_mapped = sorted(y_map.items(), key=lambda kv: kv[1])
                for idx in range(1, len(sorted_mapped)):
                    old_y, new_y = sorted_mapped[idx]
                    prev_y = sorted_mapped[idx - 1][1]
                    if old_y not in _needs_gap_ys:
                        continue
                    gap_slots = round((new_y - prev_y) / effective_y_spacing)
                    if gap_slots < 2:
                        extra = (2 - gap_slots) * effective_y_spacing
                        for j in range(idx, len(sorted_mapped)):
                            k = sorted_mapped[j][0]
                            y_map[k] += extra
                        sorted_mapped = sorted(y_map.items(), key=lambda kv: kv[1])

            # Snap isolated Y values to the nearest grid slot (any
            # multiple of effective_y_spacing, not just mapped slots).
            # This keeps diamond join points between tracks on-grid
            # without collapsing them onto a track endpoint.  Skip for
            # large fan-outs where snapping disrupts routing geometry.
            if max_layer_size <= 3:
                for old_y in all_ys:
                    if old_y not in y_map:
                        slot = round(old_y / effective_y_spacing)
                        y_map[old_y] = slot * effective_y_spacing

            for station in sub.stations.values():
                if station.y in y_map:
                    station.y = y_map[station.y]

            # y_pad compensation: shift all stations so that the
            # distance from the top of the Y range to the first
            # station equals max_y_pad.  After Stage 3.5 top-aligns
            # bbox_y, this makes first_station_y consistent across
            # sections with different multiline label padding.
            y_pad = section_y_padding + _multiline_label_padding(sub)
            shift = max_y_pad - y_pad
            if shift > 0:
                for station in sub.stations.values():
                    station.y += shift

            # Recompute bbox to match remapped + shifted positions.
            ys = [s.y for s in sub.stations.values()]
            section.bbox_y = min(ys) - max_y_pad
            section.bbox_h = (max(ys) - min(ys)) + max_y_pad * 2

    graph._row_y_grid_info = grid_info


def _recompute_grid_group_bboxes(graph: MetroGraph) -> None:
    """Recompute bboxes for sections in grid groups after port finalisation.

    Earlier phases may temporarily expand bboxes for port positions that
    are later corrected.  This step resets each grid-group section's bbox
    to symmetric ``max_y_pad`` padding around the final non-port station
    Y range, then expands for any ports that fall outside.
    """
    grid_info = graph._row_y_grid_info
    for _row, info in grid_info.items():
        max_y_pad = info["max_y_pad"]
        for sec_id in info["section_ids"]:
            section = graph.sections.get(sec_id)
            if not section:
                continue
            non_port_ys = [
                graph.stations[sid].y
                for sid in section.station_ids
                if sid in graph.stations and not graph.stations[sid].is_port
            ]
            if not non_port_ys:
                continue
            min_y = min(non_port_ys)
            max_y = max(non_port_ys)
            section.bbox_y = min_y - max_y_pad
            section.bbox_h = (max_y - min_y) + max_y_pad * 2
            # Expand for ports that landed outside the symmetric bbox.
            # Use bare containment (no extra padding) to avoid
            # inflating the bbox asymmetrically for off-grid ports.
            top = section.bbox_y
            bot = section.bbox_y + section.bbox_h
            for sid in section.station_ids:
                st = graph.stations.get(sid)
                if st and st.is_port:
                    if st.y < top:
                        section.bbox_h += top - st.y
                        section.bbox_y = st.y
                    elif st.y > bot:
                        section.bbox_h = st.y - section.bbox_y
                    top = section.bbox_y
                    bot = section.bbox_y + section.bbox_h


def _top_align_row_sections(graph: MetroGraph) -> None:
    """Shift sections up so bbox tops align within each grid row.

    Only aligns sections that form contiguous column groups within the
    row.  Sections separated by a column gap (e.g. reporting at col 3
    vs dna_analysis at col 1 with no row-mate at col 2) are aligned
    independently so structurally-determined positions aren't disturbed.
    """
    row_sections: dict[int, list[Section]] = defaultdict(list)
    for section in graph.sections.values():
        if section.bbox_h > 0 and section.grid_row >= 0:
            row_sections[section.grid_row].append(section)

    for row, sections in row_sections.items():
        if len(sections) < 2:
            continue
        # Group into contiguous column runs
        sections_by_col = sorted(sections, key=lambda s: s.grid_col)
        groups: list[list[Section]] = [[sections_by_col[0]]]
        for s in sections_by_col[1:]:
            if s.grid_col - groups[-1][-1].grid_col <= 1:
                groups[-1].append(s)
            else:
                groups.append([s])

        for group in groups:
            if len(group) < 2:
                continue
            min_top = min(s.bbox_y for s in group)
            for section in group:
                delta = section.bbox_y - min_top
                if delta <= 0:
                    continue
                for sid in section.station_ids:
                    station = graph.stations.get(sid)
                    if station:
                        station.y -= delta
                section.bbox_y -= delta


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


def _top_align_row_bboxes_only(graph: MetroGraph) -> None:
    """Align bbox tops within each row by growing bboxes upward.

    Unlike ``_top_align_row_sections`` (which shifts stations together
    with their bbox), this phase only moves ``bbox_y`` and grows
    ``bbox_h`` so the section background extends upward to match the
    row's topmost bbox.  Station, port and junction Ys inside the
    section are left in place, producing empty space at the top of
    sections that didn't have off-track inputs to lift.

    Used after ``_lift_off_track_stations`` so off-track expansion in
    one section doesn't leave other row-mates with misaligned bbox
    tops.
    """
    for group in _row_contiguous_column_groups(graph):
        min_top = min(s.bbox_y for s in group)
        for section in group:
            delta = section.bbox_y - min_top
            if delta <= 0:
                continue
            section.bbox_y = min_top
            section.bbox_h += delta


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


def _align_row_trunk_ys(graph: MetroGraph) -> None:
    """Shift sections vertically so trunk Ys align within each grid row.

    Sections in a row's contiguous column run whose trunk Y sits above
    the row's deepest trunk shift down to match.  Bbox tops are
    preserved (heights grow downward).  Row-spanning sections
    (grid_row_span > 1) are skipped to avoid disturbing cross-row
    vertical relationships.
    """
    row_sections: dict[int, list[Section]] = defaultdict(list)
    for section in graph.sections.values():
        if (
            section.bbox_h <= 0
            or section.grid_row < 0
            or section.direction not in ("LR", "RL")
            or section.grid_row_span > 1
        ):
            continue
        row_sections[section.grid_row].append(section)

    for sections in row_sections.values():
        if len(sections) < 2:
            continue
        sections_by_col = sorted(sections, key=lambda s: s.grid_col)
        groups: list[list[Section]] = [[sections_by_col[0]]]
        for s in sections_by_col[1:]:
            if s.grid_col - groups[-1][-1].grid_col <= 1:
                groups[-1].append(s)
            else:
                groups.append([s])

        for group in groups:
            if len(group) < 2:
                continue
            # Only realign when every LR-bearing section in the group
            # shares the same bundle.  Differing bundles mean the row
            # has no single trunk crossing all sections, so forcing a
            # common Y just shifts content downward without any
            # geometric gain.
            bundles = [_section_bundle_lines(graph, s) for s in group]
            non_empty = [b for b in bundles if b]
            if not non_empty or any(b != non_empty[0] for b in non_empty):
                continue
            trunks = {
                s.id: t for s in group if (t := _section_trunk_y(graph, s)) is not None
            }
            if len(trunks) < 2:
                continue
            target_y = max(trunks.values())
            shifted: set[str] = set()
            for section in group:
                ty = trunks.get(section.id)
                if ty is None:
                    continue
                delta = target_y - ty
                if delta < 0.5:
                    continue
                for sid in section.station_ids:
                    st = graph.stations.get(sid)
                    if st:
                        st.y += delta
                    port = graph.ports.get(sid)
                    if port:
                        port.y += delta
                section.bbox_h += delta
                shifted.add(section.id)

            # Re-snap each shifted section's LR ports to target_y when
            # they have a single internal station at target_y.  Skip
            # ports with 2+ distinct internal Ys (fan-in centering).
            for section in group:
                if section.id not in shifted:
                    continue
                port_set = section.port_ids
                internal_ids = set(section.station_ids) - port_set
                for pid in port_set:
                    p = graph.ports.get(pid)
                    port_st = graph.stations.get(pid)
                    if (
                        p is None
                        or port_st is None
                        or p.side not in (PortSide.LEFT, PortSide.RIGHT)
                        or abs(port_st.y - target_y) < 0.5
                    ):
                        continue
                    connected_ys: set[float] = set()
                    target_aligned = False
                    neighbours: list[str] = []
                    for edge in graph.edges_from(pid):
                        if edge.target in internal_ids:
                            neighbours.append(edge.target)
                    for edge in graph.edges_to(pid):
                        if edge.source in internal_ids:
                            neighbours.append(edge.source)
                    for other_id in neighbours:
                        st = graph.stations.get(other_id)
                        if st and not st.is_port:
                            connected_ys.add(round(st.y, 1))
                            if abs(st.y - target_y) < 0.5:
                                target_aligned = True
                    if len(connected_ys) < 2 and target_aligned:
                        _set_port_y(graph, pid, target_y)


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


def _compact_row_content_to_bbox_top(
    graph: MetroGraph, section_y_padding: float, y_spacing: float
) -> None:
    """Pull row-mate sections up and shrink bottoms so content fits snugly.

    Two-step compaction within each grid row's contiguous column run:

    1. Per section, compute the allowable upward shift bounded by
       ``min(content_y) - bbox_y - section_y_padding`` so the topmost
       station (on-track or off-track) stays inside the bbox padding
       zone.  The uniform shift applied to the group is the minimum
       allowable shift across same-row sections; that preserves the
       trunk-Y alignment established by Stage 4.8.  Both on-track and
       off-track stations move together so the gap between each
       off-track input and its consumer is preserved.
    2. Shrink each section's ``bbox_h`` so the bottom slack matches
       ``section_y_padding`` (clamped so ports inside the section stay
       within the bbox).

    Row-spanning sections (``grid_row_span > 1``) are only isolated
    from their row-mates when their trunk Y differs (no shared
    horizontal bundle).  When a rowspan section trunks at the row's
    bundle Y, it must compact together with its column neighbours so
    the shared bundle stays straight.
    """
    row_sections: dict[int, list[Section]] = defaultdict(list)
    for section in graph.sections.values():
        if section.bbox_h <= 0 or section.grid_row < 0:
            continue
        row_sections[section.grid_row].append(section)

    def _shares_bundle(a: Section, b: Section) -> bool:
        if a.grid_row_span <= 1 and b.grid_row_span <= 1:
            return True
        a_t = _section_trunk_y(graph, a)
        b_t = _section_trunk_y(graph, b)
        return a_t is not None and b_t is not None and abs(a_t - b_t) < 0.5

    for sections in row_sections.values():
        if not sections:
            continue
        sections_by_col = sorted(sections, key=lambda s: s.grid_col)
        groups: list[list[Section]] = [[sections_by_col[0]]]
        for s in sections_by_col[1:]:
            prev = groups[-1][-1]
            if s.grid_col - prev.grid_col <= 1 and _shares_bundle(prev, s):
                groups[-1].append(s)
            else:
                groups.append([s])

        for group in groups:
            # An isolated rowspan section has no shared bundle to keep
            # straight; compacting it alone yanks its content above the
            # rowspan-1 cohort's trunk Y.
            if len(group) == 1 and group[0].grid_row_span > 1:
                continue
            allowed_shifts: list[float] = []
            for section in group:
                # Use all real (non-port) stations as the top reference so
                # off-track inputs lifted above their consumers also stay
                # inside the bbox padding zone.  Off-track stations now
                # move together with on-track during compaction.
                content_ys = [
                    graph.stations[sid].y
                    for sid in section.station_ids
                    if sid in graph.stations and not graph.stations[sid].is_port
                ]
                if not content_ys:
                    continue
                content_min = min(content_ys)
                shift = content_min - section.bbox_y - section_y_padding
                allowed_shifts.append(max(0.0, shift))
            delta = min(allowed_shifts) if allowed_shifts else 0.0

            if delta >= 0.5:
                for section in group:
                    # bbox_y is preserved; only bbox_h shrinks.  Clamp
                    # edge-pinned ports so the shift doesn't push them
                    # outside the (now-shorter) bbox.
                    new_bottom = section.bbox_y + max(0.0, section.bbox_h - delta)
                    for sid in section.station_ids:
                        st = graph.stations.get(sid)
                        if st is None:
                            continue
                        new_y = st.y - delta
                        port = graph.ports.get(sid)
                        if port is not None:
                            if port.side == PortSide.TOP:
                                new_y = max(new_y, section.bbox_y)
                            elif port.side == PortSide.BOTTOM:
                                new_y = min(new_y, new_bottom)
                            _set_port_y(graph, sid, new_y)
                        else:
                            st.y = new_y
                    section.bbox_h = max(0.0, section.bbox_h - delta)

            for section in group:
                on_track_ys, off_track_ys, port_ys = _classify_section_station_ys(
                    graph, section
                )
                content_ys = on_track_ys + off_track_ys
                if not content_ys:
                    continue
                desired_bot = max(content_ys) + section_y_padding
                if port_ys:
                    desired_bot = max(desired_bot, max(port_ys))
                new_h = desired_bot - section.bbox_y
                if new_h < section.bbox_h - 0.5:
                    section.bbox_h = max(0.0, new_h)


def _snap_inter_section_port_pairs(graph: MetroGraph) -> None:
    """Snap exit/entry port pairs in the same row to a shared Y.

    For each LEFT/RIGHT exit port that connects (directly or via a
    junction) to a same-row LEFT/RIGHT entry port, picks the entry's Y
    as the shared anchor.  This eliminates the small Y kinks left by
    sections whose trunk Y couldn't be aligned via Stage 4.8 (e.g.
    row-spanning sections that the trunk aligner skips); the inside-
    section link from the internal source to the port may bend by a
    pixel or two but the inter-section bundle stays perfectly
    horizontal.

    Fan-in exits (two or more distinct internal source Ys) are skipped
    so the visual convergence into a single port stays meaningful.

    Returns True when at least one port was moved (caller may need to
    re-run junction positioning to pick up the new exit-port Y).

    Scoped to pipelines that use explicit ``%%metro grid:`` directives;
    auto-layout pipelines keep their existing inter-section routing so
    line-offset ordering tests stay stable.  The fan-in entry-port snap
    branch below runs unconditionally because it only moves the entry
    port (not the exit), preserving the auto-layout fan-in convergence.
    """
    explicit_grid = bool(graph._explicit_grid)
    junction_ids = graph.junction_ids

    for port_id, port in graph.ports.items():
        if port.is_entry:
            continue
        if port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        section = graph.sections.get(port.section_id)
        if section is None or section.direction not in ("LR", "RL"):
            continue
        port_st = graph.stations.get(port_id)
        if port_st is None:
            continue

        # Find downstream entry port(s) in the same row.
        targets: list[float] = []
        for edge in graph.edges_from(port_id):
            entry_candidates: list[str] = []
            tgt_port = graph.ports.get(edge.target)
            if tgt_port and tgt_port.is_entry:
                entry_candidates.append(edge.target)
            elif edge.target in junction_ids:
                for e2 in graph.edges_from(edge.target):
                    tp2 = graph.ports.get(e2.target)
                    if tp2 and tp2.is_entry:
                        entry_candidates.append(e2.target)
            for eid in entry_candidates:
                ep = graph.ports.get(eid)
                if ep is None or ep.side not in (PortSide.LEFT, PortSide.RIGHT):
                    continue
                ds_sec = graph.sections.get(ep.section_id)
                if ds_sec is None or ds_sec.grid_row != section.grid_row:
                    continue
                ep_st = graph.stations.get(eid)
                if ep_st is not None:
                    targets.append(ep_st.y)
        if not targets:
            continue
        target_y = min(targets, key=lambda y: abs(y - port_st.y))
        if abs(port_st.y - target_y) < 0.5:
            continue

        # Fan-in exits (multiple distinct internal source Ys) want to
        # keep their centred-midpoint convergence Y, so don't move the
        # exit port itself.  Instead, snap the downstream entry port
        # to the exit port's Y so the inter-section trunk stays flat.
        port_set = section.port_ids
        src_ys: set[float] = set()
        for edge in graph.edges_to(port_id):
            src = graph.stations.get(edge.source)
            if src and not src.is_port and edge.source not in port_set:
                src_ys.add(round(src.y, 1))
        if len(src_ys) >= 2:
            for edge in graph.edges_from(port_id):
                entry_candidates: list[str] = []
                tgt_port = graph.ports.get(edge.target)
                if tgt_port and tgt_port.is_entry:
                    entry_candidates.append(edge.target)
                elif edge.target in junction_ids:
                    for e2 in graph.edges_from(edge.target):
                        tp2 = graph.ports.get(e2.target)
                        if tp2 and tp2.is_entry:
                            entry_candidates.append(e2.target)
                for eid in entry_candidates:
                    ep = graph.ports.get(eid)
                    if ep is None or ep.side not in (PortSide.LEFT, PortSide.RIGHT):
                        continue
                    ds_sec = graph.sections.get(ep.section_id)
                    if ds_sec is None or ds_sec.grid_row != section.grid_row:
                        continue
                    ep_st = graph.stations.get(eid)
                    if ep_st is None or abs(ep_st.y - port_st.y) < 0.5:
                        continue
                    _set_port_y(graph, eid, port_st.y)
            continue

        if not explicit_grid:
            continue
        _set_port_y(graph, port_id, target_y)


def _fan_free_content_upward(
    graph: MetroGraph, section_y_padding: float, y_spacing: float
) -> None:
    """Fill empty top space by fanning trunk-candidate siblings up.

    When ``_compact_row_content_to_bbox_top`` is bounded by a row-mate
    section (e.g. one with off-track inputs), other sections in the
    group may keep visible empty space above their topmost station.
    For each LR/RL section with no off-track stations and at least one
    ``y_spacing`` slot of top slack, redistribute the section's
    trunk-candidate siblings (stations carrying the full bundle in the
    same entry column) into the empty top space.

    The topmost trunk candidate stays pinned (preserves the row trunk
    Y); the rest are stacked above it at ``y_spacing`` pitch.  Stations
    in later columns are not moved here so downstream chains keep their
    relative position to the trunk station they branch from.

    Scoped to pipelines using explicit ``%%metro grid:`` directives so
    the auto-layout path is unaffected.
    """
    if not graph._explicit_grid:
        return
    for section in graph.sections.values():
        if section.bbox_h <= 0 or section.direction not in ("LR", "RL"):
            continue
        if any(
            getattr(graph.stations.get(sid), "off_track", False)
            for sid in section.station_ids
        ):
            continue
        port_set = section.port_ids
        internal_ids = [
            sid
            for sid in section.station_ids
            if sid not in port_set
            and sid in graph.stations
            and not graph.stations[sid].is_port
        ]
        if not internal_ids:
            continue
        ys = [graph.stations[sid].y for sid in internal_ids]
        top_y = min(ys)
        slack = top_y - section.bbox_y - section_y_padding
        if slack < y_spacing - 0.5:
            continue
        slots = int(slack // y_spacing)
        if slots <= 0:
            continue

        bundle = _section_bundle_lines(graph, section)
        if not bundle:
            continue

        # Trunk candidates: stations in the entry column carrying the
        # full bundle.  Sort by current Y; topmost stays pinned, others
        # stack above at y_spacing pitch.
        xs = sorted({round(graph.stations[sid].x, 3) for sid in internal_ids})
        if not xs:
            continue
        entry_x = xs[0] if section.direction == "LR" else xs[-1]
        trunk_candidates = [
            sid
            for sid in internal_ids
            if round(graph.stations[sid].x, 3) == entry_x
            and set(graph.station_lines(sid)) == bundle
        ]
        if len(trunk_candidates) < 2:
            continue
        # Skip if every station in the entry column carries the full
        # bundle (no unique trunk): ``_redistribute_full_bundle_columns``
        # has already symmetrically fanned them around the section's
        # port Y and we must not collapse that into a one-sided stack.
        entry_col_all = [
            sid for sid in internal_ids if round(graph.stations[sid].x, 3) == entry_x
        ]
        if len(trunk_candidates) == len(entry_col_all) and graph.center_ports:
            continue
        trunk_candidates.sort(key=lambda s: graph.stations[s].y)
        pinned = trunk_candidates[0]
        anchor_y = graph.stations[pinned].y
        to_lift = [
            sid
            for sid in trunk_candidates[1 : 1 + slots]
            if not _lift_would_cause_uturn(graph, sid, section.id, anchor_y)
        ]
        for i, sid in enumerate(to_lift, 1):
            graph.stations[sid].y = anchor_y - i * y_spacing


def _shift_linear_consumer_chain(
    graph: MetroGraph,
    src: str,
    delta: float,
    internal_ids: set[str],
    *,
    cycle_guard: bool = False,
) -> None:
    """Walk a strictly-linear consumer chain and shift each station Y by *delta*.

    Each step requires the current station to have a single outbound edge,
    the next link to live inside *internal_ids* with a single inbound edge,
    and an unchanged line-set across the hop.  Stops at the first hop that
    fails any of those conditions.  Pass ``cycle_guard=True`` to additionally
    bail out when the walk revisits a station.
    """
    cur = src
    src_lines = set(graph.station_lines(src))
    visited: set[str] = {src} if cycle_guard else set()
    while True:
        outs = {e.target for e in graph.edges_from(cur)}
        if len(outs) != 1:
            return
        nxt = next(iter(outs))
        if nxt not in internal_ids or (cycle_guard and nxt in visited):
            return
        if len({e.source for e in graph.edges_to(nxt)}) != 1:
            return
        if set(graph.station_lines(nxt)) != src_lines:
            return
        graph.stations[nxt].y += delta
        if cycle_guard:
            visited.add(nxt)
        cur = nxt


def _fan_source_inputs_upward(graph: MetroGraph, y_spacing: float) -> None:
    """Fill empty top space by lifting source-input chains above the trunk.

    Companion to ``_fan_free_content_upward`` for sections whose entry
    column contains a single full-bundle trunk station plus subset-bundle
    sources (file inputs with no inbound edges).  The trunk-candidate
    path skips these (it requires >=2 full-bundle candidates), leaving
    every source stacked at or below the trunk and the bbox top empty.

    For each qualifying LR/RL grid section, sort sources by current Y
    (closest to trunk first) and lift up to ``slack // y_spacing`` of
    them above the trunk at ``y_spacing`` pitch.  Each lifted source
    drags its linear consumer chain (one inbound edge, identical line
    set, strictly inside the section) so per-line tracks stay straight.

    Scoped to explicit ``%%metro grid:`` pipelines so the auto-layout
    path is unaffected.  U-turn risk is nil because sources have no
    upstream feeders by definition.
    """
    if not graph._explicit_grid:
        return

    for section in graph.sections.values():
        if section.bbox_h <= 0 or section.direction not in ("LR", "RL"):
            continue
        if any(
            getattr(graph.stations.get(sid), "off_track", False)
            for sid in section.station_ids
        ):
            continue
        port_set = section.port_ids
        internal_ids = [
            sid
            for sid in section.station_ids
            if sid not in port_set
            and sid in graph.stations
            and not graph.stations[sid].is_port
        ]
        if not internal_ids:
            continue
        bundle = _section_bundle_lines(graph, section)
        if not bundle:
            continue

        xs = sorted({round(graph.stations[sid].x, 3) for sid in internal_ids})
        entry_x = xs[0] if section.direction == "LR" else xs[-1]
        entry_col = [
            sid for sid in internal_ids if round(graph.stations[sid].x, 3) == entry_x
        ]
        trunks = [s for s in entry_col if set(graph.station_lines(s)) == bundle]
        if len(trunks) != 1:
            continue
        trunk_sid = trunks[0]
        trunk_y = graph.stations[trunk_sid].y

        sources = [
            s
            for s in entry_col
            if s != trunk_sid
            and graph.station_lines(s)
            and set(graph.station_lines(s)) < bundle
            and not graph.edges_to(s)
            and graph.stations[s].y > trunk_y - 0.5
        ]
        if len(sources) < 2:
            continue

        # Reserve icon_half when any source renders as a file icon so the
        # icon's vertical extent stays inside the bbox.
        any_terminus = any(graph.stations[s].is_terminus for s in sources)
        top_margin = y_spacing / 4 + (ICON_HALF_HEIGHT if any_terminus else 0.0)
        slack = trunk_y - section.bbox_y - top_margin
        slots = int((slack + 0.5) // y_spacing)
        if slots < 1:
            continue
        # Keep at least half the sources at or below the trunk so the
        # section stays bottom-weighted when only a couple fit above.
        n_lift = min(slots, len(sources) // 2)
        if n_lift == 0:
            continue

        sources.sort(key=lambda s: graph.stations[s].y)
        internal_set = set(internal_ids)

        # Drag each strictly-linear consumer chain so per-line tracks stay
        # straight from icon to trunk junction.
        for i, src in enumerate(sources[:n_lift], 1):
            new_y = trunk_y - i * y_spacing
            delta = new_y - graph.stations[src].y
            if abs(delta) < 0.5:
                continue
            graph.stations[src].y = new_y
            _shift_linear_consumer_chain(graph, src, delta, internal_set)

        # Compact the remaining below-trunk sources upward to fill the
        # rows their predecessors vacated.  Without this step, lifted
        # sources leave a multi-slot gap between the trunk and the
        # first below-trunk source (e.g. trunk -> empty -> empty -> Affy
        # row when GTF and Matrix were lifted).  Place the i-th
        # below-trunk source at ``trunk_y + i * y_spacing``.
        for i, src in enumerate(sources[n_lift:], 1):
            new_y = trunk_y + i * y_spacing
            delta = new_y - graph.stations[src].y
            if abs(delta) < 0.5:
                continue
            graph.stations[src].y = new_y
            _shift_linear_consumer_chain(graph, src, delta, internal_set)


def _balance_direct_external_feeder_ys(
    graph: MetroGraph, station_id: str, section_id: str
) -> list[float]:
    """Return Ys of the candidate station's per-line external feeders.

    Walks edges INTO ``station_id`` by line: for each inbound (src, lid)
    pair, traverse the (src, lid) chain through junctions and ports
    until reaching the first non-port, non-junction station.  Filtering
    by line means transit-only stations feeding the same shared port
    on a different line are not counted.
    """
    junction_ids = graph.junction_ids
    feeder_ys: list[float] = []
    seen: set[tuple[str, str]] = set()
    stack: list[tuple[str, str]] = [
        (edge.source, edge.line_id) for edge in graph.edges_to(station_id)
    ]
    while stack:
        cur_id, lid = stack.pop()
        if (cur_id, lid) in seen:
            continue
        seen.add((cur_id, lid))
        if cur_id in junction_ids:
            for edge in graph.edges_to(cur_id):
                if edge.line_id == lid:
                    stack.append((edge.source, lid))
            continue
        src = graph.stations.get(cur_id)
        if src is None:
            continue
        if src.is_port:
            for edge in graph.edges_to(cur_id):
                if edge.line_id == lid:
                    stack.append((edge.source, lid))
            continue
        if src.section_id == section_id:
            continue
        feeder_ys.append(src.y)
    return feeder_ys


def _balance_section_content_around_trunk(
    graph: MetroGraph, section_y_padding: float, y_spacing: float
) -> None:
    """Rebalance fan-out siblings to fill empty bands above the trunk.

    Runs after fan-upward and re-centering finalises the trunk Y.
    For sections whose final layout still leaves a >= 1 * ``y_spacing``
    empty band above the topmost station while more siblings sit below
    the trunk than above, either:

    - LIFTS the bottommost (or topmost, depending on line homogeneity)
      below-trunk movable sibling to one slot above the topmost station
      when the bbox has room for the marker plus its above-marker
      label.  The lifted station's linear consumer chain follows so
      per-line tracks stay straight.

    - SWAPS the bottommost below-trunk movable with the topmost above-
      trunk station when the bbox has no headroom for an extra slot.

    Scoped to explicit ``%%metro grid:`` pipelines.  Line-aware U-turn
    safety prevents lifts that would force the line to climb past the
    trunk and double back.
    """
    if not graph._explicit_grid:
        return
    if not graph.center_ports:
        return

    for section in graph.sections.values():
        if section.bbox_h <= 0 or section.direction not in ("LR", "RL"):
            continue
        bundle = _section_bundle_lines(graph, section)
        if not bundle:
            continue
        port_set = section.port_ids
        internal_ids = [
            sid
            for sid in section.station_ids
            if sid not in port_set
            and sid in graph.stations
            and not graph.stations[sid].is_port
            and not graph.stations[sid].is_hidden
            and not graph.stations[sid].off_track
        ]
        if not internal_ids:
            continue

        trunk_y: float | None = None
        for pid in section.entry_ports + section.exit_ports:
            port = graph.ports.get(pid)
            st = graph.stations.get(pid)
            if port and st and port.side in (PortSide.LEFT, PortSide.RIGHT):
                trunk_y = st.y
                break
        if trunk_y is None:
            full_ys = sorted(
                graph.stations[s].y
                for s in internal_ids
                if set(graph.station_lines(s)) == bundle
            )
            if not full_ys:
                continue
            trunk_y = full_ys[len(full_ys) // 2]

        cols: dict[float, list[str]] = defaultdict(list)
        for sid in internal_ids:
            cols[round(graph.stations[sid].x, 3)].append(sid)

        section_top_y = min(graph.stations[s].y for s in internal_ids)
        top_band = section_top_y - section.bbox_y
        if top_band <= y_spacing + 0.5:
            continue

        movable: list[str] = []
        for x, sids in cols.items():
            trunks_in_col = [s for s in sids if set(graph.station_lines(s)) == bundle]
            if not trunks_in_col:
                continue
            for s in sids:
                if s in trunks_in_col:
                    continue
                lines = set(graph.station_lines(s))
                if not lines or not (lines < bundle):
                    continue
                movable.append(s)

        if not movable:
            continue

        ys = [graph.stations[s].y for s in movable]
        above_count = sum(1 for y in ys if y < trunk_y - 0.5)
        below_count = sum(1 for y in ys if y > trunk_y + 0.5)
        if below_count <= above_count:
            continue

        section_internal_set = set(internal_ids)

        # Ys of every other station (incl. off-track icons) at ``col_x``.
        # Lift candidates must avoid these slots or the lifted marker
        # overlaps an existing marker / icon at render time.
        def _column_occupied_ys(col_x: float, skip_sid: str) -> list[float]:
            occ: list[float] = []
            for sid2 in section.station_ids:
                if sid2 == skip_sid:
                    continue
                st2 = graph.stations.get(sid2)
                if st2 is None or st2.is_port or st2.is_hidden:
                    continue
                if abs(st2.x - col_x) > 0.5:
                    continue
                occ.append(st2.y)
            return occ

        max_iters = len(movable)
        for _ in range(max_iters):
            section_top_y = min(graph.stations[s].y for s in internal_ids)
            top_band = section_top_y - section.bbox_y
            if top_band <= y_spacing + 0.5:
                break
            ys = {s: graph.stations[s].y for s in movable}
            above = [s for s, y in ys.items() if y < trunk_y - 0.5]
            below = [s for s, y in ys.items() if y > trunk_y + 0.5]
            if len(below) <= len(above):
                break
            line_sets = {frozenset(graph.station_lines(s)) for s in movable}
            if len(line_sets) == 1:
                below.sort(key=lambda s: graph.stations[s].y, reverse=True)
            else:
                below.sort(key=lambda s: graph.stations[s].y)
            # First below-trunk candidate whose lift Y doesn't collide
            # with another station/icon already occupying the same column.
            candidate = None
            new_y = section_top_y - y_spacing
            for cand in below:
                col_x = graph.stations[cand].x
                occ = _column_occupied_ys(col_x, cand)
                if any(abs(oy - new_y) < y_spacing - 0.5 for oy in occ):
                    continue
                candidate = cand
                break
            if candidate is None:
                break
            st = graph.stations.get(candidate)
            has_above_label = bool(st and st.label and st.label.strip())
            label_clearance = y_spacing / 2 if has_above_label else 0.0
            # Off-track file icons reach ~16 px above centre; on-track
            # markers reach ~9.5 px.  Use the wider reach when relevant.
            marker_clearance = 16.0 if (st and st.off_track) else 9.5
            min_y = section.bbox_y + max(label_clearance, marker_clearance)
            if new_y < min_y - 0.5:
                if not above:
                    break
                above.sort(key=lambda s: graph.stations[s].y)
                top_above = above[0]
                ya = graph.stations[top_above].y
                yc = graph.stations[candidate].y
                if candidate != below[0]:
                    break
                graph.stations[candidate].y = ya
                graph.stations[top_above].y = yc
                _shift_linear_consumer_chain(
                    graph, candidate, ya - yc, section_internal_set
                )
                _shift_linear_consumer_chain(
                    graph, top_above, yc - ya, section_internal_set
                )
                break
            ext_feeders = _balance_direct_external_feeder_ys(
                graph, candidate, section.id
            )
            if len(ext_feeders) >= 2 and all(fy >= new_y - 0.5 for fy in ext_feeders):
                break
            delta = new_y - graph.stations[candidate].y
            graph.stations[candidate].y = new_y
            _shift_linear_consumer_chain(graph, candidate, delta, section_internal_set)

        # Below-trunk compaction: when the first row below the trunk is
        # empty but content sits two or more slots below, lift all
        # below-trunk stations up by one ``y_spacing`` so they pack
        # against the trunk row.  Honours the existing column-occupied
        # guard so off-track icons and marker clearance aren't violated.
        _compact_below_trunk_band(graph, section, trunk_y, y_spacing)


def _compact_below_trunk_band(
    graph: MetroGraph,
    section: Section,
    trunk_y: float,
    y_spacing: float,
) -> None:
    """Shift the entire below-trunk stack up by one y_spacing slot.

    Fires when the first below-trunk slot (``trunk_y + y_spacing``) is
    empty for non-port, non-hidden content while there is at least one
    station two or more slots below the trunk.  All non-port, non-hidden
    stations strictly below the trunk are shifted up by ``y_spacing``,
    including off-track inputs and their consumers.  The bbox is left
    alone: bottom shrink in :func:`_shrink_bboxes_to_content_bottom`
    will collapse the freed bottom space afterward.

    Symmetric counterpart to the above-trunk auto-balance: lift the
    bottom stack against the trunk when there's wasted space, instead
    of leaving an empty row directly below the trunk.
    """
    if y_spacing <= 0:
        return
    movables: list[str] = []
    for sid in section.station_ids:
        st = graph.stations.get(sid)
        if st is None or st.is_port or st.is_hidden:
            continue
        if st.y > trunk_y + 0.5:
            movables.append(sid)
    if not movables:
        return
    # Is there an empty row right below trunk?  Define "empty" as: no
    # station at trunk_y + y_spacing within half a slot.
    first_row_y = trunk_y + y_spacing
    has_first_row = any(
        abs(graph.stations[s].y - first_row_y) < y_spacing / 2 - 0.5 for s in movables
    )
    if has_first_row:
        return
    # Confirm there's content further below (otherwise nothing to lift).
    deeper = [s for s in movables if graph.stations[s].y >= trunk_y + 1.5 * y_spacing]
    if not deeper:
        return
    # Collision check: ensure shifting up by y_spacing doesn't collide
    # with any non-moving station in the same column.  Build a set of
    # non-moving Ys per column.
    cols_nonmovable: dict[float, set[float]] = defaultdict(set)
    movable_set = set(movables)
    for sid in section.station_ids:
        if sid in movable_set:
            continue
        st = graph.stations.get(sid)
        if st is None or st.is_hidden:
            continue
        cols_nonmovable[round(st.x, 3)].add(round(st.y, 3))
    for sid in movables:
        st = graph.stations[sid]
        new_y = st.y - y_spacing
        col_x = round(st.x, 3)
        if any(
            abs(oy - new_y) < y_spacing / 2 - 0.5
            for oy in cols_nonmovable.get(col_x, set())
        ):
            return  # collision; abort
    # Apply uniform shift to every below-trunk movable.
    for sid in movables:
        graph.stations[sid].y -= y_spacing


def _recenter_loop_side_stations(graph: MetroGraph) -> None:
    """Reposition fan-out side stations on the centre of their loop run.

    A "loop side station" is an off-trunk station fed by exactly one
    on-trunk predecessor and feeding exactly one on-trunk successor,
    forming a diamond loop with the trunk.  ``propd``, ``dream`` and
    ``DESeq2`` in the differential section are the canonical example:
    each takes the trunk bundle into limma's column, runs horizontally
    off the trunk for one slot, then rejoins limma's outgoing trunk at
    ``annotate``.

    Layer-based X placement puts these stations at ``layer * x_spacing``
    relative to the section's entry, ignoring asymmetry in the routing
    diagonals.  When the source-side diagonal is shorter than the
    target-side diagonal (e.g. wide join-station labels widen the
    target-side gap), the side station appears visibly biased toward
    the source.  Recompute its X as the midpoint of the loop's two
    diagonal corner Xs so it sits centred on the horizontal run.

    Honours the same constraints as the routing pass:
    - ``MIN_STRAIGHT_PORT`` / ``MIN_STRAIGHT_EDGE`` at endpoints.
    - Source label clearance at the fork station.
    - Target label clearance at the join station.
    - ``DIAGONAL_RUN`` length for the 45-degree transition.

    A second pass aligns trunk-Y stations that share the same
    ``(predecessor, successor)`` column with off-trunk siblings (e.g.
    ``limma`` shares its column with ``DESeq2``, ``dream`` and
    ``propd``) so column-mates land at the same X regardless of
    whether they sit on or off the trunk row.

    No-op for any station that doesn't form a clean two-edge loop, and
    skipped when shifting would leave fewer than ``DIAGONAL_RUN`` worth
    of horizontal room on either side.
    """
    # Single pass over graph.edges to accumulate fork/join sets.
    # (Adjacency itself is served by graph.edges_from / edges_to.)
    fork_targets: dict[str, set[str]] = defaultdict(set)
    join_sources: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges:
        fork_targets[e.source].add(e.target)
        join_sources[e.target].add(e.source)
    fork_stations = {sid for sid, t in fork_targets.items() if len(t) > 1}
    join_stations = {sid for sid, s in join_sources.items() if len(s) > 1}

    # Minimum recenter delta below which we'd be moving the station
    # without enough visual benefit to justify breaking any incidental
    # column alignment with stacked siblings.  Anything smaller is in
    # the noise where the layer-X placement already reads as centred.
    min_recenter_delta = DIAGONAL_RUN / 3.0

    # Pass 1: re-centre off-trunk loop side stations using the diagonal
    # corner geometry.
    for section in graph.sections.values():
        if section.bbox_h <= 0 or section.direction not in ("LR", "RL"):
            continue
        port_ids = section.port_ids
        # Each side station: not a port, not hidden, has exactly one
        # incoming edge and one outgoing edge, both endpoints sit on
        # the section trunk Y (single source / single target on-trunk).
        for sid in section.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.is_hidden:
                continue
            ins = graph.edges_to(sid)
            outs = graph.edges_from(sid)
            if len(ins) != 1 or len(outs) != 1:
                continue
            src_id = ins[0].source
            tgt_id = outs[0].target
            src = graph.stations.get(src_id)
            tgt = graph.stations.get(tgt_id)
            if src is None or tgt is None:
                continue
            # Side station must sit OFF the trunk Y of its source and
            # target (a vertical hop is needed at both endpoints).
            if abs(src.y - tgt.y) > 0.5:
                # Source and target aren't on the same trunk row, so
                # this isn't a simple horizontal loop side station.
                continue
            trunk_y = src.y
            if abs(st.y - trunk_y) < 0.5:
                continue  # Already on trunk, no loop.
            # Both endpoints must lie strictly to opposite sides of the
            # side station (a real horizontal loop, not a U-turn).
            if not ((src.x < st.x < tgt.x) or (tgt.x < st.x < src.x)):
                continue
            # Require at least one OFF-TRUNK sibling sharing the same
            # single src and tgt: this is what makes the station part
            # of a genuine parallel fan-out where the column is owned
            # by the loop, not by an unrelated trunk station that just
            # happens to share the layer-X.  Single side branches (e.g.
            # ``search`` paired with the trunk continuation ``align``)
            # carry no fan, and recentering them off the column where
            # the trunk station sits visibly breaks the layout.
            has_off_trunk_sibling = False
            for other_sid in section.station_ids:
                if other_sid == sid:
                    continue
                other = graph.stations.get(other_sid)
                if other is None or other.is_port or other.is_hidden:
                    continue
                if abs(other.y - trunk_y) < 0.5:
                    continue  # on-trunk co-loopers don't establish a fan
                other_ins = graph.edges_to(other_sid)
                other_outs = graph.edges_from(other_sid)
                other_srcs = {e.source for e in other_ins}
                other_tgts = {e.target for e in other_outs}
                if other_srcs == {src_id} and other_tgts == {tgt_id}:
                    has_off_trunk_sibling = True
                    break
            if not has_off_trunk_sibling:
                continue
            # Compute the two diagonal corner Xs using routing's
            # placement rule (see _compute_diagonal_placement).
            corner_left = _loop_corner_x(
                src, st, fork_stations, join_stations, role="src"
            )
            corner_right = _loop_corner_x(
                st, tgt, fork_stations, join_stations, role="tgt"
            )
            if corner_left is None or corner_right is None:
                continue
            # Ensure room on both sides for the horizontal run after
            # the new midpoint.  Skip if the move would push the
            # station past either corner.
            midpoint = (corner_left + corner_right) / 2.0
            if not (
                min(corner_left, corner_right)
                <= midpoint
                <= max(corner_left, corner_right)
            ):
                continue
            # Skip moves smaller than the minimum visual-benefit
            # threshold: they trade an imperceptible re-centre for a
            # visible column break against on-trunk co-loopers (e.g.
            # rnaseq_lite ``star_align`` ↔ ``hisat_align``).
            if abs(midpoint - st.x) < min_recenter_delta:
                continue
            st.x = midpoint

    # Pass 2: snap loop-column-mate stations (trunk-row station and any
    # off-trunk siblings whose multi-edge topology disqualified them
    # from pass 1) to the column X defined by pass-1's clean siblings.
    for section in graph.sections.values():
        if section.bbox_h <= 0 or section.direction not in ("LR", "RL"):
            continue
        port_ids = section.port_ids
        # Determine the section's trunk Y from a horizontal port.
        trunk_y: float | None = None
        for pid in section.entry_ports + section.exit_ports:
            ps = graph.stations.get(pid)
            port = graph.ports.get(pid)
            if (
                ps is not None
                and port is not None
                and port.side in (PortSide.LEFT, PortSide.RIGHT)
            ):
                trunk_y = ps.y
                break
        if trunk_y is None:
            continue

        # Visible trunk-Y predecessor/successor X-extent for a station.
        # Returns ``None`` when the station has no trunk-Y neighbour on
        # one side, or when any visible neighbour sits off the trunk
        # row (off-track inputs anchor the station elsewhere).
        def _column_key(sid: str) -> tuple[float, float] | None:
            pred_x: float | None = None
            succ_x: float | None = None
            for e in graph.edges_to(sid):
                p = graph.stations.get(e.source)
                if p is None or p.is_hidden:
                    continue
                if abs(p.y - trunk_y) > 0.5:
                    return None
                if (
                    pred_x is None
                    or (section.direction == "LR" and p.x > pred_x)
                    or (section.direction == "RL" and p.x < pred_x)
                ):
                    pred_x = p.x
            for e in graph.edges_from(sid):
                t = graph.stations.get(e.target)
                if t is None or t.is_hidden:
                    continue
                if abs(t.y - trunk_y) > 0.5:
                    return None
                if (
                    succ_x is None
                    or (section.direction == "LR" and t.x < succ_x)
                    or (section.direction == "RL" and t.x > succ_x)
                ):
                    succ_x = t.x
            if pred_x is None or succ_x is None:
                return None
            return (round(pred_x, 3), round(succ_x, 3))

        columns: dict[tuple[float, float], list[str]] = defaultdict(list)
        for sid in section.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.is_hidden:
                continue
            key = _column_key(sid)
            if key is None:
                continue
            # Station must sit strictly between its trunk-Y neighbours
            # for the column to be a meaningful horizontal extent.
            pred_x, succ_x = key
            lo, hi = min(pred_x, succ_x), max(pred_x, succ_x)
            if not (lo < st.x < hi):
                continue
            columns[key].append(sid)

        for key, members in columns.items():
            if len(members) < 2:
                continue
            trunk_members: list[str] = []
            anchor_xs: list[float] = []
            for sid in members:
                st = graph.stations[sid]
                if abs(st.y - trunk_y) <= 0.5:
                    trunk_members.append(sid)
                    continue
                # Anchor X must come from a station pass-1 already
                # placed at the loop midpoint; restrict to the same
                # single-in/single-out filter pass-1 uses.
                visible_ins = [
                    e
                    for e in graph.edges_to(sid)
                    if (
                        (gs := graph.stations.get(e.source)) is not None
                        and not gs.is_hidden
                    )
                ]
                visible_outs = [
                    e
                    for e in graph.edges_from(sid)
                    if (
                        (gs := graph.stations.get(e.target)) is not None
                        and not gs.is_hidden
                    )
                ]
                if len(visible_ins) == 1 and len(visible_outs) == 1:
                    anchor_xs.append(st.x)
            if not trunk_members or not anchor_xs:
                continue
            target_x = sum(anchor_xs) / len(anchor_xs)
            pred_x, succ_x = key
            lo, hi = min(pred_x, succ_x), max(pred_x, succ_x)
            if not (lo <= target_x <= hi):
                continue
            for sid in trunk_members:
                graph.stations[sid].x = target_x


def _shift_and_propagate_loop_stations(
    graph: MetroGraph,
    y_spacing: float,
    section_y_padding: float,
    section_y_gap: float,
) -> None:
    """Shift sparse loop-side stations onto a half-pitch Y, then
    propagate any bbox growth to lower rows.

    Two-phase unified helper.  Phase 1 shifts sparse single-line loop
    stations clear of busier siblings' inbound bundles (and may grow
    the section bbox downward).  Phase 2 propagates any bbox growth
    to lower rows so ``section_y_gap`` is preserved; it is a no-op
    when phase 1 didn't grow anything.
    """
    _shift_sparse_loop_stations_to_clear_bundle(graph, y_spacing, section_y_padding)
    _push_lower_rows_after_bbox_grow(graph, section_y_gap)


def _shift_sparse_loop_stations_to_clear_bundle(
    graph: MetroGraph,
    y_spacing: float,
    section_y_padding: float = SECTION_Y_PADDING,
) -> None:
    """Phase 1 of :func:`_shift_and_propagate_loop_stations`.

    Shift single-line loop side stations onto a half-pitch Y when
    their full-row Y collides with a busier sibling's inbound bundle.

    The bypass virtual station mechanism (``_insert_bypass_stations``)
    covers the pred -> exit_port case (e.g. ``annotate`` between limma
    and the section exit).  It does not cover the case where a sparse
    consumer S sits in the same section column band as a busier
    sibling T and shares S's row Y, so the lines bound for T cross S's
    marker bbox on the way in (the ``grea`` / ``decoupler`` pattern).

    For each loop side station S in an LR/RL section -- one incoming
    edge, one outgoing edge, both endpoints on the section trunk Y --
    that:

      * consumes strictly fewer lines than at least one same-row
        sibling T in the same section, and
      * shares the same Y row as that sibling,

    shift S vertically by one full ``y_spacing`` away from the trunk
    on the side it already sits on, so its marker bbox sits clear of
    the sibling's bundle Y range.  The section bbox is grown when
    necessary; phase 2 then closes the row gap that growth opened.
    """
    if y_spacing <= 0:
        return

    def _consumed_lines(sid: str) -> set[str]:
        return {e.line_id for e in graph.edges_to(sid)}

    for section in graph.sections.values():
        if section.bbox_h <= 0 or section.direction not in ("LR", "RL"):
            continue
        port_ids = section.port_ids
        # Trunk Y from the LR/RL ports.
        trunk_y: float | None = None
        for pid in section.entry_ports + section.exit_ports:
            port = graph.ports.get(pid)
            ps = graph.stations.get(pid)
            if (
                port is not None
                and ps is not None
                and port.side in (PortSide.LEFT, PortSide.RIGHT)
            ):
                trunk_y = ps.y
                break
        if trunk_y is None:
            continue

        for sid in section.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.is_hidden or st.off_track:
                continue
            ins = graph.edges_to(sid)
            outs = graph.edges_from(sid)
            if len(ins) != 1 or len(outs) != 1:
                continue
            src = graph.stations.get(ins[0].source)
            tgt = graph.stations.get(outs[0].target)
            if src is None or tgt is None:
                continue
            if abs(src.y - trunk_y) > 0.5 or abs(tgt.y - trunk_y) > 0.5:
                continue
            # S must sit clearly off the trunk and share its Y row
            # with a same-section sibling whose inbound bundle is
            # busier than S's.
            dy = st.y - trunk_y
            if abs(dy) < 0.5:
                continue
            s_lines = _consumed_lines(sid)
            sibling: Station | None = None
            for sib_id in section.station_ids:
                if sib_id == sid or sib_id in port_ids:
                    continue
                sib = graph.stations.get(sib_id)
                if (
                    sib is None
                    or sib.is_port
                    or sib.is_hidden
                    or sib.off_track
                    or sib.is_terminus
                ):
                    continue
                if abs(sib.y - st.y) > 0.5:
                    continue
                sib_lines = _consumed_lines(sib_id)
                if len(sib_lines) > len(s_lines):
                    sibling = sib
                    break
            if sibling is None:
                continue
            # Shift S one full ``y_spacing`` further FROM the trunk on
            # the side S is already on.  This lifts S clear of the
            # busier sibling's bundle Y range (which is centred at
            # the row Y with up to ``max_offset`` of extra height for
            # the line stack).  A half-pitch shift would leave S on a
            # half-grid Y; the half-grid offset is reserved for the
            # 2-branch symmetric fan case, so single sparse-loop
            # stations must land on a full grid row.
            shift = y_spacing if dy > 0 else -y_spacing
            new_y = st.y + shift
            # Grow the section bbox so the standard ``section_y_padding``
            # sits between the shifted station's marker edge and the
            # bbox edge.  The earlier ``+ STATION_RADIUS_APPROX`` -only
            # buffer kept the validator happy but left the bbox flush
            # against the station marker, breaking the visual padding
            # invariant other sections satisfy after
            # ``_shrink_bboxes_to_content_bottom``.
            edge_pad = STATION_RADIUS_APPROX + section_y_padding
            sec_top = section.bbox_y
            sec_bottom = section.bbox_y + section.bbox_h
            if new_y < sec_top + edge_pad:
                grow = sec_top + edge_pad - new_y
                section.bbox_y -= grow
                section.bbox_h += grow
            elif new_y > sec_bottom - edge_pad:
                grow = new_y - (sec_bottom - edge_pad)
                section.bbox_h += grow
            st.y = new_y


def _predicted_bypass_bottom_in_row(
    graph: MetroGraph, row: int
) -> dict[tuple[int, int], float]:
    """Predict bypass U-route bottom Ys for edges anchored in *row*.

    Mirrors ``layout.routing.common.bypass_bottom_y`` for layout-time
    prediction: returns ``{(lo, hi): max(intervening_bottoms) + BYPASS_CLEARANCE}``
    for each edge whose endpoints (after walking junctions) resolve to
    same-row sections spanning more than one column with at least one
    intervening section.  Empty when *row* has no bypass-eligible edges.
    """
    sections_in_row = [
        s for s in graph.sections.values() if s.grid_row == row and s.bbox_w > 0
    ]
    if not sections_in_row:
        return {}

    def _node_section(node_id: str):
        st = graph.stations.get(node_id) or graph.ports.get(node_id)
        if st is None:
            return None
        sec_id = getattr(st, "section_id", None)
        return graph.sections.get(sec_id) if sec_id else None

    resolve_cache: dict[tuple[str, bool], Section | None] = {}

    def _resolve(node_id: str, upstream: bool, visited: set[str] | None = None):
        key = (node_id, upstream)
        if key in resolve_cache:
            return resolve_cache[key]
        if visited is None:
            visited = set()
        if node_id in visited:
            return None
        visited.add(node_id)
        sec = _node_section(node_id)
        if sec is None:
            edges = graph.edges_to(node_id) if upstream else graph.edges_from(node_id)
            for e in edges:
                nb = e.source if upstream else e.target
                sec = _resolve(nb, upstream, visited)
                if sec is not None:
                    break
        resolve_cache[key] = sec
        return sec

    per_span: dict[tuple[int, int], float] = {}
    for edge in graph.edges:
        src_sec = _resolve(edge.source, upstream=True)
        tgt_sec = _resolve(edge.target, upstream=False)
        if src_sec is None or tgt_sec is None:
            continue
        if src_sec.grid_row != row or tgt_sec.grid_row != row:
            continue
        if abs(src_sec.grid_col - tgt_sec.grid_col) <= 1:
            continue
        lo, hi = sorted((src_sec.grid_col, tgt_sec.grid_col))
        intervening = [s for s in sections_in_row if lo < s.grid_col < hi]
        if not intervening:
            continue
        bot = max(s.bbox_y + s.bbox_h for s in intervening) + BYPASS_CLEARANCE
        if bot > per_span.get((lo, hi), 0.0):
            per_span[(lo, hi)] = bot
    return per_span


def _aggregate_bypass_spans(
    graph: MetroGraph, upper_sections: list[Section]
) -> dict[tuple[int, int], float]:
    """Aggregate bypass span->bottom predictions across upper sections.

    A row-spanning section carries its bypass routes from its start row
    down to the row below its end row, so the prediction must key off
    ``grid_row`` (start), not the end row.
    """
    combined: dict[tuple[int, int], float] = {}
    for upper_start_row in {s.grid_row for s in upper_sections}:
        for span, bot in _predicted_bypass_bottom_in_row(
            graph, upper_start_row
        ).items():
            if bot > combined.get(span, 0.0):
                combined[span] = bot
    return combined


def _push_lower_rows_after_bbox_grow(graph: MetroGraph, section_y_gap: float) -> None:
    """Push lower-row sections down when an upper-row bbox grows.

    Shared helper called by stages that may grow a section's
    ``bbox_h`` downward after row offsets are already fixed (e.g.
    ``_shift_and_propagate_loop_stations`` at Stage 6.14, the sparse
    loop-station shift).  Row offsets were fixed earlier by
    ``_compute_section_offsets`` from pre-grow bbox heights, so the
    section below a grown one can end up sitting closer than
    ``section_y_gap`` from the new bbox bottom.

    For each row ``r >= 1``, measure the deficit between the lowest
    bbox bottom of sections ending at row ``r - 1`` and the top of
    sections at row ``r``, but only count pairs whose column spans
    overlap.  Two sections that share a vertical edge in column space
    must keep ``section_y_gap`` between them; sections in different
    columns can sit with smaller (or no) vertical separation without
    visual interference.  If a positive deficit remains, shift row
    ``r`` and below downward by that deficit (sections + stations +
    ports).  Junctions live in inter-section space and are reproduced
    by routing.
    """
    if not graph.sections:
        return

    sections_by_row_start: dict[int, list[Section]] = defaultdict(list)
    for s in graph.sections.values():
        sections_by_row_start[s.grid_row].append(s)
    if not sections_by_row_start:
        return
    max_row = max(s.grid_row + s.grid_row_span - 1 for s in graph.sections.values())

    def _cols_overlap(a: Section, b: Section) -> bool:
        a_start = a.grid_col
        a_end = a_start + a.grid_col_span - 1
        b_start = b.grid_col
        b_end = b_start + b.grid_col_span - 1
        return not (a_end < b_start or b_end < a_start)

    for r in range(1, max_row + 1):
        lower = sections_by_row_start.get(r, [])
        if not lower:
            continue
        ending_at_prev = [
            s
            for s in graph.sections.values()
            if s.grid_row + s.grid_row_span - 1 == r - 1 and s.bbox_h > 0
        ]
        if not ending_at_prev:
            continue
        bypass_by_span = _aggregate_bypass_spans(graph, ending_at_prev)

        # Only consider column-overlapping (upper, lower) pairs for
        # deficit computation: a tall upper-row bbox that lives in a
        # different column from the lower-row content does not need
        # additional vertical clearance to satisfy the row gap.
        deficit = 0.0
        for us in ending_at_prev:
            for ls in lower:
                if ls.bbox_h <= 0:
                    continue
                if not _cols_overlap(us, ls):
                    continue
                upper_bot = us.bbox_y + us.bbox_h
                lower_top = ls.bbox_y
                d = (upper_bot + section_y_gap) - lower_top
                if d > deficit:
                    deficit = d
        # Bypass routes do not need column overlap with the upper-row
        # endpoint bbox; they only need column overlap with the lower
        # section they would otherwise crowd against.
        for (lo, hi), bypass_bot in bypass_by_span.items():
            for ls in lower:
                if ls.bbox_h <= 0:
                    continue
                ls_lo = ls.grid_col
                ls_hi = ls.grid_col + ls.grid_col_span - 1
                if ls_hi < lo or ls_lo > hi:
                    continue
                d = (bypass_bot + section_y_gap) - ls.bbox_y
                if d > deficit:
                    deficit = d
        if deficit <= 0.5:
            continue

        shifted_section_ids = {
            sid for sid, s in graph.sections.items() if s.grid_row >= r
        }
        for sid in shifted_section_ids:
            graph.sections[sid].bbox_y += deficit
        shifted_station_ids = set()
        for sid in shifted_section_ids:
            shifted_station_ids.update(graph.sections[sid].station_ids)
        for stid in shifted_station_ids:
            st = graph.stations.get(stid)
            if st is not None:
                st.y += deficit
            port = graph.ports.get(stid)
            if port is not None:
                port.y += deficit


def _loop_corner_x(
    a: Station,
    b: Station,
    fork_stations: set[str],
    join_stations: set[str],
    role: str,
) -> float | None:
    """Compute the diagonal corner X for a single edge a->b.

    Mirrors ``_compute_diagonal_placement`` in
    ``layout/routing/core.py``: places the diagonal centred near the
    fork (when a is a fork station) or near the join (when b is a
    join station), with MIN_STRAIGHT endpoint clearance and optional
    label clearance.  Returns the corner X on the side opposite to
    ``role``: ``role='src'`` returns the corner near b (target side
    of edge a->b, i.e. the LEFT corner of the loop b is part of);
    ``role='tgt'`` returns the corner near a (source side of edge
    a->b, i.e. the RIGHT corner of the loop a is part of).
    """
    sx, _ = a.x, a.y
    tx, _ = b.x, b.y
    if abs(tx - sx) < 1e-6:
        return None
    sign = 1.0 if tx > sx else -1.0
    src_min = CURVE_RADIUS + MIN_STRAIGHT_PORT if a.is_port else MIN_STRAIGHT_EDGE
    tgt_min = CURVE_RADIUS + MIN_STRAIGHT_PORT if b.is_port else MIN_STRAIGHT_EDGE
    # Label clearance at fork/join stations (per _route_diagonal).
    if a.id in fork_stations and a.label.strip():
        src_min = max(src_min, label_text_width(a.label) / 2)
    if b.id in join_stations and b.label.strip():
        tgt_min = max(tgt_min, label_text_width(b.label) / 2)
    half_diag = DIAGONAL_RUN / 2
    is_fork = a.id in fork_stations
    is_join = b.id in join_stations
    if is_fork:
        mid = sx + sign * (src_min + half_diag)
    elif is_join:
        mid = tx - sign * (tgt_min + half_diag)
    else:
        mid = (sx + tx) / 2.0
    # Clamp to keep minimum straight endpoint runs.
    if sign > 0:
        diag_start = max(mid - half_diag, sx + src_min)
        diag_end = min(mid + half_diag, tx - tgt_min)
    else:
        diag_start = min(mid - sign * half_diag, sx - src_min)
        diag_end = max(mid + sign * half_diag, tx + tgt_min)
    # role='src' returns the END of the diagonal (corner near b),
    # role='tgt' returns the START of the diagonal (corner near a).
    return diag_end if role == "src" else diag_start


def _lift_would_cause_uturn(
    graph: MetroGraph, station_id: str, section_id: str, anchor_y: float
) -> bool:
    """Return True when lifting *station_id* above ``anchor_y`` would
    force its incoming bundle to make a U-turn.

    A station U-turns when every external feeder sits at Y >= anchor_y:
    the line bundle has to climb from the section's entry port (anchored
    at the row's trunk Y) up to the lifted station, then back down to
    rejoin the trunk for downstream stations.  When two or more feeders
    share that situation, the upward climb visibly bends the bundle
    against the trunk and may cross sibling routes that stay at trunk Y.

    Returns False when there's no risk (no feeders, single feeder, or
    any feeder sits above the anchor giving the bundle a reason to climb).
    """
    junction_ids = graph.junction_ids
    seen: set[str] = set()
    feeder_ys: list[float] = []

    def _collect(node_id: str) -> None:
        for edge in graph.edges_to(node_id):
            src_id = edge.source
            if src_id in seen:
                continue
            seen.add(src_id)
            if src_id in junction_ids:
                _collect(src_id)
                continue
            src = graph.stations.get(src_id)
            if src is None:
                continue
            if src.is_port:
                _collect(src_id)
                continue
            if src.section_id == section_id:
                continue
            feeder_ys.append(src.y)

    _collect(station_id)
    if len(feeder_ys) < 2:
        return False
    return all(y >= anchor_y - 0.5 for y in feeder_ys)


def _snap_all_y_to_grid(graph: MetroGraph, y_spacing: float) -> None:
    """Snap every station and port Y to the nearest row-wide grid slot.

    Earlier phases (``_align_row_trunk_ys``, port-snap, downstream
    alignment) compute shifts that don't respect the grid pitch, so
    stations can land at fractional Ys (e.g. ``298.785`` when the pitch
    is 55).  This final pass restores a clean grid by:

    1. Grouping sections by row.  Sections sharing a row from
       ``_align_row_y_grids`` use the row's ``slot_spacing`` as pitch
       and snap to a single origin so trunks stay co-linear across the
       row.  Sections without a row grid entry are treated as their
       own one-section group at the input ``y_spacing``.
    2. Finding the group's grid origin as the mode of ``y % pitch``
       across ALL non-port, on-track stations in the group.  Using a
       global mode prevents per-section origins from drifting (which
       would kink the trunk between sections).
    3. Snapping every station and LEFT/RIGHT port in the group to the
       nearest ``origin + n * pitch``, bounded by half a pitch so
       adjacency cannot flip.

    Two exclusions preserve deliberate non-grid Ys:

    * LR/RL exit ports on TB-direction sections were placed by
      ``_resolve_tb_exit_y`` at the receiving section's entry-port Y
      (in a different row).  Snapping them to the TB's own row grid
      reintroduces the kink the alignment removed.
    * Stations that act as a convergence point for two or more inbound
      sources at different Ys (fan-in midpoint) carry geometric meaning
      that snapping destroys.

    Fan-out divergence hubs (stations whose Y sits strictly between
    targets above and below) are snapped to grid like other stations.
    The downstream column-centring pass identifies the snap-induced
    flat connection from such a fork hub to one of its targets and
    declines to treat it as a chain predecessor, so the target column
    still centres.

    Groups with no on-grid majority are left untouched.
    """
    if y_spacing <= 0:
        return
    # Map each convergence station/port to the set of source Ys it
    # converges (recorded pre-snap so the midpoint can be restored
    # after sources move).
    convergence_sources = _convergence_source_ys(graph)
    # Divergence anchors (fan-out hubs sitting between target Ys) are
    # snapped to grid like everyone else: the routing column-centring
    # pass treats their incidental flat connection (induced by snap)
    # as non-chain so the target column still centres correctly.
    groups: dict[object, tuple[float, list[str]]] = {}
    grouped_ids: set[str] = set()
    for row, info in (graph._row_y_grid_info or {}).items():
        pitch = info.get("slot_spacing", y_spacing)
        sec_ids = list(info.get("section_ids", []))
        groups[("row", row)] = (pitch, sec_ids)
        grouped_ids.update(sec_ids)
    for section in graph.sections.values():
        if section.id not in grouped_ids:
            groups[("solo", section.id)] = (y_spacing, [section.id])

    for pitch, sec_ids in groups.values():
        half = pitch / 2.0
        # Collect non-port, on-track station Ys across the whole group
        # to estimate the row's shared grid offset.  Off-track stations
        # were lifted by Stage 5.2 relative to their consumers; they
        # snap to the same grid (so the y_spacing gap above the
        # consumer is preserved) but don't influence the origin.
        residues: Counter[float] = Counter()
        per_section_ports: dict[str, set[str]] = {}
        half_grid_ids = graph.half_grid_station_ids
        for sec_id in sec_ids:
            section = graph.sections.get(sec_id)
            if section is None or section.bbox_h <= 0:
                continue
            port_ids = section.port_ids
            per_section_ports[sec_id] = port_ids
            for sid in section.station_ids:
                if sid in port_ids:
                    continue
                if sid in half_grid_ids:
                    # Half-grid stations sit at origin + 0.5 * pitch by
                    # design; don't let them shift the row's grid origin.
                    continue
                st = graph.stations.get(sid)
                if st is None or st.off_track:
                    continue
                residues[round(st.y % pitch, 3)] += 1
        if not residues:
            continue
        origin_r, top = residues.most_common(1)[0]
        if top < 2 and len(residues) > 1:
            continue

        def _snap(
            y: float, origin: float = origin_r, p: float = pitch, h: float = half
        ) -> float:
            snapped = origin + round((y - origin) / p) * p
            return snapped if abs(snapped - y) <= h + 1e-6 else y

        for sec_id, port_ids in per_section_ports.items():
            section = graph.sections.get(sec_id)
            if section is None:
                continue
            is_tb_section = section.direction == "TB"
            for sid in section.station_ids:
                if sid in port_ids:
                    continue
                if sid in half_grid_ids:
                    continue
                st = graph.stations.get(sid)
                if st is None:
                    continue
                if sid in convergence_sources:
                    continue
                st.y = _snap(st.y)
            for pid in port_ids:
                port = graph.ports.get(pid)
                port_st = graph.stations.get(pid)
                if port is None or port_st is None:
                    continue
                if port.side not in (PortSide.LEFT, PortSide.RIGHT):
                    continue
                # TB exit ports are anchored to the downstream entry-port
                # Y by _resolve_tb_exit_y; preserve that alignment.
                if is_tb_section and not port.is_entry:
                    continue
                if pid in convergence_sources:
                    continue
                port_st.y = _snap(port_st.y)

    # Restore convergence midpoints after snap: if a convergence
    # target's source Ys moved during snap, re-place the target at the
    # new midpoint so a fan-in still visually converges symmetrically.
    for target_id, src_ids in convergence_sources.items():
        st = graph.stations.get(target_id)
        if st is None or st.off_track:
            continue
        # Convergence sources whose snap displaced them away from their
        # original Y may break the midpoint relationship; recompute
        # from the post-snap source coordinates.
        new_src_ys = [graph.stations[sid].y for sid in src_ids if sid in graph.stations]
        if len(set(round(y, 3) for y in new_src_ys)) < 2:
            continue
        midpoint = (max(new_src_ys) + min(new_src_ys)) / 2.0
        st.y = midpoint


def _snap_canvas_y_to_grid(
    graph: MetroGraph,
    y_spacing: float,
    section_y_padding: float,
) -> None:
    """Final pass: align canvas-wide so stations land on integer y_spacing.

    The user rule is that real stations sit at integer multiples of
    ``y_spacing`` from a consistent canvas origin.  Earlier phases
    (Stage 6.4 ``_snap_all_y_to_grid`` + Stage 6.4's junction repos)
    produce a per-row grid, but late helpers can still shift the whole
    canvas by a non-grid amount.  Notably ``_shift_graph_into_canvas``
    can shift by ``section_y_padding - min_bbox_y`` which is not a
    multiple of ``y_spacing`` when padding is not a multiple of the
    pitch (default 50 / 40 = half-grid drift).

    Detection: collect the residue ``station.y % y_spacing`` for every
    real (non-port, non-off-track, non-half-grid, non-convergence)
    station.  If a single residue covers
    ``>= CANVAS_GRID_SHIFT_THRESHOLD`` of the population (default 85%),
    the canvas as a whole is uniformly off-grid by that residue.
    Compute the smallest signed shift ``delta`` such that:

      * ``(residue + delta) % y_spacing == 0`` (residue returns to grid)
      * ``min(section.bbox_y) + delta >= section_y_padding`` (top
        margin preserved)

    Apply ``delta`` to every station, port, junction (via bbox + offset
    chain) and section bbox.  If the dominant residue does NOT meet
    threshold, no shift is applied: the per-section snap from Stage
    6.4 is honoured as the best-effort alignment.
    """
    if y_spacing <= 0 or not graph.sections:
        return
    half_grid_ids = graph.half_grid_station_ids
    convergence_sources = _convergence_source_ys(graph)
    residues: Counter[float] = Counter()
    for st in graph.stations.values():
        if st.is_port or st.off_track:
            continue
        if st.id in half_grid_ids or st.id in convergence_sources:
            continue
        residues[round(st.y % y_spacing, 3)] += 1
    total = sum(residues.values())
    if total == 0:
        return
    mode_residue, mode_count = residues.most_common(1)[0]
    if mode_count / total < CANVAS_GRID_SHIFT_THRESHOLD:
        return
    if abs(mode_residue) < 1e-3 or abs(mode_residue - y_spacing) < 1e-3:
        return  # already on grid

    # Two candidate shifts: down by `-mode_residue`, or up by
    # `y_spacing - mode_residue`.  Prefer the one that preserves the
    # top margin; among equal choices prefer the smaller absolute shift.
    min_top = _min_section_bbox_top(graph, section_y_padding)
    shift_down = -mode_residue
    shift_up = y_spacing - mode_residue
    candidates: list[float] = []
    if min_top + shift_down >= section_y_padding - 1e-6:
        candidates.append(shift_down)
    if min_top + shift_up >= section_y_padding - 1e-6:
        candidates.append(shift_up)
    if not candidates:
        # Neither preserves the margin; pick the up-shift since
        # shifting down would clip the canvas.
        candidates.append(shift_up)
    shift = min(candidates, key=abs)
    if abs(shift) < 1e-6:
        return
    _translate_graph_y(graph, shift)
    # Junctions ride the same shift via _position_junctions, which keys
    # off the (now-shifted) exit/entry port Ys.
    _position_junctions(graph)


def _convergence_source_ys(graph: MetroGraph) -> dict[str, list[str]]:
    """Return {target_id: [source_station_ids]} for fan-in convergences.

    A station/port qualifies as a convergence target when it has two or
    more inbound real-station predecessors at distinct Ys and the
    target's own Y is the midpoint of those sources' Ys.  Snapping such
    a target to a grid slot pulls it off the midpoint, forcing a
    formerly-symmetric merge into an asymmetric one.

    Walks one step back through junctions to identify the real
    predecessors so fan-in via a single junction is still detected.
    """
    junction_ids = graph.junction_ids
    inbound: dict[str, set[str]] = defaultdict(set)
    for edge in graph.edges:
        src_id = edge.source
        if src_id in junction_ids:
            for e2 in graph.edges_to(src_id):
                pre = graph.stations.get(e2.source)
                if pre is None or pre.is_port:
                    continue
                inbound[edge.target].add(e2.source)
        else:
            src = graph.stations.get(src_id)
            if src is None or src.is_port:
                continue
            inbound[edge.target].add(src_id)

    convergence: dict[str, list[str]] = {}
    for target_id, src_ids in inbound.items():
        if len(src_ids) < 2:
            continue
        st = graph.stations.get(target_id)
        if st is None:
            continue
        src_ys = sorted({round(graph.stations[sid].y, 3) for sid in src_ids})
        if len(src_ys) < 2:
            continue
        midpoint = (src_ys[0] + src_ys[-1]) / 2.0
        # Treat as a convergence only when the target sits at the
        # midpoint of the source Y range (within a small tolerance).
        # Stations that just happen to receive multiple inbound edges
        # but sit on a single track (e.g. fan-in to the existing trunk)
        # are excluded so they remain on-grid.
        if abs(st.y - midpoint) < 1.0:
            convergence[target_id] = sorted(src_ids)
    return convergence


def _divergence_target_ys(graph: MetroGraph) -> set[str]:
    """Return station/port ids that are fan-out divergence anchors.

    A station/port qualifies as a divergence anchor when it has two or
    more outbound real-station successors at distinct Ys and the
    station's own Y lies strictly between at least one successor above
    and one successor below.  Snapping such a hub onto one of those
    successor tracks converts that outbound diagonal into a flat
    segment, which the downstream routing centring pass treats as a
    chain predecessor and consequently refuses to centre the
    successor's column.

    Walks one step forward through junctions to identify the real
    successors so fan-out via a single junction is still detected.
    """
    junction_ids = graph.junction_ids
    outbound: dict[str, set[str]] = defaultdict(set)
    for edge in graph.edges:
        tgt_id = edge.target
        if tgt_id in junction_ids:
            for e2 in graph.edges_from(tgt_id):
                post = graph.stations.get(e2.target)
                if post is None or post.is_port:
                    continue
                outbound[edge.source].add(e2.target)
        else:
            tgt = graph.stations.get(tgt_id)
            if tgt is None or tgt.is_port:
                continue
            outbound[edge.source].add(tgt_id)

    anchors: set[str] = set()
    for src_id, tgt_ids in outbound.items():
        if len(tgt_ids) < 2:
            continue
        st = graph.stations.get(src_id)
        if st is None:
            continue
        tgt_ys = sorted({round(graph.stations[sid].y, 3) for sid in tgt_ids})
        if len(tgt_ys) < 2:
            continue
        # Only treat as an anchor when the station sits strictly between
        # at least one outbound target above and one below.  Hubs sitting
        # at or beyond either extreme can snap freely - the snap won't
        # collapse a diagonal onto a target track.
        sy = st.y
        has_below = any(ty < sy - 0.5 for ty in tgt_ys)
        has_above = any(ty > sy + 0.5 for ty in tgt_ys)
        if has_below and has_above:
            anchors.add(src_id)
    return anchors


def _section_bundle_lines(graph: MetroGraph, section: Section) -> set[str]:
    """Return the set of line IDs crossing a section's LEFT/RIGHT ports."""
    bundle: set[str] = set()
    for pid in list(section.entry_ports) + list(section.exit_ports):
        port = graph.ports.get(pid)
        if port is None or port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        bundle.update(graph.station_lines(pid))
    return bundle


def _redistribute_fanout_siblings(graph: MetroGraph, y_spacing: float) -> None:
    """Symmetrically distribute fan-out siblings around a trunk junction.

    Active when ``graph.center_ports`` is True.  For each LR/RL section
    in the grid, iterate by column: a column qualifies as a fan-out
    junction when it has exactly one station whose line set equals the
    section's full LEFT/RIGHT bundle (the trunk junction) AND at least
    one sibling whose line set is a strict subset of the bundle.

    In those columns, the trunk station is pinned at its current Y and
    the strict-subset siblings are redistributed in alternating slots
    ``+1, -1, +2, -2, ...`` at ``y_spacing`` pitch above and below it.

    Strict scoping: only stations in a trunk-junction column AND with
    a strict-subset line set are moved.  File inputs, processing
    chains, fan-in stations, columns without a unique trunk, and
    siblings carrying the full bundle (linear pass-throughs) are left
    in place so non-fan-out topologies keep their natural Y ordering.

    Additionally, a sibling is only redistributed when it has at
    least one predecessor in the edge graph.  This excludes columns
    of source stations (file inputs, in-degree 0) that happen to sit
    in a column with a full-bundle station: with no upstream
    producer, they aren't fan-out branches and must stay on their
    per-line track Y so they line up with their downstream consumers.
    Siblings fed by a different predecessor than the trunk (but still
    fed by something) are real fan-out branches arriving via separate
    upstream methods and DO participate in the symmetric fan.

    No-op when ``--no-center-ports`` is set, when a section has no
    qualifying trunk-junction column, or when there are no
    strict-subset siblings.
    """
    if not graph.center_ports:
        return
    grid_sec_ids = _grid_group_section_ids(graph)
    if not grid_sec_ids:
        return

    for section in graph.sections.values():
        if (
            section.id not in grid_sec_ids
            or section.direction not in ("LR", "RL")
            or section.bbox_h <= 0
        ):
            continue
        bundle = _section_bundle_lines(graph, section)
        if not bundle:
            continue
        port_ids = section.port_ids

        # Group non-port, on-track stations by column x.  Off-track
        # stations (file inputs lifted above their consumer) are placed
        # by ``_lift_off_track_stations`` and must not occupy a column
        # slot here.
        cols: dict[float, list[str]] = defaultdict(list)
        for sid in section.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.off_track:
                continue
            cols[round(st.x, 3)].append(sid)

        for sids in cols.values():
            # Identify trunk station in this column: lines == bundle, unique.
            trunks = [s for s in sids if set(graph.station_lines(s)) == bundle]
            if len(trunks) != 1:
                continue
            trunk_sid = trunks[0]
            trunk_y = graph.stations[trunk_sid].y
            # Fan-out siblings: strict subset of bundle (skip full-bundle
            # pass-throughs and orphan stations with no lines).  Require
            # at least one predecessor so source stations (file inputs
            # with no inbound edges) stay on their per-line track Y
            # instead of being pulled to a uniform fan around an
            # unrelated trunk.  Siblings whose predecessor differs
            # from the trunk's are still real fan-out branches (e.g.
            # methods fed by separate upstream stations within the
            # same upstream section) and DO participate.
            siblings = [
                s
                for s in sids
                if s != trunk_sid
                and set(graph.station_lines(s))
                and set(graph.station_lines(s)) < bundle
                and graph.edges_to(s)
            ]
            if not siblings:
                continue
            siblings.sort(key=lambda s: graph.stations[s].y)
            for i, sid in enumerate(siblings, 1):
                k = (i + 1) // 2
                sign = 1 if (i % 2 == 1) else -1
                graph.stations[sid].y = trunk_y + sign * k * y_spacing


def _apply_half_grid_2branch_symfan(
    graph: MetroGraph, y_spacing: float, section_y_padding: float = SECTION_Y_PADDING
) -> None:
    """Compact 2-branch symfan sections onto half-pitch offsets.

    For every section that satisfies ``_section_symfan_uses_half_grid``
    (exactly two on-track non-terminus branch stations sharing a column,
    no off-track inputs), this places the two branches at
    ``trunk_y +/- 0.5 * y_spacing`` regardless of what the per-column
    redistribute passes did.

    Why a dedicated phase: ``_redistribute_full_bundle_columns`` and
    ``_recenter_full_bundle_columns`` gate on ``_grid_group_section_ids``
    (sections that share a row with at least one other section), so a
    section sitting alone on its row never participates.  The 2-branch
    symfan case is well-defined regardless of row membership, so this
    phase fires on the section directly.

    Trunk anchor preference (in order):
      1. LR/RL entry port Y (the inter-section bundle line).
      2. LR/RL exit port Y.
      3. Midpoint of the two branch stations' current Ys.

    The branches are marked in ``graph.half_grid_station_ids`` so the
    subsequent ``_snap_all_y_to_grid`` pass leaves their half-pitch
    offsets intact (and ignores them when computing the row grid
    origin).
    """
    if y_spacing <= 0:
        return
    for section in graph.sections.values():
        if section.bbox_h <= 0 or section.direction not in ("LR", "RL"):
            continue
        if not _section_symfan_uses_half_grid(graph, section):
            continue

        port_ids = section.port_ids
        branches: list[Station] = []
        for sid in section.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if (
                st is None
                or st.is_port
                or st.is_hidden
                or st.off_track
                or st.is_terminus
            ):
                continue
            branches.append(st)
        if len(branches) != 2:
            continue

        # Trunk Y from LR/RL ports (preferred) or the branches' midpoint.
        trunk_y: float | None = None
        for pid in section.entry_ports:
            p = graph.ports.get(pid)
            ps = graph.stations.get(pid)
            if (
                p is not None
                and ps is not None
                and p.side in (PortSide.LEFT, PortSide.RIGHT)
            ):
                trunk_y = ps.y
                break
        if trunk_y is None:
            for pid in section.exit_ports:
                p = graph.ports.get(pid)
                ps = graph.stations.get(pid)
                if (
                    p is not None
                    and ps is not None
                    and p.side in (PortSide.LEFT, PortSide.RIGHT)
                ):
                    trunk_y = ps.y
                    break
        if trunk_y is None:
            trunk_y = (branches[0].y + branches[1].y) / 2.0

        branches.sort(key=lambda s: s.y)
        branches[0].y = trunk_y - 0.5 * y_spacing
        branches[1].y = trunk_y + 0.5 * y_spacing
        graph.half_grid_station_ids.update(b.id for b in branches)

        # Half-grid branches consume half a y_spacing above and below
        # the trunk instead of a full slot.  Shrink the bbox top to match
        # the new compact extent.  All real (non-port) content sits
        # between branches[0].y and branches[1].y, so the bbox top
        # should be branches[0].y - section_y_padding.  Preserve the
        # current padding by computing it from existing bbox geometry.
        content_ys = [
            graph.stations[sid].y
            for sid in section.station_ids
            if sid in graph.stations and not graph.stations[sid].is_port
        ]
        if content_ys:
            new_top = min(content_ys) - section_y_padding
            delta = new_top - section.bbox_y
            if delta > 0.5:
                section.bbox_y = new_top
                section.bbox_h = max(0.0, section.bbox_h - delta)


def _section_symfan_uses_half_grid(graph: MetroGraph, section: Section) -> bool:
    """Return True when a section's symfan should use half-pitch offsets.

    Trigger conditions (must all hold):
      - Section has exactly two real "branch" stations: on-track,
        non-port, non-hidden, non-terminus internal stations sharing
        a single X column.  Terminus icons (file outputs) and hidden
        convergence stations are excluded - they're downstream join
        points that don't constrain symfan spacing.
      - No off-track stations exist in the section (no input rows
        sitting in the participants' Y band).
      - The section has no other columns with multiple branch stations
        (this is the only fan, so the section height is bounded by
        these two stations).

    When the trigger fires the two branch stations are placed at
    ``trunk_y +/- 0.5 * y_spacing`` instead of the default
    ``trunk_y +/- 1 * y_spacing``, so the section needs only one
    vertical grid unit instead of two.

    Trunk Y itself is unchanged.  The branches sit at half-pitch
    relative to the row grid; ``_snap_all_y_to_grid`` skips them via
    ``graph.half_grid_station_ids``.
    """
    port_ids = section.port_ids
    branches: list[Station] = []
    has_off_track = False
    by_col: dict[float, int] = defaultdict(int)
    for sid in section.station_ids:
        if sid in port_ids:
            continue
        st = graph.stations.get(sid)
        if st is None or st.is_port or st.is_hidden:
            continue
        if st.off_track:
            has_off_track = True
            continue
        if st.is_terminus:
            # Terminus icons (file outputs) sit downstream of the fan
            # and are not symfan participants.
            continue
        branches.append(st)
        by_col[round(st.x, 3)] += 1
    if has_off_track or len(branches) != 2:
        return False
    if abs(branches[0].x - branches[1].x) >= 0.5:
        return False
    # No other column may have a multi-branch population (would force
    # full-grid height anyway).  With exactly two branches sharing one
    # column this is implicit, but the check is cheap and future-proofs
    # the trigger when terminus filtering changes.
    return all(count <= 2 for count in by_col.values())


def _fan_offsets(n: int) -> list[int]:
    """Symmetric vertical slot offsets for ``n`` stations fanned about a
    trunk Y: even ``n`` leaves the trunk row empty (-n//2..-1, 1..n//2),
    odd ``n`` keeps a middle station on the trunk (-(n//2)..n//2).
    """
    if n % 2 == 0:
        return list(range(-(n // 2), 0)) + list(range(1, n // 2 + 1))
    return list(range(-(n // 2), n // 2 + 1))


def _redistribute_full_bundle_columns(graph: MetroGraph, y_spacing: float) -> None:
    """Fan a full-bundle column around the trunk Y.

    Active when ``graph.center_ports`` is True.  Handles columns where
    every on-track station carries the full section bundle (so no
    unique trunk junction exists for ``_redistribute_fanout_siblings``
    to anchor on).  Stations are placed symmetrically around a trunk Y
    derived from the section's LR ports (or other full-bundle stations).

    A relaxed mode also fires when the column has at least one
    full-bundle station AND every non-full column-mate is a
    strict-subset sibling with a predecessor (i.e. a real fan-out
    branch arriving via a separate upstream method, not a source
    file).  In that mixed-bundle case every column-mate participates
    in the symmetric fan, so a minor side branch (e.g. a single-line
    method joining three full-bundle methods) slots into the
    arrangement instead of stranding at the bottom of the section.

    Even count leaves the trunk row empty (``trunk_y ± s, ± 2s, ...``);
    odd count keeps a middle station at ``trunk_y`` with the rest
    flanking.  Fires on both terminal (Reporting-style) and
    non-terminal (Functional-style) sections; columns containing a
    non-full, predecessorless station (a source file with no inbound
    edges) are left untouched so file-input stacks keep their per-line
    track Y.
    """
    if not graph.center_ports:
        return
    grid_sec_ids = _grid_group_section_ids(graph)
    if not grid_sec_ids:
        return

    for section in graph.sections.values():
        if (
            section.id not in grid_sec_ids
            or section.direction not in ("LR", "RL")
            or section.bbox_h <= 0
        ):
            continue
        bundle = _section_bundle_lines(graph, section)
        if not bundle:
            continue
        port_ids = section.port_ids

        cols: dict[float, list[str]] = defaultdict(list)
        for sid in section.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.off_track:
                # Off-track inputs (file icons) are placed later by
                # ``_lift_off_track_stations`` and must not occupy a
                # column slot in the fan-out logic.
                continue
            cols[round(st.x, 3)].append(sid)

        def _has_pred(sid: str) -> bool:
            return bool(graph.edges_to(sid))

        full_by_col = {
            x: [s for s in sids if set(graph.station_lines(s)) == bundle]
            for x, sids in cols.items()
        }
        # Snapshot pre-fan Ys so iteration order of columns doesn't
        # drift the trunk reference: a later column must not see an
        # earlier column's already-fanned positions.
        pre_fan_y = {
            sid: graph.stations[sid].y for sids in cols.values() for sid in sids
        }
        port_ys = [
            graph.ports[pid].y
            for pid in port_ids
            if graph.ports.get(pid) is not None
            and graph.ports[pid].side in (PortSide.LEFT, PortSide.RIGHT)
        ]

        # A column participates in the section-wide symfan when it has
        # at least one full-bundle station to anchor on AND any other
        # column-mates are non-source subset siblings (real fan-out
        # branches with predecessors, not file inputs).  Source files
        # in a column with a full-bundle station leave it ineligible
        # so they stay on their per-line track Y.
        col_eligible: dict[float, list[str]] = {}
        for x, sids in cols.items():
            full = full_by_col[x]
            non_full = [s for s in sids if s not in full]
            ok = bool(full) and all(
                set(graph.station_lines(s))
                and set(graph.station_lines(s)) < bundle
                and _has_pred(s)
                for s in non_full
            )
            if ok and len(sids) >= 2:
                col_eligible[x] = sids
        # Suppress the column when at least one full-bundle column-mate
        # would otherwise be the unique trunk for a SINGLE sibling and
        # there's no other full-bundle column in the section to fix
        # the row-wide anchor (handed off to fanout_siblings instead).
        # In practice we still fire whenever another column has >=2
        # full-bundle stations, so all full-bundle columns share a
        # consistent trunk_y.
        any_all_full_col = any(
            len(full_by_col[x]) >= 2 and len(full_by_col[x]) == len(cols[x])
            for x in cols
        )

        for x, sids in col_eligible.items():
            full = full_by_col[x]
            non_full = [s for s in sids if s not in full]
            # Strict all-full columns always fire (the original
            # behaviour).  Mixed columns (full + non-source siblings)
            # only fire when another column in the section is
            # all-full, so we have a consistent trunk_y for the row
            # and don't accidentally fan single trunk + 1 sibling
            # cases that belong to ``_redistribute_fanout_siblings``.
            all_full = not non_full
            if not all_full and not any_all_full_col:
                continue
            participants = list(sids)
            # Trunk Y is the section's LR port Y when available (the
            # inter-section bundle line) so all full-bundle columns
            # in the section share a single trunk reference.  Falls
            # back to the median pre-fan Y of full-bundle stations in
            # other columns when the section has no LR ports.
            if port_ys:
                trunk_y = sum(port_ys) / len(port_ys)
            else:
                others = sorted(
                    pre_fan_y[s]
                    for ox, sids in full_by_col.items()
                    if ox != x
                    for s in sids
                )
                if not others:
                    continue
                trunk_y = others[len(others) // 2]
            participants.sort(key=lambda s: pre_fan_y[s])
            n = len(participants)
            offsets = _fan_offsets(n)
            for sid, off in zip(participants, offsets):
                graph.stations[sid].y = trunk_y + off * y_spacing


def _recenter_full_bundle_columns(graph: MetroGraph, y_spacing: float) -> None:
    """Re-fan full-bundle station columns around the row's final trunk Y.

    Late-pass companion to ``_redistribute_full_bundle_columns``.  The
    early pass uses the section's local LR port Y as the symmetric
    centre, which becomes stale when subsequent phases shift the
    section relative to the row trunk (e.g. terminal sections whose
    sole LR port doesn't match the bundle line entering from upstream).

    For each LR/RL grid section, locate the inter-section bundle Y from
    the entry/exit port station Y (which by this point sits on the
    row's bundle Y after row alignment).  Then re-distribute each
    column of >=2 full-bundle stations around that anchor at
    ``y_spacing`` pitch, preserving the order produced by the first
    pass.

    No-op when the existing layout is already symmetric around the
    anchor; bbox heights are not adjusted because earlier compaction
    already sized the band to fit two slots either side of the trunk.
    """
    grid_sec_ids = _grid_group_section_ids(graph)
    if not grid_sec_ids:
        return

    for section in graph.sections.values():
        if (
            section.id not in grid_sec_ids
            or section.direction not in ("LR", "RL")
            or section.bbox_h <= 0
        ):
            continue
        bundle = _section_bundle_lines(graph, section)
        if not bundle:
            continue
        port_ids = section.port_ids

        cols: dict[float, list[str]] = defaultdict(list)
        for sid in section.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.off_track:
                continue
            cols[round(st.x, 3)].append(sid)

        def _has_pred(sid: str) -> bool:
            return bool(graph.edges_to(sid))

        full_by_col = {
            x: [s for s in sids if set(graph.station_lines(s)) == bundle]
            for x, sids in cols.items()
        }

        # Trunk anchor: prefer the entry port station's Y, which after
        # row alignment sits on the row's bundle line.  Fall back to
        # the exit port station, then a single-station full-bundle
        # column (natural pass-through), then the median Y.
        anchor_y: float | None = None
        for pid in section.entry_ports:
            p = graph.ports.get(pid)
            ps = graph.stations.get(pid)
            if p is None or ps is None:
                continue
            if p.side in (PortSide.LEFT, PortSide.RIGHT):
                anchor_y = ps.y
                break
        if anchor_y is None:
            for pid in section.exit_ports:
                p = graph.ports.get(pid)
                ps = graph.stations.get(pid)
                if p is None or ps is None:
                    continue
                if p.side in (PortSide.LEFT, PortSide.RIGHT):
                    anchor_y = ps.y
                    break
        if anchor_y is None:
            single_ys = [
                graph.stations[full[0]].y
                for full in full_by_col.values()
                if len(full) == 1
            ]
            if single_ys:
                anchor_y = sorted(single_ys)[len(single_ys) // 2]
        if anchor_y is None:
            continue

        # Mirror the gate from ``_redistribute_full_bundle_columns``:
        # strict (all column-mates full) always fires; mixed (full +
        # non-source siblings) fires only when another column has
        # >=2 all-full stations, so we don't accidentally pull
        # fanout_siblings columns onto a different anchor.
        any_all_full_col = any(
            len(full_by_col[x]) >= 2 and len(full_by_col[x]) == len(cols[x])
            for x in cols
        )

        for x, full in full_by_col.items():
            non_full = [s for s in cols[x] if s not in full]
            mixed_ok = (
                bool(full)
                and non_full
                and all(
                    set(graph.station_lines(s))
                    and set(graph.station_lines(s)) < bundle
                    and _has_pred(s)
                    for s in non_full
                )
            )
            all_full = len(full) >= 2 and len(full) == len(cols[x])
            if not (all_full or (mixed_ok and any_all_full_col)):
                continue
            participants = list(full) + (non_full if mixed_ok else [])
            if len(participants) < 2:
                continue
            participants.sort(key=lambda s: graph.stations[s].y)
            n = len(participants)
            offsets = _fan_offsets(n)
            for sid, off in zip(participants, offsets):
                graph.stations[sid].y = anchor_y + off * y_spacing


def _shrink_and_tighten_rows(
    graph: MetroGraph,
    section_y_padding: float,
    section_y_gap: float,
) -> None:
    """Shrink section bbox bottoms to content, then pull lower rows up
    to close any slack the shrink revealed.

    Two-phase unified helper:

    Phase 1 - shrink:
      Resize each section's ``bbox_h`` so the bottom sits
      ``section_y_padding`` below the bottom-most station / port,
      shrinking when content rose during earlier passes
      (``_fan_source_inputs_upward``, ``_recenter_full_bundle_columns``)
      and growing when ``_snap_all_y_to_grid`` snapped a station
      downward.  Station Ys are unchanged so trunk alignment is
      preserved.  Never trims past the maximum bbox bottom of any
      row-mate (another section whose ``grid_row`` equals this
      section's starting row, accounting for the other section's
      ``grid_row_span``); trimming below a row-mate would undo
      intentional bottom alignment from Stage 6.5 or TB-rowspan
      neighbours.  The check is keyed on this section's STARTING row
      rather than its full row-span -- a rowspan>1 LR sidebar whose
      content fits in one row is not pinned to neighbours in the
      claimed-but-unfilled extra rows.

    Phase 2 - tighten:
      ``_compute_section_offsets`` sizes ``row_heights[r]`` from the
      pre-shrink bbox heights, and a rowspan section that ends at row
      ``r`` inflates the height further to fit its (then-tall) bbox.
      Once phase 1 collapses bbox bottoms to actual content, row
      ``r + 1`` can sit below empty space.  For each row pair, close
      any slack beyond ``section_y_gap`` by shifting lower rows
      (sections + stations + ports) upward.  The tighten step needs
      every row's shrink to finish first so the row-gap deficit is
      measurable against the final bbox bottoms, which is why this
      runs as a second pass over the same graph rather than per
      section.
    """
    _shrink_bboxes_to_content_bottom(graph, section_y_padding)
    _tighten_lower_rows_after_shrink(graph, section_y_gap)


def _shrink_bboxes_to_content_bottom(
    graph: MetroGraph, section_y_padding: float
) -> None:
    """Phase 1 of :func:`_shrink_and_tighten_rows`.

    Resize each section's ``bbox_h`` so the bottom sits
    ``section_y_padding`` below the bottom-most station / port.  See
    the parent helper's docstring for the full contract; this
    function is split out so the runtime guard at "after Stage 6.13"
    still bisects to a meaningful intermediate state.
    """

    def _row_mate_bottoms(section: Section) -> list[float]:
        # Two policies depending on this section's direction:
        #
        # TB sections (folds) get their bbox grown by ``section_y_gap``
        # in section_placement so they visually span into the next row's
        # target.  Their intended bottom is the target row-mate's bottom,
        # which is in a different grid row but Y-overlapping.  Honour
        # Y-overlap for these so the bottom-alignment from Stage 6.5 /
        # the fold extension survives.
        #
        # LR/RL sections use ONLY their STARTING grid row to find
        # row-mates.  Counting this section's rowspan would pull in
        # sections from rows the rowspan claims but doesn't fill -- a
        # rowspan=2 LR sidebar whose content fits in row 0 must not be
        # pinned to a row-1 neighbour just because its declared span
        # overlaps row 1.  Y-overlap is intentionally excluded for
        # LR/RL: a stale pre-shrink bbox would otherwise be
        # self-protecting (the overlap blocks the shrink that would
        # remove the overlap).
        my_grid_row = section.grid_row if section.grid_row >= 0 else None
        my_y_top = section.bbox_y
        my_y_bot = section.bbox_y + section.bbox_h
        # LR/RL sections with grid coords match on starting row only;
        # TB sections (and any unplaced section) fall back to bbox-Y
        # overlap.  See block comment above for the why.
        use_grid = section.direction != "TB" and my_grid_row is not None
        out: list[float] = []
        for other in graph.sections.values():
            if other.id == section.id or other.bbox_h <= 0:
                continue
            o_y_bot = other.bbox_y + other.bbox_h
            if use_grid and other.grid_row >= 0:
                o_grid_top = other.grid_row
                o_grid_bot = other.grid_row + max(1, other.grid_row_span)
                mate = o_grid_top <= my_grid_row < o_grid_bot
            else:
                mate = other.bbox_y < my_y_bot and o_y_bot > my_y_top
            if mate:
                out.append(o_y_bot)
        return out

    v_curve_clearance = CURVE_RADIUS + MIN_STATION_FLAT_LENGTH / 2
    for section in graph.sections.values():
        if section.bbox_h <= 0:
            continue
        section_dir = section.direction or "LR"
        # Each non-port station reserves at least ``section_y_padding`` below
        # its marker; a TB/BT terminus whose icons hang below reserves their
        # full vertical extent instead.  (Overhang is 0 for LR/RL, so this
        # stays byte-identical there.)
        content_bots = [
            graph.stations[sid].y
            + max(
                section_y_padding,
                _terminus_y_overhang(graph.stations[sid], section_dir, graph)[1],
            )
            for sid in section.station_ids
            if (
                sid in graph.stations
                and not graph.stations[sid].is_port
                and not sid.startswith("__bypass_")
            )
        ]
        bypass_max_ys = [
            graph.stations[sid].y
            for sid in section.station_ids
            if sid in graph.stations and sid.startswith("__bypass_")
        ]
        port_max_ys = [
            graph.stations[sid].y
            for sid in section.station_ids
            if sid in graph.stations and graph.stations[sid].is_port
        ]
        if not content_bots:
            continue
        content_bot = max(content_bots)
        if bypass_max_ys:
            content_bot = max(content_bot, max(bypass_max_ys) + v_curve_clearance)
        if port_max_ys:
            content_bot = max(content_bot, max(port_max_ys))
        current_bot = section.bbox_y + section.bbox_h
        if content_bot > current_bot + 0.5:
            section.bbox_h = content_bot - section.bbox_y
            continue
        desired_bot = content_bot
        mate_bots = _row_mate_bottoms(section)
        if mate_bots:
            desired_bot = max(desired_bot, max(mate_bots))
        new_h = desired_bot - section.bbox_y
        if new_h < section.bbox_h - 0.5:
            section.bbox_h = max(0.0, new_h)


def _section_content_top_target(
    graph: MetroGraph,
    section: Section,
    section_y_padding: float,
    section_y_gap: float,
) -> float | None:
    """Return the bbox top that gives ``section`` a full top padding band.

    Mirror of the bottom anchor in :func:`_shrink_bboxes_to_content_bottom`:
    the top sits ``section_y_padding`` above the highest content marker
    (bypass helpers use curve-only clearance; ports must stay inside).

    The bound against the row above reserves ``section_y_gap +
    SECTION_HEADER_PROTRUSION``, not just the bbox gap: the section's
    header badge protrudes ``SECTION_HEADER_PROTRUSION`` above its bbox
    top, and inter-section routes dip into the gap, so reserving the
    protrusion keeps the grow from crowding the badge into a route.

    Returns ``None`` when the section has no real content to anchor to.
    """
    content_min_ys = [
        graph.stations[sid].y
        for sid in section.station_ids
        if (
            sid in graph.stations
            and not graph.stations[sid].is_port
            and not sid.startswith("__bypass_")
        )
    ]
    if not content_min_ys:
        return None
    bypass_min_ys = [
        graph.stations[sid].y
        for sid in section.station_ids
        if sid in graph.stations and sid.startswith("__bypass_")
    ]
    port_min_ys = [
        graph.stations[sid].y
        for sid in section.station_ids
        if sid in graph.stations and graph.stations[sid].is_port
    ]
    v_curve_clearance = CURVE_RADIUS + MIN_STATION_FLAT_LENGTH / 2
    target = min(content_min_ys) - section_y_padding
    if bypass_min_ys:
        target = min(target, min(bypass_min_ys) - v_curve_clearance)
    if port_min_ys:
        target = min(target, min(port_min_ys))

    above_bots: list[float] = []
    for other in graph.sections.values():
        if other.id == section.id or other.bbox_w <= 0 or other.bbox_h <= 0:
            continue
        if other.grid_row + max(1, other.grid_row_span) != section.grid_row:
            continue
        if not _bbox_cols_overlap(other, section):
            continue
        above_bots.append(other.bbox_y + other.bbox_h)
    if above_bots:
        target = max(
            target, max(above_bots) + section_y_gap + SECTION_HEADER_PROTRUSION
        )
    return target


def _grow_bboxes_to_content_top(
    graph: MetroGraph,
    section_y_padding: float,
    section_y_gap: float,
) -> None:
    """Grow section bbox tops so the highest marker keeps a full padding band.

    Symmetric counterpart to :func:`_shrink_bboxes_to_content_bottom`.
    Fan-redistribution passes (Stages 4.9 / 4.10 / 6.7 / 6.11) can lift a
    branch station above the content-top line the bbox was sized for,
    leaving the topmost marker crowded against the bbox top while the
    bottom keeps its full ``section_y_padding`` band -- so a fan
    symmetric about the trunk reads as pushed up within its box
    (issue #406).

    Grow-only: the top is never lowered, so intentional top-flush row
    alignment from :func:`_top_align_row_bboxes_only` is preserved.  TOP
    ports follow the new edge via :func:`_grow_section_bbox_upward`.
    """
    for section in graph.sections.values():
        if section.bbox_h <= 0:
            continue
        target = _section_content_top_target(
            graph, section, section_y_padding, section_y_gap
        )
        if target is not None and target < section.bbox_y - 0.5:
            _grow_section_bbox_upward(graph, section, target)


def _tighten_lower_rows_after_shrink(graph: MetroGraph, section_y_gap: float) -> None:
    """Phase 2 of :func:`_shrink_and_tighten_rows`.

    Pull lower-row sections up to close the slack revealed once
    phase 1 collapsed bbox bottoms.  For each row ``r >= 1``, measure
    the gap between row ``r``'s current top and the max bbox bottom
    of sections that *end* at row ``r - 1``.  Rowspan sections that
    *extend into* row ``r`` are excluded -- their bbox bottom is now
    content-bounded, not row-bounded, so they no longer constrain
    row ``r``'s top.  Any slack beyond ``section_y_gap`` is closed
    by shifting sections in row ``r`` and below (along with their
    stations and ports) upward by that amount.  Junctions live in
    inter-section space and routing recomputes after layout, so
    their positions are left alone.
    """
    if not graph.sections:
        return

    from nf_metro.layout.section_placement import _wrap_bundle_row_minimums

    # An inter-row wrap bundle needs a wider gap than the bare
    # ``section_y_gap`` so its horizontal run clears both bounding
    # sections; honour that minimum here so tightening doesn't reclaim
    # the space ``_enforce_min_row_gaps`` reserved at placement.
    wrap_min = _wrap_bundle_row_minimums(graph)

    sections_by_start_row: dict[int, list[Section]] = defaultdict(list)
    sections_by_end_row: dict[int, list[Section]] = defaultdict(list)
    for s in graph.sections.values():
        if s.bbox_h <= 0:
            continue
        sections_by_start_row[s.grid_row].append(s)
        sections_by_end_row[s.grid_row + s.grid_row_span - 1].append(s)
    if not sections_by_start_row:
        return
    max_row = max(sections_by_end_row)

    for r in range(1, max_row + 1):
        lower = sections_by_start_row.get(r, [])
        ending_at_prev = sections_by_end_row.get(r - 1, [])
        if not lower or not ending_at_prev:
            continue
        max_above_bot = max(s.bbox_y + s.bbox_h for s in ending_at_prev)
        # Bypass routes dip below intervening bboxes into the inter-row
        # gap; tightening must not pull lower rows up into them.
        bypass_spans = _aggregate_bypass_spans(graph, ending_at_prev)
        effective_floor = max(max_above_bot, max(bypass_spans.values(), default=0.0))
        current_top = min(s.bbox_y for s in lower)
        target_gap = max(section_y_gap, wrap_min.get((r - 1, r), 0.0))
        slack = current_top - (effective_floor + target_gap)
        if slack <= 0.5:
            continue

        for s in graph.sections.values():
            if s.grid_row < r:
                continue
            s.bbox_y -= slack
            for stid in s.station_ids:
                st = graph.stations.get(stid)
                if st is not None:
                    st.y -= slack


def _align_terminus_to_upstream(graph: MetroGraph) -> None:
    """Pin a single downstream terminus to its sole upstream's Y.

    After ``_recenter_full_bundle_columns`` re-pitches fanned columns,
    a single-station downstream column (e.g. a ``file`` terminus
    consuming the fanned station's output) can be left at its pre-fan Y,
    so the connecting line and the icon caption drift away from the
    source station.  When the downstream station has exactly one in-
    section predecessor, snap it back onto the source's Y so its file
    icon sits level with the station it follows.

    Skips the pin when the target Y is already occupied by a sibling
    in the same X column: when a source fans out to a chain station
    (``bundle -> bundle_zip``) AND a terminus (``report_html``), pulling
    the terminus to the source's Y collides with the chain station that
    sits there.  Leaving the terminus at its grid Y preserves visual
    separation; the diagonal connector to the source is acceptable.
    """
    for section in graph.sections.values():
        if section.direction not in ("LR", "RL"):
            continue
        sec_sids = set(section.station_ids)
        for sid in section.station_ids:
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.off_track:
                continue
            if not st.is_terminus:
                continue
            preds = {
                e.source
                for e in graph.edges_to(sid)
                if e.source in sec_sids and not graph.stations[e.source].is_port
            }
            if len(preds) != 1:
                continue
            src = graph.stations[next(iter(preds))]
            if abs(src.y - st.y) < 0.5:
                continue
            collision = False
            for sib_sid in section.station_ids:
                if sib_sid == sid:
                    continue
                sib = graph.stations.get(sib_sid)
                if sib is None or sib.is_port or sib.is_hidden:
                    continue
                if abs(sib.x - st.x) > 0.5:
                    continue
                if abs(sib.y - src.y) < 0.5:
                    collision = True
                    break
            if collision:
                continue
            st.y = src.y


def _has_horizontal_predecessor_section(graph: MetroGraph, section: Section) -> bool:
    """True if any entry-port predecessor lives in an LR/RL section."""
    for pid in section.entry_ports:
        for edge in graph.edges_to(pid):
            src_port = graph.ports.get(edge.source)
            if not src_port:
                continue
            src_sec = graph.sections.get(src_port.section_id)
            if src_sec and src_sec.direction in ("LR", "RL"):
                return True
    return False


def _layout_single_section(
    graph: MetroGraph,
    section: Section,
    x_spacing: float,
    y_spacing: float,
    section_x_padding: float,
    section_y_padding: float,
) -> MetroGraph | None:
    """Lay out a single section's internal stations and compute its bbox.

    Runs layer/track assignment on the section's real stations, applies
    direction-specific adjustments (RL mirror, TB label extent, entry shifts),
    and computes the section bounding box. Returns the section subgraph with
    positioned stations, or None if the section has no layoutable stations.
    """
    sub = _build_section_subgraph(graph, section)
    if not sub.stations:
        return None

    # Insert phantom pass-throughs into the subgraph (not the main graph)
    # so that lines entering at a deep layer get their own track.
    _insert_phantom_pass_throughs(graph, section, sub)

    layers = assign_layers(sub)

    # Use entry-top ordering when the immediate predecessor section is
    # horizontal (LR/RL), so the entry-connected station stays at the
    # top and aligns with the upstream exit station (#165).  Skip for
    # TB predecessors where vertical entry makes top-biasing inappropriate.
    entry_top = section.direction in (
        "LR",
        "RL",
    ) and _has_horizontal_predecessor_section(graph, section)

    tracks = assign_tracks(sub, layers, entry_top=entry_top)

    if not layers:
        return None

    # Snap phantom pass-throughs' successors to the pass-through track
    # so the trunk line stays horizontal past bypassed stations.
    _align_phantom_pass_throughs(sub, tracks)

    # Compact tracks so widely-spaced line priorities don't inflate
    # the vertical spread.  Gaps larger than LINE_GAP get capped so
    # distant line base tracks don't create excessive whitespace.
    # Off-track stations carry a placeholder track that will be
    # overwritten by Stage 5.2's lift-to-consumer pass, so they must not
    # influence the rank compaction of the on-track stations.
    unique_tracks = sorted(
        {tracks[sid] for sid in tracks if not sub.stations[sid].off_track}
    )
    track_rank: dict[float, float] = {}
    if unique_tracks:
        track_rank[unique_tracks[0]] = 0.0
        for idx in range(1, len(unique_tracks)):
            gap = unique_tracks[idx] - unique_tracks[idx - 1]
            track_rank[unique_tracks[idx]] = track_rank[unique_tracks[idx - 1]] + min(
                gap, LINE_GAP
            )

    # Detect fork/join layers and add extra spacing so stations
    # aren't too close to divergence/convergence points.
    section_sids = set(section.station_ids)
    layer_extra = _compute_fork_join_gaps(
        sub, layers, tracks, x_spacing, graph, section_sids
    )

    # Widen track spacing when multi-line labels need more vertical room
    effective_y_spacing = _multiline_track_spacing(sub, y_spacing)

    # Assign local coordinates based on section direction
    for sid, station in sub.stations.items():
        station.layer = layers.get(sid, 0)
        station.track = tracks.get(sid, 0)
        # Off-track stations get rank 0 here as a placeholder; Stage 5.2
        # overwrites their Y to ``consumer.y - n*y_spacing``.  On-track
        # stations must have a track that made it into the rank map.
        if not station.off_track:
            assert station.track in track_rank, (
                f"on-track station {sid!r} has track {station.track} "
                f"missing from rank map {sorted(track_rank)}"
            )
        rank = track_rank.get(station.track, 0.0)
        if section.direction == "TB":
            station.x = rank * x_spacing
            station.y = station.layer * y_spacing + layer_extra.get(station.layer, 0)
        else:
            station.x = station.layer * x_spacing + layer_extra.get(station.layer, 0)
            station.y = rank * effective_y_spacing

    # Resolve same-cell station collisions: two stations on the same line
    # priority can land on identical (x,y) when the track allocator collapses
    # distinct line tracks at a layer with only one occupant per line.
    _resolve_station_collisions(sub, section, x_spacing, effective_y_spacing)

    # Normalize Y so minimum is 0 (raw tracks can be negative)
    _normalize_min(sub, axis="y")

    # RL: mirror X so layer 0 is rightmost
    if section.direction == "RL":
        _mirror_rl(sub)

    # Normalize local X so leftmost station is at x=0
    _normalize_min(sub, axis="x")

    # Ensure minimum inner extent so stations sit on visible track
    _enforce_min_extent(sub, section, x_spacing, y_spacing)

    # Bypass V helpers (``__bypass_``) have no rendered marker.  Use
    # them to extend the bbox only when V sits beyond the real-station
    # extent, and only by enough for the diversion curve to clear the
    # section edge (~CURVE_RADIUS + half a station flat) - much less
    # than the full station_y_padding (which is reserved for label
    # clearance around real stations).
    real_for_bbox = [
        s for s in sub.stations.values() if not s.id.startswith("__bypass_")
    ]
    if not real_for_bbox:
        real_for_bbox = list(sub.stations.values())
    bypass_v_ys = [s.y for s in sub.stations.values() if s.id.startswith("__bypass_")]
    xs = [s.x for s in real_for_bbox]
    ys = [s.y for s in real_for_bbox]
    extra_label_h = _multiline_label_padding(sub)
    y_pad = section_y_padding + extra_label_h
    v_curve_clearance = CURVE_RADIUS + MIN_STATION_FLAT_LENGTH / 2
    y_min = min(ys)
    y_max = max(ys)
    section.bbox_x = min(xs) - section_x_padding
    section.bbox_w = (max(xs) - min(xs)) + section_x_padding * 2
    bbox_top = y_min - y_pad
    bbox_bot = y_max + y_pad
    if bypass_v_ys:
        # When V sits beyond the real-station extent, use curve-only
        # clearance rather than full label padding: V has no marker,
        # no label, just a curve corner to render past.
        v_min = min(bypass_v_ys)
        v_max = max(bypass_v_ys)
        if v_min < y_min:
            bbox_top = min(bbox_top, v_min - v_curve_clearance)
        if v_max > y_max:
            bbox_bot = v_max + v_curve_clearance
    section.bbox_y = bbox_top
    section.bbox_h = bbox_bot - bbox_top

    # Apply direction-specific bbox adjustments
    _adjust_tb_labels(sub, section, graph)
    _adjust_tb_entry_shifts(section, sub, graph, y_spacing)
    _adjust_lr_entry_inset(sub, section, graph, x_spacing)
    _adjust_lr_exit_gap(sub, section, graph, layers, x_spacing)
    _adjust_lr_label_clearance(sub, section)
    _adjust_terminus_icon_clearance(sub, section, graph)

    return sub


def _resolve_station_collisions(
    sub: MetroGraph,
    section: Section,
    x_spacing: float,
    y_spacing: float,
) -> None:
    """Push stations apart when track compaction collides them in the same cell.

    The track allocator can return identical track values for two stations on
    different lines when each is the sole occupant of its line at a given
    layer (e.g. side-by-side terminus branches). After coordinate assignment
    they end up at the same (x, y), causing visual overlap. This pass detects
    such collisions and shifts the later-defined station along the section's
    secondary axis by one spacing unit, repeating until the cell is unique.
    """
    if section.direction == "TB":
        primary, secondary, primary_step, step = "y", "x", y_spacing, x_spacing
    else:
        primary, secondary, primary_step, step = "x", "y", x_spacing, y_spacing

    EPS = 0.5
    real = [s for s in sub.stations.values() if not s.is_port and not s.is_hidden]
    if len(real) < 2:
        return

    # Group stations by primary-axis bucket (layer column for LR/RL,
    # row for TB).  Use the primary-axis step size; the bucket spans a
    # half-step either side of a layer centre so off-grid layer_extra
    # offsets stay in the same bucket as their layer peers.
    primary_step_norm = max(primary_step, 1.0)
    by_primary: dict[float, list] = {}
    for s in real:
        bucket = round(getattr(s, primary) / primary_step_norm)
        by_primary.setdefault(bucket, []).append(s)

    # Stable tiebreaker so the earlier-defined station keeps its slot
    # when two share a secondary coord (insertion order in sub.stations).
    order = {sid: i for i, sid in enumerate(sub.stations)}

    for stations in by_primary.values():
        if len(stations) < 2:
            continue
        stations.sort(key=lambda s: (getattr(s, secondary), order.get(s.id, 0)))
        used: list[float] = []
        for s in stations:
            pos = getattr(s, secondary)
            while any(abs(pos - u) < step - EPS for u in used):
                pos += step
            if pos != getattr(s, secondary):
                setattr(s, secondary, pos)
            used.append(pos)


def _multiline_track_spacing(sub: MetroGraph, y_spacing: float) -> float:
    """Return effective Y track spacing, widened for multi-line labels.

    When labels from adjacent tracks face each other (one below, one
    above due to layer alternation) the track gap must be large enough
    for both labels plus clearance.  Returns *y_spacing* unchanged when
    no multi-line labels are present.
    """
    max_text_h = FONT_HEIGHT
    for s in sub.stations.values():
        n = s.label.count("\n")
        if n > 0:
            h = FONT_HEIGHT + n * FONT_HEIGHT * LABEL_LINE_HEIGHT
            max_text_h = max(max_text_h, h)

    if max_text_h <= FONT_HEIGHT:
        return y_spacing  # no multi-line labels

    # Worst case: adjacent tracks with labels facing inward.
    # Each side needs label_offset + its text height.
    min_gap = LABEL_OFFSET + max_text_h + LABEL_OFFSET + FONT_HEIGHT + LABEL_MARGIN
    return max(y_spacing, min_gap)


def _multiline_label_padding(sub: MetroGraph) -> float:
    """Return extra bbox Y padding for the tallest multi-line label."""
    max_extra = 0.0
    for s in sub.stations.values():
        n = s.label.count("\n")
        if n > 0:
            extra = n * FONT_HEIGHT * LABEL_LINE_HEIGHT
            max_extra = max(max_extra, extra)
    return max_extra


def _normalize_min(sub: MetroGraph, axis: str) -> None:
    """Shift all stations so the minimum coordinate on the given axis is 0."""
    vals = [getattr(s, axis) for s in sub.stations.values()]
    if vals:
        min_val = min(vals)
        if min_val != 0:
            for s in sub.stations.values():
                setattr(s, axis, getattr(s, axis) - min_val)


def _mirror_rl(sub: MetroGraph) -> None:
    """Mirror X coordinates for RL sections so layer 0 is rightmost.

    Anchors on non-terminus stations so adding terminus layers
    extends leftward without shifting the entry point.
    """
    non_term = [
        s for s in sub.stations.values() if not (s.is_terminus and not s.label.strip())
    ]
    anchor_stations = non_term if non_term else list(sub.stations.values())
    max_x_val = max(s.x for s in anchor_stations)
    for s in sub.stations.values():
        s.x = max_x_val - s.x


def _enforce_min_extent(
    sub: MetroGraph,
    section: Section,
    x_spacing: float,
    y_spacing: float,
) -> None:
    """Ensure minimum inner extent so stations sit on visible track."""
    xs = [s.x for s in sub.stations.values()]
    ys = [s.y for s in sub.stations.values()]
    if section.direction == "TB":
        inner_h = max(ys) - min(ys)
        min_inner_h = y_spacing
        if inner_h < min_inner_h:
            shift = (min_inner_h - inner_h) / 2
            for station in sub.stations.values():
                station.y += shift
    else:
        inner_w = max(xs) - min(xs)
        min_inner_w = x_spacing
        if inner_w < min_inner_w:
            shift = (min_inner_w - inner_w) / 2
            for station in sub.stations.values():
                station.x += shift


def _adjust_tb_labels(
    sub: MetroGraph,
    section: Section,
    graph: MetroGraph,
) -> None:
    """TB sections: expand bbox and shift stations right so labels fit.

    Labels extend leftward from the station (text_anchor=end).
    """
    if section.direction != "TB":
        return

    xs = [s.x for s in sub.stations.values()]
    max_label_extent = 0.0
    for sid, s in sub.stations.items():
        if s.label.strip():
            n_lines = len(sub.station_lines(sid))
            offset_span = (n_lines - 1) * TB_LINE_Y_OFFSET
            extent = offset_span / 2 + 11 + label_text_width(s.label)
            max_label_extent = max(max_label_extent, extent)
    need_left = max_label_extent + LABEL_PAD
    have_left = min(xs) - section.bbox_x
    if need_left > have_left:
        extra = need_left - have_left
        for s in sub.stations.values():
            s.x += extra
        section.bbox_w += extra


def _adjust_tb_entry_shifts(
    section: Section,
    sub: MetroGraph,
    graph: MetroGraph,
    y_spacing: float,
) -> None:
    """Apply TB section entry shifts for perpendicular and cross-column entries."""
    if section.direction != "TB":
        return

    # Perpendicular entry: shift stations down so first station isn't
    # at the entry port (avoiding station-as-elbow).
    has_perp_entry = any(
        graph.ports[pid].side in (PortSide.LEFT, PortSide.RIGHT)
        for pid in section.entry_ports
        if pid in graph.ports
    )
    if has_perp_entry:
        entry_shift = y_spacing * ENTRY_SHIFT_TB
        for s in sub.stations.values():
            s.y += entry_shift
        section.bbox_h += entry_shift

    # Cross-column TOP entry: shift stations down for L-shape routing room.
    has_cross_col_top_entry = False
    for pid in section.entry_ports:
        port = graph.ports.get(pid)
        if not port or port.side != PortSide.TOP:
            continue
        for edge in graph.edges_to(pid):
            src = graph.stations.get(edge.source)
            if src and src.section_id:
                src_sec = graph.sections.get(src.section_id)
                if src_sec and src_sec.grid_col != section.grid_col:
                    has_cross_col_top_entry = True
                    break
        if has_cross_col_top_entry:
            break
    if has_cross_col_top_entry:
        entry_shift = y_spacing * ENTRY_SHIFT_TB_CROSS
        for s in sub.stations.values():
            s.y += entry_shift
        section.bbox_h += entry_shift


def _adjust_lr_entry_inset(
    sub: MetroGraph,
    section: Section,
    graph: MetroGraph,
    x_spacing: float,
) -> None:
    """LR/RL sections: add extra bbox width when entry has curves."""
    if section.direction not in ("LR", "RL"):
        return

    has_perp_entry = any(
        graph.ports[pid].side in (PortSide.TOP, PortSide.BOTTOM)
        for pid in section.entry_ports
        if pid in graph.ports
    )
    if has_perp_entry:
        # Reserve enough width for the perp-entry station shift that creates
        # a gap between the perpendicular entry port and the first station.
        # This ensures the grid column is sized correctly before the shift.
        entry_inset = x_spacing * ENTRY_SHIFT_LR
        section.bbox_w += entry_inset
        return

    # Flow-side entry that fans out to multiple internal stations at
    # different Y positions needs extra room for the diagonal transitions.
    for pid in section.entry_ports:
        if pid not in graph.ports:
            continue
        flow_side = PortSide.LEFT if section.direction == "LR" else PortSide.RIGHT
        if graph.ports[pid].side != flow_side:
            continue
        targets = {
            e.target for e in graph.edges_from(pid) if e.target in section.station_ids
        }
        if len(targets) > 1:
            entry_inset = x_spacing * EXIT_GAP_MULTIPLIER
            # For single-layer sections the asymmetry is very visible,
            # so split the inset between both sides to keep stations
            # visually centered (same logic as _adjust_lr_exit_gap).
            n_layers = len({s.layer for s in sub.stations.values()})
            shift = entry_inset / 2 if n_layers <= 1 else entry_inset
            for s in sub.stations.values():
                s.x += shift
            section.bbox_w += entry_inset
            return


def _adjust_lr_exit_gap(
    sub: MetroGraph,
    section: Section,
    graph: MetroGraph,
    layers: dict[str, int],
    x_spacing: float,
) -> None:
    """LR/RL sections with flow-side exit: add label clearance gap.

    The gap is only added when lines converge from different Y tracks to
    the exit port (requiring diagonal routing).  When all feeder stations
    share the same Y, lines exit straight horizontally and no extra space
    is needed.
    """
    if section.direction not in ("LR", "RL"):
        return

    flow_exit_side = PortSide.RIGHT if section.direction == "LR" else PortSide.LEFT
    flow_exit_port_ids = {
        pid
        for pid in section.exit_ports
        if pid in graph.ports and graph.ports[pid].side == flow_exit_side
    }
    if not flow_exit_port_ids or not layers:
        return

    # Collect Y positions of internal stations that feed into flow-side
    # exit ports.  If they all share the same Y, no diagonal convergence
    # is needed and the gap can be skipped.  When the feeder is a bypass
    # V helper (``__bypass_`` id), trace back to its visible predecessor
    # so the diagonal at the V is collapsed back onto the predecessor's
    # Y - the V exists only because the line couldn't cross a consumer
    # marker, but the diagonal still terminates at a visible station.
    feeder_ys: set[float] = set()
    real_ids = set(sub.stations)
    for pid in flow_exit_port_ids:
        for edge in graph.edges_to(pid):
            if edge.source not in real_ids:
                continue
            src_id = edge.source
            if src_id.startswith("__bypass_"):
                pred_y = None
                for pe in graph.edges_to(src_id):
                    if pe.source in real_ids and not pe.source.startswith("__bypass_"):
                        ps = sub.stations.get(pe.source)
                        if ps is not None:
                            pred_y = ps.y
                            break
                if pred_y is None:
                    continue
                feeder_ys.add(pred_y)
            else:
                feeder_ys.add(sub.stations[src_id].y)

    if len(feeder_ys) <= 1:
        return

    exit_gap = x_spacing * EXIT_GAP_MULTIPLIER

    # For single-layer sections the asymmetry is very visible, so split the
    # gap between both sides to keep the station visually centered.  For
    # multi-layer sections the gap belongs entirely on the exit side.
    n_layers = len(set(layers.values()))
    center = n_layers <= 1

    if section.direction == "LR":
        if center:
            half_gap = exit_gap / 2
            for s in sub.stations.values():
                s.x += half_gap
        section.bbox_w += exit_gap
    else:
        shift = exit_gap / 2 if center else exit_gap
        for s in sub.stations.values():
            s.x += shift
        section.bbox_w += exit_gap


def _adjust_lr_label_clearance(
    sub: MetroGraph,
    section: Section,
) -> None:
    """LR/RL sections: expand bbox so station labels fit within the box.

    Labels are centered on their station. If any label extends past the
    section bbox edge, expand the bbox (and shift stations if needed) so
    that section placement can equalize column widths correctly.
    """
    if section.direction not in ("LR", "RL"):
        return

    margin = LABEL_BBOX_MARGIN
    for s in sub.stations.values():
        if not s.label.strip():
            continue
        half_w = label_text_width(s.label) / 2
        label_left = s.x - half_w - margin
        label_right = s.x + half_w + margin

        if label_left < section.bbox_x:
            deficit = section.bbox_x - label_left
            # Shift all stations right and expand bbox on the left.
            # This moves the current station too, so we recompute
            # label_right below.  Later stations get more left-side
            # clearance, which is safe (they can only trigger further
            # right-side expansion, not undo this shift).
            for st in sub.stations.values():
                st.x += deficit
            section.bbox_w += deficit

        # Recompute after possible left-side shift
        label_right = s.x + half_w + margin
        bbox_right = section.bbox_x + section.bbox_w
        if label_right > bbox_right:
            section.bbox_w = label_right - section.bbox_x


def _terminus_icon_clearance(
    n_icons: int,
    names: list[str] | None = None,
) -> float:
    """Compute clearance needed for *n_icons* file icons side-by-side.

    The base ``TERMINUS_ICON_CLEARANCE`` covers one icon (station_radius +
    gap + icon_width + margin).  Each additional icon adds the per-icon
    centre-to-centre step computed by the renderer's
    ``caption_aware_icon_step`` -- widened when adjacent captions would
    overrun the default ``ICON_INTER_GAP`` step.

    Layout doesn't know the theme, so caption widths are estimated
    using the default label size (14px, matches built-in themes).
    Slight over-budget is harmless: bbox just gets a few extra px of
    right padding.
    """
    if n_icons <= 1:
        return TERMINUS_ICON_CLEARANCE
    from nf_metro.render.constants import ICON_NAME_FONT_SCALE
    from nf_metro.render.svg import caption_aware_icon_step

    safe_names = names or [""] * n_icons
    caption_font_size = 14.0 * ICON_NAME_FONT_SCALE
    name_widths = [len(n) * caption_font_size * 0.55 if n else 0.0 for n in safe_names]
    step = caption_aware_icon_step(safe_names, name_widths, TERMINUS_WIDTH)
    extra = (n_icons - 1) * step
    return TERMINUS_ICON_CLEARANCE + extra


def _terminus_icon_clearance_vertical(
    n_icons: int,
    names: list[str] | None = None,
) -> float:
    """Vertical clearance for *n_icons* file icons stacked along the flow axis.

    TB/BT counterpart of ``_terminus_icon_clearance``: icons stack along Y,
    so each additional icon adds the icon height plus (when captions are
    present) a caption row, matching the renderer's TB step.
    """
    if n_icons <= 1:
        return TERMINUS_ICON_CLEARANCE_V
    caption_room = (
        ICON_CAPTION_GAP + ICON_CAPTION_FONT_HEIGHT if names and any(names) else 0.0
    )
    step = 2 * ICON_HALF_HEIGHT + ICON_INTER_GAP + caption_room
    return TERMINUS_ICON_CLEARANCE_V + (n_icons - 1) * step


def _terminus_icons_extend_forward(is_source: bool, section_dir: str) -> bool:
    """Whether a terminus's icons extend in the section's forward flow.

    Sinks extend forwards (down for TB, right for LR), sources backwards;
    RL/BT mirror that.  Single source of truth for the rule that
    ``render.svg._terminus_icon_centers`` applies on the render side.
    """
    return is_source if section_dir in ("RL", "BT") else not is_source


def _terminus_y_overhang(
    station: Station, section_dir: str, graph: MetroGraph
) -> tuple[float, float]:
    """(above, below) px a TB/BT terminus's icons extend past its marker.

    Returns ``(0.0, 0.0)`` for non-terminus stations and for LR/RL
    sections (whose icons extend horizontally), so content-extent callers
    stay byte-identical there.
    """
    if not station.is_terminus or section_dir not in ("TB", "BT"):
        return 0.0, 0.0
    is_source = not graph.edges_to(station.id)
    extent = _terminus_icon_clearance_vertical(
        len(station.terminus_labels), station.terminus_names
    )
    if _terminus_icons_extend_forward(is_source, section_dir):  # below
        return 0.0, extent
    return extent, 0.0


def _adjust_terminus_icon_clearance(
    sub: MetroGraph,
    section: Section,
    graph: MetroGraph,
) -> None:
    """Expand bbox when terminus file icons would be too close to the edge.

    Terminus icons march along the section's flow axis (horizontally for
    LR/RL, vertically for TB/BT), on the station's "outside": forwards for
    sinks, backwards for sources, with RL/BT mirrored.  When the section
    padding doesn't leave enough room, grow the bbox on the affected side.
    """
    section_dir = section.direction or "LR"
    is_tb = section_dir in ("TB", "BT")

    for station in sub.stations.values():
        if not station.is_terminus:
            continue

        n_icons = len(station.terminus_labels)
        is_source = not graph.edges_to(station.id)
        extends_forward = _terminus_icons_extend_forward(is_source, section_dir)

        if is_tb:
            needed = _terminus_icon_clearance_vertical(n_icons, station.terminus_names)
            if extends_forward:  # icons below the station
                clearance = section.bbox_y + section.bbox_h - station.y
                if clearance < needed:
                    section.bbox_h += needed - clearance
            else:  # icons above the station
                clearance = station.y - section.bbox_y
                if clearance < needed:
                    expand = needed - clearance
                    section.bbox_y -= expand
                    section.bbox_h += expand
        else:
            needed = _terminus_icon_clearance(n_icons, station.terminus_names)
            if not extends_forward:  # icons left of the station
                clearance = station.x - section.bbox_x
                if clearance < needed:
                    expand = needed - clearance
                    section.bbox_x -= expand
                    section.bbox_w += expand
            else:  # icons right of the station
                clearance = section.bbox_x + section.bbox_w - station.x
                if clearance < needed:
                    section.bbox_w += needed - clearance


def _shift_lr_perp_entry_stations(
    graph: MetroGraph,
    x_spacing: float,
) -> None:
    """Shift internal stations in LR/RL sections with perpendicular entry.

    Mirrors ``_adjust_tb_entry_shifts`` for horizontal-flow sections.
    In TB sections the station shift is applied in Stage 1.1, and entry-port
    alignment later overrides the port Y with the upstream source Y,
    creating a gap.  For LR/RL sections no such port-X override exists,
    so we shift stations after port initialisation (Stage 3.1) while ports
    stay put and internal stations move inward.

    The shift is only applied when the gap between the perpendicular entry
    port and the nearest entry-side internal station is smaller than the
    desired gap.  Sections where the gap is already sufficient are left
    untouched.
    """
    desired_gap = x_spacing * ENTRY_SHIFT_LR

    for section in graph.sections.values():
        if section.direction not in ("LR", "RL"):
            continue

        # Collect perpendicular entry port positions
        perp_port_xs: list[float] = []
        for pid in section.entry_ports:
            port = graph.ports.get(pid)
            if port and port.side in (PortSide.TOP, PortSide.BOTTOM):
                perp_port_xs.append(graph.stations[pid].x)
        if not perp_port_xs:
            continue

        # Collect internal station X positions
        port_ids = section.port_ids
        internal_xs: list[float] = []
        for sid in section.station_ids:
            if sid in port_ids:
                continue
            s = graph.stations.get(sid)
            if s and not s.is_port:
                internal_xs.append(s.x)
        if not internal_xs:
            continue

        # Compute the current gap between port and nearest entry-side station
        if section.direction == "LR":
            # Entry is LEFT: port is left of stations
            nearest_x = min(internal_xs)
            port_x = min(perp_port_xs)
            current_gap = nearest_x - port_x
        else:
            # RL: entry is RIGHT: port is right of stations
            nearest_x = max(internal_xs)
            port_x = max(perp_port_xs)
            current_gap = port_x - nearest_x

        shift = desired_gap - current_gap
        if shift <= 0:
            continue  # gap is already sufficient

        # Shift internal stations away from the entry side.
        # Stage 1.1 (_adjust_lr_entry_inset) already reserved bbox space
        # for this shift, so no bbox expansion is needed here.
        for sid in section.station_ids:
            if sid in port_ids:
                continue
            s = graph.stations.get(sid)
            if not s or s.is_port:
                continue
            if section.direction == "LR":
                s.x += shift
            else:
                s.x -= shift


def _required_junction_margin(n: int) -> float:
    """Margin needed so an n-line fan's leftmost lead-in clears the source.

    For an n-line concentric fan-out the per-line ``fan_delta`` stagger
    and per-line ``r_wrap`` curve radius cancel exactly: every line's
    first-corner curve start lands at ``junction.x``.  The required
    clearance therefore depends only on the lead-in length immediately
    before the curve (``CURVE_RADIUS``), not on the fan width.

    Returns ``JUNCTION_MARGIN`` directly - the baseline already exceeds
    the curve-start clearance requirement for any reasonable ``n``.
    The signature keeps a per-junction ``n`` so future routing layouts
    that genuinely depend on fan width can override it without changing
    every call site.
    """
    del n  # currently unused; see docstring
    return JUNCTION_MARGIN


def _junction_outgoing_line_count(graph: MetroGraph, jid: str) -> int:
    """Return the number of distinct line_ids fanning out of *jid*."""
    return len({e.line_id for e in graph.edges_from(jid)}) or 1


def _junction_incoming_line_count(graph: MetroGraph, jid: str) -> int:
    """Return the number of distinct line_ids merging into *jid*."""
    return len({e.line_id for e in graph.edges_to(jid)}) or 1


def _position_junctions(graph: MetroGraph) -> None:
    """Position junction stations at the midpoint of the inter-section gap.

    A junction is where bundled lines diverge to different downstream sections.
    It sits horizontally between the exit port and the entry ports, at the
    exit port's Y coordinate so lines travel straight from exit to junction.

    Merge junctions (N>1 predecessors, 1 entry port successor) are positioned
    at ``max(pred.x) + _required_junction_margin(n)``, y = entry_port.y to
    create a visible single-line segment from merge point to entry.
    """
    for jid in graph.junctions:
        junction = graph.stations.get(jid)
        if not junction:
            continue

        # Collect predecessors and successors
        predecessors: list[Station] = []
        successor_ports: list[Station] = []
        exit_port_id: str | None = None

        for edge in graph.edges_to(jid):
            src = graph.stations.get(edge.source)
            if src:
                predecessors.append(src)
                if src.is_port:
                    exit_port_id = edge.source
        for edge in graph.edges_from(jid):
            tgt = graph.stations.get(edge.target)
            if tgt and tgt.is_port:
                successor_ports.append(tgt)

        # Merge junction: N>1 predecessors, 1 entry port successor
        if len(predecessors) > 1 and len(successor_ports) == 1:
            entry_port = successor_ports[0]
            entry_port_obj = graph.ports.get(entry_port.id)
            if entry_port_obj and entry_port_obj.is_entry:
                _position_merge_junction(
                    junction,
                    predecessors,
                    entry_port,
                    n=_junction_incoming_line_count(graph, jid),
                )
                continue

        # Fan-out junction: 1 exit port predecessor, N>1 entry port successors
        exit_port_x: float | None = None
        exit_port_y: float | None = None
        entry_port_xs: list[float] = []

        for pred in predecessors:
            if pred.is_port:
                exit_port_x = pred.x
                exit_port_y = pred.y

        for succ in successor_ports:
            entry_port_xs.append(succ.x)

        if exit_port_x is not None and exit_port_y is not None and entry_port_xs:
            margin = _required_junction_margin(
                _junction_outgoing_line_count(graph, jid)
            )
            exit_port_obj = graph.ports.get(exit_port_id) if exit_port_id else None
            if exit_port_obj and exit_port_obj.side == PortSide.BOTTOM:
                junction.x = exit_port_x
                junction.y = exit_port_y + margin
            elif exit_port_obj and exit_port_obj.side in (
                PortSide.RIGHT,
                PortSide.LEFT,
            ):
                direction = 1.0 if exit_port_obj.side == PortSide.RIGHT else -1.0
                junction.x = exit_port_x + direction * margin
                junction.y = exit_port_y
            else:
                nearest_entry_x = min(entry_port_xs, key=lambda x: abs(x - exit_port_x))
                direction = 1.0 if nearest_entry_x > exit_port_x else -1.0
                junction.x = exit_port_x + direction * margin
                junction.y = exit_port_y


def _position_merge_junction(
    junction: Station,
    predecessors: list[Station],
    entry_port: Station,
    n: int = 1,
) -> None:
    """Position a merge junction near the entry port it feeds.

    Places at x = max(predecessor.x) + _required_junction_margin(n),
    y = entry_port.y so all converging lines share a visible single-line
    segment into the entry port.  *n* is the number of distinct lines
    merging at the junction; passing 1 falls back to the baseline margin.
    """
    max_pred_x = max(p.x for p in predecessors)
    margin = _required_junction_margin(n)
    # Normal forward fan-in: merge just past the right-most predecessor on its
    # way into a target to the right.  But when the target sits well to the LEFT
    # of the predecessors (a collector like MultiQC fed from across the map),
    # merging at max_pred_x forces the whole merged bundle to backtrack the full
    # width into the entry.  Merge local to the target instead, so only the
    # individual feeders make the long approach and the merge->entry hop is short.
    if entry_port.x < max_pred_x - margin:
        junction.x = entry_port.x - margin
    else:
        junction.x = max_pred_x + margin
    junction.y = entry_port.y


def _resolve_source_section_id(
    graph: MetroGraph, edge_source: str, junction_ids: set[str]
) -> str | None:
    """Resolve the section ID of an edge's source, tracing through junctions.

    For port stations, returns section_id directly. For junctions, follows
    edges backward to find the connected port's section.
    """
    src = graph.stations.get(edge_source)
    if not src:
        return None
    src_section_id = src.section_id
    if edge_source in junction_ids:
        for e2 in graph.edges_to(edge_source):
            s2 = graph.stations.get(e2.source)
            if s2 and s2.section_id:
                src_section_id = s2.section_id
                break
    return src_section_id


def _resolve_source_xy(
    graph: MetroGraph,
    edge_source: str,
    junction_ids: set[str],
    _seen: set[str] | None = None,
) -> tuple[float, float] | None:
    """Return effective (x, y) for an edge source.

    For port stations, returns coordinates directly.  For junctions,
    derives coordinates from the feeding exit port, mirroring
    ``_position_junctions`` logic so that entry-port alignment does
    not depend on junctions being pre-positioned.  Recurses through
    chained junctions (junction-to-junction edges) to find the
    underlying exit port.
    """
    src = graph.stations.get(edge_source)
    if not src:
        return None
    if edge_source not in junction_ids:
        return src.x, src.y

    if _seen is None:
        _seen = set()
    if edge_source in _seen:
        return src.x, src.y
    _seen.add(edge_source)

    # Junction: find the feeding exit port and compute placement.
    chained: list[str] = []
    for e in graph.edges_to(edge_source):
        if e.source in junction_ids:
            chained.append(e.source)
            continue
        exit_st = graph.stations.get(e.source)
        if not exit_st or not exit_st.is_port:
            continue
        exit_port_obj = graph.ports.get(e.source)
        if not exit_port_obj:
            return exit_st.x, exit_st.y
        # Mirror _position_junctions: the resolved junction X must match
        # what _position_junctions would write so that downstream
        # alignment passes consuming this helper see the same coordinate.
        margin = _required_junction_margin(
            _junction_outgoing_line_count(graph, edge_source)
        )
        if exit_port_obj.side == PortSide.BOTTOM:
            return exit_st.x, exit_st.y + margin
        elif exit_port_obj.side == PortSide.RIGHT:
            return exit_st.x + margin, exit_st.y
        elif exit_port_obj.side == PortSide.LEFT:
            return exit_st.x - margin, exit_st.y
        else:
            return exit_st.x + margin, exit_st.y

    # Recurse through chained junctions to find the underlying exit port.
    for js in chained:
        resolved = _resolve_source_xy(graph, js, junction_ids, _seen)
        if resolved is not None and resolved != (0.0, 0.0):
            return resolved

    # Fallback: use junction station's current coordinates.
    return src.x, src.y


def _set_port_y(graph: MetroGraph, port_id: str, y: float) -> None:
    """Set the Y coordinate on both the station and port objects."""
    station = graph.stations.get(port_id)
    port = graph.ports.get(port_id)
    if station:
        station.y = y
    if port:
        port.y = y


def _set_port_x(graph: MetroGraph, port_id: str, x: float) -> None:
    """Set the X coordinate on both the station and port objects."""
    station = graph.stations.get(port_id)
    port = graph.ports.get(port_id)
    if station:
        station.x = x
    if port:
        port.x = x


def _align_entry_ports(graph: MetroGraph, tb_only: bool = False) -> None:
    """Align entry ports with their incoming connection's coordinates.

    LEFT/RIGHT ports: align Y for straight horizontal runs.
    TOP/BOTTOM ports: align X for vertical drops or Y for cross-column.

    With ``tb_only`` set, only ports on TB/BT sections are re-aligned --
    used by the late re-alignment pass (Stage 6.16), which targets the
    perpendicular-entry drift that vertical settling introduces in
    TB sections without disturbing settled LR/RL geometry.
    """
    junction_ids = graph.junction_ids

    for port_id, port in graph.ports.items():
        if not port.is_entry:
            continue

        entry_section = graph.sections.get(port.section_id)
        if not entry_section:
            continue

        if tb_only and entry_section.direction not in ("TB", "BT"):
            continue

        if port.side in (PortSide.LEFT, PortSide.RIGHT):
            _align_lr_entry_port(graph, port_id, port, entry_section, junction_ids)
        elif port.side in (PortSide.TOP, PortSide.BOTTOM):
            _align_tb_entry_port(graph, port_id, port, entry_section, junction_ids)


def _align_lr_entry_port(
    graph: MetroGraph,
    port_id: str,
    port: Port,
    entry_section: Section,
    junction_ids: set[str],
) -> None:
    """Align a LEFT/RIGHT entry port's Y with its incoming source."""
    for edge in graph.edges_to(port_id):
        src = graph.stations.get(edge.source)
        if not src or not (src.is_port or edge.source in junction_ids):
            continue

        # Derive effective source coordinates (computes junction
        # placement on-the-fly so we don't need pre-positioned junctions).
        src_xy = _resolve_source_xy(graph, edge.source, junction_ids)
        if src_xy is None:
            continue
        src_x, src_y = src_xy

        src_section_id = _resolve_source_section_id(graph, edge.source, junction_ids)
        src_section = graph.sections.get(src_section_id) if src_section_id else None
        if not src_section:
            continue

        if entry_section.grid_row != src_section.grid_row:
            break

        # Skip alignment if source Y is too far outside entry section bbox.
        # Allow moderate expansion so ports align when adjacent sections
        # have different track counts (#165).
        entry_station = graph.stations.get(port_id)
        if entry_station:
            bbox_top = entry_section.bbox_y
            bbox_bot = entry_section.bbox_y + entry_section.bbox_h
            max_expand = entry_section.bbox_h * MAX_PORT_ALIGN_BBOX_EXPANSION_FRAC
            if src_y < bbox_top - max_expand or src_y > bbox_bot + max_expand:
                break
            # Expand bbox to contain aligned port if needed
            if src_y < bbox_top or src_y > bbox_bot:
                _expand_bbox_for_y(entry_section, src_y)

        target_y = src_y

        # Clamp for TB sections with perpendicular entry
        if entry_section.direction == "TB" and port.side in (
            PortSide.LEFT,
            PortSide.RIGHT,
        ):
            target_y = _clamp_tb_entry_port(
                graph,
                entry_section,
                target_y,
                edge,
                src,
                junction_ids,
            )

        _set_port_y(graph, port_id, target_y)
        break


def _align_tb_entry_port(
    graph: MetroGraph,
    port_id: str,
    port: Port,
    entry_section: Section,
    junction_ids: set[str],
) -> None:
    """Align a TOP/BOTTOM entry port with its incoming sources."""
    # Collect all incoming sources.  Coordinates are derived via
    # _resolve_source_xy so junctions don't need to be pre-positioned.
    sources: list[tuple[float, float, str | None]] = []
    for edge in graph.edges_to(port_id):
        src = graph.stations.get(edge.source)
        if not src or not (src.is_port or edge.source in junction_ids):
            continue
        src_xy = _resolve_source_xy(graph, edge.source, junction_ids)
        if src_xy is None:
            continue
        src_section_id = _resolve_source_section_id(graph, edge.source, junction_ids)
        sources.append((src_xy[0], src_xy[1], src_section_id))

    if not sources:
        return

    # Check if any source is cross-column
    my_cols = set(
        range(
            entry_section.grid_col,
            entry_section.grid_col + entry_section.grid_col_span,
        )
    )
    is_cross_column = False
    for _, _, src_sid in sources:
        src_sec = graph.sections.get(src_sid) if src_sid else None
        if src_sec:
            src_cols = set(
                range(src_sec.grid_col, src_sec.grid_col + src_sec.grid_col_span)
            )
            if not (src_cols & my_cols):
                is_cross_column = True
                break

    if is_cross_column:
        # Cross-column: set Y to the closest source level
        src_ys = [y for _, y, _ in sources]
        if port.side == PortSide.TOP:
            target_y = min(src_ys)
        else:
            target_y = max(src_ys)
        # Clamp within bbox
        target_y = max(target_y, entry_section.bbox_y)
        target_y = min(target_y, entry_section.bbox_y + entry_section.bbox_h)
        _set_port_y(graph, port_id, target_y)
        # Only nudge X for LR/RL sections where TOP/BOTTOM ports are perpendicular
        if entry_section.direction in ("LR", "RL"):
            _nudge_port_from_stations(port_id, entry_section, graph)
    else:
        # Same-column: align X with source for vertical drop
        src_x, _, _ = sources[0]
        _set_port_x(graph, port_id, src_x)


def _nudge_port_from_stations(
    port_id: str,
    section: Section,
    graph: MetroGraph,
    tolerance: float = STATION_ELBOW_TOLERANCE,
) -> None:
    """Nudge a TOP/BOTTOM port away from any internal station at the same X.

    Moves the port toward the entry side of the section so it doesn't
    visually pass through a station marker (station-as-elbow).
    """
    station = graph.stations.get(port_id)
    port = graph.ports.get(port_id)
    if not station or not port:
        return

    internal_ids = (
        set(section.station_ids) - set(section.entry_ports) - set(section.exit_ports)
    )
    internal_xs = [
        graph.stations[sid].x
        for sid in internal_ids
        if sid in graph.stations and not graph.stations[sid].is_port
    ]
    if not internal_xs:
        return

    # Check if port X coincides with any internal station X
    if not any(abs(station.x - ix) < tolerance for ix in internal_xs):
        return

    # Move port toward the entry side of the section
    # For LR: entry is left, so move port left (toward bbox_x)
    # For RL: entry is right, so move port right (toward bbox_x + bbox_w)
    if section.direction == "RL":
        new_x = max(internal_xs) + tolerance
        # Clamp within bbox
        new_x = min(new_x, section.bbox_x + section.bbox_w - tolerance)
    else:
        new_x = min(internal_xs) - tolerance
        # Clamp within bbox
        new_x = max(new_x, section.bbox_x + tolerance)

    station.x = new_x
    port.x = new_x


def _align_ports_to_downstream(graph: MetroGraph) -> None:
    """Pull exit-entry port pairs toward downstream station positions.

    After entry ports are aligned to their source (exit port), the
    exit-entry pair may sit at a Y that is far from the downstream
    section's internal stations, forcing lines to detour vertically
    between sections.  This pass moves both ports toward the downstream
    section's average station Y when that would reduce the detour.

    Only applies to non-fold LR/RL sections without fan-out junctions
    (fold/TB sections are handled by ``_align_exit_ports``).
    """
    junction_ids = graph.junction_ids

    for port_id, port in graph.ports.items():
        if port.is_entry:
            continue

        exit_section = graph.sections.get(port.section_id)
        if not exit_section:
            continue

        # Skip fold/TB sections (handled by _align_exit_ports)
        if exit_section.grid_row_span > 1 or exit_section.direction == "TB":
            continue

        if port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue

        # Find the single target entry port (skip fan-out via junctions)
        target_entry_id: str | None = None
        for edge in graph.edges_from(port_id):
            if edge.target in junction_ids:
                # Fan-out to junction -- don't override
                target_entry_id = None
                break
            tgt = graph.stations.get(edge.target)
            if tgt and tgt.is_port:
                tgt_port = graph.ports.get(edge.target)
                if tgt_port and tgt_port.is_entry:
                    target_entry_id = edge.target
                    # Keep scanning to detect junctions on later edges

        if not target_entry_id:
            continue

        # Locate the downstream section and its internal stations
        entry_port_obj = graph.ports.get(target_entry_id)
        if not entry_port_obj:
            continue
        entry_section = graph.sections.get(entry_port_obj.section_id)
        if not entry_section:
            continue

        # Skip cross-row connections (different grid rows)
        if exit_section.grid_row != entry_section.grid_row:
            continue

        # Skip when entry port is perpendicular to its section's flow.
        # A LEFT port on a TB section must bend, so aligning it with an
        # internal station's Y would route the line through that station.
        _perp = False
        if entry_section.direction == "TB" and entry_port_obj.side in (
            PortSide.LEFT,
            PortSide.RIGHT,
        ):
            _perp = True
        elif entry_section.direction in ("LR", "RL") and entry_port_obj.side in (
            PortSide.TOP,
            PortSide.BOTTOM,
        ):
            _perp = True
        if _perp:
            continue

        internal_ids = (
            set(entry_section.station_ids)
            - set(entry_section.entry_ports)
            - set(entry_section.exit_ports)
        )
        downstream_ys: list[float] = []
        for edge in graph.edges_from(target_entry_id):
            if edge.target in internal_ids:
                downstream_ys.append(graph.stations[edge.target].y)
        if not downstream_ys:
            continue

        if graph.diamond_style == "straight":
            # Snap to the Y that the most lines target, so the majority
            # of lines flow straight.  Ties broken by topmost (smallest Y).
            y_counts: Counter[float] = Counter(downstream_ys)
            target_y = min(y_counts, key=lambda y: (-y_counts[y], y))
        else:
            target_y = sum(downstream_ys) / len(downstream_ys)

        if graph.center_ports:
            # Centre on the shorter section's midpoint.  Skip when
            # centring would create a V-shaped detour (center_y is
            # outside the range spanned by upstream and downstream Ys),
            # but allow it when both Ys match (no existing detour).
            exit_internal_ids = (
                set(exit_section.station_ids)
                - set(exit_section.entry_ports)
                - set(exit_section.exit_ports)
            )
            upstream_ys: list[float] = []
            for edge in graph.edges_to(port_id):
                if edge.source in exit_internal_ids:
                    upstream_ys.append(graph.stations[edge.source].y)
            if upstream_ys:
                upstream_y = sum(upstream_ys) / len(upstream_ys)
                shorter = min(exit_section, entry_section, key=lambda s: s.bbox_h)
                center_y = shorter.bbox_y + shorter.bbox_h / 2
                lo = min(upstream_y, target_y)
                hi = max(upstream_y, target_y)
                if lo <= center_y <= hi or abs(upstream_y - target_y) < 1.0:
                    target_y = center_y

        # Only move if target_y fits within both section bboxes
        exit_top = exit_section.bbox_y
        exit_bot = exit_section.bbox_y + exit_section.bbox_h
        if not (exit_top <= target_y <= exit_bot):
            continue

        entry_top = entry_section.bbox_y
        entry_bot = entry_section.bbox_y + entry_section.bbox_h
        if not (entry_top <= target_y <= entry_bot):
            continue

        _set_port_y(graph, port_id, target_y)
        _set_port_y(graph, target_entry_id, target_y)


def _snap_sole_layer_stations_to_ports(graph: MetroGraph) -> None:
    """Snap port-connected stations to their port Y when alone in their layer.

    After port alignment, a port may sit at a different Y than its
    connected internal station, producing a diagonal.  When that station
    is the only one at its layer within the section, it can safely move
    to the port Y without colliding with layer-siblings.
    """
    # Build set of section IDs that participated in grid alignment.
    # Stage 4.2 must not override the shared Y grid for these sections.
    grid_group_secs = _grid_group_section_ids(graph)

    for section in graph.sections.values():
        # Only applies to horizontal (LR/RL) sections where ports
        # are on LEFT/RIGHT and the free axis is Y.
        if section.direction not in ("LR", "RL"):
            continue

        # Skip grid-group sections: their stations are on a shared Y
        # grid and must not be pulled off-grid by port alignment.
        if section.id in grid_group_secs:
            continue

        port_ids = section.port_ids
        internal_ids = set(section.station_ids) - port_ids

        # Skip single-station sections: the station is already centred
        # in the section, and snapping it to a port just drags it to
        # an extreme position.
        if len(internal_ids) <= 1:
            continue

        # Build layer -> set of station IDs for collision checking.
        layer_groups: dict[int, set[str]] = {}
        for sid in internal_ids:
            st = graph.stations.get(sid)
            if st and not st.is_port and hasattr(st, "layer"):
                layer_groups.setdefault(st.layer, set()).add(sid)

        # For each LEFT/RIGHT port, check its connected internal stations.
        # Skip TOP/BOTTOM ports - snapping to a boundary port Y would
        # pull stations to the section edge rather than aligning them
        # with a horizontal predecessor.
        for pid in port_ids:
            port_obj = graph.ports.get(pid)
            if not port_obj or port_obj.side not in (PortSide.LEFT, PortSide.RIGHT):
                continue
            port_st = graph.stations.get(pid)
            if not port_st:
                continue
            port_y = port_st.y

            # Collect the distinct internal stations connected to this port.
            connected: set[str] = set()
            for edge in graph.edges_from(pid):
                if edge.target in internal_ids:
                    connected.add(edge.target)
            for edge in graph.edges_to(pid):
                if edge.source in internal_ids:
                    connected.add(edge.source)

            # Only snap when exactly one station connects to the port
            # (not a fan-in / fan-out bundle).
            if len(connected) != 1:
                continue

            # Snap the port-connected station if it's a sole layer
            # occupant and has no other predecessors/successors on the
            # port side (otherwise snapping would break those connections).
            current = next(iter(connected))
            is_entry = graph.ports[pid].is_entry

            # Skip if the station has internal predecessors (other than
            # the port).  For entry ports this means the station receives
            # from another source inside the section; for exit ports it
            # means internal stations feed into it, so snapping would
            # create a diagonal on those connections.
            has_internal_pred = any(
                edge.source != pid and edge.source in internal_ids
                for edge in graph.edges_to(current)
            )
            if has_internal_pred:
                continue

            visited: set[str] = set()
            while current and current not in visited:
                visited.add(current)
                st = graph.stations[current]

                layer = getattr(st, "layer", None)
                if layer is None:
                    break
                siblings = layer_groups.get(layer, set())
                if len(siblings) > 1:
                    break

                if abs(st.y - port_y) >= 1.0:
                    st.y = port_y

                # Only continue the chain when center_ports is on.
                if not graph.center_ports:
                    break

                # Follow to the next singleton: successors for entry
                # ports (walking inward), predecessors for exit ports.
                nexts: set[str] = set()
                if is_entry:
                    for edge in graph.edges_from(current):
                        if edge.target in internal_ids:
                            nexts.add(edge.target)
                else:
                    for edge in graph.edges_to(current):
                        if edge.source in internal_ids:
                            nexts.add(edge.source)
                current = next(iter(nexts)) if len(nexts) == 1 else None


def _snap_grid_group_entry_ports(graph: MetroGraph) -> None:
    """Snap entry ports of grid-group sections to their connected station Y.

    Stage 3.2 aligns entry ports to the upstream junction Y (e.g. the
    midpoint of two exit stations in the source section).  When Stage 4.2
    is skipped for grid-group sections, the internal station stays on the
    grid but the port keeps the junction-derived Y, creating a diagonal.

    This step corrects that by moving the port to the Y of its first
    connected non-port station inside the section, giving a straight
    horizontal connection.
    """
    grid_group_secs = _grid_group_section_ids(graph)

    if not grid_group_secs:
        return

    for port_id, port in graph.ports.items():
        if not port.is_entry:
            continue
        if port.section_id not in grid_group_secs:
            continue
        if port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue

        port_st = graph.stations.get(port_id)
        if not port_st:
            continue

        section = graph.sections.get(port.section_id)
        if not section or section.direction not in ("LR", "RL"):
            continue

        # Find the first non-port station connected to this port.
        target_y = None
        for edge in graph.edges_from(port_id):
            tgt = graph.stations.get(edge.target)
            if tgt and not tgt.is_port and tgt.section_id == section.id:
                target_y = tgt.y
                break

        if target_y is not None and abs(port_st.y - target_y) >= 1.0:
            port_st.y = target_y


def _snap_grid_group_exit_ports(graph: MetroGraph) -> None:
    """Snap exit ports of grid-group sections to their connected station Y.

    Mirrors Stage 4.3 (entry port snap) for exit ports.  When a
    grid-group section's exit port is at a midpoint between internal
    stations (the default centering), move it to the Y of the connected
    internal station that feeds into it.  This eliminates midpoint
    detours (e.g. get_reference exit at y=340 midpoint instead of y=320
    where get_pcgr sits).

    When multiple internal stations feed the exit port, picks the one
    whose Y is closest to the downstream entry port (if resolvable),
    otherwise picks the nearest to the current port position.
    """
    grid_group_secs = _grid_group_section_ids(graph)

    if not grid_group_secs:
        return

    junction_ids = graph.junction_ids

    for port_id, port in graph.ports.items():
        if port.is_entry:
            continue
        if port.section_id not in grid_group_secs:
            continue
        if port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue

        port_st = graph.stations.get(port_id)
        if not port_st:
            continue

        section = graph.sections.get(port.section_id)
        if not section or section.direction not in ("LR", "RL"):
            continue

        # Collect internal sources that feed this exit port and the
        # line ids carried by the exit edges in a single edge pass.
        source_ys: list[float] = []
        exit_lines: set[str] = set()
        for edge in graph.edges_to(port_id):
            exit_lines.add(edge.line_id)
            src = graph.stations.get(edge.source)
            if src and not src.is_port and src.section_id == section.id:
                source_ys.append(src.y)

        if not source_ys:
            continue

        ds_y = _resolve_downstream_entry_y(graph, port_id, junction_ids)

        # Already aligned with downstream entry: straight run is correct
        # even if internal sources sit at a different Y.
        if ds_y is not None and abs(port_st.y - ds_y) < 1.0:
            continue

        unique_source_ys = sorted(set(source_ys))
        spread = unique_source_ys[-1] - unique_source_ys[0]
        n_unique = len(unique_source_ys)

        if n_unique == 1 or spread <= 1.0:
            target_y = source_ys[0]
        else:
            # Multi-Y sources: snap onto the source that aligns with the
            # downstream entry so the inter-section run stays horizontal.
            # For 3+ sources this overrides the default centered merge
            # only when the exit carries a multi-line bundle (parallel-
            # redundant rather than a true fan-in).
            if ds_y is None or (n_unique >= 3 and len(exit_lines) < 2):
                continue
            match = next((y for y in unique_source_ys if abs(y - ds_y) < 1.0), None)
            if match is None:
                continue
            target_y = match

        if abs(port_st.y - target_y) >= 1.0:
            port_st.y = target_y


def _resolve_downstream_entry_y(
    graph: MetroGraph,
    exit_port_id: str,
    junction_ids: set[str],
) -> float | None:
    """Resolve the downstream entry port Y reachable from an exit port.

    Handles two patterns:
    - Direct: exit_port -> entry_port
    - Via junction: exit_port -> junction -> entry_port(s)

    Returns the nearest downstream entry port Y, or None if not found.
    """
    port_st = graph.stations.get(exit_port_id)
    if not port_st:
        return None

    entry_ys: list[float] = []
    for edge in graph.edges_from(exit_port_id):
        # Direct exit -> entry connection
        dp = graph.ports.get(edge.target)
        if dp and dp.is_entry:
            ds_st = graph.stations.get(edge.target)
            if ds_st:
                entry_ys.append(ds_st.y)
            continue
        # Via junction
        if edge.target in junction_ids:
            for e2 in graph.edges_from(edge.target):
                dp2 = graph.ports.get(e2.target)
                if dp2 and dp2.is_entry:
                    ds_st = graph.stations.get(e2.target)
                    if ds_st:
                        entry_ys.append(ds_st.y)

    if entry_ys:
        return min(entry_ys, key=lambda y: abs(y - port_st.y))
    return None


def _align_exit_ports(graph: MetroGraph) -> None:
    """Align LEFT/RIGHT exit ports on fold sections with their target's Y.

    Applies to sections with grid_row_span > 1 OR TB direction (fold bridges).
    These have exit ports placed near the section bottom, but the target
    section's entry may be at a different Y. Aligning ensures a straight
    horizontal inter-section connection.
    """
    junction_ids = graph.junction_ids

    for port_id, port in graph.ports.items():
        if port.is_entry:
            continue

        exit_section = graph.sections.get(port.section_id)
        if not exit_section:
            continue
        if exit_section.grid_row_span <= 1 and exit_section.direction != "TB":
            continue

        if port.side in (PortSide.LEFT, PortSide.RIGHT):
            _align_lr_exit_port(graph, port_id, port, exit_section, junction_ids)


def _align_lr_exit_port(
    graph: MetroGraph,
    port_id: str,
    port: Port,
    exit_section: Section,
    junction_ids: set[str],
) -> None:
    """Align a LEFT/RIGHT exit port's Y with its target entry port."""
    for edge in graph.edges_from(port_id):
        tgt = graph.stations.get(edge.target)
        if not tgt:
            continue

        # Don't align with fan-out junctions
        if edge.target in junction_ids:
            break

        if not tgt.is_port:
            continue

        # Don't align with perpendicular target ports (cross-axis)
        tgt_port_obj = graph.ports.get(tgt.id)
        if tgt_port_obj and tgt_port_obj.side in (PortSide.TOP, PortSide.BOTTOM):
            break

        # Don't pull exit port outside its section bbox
        bbox_top = exit_section.bbox_y
        bbox_bot = exit_section.bbox_y + exit_section.bbox_h
        if not (bbox_top <= tgt.y <= bbox_bot):
            break

        if exit_section.direction == "TB":
            tgt_y = _resolve_tb_exit_y(graph, port, tgt, exit_section)
        else:
            tgt_y = tgt.y

        _set_port_y(graph, port_id, tgt_y)
        break


def _resolve_tb_exit_y(
    graph: MetroGraph,
    port: Port,
    tgt: Station,
    exit_section: Section,
) -> float:
    """Resolve the Y coordinate for a TB section's exit port.

    Mirrors the entry-side gap: finds how far the perpendicular entry
    port sits above the first internal station, and places the exit port
    the same distance below the last internal station. Pushes the target
    section down if needed so the inter-section line is straight.
    """
    internal_ys = [
        graph.stations[sid].y
        for sid in exit_section.station_ids
        if sid in graph.stations and not graph.stations[sid].is_port
    ]
    last_y = max(internal_ys) if internal_ys else port.y
    first_y = min(internal_ys) if internal_ys else port.y

    # Mirror the entry-side gap (distance from entry port to first station)
    entry_gap = MIN_PORT_STATION_GAP
    for pid in exit_section.entry_ports:
        ep = graph.ports.get(pid)
        if ep and ep.side in (PortSide.LEFT, PortSide.RIGHT):
            entry_gap = max(entry_gap, first_y - graph.stations[pid].y)
            break

    # Ensure the gap below the last station is large enough for the
    # exit corner curve (CURVE_RADIUS) plus a straight run so the
    # curve doesn't crowd the station pill.
    min_exit_gap = max(entry_gap, CURVE_RADIUS + MIN_PORT_STATION_GAP)
    min_exit_y = last_y + min_exit_gap
    if tgt.y >= min_exit_y:
        tgt_y = tgt.y
    else:
        # Push target section down to align with exit port
        tgt_y = min_exit_y
        delta = tgt_y - tgt.y

        tgt.y = tgt_y
        tgt_port = graph.ports.get(tgt.id)
        if tgt_port:
            tgt_port.y = tgt_y
            tgt_sec = graph.sections.get(tgt_port.section_id)
            if tgt_sec:
                for sid in tgt_sec.station_ids:
                    s = graph.stations.get(sid)
                    if s and s.id != tgt.id:
                        s.y += delta
                        p = graph.ports.get(sid)
                        if p:
                            p.y += delta
                tgt_sec.bbox_y += delta

    # Extend exit section bbox so padding below the exit port
    # mirrors the padding above the entry port.
    entry_port_y = None
    for pid in exit_section.entry_ports:
        ep = graph.ports.get(pid)
        if ep and ep.side in (PortSide.LEFT, PortSide.RIGHT):
            entry_port_y = graph.stations[pid].y
            break
    if entry_port_y is not None:
        top_pad = entry_port_y - exit_section.bbox_y
        desired_bot = tgt_y + top_pad
        current_bot = exit_section.bbox_y + exit_section.bbox_h
        if desired_bot > current_bot:
            exit_section.bbox_h = desired_bot - exit_section.bbox_y

    return tgt_y


def _align_tb_section_bbox_bottoms(graph: MetroGraph) -> None:
    """Extend each TB-section's bbox bottom to match its downstream
    target section's bbox bottom.

    A TB (fold) section's exit port sits at the Y of the downstream
    LR/RL section's entry port (placed by ``_resolve_tb_exit_y``).
    When the TB section's bbox bottom equals its exit-port Y, the
    inter-section line runs flush against the section edge.

    For every TB section with an LR/RL exit, find the target sections
    its exit ports feed into (directly or via a junction) and grow the
    TB section's ``bbox_h`` so its bottom reaches the maximum of those
    targets' bbox bottoms.  Bbox tops are preserved; only ``bbox_h``
    grows.

    Skipped for TB sections with BOTTOM-side exit ports (TB->TB flow)
    so the bottom-edge port placement invariant continues to hold.
    """
    junction_ids = graph.junction_ids

    def _downstream_section_ids(tb_section: Section) -> set[str]:
        out: set[str] = set()
        for pid in tb_section.exit_ports:
            for edge in graph.edges_from(pid):
                candidates: list[str] = []
                if edge.target in junction_ids:
                    for e2 in graph.edges_from(edge.target):
                        candidates.append(e2.target)
                else:
                    candidates.append(edge.target)
                for tid in candidates:
                    tport = graph.ports.get(tid)
                    if tport is None:
                        continue
                    tsec = graph.sections.get(tport.section_id)
                    if tsec is None or tsec.id == tb_section.id:
                        continue
                    out.add(tsec.id)
        return out

    for section in list(graph.sections.values()):
        if section.direction != "TB" or section.bbox_h <= 0:
            continue
        exit_sides = {
            graph.ports[pid].side for pid in section.exit_ports if pid in graph.ports
        }
        if not exit_sides & {PortSide.LEFT, PortSide.RIGHT}:
            continue
        if PortSide.BOTTOM in exit_sides:
            continue
        target_ids = _downstream_section_ids(section)
        if not target_ids:
            continue
        target_bots = [
            graph.sections[tid].bbox_y + graph.sections[tid].bbox_h
            for tid in target_ids
            if tid in graph.sections and graph.sections[tid].bbox_h > 0
        ]
        if not target_bots:
            continue
        desired_bot = max(target_bots)
        current_bot = section.bbox_y + section.bbox_h
        if desired_bot - current_bot <= 0.5:
            continue
        section.bbox_h = desired_bot - section.bbox_y


def _clamp_tb_entry_port(
    graph: MetroGraph,
    entry_section: Section,
    target_y: float,
    edge: Edge,
    src: Station,
    junction_ids: set[str],
) -> float:
    """Clamp a TB section's perpendicular entry port above internal stations.

    The entry port must stay above the first internal station so the
    direction-change curve has room. When clamped, also pulls the source
    station/junction up to maintain a straight horizontal run.

    Returns the (possibly clamped) target_y.
    """
    internal_ids = (
        set(entry_section.station_ids)
        - set(entry_section.entry_ports)
        - set(entry_section.exit_ports)
    )
    internal_ys = [
        graph.stations[sid].y
        for sid in internal_ids
        if sid in graph.stations and not graph.stations[sid].is_port
    ]
    if not internal_ys:
        return target_y

    first_y = min(internal_ys)
    max_y = first_y - MIN_PORT_STATION_GAP
    if target_y <= max_y:
        return target_y

    # Prefer the topmost source-side station feeding the exit port
    # so that line exits horizontally.
    exit_pid = edge.source
    if edge.source in junction_ids:
        for e2 in graph.edges:
            if e2.target == edge.source:
                ep = graph.stations.get(e2.source)
                if ep and ep.is_port:
                    exit_pid = e2.source
                    break

    top_src_y = None
    for e3 in graph.edges:
        if e3.target == exit_pid:
            s3 = graph.stations.get(e3.source)
            if s3 and not s3.is_port and e3.source not in junction_ids:
                if top_src_y is None or s3.y < top_src_y:
                    top_src_y = s3.y

    if top_src_y is not None and top_src_y < max_y:
        target_y = top_src_y
    else:
        target_y = max_y

    # Pull source up to maintain straight horizontal run
    src.y = target_y
    if src.is_port and edge.source in graph.ports:
        graph.ports[edge.source].y = target_y
    # If source is a junction, also pull the exit port feeding it
    if edge.source in junction_ids:
        for e2 in graph.edges:
            if e2.target == edge.source:
                ep = graph.stations.get(e2.source)
                if ep and ep.is_port:
                    ep.y = target_y
                    if e2.source in graph.ports:
                        graph.ports[e2.source].y = target_y

    return target_y


def _space_ports_from_termini(
    graph: MetroGraph,
    y_spacing: float,
) -> None:
    """Push ports away from terminus stations so there is a full row gap.

    After port alignment, an entry or exit port may sit very close to a
    terminus station in the same section.  Lines routed from that port
    then overlap the terminus file icon.

    Only entry ports are checked against entry-side (source) termini, and
    exit ports against exit-side (sink) termini, to avoid displacing
    ports on the opposite side of the section.

    Exit ports on fold sections (grid_row_span > 1 or TB direction) are
    skipped because ``_align_exit_ports`` will overwrite them.
    """
    # Pre-compute edge adjacency (used to identify direct connections
    # and to propagate port moves across section boundaries).
    adjacency: dict[str, set[str]] = {}
    successors: dict[str, set[str]] = {}
    predecessors: dict[str, set[str]] = {}
    for edge in graph.edges:
        adjacency.setdefault(edge.source, set()).add(edge.target)
        adjacency.setdefault(edge.target, set()).add(edge.source)
        successors.setdefault(edge.source, set()).add(edge.target)
        predecessors.setdefault(edge.target, set()).add(edge.source)

    for section in graph.sections.values():
        entry_port_ids = set(section.entry_ports)
        exit_port_ids = set(section.exit_ports)
        all_port_ids = entry_port_ids | exit_port_ids
        real_sids = {s for s in section.station_ids if s not in all_port_ids}

        # Skip exit ports on fold sections -- _align_exit_ports handles them.
        is_fold = section.grid_row_span > 1 or section.direction == "TB"

        # Classify termini by side.  A station with no in-section
        # predecessors is an entry-side (source) terminus; one with no
        # in-section successors is an exit-side (sink) terminus.  A
        # station can be both (isolated within the section), but we only
        # add it to entry_termini to avoid conflicting pushes from both
        # the entry and exit port passes.
        entry_termini: list[tuple[str, float]] = []
        exit_termini: list[tuple[str, float]] = []
        for sid in real_sids:
            st = graph.stations.get(sid)
            if not st or not st.is_terminus or st.is_port:
                continue
            # Off-track stations get lifted above the topmost line track
            # later (Stage 5.2), so they no longer share a Y with the
            # inter-section bundle.  Excluding them here prevents ports
            # from being pushed away (and dragging the upstream port via
            # junction propagation) for a conflict that won't exist by
            # render time.
            if st.off_track:
                continue
            preds = predecessors.get(sid, set())
            succs = successors.get(sid, set())
            is_source = not (preds & real_sids)
            is_sink = not (succs & real_sids)
            if is_source:
                entry_termini.append((sid, st.y))
            elif is_sink:
                # Only classify as exit terminus if not already an
                # entry terminus (avoids double-counting isolated nodes).
                exit_termini.append((sid, st.y))

        _push_ports_from_termini(
            graph,
            sorted(entry_port_ids),
            entry_termini,
            section,
            adjacency,
            predecessors,
            y_spacing,
        )
        if not is_fold:
            _push_ports_from_termini(
                graph,
                sorted(exit_port_ids),
                exit_termini,
                section,
                adjacency,
                predecessors,
                y_spacing,
            )


def _push_ports_from_termini(
    graph: MetroGraph,
    port_ids: list[str],
    termini: list[tuple[str, float]],
    section: Section,
    adjacency: dict[str, set[str]],
    predecessors: dict[str, set[str]],
    y_spacing: float,
) -> None:
    """Ensure *y_spacing* between each port and non-connected termini.

    The strategy depends on how the port connects across sections:

    - **Junction link** (fan-out): move the port and propagate through
      the junction to its *upstream* (predecessor) port only, keeping
      the exit-junction-entry chain straight without disturbing other
      fan-out targets.
    - **Direct port-to-port link** (no junction): moving the port would
      cascade to the other section and mis-align its internal stations.
      Instead, push the conflicting *terminus* away from the port.
    - **No cross-section link**: move the port freely.

    *port_ids* must be a sorted list so that results are deterministic
    when multiple ports in the same section conflict with the same
    terminus.
    """
    junction_ids = graph.junction_ids
    section_port_set = set(port_ids)

    for pid in port_ids:
        port_st = graph.stations.get(pid)
        if not port_st:
            continue
        port_obj = graph.ports.get(pid)
        assert port_obj is not None, f"port {pid} missing from graph.ports"
        neighbours = adjacency.get(pid, set())

        # Classify cross-section connection type.
        has_junction = bool(neighbours & junction_ids)
        has_direct_port = False
        if not has_junction:
            for nb in neighbours:
                if nb in graph.ports and nb not in section_port_set:
                    has_direct_port = True
                    break

        # Collect all termini that are too close and not directly
        # connected to this port.
        conflict_ids: list[str] = []
        conflict_ys: list[float] = []
        for tid, ty in termini:
            if tid in neighbours:
                continue
            if abs(port_st.y - ty) < y_spacing:
                conflict_ids.append(tid)
                conflict_ys.append(ty)

        if not conflict_ys:
            continue

        if has_direct_port:
            # Move the terminus instead of the port so the
            # inter-section line stays straight.
            _push_termini_from_port(graph, conflict_ids, port_st.y, section, y_spacing)
            continue

        # Compute the single best Y that satisfies all conflicts.
        above_candidates = [ty - y_spacing for ty in conflict_ys]
        below_candidates = [ty + y_spacing for ty in conflict_ys]

        best_above = min(above_candidates)
        best_below = max(below_candidates)

        dist_above = abs(port_st.y - best_above)
        dist_below = abs(port_st.y - best_below)
        # Ties go above (smaller Y) to keep ports near the top.
        new_y = best_above if dist_above <= dist_below else best_below

        port_st.y = new_y
        port_obj.y = new_y

        # Propagate through junctions so inter-section lines stay straight.
        _propagate_through_junctions(
            graph,
            pid,
            new_y,
            neighbours,
            junction_ids,
            predecessors,
        )

        # Grow this section's bbox to contain the moved port.
        _expand_bbox_for_y(section, new_y)


def _propagate_through_junctions(
    graph: MetroGraph,
    origin_pid: str,
    new_y: float,
    neighbours: set[str],
    junction_ids: set[str],
    predecessors: dict[str, set[str]],
) -> None:
    """Move connected junctions and their upstream exit ports to *new_y*.

    Only propagates to the junction's upstream (predecessor) ports, not
    to other fan-out targets (entry ports to other sections).
    """
    for nb in neighbours:
        if nb not in junction_ids:
            continue
        nb_st = graph.stations.get(nb)
        if not nb_st:
            continue

        nb_st.y = new_y
        for jnb in predecessors.get(nb, set()):
            if jnb == origin_pid:
                continue
            jnb_st = graph.stations.get(jnb)
            if not jnb_st or not jnb_st.is_port:
                continue
            jnb_st.y = new_y
            jnb_obj = graph.ports.get(jnb)
            if jnb_obj:
                jnb_obj.y = new_y
                jnb_sec = graph.sections.get(jnb_obj.section_id)
                if jnb_sec:
                    _expand_bbox_for_y(jnb_sec, new_y)


def _push_termini_from_port(
    graph: MetroGraph,
    terminus_ids: list[str],
    port_y: float,
    section: Section,
    y_spacing: float,
) -> None:
    """Push terminus stations to the nearest station row that clears the port.

    Instead of placing the terminus at the arbitrary ``port_y ± y_spacing``,
    snap it to an existing station Y in the section that satisfies the
    minimum clearance.  This keeps the terminus aligned with an actual
    track row rather than floating at an unrelated Y coordinate.
    """
    # Collect existing station Y values in the section (excluding ports
    # and the termini being moved) as candidate snap targets.
    port_ids = section.port_ids
    tid_set = set(terminus_ids)
    section_ys: set[float] = set()
    for sid in section.station_ids:
        if sid in port_ids or sid in tid_set:
            continue
        st = graph.stations.get(sid)
        if st and not st.is_port:
            section_ys.add(st.y)

    for tid in terminus_ids:
        t_st = graph.stations.get(tid)
        if not t_st:
            continue

        # Determine push direction
        going_down = t_st.y > port_y

        # Find the nearest section row Y that satisfies clearance
        candidates = sorted(
            (y for y in section_ys if abs(y - port_y) >= y_spacing),
            key=lambda y: abs(y - t_st.y),
        )
        if going_down:
            candidates = [y for y in candidates if y >= port_y + y_spacing]
        else:
            candidates = [y for y in candidates if y <= port_y - y_spacing]

        if candidates:
            new_y = candidates[0]
        else:
            # No existing row satisfies clearance; fall back to offset.
            new_y = (port_y + y_spacing) if going_down else (port_y - y_spacing)

        t_st.y = new_y
        _expand_bbox_for_y(section, new_y)


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


def _insert_phantom_pass_throughs(
    graph: MetroGraph,
    section: Section,
    sub: MetroGraph,
) -> None:
    """Insert phantom stations into *sub* so deep-entry lines get own tracks.

    When a line enters a section via an entry port but its first internal
    station is deeper than layer 0, the line would share a track with
    unrelated stations at the early layers.  Adding a hidden phantom at
    layer 0 gives the line a dedicated track for a clear horizontal runway.

    Only modifies the temporary subgraph -- the main graph stays immutable.
    """
    if not sub.stations:
        return

    from nf_metro.layout.layers import assign_layers

    layers = assign_layers(sub)
    if not layers:
        return
    min_layer = min(layers.values())

    entry_port_ids = set(section.entry_ports)

    # Find lines entering from entry ports to deep-layer internal stations.
    entry_targets: dict[str, set[str]] = {}
    for pid in entry_port_ids:
        for edge in graph.edges_from(pid):
            if edge.target in sub.stations:
                entry_targets.setdefault(edge.line_id, set()).add(edge.target)

    for line_id, targets in entry_targets.items():
        target_layers = [layers.get(t, min_layer) for t in targets]
        if all(ly > min_layer for ly in target_layers):
            earliest_target = min(targets, key=lambda t: layers.get(t, 0))
            phantom_id = f"_phantom_{section.id}_{line_id}"

            sub.add_station(
                Station(
                    id=phantom_id,
                    label="",
                    section_id=section.id,
                    is_hidden=True,
                )
            )
            sub.add_edge(
                Edge(source=phantom_id, target=earliest_target, line_id=line_id)
            )


def _align_phantom_pass_throughs(
    sub: MetroGraph,
    tracks: dict[str, float],
) -> None:
    """Snap convergence nodes to their phantom pass-through's track.

    The phantom ensures a dedicated track for the bypassing line.
    Moving the convergence node (the phantom's sole successor) to that
    track keeps the trunk horizontal so the optional branch visually
    "bubbles" away from it.
    """
    for sid, station in sub.stations.items():
        if not station.is_hidden or sid not in tracks:
            continue
        succs = {e.target for e in sub.edges_from(sid)}
        if len(succs) == 1:
            succ = next(iter(succs))
            if succ in tracks:
                tracks[succ] = tracks[sid]


def _compute_fork_join_gaps(
    sub: MetroGraph,
    layers: dict[str, int],
    tracks: dict[str, float],
    x_spacing: float,
    full_graph: MetroGraph | None = None,
    section_station_ids: set[str] | None = None,
) -> dict[int, float]:
    """Compute extra X offset per layer at fork/join points.

    Adds a fractional gap after fork layers (where tracks diverge) and
    before join layers (where tracks converge) so labels aren't obscured
    by diagonal crossings.

    When full_graph and section_station_ids are provided, fork/join
    detection uses all edges within the section (including port-touching
    edges). This catches divergences where a station connects to both
    internal stations and exit ports.

    In single-track sections (all stations on the same Y), port-bound
    divergences are suppressed because there are no diagonal transitions
    and the extra spacing is purely wasteful.
    """
    out_targets: dict[str, set[str]] = defaultdict(set)
    in_sources: dict[str, set[str]] = defaultdict(set)

    # Use full graph edges for fork/join detection when available,
    # so that edges to/from port stations are counted as divergences.
    if full_graph is not None and section_station_ids is not None:
        for edge in full_graph.edges:
            src_in = edge.source in section_station_ids
            tgt_in = edge.target in section_station_ids
            if src_in and tgt_in:
                out_targets[edge.source].add(edge.target)
                in_sources[edge.target].add(edge.source)
    else:
        for edge in sub.edges:
            out_targets[edge.source].add(edge.target)
            in_sources[edge.target].add(edge.source)

    # Only count forks/joins that span multiple tracks (requiring a
    # diagonal routing transition).  Same-track fan-outs (e.g. a station
    # connecting to both an internal successor and an exit port on the
    # same Y) don't need extra horizontal room.
    #
    # Port stations aren't in ``tracks`` (they're positioned later), so
    # treat them conservatively: if any participant is missing from
    # tracks, assume it may be on a different track and count the
    # fork/join.
    #
    # Exception for **forks** in single-track sections: exit-side ports
    # sit at the far section boundary, so the diagonal from the fork
    # station has ample horizontal room without extra layer spacing.
    # Join gaps are kept even in single-track sections because entry
    # ports are close to the first internal station, and the diagonal
    # from a different-Y entry needs the extra room.
    # Bypass V helpers (id prefix ``__bypass_``) are routing-only.  A
    # V on its own off-trunk track must not flip an otherwise
    # single-track section into "multi-track", or it would turn
    # port-bound divergences into fork gaps that shift visible stations
    # rightward.  Specifically when a V is one of the fork/join peers:
    # exclude its track AND fold the owner's own track into the visible
    # set so that visible-vs-owner diagonals still trigger a gap, but a
    # V-only off-trunk peer (e.g. ``trim -> {align, V}`` in the 05 guide
    # family) does not.  When no V is involved, fall back to the original
    # peer-set track count so non-bypass topologies stay byte-identical.
    visible_tracks = {t for sid, t in tracks.items() if not sid.startswith("__bypass_")}
    is_single_track = len(visible_tracks) <= 1

    def _has_bypass(ids):
        return any(nid.startswith("__bypass_") for nid in ids)

    def _bypass_aware_tracks(ids, owner_sid):
        """Visible peer tracks plus the owner's own track, V's removed."""
        result: set[float] = set()
        owner_track = tracks.get(owner_sid)
        if owner_track is not None:
            result.add(owner_track)
        for nid in ids:
            if nid.startswith("__bypass_"):
                continue
            t = tracks.get(nid)
            if t is not None:
                result.add(t)
        return result

    fork_layers: set[int] = set()
    for sid, targets in out_targets.items():
        if len(targets) > 1 and sid in layers:
            if any(t not in tracks for t in targets):
                if not is_single_track:
                    fork_layers.add(layers[sid])
            else:
                if _has_bypass(targets):
                    target_tracks = _bypass_aware_tracks(targets, sid)
                else:
                    target_tracks = {tracks[t] for t in targets}
                if len(target_tracks) > 1:
                    fork_layers.add(layers[sid])

    join_layers: set[int] = set()
    for sid, sources in in_sources.items():
        if len(sources) > 1 and sid in layers:
            if any(s not in tracks for s in sources):
                join_layers.add(layers[sid])
            else:
                if _has_bypass(sources):
                    source_tracks = _bypass_aware_tracks(sources, sid)
                else:
                    source_tracks = {tracks[s] for s in sources}
                if len(source_tracks) > 1:
                    join_layers.add(layers[sid])

    if not fork_layers and not join_layers:
        return {}

    max_layer = max(layers.values()) if layers else 0
    base_gap = x_spacing * EXIT_GAP_MULTIPLIER

    # Compute per-layer gap scaled by label width at fork/join stations.
    # The gap must be large enough that the diagonal transition starts
    # past the label text and still has room for the transition itself.
    #
    # For multi-target forks / multi-source joins, bubble station
    # centering is skipped in routing, so the flat run at the bubble
    # end can be very short.  When bubble stations sit on different
    # tracks from the fork/join and have wide labels, add extra space
    # so the flat run accommodates them.
    layer_gap: dict[int, float] = {}
    for layer in fork_layers | join_layers:
        fj_label_half = 0.0
        fj_tracks: set[float] = set()
        for sid, lyr in layers.items():
            if lyr == layer:
                station = sub.stations.get(sid)
                if station and station.label.strip():
                    label_half = label_text_width(station.label) / 2
                    fj_label_half = max(fj_label_half, label_half)
                if sid in tracks:
                    fj_tracks.add(tracks[sid])

        # Check adjacent bubble layer for off-track stations with
        # wide labels.  Only applies for wide fan-outs (3+ off-track
        # targets/sources) where bubble station centering is skipped
        # in routing and middle stations must have inside labels.
        bubble_label_half = 0.0
        is_wide_fork = False
        is_wide_join = False
        if layer in fork_layers:
            for sid, tgts in out_targets.items():
                if layers.get(sid) == layer and sid in tracks:
                    off_track = sum(
                        1 for t in tgts if t in tracks and tracks[t] != tracks[sid]
                    )
                    if off_track >= 3:
                        is_wide_fork = True
                        break
        if layer in join_layers:
            for sid, srcs in in_sources.items():
                if layers.get(sid) == layer and sid in tracks:
                    off_track = sum(
                        1 for s in srcs if s in tracks and tracks[s] != tracks[sid]
                    )
                    if off_track >= 3:
                        is_wide_join = True
                        break
        if is_wide_fork:
            for sid, lyr in layers.items():
                if lyr == layer + 1 and sid in tracks and tracks[sid] not in fj_tracks:
                    station = sub.stations.get(sid)
                    if station and station.label.strip():
                        bubble_label_half = max(
                            bubble_label_half, label_text_width(station.label) / 2
                        )
        if is_wide_join:
            for sid, lyr in layers.items():
                if lyr == layer - 1 and sid in tracks and tracks[sid] not in fj_tracks:
                    station = sub.stations.get(sid)
                    if station and station.label.strip():
                        bubble_label_half = max(
                            bubble_label_half, label_text_width(station.label) / 2
                        )

        # The bubble station is centered on its flat run.  The total
        # space needed is 2 * label_half + DIAGONAL_RUN, but the gap
        # is added on BOTH sides (after fork, before join), so each
        # side contributes half the total requirement.
        bubble_extra = max(
            0.0, (bubble_label_half * 2 + DIAGONAL_RUN - x_spacing) / 1.5
        )
        layer_gap[layer] = max(base_gap, fj_label_half + bubble_extra)

    cumulative = 0.0
    layer_extra: dict[int, float] = {}
    for layer in range(max_layer + 1):
        # Add gap before join layers
        if layer in join_layers:
            cumulative += layer_gap.get(layer, base_gap)
        layer_extra[layer] = cumulative
        # Add gap after fork layers
        if layer in fork_layers:
            cumulative += layer_gap.get(layer, base_gap)

    return layer_extra


def _off_track_groups(
    graph: MetroGraph,
) -> dict[str, tuple[str, dict[str, list[Station]]]]:
    """Group off-track stations by section and consumer.

    Returns a mapping ``section_id -> (fallback_consumer_id, groups)``
    where ``groups`` maps consumer-station-id (or ``""`` for inputs with
    no same-section consumer) to a list of off-track stations feeding
    that consumer.  ``fallback_consumer_id`` is the topmost on-track
    station in the section, used as the anchor for the ``""`` bucket.
    """
    junction_ids = graph.junction_ids

    by_section: dict[str, list[Station]] = defaultdict(list)
    for sid, st in graph.stations.items():
        if not st.off_track or st.is_port or sid in junction_ids:
            continue
        if not st.section_id:
            continue
        by_section[st.section_id].append(st)

    consumer_of: dict[str, str] = {}
    for off_stations in by_section.values():
        for off_st in off_stations:
            for edge in graph.edges_from(off_st.id):
                tgt = graph.stations.get(edge.target)
                if tgt is None:
                    continue
                if tgt.is_port or tgt.id in junction_ids or tgt.off_track:
                    continue
                if off_st.section_id != tgt.section_id:
                    continue
                consumer_of.setdefault(off_st.id, tgt.id)
                break

    result: dict[str, tuple[str, dict[str, list[Station]]]] = {}
    for sec_id, off_stations in by_section.items():
        section = graph.sections.get(sec_id)
        if not section:
            continue
        anchor_pairs = [
            (graph.stations[sid].y, sid)
            for sid in section.station_ids
            if sid in graph.stations
            and not graph.stations[sid].is_port
            and not graph.stations[sid].off_track
            and sid not in junction_ids
        ]
        if not anchor_pairs:
            continue
        fallback_id = min(anchor_pairs)[1]
        groups: dict[str, list[Station]] = defaultdict(list)
        for st in off_stations:
            groups[consumer_of.get(st.id, "")].append(st)
        result[sec_id] = (fallback_id, groups)
    return result


def _place_off_track_above_consumers(
    graph: MetroGraph,
    y_spacing: float,
    section_id: str,
    fallback_consumer_id: str,
    by_consumer: dict[str, list[Station]],
) -> float | None:
    """Place each off-track input ``n*y_spacing`` above its consumer.

    Multiple inputs feeding the same consumer stack upward in
    ``y_spacing`` steps.  When the natural ``consumer_y - k*y_spacing``
    slot would put the icon on top of another trunk station's line band
    in the same column (e.g. ``net_in`` at the gsea-trunk Y when
    decoupler sits one slot below gsea at non-savepoint params), the
    slot is bumped upward by additional ``y_spacing`` steps until the
    icon's vertical bbox clears every line-bearing track in its column
    and every sibling off-track already placed in the same column.

    Returns the smallest assigned Y (topmost lifted station), or
    ``None`` when no stations were placed.
    """
    section = graph.sections.get(section_id)
    sec_dir = section.direction if section is not None else "LR"
    junction_ids = graph.junction_ids

    # Track already-placed off-track Ys per column so a bumped icon
    # doesn't crash into a sibling off-track already at the desired Y.
    used_ys_per_col: dict[float, list[float]] = defaultdict(list)

    # Iterate consumers bottom-up (largest consumer Y first).  The
    # bumping mechanism only pushes upward, so placing the bottommost
    # consumer's icon first lets subsequent (higher-consumer) icons
    # stack above it.  The resulting visual order matches the consumer
    # Y order: an upper consumer gets an upper icon, a lower consumer
    # gets a lower icon, regardless of edge declaration order in the mmd.
    def _consumer_anchor_y(item: tuple[str, list[Station]]) -> float:
        cid = item[0] if item[0] else fallback_consumer_id
        a = graph.stations.get(cid)
        return a.y if a is not None else 0.0

    ordered_consumers = sorted(
        by_consumer.items(), key=_consumer_anchor_y, reverse=True
    )

    highest_y: float | None = None
    for consumer_id, stations in ordered_consumers:
        anchor_id = consumer_id if consumer_id else fallback_consumer_id
        anchor = graph.stations.get(anchor_id)
        if anchor is None:
            continue
        consumer_y = anchor.y
        # Preserve original Y order: input closest to the top stays
        # topmost in the stack.
        stations.sort(key=lambda s: s.y)
        n = len(stations)
        for i, st in enumerate(stations):
            base_step = n - i
            candidate_y = consumer_y - base_step * y_spacing
            if section is not None and sec_dir in ("LR", "RL"):
                candidate_y = _bump_off_track_clear_of_trunks(
                    graph,
                    st,
                    candidate_y,
                    y_spacing,
                    section,
                    junction_ids,
                    sibling_ys=used_ys_per_col[round(st.x, 1)],
                )
            st.y = candidate_y
            used_ys_per_col[round(st.x, 1)].append(st.y)
            if highest_y is None or st.y < highest_y:
                highest_y = st.y
    return highest_y


def _bump_off_track_clear_of_trunks(
    graph: MetroGraph,
    off_st: Station,
    candidate_y: float,
    y_spacing: float,
    section: Section,
    junction_ids: set[str],
    sibling_ys: list[float] | None = None,
) -> float:
    """Return ``candidate_y`` raised so the off-track icon clears any
    trunk line track passing through the icon's X column.

    The renderer places an off-track icon at the station's Y with file-
    icon half-height ~16 px; a trunk station's line tracks run at
    ``trunk.y + offset(line)`` for each line on the trunk.  When a
    trunk station downstream of the icon (LR: higher X; RL: lower X)
    has tracks at Y values inside ``[candidate_y - icon_half,
    candidate_y + icon_half]``, the segment from the section's entry
    port to that trunk crosses the icon.  Bump up by ``y_spacing``
    steps until the band clears.

    ``sibling_ys`` is a list of Ys already taken by other off-track
    inputs in the same column - the bump must also clear those (within
    one ``y_spacing`` slot) so two icons don't end up in the same row.

    Capped at six steps to avoid runaway lifts.
    """
    if y_spacing <= 0:
        return candidate_y

    # Match the renderer's terminus icon height and add a small margin
    # so the icon's stroke doesn't touch a track.
    MARGIN = 2.0
    # Limit lift attempts so a pathological column doesn't pull the
    # icon off-canvas.
    MAX_STEPS = 6

    # Find trunk stations in the same section whose row-bundle crosses
    # the icon's X column.
    trunk_offsets_at_x: list[float] = []
    for sid in section.station_ids:
        st2 = graph.stations.get(sid)
        if st2 is None or st2.is_port or st2.is_hidden:
            continue
        if st2.id == off_st.id or sid in junction_ids:
            continue
        if st2.off_track or st2.is_terminus:
            continue
        # Only stations on the OTHER side of the icon (i.e. the trunk
        # the entry port feeds) have tracks crossing the icon's column.
        if section.direction == "LR" and st2.x <= off_st.x + 0.5:
            continue
        if section.direction == "RL" and st2.x >= off_st.x - 0.5:
            continue
        # Collect the line-track band Y range at the icon's column.
        # Tracks run horizontally so each line's Y here equals
        # st2.y + offset(line); offsets aren't computed at this phase
        # but they're bounded by ``(n_lines - 1) * OFFSET_STEP`` total
        # spread (centred on st2.y).  Use line-track extents only - no
        # marker radius - because the icon is at a different X from st2
        # so st2's pill doesn't intersect the icon's column.
        lines = graph.station_lines(sid)
        n_lines = len(lines)
        if n_lines == 0:
            continue
        half_span = (n_lines - 1) * OFFSET_STEP / 2
        trunk_offsets_at_x.append(st2.y - half_span)
        trunk_offsets_at_x.append(st2.y + half_span)

    sib_ys = list(sibling_ys or [])

    if not trunk_offsets_at_x and not sib_ys:
        return candidate_y

    def _overlaps(y: float) -> bool:
        top = y - ICON_HALF_HEIGHT - MARGIN
        bot = y + ICON_HALF_HEIGHT + MARGIN
        for tl_y_lo, tl_y_hi in zip(trunk_offsets_at_x[::2], trunk_offsets_at_x[1::2]):
            if not (bot < tl_y_lo or tl_y_hi < top):
                return True
        # Sibling clearance: keep at least 2 * ICON_HALF_HEIGHT + MARGIN between
        # icon centres in the same column so the icon bboxes don't
        # touch.
        for sy in sib_ys:
            if abs(sy - y) < 2 * ICON_HALF_HEIGHT + MARGIN:
                return True
        return False

    y = candidate_y
    steps = 0
    while _overlaps(y) and steps < MAX_STEPS:
        y -= y_spacing
        steps += 1
    return y


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


def _min_section_bbox_top(graph: MetroGraph, default: float) -> float:
    """Smallest ``bbox_y`` among non-empty sections, or ``default``."""
    return min(
        (s.bbox_y for s in graph.sections.values() if s.bbox_h > 0),
        default=default,
    )


def _translate_graph_y(graph: MetroGraph, shift: float) -> None:
    """Shift every station, section bbox, and port down by ``shift``."""
    for st in graph.stations.values():
        st.y += shift
    for section in graph.sections.values():
        section.bbox_y += shift
    for port in graph.ports.values():
        port.y += shift


def _shift_graph_into_canvas(graph: MetroGraph, section_y_padding: float) -> None:
    """Shift the whole graph down if the topmost section is above the canvas.

    Keeps the topmost section's ``section_y_padding`` margin from the
    canvas edge.  No-op when all sections already sit inside.
    """
    min_top = _min_section_bbox_top(graph, section_y_padding)
    if min_top >= section_y_padding:
        return
    _translate_graph_y(graph, section_y_padding - min_top)


def _lift_off_track_stations(
    graph: MetroGraph,
    y_spacing: float,
    section_y_padding: float,
) -> None:
    """Lift off_track stations to the row above their consumer station.

    Off-track stations are file-input nodes that should not consume a
    line-track Y slot.  Each marked station is placed one ``y_spacing``
    row above its consumer (the on-track station it feeds), so the
    input sits adjacent to where its data is read rather than at a
    uniform top-of-section band.  When several off-track inputs feed
    the same consumer, they stack upward in ``y_spacing`` steps.

    If an off-track station has no on-track consumer in the same
    section, it falls back to the section's topmost on-track station
    as its anchor.  After placement, the section bbox grows upward to
    fit the highest lifted input, and same-section TOP ports are
    nudged back to the new top edge.

    Caller is responsible for invoking ``_shift_graph_into_canvas``
    afterwards: the upward bbox growth here can push the topmost
    section above the canvas top margin set by Stage 1.5.
    """
    groups = _off_track_groups(graph)
    if not groups:
        return

    for sec_id, (fallback_id, by_consumer) in groups.items():
        section = graph.sections.get(sec_id)
        if section is None:
            continue
        highest_y = _place_off_track_above_consumers(
            graph, y_spacing, sec_id, fallback_id, by_consumer
        )
        if highest_y is None:
            continue
        new_bbox_top = highest_y - section_y_padding
        if new_bbox_top < section.bbox_y:
            _grow_section_bbox_upward(graph, section, new_bbox_top)


def _reanchor_off_track_to_consumer(
    graph: MetroGraph,
    y_spacing: float,
    section_y_padding: float = SECTION_Y_PADDING,
) -> None:
    """Re-place off-track inputs relative to consumer Ys after final snap.

    Stage 5.2 placed each off-track input at ``consumer.y - n*y_spacing``
    using the consumer's pre-snap Y.  Later phases (compaction, grid
    snap, fan re-centering) may shift the consumer, which would
    collapse or shrink the gap between the off-track input and its
    consumer.  This pass re-pins each off-track at
    ``consumer.y - n*y_spacing`` on the consumer's final snapped Y.

    Bboxes were grown in Stage 5.2 based on the off-track positions at
    the time.  If a re-anchor moves an off-track above the current bbox
    top minus padding, expand the bbox upward so the lifted input still
    sits inside the section's padding zone.  Same-section TOP ports
    follow the new top edge.

    Caller is responsible for invoking ``_shift_graph_into_canvas``
    afterwards: the upward bbox growth here can push the topmost
    section above the canvas top margin (mirrors the same caller
    contract as ``_lift_off_track_stations``).
    """
    groups = _off_track_groups(graph)
    for sec_id, (fallback_id, by_consumer) in groups.items():
        highest_y = _place_off_track_above_consumers(
            graph, y_spacing, sec_id, fallback_id, by_consumer
        )
        if highest_y is None:
            continue
        section = graph.sections.get(sec_id)
        if section is None:
            continue
        desired_top = highest_y - section_y_padding
        if desired_top < section.bbox_y - 0.5:
            _grow_section_bbox_upward(graph, section, desired_top)
