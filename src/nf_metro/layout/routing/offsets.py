"""Station offset computation for per-line Y positioning within bundles."""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import (
    Callable,
    Container,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field
from typing import Literal, NamedTuple

from nf_metro.layout.constants import (
    COORD_TOLERANCE_FINE,
    OFFSET_STEP,
    SAME_Y_TOLERANCE,
    graph_offset_step,
)
from nf_metro.layout.geometry import (
    AxisFrame,
    flow_port_sides,
    lanes_run_along_x,
    lanes_run_along_y,
    perpendicular_port_sides,
    station_lane_coord,
)
from nf_metro.layout.phases._common import (
    iter_corridor_fed_solo_entries,
    iter_flat_seam_solo_entries,
    line_forks_within_section,
    seam_is_flat,
)
from nf_metro.layout.route_topology import divergence_junction_exit_ports
from nf_metro.layout.routing.arranger import BoundaryConfig, lane_order
from nf_metro.layout.routing.common import (
    bypass_bottom_y,
    merge_junction_ids,
    needs_perp_approach_fan,
    perp_entry_consumer,
    tb_right_entry_sections,
    vertical_flow_sections,
)
from nf_metro.layout.routing.context import (
    _has_intervening_sections,
    _resolve_section_colrow,
    _section_lane_frame,
    fanout_divergence_peel_order,
    is_near_vertical_junction_right_entry,
    partial_flat_continuation_lines,
)
from nf_metro.layout.routing.corners import reversed_offset
from nf_metro.layout.routing.invariants import (
    check_partial_branch_offset_gaps,
    classify_merge_port_feeders,
    distinct_offset_levels,
    fan_port_and_station,
    max_interior_offset_gap,
)
from nf_metro.layout.routing.reversal import detect_reversed_sections
from nf_metro.layout.routing.seam import SeamOrientation, seam_orientation
from nf_metro.parser.model import (
    Edge,
    LineSpread,
    MetroGraph,
    Port,
    PortSide,
    Section,
    Station,
)
from nf_metro.parser.provenance import DecisionReason
from nf_metro.parser.route_topology import (
    RouteTopologyQuery,
    build_route_topology_query,
)

# Tolerances used across offset phases
_SAME_Y_TOLERANCE: float = SAME_Y_TOLERANCE
_OFFSET_EQ_TOLERANCE: float = 0.001


@dataclass
class _OffsetCtx:
    """Shared state threaded through offset computation phases."""

    graph: MetroGraph
    topology: RouteTopologyQuery | None = None
    divergence_exit_ports: dict[str, str] = field(default_factory=dict)
    bundle_re_slots_whole: dict[tuple[str, str], bool] = field(default_factory=dict)
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
    station_rank: dict[str, int] = field(default_factory=dict)
    # Section -> flat-frame component root, populated by section-local re-indexing
    frame_roots: dict[str, str] = field(default_factory=dict)
    fan_owned_offsets: dict[tuple[str, str], float] = field(default_factory=dict)


@dataclass(frozen=True)
class _LinearEntryFrame:
    """A section bundle whose entry cohort owns its continuing lane slots."""

    section_id: str
    entry_port_id: str
    feeder_section_id: str
    feeder_station_id: str
    continuing: tuple[tuple[str, float], ...]
    assignments: tuple[tuple[str, float], ...]
    carrier_ids: tuple[str, ...]


class LaneFrameInvariantError(RuntimeError):
    """A materialized linear entry frame violated its ownership contract."""


@dataclass(frozen=True)
class LinearEntryFrameAssignment:
    """One station-lane assignment owned by a materialized entry frame."""

    section_id: str
    station_id: str
    line_id: str
    offset: float


@dataclass(frozen=True)
class LinearEntryFrameOwnership:
    """Frozen station-lane assignments owned by materialized entry frames."""

    assignments: tuple[LinearEntryFrameAssignment, ...]


def _build_offset_ctx(graph: MetroGraph, offset_step: float) -> _OffsetCtx:
    """Build shared context for offset computation phases."""
    topology = build_route_topology_query(graph)
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
    tb_sections = vertical_flow_sections(graph)
    lr_rl_sections = {
        sid for sid, s in graph.sections.items() if s.direction in ("LR", "RL")
    }
    fan_owned_offsets = {
        (carrier.station_id, assignment.line_id): assignment.slot * offset_step
        for plan in graph.fan_plans
        if plan.owns_geometry
        for carrier in plan.offset_carriers
        for assignment in carrier.assignments
    }

    divergence_exit_ports = divergence_junction_exit_ports(graph, topology)
    bundle_re_slots_whole = {
        (junction_id, exit_port_id): _junction_bundle_re_slots_whole(
            graph, junction_id, exit_port_id
        )
        for junction_id, exit_port_id in divergence_exit_ports.items()
    }

    return _OffsetCtx(
        graph=graph,
        topology=topology,
        divergence_exit_ports=divergence_exit_ports,
        bundle_re_slots_whole=bundle_re_slots_whole,
        line_priority=line_priority,
        max_priority=max_priority,
        offset_step=offset_step,
        compact=compact,
        reversed_sections=reversed_sections,
        tb_sections=tb_sections,
        lr_rl_sections=lr_rl_sections,
        inbound=inbound,
        outbound=outbound,
        station_rank={sid: rank for rank, sid in enumerate(graph.stations)},
        fan_owned_offsets=fan_owned_offsets,
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
        src, tgt = graph.edge_endpoints(edge)
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


def _build_sec_layer_stations(graph: MetroGraph) -> dict[str, dict[int, list[str]]]:
    """Map each section to its real (non-port) stations, grouped by layer.

    Used by the compaction passes' same-layer peer-conflict check
    (:func:`_compaction_peer_conflict`).
    """
    sec_layer_stations: dict[str, dict[int, list[str]]] = {}
    for sid, st in graph.stations.items():
        if st.section_id and not st.is_port:
            sec_layer_stations.setdefault(st.section_id, {}).setdefault(
                st.layer, []
            ).append(sid)
    return sec_layer_stations


def _stores_reflected(ctx: _OffsetCtx, sec_id: str | None) -> bool:
    """Whether *sec_id* stores its per-line offsets reflected against the max.

    A reverse-flow horizontal section stores the reflection ``(max - slot)`` so
    its bundle draws on the far side of the trunk for the reversed flow.  A
    vertical-flow (TB) section instead stores its arrival order positively and
    draws the rotation ``x - offset`` (:func:`context._tb_x_offset`); there the
    side is carried by the draw sign, not by reflecting the stored slot, so the
    marker span and the drawn lines agree by construction.

    This horizontal reflection is a storage convention threaded through every
    base-offset assignment, and it flips the draw *side*, not just the bundle
    order.  The seam-classifier arrival-order path (:func:`_reorder_reconvergence`)
    transposes order alone, so it cannot express this side flip; carrying the
    reverse-flow side without reflected storage needs a per-section lane sign (the
    horizontal analogue of TB's :func:`context._tb_x_offset`).
    """
    return sec_id in ctx.reversed_sections and sec_id not in ctx.tb_sections


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
        reverse = _stores_reflected(ctx, station.section_id)

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
    for sec_a, sec_b in _junction_flat_pairs(ctx):
        parent[find(sec_a)] = find(sec_b)
    return {sec_id: find(sec_id) for sec_id in sec_ids}


def _junction_flat_pairs(ctx: _OffsetCtx) -> Iterator[tuple[str, str]]:
    """Yield section pairs a junction joins on one line along its own trunk Y.

    A junction draws one trunk at one Y, so every section it reaches on that Y --
    the one whose exit port feeds it as much as the ones whose entry ports it
    feeds -- reads the line off a single lane the junction cannot hold in two
    places at once.  That is the same flat frame an adjacent column shares,
    reached over a bypass rather than across a boundary.
    """
    graph = ctx.graph
    for jid in graph.junctions:
        junction = graph.stations[jid]
        joined: dict[str, list[str]] = {}
        for edge in (*graph.edges_from(jid), *graph.edges_to(jid)):
            other_id = edge.target if edge.source == jid else edge.source
            port = graph.ports.get(other_id)
            other = graph.stations.get(other_id)
            if port is None or other is None or port.section_id is None:
                continue
            if abs(other.y - junction.y) > _SAME_Y_TOLERANCE:
                continue
            joined.setdefault(edge.line_id, []).append(port.section_id)
        for sec_ids in joined.values():
            for other_sec in sec_ids[1:]:
                yield sec_ids[0], other_sec


def _assert_sections_anchored_on_trunk(ctx: _OffsetCtx) -> None:
    """Raise :class:`OffsetAnchorError` if a section's bundle is off its trunk.

    Backstop on the postcondition of :func:`_reindex_section_local`: every
    section's non-port stations sit on consecutive levels ``step`` apart, and a
    section with no flat-frame neighbour sits on the top-anchored ones,
    ``0, step, ..., (m-1)*step``.  A flat-frame member may instead hold a block
    lower down so a line stays level across a boundary, but it holds one block:
    an unclaimed level inside the bundle spreads its stations past the routes
    that join them.  Fails loudly if a future change stops re-basing a
    subset-carrying section, rather than letting the misaligned markers reach the
    canvas.  Compact mode allocates slots by a different rule (max lines per
    side) and is exempt.
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
        levels_by_section.setdefault(station.section_id, set()).add(round(off, 1))
    for sec_id, levels in levels_by_section.items():
        ordered = sorted(levels)
        if component_size[roots[sec_id]] > 1:
            anchor, wording = ordered[0], "one block from"
        else:
            anchor, wording = 0.0, "top-anchored at"
        expected = [round(anchor + i * ctx.offset_step, 1) for i in range(len(ordered))]
        if ordered != expected:
            raise OffsetAnchorError(
                f"section {sec_id!r} bundle offsets {ordered} are not "
                f"{wording} {expected}; markers sit off the trunk"
            )


def _base_rank(ctx: _OffsetCtx, lid: str, reverse: bool) -> int:
    """*lid*'s rank on its global-priority slot, from the bottom if reversed."""
    pri = ctx.line_priority.get(lid, 0)
    return ctx.max_priority - pri if reverse else pri


def _predicted_local_offset(
    ctx: _OffsetCtx, sec_id: str, lid: str, section_local: dict[str, dict[str, int]]
) -> float:
    """Offset *lid* will take in *sec_id* given the current re-index decisions.

    Re-indexed sections draw from their section-local order; the rest keep their
    base (global-priority) offset.  Reversed sections count from the bottom.
    """
    reverse = _stores_reflected(ctx, sec_id)
    if sec_id in section_local:
        local = section_local[sec_id]
        slot = local.get(lid, 0)
        local_max = max(local.values()) if local else 0
        rank = local_max - slot if reverse else slot
    else:
        rank = _base_rank(ctx, lid, reverse)
    return rank * ctx.offset_step


def _trunk_endpoint_offset(
    ctx: _OffsetCtx,
    node_id: str,
    lid: str,
    section_local: dict[str, dict[str, int]],
) -> float | None:
    """Offset *lid* settles to at the section anchoring *node_id*'s trunk.

    A section-bound node (station or section port) returns its own section's
    predicted offset.  A bypass-trunk junction (no section) carries the offset of
    whatever section feeds it, so it is followed back along *lid* to the nearest
    section.  Returns ``None`` if the chain leaves no section to anchor on.
    """
    graph = ctx.graph
    seen: set[str] = set()
    cur: str | None = node_id
    while cur is not None and cur not in seen:
        seen.add(cur)
        section_id = graph.stations[cur].section_id
        if section_id is not None:
            return _predicted_local_offset(ctx, section_id, lid, section_local)
        cur = next(
            (e.source for e in graph.edges_to(cur) if e.line_id == lid),
            None,
        )
    return None


class _BoundaryRun(NamedTuple):
    """One line crossing one of a section's ports, under a candidate slotting."""

    offset: float
    """Where the candidate puts the line on this section's trunk."""
    base: float
    """Where the line sits with no re-base at all: its global-priority slot."""
    neighbour: float
    """Where the line settles on the trunk waiting on the far side of the port."""

    @property
    def level(self) -> bool:
        """Whether the run draws level rather than stepping across the port."""
        return abs(self.offset - self.neighbour) <= _SAME_Y_TOLERANCE

    @property
    def level_unbased(self) -> bool:
        """Whether the run draws level with the section left un-re-based."""
        return abs(self.base - self.neighbour) <= _SAME_Y_TOLERANCE


def _boundary_crossings(
    ctx: _OffsetCtx,
    sec_id: str,
    section_local: dict[str, dict[str, int]],
) -> list[tuple[str, float, float]]:
    """``(line_id, base, neighbour)`` for each line crossing one of *sec_id*'s ports.

    Edges into the section's own interior are skipped: they carry the bundle as a
    whole, so moving the bundle shifts both of their ends together.  These facts
    hold for any candidate slotting of *sec_id*, so :func:`_boundary_runs` builds
    its per-candidate ranks from this list rather than re-deriving them.
    """
    graph = ctx.graph
    section = graph.sections[sec_id]
    reverse = _stores_reflected(ctx, sec_id)
    crossings: list[tuple[str, float, float]] = []
    for pid in (*section.entry_ports, *section.exit_ports):
        for edge in (*graph.edges_to(pid), *graph.edges_from(pid)):
            lid = edge.line_id
            other_id = edge.target if edge.source == pid else edge.source
            other = graph.stations.get(other_id)
            if other is None or other.section_id == sec_id:
                continue
            neighbour = _trunk_endpoint_offset(ctx, other_id, lid, section_local)
            if neighbour is None:
                continue
            base_rank = _base_rank(ctx, lid, reverse)
            crossings.append((lid, base_rank * ctx.offset_step, neighbour))
    return crossings


def _boundary_runs(
    ctx: _OffsetCtx,
    sec_id: str,
    candidate: dict[str, int],
    section_local: dict[str, dict[str, int]],
    crossings: Sequence[tuple[str, float, float]] | None = None,
) -> Iterator[_BoundaryRun]:
    """Yield one :class:`_BoundaryRun` per line crossing one of *sec_id*'s ports."""
    reverse = _stores_reflected(ctx, sec_id)
    local_max = max(candidate.values(), default=0)
    if crossings is None:
        crossings = _boundary_crossings(ctx, sec_id, section_local)
    for lid, base, neighbour in crossings:
        slot = candidate.get(lid)
        if slot is None:
            continue
        rank = local_max - slot if reverse else slot
        yield _BoundaryRun(rank * ctx.offset_step, base, neighbour)


def _reanchor_keeps_runs_level(
    ctx: _OffsetCtx,
    sec_id: str,
    candidate: dict[str, int],
    section_local: dict[str, dict[str, int]],
) -> bool:
    """Whether re-anchoring *sec_id* onto *candidate* leaves level runs level.

    Every edge crossing the section's ports either runs level without a re-base
    (its line's base offset matches the connected trunk) or steps (the offsets
    differ, so routing bridges it).  Re-anchoring is rejected only when it would
    pull a level run off level -- the case that paints a straight-through line as
    a kink or an almost-horizontal slope.  A run that already steps stays free,
    which is why a member fed only through a bypass that re-based upstream may
    re-anchor.
    """
    return all(
        run.level or not run.level_unbased
        for run in _boundary_runs(ctx, sec_id, candidate, section_local)
    )


def _level_run_lane_block(
    ctx: _OffsetCtx,
    sec_id: str,
    ordered: Sequence[str],
    section_local: dict[str, dict[str, int]],
) -> dict[str, int]:
    """Section-local slots for *sec_id* on the lanes its neighbours meet it on.

    Its lines take consecutive lanes whatever happens: a lane left unclaimed
    inside the bundle spreads the section's stations wider than the routes
    joining them, stranding those routes off the markers they meet.  Which lanes
    that block occupies is free, so it slides down the trunk to the first
    position where every line crossing the section's ports meets the trunk on the
    far side on its own lane -- a line met a lane out arrives on a slant instead.
    Where no position does, the block stays at the top of the trunk, which is
    what puts two unrelated bundles on one row at the same height; settling for
    a position that fixes some runs by tilting others just moves the slant.

    A reflected section ranks its lanes from its own last slot rather than from
    the trunk, so its block has only the one position to take.
    """
    top_anchored = {lid: i for i, lid in enumerate(ordered)}
    crossings = _boundary_crossings(ctx, sec_id, section_local)

    for shift in range(max(ctx.max_priority - len(ordered) + 1, 0) + 1):
        candidate = {lid: slot + shift for lid, slot in top_anchored.items()}
        runs = _boundary_runs(ctx, sec_id, candidate, section_local, crossings)
        if all(run.level for run in runs):
            return candidate
    return top_anchored


def _reindex_local_priority_gaps(ctx: _OffsetCtx) -> dict[str, dict[str, int]]:
    """Re-anchor section bundles on their trunk, returning the section-local
    orderings.

    Base offsets place each line on its global-priority slot, so a section
    carrying only a subset of the bundle inherits that subset's slots and draws
    its trunk off-centre.

    Re-basing is gated on whether a section shares a flat offset frame with a
    neighbour.  An *independent* section -- one whose lines reach it through a
    vertical leg, not flat from an adjacent column -- re-centres any subset off
    the top-anchored slots, so two non-interacting sections on the same row
    align.  A section in a multi-member frame closes interior priority gaps
    unconditionally; a frame member sitting below its trunk with no interior gap
    re-anchors only when a second pass confirms it carries no line flat across an
    adjacent-neighbour boundary (such a line would slope if its slot moved).
    """
    graph = ctx.graph
    present = _section_present_lines(graph)
    roots = _flat_frame_components(ctx, present)
    ctx.frame_roots = roots
    component_size = Counter(roots.values())

    section_local: dict[str, dict[str, int]] = {}
    ordered_by_section: dict[str, list[str]] = {}
    not_anchored_frame: list[str] = []
    closed_up: list[str] = []
    for sec_id in graph.sections:
        ordered = sorted(present[sec_id], key=lambda lid: ctx.line_priority.get(lid, 0))
        ordered_by_section[sec_id] = ordered
        global_pris = [ctx.line_priority.get(lid, 0) for lid in ordered]
        n = len(global_pris)
        if _stores_reflected(ctx, sec_id):
            anchored_run = list(range(ctx.max_priority - n + 1, ctx.max_priority + 1))
        else:
            anchored_run = list(range(n))
        not_anchored = global_pris != anchored_run
        if component_size[roots[sec_id]] > 1:
            # Coordinated through the shared frame: an interior gap closes here;
            # a below-trunk bundle with no interior gap is deferred to the second
            # pass, which re-anchors it only when no flat boundary run slants.
            interior_gap = any(
                global_pris[i + 1] - global_pris[i] > 1 for i in range(n - 1)
            )
            if interior_gap:
                section_local[sec_id] = {lid: i for i, lid in enumerate(ordered)}
                closed_up.append(sec_id)
            elif not_anchored:
                not_anchored_frame.append(sec_id)
        elif not_anchored:
            # Independent: re-centre any subset off the top-anchored run.
            section_local[sec_id] = {lid: i for i, lid in enumerate(ordered)}

    # Every closed-up bundle is published top-anchored above before any of them
    # picks its lanes, so a section's neighbours read the chosen lanes when
    # they look this section up.
    for sec_id in closed_up:
        section_local[sec_id] = _level_run_lane_block(
            ctx, sec_id, ordered_by_section[sec_id], section_local
        )

    # Second pass: a frame member sitting below its trunk re-anchors to the top
    # only when doing so keeps every flat run to an adjacent frame neighbour
    # level.  A member fed solely through risers or bypass junctions has no such
    # run and re-bases like its independently-anchored siblings; one carrying a
    # line straight across a flat boundary keeps its slot so the line stays level.
    for sec_id in not_anchored_frame:
        candidate = {lid: i for i, lid in enumerate(ordered_by_section[sec_id])}
        if _reanchor_keeps_runs_level(ctx, sec_id, candidate, section_local):
            section_local[sec_id] = candidate

    for sid_s, station in graph.stations.items():
        st_sec = station.section_id
        if st_sec is None or st_sec not in section_local:
            continue
        local_pri = section_local[st_sec]
        local_max = max(local_pri.values()) if local_pri else 0
        reverse = _stores_reflected(ctx, st_sec)
        for lid in graph.station_lines(sid_s):
            p = local_pri.get(lid, 0)
            if reverse:
                ctx.offsets[(sid_s, lid)] = (local_max - p) * ctx.offset_step
            else:
                ctx.offsets[(sid_s, lid)] = p * ctx.offset_step
    return section_local


def _section_line_feeders(
    ctx: _OffsetCtx, section: Section, sides: Container[PortSide] | None = None
) -> dict[str, str]:
    """Map each entering line to the upstream section that feeds it.

    *sides* restricts the scan to entry ports on those boundary sides; ``None``
    considers every entry port.
    """
    graph = ctx.graph
    line_feeder: dict[str, str] = {}
    for pid in section.entry_ports:
        if sides is not None:
            port = graph.ports.get(pid)
            if port is None or port.side not in sides:
                continue
        for edge in graph.edges_to(pid):
            src = graph.station_for_edge_source(edge)
            feeder_sec = src.section_id
            if feeder_sec is not None:
                line_feeder[edge.line_id] = feeder_sec
    return line_feeder


def _section_present_line_set(ctx: _OffsetCtx, sec_id: str) -> set[str]:
    """Lines that appear on any station of section *sec_id*."""
    return {lid for _sid, lid in section_node_lines(ctx.graph, sec_id)}


def _section_order_offsets(
    ctx: _OffsetCtx, sec_id: str, new_order: Sequence[str]
) -> dict[tuple[str, str], float]:
    """Per-(station, line) stored offsets that re-slot *sec_id* onto *new_order*.

    Slot 0 is the top (smallest offset); reversed sections count from the
    bottom so the same logical order draws on the same trunk side.
    """
    new_local = {lid: i for i, lid in enumerate(new_order)}
    local_max = len(new_order) - 1
    reverse = _stores_reflected(ctx, sec_id)
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
    ctx: _OffsetCtx, sec_id: str, new_order: Sequence[str]
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


def _feeder_seam_ports(
    ctx: _OffsetCtx, sec_id: str, feeder_id: str
) -> tuple[Port, Port] | None:
    """The ``(exit_port, entry_port)`` of the direct ``feeder_id -> sec_id`` seam."""
    graph = ctx.graph
    section = graph.sections[sec_id]
    for pid in section.entry_ports:
        entry_port = graph.ports.get(pid)
        if entry_port is None:
            continue
        for edge in graph.edges_to(pid):
            src_port = graph.ports.get(edge.source)
            if src_port is None or src_port.is_entry:
                continue
            if src_port.section_id == feeder_id:
                return src_port, entry_port
    return None


def _reorder_reconvergence(
    ctx: _OffsetCtx, section_local: dict[str, dict[str, int]]
) -> None:
    """Settle each reconvergence section's bundle on its primary feeder.

    When the primary feeder is a flat-frame neighbour the continuing lines must
    keep the feeder's offsets so the inter-section run stays level; otherwise
    they reach the section through a riser and just lead the bundle at the top.
    Single-line feeders from distinct rows order the bundle by feeder row.  A
    vertical-flow section fed by a single multi-line feeder takes that feeder's
    delivered logical order, transposed once iff the seam classifier reverses
    it, so the bundle rides the column in the order it arrives.
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

        primary_fid = max(lines_by_feeder, key=lambda f: len(lines_by_feeder[f]))
        primary_lines = set(lines_by_feeder[primary_fid])

        if len(lines_by_feeder) < 2:
            if len(primary_lines) < 2:
                continue
            seam = _feeder_seam_ports(ctx, sec_id, primary_fid)
            if seam is None:
                continue
            # The feeder's stored offsets are its delivered order.  A TOP/BOTTOM
            # column continuation drops straight and preserves that order; a
            # LEFT/RIGHT seam turns a corner that transposes it when the
            # classifier says so (a transposition the straight-drop offsets do
            # not already carry).
            is_side = seam[1].side in (PortSide.LEFT, PortSide.RIGHT)
            reverse = (
                is_side
                and seam_orientation(ctx.graph, *seam) is SeamOrientation.REVERSE
            )
            # Vertical-flow sections always settle on the feeder's delivered
            # order.  A horizontal section takes this path only for the
            # around-below half-turn -- a reversing seam into a section that does
            # not store its bundle reflected; forward LR/RL keep their priority
            # slots and reflected reverse-flow sections keep their stored order.
            if sec_id not in ctx.tb_sections and (
                not reverse or _stores_reflected(ctx, sec_id)
            ):
                continue
            feeder_off = _section_line_offsets(ctx, primary_fid)
            if not all(lid in feeder_off for lid in primary_lines):
                continue
            # ``_section_line_offsets`` reports each line's offset from the first
            # feeder station carrying it.  When the feeder's lines originate at
            # separate single-line producers that never share a station, those
            # offsets are each a local slot 0, not a unified bundle order, so two
            # lines can collide on one offset.  The delivered order is then
            # ambiguous (it resolves only at the feeder's exit port): leave the
            # section on its priority order rather than on an arbitrary tie-break.
            if len(
                distinct_offset_levels(feeder_off[lid] for lid in primary_lines)
            ) < len(primary_lines):
                continue
            delivered = sorted(primary_lines, key=lambda lid: feeder_off[lid])
            if reverse:
                delivered = list(reversed(delivered))
            config = BoundaryConfig(
                present=tuple(_section_present_line_set(ctx, sec_id)),
                determining=tuple(delivered),
            )
            new_order = lane_order(config, ctx.line_priority)
            if new_order is None:
                continue
            _apply_section_line_order(ctx, sec_id, new_order)
            continue

        if len(primary_lines) < 2:
            _order_reconvergence_by_feeder_row(ctx, sec_id, line_feeder)
            continue

        primary_order = section_local.get(primary_fid, ctx.line_priority)
        continuing = sorted(primary_lines, key=lambda lid: primary_order.get(lid, 0))

        sec_present = _section_present_line_set(ctx, sec_id)

        if _share_flat_frame(ctx, sec_id, primary_fid):
            returning = sorted(
                sec_present - primary_lines,
                key=lambda lid: ctx.line_priority.get(lid, 0),
            )
            _align_reconvergence_to_feeder(
                ctx, sec_id, continuing, returning, primary_fid
            )
            continue

        config = BoundaryConfig(
            present=tuple(sec_present), determining=tuple(continuing)
        )
        new_order = lane_order(config, ctx.line_priority)
        if new_order is None:
            continue

        _apply_section_line_order(ctx, sec_id, new_order)


def _section_exit_fanout_junction(ctx: _OffsetCtx, section: Section) -> str | None:
    """The single fan-out junction *section* exits into, if exactly one."""
    junction_ids = tuple(
        junction_id
        for junction_id, exit_port_id in ctx.divergence_exit_ports.items()
        if ctx.graph.ports[exit_port_id].section_id == section.id
    )
    return junction_ids[0] if len(junction_ids) == 1 else None


def _free_perp_entry_feeder(
    ctx: _OffsetCtx, section: Section, bundle: set[str]
) -> str | None:
    """The upstream feeder free to inherit *section*'s fan peel order.

    *section* owns a fan-out divergence, so its bundle must reach it already
    stacked in the order the fan peels off; a feeder delivering the same lines
    in the opposite order across *section*'s perpendicular entry makes the two
    descents cross in the drop.  Reslotting the feeder to match is only sound
    when the feeder's own order is unconstrained -- otherwise the mismatch has
    to be resolved on *section*'s side instead.  Returns the feeder section id
    when one upstream section delivers the whole *bundle* into *section* through
    a perpendicular entry port and is free: *section* is its only consumer and
    it owns no divergence junction of its own.

    Two scope limits are deliberate.  Only a horizontal-flow (LR/RL) feeder is
    eligible: a vertical-flow feeder stacks its bundle along the flow axis and
    is left to settle on its own.  And only the one direct feeder is inspected;
    the check never recurses up a chain of feeders, so a feeder that itself has
    an entry port is treated as constrained and left alone rather than followed
    to discover whether its own source is free.  An indirect upstream pin can
    therefore never be missed.
    """
    graph = ctx.graph
    perp_sides = perpendicular_port_sides(section.direction)
    line_feeder = _section_line_feeders(ctx, section, perp_sides)
    if not bundle <= set(line_feeder):
        return None
    feeder_ids = {line_feeder[lid] for lid in bundle}
    if len(feeder_ids) != 1:
        return None
    feeder_id = next(iter(feeder_ids))

    feeder = graph.sections.get(feeder_id)
    if feeder is None or not lanes_run_along_y(feeder.direction):
        return None
    if feeder.entry_ports:
        return None
    if _section_exit_fanout_junction(ctx, feeder) is not None:
        return None
    consumers = {
        tgt.section_id
        for pid in feeder.exit_ports
        for edge in graph.edges_from(pid)
        if (tgt := graph.station_for_edge_target(edge)).section_id is not None
    }
    if consumers != {section.id}:
        return None
    return feeder_id


def _perp_entry_port_carrying(
    graph: MetroGraph, section: Section, bundle: set[str]
) -> str | None:
    """The section's perpendicular entry port the whole *bundle* arrives on."""
    perp_sides = perpendicular_port_sides(section.direction)
    for pid in section.entry_ports:
        port = graph.ports.get(pid)
        if port is None or port.side not in perp_sides:
            continue
        if bundle <= set(graph.station_lines(pid)):
            return pid
    return None


def _perp_entry_arrival_order(
    graph: MetroGraph, port_id: str, peel_order: Sequence[str]
) -> tuple[str, ...]:
    """The order a free feeder must deliver *peel_order* across *port_id*.

    The bundle turns once from the feeder's vertical drop into the section's
    trunk.  That concentric corner maps the drop's cross-flow order to the
    trunk's peel order, and the mapping flips with two independent axes: the
    entry side the bundle arrives on (a BOTTOM arrival mirrors a TOP one) and the
    direction the run turns out of the port (:func:`_perp_entry_run_turns_right`;
    a leftward turn mirrors a rightward one).  The two flips compose, so the
    feeder inherits the peel order directly on one diagonal of that pair and
    reversed on the other.
    """
    port = graph.ports.get(port_id)
    bottom_entry = port is not None and port.side is PortSide.BOTTOM
    if bottom_entry != _perp_entry_run_turns_right(graph, port_id):
        return tuple(reversed(peel_order))
    return tuple(peel_order)


def _reorder_fanout_divergence(ctx: _OffsetCtx) -> None:
    """Order a section's bundle by where its lines peel off a shared exit fan.

    When distinct lines leave one section through a shared exit junction and drop
    to different columns on another row, they should descend as one bundle and
    split only where each peels into its target.  That is crossing-free only when
    the bundle's lead-in Y order matches the descent X order the fan channel
    assigns, so the source-section bundle is re-slotted into the same peel order
    (:func:`fanout_divergence_peel_order`) before the exit/junction ports inherit
    their offsets.

    The peel order also governs how the bundle arrives across the section's
    perpendicular entry.  The entry port turns the drop into the trunk through a
    concentric corner, so it re-slots onto the peel order turned through that
    corner (:func:`_perp_entry_arrival_order`), which mirrors with entry side and
    turn direction; that nests the drop into the trunk without crossing.  A trunk
    whose peel order matches plain priority skips its own re-slot, yet a mirrored
    entry side turns the port against that trunk regardless, so this arrival
    re-slot runs whenever a free upstream feeder
    (:func:`_free_perp_entry_feeder`) delivers the bundle.  The feeder itself
    keeps the plain peel order: it sits on the far side of the same corner, where
    its own exit turn already reflects its section order, so a second reflection
    would cross the bundle in the descent.

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
        peel_order = fanout_divergence_peel_order(
            graph, jid, ctx.line_priority, ctx.topology
        )
        if peel_order is None:
            continue

        config = BoundaryConfig(
            present=tuple(_section_present_line_set(ctx, sec_id)),
            determining=tuple(peel_order),
        )
        new_order = lane_order(config, ctx.line_priority)
        if new_order is not None:
            _apply_section_line_order(ctx, sec_id, new_order)

        feeder_id = _free_perp_entry_feeder(ctx, section, set(peel_order))
        if feeder_id is None:
            continue
        port_id = _perp_entry_port_carrying(graph, section, set(peel_order))
        if port_id is None:
            continue
        arrival = _perp_entry_arrival_order(graph, port_id, peel_order)

        for lid, off in _deal_slots_in_order(ctx, port_id, arrival).items():
            ctx.offsets[(port_id, lid)] = off

        feeder_config = BoundaryConfig(
            present=tuple(_section_present_line_set(ctx, feeder_id)),
            determining=tuple(peel_order),
        )
        feeder_order = lane_order(feeder_config, ctx.line_priority)
        if feeder_order is not None:
            _apply_section_line_order(ctx, feeder_id, feeder_order)


def _lines_holding_offset(
    ctx: _OffsetCtx,
    station_id: str,
    value: float,
    exclude: str,
    candidates: Iterable[str] | None = None,
) -> list[str]:
    """Lines sitting on lane *value* at *station_id*, *exclude* aside.

    *candidates* narrows the scan to a cohort; the station's whole line set
    otherwise.  Callers decide their own arity policy: a swap that only needs
    somebody to trade with takes the first, one that needs the lane provably
    held by a single line requires exactly one.
    """
    lines = ctx.graph.station_lines(station_id) if candidates is None else candidates
    return [
        line_id
        for line_id in lines
        if line_id != exclude
        and abs(ctx.offsets.get((station_id, line_id), 0.0) - value)
        <= _OFFSET_EQ_TOLERANCE
    ]


def _restore_fanout_peel_order(
    ctx: _OffsetCtx,
    same_y_adj: dict[str, dict[str, list[tuple[str, str]]]],
) -> None:
    """Re-seat a fan-out source bundle on its semantic peel order."""
    if ctx.compact:
        return
    graph = ctx.graph
    for junction_id, exit_port_id in ctx.divergence_exit_ports.items():
        section = graph.section_for_port(graph.ports[exit_port_id])
        if not lanes_run_along_y(section.direction):
            continue
        peel_order = fanout_divergence_peel_order(
            graph, junction_id, ctx.line_priority, ctx.topology
        )
        if peel_order is None:
            continue
        settled = sorted(
            ctx.offsets.get((exit_port_id, line_id), 0.0) for line_id in peel_order
        )
        for line_id, target in zip(peel_order, settled):
            current = ctx.offsets.get((exit_port_id, line_id), 0.0)
            if abs(current - target) <= _OFFSET_EQ_TOLERANCE:
                continue
            swap_line = next(
                iter(
                    _lines_holding_offset(
                        ctx, exit_port_id, target, line_id, peel_order
                    )
                ),
                None,
            )
            if swap_line is None:
                continue
            _propagate_offset_swap(
                ctx,
                same_y_adj,
                section.id,
                exit_port_id,
                line_id,
                swap_line,
                target,
                current,
            )
        _copy_exit_lanes_to_junction(ctx, junction_id, exit_port_id)


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
            src = graph.station_for_edge_source(e)
            if not src.is_port and src.section_id == sec_id:
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

    swap_lid = next(
        iter(_lines_holding_offset(ctx, sid, desired_off, lid, lines)), None
    )
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
        sec_reverse = _stores_reflected(ctx, sec_id)
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


def _hub_copy_would_desync_trunk_row(
    ctx: _OffsetCtx, hub_id: str, overlap: Sequence[str], offs: dict[str, float]
) -> bool:
    """Whether copying *offs* onto *hub_id* would only base-shift a trunk-row hub.

    The port re-centres its bundle on the feeder nearest its own Y (see
    :func:`_compute_exit_port_offsets`); when that leaves the port's overlap lines
    in the same rank order the hub already has, copying it does not reorder the
    bundle -- it only shifts its base.  Applying that shift to a hub sitting on a
    shared trunk row (a same-section, same-Y neighbour carrying two or more of the
    overlap lines) desyncs the hub from its row-mates, and horizontal
    reconciliation then unions the mismatch out to double width with an empty
    centre lane.  Only this base-shift case is caught; a genuine reorder (a
    different rank order, as a two-feeder fan-out needs) is not.
    """
    graph = ctx.graph
    hub = graph.stations[hub_id]
    port_order = sorted(overlap, key=lambda lid: offs[lid])
    hub_order = sorted(overlap, key=lambda lid: ctx.offsets.get((hub_id, lid), 0.0))
    if port_order != hub_order:
        return False
    overlap_set = set(overlap)
    section = graph.sections.get(hub.section_id) if hub.section_id else None
    for sid in section.station_ids if section else ():
        station = graph.stations[sid]
        if (
            sid == hub_id
            or station.is_port
            or abs(station.y - hub.y) > _SAME_Y_TOLERANCE
        ):
            continue
        if len(overlap_set & set(graph.station_lines(sid))) >= 2:
            return True
    return False


def _propagate_exit_offsets_to_hubs(
    ctx: _OffsetCtx, port_id: str, offs: dict[str, float]
) -> None:
    """Copy a port's per-line offsets onto its upstream hub stations.

    A hub is a station feeding two or more of the port's feeders; giving it the
    port's bundle ordering keeps the in-section run consistent up to the fan-out
    point.  A copy that would only base-shift a hub already on a shared trunk row
    is skipped; see :func:`_hub_copy_would_desync_trunk_row` for why.
    """
    graph = ctx.graph
    feeder_ids = {
        edge.source
        for edge in graph.edges_to(port_id)
        if not graph.station_for_edge_source(edge).is_port
    }
    if len(feeder_ids) < 2:
        return
    hub_candidates = {edge.source for fid in feeder_ids for edge in graph.edges_to(fid)}
    for hub_id in sorted(hub_candidates, key=ctx.station_rank.__getitem__):
        overlap = [lid for lid in graph.station_lines(hub_id) if lid in offs]
        if len(overlap) < 2 or _hub_copy_would_desync_trunk_row(
            ctx, hub_id, overlap, offs
        ):
            continue
        for lid in overlap:
            ctx.offsets[(hub_id, lid)] = offs[lid]


def _tb_exit_port_offset(
    ioff: float, max_int: float, right_entry: bool, right_exit: bool
) -> float:
    """The TB LEFT/RIGHT exit-port slot for a feeder's internal offset *ioff*.

    A RIGHT exit (down -> east turn) reverses the column across the corner; a
    LEFT exit keeps it.  A RIGHT-entry section already runs its column in raw
    order, so only one of the two reversals applies.
    """
    column_off = ioff if right_entry else max_int - ioff
    return max_int - column_off if right_exit else column_off


def _rerank_contiguous(
    ctx: _OffsetCtx, lines: Iterable[str], values: dict[str, float]
) -> dict[str, float]:
    """Re-rank *lines* onto contiguous ``offset_step``-spaced slots by *values*.

    Ties broken by line priority. Collapses a bundle whose incoming values
    carry gaps (an absent line's reserved slot, or distinct feeders that
    land on the same value) onto adjacent slots.
    """
    order = sorted(lines, key=lambda lid: (values[lid], ctx.line_priority.get(lid, 0)))
    return {lid: i * ctx.offset_step for i, lid in enumerate(order)}


def _exit_line_destination_y(
    graph: MetroGraph, port_id: str, line_id: str
) -> float | None:
    """Y of the node *line_id* lands on after leaving exit port *port_id*.

    Follows the line forward through intermediate junctions (which carry the
    line but are not where it settles) to the first entry port or real station
    it reaches, and returns that node's Y. Returns ``None`` when the line dead-
    ends or loops before reaching such a node.
    """
    current = port_id
    seen = {port_id}
    for _ in range(len(graph.stations) + 1):
        edge = next(
            (e for e in graph.edges_from(current) if e.line_id == line_id), None
        )
        if edge is None:
            return None
        nxt = edge.target
        if nxt in seen:
            return None
        seen.add(nxt)
        st = graph.station_for_edge_target(edge)
        port_obj = graph.ports.get(nxt)
        if (port_obj is not None and port_obj.is_entry) or (
            st is not None and not st.is_port
        ):
            return st.y if st is not None else None
        current = nxt
    return None


def _exit_trunk_feeder(
    graph: MetroGraph,
    line_feeders: dict[str, list[tuple[str, float]]],
    port_lines: set[str],
    station_rank: dict[str, int],
) -> str | None:
    """The feeder that carries the whole exit bundle out on one row, or None.

    A trunk feeder anchors every port line to its Y.  ``station_lines`` counts a
    line as present if it merely *arrives* at the feeder, even one the feeder
    re-tags into a new line rather than forwarding here, so a station that
    forwards just part of the port bundle can match every port line by that
    test.  Such a feeder is a real trunk only when the port lines it does not
    forward share its row: then pinning the bundle to its Y is harmless.  When
    those lines are fed from a station on another row there are genuinely two
    feeder heights, and the bundle must be ordered spatially so the higher-fed
    lines do not dive across the lower trunk to the far lanes.
    """
    all_feeders = {fid for entries in line_feeders.values() for fid, _ in entries}
    feeder_port_lines: dict[str, set[str]] = {}
    for lid, entries in line_feeders.items():
        for fid, _ in entries:
            feeder_port_lines.setdefault(fid, set()).add(lid)
    ordered_feeders = sorted(all_feeders, key=station_rank.__getitem__)
    for feeder_id in ordered_feeders:
        if port_lines.issubset(feeder_port_lines.get(feeder_id, set())):
            return feeder_id

    for feeder_id in ordered_feeders:
        if not port_lines.issubset(graph.station_lines(feeder_id)):
            continue
        trunk_y = graph.stations[feeder_id].y
        unforwarded_ys = {
            y
            for lid in port_lines - feeder_port_lines.get(feeder_id, set())
            for _, y in line_feeders[lid]
        }
        if all(abs(y - trunk_y) <= _SAME_Y_TOLERANCE for y in unforwarded_ys):
            return feeder_id
    return None


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
            src_st = graph.station_for_edge_source(edge)
            if not src_st.is_port:
                internal_offs[edge.line_id] = ctx.offsets.get(
                    (edge.source, edge.line_id), 0.0
                )
        if internal_offs:
            max_int = max(internal_offs.values())
            right_entry = port_obj.section_id in tb_right_entry
            right_exit = port_obj.side == PortSide.RIGHT
            assigned = {
                lid: _tb_exit_port_offset(ioff, max_int, right_entry, right_exit)
                for lid, ioff in internal_offs.items()
            }
            # Two lines fed from different stations can carry the same internal
            # offset (each feeder compacts its own gaps), collapsing them onto
            # one exit slot.  Re-rank the port onto distinct slots in the same
            # order so the converging lines stack instead of drawing on top.
            if len(set(assigned.values())) < len(assigned):
                assigned = _rerank_contiguous(ctx, assigned, assigned)
            for lid, off in assigned.items():
                ctx.offsets[(port_id, lid)] = off

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
            src_st = graph.station_for_edge_source(edge)
            if not src_st.is_port:
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
        section = graph.section_for_port(port_obj)
        entry_ports = list(section.entry_ports)
        flat_continuation = partial_flat_continuation_lines(graph, port_id, port_lines)
        if len(entry_ports) == 1 and not flat_continuation:
            entry_id = entry_ports[0]
            entry_lines = graph.station_lines(entry_id)
            if port_lines.issubset(entry_lines):
                if len(entry_lines) == len(port_lines):
                    inherited = {
                        lid: ctx.offsets.get((entry_id, lid), 0.0) for lid in port_lines
                    }
                else:
                    # A line that terminates inside the section without
                    # reaching this exit reserves an entry slot; re-rank the
                    # survivors onto contiguous slots so that reserved lane
                    # doesn't leave a gap here that the far side's entry port
                    # doesn't share.
                    values = {
                        lid: ctx.offsets.get((entry_id, lid), 0.0) for lid in port_lines
                    }
                    inherited = _rerank_contiguous(ctx, port_lines, values)
                for lid, off in inherited.items():
                    ctx.offsets[(port_id, lid)] = off
                _propagate_exit_offsets_to_hubs(ctx, port_id, inherited)
                continue

        trunk_feeder_id = _exit_trunk_feeder(
            graph, line_feeders, port_lines, ctx.station_rank
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
        # A line fed by two or more in-section stations is a merge/reporting
        # line that gathers the fan; its feeder-average Y lands mid-fan even
        # though it continues along the trunk to a station on the port's own
        # row.  Ordering it by that average wedges it into the middle of the
        # bundle, forcing the trunk-row feeder's contribution to dive across
        # the fan.  Ride it out on the trunk instead: anchor it at the port's
        # Y and lead the single-feeder fan lines, which then stack below.
        port_y = graph.stations[port_id].y
        merge_trunk_lines = {
            lid
            for lid, entries in line_feeders.items()
            if len(entries) >= 2
            and (dy := _exit_line_destination_y(graph, port_id, lid)) is not None
            and abs(dy - port_y) <= _SAME_Y_TOLERANCE
        }
        for lid in merge_trunk_lines:
            line_avg_y[lid] = port_y
        sorted_lines = sorted(
            line_avg_y,
            key=lambda lid: (
                line_avg_y[lid],
                0 if lid in merge_trunk_lines else 1,
                ctx.line_priority.get(lid, 0),
            ),
        )
        spatial_offs = {lid: i * ctx.offset_step for i, lid in enumerate(sorted_lines)}

        # Centre offsets on the feeder closest to the port's own Y.
        # Without this, reconciliation snaps same-Y stations to the
        # port's non-zero spatial offset, pushing them off-grid.
        # Ties broken by lowest spatial offset to avoid negative shifts.
        anchor_line = min(
            line_avg_y,
            key=lambda lid: (abs(line_avg_y[lid] - port_y), spatial_offs[lid]),
        )
        anchor_off = spatial_offs[anchor_line]
        # The feeder-Y anchor can pick a line that TURNS AWAY (its downstream
        # destination sits off the port's Y) while a different line continues
        # LEVEL to a destination at the port's Y. Anchoring on the turning line
        # leaves the level line off-centre, ramping its junction->entry
        # connector into an almost-horizontal segment. When a line genuinely
        # continues level and the feeder-Y anchor does not, re-anchor on the
        # level line so its connector stays flat and the turning line absorbs
        # the offset in its turn.
        dest_ys = {
            lid: _exit_line_destination_y(graph, port_id, lid) for lid in line_avg_y
        }
        level_lines = {
            lid: dy
            for lid, dy in dest_ys.items()
            if dy is not None and abs(dy - port_y) <= _SAME_Y_TOLERANCE
        }
        if flat_continuation:
            level_lines = {lid: port_y for lid in flat_continuation}
        if level_lines and anchor_line not in level_lines:
            anchor_line = min(
                level_lines,
                key=lambda lid: (abs(level_lines[lid] - port_y), spatial_offs[lid]),
            )
            anchor_off = spatial_offs[anchor_line]
        # A section whose flow was flipped to keep this exit on its producer's
        # end (a re-oriented backward feed) carries a cross-row fan whose
        # feeders sit on non-zero base slots; re-centring the port-nearest line
        # on zero would desync the port from those feeders and leave the bundle
        # on non-adjacent slots after reconciliation.  Anchor on the feeder's
        # own offset instead so the whole bundle keeps one frame.
        if graph.layout_provenance.direction_has_reason(
            port_obj.section_id, DecisionReason.FLOW_REORIENTED_DIRECTION
        ):
            anchor_feeders = line_feeders.get(anchor_line)
            if anchor_feeders:
                anchor_feeder_id = anchor_feeders[0][0]
                anchor_off -= ctx.offsets.get((anchor_feeder_id, anchor_line), 0.0)
        spatial_offs = {lid: off - anchor_off for lid, off in spatial_offs.items()}

        for lid, off in spatial_offs.items():
            ctx.offsets[(port_id, lid)] = off

        if not flat_continuation:
            _propagate_exit_offsets_to_hubs(ctx, port_id, spatial_offs)


def _copy_exit_lanes_to_junction(
    ctx: _OffsetCtx, junction_id: str, exit_port_id: str
) -> None:
    """Give a divergence junction the lanes its feeding exit port holds."""
    for line_id in ctx.graph.station_lines(junction_id):
        port_off = ctx.offsets.get((exit_port_id, line_id))
        if port_off is not None:
            ctx.offsets[(junction_id, line_id)] = port_off


def _propagate_to_junctions(ctx: _OffsetCtx) -> None:
    """Inherit offsets from upstream exit ports to junctions.

    Junctions have section_id=None so they get default line-priority
    ordering, which may not match the exit port feeding them.
    """
    for junction_id, exit_port_id in ctx.divergence_exit_ports.items():
        _copy_exit_lanes_to_junction(ctx, junction_id, exit_port_id)


def _apply_planned_fan_offsets(ctx: _OffsetCtx) -> None:
    """Apply the complete immutable offset assignment of every planned fan."""
    for plan in ctx.graph.fan_plans:
        if not plan.owns_geometry:
            continue
        for carrier in plan.offset_carriers:
            for assignment in carrier.assignments:
                ctx.offsets[(carrier.station_id, assignment.line_id)] = (
                    assignment.slot * ctx.offset_step
                )


def section_node_lines(graph: MetroGraph, sec_id: str) -> list[tuple[str, str]]:
    """Every ``(node_id, line_id)`` lane slot a section owns, ports included."""
    section = graph.sections[sec_id]
    return [
        (sid, lid)
        for sid in section.station_ids
        if sid in graph.stations
        for lid in graph.station_lines(sid)
    ]


def _section_lane_squeeze(
    ctx: _OffsetCtx, keys: Sequence[tuple[str, str]]
) -> dict[tuple[str, str], float]:
    """The drop each of *keys* takes to close the free slots between them.

    Each gap wider than one ``offset_step`` between consecutive occupied levels
    holds slots no line rides, so everything above it comes down by that many
    steps.  The drop never exceeds the gap that produced it, so levels stay
    strictly ordered and distinct: a station keeps one lane per line it carries.
    """
    step = ctx.offset_step
    levels = distinct_offset_levels(ctx.offsets.get(key, 0.0) for key in keys)
    drop_at: dict[float, float] = {levels[0]: 0.0} if levels else {}
    free = 0
    for lower, level in zip(levels, levels[1:]):
        free += max(0, round((level - lower) / step) - 1)
        drop_at[level] = free * step
    return {
        key: min(
            (
                drop
                for level, drop in drop_at.items()
                if abs(level - ctx.offsets.get(key, 0.0)) <= COORD_TOLERANCE_FINE
            ),
            default=0.0,
        )
        for key in keys
    }


def _junction_lanes_follow_port_squeeze(
    ctx: _OffsetCtx, shift: Mapping[tuple[str, str], float]
) -> dict[tuple[str, str], float]:
    """The drops of *shift* again for each junction riding a squeezed port's lanes.

    A divergence junction belongs to no section, so a section-wide relabelling
    of lane levels passes it by and strands it on the levels its feeding exit
    port has just come off.  Taking the same drops moves the two as one, which
    is what :func:`_dead_lane_squeeze_keeps_runs` then measures.  A junction
    already off its feeder's lanes is left where it is: it is holding a frame
    some other phase settled, and only whole-bundle inheritors take a new one
    (:func:`_junction_bundle_re_slots_whole`).
    """
    followed: dict[tuple[str, str], float] = {}
    shifted_stations = {station_id for station_id, _lid in shift}
    for junction_id, exit_port_id in ctx.divergence_exit_ports.items():
        if exit_port_id not in shifted_stations:
            continue
        if not ctx.bundle_re_slots_whole[(junction_id, exit_port_id)]:
            continue
        lines = ctx.graph.station_lines(junction_id)
        drops = {
            (junction_id, lid): shift[(exit_port_id, lid)]
            for lid in lines
            if (exit_port_id, lid) in shift
            and abs(
                ctx.offsets.get((junction_id, lid), 0.0)
                - ctx.offsets.get((exit_port_id, lid), 0.0)
            )
            <= _OFFSET_EQ_TOLERANCE
        }
        if len(drops) == len(lines):
            followed.update(drops)
    return followed


def _dead_lane_squeeze_keeps_runs(
    ctx: _OffsetCtx,
    shift: Mapping[tuple[str, str], float],
    edges_by_line: Mapping[str, Sequence[Edge]],
) -> bool:
    """Whether closing a section's free lanes leaves every run no worse.

    ``shift`` is the drop each of the section's lane slots takes, zero for the
    slots that stay.  Every edge carrying a shifted line is measured across: the
    lane difference between its two ends may shrink but never grow, so a run that
    was level inside the section or across one of its seams stays level, and a hop
    that already stepped never steps further.  That covers the seam ports, the
    bypass junctions that copy a port's lane and the plain interior runs, and it
    is what lets a boundary port follow the squeeze when doing so lands it on the
    lane its counterpart across the seam already holds.
    """
    for lid in {lid for (_, lid), drop in shift.items() if drop}:
        for edge in edges_by_line.get(lid, ()):
            src_key, tgt_key = (edge.source, lid), (edge.target, lid)
            drops = (shift.get(src_key, 0.0), shift.get(tgt_key, 0.0))
            if drops == (0.0, 0.0):
                continue
            before = ctx.offsets.get(src_key, 0.0) - ctx.offsets.get(tgt_key, 0.0)
            after = before - drops[0] + drops[1]
            if abs(after) > abs(before) + COORD_TOLERANCE_FINE:
                return False
    return True


def _close_section_dead_lanes(ctx: _OffsetCtx) -> None:
    """Drop lane levels no line of a section occupies anywhere.

    A station whose bundle skips a level draws a marker tall enough to span it.
    That reservation earns its space when a section-mate rides the level: a line
    cut at this station but carried either side of it keeps one lane through the
    whole section instead of stepping around the hole.  A level *no* line of the
    section rides reserves nothing, so every lane above it drops one step.

    The shift is a rigid, order-preserving relabelling of levels applied to every
    station and port of the section at once -- plus any divergence junction
    riding one of its exit ports' lanes
    (:func:`_junction_lanes_follow_port_squeeze`) -- so each station keeps exactly
    one lane per line it carries and the bundle order is untouched.  It is applied
    only when :func:`_dead_lane_squeeze_keeps_runs` confirms no run inside the
    section or across one of its seams tilts further for it.  Distinct from
    ``compact_offsets``, which sizes each station's bundle from that station's own
    line count and so gives one line different lanes at different stations; here a
    line keeps its single section-wide lane and only genuinely unclaimed levels
    are reclaimed.

    Where :func:`_compact_station_gaps` re-slots one station's lines early, before
    the port, fan and entry-frame phases have settled anything, this runs once
    everything else has, and moves a whole section's levels together or not at
    all.  It runs before the planned-fan offsets so a fan plan that owns a lane
    still has the last word on it.  A rail-laid section is skipped: its geometry
    is drawn from absolute rail coordinates, so its lane levels are not a bundle
    to tighten.
    """
    if ctx.compact:
        return
    squeezes: list[dict[tuple[str, str], float]] = []
    for sec_id in ctx.graph.sections:
        if ctx.graph.is_rail_section(sec_id):
            continue
        shift = _section_lane_squeeze(ctx, section_node_lines(ctx.graph, sec_id))
        if any(shift.values()):
            shift.update(_junction_lanes_follow_port_squeeze(ctx, shift))
            squeezes.append(shift)
    if not squeezes:
        return
    edges_by_line: dict[str, list[Edge]] = {}
    for edge in ctx.graph.edges:
        edges_by_line.setdefault(edge.line_id, []).append(edge)
    for shift in squeezes:
        if not _dead_lane_squeeze_keeps_runs(ctx, shift, edges_by_line):
            continue
        for key, drop in shift.items():
            if drop:
                ctx.offsets[key] = ctx.offsets.get(key, 0.0) - drop


def _perp_entry_run_turns_right(graph: MetroGraph, port_id: str) -> bool:
    """Whether the run leaving a TOP/BOTTOM entry port heads to larger X.

    The drop arrives at the port column and turns once into the consumer.  A
    consumer placed to the right of the port turns the bundle toward larger X
    (a down-then-right corner); one to the left turns it toward smaller X.  The
    turn side decides which exit slot lands on the inside of the entry corner,
    so it selects between the direct and mirrored offset maps.  Returns ``False``
    when no internal consumer is found or the consumer sits on the port column.
    """
    port_st = graph.stations.get(port_id)
    if port_st is None:
        return False
    consumer = perp_entry_consumer(graph, port_id)
    return consumer is not None and consumer.x > port_st.x + COORD_TOLERANCE_FINE


def _slot_perp_fan_bundle(ctx: _OffsetCtx, port_id: str) -> None:
    """Slot a distinct-line perp-entry bundle by feeder approach order.

    At a fan port (:func:`needs_perp_approach_fan`) the lines arrive on disjoint
    single-line feeders stacked above the section.  Order them by approach -- the
    feeder descending from furthest away (smallest source Y) takes the top slot --
    and carry that order through the section.  This must match the source-Y
    fan-in order :func:`common.compute_bundle_info` assigns, since
    :func:`perp._perp_approach_fan_x` fans the approach channels by that bundle
    index; agreeing keeps the descent, the turn, and the shared run consistent so
    the distinct lines never cross.
    """
    graph = ctx.graph
    feeders = sorted(
        (src.y, ctx.line_priority.get(edge.line_id, 0), edge.line_id)
        for edge in graph.edges_to(port_id)
        if (src := graph.station_for_edge_source(edge)).is_port
    )
    new_offs = {
        line_id: rank * ctx.offset_step
        for rank, (_y, _priority, line_id) in enumerate(feeders)
    }
    _apply_offsets_along_bundle(ctx, port_id, graph.ports[port_id].section_id, new_offs)


def _entry_top_from_tb_bottom_exits(ctx: _OffsetCtx) -> None:
    """Match TOP entry ports to the offsets of feeding TB BOTTOM exits.

    A TB BOTTOM exit drops each line straight down, preserving the per-line X
    position.  How the entry port matches depends on the receiver's flow axis:

    - **Vertical (TB/BT) receiver**: a straight column continuation -- both
      sections share the same rotation sign, so the exit offset is copied
      directly for each line.  Lines that arrive via a different feeder (not
      the TB BOTTOM exit) default to 0.0, collapsing them onto the column
      spine so they each drop straight to their target station.

    - **Horizontal (LR/RL) receiver**: the receiver is marked positive_fan by
      ``_detect_tb_bottom_top_entries``; its in-section draw uses
      ``y + offset`` while the drop places line ``i`` at ``x - offset_i``
      (for a standard-sign TB exit).  The concentric perp-entry corner pairs
      the line on the inside of the vertical drop with the line on the inside
      of the horizontal turn-in, and which exit slot lands inside depends on
      which way the run turns out of the port: a consumer to the right (the
      run turns toward larger X) keeps the order, ``entry_off = exit_off``; a
      consumer to the left (toward smaller X) reverses it, ``entry_off =
      max_exit_off - exit_off``.  Lines not at the exit also default to 0.0 and
      thus collapse to the innermost slot.

    In both cases the 0.0 default for lines absent from the exit port is
    intentional: it collapses lines from other feeders onto one slot, so each
    can drop vertically to its consumer rather than jogging horizontally first.

    A distinct-line perp entry (:func:`needs_perp_approach_fan` -- disjoint
    single-line feeders into a horizontal section) is exempt: collapsing its lines
    onto one slot would draw any shared run as a zero-offset collinear bundle.
    Its lines keep their distinct base/priority slots so the bundle separates,
    and the per-line approach channels are fanned at routing time.
    """
    graph = ctx.graph
    for port_id, port_obj in graph.ports.items():
        if not port_obj.is_entry or port_obj.side != PortSide.TOP:
            continue
        if needs_perp_approach_fan(graph, port_id):
            _slot_perp_fan_bundle(ctx, port_id)
            continue
        entry_section = graph.section_for_port(port_obj)
        for edge in graph.edges_to(port_id):
            src = graph.station_for_edge_source(edge)
            if not src.is_port:
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
            lines = graph.station_lines(port_id)
            if lanes_run_along_x(entry_section.direction):
                for lid in lines:
                    ctx.offsets[(port_id, lid)] = ctx.offsets.get(
                        (exit_port_id, lid), 0.0
                    )
            else:
                exit_line_offs = [
                    ctx.offsets.get((exit_port_id, lid), 0.0)
                    for lid in graph.station_lines(exit_port_id)
                ]
                max_exit_off = max(exit_line_offs) if exit_line_offs else 0.0
                keep_order = _perp_entry_run_turns_right(graph, port_id)
                new_offs = {}
                for lid in lines:
                    exit_off = ctx.offsets.get((exit_port_id, lid), 0.0)
                    new_offs[lid] = exit_off if keep_order else max_exit_off - exit_off
                _apply_offsets_along_bundle(ctx, port_id, entry_section.id, new_offs)
            break


def _deal_slots_in_order(
    ctx: _OffsetCtx, station_id: str, order: Sequence[str]
) -> dict[str, float]:
    """*station_id*'s own slots for ``order``'s lines, dealt in that sequence.

    Re-dealing the offsets already stored there keeps the bundle's spread and its
    place inside any wider bundle at that station, so only which line rides which
    slot changes.
    """
    slots = sorted(ctx.offsets.get((station_id, lid), 0.0) for lid in order)
    return dict(zip(order, slots))


def _straight_drop_feeder_exit(ctx: _OffsetCtx, port_id: str) -> Port | None:
    """The horizontal-flow BOTTOM exit dropping straight down into *port_id*.

    This is the seam whose per-line drop column is ``port_x + offset``, read off
    this entry port both by the drop (:func:`perp._perp_entry_crossing_x` for a
    horizontal-flow feeder) and by the feeding exit's own column
    (:func:`perp._perp_riser_lateral`).  ``None`` for every other feed, whose
    column comes from elsewhere: a vertical-flow feeder crosses on its own
    section lane, a distinct-line approach fan channels by bundle index, and an
    exit sharing the entry's Y rises into the inter-row corridor and comes back
    down on the reflected lateral.
    """
    graph = ctx.graph
    sources = {edge.source for edge in graph.edges_to(port_id)}
    if len(sources) != 1 or needs_perp_approach_fan(graph, port_id):
        return None
    exit_port = graph.ports.get(next(iter(sources)))
    if exit_port is None or exit_port.is_entry or exit_port.side is not PortSide.BOTTOM:
        return None
    if lanes_run_along_x(graph.section_for_port(exit_port).direction):
        return None
    exit_st, entry_st = graph.stations[exit_port.id], graph.stations[port_id]
    if exit_st.y >= entry_st.y or abs(exit_st.x - entry_st.x) > COORD_TOLERANCE_FINE:
        return None
    return exit_port


def _perp_exit_feed_lanes(ctx: _OffsetCtx, exit_port_id: str) -> dict[str, float]:
    """Lane offsets of the run arriving at perpendicular exit *exit_port_id*.

    Read off the in-section stations feeding the exit, which carry the lane each
    line rides right up to the turn down onto its drop column.
    """
    graph = ctx.graph
    return {
        lid: ctx.offsets.get((src.id, lid), 0.0)
        for edge in graph.edges_to(exit_port_id)
        if not (src := graph.station_for_edge_source(edge)).is_port
        for lid in graph.station_lines(src.id)
    }


def _order_perp_entry_seam_lanes(ctx: _OffsetCtx) -> None:
    """Nest a straight-drop TOP-entry seam's column and trunk against its turns.

    A horizontal-flow section entered through a TOP port fed by the BOTTOM exit
    straight above it (:func:`_straight_drop_feeder_exit`) carries the bundle
    through two 90-degree turns: the feeder's run turns down onto a per-line drop
    column, and that column turns into the receiver's trunk.  Both corners
    translate wholesale per line, so each nests concentrically only where the
    orders either side of it agree with the side it turns to.

    The column is ``port_x + offset`` read off the entry port, so the two orders
    settled here are the entry port's own offset (the column, west to east) and
    the receiver's trunk lane (top to bottom):

    * turning down out of the feeder's run puts the lane nearest the turn side on
      the inside of the bend -- an eastward (LR) run lands its topmost lane on the
      eastmost channel, a westward (RL) run its bottommost -- so the column
      reverses the feeder's lane order for LR and copies it for RL;
    * turning out of the column into the trunk puts the eastmost channel on the
      inside of an eastward turn, and the inside of that bend is the topmost trunk
      lane, so the trunk reverses the column there and copies it turning westward.

    The two are settled independently because the column has to nest against the
    feeder's turn as well: reversing the port along with the trunk would fix one
    corner by crossing the other.
    """
    graph = ctx.graph
    for port_id, port_obj in graph.ports.items():
        if not port_obj.is_entry or port_obj.side is not PortSide.TOP:
            continue
        section = graph.section_for_port(port_obj)
        if lanes_run_along_x(section.direction):
            continue
        feeder = _straight_drop_feeder_exit(ctx, port_id)
        if feeder is None:
            continue
        feed_lanes = _perp_exit_feed_lanes(ctx, feeder.id)
        seam = [lid for lid in graph.station_lines(port_id) if lid in feed_lanes]
        consumer = perp_entry_consumer(graph, port_id)
        if len(seam) < 2 or consumer is None:
            continue
        column = sorted(seam, key=lambda lid: (feed_lanes[lid], lid))
        if AxisFrame.flow_sign(graph.section_for_port(feeder).direction) > 0:
            column.reverse()
        trunk = column[::-1] if _perp_entry_run_turns_right(graph, port_id) else column
        _apply_offsets_along_bundle(
            ctx, consumer.id, section.id, _deal_slots_in_order(ctx, consumer.id, trunk)
        )
        ctx.offsets.update(
            ((port_id, lid), off)
            for lid, off in _deal_slots_in_order(ctx, port_id, column).items()
        )


def _propagate_lr_rl_exit_to_entry(ctx: _OffsetCtx) -> None:
    """Propagate single LR/RL exit-port offsets onto fed LEFT/RIGHT entry ports."""
    graph = ctx.graph
    for port_id, port_obj in graph.ports.items():
        if not port_obj.is_entry:
            continue
        if port_obj.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        if port_obj.section_id in ctx.tb_sections:
            # A vertical-flow consumer rides its own arrival-order lane (set by
            # the section reindex); copying a horizontal feeder's stored offset
            # across the seam would land the port off that lane.
            continue
        feeding_exit_ports: set[str] = set()
        for edge in graph.edges_to(port_id):
            src = graph.station_for_edge_source(edge)
            if not src.is_port:
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
        if (
            not _stores_reflected(ctx, port_obj.section_id)
            and seam_orientation(graph, src_port, port_obj) is SeamOrientation.REVERSE
        ):
            # A reversing seam (the around-below half-turn) into a section that
            # does not store its bundle reflected: the consumer rides its own
            # arrival-order lane (set by the reindex, _reorder_reconvergence), so
            # copying the feeder's stored offset across would undo the transpose.
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
                tgt_st = graph.station_for_edge_target(e2)
                if not tgt_st.is_port:
                    tgt_lines = graph.station_lines(e2.target)
                    overlap = [lid for lid in tgt_lines if lid in entry_offs]
                    if len(overlap) >= 2:
                        for lid in overlap:
                            ctx.offsets[(e2.target, lid)] = entry_offs[lid]


def _inherit_level_convergence_entry_offsets(ctx: _OffsetCtx) -> None:
    """A LR/RL entry port fed level by two or more upstream sources inherits
    each line's feeder offset, keeping the converged bundle consistent.

    The single-feeder propagation (:func:`_propagate_lr_rl_exit_to_entry`) does
    not fire when a port's lines arrive from more than one upstream port or
    junction, so a convergence entry falls back to base declaration order.  A
    through-trunk line fed level from the adjacent section (a reporting line
    that visited that section's station) is then slotted by declaration
    priority and dives across the fan to reach its lane, instead of riding the
    slot its feeder already presents on the trunk.

    Restricted to a genuine convergence (two or more distinct feeders) whose
    every feeder arrives on the port's own row (a level, horizontal seam) and
    whose inherited offsets form one distinct, contiguous run.  A gapped
    inherit would spread the entry bundle wider than base ordering (leaving an
    empty interior lane), so a subset-reserving or cross-row bundle - which the
    reindex and convergence-approach phases own - is left untouched.  An
    inherit that would collide a bundle line with a section-local line the
    bundle does not carry (:func:`_bundle_reslot_collides`) is left untouched
    too: the imported values answer to the feeders' bundle, not to the slots
    already taken further along this run.
    """
    graph = ctx.graph
    for port_id, port_obj in graph.ports.items():
        if not port_obj.is_entry or port_obj.side not in (
            PortSide.LEFT,
            PortSide.RIGHT,
        ):
            continue
        if port_obj.section_id not in ctx.lr_rl_sections:
            continue
        port_y = graph.stations[port_id].y
        feeders: set[str] = set()
        inherited: dict[str, float] = {}
        level = True
        for edge in graph.edges_to(port_id):
            feeders.add(edge.source)
            if abs(graph.stations[edge.source].y - port_y) > _SAME_Y_TOLERANCE:
                level = False
                break
            inherited[edge.line_id] = ctx.offsets.get((edge.source, edge.line_id), 0.0)
        if not level or len(feeders) < 2:
            continue
        if set(inherited) != set(graph.station_lines(port_id)):
            continue
        if len(set(inherited.values())) != len(inherited):
            continue
        levels = distinct_offset_levels(inherited.values())
        if max_interior_offset_gap(levels, ctx.offset_step) is not None:
            continue
        if _bundle_reslot_collides(ctx, port_id, port_obj.section_id, inherited):
            continue
        _apply_offsets_along_bundle(ctx, port_id, port_obj.section_id, inherited)


def _align_flat_tb_exit_to_entry(ctx: _OffsetCtx) -> None:
    """Snap a TB section's flat-seam LEFT/RIGHT exit bundle onto the entry it feeds.

    In an auto-folded serpentine the turn-around TB section exits sideways onto
    the return row: its LEFT/RIGHT exit port feeds an LR/RL section's LEFT/RIGHT
    entry port at the same Y, so the connector is a horizontal run.  The TB exit
    reflects its bundle within its own present-line width, while the receiving
    section anchors the same lines against its full bundle (reserving slots for
    lines that peel off deeper in the section).  When the two anchorings differ
    by a constant the shared lines keep their order but the connector slopes.

    Copy the entry's per-line offsets onto the exit port so the run is level.
    The exit's own feeder reaches it from a different column (a vertical drop),
    so absorbing the shift on the exit side adds no new slope; shifting the entry
    bundle the other way would collide it with the reserved peel-off slot.
    """
    graph = ctx.graph
    for port_id, port_obj in graph.ports.items():
        if port_obj.is_entry or port_obj.section_id not in ctx.tb_sections:
            continue
        if port_obj.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        exit_y = graph.stations[port_id].y
        for edge in graph.edges_from(port_id):
            entry = graph.ports.get(edge.target)
            if not (entry and entry.is_entry):
                continue
            if entry.side not in (PortSide.LEFT, PortSide.RIGHT):
                continue
            if entry.section_id not in ctx.lr_rl_sections:
                continue
            if abs(graph.stations[edge.target].y - exit_y) > _SAME_Y_TOLERANCE:
                continue
            entry_lines = set(graph.station_lines(edge.target))
            shared = [lid for lid in graph.station_lines(port_id) if lid in entry_lines]
            if len(shared) < 2:
                continue
            delta_levels = distinct_offset_levels(
                ctx.offsets.get((edge.target, lid), 0.0)
                - ctx.offsets.get((port_id, lid), 0.0)
                for lid in shared
            )
            # One delta level means the bundles share an order and differ only
            # in anchoring; multiple levels are a transpose handled elsewhere,
            # and a near-zero delta is already level.
            if len(delta_levels) != 1 or abs(delta_levels[0]) <= _OFFSET_EQ_TOLERANCE:
                continue
            for lid in shared:
                ctx.offsets[(port_id, lid)] = ctx.offsets.get((edge.target, lid), 0.0)


def _recenter_single_line_corridor_entry(ctx: _OffsetCtx) -> None:
    """Anchor a corridor-fed single-line section onto its trunk.

    A LEFT/RIGHT entry port of an LR/RL section that carries a single present
    line has no bundle to keep ordered: its global-priority offset is the lane
    the line held in the upstream multi-line section, and keeping it only drags
    the lone consumer off the section trunk, so the section reserves empty space
    for lines that never enter it.  When every feeder reaches the port on a
    different base Y -- a vertical corridor -- the lane step resolves in that
    vertical leg, so re-anchor the entry port (and, for a straight chain, every
    consumer carrying the line) at offset 0.  Anchoring the consumers too, rather
    than leaving horizontal reconciliation to settle them, keeps reconciliation's
    larger-magnitude preference from snapping the port back off the trunk onto
    the consumer's lane.

    When the single line forks internally, only the entry port is re-anchored:
    the fan branches straddle the trunk and each may hold a lane that aligns it
    with a downstream multi-line section, so pinning them to offset 0 would kink
    those hand-offs.

    A flat (same-Y) seam is re-anchored too, but only when its feeder already
    rides the trunk (offset 0): a solo entry level with a fan-out junction that
    was re-slotted onto slot 0 keeps a stale priority lane, so its
    junction-to-port run reads as an almost-horizontal slope.  Anchoring the
    entry to 0 there lands it flat and on the trunk.  When the feeder rides a
    non-zero lane the seam is left alone -- re-basing would tilt the level run.

    The corridor scope is exactly :func:`iter_corridor_fed_solo_entries` -- the
    same set the :func:`_guard_corridor_fed_solo_rides_trunk` invariant
    certifies.
    """
    graph = ctx.graph

    def _anchor(sec_id: str, port_id: str, line_id: str) -> None:
        ctx.offsets[(port_id, line_id)] = 0.0
        if line_forks_within_section(graph, graph.sections[sec_id], line_id):
            return
        for sid in graph.sections[sec_id].station_ids:
            st = graph.stations.get(sid)
            if st is None or st.is_port or line_id not in graph.station_lines(sid):
                continue
            ctx.offsets[(sid, line_id)] = 0.0

    for sec_id, port_id, line_id in iter_corridor_fed_solo_entries(
        graph, _SAME_Y_TOLERANCE
    ):
        _anchor(sec_id, port_id, line_id)

    for sec_id, port_id, line_id in iter_flat_seam_solo_entries(
        graph, _SAME_Y_TOLERANCE
    ):
        feeder_offsets = [
            ctx.offsets.get((e.source, line_id), 0.0)
            for e in graph.edges_to(port_id)
            if e.source in graph.stations
        ]
        if feeder_offsets and all(
            abs(off) <= COORD_TOLERANCE_FINE for off in feeder_offsets
        ):
            _anchor(sec_id, port_id, line_id)


def _single_feeding_exit_port(
    graph: MetroGraph, port_id: str, lines: Iterable[str]
) -> Port | None:
    """The single exit port supplying every one of *lines* into *port_id*, if any.

    ``None`` when the lines arrive from more than one upstream port (a genuine
    convergence, with no single feeder order to stay consistent with) or from
    no port at all.
    """
    feeders: set[str] = set()
    for edge in graph.edges_to(port_id):
        if edge.line_id not in lines:
            continue
        src_port = graph.ports.get(edge.source)
        if src_port is None or src_port.is_entry:
            return None
        feeders.add(edge.source)
    if len(feeders) != 1:
        return None
    return graph.ports[next(iter(feeders))]


def _order_perp_entry_by_landing_column(ctx: _OffsetCtx) -> None:
    """Order a TB/BT section's LEFT/RIGHT entry bundle by each line's landing column.

    Such a port carries its lines in on a single run that then turns onto each
    line's in-section column. When every line lands on the same column (the
    common case: one continuing trunk), the existing arrival order already
    nests. But when two or more lines turn onto *different* columns straight
    from the port -- one chain feeding one station, another chain feeding a
    different station -- concentric turns require the port's arrival order to
    match the columns' order along the run, not the lines' declaration
    priority: the line landing furthest into the section must ride the
    outermost (port-nearest) lane, else its run cuts across a nearer column's
    turn. Scoped to direct port-to-station edges only; a line whose next hop
    is itself a port (an interchange, a further fan-out) is left to whatever
    downstream phase owns that idiom.

    Skipped when a single exit port feeds every line here from a different
    trunk level: the exit-to-entry hop is then a genuine cornered turn whose
    concentricity depends on keeping the feeder's delivered order, not this
    port's landing columns (a level feeder has no such corner -- the hop is a
    flat run, so a swapped arrival order costs nothing there).
    """
    graph = ctx.graph
    for port_id, port_obj in graph.ports.items():
        if not port_obj.is_entry or port_obj.side not in (
            PortSide.LEFT,
            PortSide.RIGHT,
        ):
            continue
        section = graph.section_for_port(port_obj)
        if not lanes_run_along_x(section.direction):
            continue
        lines = graph.station_lines(port_id)
        if len(lines) < 2:
            continue

        feeder = _single_feeding_exit_port(graph, port_id, lines)
        if feeder is not None:
            port_station = graph.stations[port_id]
            feeder_station = graph.stations[feeder.id]
            if abs(feeder_station.y - port_station.y) > _SAME_Y_TOLERANCE:
                continue

        landing: dict[str, str] = {}
        for lid in lines:
            target_id: str | None = None
            for edge in graph.edges_from(port_id):
                if edge.line_id != lid:
                    continue
                tgt = graph.station_for_edge_target(edge)
                if tgt.is_port:
                    target_id = None
                else:
                    target_id = edge.target
                break
            if target_id is None:
                break
            landing[lid] = target_id
        if len(landing) != len(lines) or len(set(landing.values())) < 2:
            continue

        frame = _section_lane_frame(graph, section)
        # Concentric turns put the widest radius furthest along the run AND
        # furthest against the turn's vertical sense, so the lane reaching
        # deepest into the section rides the near lane where the flow runs down
        # the column and the far lane where it runs back up it.
        side_sign = 1.0 if port_obj.side is PortSide.LEFT else -1.0
        sign = side_sign * AxisFrame.flow_sign(section.direction)
        turn_reach = {
            lid: sign
            * station_lane_coord(
                frame, graph.stations[tid], ctx.offsets.get((tid, lid), 0.0)
            )
            for lid, tid in landing.items()
        }
        current_order = sorted(
            lines, key=lambda lid: ctx.offsets.get((port_id, lid), 0.0)
        )
        new_order = sorted(lines, key=lambda lid: turn_reach[lid], reverse=True)
        if new_order == current_order:
            continue
        for rank, lid in enumerate(new_order):
            ctx.offsets[(port_id, lid)] = rank * ctx.offset_step


def _compute_entry_port_offsets(ctx: _OffsetCtx) -> None:
    """Compute entry port offsets and propagate to downstream stations.

    Handles five cases:
    1. TOP entry ports fed by TB BOTTOM exits: match the reversed offset
       scheme used by inter-section routing.
    2. TOP entry ports fed by the BOTTOM exit straight above: nest the drop
       column and the trunk against the two turns the seam makes.
    3. LEFT/RIGHT entry ports fed by a single LR/RL exit: propagate
       spatial ordering to prevent bundle crossings.
    4. Corridor-fed single-line sections: re-anchor the entry port on the
       trunk so the lone consumer is not dragged into a phantom bundle lane.
    5. LEFT/RIGHT entry ports of a TB/BT section whose lines land on two or
       more distinct columns: reorder the bundle so the turns nest.
    """
    _entry_top_from_tb_bottom_exits(ctx)
    _order_perp_entry_seam_lanes(ctx)
    _propagate_lr_rl_exit_to_entry(ctx)
    _inherit_level_convergence_entry_offsets(ctx)
    _recenter_single_line_corridor_entry(ctx)
    _order_perp_entry_by_landing_column(ctx)


def _compact_station_gaps(
    ctx: _OffsetCtx,
    same_y_adj: dict[str, dict[str, list[tuple[str, str]]]],
    sec_layer_stations: dict[str, dict[int, list[str]]],
) -> None:
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

    Only triggers when gaps are actually found; no-op otherwise.  ``same_y_adj``
    and ``sec_layer_stations`` are section/layer indexes over graph structure
    (not per-line offsets), so :func:`_recompact_fan_port_bordering_stations`
    reuses the same ones rather than rebuilding them.
    """
    if ctx.compact:
        return

    graph = ctx.graph
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


def _recompact_fan_port_bordering_stations(
    ctx: _OffsetCtx,
    same_y_adj: dict[str, dict[str, list[tuple[str, str]]]],
    sec_layer_stations: dict[str, dict[int, list[str]]],
) -> None:
    """Re-close gaps at real stations a same-Y fan port re-opened after phase 4.

    Phase 4 (:func:`_compact_station_gaps`) runs before the exit and entry
    port phases (5-7) settle each port's own spatial or inherited order, and
    that settling always wins a straight rerun -- it can reopen a gap phase 4
    already closed at a real station bordering the port.  Resweeping the
    whole corpus a second time would risk disturbing unrelated compactions
    elsewhere that phases 5-11 legitimately depend on, so this only rechecks
    the stations :func:`invariants.fan_port_and_station` itself would flag --
    the exact precondition
    :func:`invariants.check_station_bundle_contiguous_at_fan_port` checks --
    and recompacts just those, via :func:`_compact_one_seed_with_retry`.
    """
    if ctx.compact:
        return
    graph = ctx.graph
    candidates: set[tuple[str, str]] = set()
    for edge in graph.edges:
        pair = fan_port_and_station(graph, edge)
        if pair is None:
            continue
        port_id, station_id = pair
        station = graph.stations[station_id]
        if abs(graph.stations[port_id].y - station.y) > _SAME_Y_TOLERANCE:
            continue
        if station.section_id is not None:
            candidates.add((station.section_id, station_id))

    # A line reused on non-adjacent fan legs can also leave the shared port's
    # own bundle non-contiguous (an unclaimed priority-rank slot), even once
    # every bordering real station is individually fixed above: the port
    # itself is never a compaction seed otherwise, since it borders no wider
    # port of its own.
    for port_id, port in graph.ports.items():
        if port.section_id is None:
            continue
        lines = graph.station_lines(port_id)
        if len(lines) < 2:
            continue
        levels = distinct_offset_levels(
            ctx.offsets.get((port_id, lid), 0.0) for lid in lines
        )
        if max_interior_offset_gap(levels, ctx.offset_step) is not None:
            candidates.add((port.section_id, port_id))

    for sec_id, station_id in sorted(candidates):
        section = graph.sections.get(sec_id)
        if section is None:
            continue
        n_sec_stations = sum(
            1 for sid in section.station_ids if not graph.stations[sid].is_port
        )
        touched = _compact_one_seed_with_retry(
            ctx, same_y_adj, sec_layer_stations, sec_id, station_id, n_sec_stations
        )
        _propagate_touched_exit_ports_to_entries(ctx, touched)


def _branch_leaves_junction_straight(
    graph: MetroGraph, junction_id: str, edge: Edge, lane_axis: str
) -> bool:
    """Whether *edge* leaves the junction along its lane instead of turning off it.

    Straight means the branch is known to reach its target without bending at
    the vertex: the target is an entry port on the side its section's flow
    starts from, sits on the junction's own lane level, and lies downstream of
    the junction along that flow, so the run into it carries on the way the
    junction already points.  A far-side port reached by wrapping around its
    section satisfies the first two and fails the third.  Everything else -- a
    perpendicular side, another lane level, a target that is not an entry port
    and so hands its shape on to routing -- counts as a turn: this is the false
    half of a guard against fusing two corners at one vertex, so what is not
    provably straight is treated as bending.
    """
    port = graph.ports.get(edge.target)
    if port is None or not port.is_entry:
        return False
    direction = graph.section_for_port(port).direction
    if port.side is not flow_port_sides(direction)[0]:
        return False
    junction, target = graph.stations[junction_id], graph.stations[edge.target]
    lane_gap = getattr(target, lane_axis) - getattr(junction, lane_axis)
    if abs(lane_gap) > _SAME_Y_TOLERANCE:
        return False
    flow_axis = AxisFrame.axes_for_direction(direction)[0]
    downstream = getattr(target, flow_axis) - getattr(junction, flow_axis)
    return downstream * AxisFrame.flow_sign(direction) > 0


def _junction_bundle_re_slots_whole(
    graph: MetroGraph, junction_id: str, exit_port_id: str
) -> bool:
    """Whether a divergence junction's bundle can take a new frame as one unit.

    A line leaving the junction on several branches that each turn off its lane
    turns them at one shared vertex, drawn as a single fused stroke whose radius
    is the widest of the legs (:func:`corners.widest_coincident_radius`).
    Pinned by the widest leg, that radius cannot follow the lane onto a
    neighbouring slot: the distinct mate it lands beside then reads as one
    wholesale-translated corner with it, and the concentric reference sized for
    the pair loses to the fusion, pinching the bundle through the bend.  Moving
    the other lanes without it would split the bundle instead, so a junction
    carrying a fused lane keeps the frame its own phases settled.

    A branch that leaves the vertex straight
    (:func:`_branch_leaves_junction_straight`) turns, if at all, further
    downstream, where it answers to its own geometry rather than to the vertex.
    It cannot fuse there, so a line holding the vertex with one turning branch
    and any number of straight ones re-slots with the rest of the bundle.
    """
    lane_axis = AxisFrame.axes_for_direction(
        graph.section_for_port(graph.ports[exit_port_id]).direction
    )[1]
    turning = Counter(
        edge.line_id
        for edge in graph.edges_from(junction_id)
        if not _branch_leaves_junction_straight(graph, junction_id, edge, lane_axis)
    )
    return all(count == 1 for count in turning.values())


def _port_frame_ranks_junction_lines_alike(
    ctx: _OffsetCtx, junction_id: str, exit_port_id: str
) -> bool:
    """Whether the port's lanes rank the junction's lines the way it holds them.

    Re-inheriting is meant to hand the junction the drop its feeder took, which
    every line of the bundle takes together and which no branch crosses another
    to follow.  A port that came out of the shift ranking those lines the other
    way is offering a transposition instead: taking it swaps which branch leaves
    the vertex on which side of the bundle, which settles the shape of the fan
    rather than closing the gap between the port and the junction, and can send
    a branch out across a section it never calls at.  That is a decision for the
    phases owning the fan, so a junction offered a reordered frame keeps the one
    its own phases settled.
    """
    ranked = sorted(
        (ctx.offsets.get((junction_id, line_id), 0.0), port_lane)
        for line_id in ctx.graph.station_lines(junction_id)
        if (port_lane := ctx.offsets.get((exit_port_id, line_id))) is not None
    )
    offered = [port_lane for _held_lane, port_lane in ranked]
    return offered == sorted(offered)


def _reinherit_junction_lanes(ctx: _OffsetCtx, moved: Container[str]) -> None:
    """Re-copy the lanes of every divergence junction whose feeder *moved*.

    :func:`_propagate_to_junctions` seats a junction on the lanes of the exit
    port feeding it, and a later phase that shifts that port's bundle leaves the
    junction holding the port's old frame.  The few pixels between the two are
    then spent slanting off the port's lane, and every branch beyond the
    junction spends them again coming back.  Restricted to junctions whose whole
    bundle can take the new frame (:func:`_junction_bundle_re_slots_whole`) in
    the order it already holds (:func:`_port_frame_ranks_junction_lines_alike`).
    """
    for junction_id, exit_port_id in ctx.divergence_exit_ports.items():
        if (
            exit_port_id in moved
            and ctx.bundle_re_slots_whole[(junction_id, exit_port_id)]
            and _port_frame_ranks_junction_lines_alike(ctx, junction_id, exit_port_id)
        ):
            _copy_exit_lanes_to_junction(ctx, junction_id, exit_port_id)


def _propagate_touched_exit_ports_to_entries(
    ctx: _OffsetCtx, touched: set[str]
) -> None:
    """Forward a recompacted exit port's new line offsets across its seam.

    :func:`_compact_one_seed_with_retry` only walks same-section edges, so any
    exit port it touched must be pushed across its seam to stay consistent
    with the entry port(s) it feeds in other sections -- the property
    :func:`invariants.check_seam_approach_equals_departure` checks.  Only the
    specific ports this recompaction changed are forwarded, not a full
    :func:`_compute_entry_port_offsets` rerun, which would re-derive entry
    ports from feeder geometry across the whole graph and undo unrelated,
    already-settled compactions elsewhere.

    Restricted to the same seams :func:`_propagate_lr_rl_exit_to_entry`
    would itself forward: a TOP/BOTTOM entry, a vertical-flow (TB) consumer,
    or a reversing seam each carry the line on their own arrival-order lane
    rather than the feeder's raw stored offset, so a direct copy there would
    be wrong, not merely redundant.  A divergence junction fed by such a port
    re-inherits through :func:`_reinherit_junction_lanes`.
    """
    graph = ctx.graph
    _reinherit_junction_lanes(ctx, touched)
    for sid in sorted(touched, key=ctx.station_rank.__getitem__):
        src_port = graph.ports.get(sid)
        if src_port is None or src_port.is_entry:
            continue
        for edge in graph.edges_from(sid):
            tgt_port = graph.ports.get(edge.target)
            if tgt_port is None or not tgt_port.is_entry:
                continue
            if tgt_port.side not in (PortSide.LEFT, PortSide.RIGHT):
                continue
            if tgt_port.section_id in ctx.tb_sections:
                continue
            reversed_seam = (
                seam_orientation(graph, src_port, tgt_port) is SeamOrientation.REVERSE
            )
            if not _stores_reflected(ctx, tgt_port.section_id) and reversed_seam:
                continue
            off = ctx.offsets.get((sid, edge.line_id))
            if off is not None:
                ctx.offsets[(edge.target, edge.line_id)] = off


def _seed_compaction(ctx: _OffsetCtx, seed_sid: str) -> dict[str, float] | None:
    """Target offsets that pack the seed's lines into consecutive slots, or None."""
    seed_lines = ctx.graph.station_lines_ordered(seed_sid)
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


def _anchored_seed_compaction(
    ctx: _OffsetCtx, seed_sid: str, fixed: frozenset[str]
) -> dict[str, float] | None:
    """Like :func:`_seed_compaction`, but ``fixed`` lines keep their offset.

    Used only by :func:`_compact_one_seed_with_retry`: a line a prior attempt
    found unsafe to move stays put, and the remaining movable lines slide
    into the free slots immediately below and above it instead.
    """
    graph = ctx.graph
    seed_lines = graph.station_lines_ordered(seed_sid)
    if len(seed_lines) < 2:
        return None

    current = {lid: ctx.offsets.get((seed_sid, lid), 0.0) for lid in seed_lines}
    fixed_here = [lid for lid in seed_lines if lid in fixed]
    movable = [lid for lid in seed_lines if lid not in fixed_here]
    if not fixed_here or not movable:
        return None

    fixed_levels = distinct_offset_levels(current[lid] for lid in fixed_here)
    if max_interior_offset_gap(fixed_levels, ctx.offset_step) is not None:
        return None  # the fixed lines themselves already have a gap
    low, high = fixed_levels[0], fixed_levels[-1]
    compacted = {lid: current[lid] for lid in fixed_here}
    below = sorted(
        (lid for lid in movable if current[lid] <= low), key=lambda lid: -current[lid]
    )
    above = sorted(
        (lid for lid in movable if current[lid] > low), key=lambda lid: current[lid]
    )
    next_below = low - ctx.offset_step
    for lid in below:
        compacted[lid] = next_below
        next_below -= ctx.offset_step
    next_above = high + ctx.offset_step
    for lid in above:
        compacted[lid] = next_above
        next_above += ctx.offset_step

    if not any(
        abs(compacted[lid] - current[lid]) > _OFFSET_EQ_TOLERANCE for lid in seed_lines
    ):
        return None
    return compacted


class _CompactionConflict(Exception):
    """Raised when propagation would need to also move an unsafe peer's line."""

    def __init__(self, lid: str) -> None:
        super().__init__(lid)
        self.lid = lid


def _compact_one_seed(
    ctx: _OffsetCtx,
    same_y_adj: dict[str, dict[str, list[tuple[str, str]]]],
    sec_layer_stations: dict[str, dict[int, list[str]]],
    sec_id: str,
    seed_sid: str,
    n_sec_stations: int,
) -> set[str]:
    """Compact one seed station's gaps, returning the stations it touched."""
    compacted = _seed_compaction(ctx, seed_sid)
    if compacted is None:
        return set()
    changed_lids = [
        lid
        for lid in ctx.graph.station_lines_ordered(seed_sid)
        if abs(compacted[lid] - ctx.offsets.get((seed_sid, lid), 0.0))
        > _OFFSET_EQ_TOLERANCE
    ]

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


def _compact_one_seed_with_retry(
    ctx: _OffsetCtx,
    same_y_adj: dict[str, dict[str, list[tuple[str, str]]]],
    sec_layer_stations: dict[str, dict[int, list[str]]],
    sec_id: str,
    seed_sid: str,
    n_sec_stations: int,
) -> set[str]:
    """Compact one seed's gaps, retrying with an unsafe line held fixed.

    A line whose propagation would cross into a same-layer peer's own
    territory (:func:`_compaction_peer_conflict`) is unsafe to move -- e.g. a
    line reused on two non-adjacent legs of a shared fan port, where each leg
    wants a different partner adjacent to it.  Rather than abandon the seed's
    gap entirely (:func:`_compact_one_seed`'s behaviour), retry holding that
    specific line fixed (:func:`_anchored_seed_compaction`) and moving only
    the seed's other lines, until either a safe combination is found or every
    line has been tried as the fixed one.  Every attempt, including the first
    (where no line is fixed yet and the target matches :func:`_seed_compaction`'s),
    excludes the seed itself from peer-conflict checks -- see ``mover`` on
    :func:`_propagate_compaction`.
    """
    graph = ctx.graph
    seed_lines = graph.station_lines_ordered(seed_sid)
    fixed: set[str] = set()
    for _ in range(len(seed_lines)):
        compacted = (
            _seed_compaction(ctx, seed_sid)
            if not fixed
            else _anchored_seed_compaction(ctx, seed_sid, frozenset(fixed))
        )
        if compacted is None:
            return set()
        changed_lids = [
            lid
            for lid in seed_lines
            if abs(compacted[lid] - ctx.offsets.get((seed_sid, lid), 0.0))
            > _OFFSET_EQ_TOLERANCE
        ]
        if not changed_lids:
            return set()

        try:
            pending = _propagate_compaction(
                ctx,
                same_y_adj,
                sec_layer_stations,
                sec_id,
                seed_sid,
                compacted,
                changed_lids,
                n_sec_stations,
                mover=seed_sid,
            )
        except _CompactionConflict as exc:
            fixed.add(exc.lid)
            continue

        if pending is None:
            return set()

        for sid, line_offsets in pending.items():
            for lid, off in line_offsets.items():
                ctx.offsets[(sid, lid)] = off
        return set(pending)

    return set()


def _compaction_peer_conflict(
    graph: MetroGraph,
    sec_layer_stations: dict[str, dict[int, list[str]]],
    sec_id: str,
    nbr_sid: str,
    lid: str,
    *,
    exclude: str | None = None,
) -> bool:
    """True if another visible same-layer peer also carries this line.

    Compaction can't guarantee consistency in that case without cascading
    into unrelated stations, so propagation must abort.  ``exclude`` -- when
    given, the station propagation is arriving from -- shares ``nbr_sid``'s
    layer only coincidentally when it has no other in-section predecessor; it
    is the line's own mover, not an external peer, so it never counts as a
    conflict against itself.  Only :func:`_compact_one_seed_with_retry`'s
    retryable propagation passes this; :func:`_compact_one_seed`'s plain,
    non-retrying path leaves it unset and applies the check unconditionally.
    """
    nbr_st = graph.stations[nbr_sid]
    layer_peers = sec_layer_stations.get(sec_id, {}).get(nbr_st.layer, [])
    for peer_sid in layer_peers:
        if peer_sid == nbr_sid or peer_sid == exclude:
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
    changed_lids: Sequence[str],
    n_sec_stations: int,
    *,
    mover: str | None = None,
) -> dict[str, dict[str, float]] | None:
    """BFS the compaction along same-Y edges; return updates, or None if unsafe.

    ``mover`` is set only by :func:`_compact_one_seed_with_retry`: it excludes
    the seed itself from peer-conflict checks (see
    :func:`_compaction_peer_conflict`) and raises :class:`_CompactionConflict`
    (naming the offending line) instead of returning ``None`` on a conflict,
    so the caller can retry holding that one line fixed.  Left unset, a
    conflict aborts the whole compaction for this seed.
    """
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
                graph, sec_layer_stations, sec_id, nbr_sid, lid, exclude=mover
            ):
                if mover is not None:
                    raise _CompactionConflict(lid)
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
    start_id: str,
    sec_id: str,
    new_offs: dict[str, float],
) -> None:
    """Set ``new_offs`` at ``start_id`` and carry it along the bundle.

    Walks ``edges_from`` from the start station, copying each moved line's new
    offset onto downstream stations.  In-section non-port stations always
    continue the bundle; ports and downstream sections continue only while the
    run stays on the start station's row, so a line re-slotted there keeps that
    slot all the way to its consumer rather than crossing back on the outgoing
    run.  A line that turns off the row stops the walk there and transitions its
    slot at the turn.

    A caller re-slotting at a perpendicular port passes the station the port
    turns into rather than the port itself: the port sits on the box edge, so a
    row measured from it holds nothing the run continues through.

    A port on one of the section's own lane-axis edges continues the bundle too:
    it never sits on the run's row, yet the trunk turns into its drop column
    through one concentric corner that carries the run's order across.  Leaving
    it on the pre-reslot order crosses the bundle at that turn.

    An off-row flow-side exit of the same section carries the re-slot as well,
    as a rank rather than as literal offsets: the climb up to it is one turn
    that takes the whole bundle across, but the port anchors its own lane set.
    The walk re-bases onto that port's row and slots and keeps going, so the
    crossing does not simply move to the seam beyond it.
    """
    graph = ctx.graph
    for lid, off in new_offs.items():
        ctx.offsets[(start_id, lid)] = off
    for tgt_id, offs in _bundle_walk(ctx, start_id, sec_id, new_offs):
        for lid in graph.station_lines(tgt_id):
            if lid in offs:
                ctx.offsets[(tgt_id, lid)] = offs[lid]


