"""Shared leaf helpers used across layout phases (bbox math, section queries)."""

from __future__ import annotations

import heapq
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING

from nf_metro.layout.constants import (
    BOUNDARY_CROSSING_INSET,
    COORD_GROUP_DIGITS_COARSE,
    COORD_GROUP_DIGITS_FINE,
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    MIN_BUNDLE_EDGE_CLEARANCE,
    PERP_PORT_EDGE_INSET,
    PORT_BOUNDARY_CROSSING_TOL,
    SAME_COORD_TOLERANCE,
    SECTION_Y_PADDING,
    STATION_RADIUS_APPROX,
    graph_offset_step,
)
from nf_metro.layout.geometry import (
    AxisFrame,
    lanes_run_along_x,
    lanes_run_along_y,
    quantize_coord,
)
from nf_metro.layout.phase_state import require_phase_field
from nf_metro.layout.route_topology import divergence_junction_sources
from nf_metro.parser.model import (
    FLOW_DIRECTIONS,
    Edge,
    LineSpread,
    MetroGraph,
    Port,
    PortSide,
    Section,
    Station,
    is_bypass_v,
)

if TYPE_CHECKING:
    from nf_metro.layout.routing.common import RoutedPath


def dominant_value(values: Iterable[float]) -> float:
    """The value the most items share, ties broken toward the smallest.

    Returns the modal value; when two values tie on count the smaller wins.
    Callers that need a tolerance (coordinates that should cluster within a
    pitch) pre-round the values before passing them.
    """
    counts = Counter(values)
    return min(counts, key=lambda v: (-counts[v], v))


def perp_entry_lands_left(section: Section, graph: MetroGraph) -> bool:
    """Which side of the internal trunk a perpendicular entry drop lands on.

    A TOP/BOTTOM entry into an LR/RL section drops in beside the trunk, then
    runs horizontally to the trunk and out the flow-axis exit.  If the drop
    lands on the *same* side as the exit, that run and the exit leg cover the
    same track in opposing directions -- the line folds back over itself.  So
    the drop must land on the side opposite the flow-axis exit: LEFT-exit ->
    drop on the right, RIGHT-exit -> drop on the left.

    With no single LEFT/RIGHT exit to key off, falls back to the flow-natural
    side (LR enters left, RL enters right); an exit on that natural side then
    also resolves to the natural side.
    """
    exit_sides = {graph.ports[pid].side for pid in flow_axis_exit_ports(section, graph)}
    if exit_sides == {PortSide.LEFT}:
        return False
    if exit_sides == {PortSide.RIGHT}:
        return True
    return section.direction == "LR"


def iter_sole_trunk_continuations(
    graph: MetroGraph,
) -> Iterator[tuple[str, str, str]]:
    """Yield the full-graph-proven continuation relation, with sections.

    Carries every exclusion :func:`continuation_track_predecessors` applies, so
    a vertical (TB/BT) section and a file-icon station yield nothing here
    either. A test that needs those chains has to derive them from the
    sections' own edges rather than from this iterator.
    """
    for node, predecessor in continuation_track_predecessors(graph).items():
        section_id = graph.stations[node].section_id
        assert section_id is not None
        yield section_id, predecessor, node


def continuation_track_is_realizable(
    graph: MetroGraph, node: str, predecessor: str
) -> bool:
    """Whether a continuation can occupy its predecessor's track at its layer."""
    station = graph.stations[node]
    predecessor_station = graph.stations[predecessor]
    section_id = station.section_id
    if section_id is None or predecessor_station.section_id != section_id:
        return False
    target = predecessor_station.y
    return not any(
        other_id not in {node, predecessor}
        and other.section_id == section_id
        and other.layer == station.layer
        and not other.is_port
        and not other.is_hidden
        and abs(other.y - target) < SAME_COORD_TOLERANCE
        for other_id, other in graph.stations.items()
    )


def _topological_station_order(
    station_ids: set[str],
    targets: Mapping[str, set[str]],
    predecessors: Mapping[str, set[str]],
) -> list[str] | None:
    """Return a station-id-stable topological order, or None for a cycle."""
    indegree = {station_id: len(predecessors[station_id]) for station_id in station_ids}
    frontier = [station_id for station_id, degree in indegree.items() if degree == 0]
    heapq.heapify(frontier)
    topological: list[str] = []
    while frontier:
        station_id = heapq.heappop(frontier)
        topological.append(station_id)
        for target in sorted(targets[station_id]):
            indegree[target] -= 1
            if indegree[target] == 0:
                heapq.heappush(frontier, target)
    return topological if len(topological) == len(station_ids) else None


def _line_bypasses_boundary(
    line_id: str,
    predecessor: str,
    node: str,
    line_predecessors: Mapping[str, Mapping[str, set[str]]],
    line_targets: Mapping[str, Mapping[str, set[str]]],
    targets: Mapping[str, set[str]],
) -> bool:
    """Whether one line reaches below a boundary without crossing it.

    Each query holds only its line-local bypass set and one global forward-walk
    set, avoiding an all-pairs descendant index for the station DAG.
    """
    predecessors_for_line = line_predecessors.get(line_id, {})
    targets_for_line = line_targets.get(line_id, {})
    upstream: set[str] = set()
    frontier = list(predecessors_for_line.get(predecessor, set()))
    while frontier:
        station_id = frontier.pop()
        if station_id in upstream:
            continue
        upstream.add(station_id)
        frontier.extend(predecessors_for_line.get(station_id, set()) - upstream)

    bypass_reachable: set[str] = set()
    visited: set[str] = set()
    frontier = list(upstream)
    while frontier:
        station_id = frontier.pop()
        if station_id in visited:
            continue
        visited.add(station_id)
        for target in targets_for_line.get(station_id, set()):
            if target not in {predecessor, node}:
                bypass_reachable.add(target)
                frontier.append(target)

    if not bypass_reachable:
        return False
    visited.clear()
    frontier = list(targets[node])
    while frontier:
        station_id = frontier.pop()
        if station_id in bypass_reachable:
            return True
        if station_id in visited:
            continue
        visited.add(station_id)
        frontier.extend(targets[station_id] - visited)
    return False


def _leads_to_flow_side_entry(
    graph: MetroGraph,
    line_targets: Mapping[str, Mapping[str, set[str]]],
    line_id: str,
    exit_port_id: str,
) -> bool:
    """Whether *line_id* reaches an entry port on a vertical section boundary.

    Such an entry proves the line continues past the section it is leaving,
    rather than ending at the exit port itself.
    """
    return any(
        successor in graph.ports
        and graph.ports[successor].is_entry
        and graph.ports[successor].side in {PortSide.LEFT, PortSide.RIGHT}
        for successor in line_targets.get(line_id, {}).get(exit_port_id, ())
    )


def _shared_y_lane_section(
    graph: MetroGraph, predecessor: str, node: str
) -> str | None:
    """The one Y-lane section a sole predecessor->node link stays inside.

    A shared secondary track exists only where the section stacks its lines
    along Y.  One that lanes along X separates its lines on the flow's own
    cross axis, so there is no common Y track to inherit.
    """
    section_id = graph.stations[predecessor].section_id
    if (
        section_id is None
        or graph.stations[node].section_id != section_id
        or lanes_run_along_x(graph.sections[section_id].direction)
    ):
        return None
    return section_id


