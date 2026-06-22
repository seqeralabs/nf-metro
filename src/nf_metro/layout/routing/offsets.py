"""Station offset computation for per-line Y positioning within bundles."""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field

from nf_metro.layout.constants import (
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    OFFSET_STEP,
    SAME_Y_TOLERANCE,
)
from nf_metro.layout.geometry import AxisFrame, axis_split
from nf_metro.layout.routing.common import tb_right_entry_sections
from nf_metro.layout.routing.context import (
    _has_intervening_sections,
    _resolve_section_col,
    _resolve_section_colrow,
    fanout_divergence_peel_order,
)
from nf_metro.layout.routing.corners import reversed_offset
from nf_metro.layout.routing.invariants import (
    check_partial_branch_offset_gaps,
    classify_merge_port_feeders,
    distinct_offset_levels,
)
from nf_metro.layout.routing.reversal import detect_reversed_sections
from nf_metro.parser.model import (
    LineSpread,
    MetroGraph,
    Port,
    PortSide,
    Section,
    Station,
)

# Tolerances used across offset phases
_SAME_Y_TOLERANCE: float = SAME_Y_TOLERANCE
_OFFSET_EQ_TOLERANCE: float = 0.001


@dataclass
class _OffsetCtx:
    """Shared state threaded through offset computation phases."""

    graph: MetroGraph
    offsets: dict[tuple[str, str], float] = field(default_factory=dict)
    line_priority: dict[str, int] = field(default_factory=dict)
    max_priority: int = 0
    offset_step: float = OFFSET_STEP
    compact: bool = False
    reversed_sections: set[str] = field(default_factory=set)
    tb_sections: set[str] = field(default_factory=set)
    lr_rl_sections: set[str] = field(default_factory=set)
    # Pre-computed per-station inbound/outbound line sets
    inbound: dict[str, set[str]] = field(default_factory=dict)
    outbound: dict[str, set[str]] = field(default_factory=dict)
    # Section -> flat-frame component root, populated by section-local re-indexing
    frame_roots: dict[str, str] = field(default_factory=dict)


def _build_offset_ctx(graph: MetroGraph, offset_step: float) -> _OffsetCtx:
    """Build shared context for offset computation phases."""
    line_order = list(graph.lines.keys())
    line_priority = {lid: i for i, lid in enumerate(line_order)}
    max_priority = len(line_order) - 1 if line_order else 0
    compact = graph.compact_offsets

    inbound: dict[str, set[str]] = {sid: set() for sid in graph.stations}
    outbound: dict[str, set[str]] = {sid: set() for sid in graph.stations}
    for edge in graph.edges:
        if edge.target in inbound:
            inbound[edge.target].add(edge.line_id)
        if edge.source in outbound:
            outbound[edge.source].add(edge.line_id)

    reversed_sections = detect_reversed_sections(graph)
    tb_sections = {sid for sid, s in graph.sections.items() if s.direction == "TB"}
    lr_rl_sections = {
        sid for sid, s in graph.sections.items() if s.direction in ("LR", "RL")
    }

    return _OffsetCtx(
        graph=graph,
        line_priority=line_priority,
        max_priority=max_priority,
        offset_step=offset_step,
        compact=compact,
        reversed_sections=reversed_sections,
        tb_sections=tb_sections,
        lr_rl_sections=lr_rl_sections,
        inbound=inbound,
        outbound=outbound,
    )


def _build_same_y_adj(
    graph: MetroGraph,
) -> dict[str, dict[str, list[tuple[str, str]]]]:
    """Build same-Y adjacency index per section.

    For each section, maps station_id -> [(neighbour_id, line_id)] for
    edges where both endpoints share the same Y coordinate (within
    tolerance).  Used by offset phases that propagate changes along
    horizontal runs.
    """
    same_y_adj: dict[str, dict[str, list[tuple[str, str]]]] = {}
    for edge in graph.edges:
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if not src or not tgt:
            continue
        if not src.section_id or src.section_id != tgt.section_id:
            continue
        if abs(src.y - tgt.y) > _SAME_Y_TOLERANCE:
            continue
        sec_id = src.section_id
        if sec_id not in same_y_adj:
            same_y_adj[sec_id] = {}
        same_y_adj[sec_id].setdefault(edge.source, []).append(
            (edge.target, edge.line_id)
        )
        same_y_adj[sec_id].setdefault(edge.target, []).append(
            (edge.source, edge.line_id)
        )
    return same_y_adj


def _compute_base_offsets(ctx: _OffsetCtx) -> None:
    """Assign initial per-station offsets from global line priority.

    In compact mode, only allocates slots for the max lines on either
    side of each station.  In non-compact mode, uses global priority
    directly.  Single-line non-port stations that are the sole occupant
    of their Y row within their section get offset 0 to stay on-grid.
    """
    graph = ctx.graph

    # Pre-compute which single-line stations should get offset 0.
    # In pure fan-out sections (all non-port stations carry a single
    # line), priority offsets are meaningless - there are no multi-line
    # bundles to separate - and they just push station markers off the
    # layout grid.  In mixed sections (some multi-line stations),
    # priority offsets maintain visual consistency with the routing.
    #
    # Additional guard: stations sharing a Y row with another single-
    # line station keep priority offsets to stay visually distinct.
    sec_has_multi: dict[str | None, bool] = {}
    sec_y_candidates: dict[tuple[str | None, float], list[str]] = {}
    for sid_s, st in graph.stations.items():
        if st.is_port:
            continue
        if len(graph.station_lines(sid_s)) > 1:
            sec_has_multi[st.section_id] = True
        else:
            bucket_y = round(st.y / _SAME_Y_TOLERANCE) * _SAME_Y_TOLERANCE
            sec_y_candidates.setdefault((st.section_id, bucket_y), []).append(sid_s)
    y_solo = {
        sids[0]
        for (sec_id, _), sids in sec_y_candidates.items()
        if len(sids) == 1 and not sec_has_multi.get(sec_id)
    }

    for sid in graph.stations:
        lines = graph.station_lines(sid)
        if not lines:
            continue
        station = graph.stations[sid]
        reverse = station.section_id in ctx.reversed_sections

        if ctx.compact:
            max_side = max(len(ctx.inbound[sid]), len(ctx.outbound[sid]), 1)
            if max_side <= 1:
                for lid in lines:
                    ctx.offsets[(sid, lid)] = 0.0
            else:
                if len(ctx.inbound[sid]) >= len(ctx.outbound[sid]):
                    ref = ctx.inbound[sid]
                else:
                    ref = ctx.outbound[sid]
                ref_sorted = sorted(ref, key=lambda lid: ctx.line_priority.get(lid, 0))
                ref_idx = {lid: i for i, lid in enumerate(ref_sorted)}
                local_max = max_side - 1
                for lid in lines:
                    idx = ref_idx.get(lid, None)
                    if idx is None:
                        ctx.offsets[(sid, lid)] = 0.0
                    elif reverse:
                        ctx.offsets[(sid, lid)] = (local_max - idx) * ctx.offset_step
                    else:
                        ctx.offsets[(sid, lid)] = idx * ctx.offset_step
        elif sid in y_solo:
            for lid in lines:
                ctx.offsets[(sid, lid)] = 0.0
        else:
            for lid in lines:
                p = ctx.line_priority.get(lid, 0)
                if reverse:
                    val = (ctx.max_priority - p) * ctx.offset_step
                    ctx.offsets[(sid, lid)] = val
                else:
                    ctx.offsets[(sid, lid)] = p * ctx.offset_step


class OffsetAnchorError(RuntimeError):
    """An independent section's bundle is not anchored on its own trunk.

    A section with no flat-frame neighbour (its lines reach it through a
    vertical leg, so its bundle order is not coordinated with an adjacent
    section across a flat boundary) must, after section-local re-indexing, have
    its non-port stations on the top-anchored offset slots ``0, step, ...``.
    Such a section left on its global-priority slots (e.g. one carrying only
    lines 3,4 of a 4-line bundle, sitting at offsets 6,9) draws its markers
    below the trunk and out of line with same-row siblings.
    """


def _section_present_lines(graph: MetroGraph) -> dict[str, set[str]]:
    """Map each section to the set of lines its non-port stations carry."""
    present: dict[str, set[str]] = {sec_id: set() for sec_id in graph.sections}
    for sid, station in graph.stations.items():
        sec_id = station.section_id
        if sec_id is None or station.is_port or sec_id not in present:
            continue
        present[sec_id] |= set(graph.station_lines(sid))
    return present