def _bundle_walk(
    ctx: _OffsetCtx,
    start_id: str,
    sec_id: str,
    new_offs: Mapping[str, float],
) -> list[tuple[str, dict[str, float]]]:
    """Stations downstream of *start_id* a bundle re-slot carries onto, paired
    with the offsets each one takes.

    The reach depends only on the graph and the rows the run stands on, so a
    caller can ask which stations a re-slot would touch before deciding whether
    to make it.

    Up to the section's own exit port the run carries *new_offs* itself: the
    re-slot names the slots it wants and the stations along it answer to the
    same lane frame.  Crossing an exit the run has to *climb* re-bases it onto
    the port's row; from there the walk carries only the *order*, re-dealing it
    onto each station's own slots.  Past the port every station belongs to some
    other section, which anchors its lanes for its own reasons; stamping this
    run's literal offsets onto them would shift their bundles wholesale rather
    than settling the one thing the climb has to settle.

    The climb is measured against the station the run leaves, not the row the
    walk started on -- these differ once the walk has stepped between lanes
    inside the section.  An exit level with the run's own row is a flat
    continuation, not a climb, and carries no order across.

    Only a Y-stacked (LR/RL) section climbs this way.  A vertical flow's exit
    is off-Y by construction rather than by climbing, and it reverses its
    offsets to arc concentrically instead of inheriting the entry's order, so
    carrying an order across it would be the wrong idiom.
    """
    graph = ctx.graph
    direction = graph.sections[sec_id].direction
    lane_edge_sides = perpendicular_port_sides(direction)
    flow_edge_sides = flow_port_sides(direction)
    lane_stacked_on_y = lanes_run_along_y(direction)
    reached: list[tuple[str, dict[str, float]]] = []
    visited = {start_id}
    # queue item: (station, row_y, offs, already-rebased-onto-own-slots)
    queue = deque([(start_id, graph.stations[start_id].y, dict(new_offs), False)])
    while queue:
        cur, row_y, offs, rebased = queue.popleft()
        cur_y = graph.stations[cur].y
        for edge in graph.edges_from(cur):
            tgt_id = edge.target
            if tgt_id in visited:
                continue
            tgt = graph.stations[tgt_id]
            tgt_port = graph.ports.get(tgt_id)
            in_section = tgt.section_id == sec_id and (
                tgt_port is None or tgt_port.side in lane_edge_sides
            )
            on_row = abs(tgt.y - row_y) <= _SAME_Y_TOLERANCE
            own_flow_exit = (
                lane_stacked_on_y
                and tgt_port is not None
                and not tgt_port.is_entry
                and tgt_port.section_id == sec_id
                and tgt_port.side in flow_edge_sides
                and abs(tgt.y - cur_y) > _SAME_Y_TOLERANCE
            )
            if not in_section and not on_row and not own_flow_exit:
                continue
            crosses_exit = not in_section and not on_row
            tgt_row = tgt.y if crosses_exit else row_y
            tgt_rebased = rebased or crosses_exit
            tgt_offs = (
                _ranked_onto_held_slots(ctx, tgt_id, cur, offs) if tgt_rebased else offs
            )
            visited.add(tgt_id)
            reached.append((tgt_id, tgt_offs))
            queue.append((tgt_id, tgt_row, tgt_offs, tgt_rebased))
    return reached