def continuation_track_predecessors(graph: MetroGraph) -> dict[str, str]:
    """Return horizontal track inheritance proven against the complete graph.

    Seeds cross a safe line-membership transition. Equal-line closure then
    extends only through one-in/one-out visible chains. Ports and hidden nodes
    remain in the adjacency and reachability facts, so an external branch or a
    hidden merge prevents inheritance instead of disappearing from the proof.

    Scoped to horizontal (LR/RL) sections: a vertical (TB/BT) section
    contributes no relation at all. It needs none -- a vertical sole successor
    already settles on its predecessor's lane column unaided -- and admitting
    one re-seats bundle geometry this relation has no business moving (it
    re-seats ``seed_41`` and gains it a defect class). Both halves of that claim
    are locked outside this function, against the sections' own edges rather
    than against the answer here: ``tests/test_continuation_tracks.py`` for the
    two named fixtures, and
    ``test_vertical_passthrough_chain_holds_one_lane_column`` in
    ``tests/test_layout_invariants.py`` across the corpus.

    A file-icon (blank-terminus) station is also outside the proof. It is drawn
    as an icon at the line convergence rather than a labelled pill and the
    router seats it against the producer it hangs off, so naming it here would
    re-assert a placement this relation does not own.
    ``test_file_icon_sole_continuation_holds_its_producer_track`` asserts the
    shared track for those chains directly.
    """
    inherited: dict[str, str] = {}
    visible = {
        station_id
        for station_id, station in graph.stations.items()
        if not station.is_port
        and not station.is_hidden
        and not station.off_track
        and not station.is_blank_terminus
        and not station.terminus_icon_types
    }
    station_ids = set(graph.stations)
    targets: dict[str, set[str]] = {station_id: set() for station_id in station_ids}
    predecessors: dict[str, set[str]] = {
        station_id: set() for station_id in station_ids
    }
    line_memberships: dict[str, set[str]] = {
        station_id: set() for station_id in station_ids
    }
    connecting_lines: dict[tuple[str, str], set[str]] = defaultdict(set)
    line_predecessors: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    line_targets: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for edge in graph.edges:
        if edge.source not in station_ids or edge.target not in station_ids:
            continue
        targets[edge.source].add(edge.target)
        predecessors[edge.target].add(edge.source)
        line_memberships[edge.source].add(edge.line_id)
        line_memberships[edge.target].add(edge.line_id)
        connecting_lines[edge.source, edge.target].add(edge.line_id)
        line_predecessors[edge.line_id][edge.target].add(edge.source)
        line_targets[edge.line_id][edge.source].add(edge.target)

    topological = _topological_station_order(station_ids, targets, predecessors)
    if topological is None:
        return inherited

    for node in topological:
        if node not in visible:
            continue
        node_predecessors = predecessors[node]
        non_port_predecessors = {p for p in node_predecessors if p not in graph.ports}
        if len(non_port_predecessors) != 1:
            continue
        predecessor = next(iter(non_port_predecessors))
        port_predecessors = node_predecessors - non_port_predecessors
        # A node fed by a section-boundary port alongside its sole internal
        # predecessor inherits that predecessor's track when the same port also
        # feeds the predecessor: they share one boundary source, so the port
        # imposes no independent Y constraint.  Confined to authored-grid
        # layouts, where the inter-section port resnap re-anchors the shifted
        # carrier's port; auto-layout freezes ports for routing stability, so a
        # lifted continuation there would strand its port off the station.  A
        # port sitting on the predecessor's own track is excluded: it feeds the
        # node along that track straight through the predecessor (which lies
        # between them), which would bow around the predecessor rather than run
        # flat, so the track cannot be inherited.
        if port_predecessors:
            shares_port_with_predecessor = not (
                port_predecessors - predecessors[predecessor]
            )
            predecessor_y = graph.stations[predecessor].y
            port_off_predecessor_track = all(
                abs(graph.stations[p].y - predecessor_y) >= SAME_COORD_TOLERANCE
                for p in port_predecessors
            )
            if not (
                graph.layout_provenance.has_authored_grids()
                and shares_port_with_predecessor
                and port_off_predecessor_track
            ):
                continue
        if predecessor not in visible:
            continue
        predecessor_station = graph.stations[predecessor]
        node_station = graph.stations[node]
        shared_section_id = _shared_y_lane_section(graph, predecessor, node)
        if targets[predecessor] != {node} or shared_section_id is None:
            continue
        predecessor_lines = line_memberships[predecessor]
        node_lines = line_memberships[node]
        if predecessor_lines == node_lines:
            next_ids = targets[node]
            if (
                len(predecessors[predecessor]) > 1
                or len(next_ids) > 1
                or any(
                    target not in graph.ports and target not in visible
                    for target in next_ids
                )
            ):
                continue
            inherited[node] = predecessor
            continue
        added_lines = node_lines - predecessor_lines
        dropped_lines = predecessor_lines - node_lines
        if any(
            _line_bypasses_boundary(
                line_id,
                predecessor,
                node,
                line_predecessors,
                line_targets,
                targets,
            )
            for line_id in dropped_lines
        ):
            continue
        if not predecessor_lines > node_lines:
            if predecessor_station.is_blank_terminus or len(targets[node]) > 1:
                continue
            rejoined_lines = (predecessor_lines & node_lines) - connecting_lines[
                predecessor, node
            ]
            flow_exit_ports = flow_axis_exit_ports(
                graph.sections[shared_section_id], graph
            )
            added_continues = (
                any(
                    target in visible
                    and graph.stations[target].section_id == node_station.section_id
                    and bool(connecting_lines[node, target] & added_lines)
                    for target in targets[node]
                )
                or len(added_lines) == 1
                and any(
                    target in flow_exit_ports
                    and any(
                        line_id in connecting_lines[node, target]
                        and _leads_to_flow_side_entry(
                            graph, line_targets, line_id, target
                        )
                        for line_id in added_lines
                    )
                    for target in targets[node]
                )
            )
            if not added_continues and not rejoined_lines:
                continue
        inherited[node] = predecessor

    frontier = list(reversed(inherited))
    while frontier:
        predecessor = frontier.pop()
        successor_ids = targets.get(predecessor, set())
        if len(successor_ids) != 1:
            continue
        node = next(iter(successor_ids))
        if node in inherited or node not in visible:
            continue
        if (
            _shared_y_lane_section(graph, predecessor, node) is None
            or predecessors[node] != {predecessor}
            or targets[predecessor] != {node}
            or line_memberships[predecessor] != line_memberships[node]
        ):
            continue
        inherited[node] = predecessor
        frontier.append(node)
    return inherited


def line_forks_within_section(
    graph: MetroGraph, section: Section, line_id: str
) -> bool:
    """``True`` when *line_id* has an on-track fan inside *section*.

    A fork is a non-port station with more than one in-section, on-line
    successor.  Its branches straddle the trunk rather than following one chain,
    so a corridor-fed solo section that forks re-anchors only its entry port to
    the trunk, leaving the fan to place the branches (the consumers cannot all
    ride offset 0).
    """
    members = set(section.station_ids)
    for sid in section.station_ids:
        st = graph.stations.get(sid)
        if st is None or st.is_port or line_id not in graph.station_lines(sid):
            continue
        on_line_successors = sum(
            1
            for edge in graph.edges_from(sid)
            if edge.target in members
            and not graph.stations[edge.target].is_port
            and line_id in graph.station_lines(edge.target)
        )
        if on_line_successors > 1:
            return True
    return False


def feeder_dys(graph: MetroGraph, port_id: str) -> list[float]:
    """Signed Y distance of each feeder from *port_id*.

    The raw material both the corridor filter (all feeders off the port's Y) and
    the flat-seam filter (all feeders level with it) select on.
    """
    port_y = graph.stations[port_id].y
    return [
        graph.stations[e.source].y - port_y
        for e in graph.edges_to(port_id)
        if e.source in graph.stations
    ]


def seam_is_flat(dys: Sequence[float], tol: float) -> bool:
    """Whether every feeder meets the port level with it, within *tol*.

    A flat seam carries a feeder-to-port lane mismatch as a horizontal slope; a
    corridor feeder (some ``dy`` past *tol*) absorbs it in a vertical leg.
    """
    return bool(dys) and all(abs(dy) <= tol for dy in dys)


def _iter_solo_lr_entries(
    graph: MetroGraph,
) -> Iterator[tuple[str, str, str, list[float]]]:
    """Yield ``(section_id, entry_port_id, line_id, feeder_dys)`` for each
    LEFT/RIGHT entry port of an LR/RL section carrying a single present line.
    """
    present: dict[str, set[str]] = defaultdict(set)
    for sid, st in graph.stations.items():
        if not st.is_port and st.section_id is not None:
            present[st.section_id].update(graph.station_lines(sid))
    for sec_id, sec in graph.sections.items():
        if not lanes_run_along_y(sec.direction):
            continue
        lines = present.get(sec_id, set())
        if len(lines) != 1:
            continue
        line_id = next(iter(lines))
        for pid in sec.entry_ports:
            port = graph.ports.get(pid)
            if port is None or port.side not in (PortSide.LEFT, PortSide.RIGHT):
                continue
            dys = feeder_dys(graph, pid)
            if dys:
                yield sec_id, pid, line_id, dys


def iter_corridor_fed_solo_entries(
    graph: MetroGraph, tol: float
) -> Iterator[tuple[str, str, str]]:
    """Yield ``(section_id, entry_port_id, line_id)`` for corridor-fed solos.

    A LEFT/RIGHT entry port of an LR/RL section (lanes spread along Y) that
    carries a single present line, where every feeder reaches the port on a
    base Y more than ``tol`` away -- a vertical corridor.  Such a section has no
    bundle to keep ordered, so its lone consumer must ride offset 0 rather than
    the lane the line held in the upstream multi-line section: the corridor's
    vertical leg absorbs the lane step with no sloped segment.  A flat (same-Y)
    seam is excluded here; see :func:`iter_flat_seam_solo_entries` for that case.
    """
    for sec_id, pid, line_id, feeder_dys in _iter_solo_lr_entries(graph):
        if all(abs(dy) > tol for dy in feeder_dys):
            yield sec_id, pid, line_id