def _flat_frame_components(
    ctx: _OffsetCtx, present: dict[str, set[str]]
) -> dict[str, str]:
    """Group sections that must share one offset frame, returning sec_id->root.

    Two sections share a frame when a line runs flat between them: they sit in
    the same grid row, in adjacent columns, and carry a common line.  That line
    crosses the boundary on one trunk Y, so re-basing either section's bundle
    independently would slant it.  Sections joined only by a vertical leg (a
    different row, a non-adjacent column routed through a corridor) are free to
    anchor independently.
    """
    sections = ctx.graph.sections
    sec_ids = list(sections)
    parent = {sec_id: sec_id for sec_id in sec_ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(sec_ids):
        sa = sections[a]
        for b in sec_ids[i + 1 :]:
            sb = sections[b]
            if (
                sa.grid_row == sb.grid_row
                and abs(sa.grid_col - sb.grid_col) == 1
                and present[a] & present[b]
            ):
                parent[find(a)] = find(b)
    return {sec_id: find(sec_id) for sec_id in sec_ids}


def _assert_sections_anchored_on_trunk(ctx: _OffsetCtx) -> None:
    """Raise :class:`OffsetAnchorError` if an independent section is not anchored.

    Backstop on the postcondition of :func:`_reindex_section_local`: a section
    with no flat-frame neighbour must have its non-port stations on the
    contiguous top-anchored levels ``0, step, ..., (m-1)*step``.  A section that
    shares a flat frame with a neighbour is exempt -- it may legitimately sit on
    a sub-range so a line stays level across the boundary -- since re-basing
    there is left to the global frame.  Fails loudly if a future change stops
    re-anchoring an independent subset-carrying section rather than letting the
    misaligned markers reach the canvas.  Compact mode allocates slots by a
    different rule (max lines per side) and is exempt.
    """
    if ctx.compact:
        return
    roots = ctx.frame_roots
    component_size = Counter(roots.values())
    levels_by_section: dict[str, set[float]] = {}
    for (sid, _lid), off in ctx.offsets.items():
        station = ctx.graph.stations.get(sid)
        if station is None or station.is_port or station.section_id is None:
            continue
        if component_size[roots[station.section_id]] > 1:
            continue
        levels_by_section.setdefault(station.section_id, set()).add(round(off, 1))
    for sec_id, levels in levels_by_section.items():
        ordered = sorted(levels)
        expected = [round(i * ctx.offset_step, 1) for i in range(len(ordered))]
        if ordered != expected:
            raise OffsetAnchorError(
                f"independent section {sec_id!r} bundle offsets {ordered} are "
                f"not top-anchored {expected}; markers sit off the trunk"
            )


def _reindex_local_priority_gaps(ctx: _OffsetCtx) -> dict[str, dict[str, int]]:
    """Re-anchor section bundles on their trunk, returning the section-local
    orderings.

    Base offsets place each line on its global-priority slot, so a section
    carrying only a subset of the bundle inherits that subset's slots and draws
    its trunk off-centre.

    Re-basing is gated on whether a section shares a flat offset frame with a
    neighbour.  A section with a flat-frame neighbour passes a line straight
    across the boundary on one trunk Y, so its bundle order is coordinated with
    that neighbour (and its entry/exit ports) through the global frame; for it
    re-basing is limited to closing interior priority gaps.  An *independent*
    section -- one whose lines reach it through a vertical leg, not flat from an
    adjacent column -- can re-centre any subset that does not already sit on the
    top-anchored slots, so two non-interacting sections on the same row align.
    """
    graph = ctx.graph
    present = _section_present_lines(graph)
    roots = _flat_frame_components(ctx, present)
    ctx.frame_roots = roots
    component_size = Counter(roots.values())

    section_local: dict[str, dict[str, int]] = {}
    for sec_id in graph.sections:
        ordered = sorted(present[sec_id], key=lambda lid: ctx.line_priority.get(lid, 0))
        global_pris = [ctx.line_priority.get(lid, 0) for lid in ordered]
        n = len(global_pris)
        if component_size[roots[sec_id]] > 1:
            # Coordinated through the shared frame: only close interior gaps.
            needs_reindex = any(
                global_pris[i + 1] - global_pris[i] > 1 for i in range(n - 1)
            )
        else:
            # Independent: re-centre any subset off the top-anchored run.
            if sec_id in ctx.reversed_sections:
                anchored_run = range(ctx.max_priority - n + 1, ctx.max_priority + 1)
            else:
                anchored_run = range(n)
            needs_reindex = global_pris != list(anchored_run)
        if needs_reindex:
            section_local[sec_id] = {lid: i for i, lid in enumerate(ordered)}

    for sid_s, station in graph.stations.items():
        st_sec = station.section_id
        if st_sec is None or st_sec not in section_local:
            continue
        local_pri = section_local[st_sec]
        local_max = max(local_pri.values()) if local_pri else 0
        reverse = st_sec in ctx.reversed_sections
        for lid in graph.station_lines(sid_s):
            p = local_pri.get(lid, 0)
            if reverse:
                ctx.offsets[(sid_s, lid)] = (local_max - p) * ctx.offset_step
            else:
                ctx.offsets[(sid_s, lid)] = p * ctx.offset_step
    return section_local


def _section_line_feeders(ctx: _OffsetCtx, section: Section) -> dict[str, str]:
    """Map each entering line to the upstream section that feeds it."""
    graph = ctx.graph
    line_feeder: dict[str, str] = {}
    for pid in section.entry_ports:
        for edge in graph.edges_to(pid):
            src = graph.stations.get(edge.source)
            if not src:
                continue
            feeder_sec = src.section_id
            if feeder_sec is not None:
                line_feeder[edge.line_id] = feeder_sec
    return line_feeder


def _section_present_line_set(ctx: _OffsetCtx, sec_id: str) -> set[str]:
    """Lines that appear on any station of section *sec_id*."""
    present: set[str] = set()
    for sid_s, station in ctx.graph.stations.items():
        if station.section_id == sec_id:
            present |= set(ctx.graph.station_lines(sid_s))
    return present


def _section_order_offsets(
    ctx: _OffsetCtx, sec_id: str, new_order: list[str]
) -> dict[tuple[str, str], float]:
    """Per-(station, line) stored offsets that re-slot *sec_id* onto *new_order*.

    Slot 0 is the top (smallest offset); reversed sections count from the
    bottom so the same logical order draws on the same trunk side.
    """
    new_local = {lid: i for i, lid in enumerate(new_order)}
    local_max = len(new_order) - 1
    reverse = sec_id in ctx.reversed_sections
    target: dict[tuple[str, str], float] = {}
    for sid_s, station in ctx.graph.stations.items():
        if station.section_id != sec_id:
            continue
        for lid in ctx.graph.station_lines(sid_s):
            p = new_local.get(lid, 0)
            if reverse:
                target[(sid_s, lid)] = (local_max - p) * ctx.offset_step
            else:
                target[(sid_s, lid)] = p * ctx.offset_step
    return target


def _apply_section_line_order(
    ctx: _OffsetCtx, sec_id: str, new_order: list[str]
) -> None:
    """Re-slot every station in *sec_id* onto the bundle order *new_order*."""
    ctx.offsets.update(_section_order_offsets(ctx, sec_id, new_order))


def _share_flat_frame(ctx: _OffsetCtx, sec_a: str, sec_b: str) -> bool:
    """Whether two sections belong to one flat-frame component.

    Members of a frame pass their common lines straight across the boundaries
    between them on shared trunk Ys, so their bundles are coordinated rather
    than anchored independently.  Reads the components
    :func:`_reindex_local_priority_gaps` records on ``ctx.frame_roots``.
    """
    roots = ctx.frame_roots
    return sec_a in roots and roots.get(sec_a) == roots.get(sec_b)


def _section_line_offsets(ctx: _OffsetCtx, sec_id: str) -> dict[str, float]:
    """Offset of each line on section *sec_id* from a representative station
    (offsets are per-line constant within a section)."""
    section = ctx.graph.sections.get(sec_id)
    result: dict[str, float] = {}
    if section is None:
        return result
    for sid_s in section.station_ids:
        if ctx.graph.stations[sid_s].is_port:
            continue
        for lid in ctx.graph.station_lines(sid_s):
            result.setdefault(lid, ctx.offsets.get((sid_s, lid), 0.0))
    return result


def _align_reconvergence_to_feeder(
    ctx: _OffsetCtx,
    sec_id: str,
    continuing: list[str],
    returning: list[str],
    feeder: str,
) -> None:
    """Pin a section's continuing lines onto their flat-frame feeder's offsets.

    The continuing lines run level out of *feeder*, so they must keep the
    feeder's trunk Y across the boundary; stack the returning lines just past
    the band (their final side is settled by the perpendicular merge re-slot).
    """
    feeder_off = _section_line_offsets(ctx, feeder)
    if not all(lid in feeder_off for lid in continuing):
        return
    new_off = {lid: feeder_off[lid] for lid in continuing}
    band_bottom = max(new_off.values())
    for rank, lid in enumerate(returning, start=1):
        new_off[lid] = band_bottom + rank * ctx.offset_step
    for sid_s in ctx.graph.sections[sec_id].station_ids:
        for lid in ctx.graph.station_lines(sid_s):
            if lid in new_off:
                ctx.offsets[(sid_s, lid)] = new_off[lid]


def _order_reconvergence_by_feeder_row(
    ctx: _OffsetCtx, sec_id: str, line_feeder: dict[str, str]
) -> None:
    """Order a section's bundle by the grid row each line is fed from.

    When several single-line feeders converge from distinct rows, the merge is
    crossing-free only if the bundle stacks in feeder-row order (nearer row on
    the near slot); declaration order can interleave a deeper feeder between two
    shallower ones.  Scoped to TB sections (whose bundle stacks across the flow
    in row order); LR/RL merges keep the approach-side handling.  Only fires when
    the feeders span at least two rows.
    """
    if sec_id not in ctx.tb_sections:
        return
    graph = ctx.graph
    feeder_row: dict[str, int] = {}
    for lid, fid in line_feeder.items():
        section = graph.sections.get(fid)
        if section is not None:
            feeder_row[lid] = section.grid_row
    if len(set(feeder_row.values())) < 2:
        return
    sec_present = _section_present_line_set(ctx, sec_id)
    new_order = sorted(
        sec_present,
        key=lambda lid: (feeder_row.get(lid, 0), ctx.line_priority.get(lid, 0)),
    )
    if new_order == sorted(sec_present, key=lambda lid: ctx.line_priority.get(lid, 0)):
        return
    _apply_section_line_order(ctx, sec_id, new_order)


def _reorder_reconvergence(
    ctx: _OffsetCtx, section_local: dict[str, dict[str, int]]
) -> None:
    """Settle each reconvergence section's bundle on its primary feeder.

    When the primary feeder is a flat-frame neighbour the continuing lines must
    keep the feeder's offsets so the inter-section run stays level; otherwise
    they reach the section through a riser and just lead the bundle at the top.
    Single-line feeders from distinct rows order the bundle by feeder row.
    """
    graph = ctx.graph
    for sec_id, section in graph.sections.items():
        if not section.entry_ports:
            continue
        line_feeder = _section_line_feeders(ctx, section)
        if not line_feeder:
            continue

        lines_by_feeder: dict[str, list[str]] = {}
        for lid, fid in line_feeder.items():
            lines_by_feeder.setdefault(fid, []).append(lid)
        if len(lines_by_feeder) < 2:
            continue

        primary_fid = max(lines_by_feeder, key=lambda f: len(lines_by_feeder[f]))
        primary_lines = set(lines_by_feeder[primary_fid])
        if len(primary_lines) < 2:
            _order_reconvergence_by_feeder_row(ctx, sec_id, line_feeder)
            continue

        primary_order = section_local.get(primary_fid, ctx.line_priority)
        continuing = sorted(primary_lines, key=lambda lid: primary_order.get(lid, 0))

        sec_present = _section_present_line_set(ctx, sec_id)
        returning = sorted(
            sec_present - primary_lines,
            key=lambda lid: ctx.line_priority.get(lid, 0),
        )

        if _share_flat_frame(ctx, sec_id, primary_fid):
            _align_reconvergence_to_feeder(
                ctx, sec_id, continuing, returning, primary_fid
            )
            continue

        new_order = continuing + returning
        global_ordered = sorted(
            sec_present, key=lambda lid: ctx.line_priority.get(lid, 0)
        )
        if new_order == global_ordered:
            continue

        _apply_section_line_order(ctx, sec_id, new_order)


def _section_exit_fanout_junction(ctx: _OffsetCtx, section: Section) -> str | None:
    """The single fan-out junction *section* exits into, if exactly one."""
    junction_ids = ctx.graph.junction_ids
    junctions = {
        e.target
        for pid in section.exit_ports
        for e in ctx.graph.edges_from(pid)
        if e.target in junction_ids
    }
    return next(iter(junctions)) if len(junctions) == 1 else None


def _reorder_fanout_divergence(ctx: _OffsetCtx) -> None:
    """Order a section's bundle by where its lines peel off a shared exit fan.

    When distinct lines leave one section through a shared exit junction and drop
    to different columns on another row, they should descend as one bundle and
    split only where each peels into its target.  That is crossing-free only when
    the bundle's lead-in Y order matches the descent X order the fan channel
    assigns, so the source-section bundle is re-slotted into the same peel order
    (:func:`fanout_divergence_peel_order`) before the exit/junction ports inherit
    their offsets.

    Non-compact LR/RL sections only -- the divergence analog of
    :func:`_reorder_reconvergence`.
    """
    if ctx.compact:
        return
    graph = ctx.graph
    for sec_id, section in graph.sections.items():
        if section.direction not in ("LR", "RL"):
            continue
        jid = _section_exit_fanout_junction(ctx, section)
        if jid is None:
            continue
        peel_order = fanout_divergence_peel_order(graph, jid, ctx.line_priority)
        if peel_order is None:
            continue

        sec_present = _section_present_line_set(ctx, sec_id)
        rest = sorted(
            sec_present - set(peel_order),
            key=lambda lid: ctx.line_priority.get(lid, 0),
        )
        new_order = [lid for lid in peel_order if lid in sec_present] + rest
        if new_order == sorted(
            sec_present, key=lambda lid: ctx.line_priority.get(lid, 0)
        ):
            continue

        _apply_section_line_order(ctx, sec_id, new_order)


def _trunk_continuation_lines(ctx: _OffsetCtx, section: Section) -> set[str]:
    """Lines that continue straight across a fan-out hub on the section's trunk lane.

    A fan-out hub is a non-port station with in-section outgoing edges reaching
    at least two distinct lanes (the line-stacking axis: X for a vertical-flow
    section, Y for a horizontal one).  The lines whose target keeps the hub's
    lane continue straight along the trunk; the others peel off to a sibling
    lane.
    """
    graph = ctx.graph
    primary_axis = AxisFrame.axes_for_direction(section.direction)[0]
    continuation: set[str] = set()
    for sid in section.station_ids:
        hub = graph.stations.get(sid)
        if hub is None or hub.is_port:
            continue
        out_lane: dict[str, float] = {}
        for edge in graph.edges_from(sid):
            tgt = graph.stations.get(edge.target)
            if tgt is None or tgt.is_port or tgt.section_id != section.id:
                continue
            out_lane[edge.line_id] = axis_split(primary_axis, (tgt.x, tgt.y))[1]
        if len(out_lane) < 2 or len({round(v, 3) for v in out_lane.values()}) < 2:
            continue
        hub_lane = axis_split(primary_axis, (hub.x, hub.y))[1]
        for lid, lane in out_lane.items():
            if abs(lane - hub_lane) < COORD_TOLERANCE:
                continuation.add(lid)
    return continuation


def _entry_feed_survives_reslot(graph: MetroGraph, section: Section) -> bool:
    """Whether re-slotting *section*'s bundle keeps its entry feed crossing-free.

    A BOTTOM exit feeding the TOP entry from directly above is itself drawn
    TB-reversed, so it tracks a re-slotted bundle straight down.  A side feed
    that reaches the TOP entry over the top is order-preserving and would cross
    if the bundle below it were reversed, so a section reached that way is left
    on its priority slots.  A hub with no entry feed (a source) is trivially
    safe.
    """
    for pid in section.entry_ports:
        for edge in graph.edges_to(pid):
            p = graph.ports.get(edge.source)
            if p is None or p.is_entry or p.side != PortSide.BOTTOM:
                return False
    return True


def _slot_trunk_continuation_lines(ctx: _OffsetCtx) -> None:
    """Slot a fan-out's in-lane continuation onto the trunk so it runs straight.

    At a fan-out hub one line may continue straight along the trunk lane while a
    sibling peels off to another lane.  A TB section draws each line at its
    offset reversed against the per-station bundle max; that max shrinks from
    the hub (the full bundle) to the continuation's solo child (one line), so a
    continuation left on its priority slot is drawn outboard at the hub but on
    the trunk at the child and jogs by one step instead of dropping straight
    (issue #929).  Re-slot the section so the continuation rides the slot that
    draws on the trunk and the peeling siblings ride outboard.  LR/RL draw their
    lane un-reversed, so the continuation already sits on the trunk there and
    those sections are left untouched, as are sections whose entry feed would
    not survive the re-slot (:func:`_entry_feed_survives_reslot`).
    """
    if ctx.compact:
        return
    graph = ctx.graph
    right_entry = tb_right_entry_sections(graph)
    for sec_id, section in graph.sections.items():
        if sec_id not in ctx.tb_sections or graph.is_rail_section(sec_id):
            continue
        if not _entry_feed_survives_reslot(graph, section):
            continue
        continuation = _trunk_continuation_lines(ctx, section)
        present = sorted(
            _section_present_line_set(ctx, sec_id),
            key=lambda lid: ctx.line_priority.get(lid, 0),
        )
        cont = [lid for lid in present if lid in continuation]
        rest = [lid for lid in present if lid not in continuation]
        if not cont or not rest:
            continue
        # The trunk-drawing slot is the back of the order on a plain TB section;
        # the right-entry override and the section-reversal each flip which end
        # that is, so the continuation rides the back exactly when they agree.
        continuation_last = (sec_id in right_entry) == (sec_id in ctx.reversed_sections)
        new_order = rest + cont if continuation_last else cont + rest
        _apply_section_line_order(ctx, sec_id, new_order)


def _reindex_section_local(ctx: _OffsetCtx) -> None:
    """Re-index offsets per-section to close priority gaps (non-compact only).

    Lines absent from a section should not reserve offset slots within it.
    Also applies reconvergence ordering: when multiple upstream sections
    feed into one section, lines from the primary feeder keep their
    relative offsets at the top.
    """
    if ctx.compact:
        return
    section_local = _reindex_local_priority_gaps(ctx)
    _reorder_reconvergence(ctx, section_local)


def _entry_fed_in_section(graph: MetroGraph, sid: str, lid: str, sec_id: str) -> bool:
    """Whether *lid* reaches *sid* from this section's entry port.

    Walks back along *lid*'s in-section edges; returns True if the chain
    originates at an entry port.  Such a line is the section's continuing
    through-trunk and should keep its offset slot.
    """
    seen: set[str] = set()
    stack = [sid]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for e in graph.edges_to(cur):
            if e.line_id != lid:
                continue
            port = graph.ports.get(e.source)
            if port and port.is_entry:
                return True
            src = graph.stations.get(e.source)
            if src and not src.is_port and src.section_id == sec_id:
                stack.append(e.source)
    return False


def _reorder_exit_only_lines(ctx: _OffsetCtx) -> None:
    """Reorder offsets at stations where a line originates and exits to a port.

    When a line has no inbound edge at a multi-line station and its
    outbound edge leads to an exit port above (lower Y) the station,
    move that line to the top offset slot to avoid an immediate
    crossing.  Similarly, if the exit port is below, move to the
    bottom slot.

    The swap is propagated along same-Y edges within the section to
    maintain horizontal consistency.  Collisions at multi-line
    neighbours are resolved by swapping there too.

    Only applies in non-compact mode for LR/RL sections.
    """
    if ctx.compact:
        return

    graph = ctx.graph
    same_y_adj = _build_same_y_adj(graph)

    # Build (source, line_id) -> target index for O(1) lookups
    outbound_target: dict[tuple[str, str], str] = {}
    for edge in graph.edges:
        outbound_target[(edge.source, edge.line_id)] = edge.target

    for sid, station in graph.stations.items():
        if station.is_port or station.section_id is None:
            continue

        section = graph.sections.get(station.section_id)
        if not section or section.direction not in ("LR", "RL"):
            continue

        lines = graph.station_lines(sid)
        if len(lines) < 2:
            continue

        # Find lines that originate at this station (no inbound edge)
        exit_only = [lid for lid in lines if lid not in ctx.inbound.get(sid, set())]
        if not exit_only:
            continue

        for lid in exit_only:
            _reorder_one_exit_line(
                ctx,
                same_y_adj,
                outbound_target,
                station,
                station.section_id,
                sid,
                lines,
                lid,
            )


def _desired_exit_slot(
    ctx: _OffsetCtx, station: Station, target_st: Station, lines: list[str], sid: str
) -> float | None:
    """Top slot when the exit port is above, bottom when below, else None."""
    all_offs = [ctx.offsets.get((sid, ol), 0.0) for ol in lines]
    if target_st.y < station.y - _SAME_Y_TOLERANCE:
        return min(all_offs)
    if target_st.y > station.y + _SAME_Y_TOLERANCE:
        return max(all_offs)
    return None


def _reorder_one_exit_line(
    ctx: _OffsetCtx,
    same_y_adj: dict[str, dict[str, list[tuple[str, str]]]],
    outbound_target: dict[tuple[str, str], str],
    station: Station,
    sec_id: str,
    sid: str,
    lines: list[str],
    lid: str,
) -> None:
    """Move one exit-only line to its crossing-free slot, propagating the swap."""
    graph = ctx.graph
    target_id = outbound_target.get((sid, lid))
    if not target_id:
        return
    target_st = graph.stations.get(target_id)
    if not target_st:
        return

    # Only act when the target is an exit port
    target_port = graph.ports.get(target_id)
    if not target_port or target_port.is_entry:
        return

    cur_off = ctx.offsets.get((sid, lid), 0.0)
    desired_off = _desired_exit_slot(ctx, station, target_st, lines, sid)
    if desired_off is None:
        return
    if abs(cur_off - desired_off) < _OFFSET_EQ_TOLERANCE:
        return  # already in the right slot

    # Find which line currently occupies the desired slot
    swap_lid = None
    for other in lines:
        if (
            other != lid
            and abs(ctx.offsets.get((sid, other), 0.0) - desired_off)
            < _OFFSET_EQ_TOLERANCE
        ):
            swap_lid = other
            break
    if swap_lid is None:
        return

    # Don't displace the continuing through-trunk when the exit-only line
    # co-travels with it to the *same* exit port: the reorder then prevents
    # no crossing (both leave together) but steps the trunk's offset mid-run,
    # slanting its junction-to-entry segment downstream (#420).  When the two
    # diverge to different targets the swap is genuinely separating them and
    # must stand (#125).
    if outbound_target.get((sid, swap_lid)) == target_id and _entry_fed_in_section(
        graph, sid, swap_lid, sec_id
    ):
        return

    _propagate_offset_swap(
        ctx, same_y_adj, sec_id, sid, lid, swap_lid, desired_off, cur_off
    )


def _propagate_offset_swap(
    ctx: _OffsetCtx,
    same_y_adj: dict[str, dict[str, list[tuple[str, str]]]],
    sec_id: str,
    sid: str,
    lid: str,
    swap_lid: str,
    desired_off: float,
    cur_off: float,
) -> None:
    """Apply an offset swap and propagate it along same-Y edges in the section."""
    graph = ctx.graph
    pending: dict[str, dict[str, float]] = {sid: {lid: desired_off, swap_lid: cur_off}}

    visited: set[tuple[str, str]] = set()
    queue: deque[tuple[str, str, float]] = deque(
        [
            (sid, lid, desired_off),
            (sid, swap_lid, cur_off),
        ]
    )
    max_steps = len(graph.stations) * len(graph.lines)

    while queue and max_steps > 0:
        max_steps -= 1
        cur_sid, cur_lid, new_off = queue.popleft()
        if (cur_sid, cur_lid) in visited:
            continue
        visited.add((cur_sid, cur_lid))

        adj = same_y_adj.get(sec_id, {}).get(cur_sid, [])
        for nbr_sid, edge_lid in adj:
            if edge_lid != cur_lid:
                continue
            if (nbr_sid, cur_lid) in visited:
                continue

            nbr_cur = ctx.offsets.get((nbr_sid, cur_lid), 0.0)
            if abs(nbr_cur - new_off) < _OFFSET_EQ_TOLERANCE:
                continue  # already matches

            nbr_lines = graph.station_lines(nbr_sid)
            pending.setdefault(nbr_sid, {})[cur_lid] = new_off
            queue.append((nbr_sid, cur_lid, new_off))

            if len(nbr_lines) < 2:
                continue

            # Multi-line station: check for collision and swap
            for other_lid in nbr_lines:
                if other_lid == cur_lid:
                    continue
                if (
                    abs(ctx.offsets.get((nbr_sid, other_lid), 0.0) - new_off)
                    < _OFFSET_EQ_TOLERANCE
                ):
                    pending[nbr_sid][other_lid] = nbr_cur
                    queue.append((nbr_sid, other_lid, nbr_cur))
                    break

    if max_steps <= 0:
        return

    # Apply all pending changes
    for s_id, line_offsets in pending.items():
        for lid_, off in line_offsets.items():
            ctx.offsets[(s_id, lid_)] = off


def _apply_compact_section_consistency(ctx: _OffsetCtx) -> None:
    """Ensure multi-line entry ports have consistent offsets (compact only).

    All lines entering a section should maintain consistent relative
    offsets at every multi-line station, including hidden pass-throughs.
    """
    if not ctx.compact:
        return

    graph = ctx.graph
    for sec_id, section in graph.sections.items():
        sec_entry_lines: list[str] = []
        for pid in section.entry_ports:
            sec_entry_lines.extend(graph.station_lines(pid))
        seen: set[str] = set()
        unique_entry: list[str] = []
        for lid in sorted(
            set(sec_entry_lines), key=lambda x: ctx.line_priority.get(x, 0)
        ):
            if lid not in seen:
                seen.add(lid)
                unique_entry.append(lid)
        if len(unique_entry) < 2:
            continue
        sec_reverse = sec_id in ctx.reversed_sections
        sec_offs: dict[str, float] = {}
        for i, lid in enumerate(unique_entry):
            if sec_reverse:
                sec_offs[lid] = (len(unique_entry) - 1 - i) * ctx.offset_step
            else:
                sec_offs[lid] = i * ctx.offset_step
        for sid_s, station in graph.stations.items():
            if station.section_id != sec_id:
                continue
            slines = graph.station_lines(sid_s)
            present = [lid for lid in slines if lid in sec_offs]
            if len(slines) >= 2 and present:
                for lid in present:
                    ctx.offsets[(sid_s, lid)] = sec_offs[lid]
            elif station.is_hidden and len(slines) == 1 and slines[0] in sec_offs:
                ctx.offsets[(sid_s, slines[0])] = sec_offs[slines[0]]


def _propagate_exit_offsets_to_hubs(
    ctx: _OffsetCtx, port_id: str, offs: dict[str, float]
) -> None:
    """Copy a port's per-line offsets onto its upstream hub stations.

    A hub is a station feeding two or more of the port's feeders; giving it
    the port's bundle ordering keeps the in-section run consistent up to the
    fan-out point.
    """
    graph = ctx.graph
    feeder_ids = {
        edge.source
        for edge in graph.edges_to(port_id)
        if (st := graph.stations.get(edge.source)) is not None and not st.is_port
    }
    if len(feeder_ids) < 2:
        return
    hub_candidates = {edge.source for fid in feeder_ids for edge in graph.edges_to(fid)}
    for hub_id in hub_candidates:
        overlap = [lid for lid in graph.station_lines(hub_id) if lid in offs]
        if len(overlap) >= 2:
            for lid in overlap:
                ctx.offsets[(hub_id, lid)] = offs[lid]


def _compute_exit_port_offsets(ctx: _OffsetCtx) -> None:
    """Compute exit port offsets for TB and LR/RL sections.

    TB sections with LEFT/RIGHT exits: the exit-port Y order is whatever makes
    the drop -> turn concentric corner nest without pinching.  The drop
    continues the in-section column order (raw internal offset for a RIGHT-entry
    section, its reverse otherwise, mirroring :func:`_tb_x_offset`).  A RIGHT
    exit (down -> east turn) reverses the column across the corner, so its port
    order is the reverse of the column; a LEFT exit (down -> west turn) keeps
    it, so its port order equals the column.  Reversing unconditionally double-
    reverses a non-right-entry RIGHT exit and crosses the bundle at the feeder
    station.

    LR/RL sections with LEFT/RIGHT exits: use spatial Y ordering of
    feeding stations to prevent visual crossings, and propagate to
    upstream hub stations.
    """
    graph = ctx.graph
    tb_right_entry = tb_right_entry_sections(graph)

    # TB section LEFT/RIGHT exit ports
    for port_id, port_obj in graph.ports.items():
        if port_obj.is_entry or port_obj.section_id not in ctx.tb_sections:
            continue
        if port_obj.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        internal_offs: dict[str, float] = {}
        for edge in graph.edges_to(port_id):
            src_st = graph.stations.get(edge.source)
            if src_st and not src_st.is_port:
                internal_offs[edge.line_id] = ctx.offsets.get(
                    (edge.source, edge.line_id), 0.0
                )
        if internal_offs:
            max_int = max(internal_offs.values())
            right_entry = port_obj.section_id in tb_right_entry
            right_exit = port_obj.side == PortSide.RIGHT
            for lid, ioff in internal_offs.items():
                column_off = ioff if right_entry else max_int - ioff
                ctx.offsets[(port_id, lid)] = (
                    max_int - column_off if right_exit else column_off
                )

    # LR/RL section LEFT/RIGHT exit ports: spatial Y ordering
    for port_id, port_obj in graph.ports.items():
        if port_obj.is_entry or port_obj.section_id not in ctx.lr_rl_sections:
            continue
        if port_obj.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        # When a single full-bundle feeder carries every port line, side-
        # branch feeders that only contribute a subset must not pull their
        # line's "average Y" off the trunk: the kink belongs at the side
        # branch, not at the bundle's exit.
        line_feeders: dict[str, list[tuple[str, float]]] = {}
        for edge in graph.edges_to(port_id):
            src_st = graph.stations.get(edge.source)
            if src_st and not src_st.is_port:
                line_feeders.setdefault(edge.line_id, []).append(
                    (edge.source, src_st.y)
                )
        if len(line_feeders) < 2:
            continue
        port_lines = set(line_feeders.keys())

        # A section fed by a single incoming bundle that already carries every
        # exit-port line has an established order: preserve it at the exit so a
        # straight-through line keeps its slot instead of being re-sorted by
        # feeder Y.
        section = graph.sections.get(port_obj.section_id)
        entry_ports = list(section.entry_ports) if section else []
        if len(entry_ports) == 1 and port_lines.issubset(
            graph.station_lines(entry_ports[0])
        ):
            inherited = {
                lid: ctx.offsets.get((entry_ports[0], lid), 0.0) for lid in port_lines
            }
            for lid, off in inherited.items():
                ctx.offsets[(port_id, lid)] = off
            _propagate_exit_offsets_to_hubs(ctx, port_id, inherited)
            continue

        all_feeders = {fid for entries in line_feeders.values() for fid, _ in entries}
        trunk_feeder_id = next(
            (
                sid
                for sid in all_feeders
                if port_lines.issubset(graph.station_lines(sid))
            ),
            None,
        )
        if trunk_feeder_id is not None:
            trunk_y = graph.stations[trunk_feeder_id].y
            line_avg_y = {lid: trunk_y for lid in line_feeders}
        else:
            line_avg_y = {
                lid: sum(y for _, y in entries) / len(entries)
                for lid, entries in line_feeders.items()
            }
        unique_ys = set(line_avg_y.values())
        if len(unique_ys) < 2:
            if trunk_feeder_id is not None:
                # Trunk feeder anchors all lines to one Y. Inherit its
                # per-line offsets so the port keeps the trunk's bundle
                # ordering instead of falling to definition order at
                # reconcile time.
                for lid in line_feeders:
                    ctx.offsets[(port_id, lid)] = ctx.offsets.get(
                        (trunk_feeder_id, lid), 0.0
                    )
            continue
        sorted_lines = sorted(
            line_avg_y,
            key=lambda lid: (line_avg_y[lid], ctx.line_priority.get(lid, 0)),
        )
        spatial_offs = {lid: i * ctx.offset_step for i, lid in enumerate(sorted_lines)}

        # Centre offsets on the feeder closest to the port's own Y.
        # Without this, reconciliation snaps same-Y stations to the
        # port's non-zero spatial offset, pushing them off-grid.
        # Ties broken by lowest spatial offset to avoid negative shifts.
        port_y = graph.stations[port_id].y
        anchor_line = min(
            line_avg_y,
            key=lambda lid: (abs(line_avg_y[lid] - port_y), spatial_offs[lid]),
        )
        anchor_off = spatial_offs[anchor_line]
        # A section whose flow was flipped to keep this exit on its producer's
        # end (a re-oriented backward feed) carries a cross-row fan whose
        # feeders sit on non-zero base slots; re-centring the port-nearest line
        # on zero would desync the port from those feeders and leave the bundle
        # on non-adjacent slots after reconciliation.  Anchor on the feeder's
        # own offset instead so the whole bundle keeps one frame.
        if port_obj.section_id in graph._fold_reoriented_sections:
            anchor_feeders = line_feeders.get(anchor_line)
            if anchor_feeders:
                anchor_feeder_id = anchor_feeders[0][0]
                anchor_off -= ctx.offsets.get((anchor_feeder_id, anchor_line), 0.0)
        spatial_offs = {lid: off - anchor_off for lid, off in spatial_offs.items()}

        for lid, off in spatial_offs.items():
            ctx.offsets[(port_id, lid)] = off

        _propagate_exit_offsets_to_hubs(ctx, port_id, spatial_offs)


def _propagate_to_junctions(ctx: _OffsetCtx) -> None:
    """Inherit offsets from upstream exit ports to junctions.

    Junctions have section_id=None so they get default line-priority
    ordering, which may not match the exit port feeding them.
    """
    graph = ctx.graph
    for jid in graph.junctions:
        for edge in graph.edges_to(jid):
            src = graph.stations.get(edge.source)
            port_obj = graph.ports.get(edge.source)
            if src and src.is_port and port_obj and not port_obj.is_entry:
                for lid in graph.station_lines(jid):
                    port_off = ctx.offsets.get((edge.source, lid))
                    if port_off is not None:
                        ctx.offsets[(jid, lid)] = port_off
                break


def _entry_top_from_tb_bottom_exits(ctx: _OffsetCtx) -> None:
    """Match TOP entry ports to the reversed offsets of feeding TB BOTTOM exits."""
    graph = ctx.graph
    tb_right_entry: set[str] = set()
    for port_obj in graph.ports.values():
        if (
            port_obj.is_entry
            and port_obj.side == PortSide.RIGHT
            and port_obj.section_id in ctx.tb_sections
        ):
            tb_right_entry.add(port_obj.section_id)

    for port_id, port_obj in graph.ports.items():
        if not port_obj.is_entry or port_obj.side != PortSide.TOP:
            continue
        for edge in graph.edges_to(port_id):
            src = graph.stations.get(edge.source)
            if not src or not src.is_port:
                continue
            src_port = graph.ports.get(edge.source)
            if not (
                src_port
                and not src_port.is_entry
                and src_port.side == PortSide.BOTTOM
                and src.section_id in ctx.tb_sections
            ):
                continue
            exit_port_id = edge.source
            all_exit_offs = [
                ctx.offsets.get((exit_port_id, lid), 0.0)
                for lid in graph.station_lines(exit_port_id)
            ]
            max_exit_off = max(all_exit_offs) if all_exit_offs else 0.0
            if src.section_id in tb_right_entry:
                for lid in graph.station_lines(port_id):
                    ctx.offsets[(port_id, lid)] = ctx.offsets.get(
                        (exit_port_id, lid), 0.0
                    )
            else:
                for lid in graph.station_lines(port_id):
                    exit_off = ctx.offsets.get((exit_port_id, lid), 0.0)
                    ctx.offsets[(port_id, lid)] = max_exit_off - exit_off
            break


def _propagate_lr_rl_exit_to_entry(ctx: _OffsetCtx) -> None:
    """Propagate single LR/RL exit-port offsets onto fed LEFT/RIGHT entry ports."""
    graph = ctx.graph
    for port_id, port_obj in graph.ports.items():
        if not port_obj.is_entry:
            continue
        if port_obj.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        feeding_exit_ports: set[str] = set()
        for edge in graph.edges_to(port_id):
            src = graph.stations.get(edge.source)
            if not src or not src.is_port:
                continue
            src_port = graph.ports.get(edge.source)
            if src_port and not src_port.is_entry:
                feeding_exit_ports.add(edge.source)
        if len(feeding_exit_ports) != 1:
            continue
        exit_port_id = next(iter(feeding_exit_ports))
        src_port = graph.ports.get(exit_port_id)
        if not (src_port and src_port.section_id in ctx.lr_rl_sections):
            continue
        exit_lines = set(graph.station_lines(exit_port_id))
        entry_lines = set(graph.station_lines(port_id))
        if exit_lines != entry_lines:
            continue
        entry_offs: dict[str, float] = {}
        for lid in graph.station_lines(port_id):
            paired_off = ctx.offsets.get((exit_port_id, lid))
            if paired_off is not None:
                ctx.offsets[(port_id, lid)] = paired_off
                entry_offs[lid] = paired_off
        if len(entry_offs) >= 2:
            for e2 in graph.edges_from(port_id):
                tgt_st = graph.stations.get(e2.target)
                if tgt_st and not tgt_st.is_port:
                    tgt_lines = graph.station_lines(e2.target)
                    overlap = [lid for lid in tgt_lines if lid in entry_offs]
                    if len(overlap) >= 2:
                        for lid in overlap:
                            ctx.offsets[(e2.target, lid)] = entry_offs[lid]


def _compute_entry_port_offsets(ctx: _OffsetCtx) -> None:
    """Compute entry port offsets and propagate to downstream stations.

    Handles two cases:
    1. TOP entry ports fed by TB BOTTOM exits: match the reversed offset
       scheme used by inter-section routing.
    2. LEFT/RIGHT entry ports fed by a single LR/RL exit: propagate
       spatial ordering to prevent bundle crossings.
    """
    _entry_top_from_tb_bottom_exits(ctx)
    _propagate_lr_rl_exit_to_entry(ctx)


def _compact_station_gaps(ctx: _OffsetCtx) -> None:
    """Close offset gaps at stations where intermediate lines are absent.

    When a station carries two non-adjacent lines (e.g. star_salmon and
    bowtie2_salmon with hisat2 absent), the gap for the missing line is
    wasted space.  This phase detects such gaps and compacts the offsets
    so present lines use consecutive slots.

    To avoid near-diagonal edges (lines transitioning between stations
    on the same base Y with different offsets), the compaction is
    propagated along same-Y edges within the section.  The entire
    compaction is abandoned if propagation would hit a station where
    the reordering conflicts with existing offset assignments (e.g. a
    multi-line hub where swapping slots would collide).

    Only triggers when gaps are actually found; no-op otherwise.
    """
    if ctx.compact:
        return

    graph = ctx.graph
    same_y_adj = _build_same_y_adj(graph)

    # Pre-build layer index per section for same-layer checks
    sec_layer_stations: dict[str, dict[int, list[str]]] = {}
    for sid, st in graph.stations.items():
        if st.section_id and not st.is_port:
            sec_layer_stations.setdefault(st.section_id, {}).setdefault(
                st.layer, []
            ).append(sid)

    for sec_id, section in graph.sections.items():
        sec_stations = [
            sid for sid in section.station_ids if not graph.stations[sid].is_port
        ]
        if not sec_stations:
            continue

        # Prevent a later seed from re-processing stations already
        # touched by an earlier compaction in this section.
        already_compacted: set[str] = set()

        for seed_sid in sec_stations:
            if seed_sid in already_compacted:
                continue
            already_compacted |= _compact_one_seed(
                ctx, same_y_adj, sec_layer_stations, sec_id, seed_sid, len(sec_stations)
            )


def _seed_compaction(ctx: _OffsetCtx, seed_sid: str) -> dict[str, float] | None:
    """Target offsets that pack the seed's lines into consecutive slots, or None."""
    seed_lines = ctx.graph.station_lines(seed_sid)
    if len(seed_lines) < 2:
        return None

    current = {lid: ctx.offsets.get((seed_sid, lid), 0.0) for lid in seed_lines}
    sorted_by_off = sorted(current.items(), key=lambda x: x[1])
    base_off = sorted_by_off[0][1]
    expected = [base_off + i * ctx.offset_step for i in range(len(sorted_by_off))]
    if [off for _, off in sorted_by_off] == expected:
        return None

    compacted = {
        lid: base_off + i * ctx.offset_step for i, (lid, _) in enumerate(sorted_by_off)
    }
    if not any(
        abs(compacted[lid] - current[lid]) > _OFFSET_EQ_TOLERANCE for lid in seed_lines
    ):
        return None
    return compacted


def _compact_one_seed(
    ctx: _OffsetCtx,
    same_y_adj: dict[str, dict[str, list[tuple[str, str]]]],
    sec_layer_stations: dict[str, dict[int, list[str]]],
    sec_id: str,
    seed_sid: str,
    n_sec_stations: int,
) -> set[str]:
    """Compact one seed station's gaps, returning the stations it touched."""
    graph = ctx.graph
    compacted = _seed_compaction(ctx, seed_sid)
    if compacted is None:
        return set()
    changed_lids = {
        lid
        for lid in graph.station_lines(seed_sid)
        if abs(compacted[lid] - ctx.offsets.get((seed_sid, lid), 0.0))
        > _OFFSET_EQ_TOLERANCE
    }

    pending = _propagate_compaction(
        ctx,
        same_y_adj,
        sec_layer_stations,
        sec_id,
        seed_sid,
        compacted,
        changed_lids,
        n_sec_stations,
    )
    if pending is None:
        return set()

    for sid, line_offsets in pending.items():
        for lid, off in line_offsets.items():
            ctx.offsets[(sid, lid)] = off
    return set(pending)


def _compaction_peer_conflict(
    graph: MetroGraph,
    sec_layer_stations: dict[str, dict[int, list[str]]],
    sec_id: str,
    nbr_sid: str,
    lid: str,
) -> bool:
    """True if a visible same-layer peer also carries this line.

    Compaction can't guarantee consistency in that case without cascading
    into unrelated stations, so propagation must abort.
    """
    nbr_st = graph.stations[nbr_sid]
    layer_peers = sec_layer_stations.get(sec_id, {}).get(nbr_st.layer, [])
    for peer_sid in layer_peers:
        if peer_sid == nbr_sid:
            continue
        if graph.stations[peer_sid].is_hidden:
            continue
        if lid in graph.station_lines(peer_sid):
            return True
    return False


def _propagate_compaction(
    ctx: _OffsetCtx,
    same_y_adj: dict[str, dict[str, list[tuple[str, str]]]],
    sec_layer_stations: dict[str, dict[int, list[str]]],
    sec_id: str,
    seed_sid: str,
    compacted: dict[str, float],
    changed_lids: set[str],
    n_sec_stations: int,
) -> dict[str, dict[str, float]] | None:
    """BFS the compaction along same-Y edges; return updates, or None if unsafe."""
    graph = ctx.graph
    # Map: station_id -> {line_id: new_offset}
    pending: dict[str, dict[str, float]] = {seed_sid: compacted}
    visited: set[tuple[str, str]] = set()
    queue: deque[tuple[str, str]] = deque((seed_sid, lid) for lid in changed_lids)
    max_steps = n_sec_stations * len(graph.lines)

    while queue and max_steps > 0:
        max_steps -= 1
        cur_sid, lid = queue.popleft()
        if (cur_sid, lid) in visited:
            continue
        visited.add((cur_sid, lid))

        new_off = pending[cur_sid][lid]

        adj = same_y_adj.get(sec_id, {}).get(cur_sid, [])
        for nbr_sid, edge_lid in adj:
            if edge_lid != lid:
                continue
            if (nbr_sid, lid) in visited:
                continue

            # Read pending value if a prior BFS step already scheduled a
            # change, otherwise use current offset.
            nbr_cur = pending.get(nbr_sid, {}).get(
                lid, ctx.offsets.get((nbr_sid, lid), 0.0)
            )
            if abs(nbr_cur - new_off) < _OFFSET_EQ_TOLERANCE:
                continue

            if _compaction_peer_conflict(
                graph, sec_layer_stations, sec_id, nbr_sid, lid
            ):
                return None

            nbr_lines = graph.station_lines(nbr_sid)
            if len(nbr_lines) == 1:
                pending.setdefault(nbr_sid, {})[lid] = new_off
                queue.append((nbr_sid, lid))
                continue

            # Check for collision with another line's offset
            collision_lid = None
            for other_lid in nbr_lines:
                if other_lid == lid:
                    continue
                other_off = pending.get(nbr_sid, {}).get(
                    other_lid,
                    ctx.offsets.get((nbr_sid, other_lid), 0.0),
                )
                if abs(other_off - new_off) < _OFFSET_EQ_TOLERANCE:
                    collision_lid = other_lid
                    break

            nbr_pending = pending.setdefault(nbr_sid, {})
            nbr_pending[lid] = new_off
            queue.append((nbr_sid, lid))
            if collision_lid is not None:
                # Swap: move collider to the slot we're vacating
                nbr_pending[collision_lid] = nbr_cur
                queue.append((nbr_sid, collision_lid))

    if max_steps <= 0:
        return None
    return pending


def _same_section(graph: MetroGraph, id_a: str, id_b: str) -> bool:
    """Check if two stations/ports belong to the same section."""
    sa = graph.stations[id_a]
    sb = graph.stations[id_b]
    sec_a = sa.section_id
    sec_b = sb.section_id
    if sec_a and sec_b and sec_a == sec_b:
        return True
    # Junctions (section_id=None): check via port lookup
    if sec_a is None and id_a in graph.ports:
        sec_a = graph.ports[id_a].section_id
    if sec_b is None and id_b in graph.ports:
        sec_b = graph.ports[id_b].section_id
    return bool(sec_a and sec_b and sec_a == sec_b)


def _would_collide(
    ctx: _OffsetCtx, station_id: str, line_id: str, value: float
) -> bool:
    """Check if setting (station_id, line_id) to value collides with another line."""
    return any(
        ctx.offsets.get((station_id, lid), 0.0) == value
        for lid in ctx.graph.station_lines(station_id)
        if lid != line_id
    )


def _align_junction_to_entry_port(ctx: _OffsetCtx) -> None:
    """Resolve same-Y junction-to-entry-port slants left by Path 2.

    When the exit-port phase inherits its trunk feeder's bundle ordering
    (collapsed-bundle case), the junction downstream inherits the same
    ordering. If that junction then feeds a single LR/RL entry port at
    the same base Y with offsets already computed by entry-port phase,
    a small per-line offset mismatch becomes a visible diagonal between
    the junction and the entry port.

    For each junction where every outbound non-junction target is an
    entry port at the junction's own base Y, and every junction line
    maps to a single such target with a known offset, snap the junction
    offsets to the target offsets. If the swap matches the feeding
    exit port's lines exactly, mirror the change there too so the
    10-px exit-to-junction segment stays horizontal.
    """
    graph = ctx.graph
    for jid in graph.junctions:
        j_st = graph.stations[jid]
        j_lines = list(graph.station_lines(jid))
        if len(j_lines) < 2:
            continue
        # Group outbound edges by line once, then check each line has a
        # single target downstream.
        line_targets: dict[str, list[str]] = {}
        for edge in graph.edges_from(jid):
            line_targets.setdefault(edge.line_id, []).append(edge.target)
        line_to_target: dict[str, str] = {}
        ok = True
        for lid in j_lines:
            targets = line_targets.get(lid, [])
            if len(targets) != 1:
                ok = False
                break
            tgt_id = targets[0]
            tgt_st = graph.stations.get(tgt_id)
            tgt_port = graph.ports.get(tgt_id)
            if not tgt_st or not tgt_port or not tgt_port.is_entry:
                ok = False
                break
            if tgt_port.side not in (PortSide.LEFT, PortSide.RIGHT):
                ok = False
                break
            if abs(tgt_st.y - j_st.y) > _SAME_Y_TOLERANCE:
                ok = False
                break
            if (tgt_id, lid) not in ctx.offsets:
                ok = False
                break
            line_to_target[lid] = tgt_id
        if not ok or len(line_to_target) != len(j_lines):
            continue

        desired = {lid: ctx.offsets[(line_to_target[lid], lid)] for lid in j_lines}
        if len(set(desired.values())) != len(desired):
            continue
        current = {lid: ctx.offsets.get((jid, lid), 0.0) for lid in j_lines}
        if all(
            abs(desired[lid] - current[lid]) <= _OFFSET_EQ_TOLERANCE for lid in j_lines
        ):
            continue

        feeding_exit: str | None = None
        single_exit = True
        for edge in graph.edges_to(jid):
            src_port = graph.ports.get(edge.source)
            if src_port and not src_port.is_entry:
                if feeding_exit is None:
                    feeding_exit = edge.source
                elif feeding_exit != edge.source:
                    single_exit = False
                    break
            else:
                single_exit = False
                break

        for lid, off in desired.items():
            ctx.offsets[(jid, lid)] = off
        if single_exit and feeding_exit is not None:
            exit_lines = set(graph.station_lines(feeding_exit))
            if exit_lines == set(j_lines):
                exit_st = graph.stations[feeding_exit]
                if abs(exit_st.y - j_st.y) <= _SAME_Y_TOLERANCE:
                    for lid, off in desired.items():
                        ctx.offsets[(feeding_exit, lid)] = off


def _allocate_merge_ports_by_approach(ctx: _OffsetCtx) -> None:
    """Re-slot perpendicular re-joining lines at multi-feeder merge ports.

    At an LR/RL entry port fed by more than one exit port, a line that
    arrives perpendicular to the bundle (rising from a section below, or
    descending from one above) with no horizontal co-travel in the
    port's row has no upstream ordering to preserve.  Forced into its
    priority slot - especially under a section-reversal flip - it can
    land on the far side of the bundle, so its riser crosses over the
    horizontally-arriving lines.

    For each such port, leave the horizontal co-travellers on their
    incoming offsets (so their feeder edges stay flat) and move only a
    mis-slotted perpendicular line: a ``below`` line is pushed just past
    the bottom of the horizontal band (one step below its largest
    offset), an ``above`` line just past the top.  Multiple perpendicular
    lines on the same side keep their incoming relative order.  Ports
    already in approach order are unchanged.  The new per-line offsets
    propagate to every downstream station in the port's section so the
    bundle stays consistent through the section.
    """
    if ctx.compact:
        return

    graph = ctx.graph
    for port_id in graph.ports:
        classified = classify_merge_port_feeders(graph, port_id)
        if classified is None:
            continue
        horizontal, below, above = classified
        cur = {
            lid: ctx.offsets.get((port_id, lid), 0.0)
            for lid in graph.station_lines(port_id)
        }

        max_horiz = max(cur[lid] for lid in horizontal)
        min_horiz = min(cur[lid] for lid in horizontal)

        new_offs: dict[str, float] = {}
        for rank, lid in enumerate(sorted(below, key=lambda lid: cur[lid]), start=1):
            new_offs[lid] = max_horiz + rank * ctx.offset_step
        for rank, lid in enumerate(
            sorted(above, key=lambda lid: cur[lid], reverse=True), start=1
        ):
            new_offs[lid] = min_horiz - rank * ctx.offset_step

        if any(
            abs(new_offs[lid] - cur[lid]) > _OFFSET_EQ_TOLERANCE for lid in new_offs
        ):
            sec_id = graph.ports[port_id].section_id
            _apply_offsets_along_bundle(ctx, port_id, sec_id, new_offs)


def _apply_offsets_along_bundle(
    ctx: _OffsetCtx,
    port_id: str,
    sec_id: str | None,
    new_offs: dict[str, float],
) -> None:
    """Set ``new_offs`` at ``port_id`` and carry it along the bundle.

    Walks ``edges_from`` from the port, copying each moved line's new offset
    onto downstream stations.  In-section non-port stations always continue
    the bundle; ports and downstream sections continue only while the run
    stays on the merge port's row, so a line re-slotted at the merge port
    keeps that slot all the way to its consumer rather than crossing back on
    the outgoing run.  A line that turns off the row stops the walk there and
    transitions its slot at the turn.
    """
    graph = ctx.graph
    row_y = graph.stations[port_id].y
    for lid, off in new_offs.items():
        ctx.offsets[(port_id, lid)] = off

    visited = {port_id}
    queue = deque([port_id])
    while queue:
        cur = queue.popleft()
        for edge in graph.edges_from(cur):
            tgt_id = edge.target
            if tgt_id in visited:
                continue
            tgt = graph.stations[tgt_id]
            in_section = not tgt.is_port and tgt.section_id == sec_id
            on_row = abs(tgt.y - row_y) <= _SAME_Y_TOLERANCE
            if not in_section and not on_row:
                continue
            visited.add(tgt_id)
            for lid in graph.station_lines(tgt_id):
                if lid in new_offs:
                    ctx.offsets[(tgt_id, lid)] = new_offs[lid]
            queue.append(tgt_id)


def _bypass_convergence_feeders(ctx: _OffsetCtx, port_id: str) -> dict[str, int] | None:
    """Source columns of a LEFT entry port's bypass-convergence bundle.

    Returns ``{line_id: source_grid_col}`` when every line feeding *port_id*
    arrives on a bypass (a multi-column hop past intervening sections) and the
    feeders span two or more source columns - the static signature of several
    lines riding one shared bypass trunk into a common port.  Returns ``None``
    for any port that is not such a convergence.
    """
    graph = ctx.graph
    tgt_col = _resolve_section_col(graph, graph.stations[port_id])
    if tgt_col is None:
        return None
    line_col: dict[str, int] = {}
    for edge in graph.edges_to(port_id):
        src = graph.stations.get(edge.source)
        if src is None:
            return None
        col, row = _resolve_section_colrow(graph, src)
        if (
            col is None
            or abs(tgt_col - col) <= 1
            or not _has_intervening_sections(graph, col, tgt_col, row)
        ):
            return None
        line_col[edge.line_id] = col
    if len(set(line_col.values())) < 2:
        return None
    return line_col


def _order_convergence_entry_ports(ctx: _OffsetCtx) -> None:
    """Slot a LEFT entry port's bypass-convergence bundle by approach order.

    Lines from two or more source columns ride one bypass trunk into a shared
    LEFT entry port.  Their crossing-free slot order is by approach depth - the
    nearer source (higher grid column) on the shallow, port-near slot - not the
    declaration order the base offsets give.  Assign each line the offset its
    approach rank earns and carry it along the consumer section so the bundle
    stays in that order from the port to its first station.  The matching peel
    order on the risers is set by ``_convergence_line_order`` at routing time.
    """
    if ctx.compact:
        return
    graph = ctx.graph
    for port_id, port in graph.ports.items():
        if not (port.is_entry and port.side is PortSide.LEFT):
            continue
        sec = graph.sections.get(port.section_id)
        if (
            sec is None
            or sec.direction != "LR"
            or port.section_id in ctx.reversed_sections
        ):
            continue
        line_col = _bypass_convergence_feeders(ctx, port_id)
        if line_col is None:
            continue
        ordered = sorted(
            line_col, key=lambda lid: (-line_col[lid], ctx.line_priority.get(lid, 0))
        )
        new_offs = {lid: rank * ctx.offset_step for rank, lid in enumerate(ordered)}
        cur = {lid: ctx.offsets.get((port_id, lid), 0.0) for lid in ordered}
        if any(
            abs(new_offs[lid] - cur[lid]) > _OFFSET_EQ_TOLERANCE for lid in new_offs
        ):
            _apply_offsets_along_bundle(ctx, port_id, port.section_id, new_offs)


def _recenter_partial_fan_branches(ctx: _OffsetCtx) -> None:
    """Collapse reserved absent-line slots at independent fan branches.

    :func:`_apply_compact_section_consistency` gives every multi-line
    station the section-wide slot map so straight through-lines keep
    aligned slots.  An independent fan branch (its lines enter from a
    fan-out and leave to a fan-in, with no straight horizontal
    through-track to a same-Y neighbour) thereby reserves an empty slot
    for any bundle line it does not carry, parking its marker off-centre
    with a visible gap.

    Remap such a station's distinct offset levels onto consecutive slots
    anchored at its top line.  This removes interior gaps while
    preserving line order and any coincident lines, and cannot bend a
    shared track since the branch has none (compact mode only).
    """
    if not ctx.compact:
        return

    for violation in check_partial_branch_offset_gaps(
        ctx.graph, ctx.offsets, offset_step=ctx.offset_step
    ):
        levels = distinct_offset_levels(off for _, off in violation.offsets)
        base = levels[0]
        for lid, cur in violation.offsets:
            idx = next(
                i
                for i, lvl in enumerate(levels)
                if abs(lvl - cur) <= COORD_TOLERANCE_FINE
            )
            ctx.offsets[(violation.station_id, lid)] = base + idx * ctx.offset_step


def _reconcile_horizontal_offsets(ctx: _OffsetCtx, max_iterations: int = 10) -> None:
    """Snap offsets for same-section edges where endpoints share base Y.

    Only processes edges where both endpoints belong to the same
    section. Inter-section offset mismatches are handled by routing
    (L-shaped paths with vertical segments), so they must not be
    reconciled here - doing so cascades offsets across section
    boundaries and breaks per-section reindexing.

    For each qualifying edge, tries snapping both stations to the
    larger-magnitude offset first, then the smaller. A candidate is
    rejected if it would collide with another line at the same
    station. If neither simple snap works, shifts the entire bundle
    at the station with fewer lines (preserving relative spacing).

    Iterates until stable, since fixing one edge can propagate
    through port -> station chains within the same section.
    """
    # Pre-filter to edges where both endpoints share the same Y and
    # section. These properties are immutable during reconciliation.
    candidates = [
        edge
        for edge in ctx.graph.edges
        if abs(ctx.graph.stations[edge.source].y - ctx.graph.stations[edge.target].y)
        <= _SAME_Y_TOLERANCE
        and _same_section(ctx.graph, edge.source, edge.target)
    ]

    for _ in range(max_iterations):
        changed = False
        for edge in candidates:
            lid = edge.line_id
            src_off = ctx.offsets.get((edge.source, lid), 0.0)
            tgt_off = ctx.offsets.get((edge.target, lid), 0.0)
            if src_off == tgt_off:
                continue

            larger = src_off if abs(src_off) >= abs(tgt_off) else tgt_off
            smaller = tgt_off if larger == src_off else src_off

            applied = False
            for candidate in (larger, smaller):
                src_ok = src_off == candidate or not _would_collide(
                    ctx, edge.source, lid, candidate
                )
                tgt_ok = tgt_off == candidate or not _would_collide(
                    ctx, edge.target, lid, candidate
                )
                if src_ok and tgt_ok:
                    ctx.offsets[(edge.source, lid)] = candidate
                    ctx.offsets[(edge.target, lid)] = candidate
                    applied = True
                    changed = True
                    break

            if not applied:
                # Both candidates collide; shift the bundle at the
                # station with fewer lines (least disruption).
                src_n = len(ctx.graph.station_lines(edge.source))
                tgt_n = len(ctx.graph.station_lines(edge.target))
                if src_n <= tgt_n:
                    move_sid, target_val = edge.source, tgt_off
                else:
                    move_sid, target_val = edge.target, src_off
                cur = ctx.offsets.get((move_sid, lid), 0.0)
                delta = target_val - cur
                for other_lid in ctx.graph.station_lines(move_sid):
                    old = ctx.offsets.get((move_sid, other_lid), 0.0)
                    ctx.offsets[(move_sid, other_lid)] = old + delta
                changed = True

        if not changed:
            break


def compute_station_offsets(
    graph: MetroGraph,
    offset_step: float = OFFSET_STEP,
) -> dict[tuple[str, str], float]:
    """Compute per-station Y offsets for each line.

    Each line gets a globally consistent offset based on its declaration
    order (priority). This ensures lines maintain their position within
    bundles across all sections - when a line splits off and later
    rejoins, it returns to its reserved slot rather than shifting.

    Runs in nine phases:

    1. **Base offsets** - global priority (or compact-mode) assignment.
    2. **Section-local re-indexing** - closes priority gaps within
       sections and applies reconvergence ordering (non-compact only).
    2b. **Exit-only line reordering** - at multi-line stations where a
       line originates (no inbound edge) and exits to a port above,
       swap it to the top offset slot to avoid immediate crossings
       (non-compact LR/RL sections only).
    2c. **Trunk-continuation slotting** - at a TB fan-out hub, re-slot
       the in-lane continuation onto the trunk-drawing offset so it
       drops straight while siblings peel off (non-compact TB sections
       fed by a straight drop from above).
    3. **Compact section consistency** - ensures entry lines have
       consistent offsets across multi-line stations (compact only).
    4. **Station gap compaction** - closes per-station offset gaps
       where intermediate lines are absent, propagating along same-Y
       edges with conservative safety checks (non-compact only).
    5. **Exit port offsets** - TB reversed offsets and LR/RL spatial
       Y ordering with hub propagation.
    6. **Junction inheritance** - copies exit port offsets to junctions.
    7. **Entry port offsets** - TOP entry override for TB BOTTOM exits,
       LR/RL exit-to-entry propagation, compact entry separation.
    7b. **Merge-port approach-side allocation** - at multi-feeder LR/RL
       entry ports, re-slots a perpendicular re-joining line to the
       bundle slot nearest its approach side (non-compact only).
    7c. **Convergence entry-port ordering** - at a LEFT entry port fed by
       a bypass trunk from two or more source columns, slots the bundle
       by approach depth (nearer source on the port-near slot) so its
       risers turn in concentrically (non-compact only).
    8. **Horizontal reconciliation** - snaps mismatched offsets on
       same-Y edges to eliminate almost-horizontal slopes.
    9. **Partial fan-branch re-centring** - collapses reserved
       absent-line slots at independent fan branches so a partial-line
       station's marker has no interior gap (compact only).

    Returns dict mapping (station_id, line_id) -> y_offset.
    """
    # Rail mode bakes absolute rail Ys into the route points and the pill
    # span, so per-line offsets are not used; return an empty map.
    if graph.line_spread is LineSpread.RAILS:
        return {}

    ctx = _build_offset_ctx(graph, offset_step)
    _compute_base_offsets(ctx)
    _reindex_section_local(ctx)
    _assert_sections_anchored_on_trunk(ctx)
    _reorder_exit_only_lines(ctx)
    _reorder_fanout_divergence(ctx)
    _slot_trunk_continuation_lines(ctx)
    _apply_compact_section_consistency(ctx)
    _compact_station_gaps(ctx)
    _compute_exit_port_offsets(ctx)
    _propagate_to_junctions(ctx)
    _compute_entry_port_offsets(ctx)
    _align_junction_to_entry_port(ctx)
    _allocate_merge_ports_by_approach(ctx)
    _order_convergence_entry_ports(ctx)
    _reconcile_horizontal_offsets(ctx)
    _recenter_partial_fan_branches(ctx)
    _reverse_tb_right_entry_offsets(ctx)
    return ctx.offsets


def _is_over_top_right_entry(
    graph: MetroGraph, port: Port, tb_sections: set[str]
) -> bool:
    """Whether *port* is a RIGHT entry reached by an over-the-top loop.

    Matches the dispatch of ``_route_right_entry_over_top``: a RIGHT entry on a
    TB section fed by an exit port in the SAME grid row, an ADJACENT column, and
    to the port's LEFT.  That feed loops over the section's top and approaches
    from the right -- a U-turn that transposes the bundle.  A right entry fed
    from the right (a fold) or across columns (a bypass) keeps its order and is
    excluded.
    """
    if not (port.is_entry and port.side == PortSide.RIGHT):
        return False
    if port.section_id not in tb_sections:
        return False
    psec = graph.sections.get(port.section_id)
    pst = graph.stations.get(port.id)
    if psec is None or pst is None:
        return False
    for edge in graph.edges_to(port.id):
        src = graph.stations.get(edge.source)
        src_port = graph.ports.get(edge.source)
        if not (src and src_port and not src_port.is_entry):
            continue
        ssec = graph.sections.get(src.section_id) if src.section_id else None
        if ssec is None:
            continue
        if (
            ssec.grid_row == psec.grid_row
            and abs(ssec.grid_col - psec.grid_col) <= 1
            and src.x < pst.x - COORD_TOLERANCE
        ):
            return True
    return False


def _reverse_tb_right_entry_offsets(ctx: _OffsetCtx) -> None:
    """Reverse the line order of TB sections entered through a RIGHT port.

    A left source reaches a RIGHT-side entry by looping over the section's top
    and approaching from the right -- a U-turn, which transposes the bundle
    end-to-end (see ``_route_right_entry_over_top``).  The section therefore
    receives its lines in the opposite order to the source, so its entry port,
    internal trunk, and bottom exit must all carry the reversed order for the
    descent into the port and the drop out of it to stay straight.  Sections
    downstream of that exit inherit the reversal so their feed stays aligned.

    Reversal is :func:`reversed_offset` applied per station, an involution, so
    two stations with equal offsets map to equal offsets -- the propagated
    exit/entry equalities are preserved.
    """
    graph = ctx.graph
    roots = {
        port.section_id
        for port in graph.ports.values()
        if _is_over_top_right_entry(graph, port, ctx.tb_sections)
    }
    if not roots:
        return

    affected = set(roots)
    dag = graph.section_dag
    if dag is not None:
        stack = list(roots)
        while stack:
            for succ in dag.successors.get(stack.pop(), ()):
                if succ not in affected:
                    affected.add(succ)
                    stack.append(succ)

    for sid, station in graph.stations.items():
        if station.section_id not in affected:
            continue
        lines = graph.station_lines(sid)
        offs = [ctx.offsets.get((sid, lid), 0.0) for lid in lines]
        if not offs:
            continue
        max_off = max(offs)
        for lid in lines:
            ctx.offsets[(sid, lid)] = reversed_offset(
                ctx.offsets.get((sid, lid), 0.0), max_off
            )