def _ranked_onto_held_slots(
    ctx: _OffsetCtx,
    station_id: str,
    source_id: str,
    pending: Mapping[str, float],
) -> dict[str, float]:
    """*source_id*'s line order re-expressed on the slots *station_id* holds.

    A port answers to its own neighbours for which lanes it occupies, so
    importing a run's literal offsets can widen or shift its bundle.  Permuting
    its lines across the slots it already holds moves the order alone, and so
    cannot seat one of them on a slot another line of the port keeps.

    The order read off *source_id* combines *pending* (the lines an in-flight
    re-slot moves) with ``ctx.offsets`` (the co-travellers it leaves in place),
    since a re-slot may touch only one line of a bundle.
    """
    graph = ctx.graph
    source_lines = set(graph.station_lines(source_id))
    carried = [lid for lid in graph.station_lines(station_id) if lid in source_lines]
    ranked = sorted(
        carried,
        key=lambda lid: (
            pending[lid] if lid in pending else ctx.offsets.get((source_id, lid), 0.0),
            lid,
        ),
    )
    return _deal_slots_in_order(ctx, station_id, ranked)


def _bundle_reslot_collides(
    ctx: _OffsetCtx,
    start_id: str,
    sec_id: str,
    new_offs: dict[str, float],
) -> bool:
    """Whether carrying *new_offs* along the bundle seats two lines on one slot.

    Values imported from another bundle need not be a permutation of the slots
    in use along this one: a station the run reaches can carry a line the bundle
    does not (one that starts inside the section), and moving a bundle line onto
    that line's slot draws the two strokes on top of each other.
    """
    graph = ctx.graph
    return any(
        abs(offs[moved] - ctx.offsets.get((sid, held), 0.0)) <= _OFFSET_EQ_TOLERANCE
        for sid, offs in (
            (start_id, dict(new_offs)),
            *_bundle_walk(ctx, start_id, sec_id, new_offs),
        )
        for lines in (set(graph.station_lines(sid)),)
        for moved in lines & offs.keys()
        for held in lines - offs.keys()
    )