def iter_flat_seam_solo_entries(
    graph: MetroGraph, tol: float
) -> Iterator[tuple[str, str, str]]:
    """Yield ``(section_id, entry_port_id, line_id)`` for flat-seam solos.

    The same-Y complement of :func:`iter_corridor_fed_solo_entries`: a solo
    LEFT/RIGHT entry whose every feeder arrives level with the port (within
    ``tol``), so the junction-to-port run is horizontal.  Such a run carries the
    lane step as a slope rather than absorbing it in a vertical leg; the caller
    re-bases it to the trunk only when the feeder already rides the trunk, so
    the seam lands flat instead of tilting.
    """
    for sec_id, pid, line_id, dys in _iter_solo_lr_entries(graph):
        if seam_is_flat(dys, tol):
            yield sec_id, pid, line_id


def flow_axis_exit_ports(section: Section, graph: MetroGraph) -> set[str]:
    """Ids of *section*'s exit ports on its flow axis (LEFT/RIGHT).

    For a vertical-flow (TB/BT) section these are the ports a route turns
    sideways to leave through -- the exit corridor -- as opposed to a
    perpendicular TOP/BOTTOM drop.
    """
    return {
        pid
        for pid in section.exit_ports
        if (p := graph.ports.get(pid)) is not None
        and p.side in (PortSide.LEFT, PortSide.RIGHT)
    }


def _is_fold_section(section: Section) -> bool:
    """``True`` for a section the row-fold logic produced.

    A fold either spans more than one grid row or runs its flow vertically
    (TB/BT).  Its exit ports are placed by the fold exit-port path
    (``_align_exit_ports``) rather than the row-level exit passes, which expect
    a single-row horizontal-flow section.
    """
    return section.grid_row_span > 1 or not lanes_run_along_y(section.direction)


def _lr_exit_aligned_target(
    graph: MetroGraph,
    port_id: str,
    exit_section: Section,
    junction_ids: set[str],
) -> Station | None:
    """Return the entry port a LEFT/RIGHT exit aligns its Y to, or ``None``.

    The exit aligns to a directly-connected LEFT/RIGHT entry port lying within
    the exit section's bbox.  A fan-out junction, a perpendicular (cross-axis)
    target port, or a target outside the bbox is not an alignment target.
    """
    bbox_top = exit_section.bbox_y
    bbox_bot = exit_section.bbox_y + exit_section.bbox_h
    for edge in graph.edges_from(port_id):
        tgt = graph.station_for_edge_target(edge)
        if edge.target in junction_ids:
            return None
        if not tgt.is_port:
            continue
        tgt_port_obj = graph.ports.get(tgt.id)
        if tgt_port_obj and tgt_port_obj.side in (PortSide.TOP, PortSide.BOTTOM):
            return None
        if not (bbox_top <= tgt.y <= bbox_bot):
            return None
        return tgt
    return None


def _iter_cross_row_aligned_fold_lr_exits(
    graph: MetroGraph,
) -> Iterator[tuple[str, Section, Station]]:
    """Yield ``(exit_port_id, exit_section, target_entry)`` for a fold's
    cross-row aligned LEFT/RIGHT exits.

    The shared scope of :func:`iter_fold_lr_exits_short_of_target` and
    :func:`iter_fold_lr_exit_straight_runs`: a vertical-flow (TB/BT) fold's
    LEFT/RIGHT exit aligned to a bbox-contained entry target in a different grid
    row (the fold relocated it, so its multi-sub-row entry can settle away from
    the exit).  Each consumer applies the final predicate that distinguishes a
    straight run from a staircase, so the two cannot drift on scope.
    """
    junction_ids = graph.junction_ids
    for port_id, port in graph.ports.items():
        if port.is_entry or port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        section = graph.sections.get(port.section_id)
        if (
            section is None
            or not _is_fold_section(section)
            or not lanes_run_along_x(section.direction)
        ):
            continue
        tgt = _lr_exit_aligned_target(graph, port_id, section, junction_ids)
        if tgt is None:
            continue
        tgt_section = graph.sections.get(tgt.section_id) if tgt.section_id else None
        if tgt_section is None or tgt_section.grid_row == section.grid_row:
            continue
        yield port_id, section, tgt


def iter_fold_lr_exits_short_of_target(
    graph: MetroGraph, tolerance: float
) -> Iterator[tuple[str, Station]]:
    """Yield ``(exit_port_id, target_entry)`` for fold exits short of their target.

    A cross-row aligned LEFT/RIGHT fold exit is yielded when its target is
    seated *along the flow* from the exit by more than ``tolerance`` -- meaning
    the exit must follow it to that Y for a straight inter-section run.  A target
    seated against the flow (keeping its own descent, an intentional staircase)
    is not yielded.

    The single source of "which fold exit is short of its target" shared by the
    re-alignment that fixes it (:func:`_realign_fold_lr_exit_ports`), the guard
    that flags it (``_guard_fold_lr_exit_follows_target``), and the layout
    invariant test -- so the three cannot drift on scope or predicate.
    """
    for port_id, section, tgt in _iter_cross_row_aligned_fold_lr_exits(graph):
        flow = AxisFrame.flow_sign(section.direction)
        if flow * (tgt.y - graph.stations[port_id].y) > tolerance:
            yield port_id, tgt


def iter_fold_lr_exit_straight_runs(
    graph: MetroGraph, tolerance: float
) -> Iterator[tuple[str, Station]]:
    """Yield ``(exit_port_id, target_entry)`` for straight folded LR/RL runs.

    The companion of :func:`iter_fold_lr_exits_short_of_target`: the same
    cross-row aligned fold exits, but yielding the runs whose exit sits *at* its
    target entry Y -- the inter-section run is straight.  A target seated off the
    exit Y (the staircase case the sibling generator covers) is excluded.

    The single source of "which folded LR/RL run is straight" shared by the
    bbox-bottom alignment (:func:`_align_tb_section_bbox_bottoms`) and the guard
    that checks the two sections clear it evenly
    (``_guard_fold_lr_exit_sections_share_bbox_bottom``).
    """
    for port_id, _section, tgt in _iter_cross_row_aligned_fold_lr_exits(graph):
        if abs(tgt.y - graph.stations[port_id].y) <= tolerance:
            yield port_id, tgt


def iter_stacked_rows_in_rowspan_band(
    graph: MetroGraph, tolerance: float
) -> Iterator[tuple[list[Section], float, float]]:
    """Yield ``(stack, band_top, band_bot)`` for single-row stacks beside a rowspan.

    A ``stack`` is the single-row sections of one column, ordered by grid row,
    that cover one-per-row the full row range an *adjacent* ``grid_row_span > 1``
    section spans.  ``band_top``/``band_bot`` are that neighbour's bbox extent.
    Only stacks whose band has slack beyond their combined height (by more than
    ``tolerance``) are yielded, so a stack already filling its band is skipped.

    The single source of "which stack must fill which rowspan band" shared by the
    pass that distributes it (:func:`_distribute_stacked_rows_in_rowspan_band`),
    the guard that flags a stack that does not
    (``_guard_stacked_rows_fill_rowspan_band``), and the layout invariant test --
    so the three cannot drift on scope or predicate.
    """
    rowspans = [
        s
        for s in graph.sections.values()
        if s.grid_row_span > 1 and s.bbox_h > 0 and s.grid_row >= 0
    ]
    if not rowspans:
        return

    by_col: dict[int, list[Section]] = defaultdict(list)
    for section in graph.sections.values():
        if section.grid_row_span == 1 and section.bbox_h > 0 and section.grid_row >= 0:
            by_col[section.grid_col].append(section)

    for col, stack in by_col.items():
        stack.sort(key=lambda s: s.grid_row)
        band = [
            r
            for r in rowspans
            if abs(r.grid_col - col) == 1
            and r.grid_row <= stack[0].grid_row
            and r.grid_row + r.grid_row_span - 1 >= stack[-1].grid_row
        ]
        if not band:
            continue
        band_top_row = min(r.grid_row for r in band)
        band_bot_row = max(r.grid_row + r.grid_row_span - 1 for r in band)
        if sorted(s.grid_row for s in stack) != list(
            range(band_top_row, band_bot_row + 1)
        ):
            continue
        band_top = min(r.bbox_y for r in band)
        band_bot = max(r.bbox_y + r.bbox_h for r in band)
        if (band_bot - band_top) - sum(s.bbox_h for s in stack) > tolerance:
            yield stack, band_top, band_bot


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


def _content_station_ids(graph: MetroGraph, section: Section) -> list[str]:
    """IDs of every content marker in ``section``.

    Content = non-port stations excluding the ``__bypass_`` helpers; hidden
    phantoms are kept.  The single definition of the content set the
    top-fit helpers (:func:`...bbox._section_content_hug_top`,
    :func:`...bbox._section_fit_top`,
    :func:`...off_track._off_track_fit_edge`) anchor on, so the set cannot
    drift between them -- e.g. a switch to ``is_hidden``, a superset that
    would drop the phantoms.
    """
    return [
        sid
        for sid in section.station_ids
        if (
            sid in graph.stations
            and not graph.stations[sid].is_port
            and not is_bypass_v(sid)
        )
    ]


