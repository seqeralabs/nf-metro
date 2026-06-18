"""Routing context: _RoutingCtx, the context builder, and shared
section-geometry helpers used across the routing handlers.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from nf_metro.layout.constants import (
    BYPASS_CLEARANCE,
    OFFSET_STEP,
)
from nf_metro.layout.routing.common import (
    bypass_bottom_y,
    compute_bundle_info,
    has_other_row_section_in_col_range,
    resolve_section,
)
from nf_metro.layout.routing.corners import (
    reversed_offset,
)
from nf_metro.parser.model import (
    Edge,
    MetroGraph,
    PortSide,
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
    tb_right_entry: set[str]
    bundle_info: dict[_EdgeKey, tuple[int, int]]
    bypass_gap_idx: dict[_EdgeKey, tuple[int, int, int, int]]
    station_offsets: dict[tuple[str, str], float] | None
    diagonal_run: float
    curve_radius: float
    skip_edges: set[_EdgeKey] = field(default_factory=set)
    junction_fan_info: dict[_EdgeKey, tuple[int, int]] = field(default_factory=dict)
    section_trunk_y: dict[str, float] = field(default_factory=dict)
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
        tgt_col = _resolve_section_col(graph, mst)
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
        # branch's).  The trunk route (``_route_merge_trunk``) forces
        # ``cross_row`` whenever its span shares the merge's row but
        # straddles another row's section, so its channel runs below
        # everything rather than in the same-row inter-section gap; the
        # branch drop level must use the same decision or it lands at a
        # different Y from where the trunk actually runs.
        tgt_row = _resolve_section_row(graph, mst)
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
                force_cross_row = (
                    pred_row is not None
                    and tgt_row == pred_row
                    and has_other_row_section_in_col_range(
                        graph, pred_col, tgt_col, pred_row
                    )
                )
                cross_row = force_cross_row or (
                    pred_row is not None
                    and tgt_row is not None
                    and pred_row != tgt_row
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

    # TB sections and their entry sides
    tb_sections = {sid for sid, s in graph.sections.items() if s.direction == "TB"}
    tb_right_entry: set[str] = set()
    for port in graph.ports.values():
        if (
            port.is_entry
            and port.side == PortSide.RIGHT
            and port.section_id in tb_sections
        ):
            tb_right_entry.add(port.section_id)

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

    return _RoutingCtx(
        graph=graph,
        fold_x=fold_x,
        junction_ids=junction_ids,
        bottom_exit_junctions=bottom_exit_junctions,
        bottom_exit_junction_ports=bottom_exit_junction_ports,
        offset_step=OFFSET_STEP,
        fork_stations=fork_stations,
        join_stations=join_stations,
        tb_sections=tb_sections,
        tb_right_entry=tb_right_entry,
        bundle_info=bundle_info,
        bypass_gap_idx=bypass_gap_idx,
        station_offsets=station_offsets,
        diagonal_run=diagonal_run,
        curve_radius=curve_radius,
        junction_fan_info=junction_fan_info,
        skip_edges=merge.skip_edges,
        section_trunk_y=section_trunk_y,
        merge=merge,
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


def _tb_x_offset(
    ctx: _RoutingCtx, station_id: str, line_id: str, section_id: str | None
) -> float:
    """Compute the TB-aware X offset for a station.

    RIGHT-entry sections use non-reversed offsets; others use reversed.
    """
    off = _get_offset(ctx, station_id, line_id)
    if section_id in ctx.tb_right_entry:
        return off
    return reversed_offset(off, _max_offset_at(ctx, station_id))


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
    graph: MetroGraph, station: Station
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

        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if not src or not tgt:
            continue

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
        outgoing: list[Edge] = []
        has_lshape = False
        has_bypass = False
        has_wrap = False
        # Source-side channels anchored NEAR the junction (no resolvable
        # inter-column gap to centre in) keyed by their rounded X.  These are
        # NOT touched by ``_normalize_gap_channels`` (which only re-stacks
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
            is_bypass = abs(tgt_col - src_col) > 1 and _has_intervening_sections(
                graph, src_col, tgt_col, src_row
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
            # gap channel that _normalize_gap_channels centres + separates, so
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
        # adjacent-column L-shape fan whose gap channels _normalize_gap_channels
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