def _is_symmetric_fork_arm(graph: MetroGraph, sid: str) -> bool:
    """True when *sid* is one arm of a symmetric in-section fork.

    Two stations that share an in-section predecessor and sit on opposite sides
    of it in Y mirror about the section trunk, so their rendered markers must
    stay equidistant from it.  Only one arm can lie on a downstream port's row,
    so a re-slot that shifts that arm's line off its base offset skews the pair
    off the trunk.
    """
    st = graph.stations[sid]
    for pred_edge in graph.edges_to(sid):
        pred = graph.stations.get(pred_edge.source)
        if pred is None:
            continue
        d = st.y - pred.y
        if abs(d) <= _SAME_Y_TOLERANCE:
            continue
        for sib_edge in graph.edges_from(pred_edge.source):
            if sib_edge.target == sid:
                continue
            sib = graph.stations.get(sib_edge.target)
            if sib is None or sib.section_id != st.section_id:
                continue
            if (sib.y - pred.y) * d < 0:
                return True
    return False


def _row_upstream_line_sources(
    graph: MetroGraph,
    port_id: str,
    line_id: str,
    descend: Callable[[str], bool] = lambda _sid: True,
) -> Iterator[str]:
    """Yield sources on *line_id*'s flat approach into *port_id*, breadth-first.

    Follows ``edges_to`` from the port along *line_id* while the run stays on the
    port's row, yielding each source-side station.  *descend* decides, per
    yielded station, whether the walk continues past it; a feeder that turns off
    the row (a riser) is never reached, and a caller can prune further (e.g. stop
    before a slot collision).
    """
    row_y = graph.stations[port_id].y
    visited = {port_id}
    queue = deque([port_id])
    while queue:
        cur = queue.popleft()
        for edge in graph.edges_to(cur):
            if edge.line_id != line_id or edge.source in visited:
                continue
            if abs(graph.stations[edge.source].y - row_y) > _SAME_Y_TOLERANCE:
                continue
            visited.add(edge.source)
            yield edge.source
            if descend(edge.source):
                queue.append(edge.source)