def _content_station_ys(graph: MetroGraph, section: Section) -> list[float]:
    """Y of every content marker in ``section``; see :func:`_content_station_ids`."""
    return [graph.stations[sid].y for sid in _content_station_ids(graph, section)]


def _trunk_symmetric_fan_ids(graph: MetroGraph, section: Section) -> set[str]:
    """Content station ids in ``section`` sitting in a Y-mirrored off-trunk pair.

    A station qualifies when another station shares its X (same layer) and
    their Ys are equidistant on opposite sides of the section's trunk Y (the
    Y shared by the most content stations).  This is the "diamond straddles
    the trunk at equal offsets" shape -- a 2-way fork/join or a fork with a
    straight-through middle branch both produce it.

    Scopes the section-bbox padding's bundle-span correction
    (:func:`...bbox._predict_section_content_bottom`,
    :func:`...bbox._section_content_hug_top`) to symmetric fans: an
    unmirrored off-trunk placement (a plain flat multi-line run, a fold, an
    asymmetric fan) keeps the existing anchor-only padding rather than
    growing every multi-line section's bbox.

    Y and X are rounded to 1dp before grouping (matching the convention
    elsewhere for float-keyed layout-coordinate grouping, e.g.
    ``_station_marker_bbox``'s callers), so settled-but-not-bit-identical
    coordinates land in the same bucket.  When no single Y is a clear
    majority, the tie-break (highest count, then topmost Y) can pick either
    row as the trunk; the effect is only fewer/no pairs found, never a wrong
    padding target, so it is left unresolved rather than special-cased.
    """
    content_ids = _content_station_ids(graph, section)
    if len(content_ids) < 3:
        return set()
    counts: dict[float, int] = defaultdict(int)
    for sid in content_ids:
        counts[quantize_coord(graph.stations[sid].y, COORD_GROUP_DIGITS_COARSE)] += 1
    trunk_y = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]
    by_x: dict[float, list[str]] = defaultdict(list)
    for sid in content_ids:
        by_x[quantize_coord(graph.stations[sid].x, COORD_GROUP_DIGITS_COARSE)].append(
            sid
        )
    result: set[str] = set()
    for sids in by_x.values():
        if len(sids) < 2:
            continue
        seen_by_offset: dict[float, list[str]] = defaultdict(list)
        for sid in sids:
            off = quantize_coord(
                graph.stations[sid].y - trunk_y, COORD_GROUP_DIGITS_COARSE
            )
            if off == 0.0:
                continue
            mirrors = seen_by_offset.get(-off)
            if mirrors:
                result.add(sid)
                result.update(mirrors)
            seen_by_offset[off].append(sid)
    return result


def _linear_entry_pill_lines(
    graph: MetroGraph,
    sid: str,
    offsets: Mapping[tuple[str, str], float],
) -> tuple[str, ...] | None:
    """Return the inherited cohort defining an accepted entry-frame pill."""
    if graph.compact_offsets or graph.line_spread is LineSpread.RAILS:
        return None
    station = graph.stations.get(sid)
    if station is None or station.section_id is None or station.is_port:
        return None
    inherited = graph._linear_entry_pill_lines_cache.get(sid)
    # Mirrors the >=2-lane guard in _cache_linear_entry_pill_lines (offsets.py).
    if inherited is None or len(inherited) < 2:
        return None
    inherited_set = set(inherited)
    served = tuple(graph.station_lines(sid))
    local = tuple(line_id for line_id in served if line_id not in inherited_set)
    if len(local) != 1 or not inherited_set.issubset(served):
        return None
    try:
        ordered = sorted(offsets[sid, line_id] for line_id in inherited)
    except KeyError:
        return None
    step = graph_offset_step(graph)
    if any(
        abs(right - left - step) > COORD_TOLERANCE_FINE
        for left, right in zip(ordered, ordered[1:])
    ):
        return None
    local_offset = offsets.get((sid, local[0]))
    if local_offset is None or not (
        abs(local_offset - (ordered[0] - step)) <= COORD_TOLERANCE_FINE
        or abs(local_offset - (ordered[-1] + step)) <= COORD_TOLERANCE_FINE
    ):
        return None
    return inherited


def _station_bundle_offset_span(
    graph: MetroGraph, sid: str, offsets: dict[tuple[str, str], float]
) -> tuple[float, float]:
    """Min/max per-line Y offset ``sid``'s drawn bundle pill spans around
    its anchor lane, ``(0.0, 0.0)`` for a station with no lines.

    A multi-line bundle's per-line offsets need not be centred on the
    anchor -- the default non-compact assignment gives each line a
    priority-ordered offset (0, step, 2*step, ...), so a station carrying
    several lines can have its whole pill sit to one side of ``station.y``.
    Shared by :func:`_station_marker_bbox` and the section-bbox padding
    targets in :mod:`...bbox`, so the room a padding constant reserves
    matches what the marker pill actually spans.
    """
    line_ids = _linear_entry_pill_lines(graph, sid, offsets)
    if line_ids is None:
        line_ids = tuple(graph.station_lines(sid))
    line_offs = [offsets.get((sid, lid), 0.0) for lid in line_ids]
    if not line_offs:
        return 0.0, 0.0
    return min(line_offs), max(line_offs)


def _station_marker_bbox(
    graph: MetroGraph,
    sid: str,
    offsets: dict[tuple[str, str], float] | None = None,
    radius: float | None = None,
) -> tuple[float, float, float, float] | None:
    """Rendered marker / icon bbox for ``sid``, or ``None`` for ports,
    hidden stations, and junctions.

    Mirrors the pill geometry used by ``nf_metro.render.svg``: width
    ``2 * radius``, height ``(max_off - min_off) + 2 * radius``, centred
    at ``(station.x, station.y + (min_off + max_off) / 2)``.
    """
    from nf_metro.layout.routing import compute_station_offsets

    if radius is None:
        radius = STATION_RADIUS_APPROX * graph.stroke_scale

    st = graph.stations.get(sid)
    if st is None or st.is_port or st.is_hidden or sid in graph.junctions:
        return None
    if offsets is None:
        offsets = compute_station_offsets(graph)
    min_off, max_off = _station_bundle_offset_span(graph, sid, offsets)
    cy = st.y + (min_off + max_off) / 2
    half_h = (max_off - min_off) / 2 + radius
    return (st.x - radius, cy - half_h, st.x + radius, cy + half_h)


def marker_cross_exempt(graph: MetroGraph, sid: str) -> bool:
    """True when a non-consumer line crossing ``sid``'s marker is no defect.

    A rail-mode section lays its lines on fixed parallel rails; a line whose
    route skips an interchange runs along its rail through the interchange's
    column and threads its knob.  That is the deliberate rail idiom, not a
    breeze-past, so the marker-cross checks exempt it - matching the render-side
    ``check_marker_crossings`` exemption (#942), which reads the same fact back
    from the drawn rail markers.
    """
    return graph.station_is_rail(sid)


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
    port_tol: float = PORT_BOUNDARY_CROSSING_TOL,
    inset: float = BOUNDARY_CROSSING_INSET,
    axis_tol: float = COORD_TOLERANCE,
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
    return _section_interior_crossings(
        graph, own=False, inset=inset, routes=routes, offsets=offsets
    )


def routes_through_own_section_interior(
    graph: MetroGraph,
    *,
    inset: float = 2.0,
    routes: list[RoutedPath] | None = None,
    offsets: dict[tuple[str, str], float] | None = None,
) -> list[tuple[RoutedPath, str]]:
    """Return ``(route, section_id)`` for every inter-section route segment
    that passes back through the interior of its *own* source or target
    section box, beyond the port-to-boundary stub.

    A route legitimately starts at its source section's exit port and ends at
    its target section's entry port -- both on the box boundary -- so a clean
    route only grazes those two boxes at their edges and travels the
    inter-section gaps between them.  A segment whose interior lies inside its
    own source or target bbox has clawed back through the box instead of
    leaving it and routing around the outside: an away-facing-exit wrap that
    renders as a backtrack (issue #1078).

    This is the complement of :func:`routes_through_unrelated_sections`, which
    exempts the route's own sections; together they cover every section box.
    """
    return _section_interior_crossings(
        graph, own=True, inset=inset, routes=routes, offsets=offsets
    )


