"""Routing context: _RoutingCtx, the context builder, and shared
section-geometry helpers used across the routing handlers.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from nf_metro.layout.constants import (
    BYPASS_CLEARANCE,
    COORD_TOLERANCE,
    JUNCTION_MARGIN,
    resolve_offset_step,
)
from nf_metro.layout.geometry import AxisFrame, lane_delta, station_lane_coord
from nf_metro.layout.routing.common import (
    RoutedPath,
    bypass_bottom_y,
    compute_bundle_info,
    fan_corridor_band,
    merge_fanout_junctions,
    merge_trunk_force_cross_row,
    resolve_section,
    row_bottom_edge,
    row_top_edge,
    vertical_flow_sections,
)
from nf_metro.layout.routing.reversal import (
    detect_reversed_sections,
    tb_positive_fan_sections,
)
from nf_metro.parser.model import (
    Edge,
    MetroGraph,
    Port,
    PortSide,
    Section,
    Station,
)

_EdgeKey = tuple[str, str, str]


@dataclass
class _MergeRouting:
    """Pre-computed merge junction routing decisions.

    Consolidates trunk/branch classification, edge skipping, and
    index exclusion so routing handlers can dispatch cleanly.
    """

    junctions: set[str]
    """IDs of merge junction stations."""

    trunk_source: dict[str, str]
    """merge_id -> source station that carries the full bypass trunk."""

    trunk_by: dict[str, float]
    """merge_id -> Y of the trunk's bypass channel, the level branches drop to.

    Computed with the trunk route's own ``cross_row`` decision so the branch
    drop level matches the channel the trunk actually runs.
    """

    entry_port_for: dict[str, str]
    """merge_id -> entry port station ID (pre-resolved)."""

    skip_edges: set[_EdgeKey]
    """Edges not routed at all (trunk covers them)."""

    index_exclude: set[_EdgeKey]
    """Edges excluded from gap/fan indexing but still routed."""


@dataclass(frozen=True)
class FanCorridor:
    """Shared traverse geometry for one fanning junction, one band per kind.

    A junction fans branches through different handlers that each traverse a
    horizontal band below the junction: the inter-row-gap branches (top-entry
    L-shape, LEFT/RIGHT entry wrap) run in the gap immediately below the
    junction's row, while a bypass branch runs lower, below the sections its
    U-shape clears.  The corridor models that band CHOICE -- it owns one band per
    kind, computed once per fanning junction and cached on the routing context,
    so every branch of a kind shares one reference instead of each sizing its own
    and leaving the normalize stack to reconcile them.

    ``band_y`` is the inter-row-gap band (``None`` when the in-column gap below
    the junction is too narrow to hold the fan bundle).  ``bypass_band_y`` is the
    below-row bypass band: the deepest ``bypass_bottom_y`` across the fan's
    bypass branches, so a shallower sibling shares the deepest one's band rather
    than running a few px above it (``None`` when the fan has no bypass branch).
    A corridor exists whenever either band is available.  Descent-column sharing
    across co-descending branches runs through ``_fan_left_entry_descent_x``,
    keyed on the fan rank.
    """

    band_y: float | None = None
    bypass_band_y: float | None = None


@dataclass
class _RoutingCtx:
    """Pre-computed state shared by edge routing handlers."""

    graph: MetroGraph
    fold_x: float
    junction_ids: set[str]
    bottom_exit_junctions: set[str]
    bottom_exit_junction_ports: dict[str, str]
    offset_step: float
    fork_stations: set[str]
    join_stations: set[str]
    tb_sections: set[str]
    reversed_sections: set[str]
    positive_fan: set[str]
    bundle_info: dict[_EdgeKey, tuple[int, int]]
    bypass_gap_idx: dict[_EdgeKey, tuple[int, int, int, int]]
    station_offsets: dict[tuple[str, str], float] | None
    diagonal_run: float
    curve_radius: float
    skip_edges: set[_EdgeKey] = field(default_factory=set)
    built_routes: list[RoutedPath] = field(default_factory=list)
    junction_fan_info: dict[_EdgeKey, tuple[int, int]] = field(default_factory=dict)
    fan_corridors: dict[str, FanCorridor] = field(default_factory=dict)
    section_trunk_y: dict[str, float] = field(default_factory=dict)
    merge_fanouts: set[str] = field(default_factory=set)
    merge: _MergeRouting = field(
        default_factory=lambda: _MergeRouting(
            junctions=set(),
            trunk_source={},
            trunk_by={},
            entry_port_for={},
            skip_edges=set(),
            index_exclude=set(),
        )
    )


def _classify_merge_edges(
    graph: MetroGraph,
    junction_ids: set[str],
    join_sources: dict[str, set[str]],
    fork_targets: dict[str, set[str]],
) -> _MergeRouting:
    """Classify merge junction edges into trunk, branch, skip, and exclude.

    A merge junction has N>1 predecessors converging on one entry port.
    The farthest bypass predecessor becomes the "trunk" (full U-shape).
    Closer bypass predecessors are "branches" (truncated descents).
    """
    # Detect merge junctions
    junctions: set[str] = set()
    for jid in junction_ids:
        preds = join_sources.get(jid, set())
        succs = fork_targets.get(jid, set())
        if len(preds) > 1 and len(succs) == 1:
            succ_id = next(iter(succs))
            succ_port = graph.ports.get(succ_id)
            if succ_port and succ_port.is_entry:
                junctions.add(jid)

    trunk_source: dict[str, str] = {}
    trunk_by: dict[str, float] = {}
    entry_port_for: dict[str, str] = {}

    def _col_for_id(sid: str) -> int | None:
        st = graph.stations.get(sid)
        if st is None:
            return None
        return _resolve_section_col(graph, st)

    for mjid in junctions:
        mst = graph.stations.get(mjid)
        if not mst:
            continue
        tgt_col, tgt_row = _resolve_section_colrow(graph, mst)
        if tgt_col is None:
            continue

        # Resolve entry port (successor of merge junction)
        for e in graph.edges_from(mjid):
            ep = graph.ports.get(e.target)
            if ep and ep.is_entry:
                entry_port_for[mjid] = e.target
                break

        # Find farthest bypass predecessor (trunk carrier).  Branches
        # must land on the trunk's own bypass_bottom_y -- the value the
        # trunk's route actually uses -- not the deepest across all
        # preds (which can disagree when the cap-at-midpoint guard in
        # bypass_bottom_y fires for the trunk's span but not a closer
        # branch's).  The branch drop level must use the trunk route's own
        # ``cross_row`` decision (see ``merge_trunk_force_cross_row``) or it
        # lands at a different Y from where the trunk actually runs.
        farthest_source: str | None = None
        farthest_span = 0
        trunk_pred_by = 0.0
        for edge in graph.edges_to(mjid):
            pred = graph.stations.get(edge.source)
            if not pred:
                continue
            pred_col, pred_row = _resolve_section_colrow(graph, pred)
            if (
                pred_col is not None
                and abs(tgt_col - pred_col) > 1
                and _has_intervening_sections(graph, pred_col, tgt_col, pred_row)
            ):
                span = abs(tgt_col - pred_col)
                cross_row = merge_trunk_force_cross_row(
                    graph, pred_col, tgt_col, pred_row, tgt_row
                ) or (
                    pred_row is not None and tgt_row is not None and pred_row != tgt_row
                )
                by = bypass_bottom_y(
                    graph,
                    pred_col,
                    tgt_col,
                    BYPASS_CLEARANCE,
                    src_row=pred_row,
                    cross_row=cross_row,
                    tgt_row=tgt_row,
                )
                if span > farthest_span:
                    farthest_span = span
                    farthest_source = edge.source
                    trunk_pred_by = by
        if farthest_source and trunk_pred_by > 0:
            trunk_source[mjid] = farthest_source
            trunk_by[mjid] = trunk_pred_by

    # Classify edges into skip (not routed) and index_exclude
    # (routed as branches but excluded from gap indexing)
    skip_edges: set[_EdgeKey] = set()
    index_exclude: set[_EdgeKey] = set()

    for mjid, trunk_src in trunk_source.items():
        m_col = _col_for_id(mjid)

        # Check for adjacent JUNCTION predecessors whose stubs need
        # the merge -> entry edge to cross the full gap.  Adjacent
        # PORT predecessors get redirected, so merge -> entry is
        # redundant for them.
        has_adjacent_junction_pred = False
        if m_col is not None:
            for e2 in graph.edges_to(mjid):
                if e2.source == trunk_src or e2.source not in junction_ids:
                    continue
                p_col = _col_for_id(e2.source)
                if p_col is not None and abs(m_col - p_col) <= 1:
                    has_adjacent_junction_pred = True
                    break

        # merge -> entry: skip unless adjacent junction pred needs it
        if not has_adjacent_junction_pred:
            for edge in graph.edges_from(mjid):
                ep = graph.ports.get(edge.target)
                if ep and ep.is_entry:
                    skip_edges.add((edge.source, edge.target, edge.line_id))
        # Non-trunk bypass junction -> merge: exclude from indexing
        # (truncated branches shouldn't occupy bundle slots)
        if m_col is not None:
            for edge in graph.edges_to(mjid):
                if edge.source == trunk_src or edge.source not in junction_ids:
                    continue
                src_col = _col_for_id(edge.source)
                if src_col is not None and abs(m_col - src_col) > 1:
                    index_exclude.add((edge.source, edge.target, edge.line_id))

    return _MergeRouting(
        junctions=junctions,
        trunk_source=trunk_source,
        trunk_by=trunk_by,
        entry_port_for=entry_port_for,
        skip_edges=skip_edges,
        index_exclude=index_exclude,
    )


def _build_routing_context(
    graph: MetroGraph,
    diagonal_run: float,
    curve_radius: float,
    station_offsets: dict[tuple[str, str], float] | None,
) -> _RoutingCtx:
    """Pre-compute all shared state for edge routing."""
    junction_ids = graph.junction_ids

    # Fold edge: max X across all stations
    all_x = [s.x for s in graph.stations.values()]
    fold_x = max(all_x) if all_x else 0

    # Junctions fed by BOTTOM exit ports
    bottom_exit_junctions: set[str] = set()
    bottom_exit_junction_ports: dict[str, str] = {}
    for e in graph.edges:
        if e.target in junction_ids:
            port = graph.ports.get(e.source)
            if port and not port.is_entry and port.side == PortSide.BOTTOM:
                bottom_exit_junctions.add(e.target)
                bottom_exit_junction_ports[e.target] = e.source

    # Fork/join stations
    fork_targets: dict[str, set[str]] = defaultdict(set)
    join_sources: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges:
        fork_targets[e.source].add(e.target)
        join_sources[e.target].add(e.source)
    fork_stations = {sid for sid, tgts in fork_targets.items() if len(tgts) > 1}
    join_stations = {sid for sid, srcs in join_sources.items() if len(srcs) > 1}

    tb_sections = vertical_flow_sections(graph)
    reversed_sections = detect_reversed_sections(graph)
    positive_fan = tb_positive_fan_sections(graph)

    # Merge routing classification
    merge = _classify_merge_edges(graph, junction_ids, join_sources, fork_targets)

    # Section trunk Ys: the dominant on-track Y per LR/RL section, used
    # to detect side-branch ascents (a below-trunk station feeding the
    # trunk bundle).  Routing such edges with a late diagonal keeps the
    # side-branch line on its own track until the section boundary.
    section_trunk_y = _compute_section_trunk_ys(graph)

    # Bundle assignments and bypass gap indices
    line_priority = {lid: i for i, lid in enumerate(graph.lines.keys())}
    bundle_info = compute_bundle_info(
        graph, junction_ids, line_priority, bottom_exit_junctions
    )
    all_exclude = merge.skip_edges | merge.index_exclude
    bypass_gap_idx = _compute_bypass_gap_indices(
        graph, junction_ids, line_priority, skip_edges=all_exclude
    )
    junction_fan_info = _compute_junction_fan_info(
        graph, junction_ids, line_priority, skip_edges=all_exclude
    )
    fan_corridors = _compute_fan_corridors(
        graph, junction_fan_info, resolve_offset_step(graph.track_gap), merge.junctions
    )

    return _RoutingCtx(
        graph=graph,
        fold_x=fold_x,
        junction_ids=junction_ids,
        bottom_exit_junctions=bottom_exit_junctions,
        bottom_exit_junction_ports=bottom_exit_junction_ports,
        offset_step=resolve_offset_step(graph.track_gap),
        fork_stations=fork_stations,
        join_stations=join_stations,
        tb_sections=tb_sections,
        reversed_sections=reversed_sections,
        positive_fan=positive_fan,
        bundle_info=bundle_info,
        bypass_gap_idx=bypass_gap_idx,
        station_offsets=station_offsets,
        diagonal_run=diagonal_run,
        curve_radius=curve_radius,
        junction_fan_info=junction_fan_info,
        fan_corridors=fan_corridors,
        skip_edges=merge.skip_edges,
        section_trunk_y=section_trunk_y,
        merge=merge,
        merge_fanouts=merge_fanout_junctions(graph, merge.junctions),
    )


def compute_junction_fan_info(graph: MetroGraph) -> dict[_EdgeKey, tuple[int, int]]:
    """Unified per-edge fan positions for fan-out junctions.

    The offset-independent subset of :func:`_build_routing_context` needed by
    the fan-coincidence guard, so it need not build the full routing context
    just to read ``junction_fan_info``.
    """
    junction_ids = graph.junction_ids
    fork_targets: dict[str, set[str]] = defaultdict(set)
    join_sources: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges:
        fork_targets[e.source].add(e.target)
        join_sources[e.target].add(e.source)
    merge = _classify_merge_edges(graph, junction_ids, join_sources, fork_targets)
    line_priority = {lid: i for i, lid in enumerate(graph.lines.keys())}
    all_exclude = merge.skip_edges | merge.index_exclude
    return _compute_junction_fan_info(
        graph, junction_ids, line_priority, skip_edges=all_exclude
    )


def _compute_section_trunk_ys(graph: MetroGraph) -> dict[str, float]:
    """Return a mapping ``section_id -> trunk_y`` for LR/RL sections.

    The trunk Y is the Y of the section's exit/entry ports, which
    coincides with the dominant on-track bundle level.  Returns an
    empty mapping for sections without horizontal ports.
    """
    result: dict[str, float] = {}
    for sec_id, section in graph.sections.items():
        if section.direction not in ("LR", "RL"):
            continue
        port_ys: list[float] = []
        for pid in list(section.entry_ports) + list(section.exit_ports):
            port = graph.ports.get(pid)
            if port is None or port.side not in (PortSide.LEFT, PortSide.RIGHT):
                continue
            port_st = graph.stations.get(pid)
            if port_st is None:
                continue
            port_ys.append(port_st.y)
        if port_ys:
            # Use the median to ignore outliers; all LR ports in a section
            # typically share the same Y after alignment.
            port_ys.sort()
            result[sec_id] = port_ys[len(port_ys) // 2]
    return result


def _get_offset(ctx: _RoutingCtx, station_id: str, line_id: str) -> float:
    """Get the station offset for a (station, line) pair, defaulting to 0."""
    if ctx.station_offsets:
        return ctx.station_offsets.get((station_id, line_id), 0.0)
    return 0.0


def _max_offset_at(ctx: _RoutingCtx, station_id: str) -> float:
    """Get the maximum offset across all lines at a station."""
    if not ctx.station_offsets:
        return 0.0
    all_offs = [
        ctx.station_offsets.get((station_id, lid), 0.0)
        for lid in ctx.graph.station_lines(station_id)
    ]
    return max(all_offs) if all_offs else 0.0


def _section_lane_frame(
    graph: MetroGraph, section: Section, positive_fan: set[str] | None = None
) -> AxisFrame:
    """The :class:`AxisFrame` for *section*'s flow.

    The lane accessors read only the frame's secondary (lane) axis name and its
    :attr:`~AxisFrame.secondary_sign`, never the axis step, so an unresolved
    spacing falls back to a unit step rather than failing.

    A vertical-flow section whose bundle draws on the ``+x`` (feed) side
    (:func:`tb_positive_fan_sections`) carries a ``+1`` lane sign so the lane
    accessor reports the side the section actually draws on, matching
    :func:`_tb_x_offset`.  ``positive_fan`` lets a caller in a per-element loop
    pass that set once rather than re-deriving its reversal fixed-point per call.
    """
    frame = AxisFrame.for_direction(
        section.direction, graph.x_spacing or 1.0, graph.y_spacing or 1.0
    )
    fan = positive_fan if positive_fan is not None else tb_positive_fan_sections(graph)
    if section.id in fan:
        frame = replace(frame, secondary_sign=1.0)
    return frame


def port_lane_coord(
    graph: MetroGraph,
    port: Station,
    line_id: str,
    station_offsets: Mapping[tuple[str, str], float],
    positive_fan: set[str] | None = None,
) -> float:
    """Screen coordinate of *line_id* along *port*'s edge -- the along-edge accessor.

    A line's position along the edge its port sits on: an X on a TOP/BOTTOM
    port, a Y on a LEFT/RIGHT port.  Built through :func:`station_lane_coord`,
    so the section's lane sign (:attr:`AxisFrame.secondary_sign`) gives a
    vertical-flow section the rotation image of a horizontal one.  Sorting a
    port's lines by this value yields their arrival order along the edge.
    """
    if port.section_id is None:
        raise ValueError(f"port {port.id!r} has no section")
    section = graph.sections[port.section_id]
    frame = _section_lane_frame(graph, section, positive_fan)
    offset = station_offsets.get((port.id, line_id), 0.0)
    return station_lane_coord(frame, port, offset)


def port_arrival_order(
    graph: MetroGraph,
    port: Station,
    station_offsets: Mapping[tuple[str, str], float],
    positive_fan: set[str] | None = None,
) -> list[str]:
    """Lines at *port* in the order they cross its edge (arrival order).

    Ties (two lines sharing a lane coordinate) break on line id, so the order is
    independent of the input line listing.
    """
    return sorted(
        graph.station_lines(port.id),
        key=lambda lid: (
            port_lane_coord(graph, port, lid, station_offsets, positive_fan),
            lid,
        ),
    )


def _entry_port_for_line(
    graph: MetroGraph, section: Section, line_id: str
) -> Station | None:
    """The entry port *line_id* crosses into *section* through, if any."""
    for pid in section.entry_ports:
        if line_id in graph.station_lines(pid):
            return graph.stations[pid]
    return None


def lane_x(
    graph: MetroGraph,
    section: Section,
    line_id: str,
    station_offsets: Mapping[tuple[str, str], float],
    positive_fan: set[str] | None = None,
) -> float:
    """The lane coordinate *line_id* draws at inside *section* (rotation-pure).

    The single source of truth for where a line rides inside a section,
    anchored at the line's **entry port** so the order lines arrive along that
    edge is the order they ride down the column and the order they leave: lane
    order in == lane order along the flow == lane order out, by construction.

    Derived purely from the section's :class:`AxisFrame` (lane axis + sign) and
    the offsets at the port -- no global flow-direction knowledge.  A horizontal
    section returns ``y + offset``; a vertical one the rotation image
    ``x - offset``, never the reflection ``x + (max - offset)``.

    The seam invariant reads it as the coordinate an inter-section approach must
    land each line on.
    """
    port = _entry_port_for_line(graph, section, line_id)
    if port is not None:
        return port_lane_coord(graph, port, line_id, station_offsets, positive_fan)
    # A line that originates inside the section crosses no seam; anchor on a
    # representative internal station so the accessor stays total.
    frame = _section_lane_frame(graph, section, positive_fan)
    for sid in section.station_ids:
        station = graph.stations[sid]
        if not station.is_port:
            offset = station_offsets.get((sid, line_id), 0.0)
            return station_lane_coord(frame, station, offset)
    raise ValueError(f"section {section.id!r} has no anchor station for {line_id!r}")


def _tb_x_offset(
    ctx: _RoutingCtx, station_id: str, line_id: str, section_id: str | None
) -> float:
    """Compute the X offset for a line at a station in a vertical-flow section.

    A vertical-flow section is the horizontal model rotated 90 degrees: where an
    LR line rides ``y + offset``, a vertical-flow line rides
    ``x + secondary_sign * offset``.  The lane sign comes from the section's
    :func:`_section_lane_frame`: a downward (TB) section rides ``-1`` (bundle
    left of the column), its upward (BT) image rides ``+1``, and either flips to
    ``+1`` for a section in :func:`tb_positive_fan_sections` whose bundle sits on
    the ``+x`` (feed) side so the seam corner nests as a rotation, not a pinching
    reflection.
    """
    section = ctx.graph.sections.get(section_id) if section_id else None
    offset = _get_offset(ctx, station_id, line_id)
    if section is None:
        return -offset
    frame = _section_lane_frame(ctx.graph, section, ctx.positive_fan)
    return lane_delta(frame, offset)


def _resolve_section_col(graph: MetroGraph, station: Station) -> int | None:
    """Resolve the grid column for a port or junction station."""
    sec = resolve_section(graph, station, prefer_upstream=False)
    if sec and sec.grid_col >= 0:
        return sec.grid_col
    return None


def _resolve_section_row(graph: MetroGraph, station: Station) -> int | None:
    """Resolve the grid row for a port or junction station."""
    sec = resolve_section(graph, station, prefer_upstream=False)
    if sec and sec.grid_row >= 0:
        return sec.grid_row
    return None


def _resolve_section_colrow(
    graph: MetroGraph, station: Station | None
) -> tuple[int | None, int | None]:
    """Resolve grid ``(col, row)`` for a port/junction station in one pass.

    ``_resolve_section_col`` and ``_resolve_section_row`` each re-resolve the
    section (an adjacency walk); callers needing both should resolve once.
    """
    sec = resolve_section(graph, station, prefer_upstream=False)
    if sec is None:
        return None, None
    col = sec.grid_col if sec.grid_col >= 0 else None
    row = sec.grid_row if sec.grid_row >= 0 else None
    return col, row


def _has_intervening_sections(
    graph: MetroGraph,
    src_col: int,
    tgt_col: int,
    src_row: int | None = None,
) -> bool:
    """Check if any same-row sections exist in columns strictly between src and tgt."""
    lo, hi = min(src_col, tgt_col), max(src_col, tgt_col)
    for s in graph.sections.values():
        if s.bbox_w > 0 and lo < s.grid_col < hi:
            if src_row is None or s.grid_row == src_row:
                return True
    return False


def _intervening_section_obstructs(
    graph: MetroGraph,
    src_col: int,
    src_row: int | None,
    tgt_col: int,
    tgt_row: int | None,
) -> bool:
    """Whether a multi-column hop is blocked by a section in a column it spans.

    The horizontal run blocks on the source row, or - for a cross-row L-shape,
    whose horizontal leg runs at the target entry Y - the target row, plowed
    through even when the source row is clear. Only meaningful when the columns
    are more than one apart; an adjacent hop has no column between them to
    intervene.
    """
    if abs(tgt_col - src_col) <= 1:
        return False
    if _has_intervening_sections(graph, src_col, tgt_col, src_row):
        return True
    return (
        src_row is not None
        and tgt_row is not None
        and tgt_row != src_row
        and _has_intervening_sections(graph, src_col, tgt_col, tgt_row)
    )


def is_far_side_around_below_left_entry(graph: MetroGraph, port: Port) -> bool:
    """Whether *port* is a LEFT entry reached by an around-below wrap.

    A LEFT entry fed by a LEFT-exit source more than one column to its RIGHT,
    with an intervening section, is a reverse-flow bypass that leaves the source
    westward, drops below every box, and rises into the far-side port from its
    outward side -- a half-turn that transposes the bundle end-to-end.  Routed
    by ``_route_left_exit_around_below_left_entry``; the destination section takes
    the feeder's delivered order transposed once by the seam classifier
    (``_reorder_reconvergence``) and the layout reserves left clearance for the
    wrap.

    Pure topology (grid columns, port sides, intervening sections), so it reads
    the same before global coordinates are assigned and after.
    """
    if not (port.is_entry and port.side is PortSide.LEFT):
        return False
    psec = graph.sections.get(port.section_id)
    if psec is None:
        return False
    for edge in graph.edges_to(port.id):
        src = graph.stations.get(edge.source)
        src_port = graph.ports.get(edge.source)
        if src is None or src_port is None or src_port.is_entry:
            continue
        if src_port.side is not PortSide.LEFT:
            continue
        scol, srow = _resolve_section_colrow(graph, src)
        if scol is None or scol - psec.grid_col <= 1:
            continue
        if _has_intervening_sections(
            graph, scol, psec.grid_col, srow
        ) or _has_intervening_sections(graph, scol, psec.grid_col, psec.grid_row):
            return True
    return False


def is_near_vertical_drop(dx: float, dy: float) -> bool:
    """Whether a junction-to-entry hop is the near-vertical same-column drop.

    The hop is within ``JUNCTION_MARGIN`` horizontally and far steeper than it is
    wide.  Shared by the routing dispatch
    (``_InterFacts.is_near_vertical_same_col_junction``) and the offset-reversal
    predicate so the two cannot drift.
    """
    return abs(dx) <= JUNCTION_MARGIN + COORD_TOLERANCE and abs(dy) > abs(dx) * 3


def is_near_vertical_junction_right_entry(graph: MetroGraph, port: Port) -> bool:
    """Whether *port* is a RIGHT entry a multi-line fan-out junction drops into.

    Detected so the section's line order can be reversed to match the descent's
    transpose (see ``_reverse_near_vertical_junction_right_entry_offsets``).  The
    same near-vertical geometry the routing dispatch deflects to the standard
    drop (``_InterFacts.takes_near_vertical_junction_drop``): the junction and
    port share a grid column, the junction overhangs the port's outward edge, the
    drop is near-vertical, and at least two lines descend together (mirroring the
    dispatch's ``f.n >= 2``; a lone line has no bundle to transpose).
    """
    if not (port.is_entry and port.side is PortSide.RIGHT):
        return False
    psec = graph.sections.get(port.section_id)
    pst = graph.stations.get(port.id)
    if psec is None or pst is None:
        return False
    lines_by_source: dict[str, set[str]] = defaultdict(set)
    for edge in graph.edges_to(port.id):
        lines_by_source[edge.source].add(edge.line_id)
    for source, lines in lines_by_source.items():
        jst = graph.stations.get(source)
        if (
            source not in graph.junction_ids
            or jst is None
            or _resolve_section_col(graph, jst) != psec.grid_col
        ):
            continue
        if (
            len(lines) >= 2
            and jst.x >= pst.x - COORD_TOLERANCE
            and is_near_vertical_drop(pst.x - jst.x, pst.y - jst.y)
        ):
            return True
    return False


def fanout_divergence_peel_order(
    graph: MetroGraph,
    jid: str,
    line_priority: dict[str, int],
) -> list[str] | None:
    """Peel order for distinct lines diverging from a shared fan-out junction.

    Returns the outgoing line ids ordered outermost-to-innermost for the turn
    out of *jid* -- the order that lets each line peel into its own target
    without crossing a bundle mate -- or ``None`` when *jid* is not a clean
    divergence and the bundle should keep its declaration order.

    The order is crossing-free only when the farthest-reaching line rides the
    outer side of the descent (dropping DOWN, the top slot; rising UP, the
    bottom): then the nearer line peels off on the inside and clears the farther
    line's onward run.  Both the fan-channel assignment and the source-section
    bundle ordering read this single order so the descent X order and the
    lead-in Y order stay in phase.

    The clean-divergence preconditions: one upstream source; at least two
    distinct lines; every line reaches its own target section (disjoint targets,
    so a co-travelling multi-target bundle is left alone); all descending lines
    drop the same way.  Two fan shapes qualify: a horizontal fan spreading to at
    least two distinct columns (farthest column outermost), and a vertical fan
    whose lines share one column but peel to at least two distinct rows (the
    lead-in Y order mirrors the target rows top to bottom, a same-row
    continuation leading as the shallowest).
    """
    sources = {e.source for e in graph.edges_to(jid)}
    if len(sources) != 1:
        return None
    jst = graph.stations.get(jid)
    if jst is None:
        return None
    src_col, src_row = _resolve_section_colrow(graph, jst)
    if src_col is None or src_row is None:
        return None

    reach: dict[str, int] = {}
    drow: dict[str, int] = {}
    claimed: dict[str, str] = {}
    for edge in graph.edges_from(jid):
        tgt = graph.stations.get(edge.target)
        if tgt is None or not (tgt.is_port or edge.target in graph.junction_ids):
            return None
        tgt_port = graph.ports.get(edge.target)
        if tgt_port is None or not tgt_port.is_entry:
            return None
        tcol, trow = _resolve_section_colrow(graph, tgt)
        if tcol is None or trow is None:
            return None
        if edge.target in claimed and claimed[edge.target] != edge.line_id:
            return None  # two distinct lines share a target: co-travelling
        if edge.line_id in reach:
            return None  # a line splitting to several targets is not a per-line fan
        claimed[edge.target] = edge.line_id
        reach[edge.line_id] = tcol - src_col
        drow[edge.line_id] = trow - src_row

    if len(reach) < 2:
        return None

    if len(set(reach.values())) == 1:
        # Same-column vertical fan: a same-row continuation (drow 0) is a valid
        # member that leads as the shallowest peel, unlike the column-spreading
        # branch below, which rejects any zero descent.
        descenders = [d for d in drow.values() if d != 0]
        if len(set(drow.values())) < 2 or len({d > 0 for d in descenders}) != 1:
            return None
        return sorted(reach, key=lambda lid: (drow[lid], line_priority.get(lid, 0)))

    if 0 in drow.values():
        return None
    if len({d > 0 for d in drow.values()}) != 1:
        return None

    drop_down = next(iter(drow.values())) > 0
    return sorted(
        reach,
        key=lambda lid: (
            -abs(reach[lid]) if drop_down else abs(reach[lid]),
            line_priority.get(lid, 0),
        ),
    )


def _compute_bypass_gap_indices(
    graph: MetroGraph,
    junction_ids: set[str],
    line_priority: dict[str, int] | None = None,
    skip_edges: set[tuple[str, str, str]] | None = None,
) -> dict[tuple[str, str, str], tuple[int, int, int, int]]:
    """Assign per-gap indices for bypass routes sharing physical gaps.

    Bypass routes from different source columns can share the same
    physical gap (e.g., routes from cols 1->4 and 2->4 both use the
    gap between cols 3 and 4 for their gap2 vertical).  This function
    groups bypass routes by their gap1 and gap2 column pairs and
    assigns per-gap indices so each route gets a unique X offset.

    Edges in *skip_edges* are excluded so truncated merge branches
    don't create gaps in the bundle.

    Returns
    -------
    dict mapping (source_id, target_id, line_id) to
    (gap1_idx, gap1_count, gap2_idx, gap2_count).
    """
    _skip = skip_edges or set()
    EdgeKey = tuple[str, str, str]
    bypass_edges: list[tuple[EdgeKey, int, int, float]] = []

    for edge in graph.edges:
        ekey: EdgeKey = (edge.source, edge.target, edge.line_id)
        if ekey in _skip:
            continue

        src, tgt = graph.edge_endpoints(edge)

        is_inter = (src.is_port or edge.source in junction_ids) and (
            tgt.is_port or edge.target in junction_ids
        )
        if not is_inter:
            continue

        src_col, src_row = _resolve_section_colrow(graph, src)
        tgt_col = _resolve_section_col(graph, tgt)
        if (
            src_col is None
            or tgt_col is None
            or abs(tgt_col - src_col) <= 1
            or not _has_intervening_sections(graph, src_col, tgt_col, src_row)
        ):
            continue

        dx = tgt.x - src.x
        bypass_edges.append((ekey, src_col, tgt_col, dx))

    gap1_groups: dict[tuple[int, int], list[tuple[EdgeKey, int]]] = defaultdict(list)
    gap2_groups: dict[tuple[int, int], list[tuple[EdgeKey, int, str]]] = defaultdict(
        list
    )

    for ekey, src_col, tgt_col, dx in bypass_edges:
        line_id = ekey[2]
        if dx > 0:
            gap1_pair = (src_col, src_col + 1)
            gap2_pair = (tgt_col - 1, tgt_col)
        else:
            gap1_pair = (src_col - 1, src_col)
            gap2_pair = (tgt_col, tgt_col + 1)
        gap1_groups[gap1_pair].append((ekey, src_col))
        gap2_groups[gap2_pair].append((ekey, src_col, line_id))

    gap1_idx: dict[EdgeKey, tuple[int, int]] = {}
    gap2_idx: dict[EdgeKey, tuple[int, int]] = {}

    for group in gap1_groups.values():
        group.sort(key=lambda x: x[1])
        n = len(group)
        for j, (ek, _) in enumerate(group):
            gap1_idx[ek] = (j, n)

    lp = line_priority or {}
    for gap2_group in gap2_groups.values():
        # Sort by line priority so the lowest-offset line (highest
        # priority) gets the outermost vertical channel.  This
        # prevents crossings when lines converge at an entry port
        # from different source columns.
        gap2_group.sort(key=lambda x: lp.get(x[2], 0))
        n = len(gap2_group)
        for j, (ek, _sc, _lid) in enumerate(gap2_group):
            gap2_idx[ek] = (j, n)

    result: dict[EdgeKey, tuple[int, int, int, int]] = {}
    all_keys = set(gap1_idx) | set(gap2_idx)
    for ek in all_keys:
        g1_j, g1_n = gap1_idx.get(ek, (0, 1))
        g2_j, g2_n = gap2_idx.get(ek, (0, 1))
        result[ek] = (g1_j, g1_n, g2_j, g2_n)

    return result


def _compute_junction_fan_info(
    graph: MetroGraph,
    junction_ids: set[str],
    line_priority: dict[str, int],
    skip_edges: set[tuple[str, str, str]] | None = None,
) -> dict[tuple[str, str, str], tuple[int, int]]:
    """Unified fan-out positions for junctions with mixed-direction edges.

    When a junction fans out to targets that would otherwise be routed
    by DIFFERENT inter-section handlers (L-shape, bypass, LEFT/RIGHT
    entry wrap), all edges of the SAME line share a single vertical
    channel so they travel together through the first corner and only
    diverge afterward.

    Assigns per-LINE positions (not per-edge), so test->preprocess and
    test->normalization share the same (i, n) and thus the same
    vertical channel X.

    Edges in *skip_edges* are excluded so that truncated merge branches
    don't create gaps in the bundle.
    """
    _skip = skip_edges or set()
    result: dict[tuple[str, str, str], tuple[int, int]] = {}

    for jid in junction_ids:
        jst = graph.stations.get(jid)
        if not jst:
            continue
        src_col, src_row = _resolve_section_colrow(graph, jst)
        if src_col is None:
            continue

        # Classify each outgoing edge into one of: L-shape (adjacent col,
        # no intervening sections), bypass (column gap with intervening),
        # or wrap (LEFT/RIGHT entry port reached from the opposite side,
        # going via the inter-row channel).  A junction qualifies for a
        # unified shared-first-corner fan when its outgoing edges use at
        # least two of these three handlers, since each handler would
        # otherwise pick a different source-side channel X.
        #
        # Category detection uses ALL outgoing inter-section edges
        # (including merge-branch edges that get skipped from the
        # per-line position assignment).  Otherwise a junction whose
        # bypass siblings are all routed as merge branches would lose
        # its bypass category and the wrap-only / L-only fallback would
        # leave the wrap routes free to pick a different corner X than
        # the bypass routes.
        # The cross-row bypass clause below is scoped to multi-line junctions:
        # it newly classifies a cross-row sibling as a bypass so distinct lines
        # peeling to different targets share one fan corner.  A single-line
        # fan-out is fused by the coincidence pass, so applying it there would
        # only reroute it identically (or, worse, perturb a merge feeder), so a
        # single-line junction keeps the same-row-only classification main uses.
        multi_line = len({e.line_id for e in graph.edges_from(jid)}) >= 2
        outgoing: list[Edge] = []
        has_lshape = False
        has_bypass = False
        has_wrap = False
        # Source-side channels anchored NEAR the junction (no resolvable
        # inter-column gap to centre in) keyed by their rounded X.  These are
        # NOT touched by ``_materialize_gap_slots`` (which only re-stacks
        # channels inside a real column gap), so two distinct lines to two
        # distinct targets that resolve to the same near-source X overlay
        # there; the unified fan is the only thing that separates them.
        near_src_channel: dict[int, set[tuple[str, str]]] = defaultdict(set)
        for edge in graph.edges_from(jid):
            tgt = graph.stations.get(edge.target)
            if not tgt or not (tgt.is_port or edge.target in junction_ids):
                continue
            tgt_col, tgt_row = _resolve_section_colrow(graph, tgt)
            if tgt_col is None:
                continue
            tgt_port = graph.ports.get(edge.target)
            # Mirror ``_build_inter_facts.needs_bypass``: an intervening section
            # blocks the source row, or - for a cross-row hop, whose horizontal
            # leg runs at the target entry Y - the target row, plowed through
            # even when the source row is clear.  A fan-out junction sits in its
            # source row while its targets sit a row below, so the target-row
            # clause is what classifies the bypassing sibling (multi-line only).
            is_bypass = abs(tgt_col - src_col) > 1 and (
                _has_intervening_sections(graph, src_col, tgt_col, src_row)
                or (
                    multi_line
                    and src_row is not None
                    and tgt_row is not None
                    and tgt_row != src_row
                    and _has_intervening_sections(graph, src_col, tgt_col, tgt_row)
                )
            )
            dx_edge = tgt.x - jst.x
            is_wrap = (
                tgt_port is not None
                and tgt_port.is_entry
                and not is_bypass
                and src_row is not None
                and tgt_row is not None
                and src_row != tgt_row
                and (
                    (tgt_port.side == PortSide.RIGHT and dx_edge > 0)
                    or (tgt_port.side == PortSide.LEFT and dx_edge < 0)
                )
            )
            if is_bypass:
                has_bypass = True
            elif is_wrap:
                has_wrap = True
            else:
                has_lshape = True
            # A plain L-shape into an ADJACENT column gets a true inter-column
            # gap channel that _materialize_gap_slots centres + separates, so
            # it is not a near-source overlay.  Every other source-anchored
            # case - a non-adjacent / same-column L-shape (near-source
            # fallback) or a wrap (source-side V1 hugging the junction, exempt
            # from gap-normalise) - hugs the source with no gap to normalise.
            lshape_gap_channel = not is_wrap and abs(tgt_col - src_col) == 1
            if not is_bypass and not lshape_gap_channel:
                near_src_channel[round(jst.x)].add((edge.line_id, edge.target))
            if (edge.source, edge.target, edge.line_id) not in _skip:
                outgoing.append(edge)

        # A near-source channel shared by 2+ distinct lines headed to 2+
        # distinct targets overlays those lines (no gap-normalise pass
        # reaches it); the unified fan gives each line its own slot.
        near_src_collision = any(
            len({lid for lid, _ in members}) >= 2 and len({t for _, t in members}) >= 2
            for members in near_src_channel.values()
        )

        # Unified fan-out only when source-side channels would otherwise
        # overlay distinct lines:
        #   * lshape + bypass: the legacy condition.
        #   * wrap + anything else: extends the same idea to wrap routes.
        #   * near_src_collision: distinct lines to distinct targets sharing
        #     one source-hugging channel that no gap-normalise pass reaches.
        # A junction whose source-side channels are all distinct (e.g. a pure
        # adjacent-column L-shape fan whose gap channels _materialize_gap_slots
        # separates) needs no shared first corner.
        needs_unified = (
            (has_lshape and has_bypass)
            or (has_wrap and (has_lshape or has_bypass))
            or near_src_collision
        )
        if not outgoing or not needs_unified:
            continue

        # Assign one position per unique line_id (sorted by priority).
        # All edges of the same line share that position, including
        # edges previously SKIPPED for the bundle.  This makes a merge
        # branch route share the same source-side fan position as a
        # sibling wrap/L-shape route, so all of them pivot through the
        # same first corner X (their geometries diverge afterward).
        all_outgoing = [
            e
            for e in graph.edges_from(jid)
            if (es := graph.stations.get(e.target)) is not None
            and (es.is_port or e.target in junction_ids)
        ]
        # A clean divergence (distinct lines peeling to disjoint targets) is
        # ordered outermost-to-innermost by reach so the descent X order stays
        # in phase with the source-section bundle's lead-in Y order; every other
        # fan (co-travelling bundles, mixed targets) keeps declaration order.
        peel_order = fanout_divergence_peel_order(graph, jid, line_priority)
        if peel_order is not None:
            line_ids = peel_order
        else:
            line_ids = sorted(
                {e.line_id for e in all_outgoing},
                key=lambda lid: line_priority.get(lid, 0),
            )
        line_pos = {lid: i for i, lid in enumerate(line_ids)}
        n = len(line_ids)

        for edge in all_outgoing:
            if edge.line_id in line_pos:
                result[(edge.source, edge.target, edge.line_id)] = (
                    line_pos[edge.line_id],
                    n,
                )

    return result


def _compute_fan_corridors(
    graph: MetroGraph,
    junction_fan_info: dict[_EdgeKey, tuple[int, int]],
    offset_step: float,
    merge_junctions: set[str],
) -> dict[str, FanCorridor]:
    """Shared traverse bands per fanning junction, one per band kind.

    A junction fans branches into the row(s) below through handlers that traverse
    different bands: the inter-row-gap branches (top-entry L-shape drops into the
    gap directly beneath the junction's row and turns; LEFT/RIGHT entry wrap
    traverses it before descending) share ``band_y``, while bypass branches share
    the lower ``bypass_band_y``.  Pinning one band per kind lets those branches
    share a reference rather than each centring an independent run.  The inter-row
    gap is measured in the junction's own column (matching
    :func:`_route_inter_row_gap_corridor`), so a tall row-span section stacked in
    another column does not collapse it; it is ``None`` when its row is bottommost,
    the fan is same-row or above, or the in-column gap is too narrow for the
    bundle.  A junction with neither band gets no corridor.

    ``merge_junctions`` are excluded from the bypass band: a feed into a merge
    converges on the merge's own ``trunk_by`` drop level, not a fan band.
    """
    fan_n: dict[str, int] = {}
    for (jsrc, _tgt, _lid), (_i, n) in junction_fan_info.items():
        fan_n[jsrc] = n

    corridors: dict[str, FanCorridor] = {}
    for jid, n in fan_n.items():
        jst = graph.stations.get(jid)
        if jst is None:
            continue
        col, src_row = _resolve_section_colrow(graph, jst)
        if src_row is None or col is None:
            continue
        upper_bottom = row_bottom_edge(graph, src_row, col=col)
        lower_top = row_top_edge(graph, src_row + 1, col=col, default=upper_bottom)
        band_y = fan_corridor_band(upper_bottom, lower_top, span=(n - 1) * offset_step)
        bypass_band_y = _fan_bypass_band(graph, jid, col, src_row, merge_junctions)
        if band_y is None and bypass_band_y is None:
            continue
        corridors[jid] = FanCorridor(band_y=band_y, bypass_band_y=bypass_band_y)
    return corridors


def _fan_bypass_band(
    graph: MetroGraph,
    jid: str,
    src_col: int,
    src_row: int,
    merge_junctions: set[str],
) -> float | None:
    """Deepest below-row bypass band across a fanning junction's bypass branches.

    Each of a fan's bypass branches sizes its own traverse via
    :func:`bypass_bottom_y` from the sections its U-shape clears; siblings that
    span different columns can land a few px apart.  Return the deepest so the
    shallower ones share it rather than running above it -- ``max`` guarantees no
    branch is pulled shallower than the sections it must clear.  ``None`` when the
    fan has no cross-column bypass branch.

    Whether an edge bypasses is :func:`_intervening_section_obstructs`, the same
    predicate :func:`_build_inter_facts` uses for ``needs_bypass`` -- so the band
    reflects the edges the dispatcher actually routes via ``_route_bypass``.  A
    feed into a merge junction is skipped: it converges on the merge's own drop
    level, not this band.
    """
    deepest: float | None = None
    for edge in graph.edges_from(jid):
        if edge.target in merge_junctions:
            continue
        tgt = graph.stations.get(edge.target)
        if tgt is None:
            continue
        tgt_col, tgt_row = _resolve_section_colrow(graph, tgt)
        if tgt_col is None:
            continue
        if not _intervening_section_obstructs(
            graph, src_col, src_row, tgt_col, tgt_row
        ):
            continue
        by = bypass_bottom_y(
            graph,
            src_col,
            tgt_col,
            BYPASS_CLEARANCE,
            src_row=src_row,
            cross_row=tgt_row is not None and tgt_row != src_row,
            tgt_row=tgt_row,
        )
        deepest = by if deepest is None else max(deepest, by)
    return deepest