def _line_traces_to_fork_arm(graph: MetroGraph, port_id: str, line_id: str) -> bool:
    """True when *line_id*'s flat approach into *port_id* rides a fork arm.

    Any symmetric fork arm on the approach must keep its base offset, so a
    re-slot that would shift the line has to anchor on it instead.
    """
    return any(
        _is_symmetric_fork_arm(graph, src)
        for src in _row_upstream_line_sources(graph, port_id, line_id)
    )


def _apply_offset_upstream_on_row(
    ctx: _OffsetCtx, port_id: str, line_id: str, off: float
) -> None:
    """Carry a reslotted feeder's offset upstream along its flat approach.

    Copies *off* onto each source-side station on *line_id*'s flat approach into
    the port.  A feeder re-slotted at the port whose approach is horizontal (an
    adjacent on-row feeder) would otherwise kink where its source-side slot
    differs from the port slot; carrying the new slot back to its source keeps it
    straight.  The walk stops before a slot collision (which would fuse two lines
    into one stroke), letting that feeder transition its slot at the collision
    station rather than upstream.
    """
    for src in _row_upstream_line_sources(
        ctx.graph,
        port_id,
        line_id,
        descend=lambda sid: not _would_collide(ctx, sid, line_id, off),
    ):
        if not _would_collide(ctx, src, line_id, off):
            ctx.offsets[(src, line_id)] = off