def _section_interior_crossings(
    graph: MetroGraph,
    *,
    own: bool,
    inset: float,
    routes: list[RoutedPath] | None,
    offsets: dict[tuple[str, str], float] | None,
) -> list[tuple[RoutedPath, str]]:
    """Shared scan behind :func:`routes_through_unrelated_sections` and
    :func:`routes_through_own_section_interior`.

    ``own`` selects which half of the sections each route is checked against:
    ``True`` its own source/target boxes, ``False`` every other box.  A
    crossing that runs along the section's own-line trunk is exempt either way
    (a forked bundle overlaying its trunk, not a foreign pass-through).
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
        if own and not rp.is_inter_section:
            continue
        own_sections = {
            graph.section_for_station(rp.edge.source),
            graph.section_for_station(rp.edge.target),
        }
        pts = apply_route_offsets(rp, offsets)
        for sid, x0, y0, x1, y1 in boxes:
            is_own_section = sid in own_sections
            if is_own_section != own:
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
            ) and not _runs_along_section_line_trunk(graph, rp, sid, pts):
                out.append((rp, sid))
    return out


def _runs_along_section_line_trunk(
    graph: MetroGraph, rp: RoutedPath, sid: str, pts: list[tuple[float, float]]
) -> bool:
    """Whether ``rp`` overlays section ``sid``'s own trunk for ``rp``'s line.

    A line that forks and rejoins -- a fan-out junction feeding a section's
    perpendicular entry while a sibling leg continues straight past it to a
    section stacked below -- overlays the intervening section's trunk along its
    own line.  That is one continuous stroke, not a foreign line plotted over a
    section it never touches, so it is exempt from the pass-through check.

    The pass is benign only when the segments crossing the box run parallel to
    the section's trunk axis at the coordinate the line's stations there occupy:
    a vertical-flow section's trunk is a constant X, a horizontal-flow one's a
    constant Y.  A section that does not carry the line has no such trunk, so any
    crossing is a real pass-through.
    """
    from nf_metro.layout.geometry import segment_intersects_bbox

    sec = graph.sections.get(sid)
    if sec is None:
        return False
    # Trunk runs along the flow axis; its constant coordinate is the lane axis.
    cross_axis, run_axis = (0, 1) if lanes_run_along_x(sec.direction) else (1, 0)
    trunk = [
        (st.x, st.y)[cross_axis]
        for stid, st in graph.stations.items()
        if st.section_id == sid and rp.line_id in graph.station_lines(stid)
    ]
    if not trunk:
        return False
    box = (sec.bbox_x, sec.bbox_y, sec.bbox_x + sec.bbox_w, sec.bbox_y + sec.bbox_h)
    for i in range(len(pts) - 1):
        if not segment_intersects_bbox(
            pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], box
        ):
            continue
        run_delta = abs(pts[i + 1][run_axis] - pts[i][run_axis])
        cross_delta = abs(pts[i + 1][cross_axis] - pts[i][cross_axis])
        if cross_delta > SAME_COORD_TOLERANCE and run_delta > SAME_COORD_TOLERANCE:
            return False
        coord = pts[i][cross_axis]
        if not any(abs(coord - t) <= SAME_COORD_TOLERANCE for t in trunk):
            return False
    return True


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
    require_phase_field(graph, "_row_y_grid_info")
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


def _column_contiguous_row_groups(
    graph: MetroGraph,
) -> list[list[Section]]:
    """Group laid-out sections by grid column into contiguous row runs.

    The X mirror of :func:`_row_contiguous_column_groups`: each returned group
    has at least 2 sections in adjacent grid rows within one column.  Adjacency
    reads the start row only, matching the row version's use of the start
    column, so a section spanning several rows joins only its start row's
    neighbours.

    Members of a packed cell are excluded.  A packed cell lays its sections
    side-by-side along X inside one grid cell, so they share a column while
    sitting at different X -- there is no common vertical edge for them to
    reach without one growing over another.
    """
    packed = {m for members in graph.cell_packs.values() for m in members}
    by_col: dict[int, list[Section]] = defaultdict(list)
    for section in graph.sections.values():
        if section.bbox_w > 0 and section.grid_col >= 0 and section.id not in packed:
            by_col[section.grid_col].append(section)

    result: list[list[Section]] = []
    for col in by_col.values():
        if len(col) < 2:
            continue
        col_sorted = sorted(col, key=lambda s: s.grid_row)
        group = [col_sorted[0]]
        for s in col_sorted[1:]:
            if s.grid_row - group[-1].grid_row <= 1:
                group.append(s)
            else:
                if len(group) >= 2:
                    result.append(group)
                group = [s]
        if len(group) >= 2:
            result.append(group)
    return result


def _is_side_entered_vertical_section(graph: MetroGraph, section: Section) -> bool:
    """Whether *section* is a vertical-flow (TB/BT) section entered from a
    perpendicular side.

    Such a section routes its entry approach across the band above its first
    internal station, so that band is never empty even before the entry port's
    Y settles.
    """
    if lanes_run_along_y(section.direction):
        return False
    return any(
        (port := graph.ports.get(pid)) is not None
        and port.side in (PortSide.LEFT, PortSide.RIGHT)
        for pid in section.entry_ports
    )


def _side_entered_vertical_feeder_pairs(
    graph: MetroGraph,
) -> Iterator[tuple[Section, Section]]:
    """Yield each side-entered vertical section with its feeder row-mate.

    The feeder is the nearest contiguous row-mate to the section's left.  The
    Stage 6.15a top-align and its guard both iterate these pairs, so the
    enforcer and its check read the same section-to-feeder relation.
    """
    for group in _row_contiguous_column_groups(graph):
        for section in group:
            if not _is_side_entered_vertical_section(graph, section):
                continue
            left = [s for s in group if s.grid_col < section.grid_col]
            if left:
                yield section, max(left, key=lambda s: s.grid_col)


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
                trunk_ys.add(quantize_coord(st.y, COORD_GROUP_DIGITS_FINE))
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


def _lr_port_lines(graph: MetroGraph, port_ids: Iterable[str]) -> set[str]:
    """Union of the line IDs crossing the LEFT/RIGHT ports among ``port_ids``."""
    lines: set[str] = set()
    for pid in port_ids:
        port = graph.ports.get(pid)
        if port is not None and port.side in (PortSide.LEFT, PortSide.RIGHT):
            lines.update(graph.station_lines(pid))
    return lines


def _section_bundle_lines(graph: MetroGraph, section: Section) -> set[str]:
    """Return the set of line IDs crossing a section's LEFT/RIGHT ports."""
    return _lr_port_lines(graph, list(section.entry_ports) + list(section.exit_ports))


def _section_fan_trunk_lines(graph: MetroGraph, section: Section) -> set[str]:
    """Lines that traverse the section's own horizontal trunk.

    This is the fan classifiers' notion of the section bundle: a station
    whose line set is a superset of it anchors the trunk, one whose set is a
    strict subset is a fan branch.

    Derived from :func:`_section_bundle_lines` (the lines crossing the LR
    ports) with the exit-only peel-offs removed: a line present on an exit
    port but on no entry port and no internal station-to-station edge only
    leaves the section toward a downstream one and never runs along this
    trunk.  Keeping it would let a retag on an outbound edge inflate the
    bundle and disqualify an otherwise clean in-section symmetric fan
    (#1426).  Falls back to the raw bundle when the filter would empty it.
    """
    bundle = _section_bundle_lines(graph, section)
    if not bundle:
        return bundle
    internal_ids = set(section.station_ids) - section.port_ids
    traversing = _lr_port_lines(graph, section.entry_ports)
    for sid in internal_ids:
        for edge in graph.edges_from(sid):
            if edge.target in internal_ids:
                traversing.add(edge.line_id)
    trunk = {ln for ln in bundle if ln in traversing}
    return trunk or bundle


def _exit_reaching_nodes(graph: MetroGraph, section: Section) -> frozenset[str]:
    """Internal stations with a forward path to one of *section*'s exit ports.

    Walks backward from each exit port through in-section, non-port feeders, so
    a station is included when the flow through it continues out of the section
    (the section's through-line) rather than dead-ending inside it.  A terminal
    section with no exit port yields the empty set.
    """
    sec_ids = set(section.station_ids)
    reaching: set[str] = set()
    stack = list(section.exit_ports)
    while stack:
        node = stack.pop()
        for edge in graph.edges_to(node):
            src = edge.source
            st = graph.stations.get(src)
            if src in reaching or src not in sec_ids or st is None or st.is_port:
                continue
            reaching.add(src)
            stack.append(src)
    return frozenset(reaching)