def _convergence_feeders(
    graph: MetroGraph, port_id: str
) -> list[tuple[str, int, bool]] | None:
    """Classify a LEFT entry port's bypass-convergence feeders.

    Returns ``[(line_id, source_col, is_bypass), ...]`` when several lines
    converge into *port_id* and need approach-depth slotting; ``None`` otherwise.

    All-bypass bundles qualify. Mixed bundles require one line per source
    column, then qualify when either at least two feeders bypass intervening
    sections or a single bypass and its nearer feeders descend from one
    off-target row. A single bypass joined by a flat same-row feeder does not
    form a climbing bundle and remains in its ordinary lane order.
    """
    target_col, target_row = _resolve_section_colrow(graph, graph.stations[port_id])
    if target_col is None:
        return None

    feeders: list[tuple[str, int, bool]] = []
    source_rows: set[int | None] = set()
    for edge in graph.edges_to(port_id):
        source = graph.station_for_edge_source(edge)
        source_col, source_row = _resolve_section_colrow(graph, source)
        if source_col is None:
            return None
        bypass = abs(target_col - source_col) > 1 and _has_intervening_sections(
            graph, source_col, target_col, source_row
        )
        feeders.append((edge.line_id, source_col, bypass))
        source_rows.add(source_row)

    source_cols = {source_col for _line_id, source_col, _bypass in feeders}
    if len(source_cols) < 2:
        return None
    if all(bypass for _line_id, _source_col, bypass in feeders):
        return feeders
    if len(source_cols) != len(feeders) or len({f[0] for f in feeders}) != len(feeders):
        return None

    bypass_count = sum(bypass for _line_id, _source_col, bypass in feeders)
    if bypass_count >= 2:
        return feeders
    if bypass_count == 1 and len(source_rows) == 1 and target_row not in source_rows:
        return feeders
    return None


def cross_row_convergence_channel_order(
    graph: MetroGraph, port_id: str
) -> list[str] | None:
    """Outer-to-inner channels for a single-bypass off-row convergence."""
    feeders = _convergence_feeders(graph, port_id)
    if feeders is None:
        return None
    if sum(bypass for _, _, bypass in feeders) != 1:
        return None
    return [line_id for line_id, _col, _bypass in sorted(feeders, key=lambda f: f[1])]


def _rises_from_same_row_bypass(
    graph: MetroGraph,
    source: Station,
    port_station: Station,
    *,
    source_colrow: tuple[int | None, int | None] | None = None,
    target_colrow: tuple[int | None, int | None] | None = None,
) -> bool:
    """Whether a feeder level with *port_station* reaches it on a riser.

    A source on the port's own grid row that hops past intervening sections in
    that row routes round below them and climbs back up into the port, so its
    connector is not flat even though both ends share a Y.  The below-row
    channel is read from :func:`bypass_bottom_y`, the lane the router lays that
    detour on; a channel no lower than the port is a straight run instead.
    Callers that already resolved either grid cell pass it in.
    """
    target_col, target_row = target_colrow or _resolve_section_colrow(
        graph, port_station
    )
    source_col, source_row = source_colrow or _resolve_section_colrow(graph, source)
    if target_col is None or source_col is None or source_row != target_row:
        return False
    if abs(target_col - source_col) <= 1 or not _has_intervening_sections(
        graph, source_col, target_col, source_row
    ):
        return False
    channel_y = bypass_bottom_y(
        graph, source_col, target_col, src_row=source_row, tgt_row=target_row
    )
    return channel_y > port_station.y + _SAME_Y_TOLERANCE


_Approach = Literal["flat", "below", "above"]


@dataclass(frozen=True)
class SameRowBypassEntry:
    """A flow-side entry where same-row bypass risers join flat feeders.

    ``feeders`` names each line's same-row feeder station and ``riser_depth``
    each riser's column distance to the port; every line not in
    ``riser_depth`` arrives flat.
    """

    feeders: Mapping[str, str]
    riser_depth: Mapping[str, int]

    @property
    def risers(self) -> frozenset[str]:
        return frozenset(self.riser_depth)

    @property
    def flat(self) -> frozenset[str]:
        return frozenset(self.feeders) - self.risers


def same_row_bypass_entry(graph: MetroGraph, port_id: str) -> SameRowBypassEntry | None:
    """Classify *port_id* as a same-row bypass entry, or ``None``.

    Every line must reach the port from a feeder level with it on the port's
    grid row, either flat or rising from a same-row bypass channel below; at
    least one of each is needed.  A line fed more than once qualifies only when
    all its feeders approach from the same side -- one flat and one rising feeder
    for the same line cannot both be honoured by a single lane, so such a port
    keeps its ordinary lane order.  A merge-fed confluence is left to the
    routing-time band alignment that owns it (see
    :func:`_port_fed_through_merge_junction`).
    """
    port_station = graph.stations[port_id]
    target_col, target_row = _resolve_section_colrow(graph, port_station)
    if target_col is None or _port_fed_through_merge_junction(graph, port_id):
        return None
    sides: dict[str, set[_Approach]] = {}
    feeders: dict[str, str] = {}
    depth: dict[str, int] = {}
    for edge in graph.edges_to(port_id):
        lid = edge.line_id
        source = graph.station_for_edge_source(edge)
        source_col, source_row = _resolve_section_colrow(graph, source)
        dy = source.y - port_station.y
        if source_row != target_row or abs(dy) > _SAME_Y_TOLERANCE:
            sides.setdefault(lid, set()).add("below" if dy > 0 else "above")
            continue
        rises = _rises_from_same_row_bypass(
            graph,
            source,
            port_station,
            source_colrow=(source_col, source_row),
            target_colrow=(target_col, target_row),
        )
        sides.setdefault(lid, set()).add("below" if rises else "flat")
        feeders.setdefault(lid, edge.source)
        if rises and source_col is not None:
            depth.setdefault(lid, abs(target_col - source_col))
    if set(sides) != set(feeders) or any(len(s) > 1 for s in sides.values()):
        return None
    entry = SameRowBypassEntry(feeders, depth)
    if not entry.flat or not entry.risers:
        return None
    return entry


def _section_flow_bundle(graph: MetroGraph, section: Section) -> frozenset[str]:
    """Lines crossing *section*'s flow-axis boundary ports."""
    sides = flow_port_sides(section.direction)
    return frozenset(
        lid
        for pid in (*section.entry_ports, *section.exit_ports)
        if graph.ports[pid].side in sides
        for lid in graph.station_lines(pid)
    )


def _keeping_flat_lanes_drifts_row_trunk(
    ctx: _OffsetCtx, section: Section, kept: Mapping[str, float]
) -> bool:
    """Whether a kept-lane slotting would drift *section*'s trunk off its row.

    Keeping the flat lines on their arrival lanes can start the section's block
    below its top lane.  That is harmless unless another section on the same
    grid row carries exactly the same bundle: the two trunk markers then stand
    at different heights along one row.  In that case the bundle has to pack
    from the top lane instead, and a flat line steps once at the seam.
    """
    if min(kept.values()) <= _OFFSET_EQ_TOLERANCE:
        return False
    graph = ctx.graph
    bundle = _section_flow_bundle(graph, section)
    return any(
        other.id != section.id
        and other.grid_row == section.grid_row
        and lanes_run_along_y(other.direction)
        and _section_flow_bundle(graph, other) == bundle
        for other in graph.sections.values()
    )


def _slot_same_row_bypass_entry(ctx: _OffsetCtx, port_id: str, port: Port) -> None:
    """Seat same-row bypass risers beside the flat lines at *port_id*.

    The flat lines keep the lanes they arrive on, in that order, so none of them
    steps aside at the port.  The risers take the lanes directly below them,
    nested by approach depth: the nearer source's riser turns in innermost, next
    to the flat band, and lines from one source keep the order they leave it in,
    since a bypass carries its bundle round the U unchanged.

    Where the flat lanes are not one contiguous block, or keeping them would
    drift the section's trunk (see :func:`_keeping_flat_lanes_drifts_row_trunk`),
    the bundle packs from the top lane and each moved flat line carries its new
    lane back along its level approach.
    """
    entry = same_row_bypass_entry(ctx.graph, port_id)
    if entry is None:
        return
    arrival = {
        lid: ctx.offsets.get((feeder, lid), 0.0)
        for lid, feeder in entry.feeders.items()
    }

    def priority(lid: str) -> int:
        return ctx.line_priority.get(lid, 0)

    flat = sorted(entry.flat, key=lambda lid: (arrival[lid], priority(lid)))
    risers = sorted(
        entry.risers,
        key=lambda lid: (entry.riser_depth[lid], arrival[lid], priority(lid)),
    )
    step = ctx.offset_step
    new_offs = {lid: arrival[lid] for lid in flat}
    new_offs.update(
        {lid: arrival[flat[-1]] + rank * step for rank, lid in enumerate(risers, 1)}
    )
    contiguous = all(
        abs(arrival[right] - arrival[left] - step) <= _OFFSET_EQ_TOLERANCE
        for left, right in zip(flat, flat[1:])
    )
    section = ctx.graph.sections[port.section_id]
    packed = not contiguous or _keeping_flat_lanes_drifts_row_trunk(
        ctx, section, new_offs
    )
    if packed:
        new_offs = {lid: rank * step for rank, lid in enumerate((*flat, *risers))}
    if not any(
        abs(new_offs[lid] - ctx.offsets.get((port_id, lid), 0.0)) > _OFFSET_EQ_TOLERANCE
        for lid in new_offs
    ):
        return
    _apply_offsets_along_bundle(ctx, port_id, port.section_id, new_offs)
    if packed:
        for lid in flat:
            _carry_lane_up_level_approach(ctx, port_id, lid, new_offs[lid])


def _carry_lane_up_level_approach(
    ctx: _OffsetCtx, port_id: str, line_id: str, off: float
) -> None:
    """Move *line_id* onto *off* along its whole level approach, or not at all.

    Walks the line's feeders back from *port_id* while they stay level with it,
    stopping at a same-row bypass riser, which absorbs a lane change in its
    climb.  If some station on the way holds *off* for another line, the lane
    cannot be carried to where the approach begins; moving part of it would
    only relocate the step inside the approach, so the line keeps its lanes and
    steps once, on its connector into the port.

    It cannot reuse :func:`_row_upstream_line_sources` /
    :func:`_apply_offset_upstream_on_row`: that walk yields a station before
    deciding whether to continue past it (so it would commit the riser's feeder)
    and applies a partial carry up to a collision rather than none at all.
    """
    graph = ctx.graph
    port_station = graph.stations[port_id]
    chain: list[str] = []
    frontier, seen = [port_id], {port_id}
    while frontier:
        target = graph.stations[frontier.pop()]
        for edge in graph.edges_to(target.id):
            source = graph.stations[edge.source]
            if (
                edge.line_id != line_id
                or edge.source in seen
                or abs(source.y - port_station.y) > _SAME_Y_TOLERANCE
                or _rises_from_same_row_bypass(graph, source, target)
            ):
                continue
            if _would_collide(ctx, edge.source, line_id, off):
                return
            seen.add(edge.source)
            chain.append(edge.source)
            frontier.append(edge.source)
    for station_id in chain:
        ctx.offsets[(station_id, line_id)] = off


def _flow_entry_lane_y_ports(ctx: _OffsetCtx) -> Iterator[tuple[str, Port]]:
    """Yield each entry port on the flow-start side of a Y-laned section."""
    graph = ctx.graph
    for port_id, port in graph.ports.items():
        if not port.is_entry:
            continue
        direction = graph.section_for_port(port).direction
        if lanes_run_along_y(direction) and port.side is flow_port_sides(direction)[0]:
            yield port_id, port


def _left_entry_lr_ports(ctx: _OffsetCtx) -> Iterator[tuple[str, Port]]:
    """Yield each LEFT entry port on a forward (non-reversed) LR section.

    A bundle re-slotted at such a port runs straight in along the section's
    flow, so the convergence-ordering passes share this guard.
    """
    graph = ctx.graph
    for port_id, port in graph.ports.items():
        if not (port.is_entry and port.side is PortSide.LEFT):
            continue
        sec = graph.section_for_port(port)
        if sec.direction != "LR" or port.section_id in ctx.reversed_sections:
            continue
        yield port_id, port


def _order_convergence_entry_ports(ctx: _OffsetCtx) -> None:
    """Slot a shared entry port's converging bundle by approach order.

    Lines from two or more source columns ride one bypass trunk into a shared
    LEFT entry port.  Their crossing-free slot order is by approach depth - the
    nearer source (higher grid column) on the shallow, port-near slot - not the
    declaration order the base offsets give.  Assign each line the offset its
    approach rank earns and carry it along the consumer section so the bundle
    stays in that order from the port to its first station.  The matching peel
    order on the risers is set by ``_convergence_line_order`` at routing time.

    A shallow feeder joining the bundle flat from an adjacent column also has
    its new slot carried back along its horizontal approach to its source, so
    it runs straight into the port instead of kinking where its source-side
    slot differs from the port slot.

    Any other flow-side entry of an LR or RL section where same-row bypass
    risers join flat feeders is slotted by :func:`_slot_same_row_bypass_entry`.
    """
    if ctx.compact:
        return
    graph = ctx.graph
    approach_depth_ports = dict(_left_entry_lr_ports(ctx))
    for port_id, port in _flow_entry_lane_y_ports(ctx):
        feeders = (
            _convergence_feeders(graph, port_id)
            if port_id in approach_depth_ports
            else None
        )
        if feeders is None:
            _slot_same_row_bypass_entry(ctx, port_id, port)
            continue
        line_col = {lid: col for lid, col, _ in feeders}
        ordered = sorted(
            line_col, key=lambda lid: (-line_col[lid], ctx.line_priority.get(lid, 0))
        )
        new_offs = {lid: rank * ctx.offset_step for rank, lid in enumerate(ordered)}
        cur = {lid: ctx.offsets.get((port_id, lid), 0.0) for lid in ordered}
        if not any(
            abs(new_offs[lid] - cur[lid]) > _OFFSET_EQ_TOLERANCE for lid in new_offs
        ):
            continue
        _apply_offsets_along_bundle(ctx, port_id, port.section_id, new_offs)
        for lid, _col, is_bypass in feeders:
            if not is_bypass:
                _apply_offset_upstream_on_row(ctx, port_id, lid, new_offs[lid])


def _order_convergence_by_approach(ctx: _OffsetCtx) -> None:
    """Slot a LEFT entry port's multi-section bundle by feeder approach Y.

    Lines from sections at different grid rows converge into one shared LEFT
    entry port.  The base offsets slot them by line-declaration order, so a
    feeder whose source sits high but whose line is declared last lands on the
    bottom lane: its line then runs down past its bundle-mates to reach that
    lane, crossing them and -- in compact mode, where the lanes pack into the
    inter-column gap -- producing a counter-direction leg that aborts the
    render.  Order the lanes by source Y instead, so the feeder approaching
    from highest takes the topmost lane and every riser turns in without
    crossing.  Carry the new order along the consumer section so the bundle
    holds it from the port to its first station.

    The non-compact bundle order emerges crossing-free from its own pipeline
    (:func:`_order_convergence_entry_ports`) except for the top-descent case
    handled by :func:`_order_top_descent_over_left_entry`, so this targets
    compact mode.
    """
    if not ctx.compact:
        return
    graph = ctx.graph
    for port_id, port in _left_entry_lr_ports(ctx):
        source_y: dict[str, float] = {}
        upstream_secs: set[str] = set()
        for edge in graph.edges_to(port_id):
            src = graph.station_for_edge_source(edge)
            lid = edge.line_id
            source_y[lid] = min(source_y.get(lid, src.y), src.y)
            if src.section_id is not None and src.section_id != port.section_id:
                upstream_secs.add(src.section_id)
        if len(source_y) < 2 or len(upstream_secs) < 2:
            continue
        ordered = sorted(
            source_y, key=lambda lid: (source_y[lid], ctx.line_priority.get(lid, 0))
        )
        cur = {lid: ctx.offsets.get((port_id, lid), 0.0) for lid in ordered}
        base = min(cur.values())
        new_offs = {
            lid: base + rank * ctx.offset_step for rank, lid in enumerate(ordered)
        }
        if not any(
            abs(new_offs[lid] - cur[lid]) > _OFFSET_EQ_TOLERANCE for lid in new_offs
        ):
            continue
        _apply_offsets_along_bundle(ctx, port_id, port.section_id, new_offs)
        for lid in ordered:
            _apply_offset_upstream_on_row(ctx, port_id, lid, new_offs[lid])


def _left_entry_feeder_rows(
    ctx: _OffsetCtx, port_id: str, grid_col: int
) -> dict[str, int] | None:
    """Each line's feeder grid row at a LEFT entry *port_id*.

    Rows resolve through fan-out junctions (whose own section is undefined).
    Returns ``None`` if any feeder reaches the port from a column right of
    *grid_col* -- a bypass wrap whose shared runway trunk cannot hold a per-lane
    split -- or from a source with no resolvable grid cell.
    """
    graph = ctx.graph
    line_row: dict[str, int] = {}
    for edge in graph.edges_to(port_id):
        col, row = _resolve_section_colrow(graph, graph.station_for_edge_source(edge))
        if col is None or row is None or col > grid_col:
            return None
        line_row[edge.line_id] = min(line_row.get(edge.line_id, row), row)
    return line_row


def _port_fed_through_merge_junction(graph: MetroGraph, port_id: str) -> bool:
    """Whether a line reaches *port_id* through a merge (reconvergence) junction.

    Such a port is a merge-fed confluence: one leg arrives through the merge
    junction while another descends from a higher row.  The descent geometry of
    that confluence is owned by ``_align_merge_fed_confluence_to_band`` at
    routing time, which seats the descent onto the band's nesting order; a
    port-offset reorder here would fight it and land the descending line on the
    inner internal lane its outer band contradicts.
    """
    merges = merge_junction_ids(graph)
    return any(edge.source in merges for edge in graph.edges_to(port_id))


def _order_top_descent_over_left_entry(ctx: _OffsetCtx) -> None:
    """Put a line descending into a LEFT entry port from above on the top lane.

    A section fed at one LEFT entry port by a line arriving level from its own
    grid row and a line descending from a row above slots the bundle by line
    declaration order, so a descending line declared last lands on the bottom
    lane and dives under the level feeder at the boundary -- reading as the
    lower stroke through every internal branch (#1410).  Order the lanes so the
    feeder from the highest row leads, matching the height each arrives at.

    Scoped to the forward top-descent case the compact-only
    :func:`_order_convergence_by_approach` mirrors: every feeder must reach the
    port from a column at or left of the target (see
    :func:`_left_entry_feeder_rows`), and at least one must descend from a row
    above.
    """
    if ctx.compact:
        return
    graph = ctx.graph
    for port_id, port in _left_entry_lr_ports(ctx):
        if _convergence_feeders(graph, port_id) is not None:
            continue
        section = graph.sections[port.section_id]
        line_feeder = _section_line_feeders(ctx, section)
        feeder_ids = set(line_feeder.values())
        if len(feeder_ids) == 1:
            seam = _feeder_seam_ports(ctx, section.id, next(iter(feeder_ids)))
            if (
                seam is not None
                and seam_orientation(graph, *seam) is SeamOrientation.REVERSE
            ):
                continue
        line_row = _left_entry_feeder_rows(ctx, port_id, section.grid_col)
        if line_row is None or len(line_row) < 2:
            continue
        if min(line_row.values()) >= section.grid_row:
            continue
        ordered = sorted(
            line_row, key=lambda lid: (line_row[lid], ctx.line_priority.get(lid, 0))
        )
        cur = {lid: ctx.offsets.get((port_id, lid), 0.0) for lid in ordered}
        base = min(cur.values())
        # A feeder whose flat approach rides a symmetric fork arm must keep its
        # base offset: shifting it strands the offset as an almost-flat seam and
        # skews the fork off the trunk.  Anchor the ladder on it so the
        # descending line takes the slot above rather than pushing it down.
        for rank, lid in enumerate(ordered):
            if _line_traces_to_fork_arm(graph, port_id, lid):
                base = cur[lid] - rank * ctx.offset_step
                break
        new_offs = {
            lid: base + rank * ctx.offset_step for rank, lid in enumerate(ordered)
        }
        if not any(
            abs(new_offs[lid] - cur[lid]) > _OFFSET_EQ_TOLERANCE for lid in new_offs
        ):
            continue
        # Placed after the reorder's detection and no-change gate, not beside
        # the classification skips above: the corpus's merge-fed ports reach here
        # through that detection, so an earlier exit strands those branches
        # un-exercised and reds the routing gate-coverage ratchet.
        if _port_fed_through_merge_junction(graph, port_id):
            continue
        _apply_offsets_along_bundle(ctx, port_id, port.section_id, new_offs)
        for lid in ordered:
            _apply_offset_upstream_on_row(ctx, port_id, lid, new_offs[lid])


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

    A shifted exit port carries its divergence junction with it
    (:func:`_reinherit_junction_lanes`), which is outside the same-section
    filter above: a junction has no section of its own.
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

    moved: set[str] = set()
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
                    for sid, off in ((edge.source, src_off), (edge.target, tgt_off)):
                        ctx.offsets[(sid, lid)] = candidate
                        if off != candidate:
                            moved.add(sid)
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
                moved.add(move_sid)
                changed = True

        if not changed:
            break

    _reinherit_junction_lanes(ctx, moved)


def _center_rail_boundary_port_bundles(ctx: _OffsetCtx) -> None:
    """Centre a rail-laid section's boundary-port bundle on the port itself.

    A rail section carries each line on its own widely-spaced rail, and the port
    is the middle of the fan between those rails and the tight lane stack the
    bundle arrives in, so the lanes belong either side of it.  Slotted from the
    top lane downwards instead, the fan opens lopsidedly and the lane nearest a
    rail crosses only its leftover lane offset, drawing a stub transition too
    short to carry the corner radii at each end of it.

    The shift is rigid, so the bundle keeps its order and its pitch and the
    connector feeding the port keeps its shape.  A line whose rail is the port's
    own Y then needs no transition at all and runs straight through, the way an
    interchange draws its through line.
    """
    graph = ctx.graph
    if not graph.has_rail_sections:
        return

    for port_id, port in graph.ports.items():
        if not graph.is_rail_section(port.section_id):
            continue
        lanes = {
            lid: ctx.offsets.get((port_id, lid), 0.0)
            for lid in graph.station_lines(port_id)
        }
        shift = (
            min(lanes.values(), default=0.0) + max(lanes.values(), default=0.0)
        ) / 2.0
        for lid, lane in lanes.items():
            ctx.offsets[(port_id, lid)] = lane - shift


def _sole_in_section_consumer(
    graph: MetroGraph, port_id: str, section_id: str, lines: Container[str]
) -> str | None:
    """The one real station in *section_id* that *lines* reach from *port_id*.

    ``None`` when they reach several, or none: a port feeding more than one
    station of the section hands its bundle to a fan, not to a single first stop.
    """
    consumers = {
        edge.target
        for edge in graph.edges_from(port_id)
        if edge.line_id in lines
        and not graph.stations[edge.target].is_port
        and graph.stations[edge.target].section_id == section_id
    }
    if len(consumers) != 1:
        return None
    return next(iter(consumers))


def _slot_entry_arrivals_on_their_approach_lane(
    ctx: _OffsetCtx,
    same_y_adj: dict[str, dict[str, list[tuple[str, str]]]],
) -> None:
    """Put a line entering a lane-stacked section on the lane it arrives on.

    An inter-section run arrives on the lane its upstream carrier draws.  Where
    the receiving section reserves a different slot for that line, the run
    between the entry port and the first station spends the difference as a
    sub-radius diagonal; a port draws no marker, so that step lands in open
    space just inside the section box.  Exchanging the arriving line's slot for
    the one already holding the approach lane keeps a reserved slot per line -
    the exchange is a permutation of the slots the station occupies - and lets
    the run in stay flat.  The swap propagates along the section's horizontal
    runs, so the line that gave up the approach lane carries its new slot for
    the rest of its chain.
    """
    if ctx.compact:
        return
    graph = ctx.graph
    for section in graph.sections.values():
        if not lanes_run_along_y(section.direction):
            continue
        flow_entry, flow_exit = flow_port_sides(section.direction)
        for port_id in section.entry_ports:
            if graph.ports[port_id].side is not flow_entry:
                continue
            for line_id in graph.station_lines(port_id):
                _slot_one_entry_arrival(
                    ctx, same_y_adj, section, port_id, line_id, flow_exit
                )


def _slot_one_entry_arrival(
    ctx: _OffsetCtx,
    same_y_adj: dict[str, dict[str, list[tuple[str, str]]]],
    section: Section,
    port_id: str,
    line_id: str,
    flow_exit: PortSide,
) -> None:
    """Swap one arriving line onto its approach lane, or leave the bundle alone.

    Declines unless the exchange is provably a permutation of lanes already in
    use at the first station and provably crossing-free: the approach lane must
    be held by exactly one other line, and that line must originate at the
    station, so it shares no run with the arriving line on the approach side and
    the two only meet under the station's own marker.  The approach lane is read
    off the feeder as a stored lane offset, which names the same lane at the
    consumer only while the feeder's trunk is level with it, so a stepped seam is
    declined rather than converted through screen coordinates.

    An exchange that reaches one of the section's exit ports carries on through
    :func:`_exchange_pair_beyond_exit`, so the bundle the pair joins outside
    holds the same order they leave on.
    """
    graph = ctx.graph
    port_station = graph.stations[port_id]
    consumer_id = _sole_in_section_consumer(graph, port_id, section.id, (line_id,))
    if consumer_id is None:
        return
    consumer = graph.stations[consumer_id]
    if abs(consumer.y - port_station.y) > _SAME_Y_TOLERANCE:
        return
    upstream = _upstream_section_lane(ctx, port_id, section.id, line_id, flow_exit)
    if upstream is None:
        return
    _feeder_section_id, feeder_station_id, feeder_offset = upstream
    if abs(graph.stations[feeder_station_id].y - consumer.y) > _SAME_Y_TOLERANCE:
        return
    desired = feeder_offset
    current = ctx.offsets.get((consumer_id, line_id), 0.0)
    if abs(current - desired) <= _OFFSET_EQ_TOLERANCE:
        return
    holders = _lines_holding_offset(ctx, consumer_id, desired, line_id)
    if len(holders) != 1:
        return
    holder = holders[0]
    if holder in ctx.inbound.get(consumer_id, set()):
        return
    before_exits = {
        exit_port_id: (
            ctx.offsets.get((exit_port_id, line_id)),
            ctx.offsets.get((exit_port_id, holder)),
        )
        for exit_port_id in section.exit_ports
    }
    _propagate_offset_swap(
        ctx, same_y_adj, section.id, consumer_id, line_id, holder, desired, current
    )
    for exit_port_id, (was_arriving, was_holder) in before_exits.items():
        if was_arriving is None or was_holder is None:
            continue
        if _offsets_exchanged(
            ctx, exit_port_id, line_id, holder, was_arriving, was_holder
        ):
            _exchange_pair_beyond_exit(ctx, section, exit_port_id, line_id, holder)


def _offsets_exchanged(
    ctx: _OffsetCtx,
    station_id: str,
    first: str,
    second: str,
    was_first: float,
    was_second: float,
) -> bool:
    """Whether *first* and *second* now hold each other's slot at *station_id*."""
    return (
        abs(ctx.offsets.get((station_id, first), 0.0) - was_second)
        <= _OFFSET_EQ_TOLERANCE
        and abs(ctx.offsets.get((station_id, second), 0.0) - was_first)
        <= _OFFSET_EQ_TOLERANCE
        and abs(was_first - was_second) > _OFFSET_EQ_TOLERANCE
    )


def _exchange_pair_at(
    ctx: _OffsetCtx, station_id: str, first: str, second: str
) -> bool:
    """Give each of two lines the other's slot at *station_id*, if both hold one."""
    arriving = ctx.offsets.get((station_id, first))
    holding = ctx.offsets.get((station_id, second))
    if arriving is None or holding is None:
        return False
    ctx.offsets[(station_id, first)] = holding
    ctx.offsets[(station_id, second)] = arriving
    return True


def _exchange_pair_beyond_exit(
    ctx: _OffsetCtx,
    section: Section,
    exit_port_id: str,
    first: str,
    second: str,
) -> None:
    """Carry a slot exchange out of *section* along the pair's shared chain.

    A line leaving through an exit port joins a bundle whose lead-in order the
    divergence junction mirrors from that port and whose peel order the
    destination's own lanes fix.  Exchanging the pair inside the section only
    leaves those two orders disagreeing, so the bundle's two legs meet the
    junction on different lanes and the pair inverts through the first corner
    outside the box.  Continuing the exchange downstream holds the whole chain
    on one order.

    A section is taken whole: a line crossing it with no station of its own
    passes through as a hidden through-lane, with no edge of its own to follow,
    so the walk exchanges the pair at every station of a section it enters that
    carries them both, and follows the pair's edges only to reach the next
    section or the junctions between them.

    Only the pair's own two slots ever move and only where both lines already
    hold one, so every station keeps the lanes it had: the exchange stays a
    permutation the whole way out.  It stops where the pair stops travelling
    together, which is where each line's own lane is free again.
    """
    graph = ctx.graph
    pair = {first, second}
    frontier = [exit_port_id]
    seen_stations = {*section.station_ids, exit_port_id}
    seen_sections = {section.id}
    while frontier:
        for edge in graph.edges_from(frontier.pop()):
            if edge.line_id not in pair:
                continue
            target = graph.stations.get(edge.target)
            if target is None:
                continue
            if target.section_id is None:
                reached: tuple[str, ...] = (edge.target,)
            elif target.section_id in seen_sections:
                continue
            else:
                seen_sections.add(target.section_id)
                reached = tuple(graph.sections[target.section_id].station_ids)
            for station_id in reached:
                if station_id in seen_stations:
                    continue
                if not pair <= set(graph.station_lines(station_id)):
                    continue
                seen_stations.add(station_id)
                if _exchange_pair_at(ctx, station_id, first, second):
                    frontier.append(station_id)


def _upstream_section_lane(
    ctx: _OffsetCtx,
    entry_port_id: str,
    target_section_id: str,
    line_id: str,
    upstream_exit_side: PortSide,
) -> tuple[str, str, float] | None:
    """Return the unique upstream carrier and slot supplying an entry lane."""
    if ctx.topology is None:
        return None
    port_connectors = tuple(
        ctx.topology.connector(connector_id)
        for connector_id in ctx.topology.connector_ids_for_port(entry_port_id)
    )
    connectors = tuple(
        connector
        for connector in port_connectors
        if connector.line_id == line_id
        and connector.target_section == target_section_id
        and connector.exit_side is upstream_exit_side
    )
    owners = {
        (
            connector.source_section,
            ctx.topology.exit_port(connector.exit_group_id),
        )
        for connector in connectors
    }
    if len(owners) != 1:
        return None
    owner_section_id, owner_station_id = next(iter(owners))
    key = owner_station_id, line_id
    if key not in ctx.offsets:
        return None
    return owner_section_id, owner_station_id, ctx.offsets[key]


def _is_flat_handover_hub(
    ctx: _OffsetCtx,
    section: Section,
    continuing_set: set[str],
    present: set[str],
    carrying: Sequence[str],
    non_carrying: Sequence[str],
) -> bool:
    """Whether a terminating cohort hands the trunk to a single flat successor.

    Admits one shape when the cohort stops short of the section's last station:
    every station past the cohort carries none of it, one carrier originates the
    whole local bundle, and each of those lines runs flat to its sole in-section
    stop.  A cohort that fans out or peels off - a line splitting to several
    stops, or a successor holding part of the cohort - draws a real turn and is
    refused.
    """
    graph = ctx.graph
    if any(
        not continuing_set.isdisjoint(graph.station_lines(sid)) for sid in non_carrying
    ):
        return False
    local_lines = present - continuing_set
    originating_by_station = {
        station_id: (
            ctx.outbound.get(station_id, set()) - ctx.inbound.get(station_id, set())
        )
        & local_lines
        for station_id in carrying
    }
    hubs = [station_id for station_id, lines in originating_by_station.items() if lines]
    if len(hubs) != 1:
        return False
    hub = hubs[0]
    originating = originating_by_station[hub]
    if originating != local_lines:
        return False
    along_y = lanes_run_along_y(section.direction)
    hub_station = graph.stations[hub]
    hub_perp = hub_station.y if along_y else hub_station.x
    for line_id in originating:
        consumer = _sole_in_section_consumer(graph, hub, section.id, (line_id,))
        if consumer is None:
            return False
        consumer_station = graph.stations[consumer]
        consumer_perp = consumer_station.y if along_y else consumer_station.x
        if abs(consumer_perp - hub_perp) > COORD_TOLERANCE_FINE:
            return False
    return True


def _entry_seam_is_flat(graph: MetroGraph, entry_port_id: str) -> bool:
    """Whether every feeder meets *entry_port_id* level with it.

    A flat seam draws a lane mismatch between the feeder and the entry as a
    horizontal jog, so inheriting the feeder lane lands the run level.  A
    corridor feeder (off the port's Y) instead absorbs the lane step in its
    vertical leg, and the trunk-anchoring invariant then requires a lone
    consumer on offset 0 (:func:`iter_corridor_fed_solo_entries`); inheriting
    the upstream lane there would only reserve empty lanes.  A same-row bypass
    (:func:`_rises_from_same_row_bypass`) is such a corridor too, though its
    source shares the port's Y.

    A merge junction fronting the port stands level with it whatever its own
    feeders do, so the seam is read from those feeders instead.
    """
    merges = merge_junction_ids(graph)
    port_station = graph.stations[entry_port_id]
    feeders = [
        graph.stations[feeder.source]
        for edge in graph.edges_to(entry_port_id)
        for feeder in (
            graph.edges_to(edge.source) if edge.source in merges else (edge,)
        )
        if feeder.source in graph.stations
    ]
    if any(
        _rises_from_same_row_bypass(graph, source, port_station) for source in feeders
    ):
        return False
    dys = [source.y - port_station.y for source in feeders]
    return seam_is_flat(dys, _SAME_Y_TOLERANCE)


def _carrier_offset_gap(
    graph: MetroGraph,
    station_id: str,
    values: Mapping[str, float],
    offset_step: float,
) -> float | None:
    """The interior lane gap *station_id*'s ridden lanes leave, or ``None``.

    A carrier reserves the span from its lowest to its highest ridden lane; a
    level in that span no line of *values* occupies strands the routes that join
    the carrier off its markers.  Only lines present in *values* count.
    """
    levels = distinct_offset_levels(
        values[line_id]
        for line_id in graph.station_lines(station_id)
        if line_id in values
    )
    return max_interior_offset_gap(levels, offset_step)


def _frame_carriers_are_conflict_free(
    graph: MetroGraph,
    carrier_ids: Sequence[str],
    assignments: Mapping[str, float],
    offset_step: float,
) -> bool:
    """Whether *assignments* leave every carrier ridden on a distinct lane.

    Two facets of the frame's ownership contract, checked before it is planned:
    no two lines of one carrier land on the same lane (the collinearity
    invariant), and no carrier's marker spans an interior lane no line rides
    (the reservation invariant).
    """
    for station_id in carrier_ids:
        lanes = {
            line_id: round(assignments[line_id], 1)
            for line_id in graph.station_lines(station_id)
            if line_id in assignments
        }
        if len(set(lanes.values())) != len(lanes):
            return False
        if _carrier_offset_gap(graph, station_id, lanes, offset_step) is not None:
            return False
    return True


def _entry_cohort_has_unresolved_jog(
    entry_port_id: str,
    inherited: Mapping[str, float],
    snapshot: Mapping[tuple[str, str], float] | None,
) -> bool:
    """Whether a boundary jog remains for a frame to resolve.

    *snapshot* holds the reactive offsets frozen before the fixed point, so the
    answer is stable across its passes.  When the reactive path already leveled
    the cohort on its inherited lane there is no jog to remove, and rebasing
    would only churn the section's other lanes; only a real step earns the
    rebase.  When no snapshot is threaded in, the check is skipped and every
    cohort is treated as jogging.
    """
    if snapshot is None:
        return True
    return any(
        abs(snapshot.get((entry_port_id, line_id), 0.0) - lane) > _OFFSET_EQ_TOLERANCE
        for line_id, lane in inherited.items()
    )


def _linear_entry_frame(
    ctx: _OffsetCtx,
    section: Section,
    snapshot: Mapping[tuple[str, str], float] | None = None,
) -> _LinearEntryFrame | None:
    """Plan a complete section frame from one flow-aligned entry cohort.

    A multi-line cohort that passes through to a flow-axis exit inherits its
    feeder's exact lanes so the boundary carries no jog.  A single-line or
    terminating cohort inherits them only across a flat seam the reactive path
    left stepping, where the mismatch draws as an offset-sized diagonal; a
    corridor-fed or reflected entry, one whose adopted lanes would collide or
    strand a reserved lane, or one already level on its feeder lane, defers to
    the reactive trunk-anchoring path instead.
    """
    graph = ctx.graph
    flow_entry, flow_exit = flow_port_sides(section.direction)
    entries = [
        port_id
        for port_id in section.entry_ports
        if graph.ports[port_id].side is flow_entry
        and len(graph.station_lines(port_id)) >= 1
    ]
    if len(entries) != 1:
        return None
    entry_port_id = entries[0]
    if is_near_vertical_junction_right_entry(graph, graph.ports[entry_port_id]):
        return None
    continuing = tuple(graph.station_lines(entry_port_id))
    if _sole_in_section_consumer(graph, entry_port_id, section.id, continuing) is None:
        return None
    supplied = {
        line_id: _upstream_section_lane(
            ctx, entry_port_id, section.id, line_id, flow_exit
        )
        for line_id in continuing
    }
    if any(owner is None for owner in supplied.values()):
        return None
    owners = {(owner[0], owner[1]) for owner in supplied.values() if owner is not None}
    if len(owners) != 1:
        return None
    inherited = {
        line_id: owner[2] for line_id, owner in supplied.items() if owner is not None
    }
    levels = distinct_offset_levels(inherited.values())
    if (
        len(levels) != len(continuing)
        or max_interior_offset_gap(levels, ctx.offset_step) is not None
    ):
        return None

    real_station_ids = tuple(
        station_id
        for station_id in section.station_ids
        if not graph.stations[station_id].is_port
        and not graph.stations[station_id].is_hidden
        and not graph.stations[station_id].off_track
    )
    continuing_set = set(continuing)
    carrying = [
        station_id
        for station_id in real_station_ids
        if continuing_set.issubset(graph.station_lines(station_id))
    ]
    if not carrying:
        return None
    carrying_set = set(carrying)
    non_carrying = [
        station_id for station_id in real_station_ids if station_id not in carrying_set
    ]
    present = _section_present_line_set(ctx, section.id)
    if any(line_id not in ctx.line_priority for line_id in present):
        return None
    if non_carrying and not _is_flat_handover_hub(
        ctx, section, continuing_set, present, carrying, non_carrying
    ):
        return None
    flow_exit_lines = {
        line_id
        for port_id in section.exit_ports
        if graph.ports[port_id].side is flow_port_sides(section.direction)[1]
        for line_id in graph.station_lines(port_id)
    }
    if (
        flow_exit_lines
        and (continuing_set & flow_exit_lines)
        and not continuing_set.issubset(flow_exit_lines)
    ):
        return None

    priority_order = tuple(sorted(present, key=ctx.line_priority.__getitem__))
    determining = tuple(sorted(continuing, key=inherited.__getitem__))
    arranged = lane_order(
        BoundaryConfig(present=priority_order, determining=determining),
        ctx.line_priority,
    )
    ordered = arranged if arranged is not None else priority_order
    local = [line_id for line_id in ordered if line_id not in continuing_set]
    first_priority = min(ctx.line_priority[line_id] for line_id in continuing)
    before = [
        line_id for line_id in local if ctx.line_priority[line_id] < first_priority
    ]
    available_below = max(0, round(min(levels) / ctx.offset_step))
    # A hub hand-over's post-hub stations carry only the local bundle, so it must
    # sit as one contiguous block above the cohort rather than straddle it.
    below = before[-available_below:] if available_below and not non_carrying else []
    after = [line_id for line_id in local if line_id not in below]

    assignments = dict(inherited)
    base = min(levels)
    for rank, line_id in enumerate(reversed(below), start=1):
        assignments[line_id] = base - rank * ctx.offset_step
    band_end = max(levels)
    for rank, line_id in enumerate(after, start=1):
        assignments[line_id] = band_end + rank * ctx.offset_step

    carrier_ids = tuple(
        station_id
        for station_id in section.station_ids
        if set(graph.station_lines(station_id)) & set(assignments)
        and (
            not graph.stations[station_id].is_port
            or graph.ports[station_id].side in (flow_entry, flow_exit)
        )
    )
    for station_id in carrier_ids:
        for line_id in graph.station_lines(station_id):
            if line_id not in assignments:
                continue
            owned = ctx.fan_owned_offsets.get((station_id, line_id))
            if (
                owned is not None
                and abs(owned - assignments[line_id]) > _OFFSET_EQ_TOLERANCE
            ):
                return None

    if len(continuing) < 2 or not continuing_set.issubset(flow_exit_lines):
        if not _entry_seam_is_flat(graph, entry_port_id):
            return None
        if _stores_reflected(ctx, section.id):
            return None
        if not _frame_carriers_are_conflict_free(
            graph, carrier_ids, assignments, ctx.offset_step
        ):
            return None
        if not _entry_cohort_has_unresolved_jog(entry_port_id, inherited, snapshot):
            return None

    feeder_section_id, feeder_station_id = next(iter(owners))
    return _LinearEntryFrame(
        section_id=section.id,
        entry_port_id=entry_port_id,
        feeder_section_id=feeder_section_id,
        feeder_station_id=feeder_station_id,
        continuing=tuple((line_id, inherited[line_id]) for line_id in determining),
        assignments=tuple(assignments.items()),
        carrier_ids=carrier_ids,
    )