def _section_row_through_lines(
    graph: MetroGraph, section: Section
) -> dict[str, set[str]]:
    """Same-row sections each of a section's LEFT/RIGHT port lines reaches.

    Keyed by line id, so the keys are the lines forming the section's share of
    the row's through-trunk and each value names the row-mates that line ties it
    to.  A LEFT/RIGHT port line that only reaches sections in a *different* grid
    row (a fork peeling off to another row via a junction) is absent: it rides a
    perpendicular runway, not the section's horizontal trunk.  This isolates the
    horizontal flow along the row from downstream forks, so trunk alignment can
    compare that rather than the raw port bundle.
    """
    junction_ids = graph.junction_ids
    row = section.grid_row

    def same_row_sections(start: str, line: str, forward: bool) -> set[str]:
        # Follow only edges carrying *line*, and only in the flow direction
        # away from this section (a junction fans several lines to different
        # rows, and a bidirectional walk would loop back to the origin), until
        # the line lands on another section's port in the same grid row.
        seen: set[str] = set()
        stack = [start]
        found: set[str] = set()
        while stack:
            nid = stack.pop()
            if nid in seen:
                continue
            seen.add(nid)
            if nid in junction_ids:
                edges = graph.edges_from(nid) if forward else graph.edges_to(nid)
                for e in edges:
                    if e.line_id == line:
                        stack.append(e.target if forward else e.source)
                continue
            nport = graph.ports.get(nid)
            if nport is not None:
                nsec = graph.sections.get(nport.section_id)
                if nsec is not None and nsec.id != section.id and nsec.grid_row == row:
                    found.add(nsec.id)
        return found

    through: dict[str, set[str]] = {}
    # Entry ports walk backward toward their feeder; exit ports walk forward.
    for port_ids, forward in ((section.entry_ports, False), (section.exit_ports, True)):
        for pid in port_ids:
            port = graph.ports.get(pid)
            if port is None or port.side not in (PortSide.LEFT, PortSide.RIGHT):
                continue
            edges = graph.edges_from(pid) if forward else graph.edges_to(pid)
            for line in graph.station_lines(pid):
                reached: set[str] = set()
                for e in edges:
                    if e.line_id == line:
                        hop = e.target if forward else e.source
                        reached |= same_row_sections(hop, line, forward)
                if reached:
                    through.setdefault(line, set()).update(reached)
    return through


def _row_through_and_carrier_lines(
    graph: MetroGraph, group: Sequence[Section]
) -> tuple[dict[str, dict[str, set[str]]], set[str]]:
    """Per-section through-lines for a row group, and the row's carrier line ids.

    The second element is the union of every through-line id across the
    group. A section whose own through-line ids equal this union carries the
    row's entire trunk (see :func:`_is_fan_branch_leaf`'s ``full_carrier``);
    one whose ids are a strict subset rides a partial lane of a richer trunk.
    """
    through = {s.id: _section_row_through_lines(graph, s) for s in group}
    carried = [set(t) for t in through.values() if t]
    carrier_lines = set().union(*carried) if carried else set()
    return through, carrier_lines


def _is_fan_branch_leaf(
    graph: MetroGraph, section: Section, *, full_carrier: bool
) -> bool:
    """True for a terminal section reached purely as a fan-out branch.

    Such a section continues nothing horizontally along its row (a leaf), and
    every line feeding it is also carried -- from the same feeding junction --
    on to another section in the same row.  It therefore rides that continuing
    line's trunk and fans off it through the junction, so leaving it compact
    costs nothing: the shared line keeps the feeder's exit port on the trunk.

    A leaf fed by a *private* line (one that terminates only here) instead
    shares its feeder's exit port with a disjoint route to another section, so
    pulling it onto the trunk keeps that port there too; compacting it would
    drag the port off the trunk and lengthen the other route.  Such a leaf is
    not a fan branch and stays aligned.

    ``full_carrier`` says the section carries the whole row trunk.  When it does,
    a sibling branch that continues anywhere -- even off the row -- makes the
    feeding line shared, because there is no richer trunk for the section to join
    and pulling it on only wastes canvas.  A *partial* carrier rides a subset
    lane of a richer trunk, so a sibling that leaves the row does not make the
    line shared: the section is not a fan branch and aligns onto that trunk as a
    follower.
    """
    row = section.grid_row
    junction_ids = graph.junction_ids

    def forward_sections(start: str, line: str) -> set[str]:
        # Sections other than this one that *line* reaches forward from *start*,
        # following the flow through junctions and stopping at the first port or
        # station in another section.
        seen: set[str] = set()
        stack = [start]
        others: set[str] = set()
        while stack:
            nid = stack.pop()
            if nid in seen:
                continue
            seen.add(nid)
            if nid in junction_ids:
                stack += [e.target for e in graph.edges_from(nid) if e.line_id == line]
                continue
            node = graph.ports.get(nid) or graph.stations.get(nid)
            osec = getattr(node, "section_id", None)
            if osec is not None and osec != section.id:
                others.add(osec)
        return others

    def continues_in_row(start: str, line: str) -> bool:
        return any(
            graph.sections[s].grid_row == row for s in forward_sections(start, line)
        )

    def sibling_shares(start: str, line: str) -> bool:
        # full_carrier semantics: see the docstring above.
        if full_carrier:
            return bool(forward_sections(start, line))
        return continues_in_row(start, line)

    for pid in section.exit_ports:
        port = graph.ports.get(pid)
        if port is None or port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        for e in graph.edges_from(pid):
            if continues_in_row(e.target, e.line_id):
                return False

    feed: set[str] = set()
    shared: set[str] = set()
    for pid in section.entry_ports:
        port = graph.ports.get(pid)
        if port is None or port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        for e in graph.edges_to(pid):
            feed.add(e.line_id)
            for sibling in graph.edges_from(e.source):
                if (
                    sibling.line_id == e.line_id
                    and sibling.target != pid
                    and sibling_shares(sibling.target, sibling.line_id)
                ):
                    shared.add(e.line_id)
                    break
    return bool(feed) and feed <= shared


def section_exit_lines(graph: MetroGraph, section: Section) -> set[str]:
    """Return the line IDs that leave a section.

    Combines the exit-port directives with the lines on the routed edges out
    of the section's exit ports. A station whose lines are disjoint from this
    set has no forward path out of the section (a terminal spur).
    """
    exit_lines: set[str] = set()
    for _side, line_ids in section.exit_hints:
        exit_lines.update(line_ids)
    for pid in section.exit_ports:
        for edge in graph.edges_from(pid):
            exit_lines.add(edge.line_id)
    return exit_lines


def _fan_offsets(n: int) -> list[int]:
    """Symmetric vertical slot offsets for ``n`` stations fanned about a
    trunk Y: even ``n`` leaves the trunk row empty (-n//2..-1, 1..n//2),
    odd ``n`` keeps a middle station on the trunk (-(n//2)..n//2).
    """
    if n % 2 == 0:
        return list(range(-(n // 2), 0)) + list(range(1, n // 2 + 1))
    return list(range(-(n // 2), n // 2 + 1))


_LANE_SIGNS_ON_AXIS: dict[str, frozenset[float]] = {
    axis: frozenset(
        AxisFrame.secondary_sign_for(direction)
        for direction in FLOW_DIRECTIONS
        if AxisFrame.axes_for_direction(direction)[1] == axis
    )
    for axis in ("x", "y")
}
"""Lane-fan signs of the flows that stack their lines on each axis.

A pure function of the axis over immutable inputs, so it is settled at import
rather than per call: ``port_bundle_edge_reach`` asks it once per port per sizing
pass, thousands of times over a corpus render.
"""


def port_bundle_edge_reach(
    graph: MetroGraph,
    pid: str,
    offsets: dict[tuple[str, str], float] | None,
    axis: str,
) -> tuple[float, float]:
    """``(low, high)`` reach of *pid*'s drawn bundle past the port station on *axis*.

    The port analogue of :func:`_bundle_edge_padding`'s ``edge_reach``: a port's
    lines cross its edge staggered by their per-line offsets
    (:func:`_station_bundle_offset_span`), so the outermost drawn lane sits that
    far off the port station and room measured from the station alone over-states
    what the lane gets.

    The run through a port is normal to the edge the port is pinned to, so its
    stagger lands on the port's *free* axis and nowhere else -- hence
    ``(0.0, 0.0)`` for the other one.  Which side of the station it lands on is
    the lane sign (:meth:`AxisFrame.secondary_sign_for`) of the flows that stack
    lines on *axis*: the flows stacking on Y all sign it ``+1``, so a LEFT/RIGHT
    port's bundle rides wholly on the +Y side of it, while the flows stacking on X
    disagree (TB fans one way, BT the other), leaving a TOP/BOTTOM port's bundle
    free to sit either side and both sides to be assumed.
    """
    port = graph.ports.get(pid)
    if port is None or offsets is None:
        return 0.0, 0.0
    travel_axis = "x" if port.side in (PortSide.LEFT, PortSide.RIGHT) else "y"
    if axis == travel_axis:
        return 0.0, 0.0
    min_off, max_off = _station_bundle_offset_span(graph, pid, offsets)
    if _LANE_SIGNS_ON_AXIS[axis] == frozenset({1.0}):
        return max(0.0, -min_off), max(0.0, max_off)
    reach = max(abs(min_off), abs(max_off))
    return reach, reach


def port_edge_inset(
    port: Port | None,
    section_direction: str,
    axis: str,
    lane_reach: float = 0.0,
) -> float:
    """Room *port* owes a bbox edge normal to *axis* (``"x"`` or ``"y"``).

    A station flagged as a port but carrying no ``Port`` record has no side to
    judge by, so it owes nothing and stays hard-contained.

    A port pinned to that edge belongs on it and owes nothing: an LR section's
    TOP port *is* its top edge.  A port free along *axis* crosses the edges normal
    to it and reads as running along the border unless the box keeps
    ``PERP_PORT_EDGE_INSET`` beyond it.

    On X that is every TOP/BOTTOM port, whichever way its section flows: a seam
    joins a BOTTOM exit to a TOP entry at one X, and both halves owe that X the
    same room.

    On Y it is only a vertical flow's LEFT/RIGHT port.  A horizontal flow's
    LEFT/RIGHT port is its trunk arriving or leaving, not a run crossing the box,
    so a single line through it owes the top and bottom edges nothing here: where
    it sits is the content padding's business, and reserving against it instead
    grows a box into its neighbours.

    ``lane_reach`` is how far the port's drawn bundle extends past the port
    station toward that edge (:func:`port_bundle_edge_reach`).  Both insets are
    room the outermost *drawn lane* owes the border rather than room the station
    owes it, so the reach adds on top: a bundle staggered 12px off its port would
    otherwise leave its outer lane 12px short of what the inset advertises.

    A port the wider inset does not cover owes ``MIN_BUNDLE_EDGE_CLEARANCE`` past
    its outermost lane whenever it carries a bundle at all: a multi-line trunk
    crossing the box is drawn ink needing label room off the border, exactly as
    an interior station's pill is (:func:`...bbox._bundle_edge_padding`).  That
    is stricter than the ``PERP_PORT_EDGE_CLEARANCE`` floor
    :func:`...guards._guard_ports_clear_unanchored_box_edges` enforces, the hard
    runtime minimum for a lane of any kind.
    """
    if port is None:
        return 0.0
    reach = max(0.0, lane_reach)
    if axis == "y" and port.side in (PortSide.LEFT, PortSide.RIGHT):
        if lanes_run_along_x(section_direction):
            return reach + PERP_PORT_EDGE_INSET
    elif axis == "x" and port.side in (PortSide.TOP, PortSide.BOTTOM):
        return reach + PERP_PORT_EDGE_INSET
    if reach:
        return reach + MIN_BUNDLE_EDGE_CLEARANCE
    return 0.0


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
    graph: MetroGraph, section: Section, side: PortSide, edge: float
) -> None:
    """Move every port on *side* of *section* to the bbox edge at *edge*.

    A TOP/BOTTOM port lives on the section's Y edge; a LEFT/RIGHT port on
    its X edge.  The coordinate the port is pinned to follows from the side,
    so one helper serves both axes.
    """
    axis = "x" if side in (PortSide.LEFT, PortSide.RIGHT) else "y"
    for pid in section.entry_ports + section.exit_ports:
        port = graph.ports.get(pid)
        port_st = graph.stations.get(pid)
        if not port or not port_st:
            continue
        if port.side == side:
            setattr(port_st, axis, edge)
            setattr(port, axis, edge)


def section_axes(section: Section | None) -> tuple[str, str]:
    """A section's ``(flow_axis, cross_axis)`` names.

    The flow (primary) axis runs along the section's layers; the cross
    (secondary) axis is the one it stacks its lines / off-track band along.
    LR/RL flow along X and stack on Y; TB/BT transpose the two.  A missing
    section defaults to LR.  The single question the off-track and bbox-padding
    machinery asks to stay orientation-agnostic.
    """
    return AxisFrame.axes_for_direction(section.direction or "LR" if section else "LR")


def section_cross_axis(section: Section) -> str:
    """Cross axis (``"x"`` or ``"y"``) a section stacks its lines along.

    The secondary of :func:`section_axes`.
    """
    return section_axes(section)[1]


# (origin, size) bbox-field names for each cross axis; the cross-min edge is
# the origin (bbox_y / bbox_x) and the cross-max edge is origin + size.
_CROSS_BBOX_FIELDS = {"y": ("bbox_y", "bbox_h"), "x": ("bbox_x", "bbox_w")}


def move_section_bbox_min_edge(
    graph: MetroGraph, section: Section, axis: str, new_min: float
) -> None:
    """Move a section's cross-min bbox edge to *new_min* (grow or shrink).

    The cross-min edge is the top (Y axis) or left (X axis) of the box; the
    opposite (cross-max) edge stays put, so the box's size absorbs the move.
    The ports on that edge (TOP for Y, LEFT for X) follow.  Bidirectional
    primitive; grow-only callers use :func:`grow_section_bbox_min_edge`.
    """
    origin_attr, size_attr = _CROSS_BBOX_FIELDS[axis]
    origin = getattr(section, origin_attr)
    setattr(section, size_attr, getattr(section, size_attr) + origin - new_min)
    setattr(section, origin_attr, new_min)
    side = PortSide.TOP if axis == "y" else PortSide.LEFT
    _pull_section_ports_to_edge(graph, section, side, new_min)


def grow_section_bbox_min_edge(
    graph: MetroGraph, section: Section, axis: str, new_min: float
) -> None:
    """Extend a section's cross-min edge to *new_min*, never contracting it."""
    origin_attr, _ = _CROSS_BBOX_FIELDS[axis]
    if new_min < getattr(section, origin_attr):
        move_section_bbox_min_edge(graph, section, axis, new_min)


def move_section_bbox_max_edge(
    graph: MetroGraph, section: Section, axis: str, new_max: float
) -> None:
    """Move a section's cross-max bbox edge (bottom / right) to *new_max*.

    Bidirectional: the cross-min edge stays put, so the box's size absorbs
    the move in either direction.  The ports on that edge (BOTTOM for Y,
    RIGHT for X) follow.  Grow-only callers use
    :func:`grow_section_bbox_max_edge`.
    """
    origin_attr, size_attr = _CROSS_BBOX_FIELDS[axis]
    setattr(section, size_attr, new_max - getattr(section, origin_attr))
    side = PortSide.BOTTOM if axis == "y" else PortSide.RIGHT
    _pull_section_ports_to_edge(graph, section, side, new_max)


def grow_section_bbox_max_edge(
    graph: MetroGraph, section: Section, axis: str, new_max: float
) -> None:
    """Extend a section's cross-max bbox edge (bottom / right) to *new_max*.

    Grow-only: a *new_max* at or inside the current edge is a no-op.  The
    ports on that edge (BOTTOM for Y, RIGHT for X) follow.
    """
    origin_attr, size_attr = _CROSS_BBOX_FIELDS[axis]
    if new_max <= getattr(section, origin_attr) + getattr(section, size_attr):
        return
    move_section_bbox_max_edge(graph, section, axis, new_max)


def section_anchor_edge(section: Section, axis: str, sign: float) -> float:
    """The *axis* box edge a *sign*-anchored section's content starts at.

    ``sign`` is a :func:`...geometry.box_growth_sign` value: ``+1`` reads the
    axis-min edge (left / top), ``-1`` the axis-max one (right / bottom).  The
    single accessor a group-levelling pass needs so the anchor side is a
    parameter rather than a second code path.
    """
    origin_attr, size_attr = _CROSS_BBOX_FIELDS[axis]
    origin = getattr(section, origin_attr)
    return origin if sign > 0 else origin + getattr(section, size_attr)


def grow_section_bbox_to_anchor(
    graph: MetroGraph, section: Section, axis: str, sign: float, target: float
) -> None:
    """Extend a section's *sign*-anchored *axis* edge out to *target*.

    The signed counterpart of :func:`grow_section_bbox_min_edge` /
    :func:`grow_section_bbox_max_edge`, dispatched on the anchor side that
    :func:`section_anchor_edge` reads.  Grow-only in both directions, and the
    ports on the moved edge follow it.
    """
    if sign > 0:
        grow_section_bbox_min_edge(graph, section, axis, target)
    else:
        grow_section_bbox_max_edge(graph, section, axis, target)


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
    return _section_span_clear(graph, section, lo, hi, set(carrier_ids))


def _section_span_clear(
    graph: MetroGraph,
    section: Section,
    lo: float,
    hi: float,
    exclude: set[str],
) -> bool:
    """Whether no non-port station of *section* sits strictly within ``(lo, hi)``.

    A run drawn along a row at some X span is ploughed through any internal
    station seated inside it; ports (invisible) and the excluded stations (the
    run's own endpoints) do not count.
    """
    for sid in section.station_ids:
        if sid in exclude:
            continue
        st = graph.stations.get(sid)
        if st is None or st.is_port:
            continue
        if lo + SAME_COORD_TOLERANCE < st.x < hi - SAME_COORD_TOLERANCE:
            return False
    return True


def _is_fanout_junction(
    graph: MetroGraph,
    jid: str,
    divergence_sources: Mapping[str, str],
) -> bool:
    """Whether *jid* is a fan-out junction whose Y follows its exit port.

    The authored topology identifies the divergence. This helper additionally
    requires every immediate successor to be an entry port, so its caller only
    claims the plain exit -> junction -> entries shape. Such a junction takes
    its Y from the resolved exit port (see :func:`_position_junctions`), so
    anchoring that exit drives the junction's row and keeps the fan-out risers
    in the inter-section gap.
    """
    succ = list(graph.edges_from(jid))
    if not succ:
        return False
    if any((tp := graph.ports.get(e.target)) is None or not tp.is_entry for e in succ):
        return False
    return jid in divergence_sources


def _exit_anchorable_downstream(
    graph: MetroGraph,
    exit_port_id: str,
    junction_ids: set[str],
    divergence_sources: Mapping[str, str],
) -> bool:
    """Whether an exit's downstream lets its level change defer to the gap.

    True when every outgoing edge lands on an entry port directly, or on a
    fan-out junction (whose Y follows this exit).  A merge junction on the far
    side pins its own Y to the downstream entry, so the exit aligns there
    instead and keeps its downstream-aligned placement.
    """
    edges = graph.edges_from(exit_port_id)
    saw_target = False
    for e in edges:
        if e.target in junction_ids:
            if not _is_fanout_junction(graph, e.target, divergence_sources):
                return False
        else:
            tp = graph.ports.get(e.target)
            if tp is None or not tp.is_entry:
                return False
        saw_target = True
    return saw_target


def exit_entry_ports_face(
    exit_port: Port,
    entry_port: Port,
    exit_section: Section,
    entry_section: Section,
) -> bool:
    """Whether a LEFT/RIGHT exit and its target entry port open toward each other.

    They face when the exit is on the RIGHT edge and the entry on the LEFT of a
    section further right (or the mirror): the inter-section link is then a
    straight horizontal hop across the column gap.  When both ports sit on the
    same horizontal side, the line must wrap vertically around one section to
    reach the other, so aligning the exit to the downstream row only drags it
    off its own carrier row without straightening the (wrapped) connection.
    """
    if exit_port.side is PortSide.RIGHT and entry_port.side is PortSide.LEFT:
        return exit_section.bbox_x < entry_section.bbox_x
    if exit_port.side is PortSide.LEFT and entry_port.side is PortSide.RIGHT:
        return exit_section.bbox_x > entry_section.bbox_x
    return False


def _in_section_exit_carriers(
    graph: MetroGraph, exit_port_id: str, section: Section
) -> dict[str, float]:
    """Y of each non-port station inside *section* that feeds *exit_port_id*."""
    carriers: dict[str, float] = {}
    for e in graph.edges_to(exit_port_id):
        s = graph.station_for_edge_source(e)
        if not s.is_port and s.section_id == section.id:
            carriers[e.source] = s.y
    return carriers


def flow_exit_carrier_anchor(
    graph: MetroGraph,
    exit_port_id: str,
    section: Section,
    junction_ids: set[str],
    *,
    divergence_sources: Mapping[str, str] | None = None,
) -> tuple[float, list[str]] | None:
    """Carrier row a flow-aligned exit should anchor to, with its carriers.

    Returns ``(carrier_y, carrier_ids)`` when a LEFT/RIGHT exit on a non-fold
    LR/RL section runs into a downstream entry port -- directly or through a
    fan-out junction -- over a clear corridor and its carriers anchor it to a
    shared row; ``None`` otherwise.  Anchoring it there turns the in-section
    run horizontal and moves the level change to a riser in the inter-section
    gap.

    The carriers anchor when they are a single internal station, a *parallel
    bundle* (several stations sharing one row, one per distinct carried line, so
    each line rides its own offset track to the port), or a *single-line chain
    fanning out through a junction* (several stations on one row carrying one
    line, connected feed-forward so the line runs through every carrier to the
    port as one trunk, and the port fans out via a junction whose row follows
    it so the level change becomes gap risers).  A bypass bundle -- several
    unconnected stations feeding one line -- is excluded: the farther feeder
    shares the nearer carrier's track and would run straight through it.  A
    single-line chain feeding one facing entry port directly is likewise a
    bypass exit that keeps its downstream-aligned placement, since that entry
    is pinned to its own row and anchoring would kink the straight hop.  A fold
    section, a merge junction on the far side, or a corridor blocked by another
    station also keep the downstream-aligned placement.
    """
    port = graph.ports.get(exit_port_id)
    if port is None or port.side not in (PortSide.LEFT, PortSide.RIGHT):
        return None
    if _is_fold_section(section) or section.direction not in ("LR", "RL"):
        return None
    if divergence_sources is None:
        divergence_sources = divergence_junction_sources(graph)
    if not _exit_anchorable_downstream(
        graph,
        exit_port_id,
        junction_ids,
        divergence_sources,
    ):
        return None
    carriers = _in_section_exit_carriers(graph, exit_port_id, section)
    if not carriers:
        return None
    ys = list(carriers.values())
    if len(carriers) > 1:
        exit_lines = {
            e.line_id for e in graph.edges_to(exit_port_id) if e.source in carriers
        }
        share_row = max(ys) - min(ys) <= SAME_COORD_TOLERANCE
        one_line_per_carrier = len(carriers) == len(exit_lines)
        single_line_chain = (
            len(exit_lines) == 1
            and _carriers_form_flow_chain(graph, section, list(carriers))
            and _exit_fans_out_via_junction(
                graph,
                exit_port_id,
                junction_ids,
                divergence_sources,
            )
        )
        carriers_anchor = share_row and (one_line_per_carrier or single_line_chain)
        if not carriers_anchor:
            return None
    carrier_ids = list(carriers)
    if not exit_run_corridor_clear(graph, exit_port_id, section, carrier_ids):
        return None
    return min(ys), carrier_ids


def _carriers_form_flow_chain(
    graph: MetroGraph, section: Section, carrier_ids: list[str]
) -> bool:
    """Whether same-line exit carriers form one feed-forward trunk to the port.

    Several carriers on one row all carrying a *single* line are one trunk, not
    a parallel bundle.  Ordered along the flow, each outer carrier must feed the
    next by an in-section edge so the line genuinely runs through every carrier
    marker, and no unrelated station may sit in the span between a consecutive
    pair for the trunk to plough through.  When both hold, the outermost
    carrier's straight run to the port rides the line's own trunk rather than
    bypassing a station off the row.
    """
    flow = AxisFrame.flow_sign(section.direction)
    ordered = sorted(carrier_ids, key=lambda c: flow * graph.stations[c].x)
    carrier_set = set(carrier_ids)
    for outer, inner in zip(ordered, ordered[1:]):
        if not any(e.target == inner for e in graph.edges_from(outer)):
            return False
        lo, hi = sorted((graph.stations[outer].x, graph.stations[inner].x))
        if not _section_span_clear(graph, section, lo, hi, carrier_set):
            return False
    return True


def _exit_fans_out_via_junction(
    graph: MetroGraph,
    exit_port_id: str,
    junction_ids: set[str],
    divergence_sources: Mapping[str, str],
) -> bool:
    """Whether every edge out of the exit lands on a fan-out junction.

    A fan-out junction's Y follows the exit port, so anchoring the exit to its
    carrier row drives the junction there and the fan risers fall in the
    inter-section gap.  An exit feeding a facing entry port directly is a
    straight-hop target whose entry is pinned to its own row, so a multi-feeder
    chain there stays downstream-aligned rather than kinking the hop.
    """
    edges = list(graph.edges_from(exit_port_id))
    return bool(edges) and all(
        e.target in junction_ids
        and _is_fanout_junction(graph, e.target, divergence_sources)
        for e in edges
    )


def wrap_exit_carrier_anchor(
    graph: MetroGraph,
    exit_port_id: str,
    section: Section,
    junction_ids: set[str],
) -> tuple[float, list[str]] | None:
    """Carrier row a *wrapping* flow-aligned exit should anchor to.

    A LEFT/RIGHT exit on a non-fold LR/RL section whose sole downstream target
    is an entry port on the *same* horizontal side must wrap vertically around
    the target to reach it -- the ports do not face across the column gap (see
    :func:`exit_entry_ports_face`).  It leaves at its carrying station's row:
    the level change belongs to a riser in the inter-section corridor, not a
    diagonal off the carrier row into the box corner.  Returns
    ``(carrier_y, carrier_ids)`` when the carriers share one row (so the anchor
    is unambiguous); ``None`` otherwise -- including the facing case that
    :func:`flow_exit_carrier_anchor` already covers, and fan-out junctions whose
    row follows the exit.
    """
    port = graph.ports.get(exit_port_id)
    if port is None or port.side not in (PortSide.LEFT, PortSide.RIGHT):
        return None
    if _is_fold_section(section) or section.direction not in ("LR", "RL"):
        return None
    targets = [e.target for e in graph.edges_from(exit_port_id)]
    if len(targets) != 1 or targets[0] in junction_ids:
        return None
    entry_port = graph.ports.get(targets[0])
    entry_section = graph.sections.get(entry_port.section_id) if entry_port else None
    if entry_port is None or not entry_port.is_entry or entry_section is None:
        return None
    if exit_entry_ports_face(port, entry_port, section, entry_section):
        return None
    carriers = _in_section_exit_carriers(graph, exit_port_id, section)
    ys = list(carriers.values())
    if not ys or max(ys) - min(ys) > SAME_COORD_TOLERANCE:
        return None
    return min(ys), list(carriers)