def _materialize_linear_entry_frames(ctx: _OffsetCtx) -> tuple[_LinearEntryFrame, ...]:
    """Settle entry-owned section frames as one fixed-point transaction."""
    if ctx.compact:
        return ()
    original = dict(ctx.offsets)
    frames: dict[str, _LinearEntryFrame] = {}
    for _iteration in range(len(ctx.graph.sections) + 1):
        changed = False
        next_frames: dict[str, _LinearEntryFrame] = {}
        for section in ctx.graph.sections.values():
            frame = _linear_entry_frame(ctx, section, snapshot=original)
            if frame is None:
                continue
            next_frames[section.id] = frame
            assignments = dict(frame.assignments)
            for station_id in frame.carrier_ids:
                for line_id in ctx.graph.station_lines(station_id):
                    if line_id not in assignments:
                        continue
                    key = station_id, line_id
                    value = assignments[line_id]
                    if abs(ctx.offsets.get(key, value) - value) > _OFFSET_EQ_TOLERANCE:
                        changed = True
                    ctx.offsets[key] = value
        if set(frames).difference(next_frames):
            ctx.offsets.clear()
            ctx.offsets.update(original)
            return ()
        frames = next_frames
        if not changed:
            return tuple(frames.values())
    ctx.offsets.clear()
    ctx.offsets.update(original)
    return ()


def _cache_linear_entry_pill_lines(
    ctx: _OffsetCtx,
    frames: tuple[_LinearEntryFrame, ...],
) -> None:
    """Publish marker spans for accepted entry-frame carriers."""
    cache = ctx.graph._linear_entry_pill_lines_cache
    for frame in frames:
        continuing = tuple(line_id for line_id, _offset in frame.continuing)
        # The end-cap only redundantly covers the lane one step beyond a
        # narrowed span when the inherited cohort itself spans >=2 lanes; a
        # single-line cohort's neighbour is a genuine bundle member, so
        # narrowing would drop it from the marker.
        if len(continuing) < 2:
            continue
        continuing_set = set(continuing)
        for station_id in frame.carrier_ids:
            station = ctx.graph.stations[station_id]
            if station.is_port or station.is_hidden or station.off_track:
                continue
            served = tuple(ctx.graph.station_lines(station_id))
            local = tuple(
                line_id for line_id in served if line_id not in continuing_set
            )
            if len(local) != 1 or not continuing_set.issubset(served):
                continue
            inherited_offsets = [
                ctx.offsets[station_id, line_id] for line_id in continuing
            ]
            ordered = sorted(inherited_offsets)
            if any(
                abs(right - left - ctx.offset_step) > COORD_TOLERANCE_FINE
                for left, right in zip(ordered, ordered[1:])
            ):
                continue
            local_offset = ctx.offsets.get((station_id, local[0]))
            if local_offset is None or not (
                abs(local_offset - (ordered[0] - ctx.offset_step))
                <= COORD_TOLERANCE_FINE
                or abs(local_offset - (ordered[-1] + ctx.offset_step))
                <= COORD_TOLERANCE_FINE
            ):
                continue
            cache[station_id] = continuing


def _validate_linear_entry_frames(
    ctx: _OffsetCtx,
    frames: tuple[_LinearEntryFrame, ...],
) -> None:
    """Certify exact ownership and contiguous carriers for settled frames."""
    graph = ctx.graph
    for frame in frames:
        assignments = dict(frame.assignments)
        continuing = dict(frame.continuing)
        for station_id in frame.carrier_ids:
            lines = set(graph.station_lines(station_id))
            for line_id, expected in continuing.items():
                if line_id not in lines:
                    continue
                actual = ctx.offsets.get((station_id, line_id), 0.0)
                if abs(actual - expected) > _OFFSET_EQ_TOLERANCE:
                    raise LaneFrameInvariantError(
                        f"section {frame.section_id!r} carrier {station_id!r} "
                        f"moves continuing line {line_id!r} from {expected} to {actual}"
                    )
            ridden = {
                line_id: ctx.offsets[(station_id, line_id)]
                for line_id in lines
                if line_id in assignments and (station_id, line_id) in ctx.offsets
            }
            gap = _carrier_offset_gap(graph, station_id, ridden, ctx.offset_step)
            if gap is not None:
                raise LaneFrameInvariantError(
                    f"section {frame.section_id!r} carrier {station_id!r} "
                    f"has an empty lane gap of {gap}"
                )


def capture_linear_entry_frame_ownership(
    graph: MetroGraph,
    station_offsets: Mapping[tuple[str, str], float],
    offset_step: float | None = None,
) -> LinearEntryFrameOwnership:
    """Freeze every fully materialized entry frame for downstream routing."""
    if graph.line_spread is LineSpread.RAILS or graph.compact_offsets:
        return LinearEntryFrameOwnership(())
    resolved = offset_step if offset_step is not None else graph_offset_step(graph)
    ctx = _build_offset_ctx(graph, resolved)
    ctx.offsets.update(station_offsets)
    frames: list[_LinearEntryFrame] = []
    owned_assignments: list[LinearEntryFrameAssignment] = []
    for section in graph.sections.values():
        frame = _linear_entry_frame(ctx, section)
        if frame is None:
            continue
        assignments = dict(frame.assignments)
        owned = [
            (station_id, line_id, expected)
            for station_id in frame.carrier_ids
            for line_id in graph.station_lines(station_id)
            if (expected := assignments.get(line_id)) is not None
        ]
        if any(
            (station_id, line_id) not in station_offsets
            or abs(station_offsets[station_id, line_id] - expected)
            > _OFFSET_EQ_TOLERANCE
            for station_id, line_id, expected in owned
        ):
            continue
        frames.append(frame)
        owned_assignments.extend(
            LinearEntryFrameAssignment(frame.section_id, station_id, line_id, expected)
            for station_id, line_id, expected in owned
        )
    frozen_frames = tuple(frames)
    _validate_linear_entry_frames(ctx, frozen_frames)
    return LinearEntryFrameOwnership(tuple(owned_assignments))


def validate_linear_entry_frame_ownership(
    station_offsets: Mapping[tuple[str, str], float],
    ownership: LinearEntryFrameOwnership,
) -> None:
    """Reject downstream changes to a frozen entry-frame assignment."""
    for assignment in ownership.assignments:
        actual = station_offsets.get((assignment.station_id, assignment.line_id))
        if actual is None or abs(actual - assignment.offset) > _OFFSET_EQ_TOLERANCE:
            raise LaneFrameInvariantError(
                f"section {assignment.section_id!r} carrier "
                f"{assignment.station_id!r} moves frame-owned line "
                f"{assignment.line_id!r} from {assignment.offset} to {actual}"
            )


def conflicting_linear_entry_frame_assignments(
    proposed_offsets: Mapping[tuple[str, str], float],
    ownership: LinearEntryFrameOwnership,
) -> set[tuple[str, str]]:
    """Return proposed assignments that disagree with frozen frame ownership."""
    expected_by_key = {
        (assignment.station_id, assignment.line_id): assignment.offset
        for assignment in ownership.assignments
    }
    return {
        key
        for key, proposed in proposed_offsets.items()
        if (expected := expected_by_key.get(key)) is not None
        and abs(proposed - expected) > _OFFSET_EQ_TOLERANCE
    }


def compute_station_offsets(
    graph: MetroGraph,
    offset_step: float | None = None,
) -> dict[tuple[str, str], float]:
    """Compute per-station Y offsets for each line.

    Each line gets a globally consistent offset based on its declaration
    order (priority). This ensures lines maintain their position within
    bundles across all sections - when a line splits off and later
    rejoins, it returns to its reserved slot rather than shifting.

    Runs in ordered phases, one per call in the body below.  Each phase's
    own docstring carries its conditions; the notes here are the ordering
    constraints that are not visible from a single phase.  The labels are
    identifiers rather than positions -- ``CONTRACT.md`` documents 14b and 14c
    under those names, and there is no phase 11.

    1. **Base offsets** - global priority (or compact-mode) assignment.
    2. **Section-local re-indexing** - closes priority gaps within
       sections and applies reconvergence ordering (non-compact only).
    2b. **Exit-only line reordering** - a line that originates at a
       multi-line station and exits to a port above takes the top slot,
       so it crosses nothing on the way out (non-compact LR/RL).
    2c. **Fan-out divergence ordering** - re-slots a source section's
       bundle into the peel order of the shared exit fan it leaves
       through, so the descent is crossing-free.  Runs before the exit
       and junction ports inherit their offsets (non-compact LR/RL).
    3. **Compact section consistency** - ensures entry lines have
       consistent offsets across multi-line stations (compact only).
    4. **Station gap compaction** - closes per-station offset gaps
       where intermediate lines are absent, propagating along same-Y
       edges with conservative safety checks (non-compact only).
    5. **Exit port offsets** - TB reversed offsets and LR/RL spatial
       Y ordering with hub propagation.
    6. **Junction inheritance** - copies exit port offsets to junctions.
    7. **Entry port offsets** - TOP entry override for TB BOTTOM exits,
       straight-drop TOP entry column/trunk nesting, LR/RL exit-to-entry
       propagation, compact entry separation.
    7a. **Junction-to-entry-port snapping** - where a junction's only
       targets are entry ports at its own base Y, takes their offsets so
       the short leg between them stays horizontal.  Needs phase 7's
       entry-port offsets.
    7b. **Merge-port approach-side allocation** - at multi-feeder LR/RL
       entry ports, re-slots a perpendicular re-joining line to the
       bundle slot nearest its approach side (non-compact only).
    7c. **Convergence entry-port ordering** - at a LEFT entry port fed by
       a bypass trunk from two or more source columns, slots the bundle by
       approach depth so its risers turn in concentrically; at any LR/RL
       flow-side entry where same-row bypass risers join flat feeders, seats
       the risers beside the flat lanes (non-compact).
    7d. **Convergence approach-Y ordering** - at a LEFT entry port fed
       from sections at different rows, slots the bundle by feeder source Y
       so a feeder above the sink is not run down across its mates into a
       bottom lane (compact only).
    7e. **Top-descent lane ordering** - the non-compact counterpart of 7d:
       at a LEFT entry port fed both level from its own row and by a line
       descending from a row above, the descending line takes the top lane
       so it does not dive under the level feeder.
    8. **Horizontal reconciliation** - snaps mismatched offsets on
       same-Y edges to eliminate almost-horizontal slopes.
    8b. **Flat TB-exit/entry alignment** - on an auto-folded return row,
       snaps a TB section's flat-seam LEFT/RIGHT exit bundle onto the LR/RL
       entry it feeds, so the connector between them runs level.
    9. **Partial fan-branch re-centring** - collapses absent-line slots at
       independent fan branches so a partial-line station's marker has no
       interior gap (compact only).
    10. **Near-vertical junction reversal** - a fan-out junction overhanging
       a same-column RIGHT entry one row below transposes the bundle as it
       drops, so that section and its DAG descendants carry the reversed
       line order.
    12. **Fan-port-bordering re-compaction** - the port phases (5-7) can
       reopen a station gap phase 4 already closed against that port, so
       recheck the stations the fan-port guard flags and any port left
       non-contiguous the same way (non-compact only).
    12a. **Fan-out peel-order restoration** - re-seats a divergence exit
        port's bundle on its semantic peel order, which the port and
        compaction phases above can permute (non-compact, lane-stacked
        sections).
    13. **Final horizontal re-reconciliation** - phase 12 can move a port
       that phase 8 already snapped a same-Y station onto, so phase 8 runs
       again to catch the stale value (non-compact only).
    14. **Rail-boundary port centring** - rigidly shifts the bundle at a
       rail-laid section's boundary port so its lanes straddle the port, the
       middle of the fan out to that section's rails.  Runs after phase 13,
       whose snapping would pull the port back onto the rail-laid
       neighbour's offsets.
    14a. **Corridor-fed solo entry re-anchoring** - a single-line LR/RL
        section holds the lane its line rode upstream, which its own trunk
        does not use; re-anchor the entry port (and a straight consumer
        chain behind it) to offset 0 where the step can resolve in a
        vertical corridor, or where a flat feeder already rides the trunk.
    14b. **Entry-arrival lane slotting** - exchanges an arriving line's slot
        for the one holding the lane it actually arrives on, so the run from a
        flow-side entry port into its first station stays flat rather than
        spending the difference as a markerless diagonal just inside the box.
        Runs after phase 13 because the upstream lane it reads is only final
        once the port phases have settled (non-compact, lane-stacked
        sections).
    14c. **Section free-lane closure** - drops any lane level no line of a
        section rides, shifting the levels above it down together, so no
        marker spans a slot held for nobody.  Runs after 14b, the last phase
        that can vacate a level (non-compact, non-rail sections).
    14d. **Planned fan offsets** - writes each geometry-owning fan plan's
        complete lane assignment, which has the last word on the lanes it
        states.
    15. **Linear entry-frame materialization** - inherits a flow-aligned entry
        cohort's exact upstream slots across every complete section carrier,
        assigns section-local lines only to exterior slots, and publishes the
        frame only once its ownership reaches a fixed point (non-compact only).

    Returns dict mapping (station_id, line_id) -> y_offset.
    """
    graph._linear_entry_pill_lines_cache.clear()
    # Rail mode bakes absolute rail Ys into the route points and the pill
    # span, so per-line offsets are not used; return an empty map.
    if graph.line_spread is LineSpread.RAILS:
        return {}

    resolved = offset_step if offset_step is not None else graph_offset_step(graph)
    ctx = _build_offset_ctx(graph, resolved)
    # Section/layer indexes over graph structure, not per-line offsets, so
    # they stay valid across every phase below and are built once here for
    # both compaction passes (phase 4 and phase 12) to share.
    same_y_adj = _build_same_y_adj(graph)
    sec_layer_stations = _build_sec_layer_stations(graph)
    _compute_base_offsets(ctx)
    _reindex_section_local(ctx)
    _assert_sections_anchored_on_trunk(ctx)
    _reorder_exit_only_lines(ctx)
    _reorder_fanout_divergence(ctx)
    _apply_compact_section_consistency(ctx)
    _compact_station_gaps(ctx, same_y_adj, sec_layer_stations)
    _compute_exit_port_offsets(ctx)
    _propagate_to_junctions(ctx)
    _compute_entry_port_offsets(ctx)
    _align_junction_to_entry_port(ctx)
    _allocate_merge_ports_by_approach(ctx)
    _order_convergence_entry_ports(ctx)
    _order_convergence_by_approach(ctx)
    _order_top_descent_over_left_entry(ctx)
    _reconcile_horizontal_offsets(ctx)
    _align_flat_tb_exit_to_entry(ctx)
    _recenter_partial_fan_branches(ctx)
    _reverse_near_vertical_junction_right_entry_offsets(ctx)
    _recompact_fan_port_bordering_stations(ctx, same_y_adj, sec_layer_stations)
    _restore_fanout_peel_order(ctx, same_y_adj)
    _reconcile_horizontal_offsets(ctx)
    _center_rail_boundary_port_bundles(ctx)
    _recenter_single_line_corridor_entry(ctx)
    _slot_entry_arrivals_on_their_approach_lane(ctx, same_y_adj)
    _close_section_dead_lanes(ctx)
    _apply_planned_fan_offsets(ctx)
    frames = _materialize_linear_entry_frames(ctx)
    _validate_linear_entry_frames(ctx, frames)
    _cache_linear_entry_pill_lines(ctx, frames)
    return ctx.offsets


def _reverse_offsets_from_roots(ctx: _OffsetCtx, roots: set[str]) -> None:
    """Reverse the per-line order of *roots* and their DAG-downstream sections.

    The shared body of the U-turn reversal passes: a section whose feed
    transposes the bundle end-to-end carries the reversed line order, and
    sections downstream inherit it so their feed stays aligned.  Reversal is
    :func:`reversed_offset` per station, an involution, so stations with equal
    offsets stay equal -- propagated port/trunk equalities are preserved.

    A divergence junction belongs to no section, so walking the stations of the
    affected sections passes it by and leaves it holding the order the exit port
    feeding it has just come off.  It takes that port's lanes again, the seat
    :func:`_propagate_to_junctions` gives it; without that the reversal lands as
    a transposition over the few pixels between the port and the junction, and
    every branch leaves the vertex on the opposite side of the bundle from the
    feed that arrived on it.
    """
    if not roots:
        return

    affected = set(roots)
    dag = ctx.graph.section_dag
    if dag is not None:
        stack = list(roots)
        while stack:
            for succ in dag.successors.get(stack.pop(), ()):
                if succ not in affected:
                    affected.add(succ)
                    stack.append(succ)

    for sid, station in ctx.graph.stations.items():
        if station.section_id not in affected:
            continue
        lines = ctx.graph.station_lines(sid)
        offs = [ctx.offsets.get((sid, lid), 0.0) for lid in lines]
        if not offs:
            continue
        max_off = max(offs)
        for lid in lines:
            ctx.offsets[(sid, lid)] = reversed_offset(
                ctx.offsets.get((sid, lid), 0.0), max_off
            )

    for junction_id, exit_port_id in ctx.divergence_exit_ports.items():
        if ctx.graph.ports[exit_port_id].section_id in affected:
            _copy_exit_lanes_to_junction(ctx, junction_id, exit_port_id)


def _reverse_near_vertical_junction_right_entry_offsets(ctx: _OffsetCtx) -> None:
    """Reverse the line order of sections a fan-out junction drops into.

    A fan-out junction overhanging a same-column RIGHT entry one row below drops
    down the port's outward side and turns once into it (the standard
    ``_route_right_entry_cross_row`` path).  That descent transposes the bundle
    into the port's lateral order, so the section receives its lines in the
    opposite order to the junction; it carries the reversed order so the drop and
    the run out of the port stay straight and the turn nests concentrically.

    Whether the drop transposes turns on the junction's pixel overhang, not on
    port sides or grid rows, so the seam-orientation classifier cannot derive it
    coordinate-free; this pass stays as a coordinate-aware residual.
    """
    graph = ctx.graph
    _reverse_offsets_from_roots(
        ctx,
        {
            port.section_id
            for port in graph.ports.values()
            if is_near_vertical_junction_right_entry(graph, port)
        },
    )
