"""Symmetric fan-out/fan-in bundle distribution and half-grid placement."""

from __future__ import annotations

import math
from collections import defaultdict, deque
from collections.abc import Iterable, Iterator

from nf_metro.layout.constants import (
    SAME_COORD_TOLERANCE,
    SECTION_Y_PADDING,
)
from nf_metro.layout.geometry import (
    lanes_run_along_x,
    lanes_run_along_y,
    perpendicular_port_sides,
    shift_section,
)
from nf_metro.layout.phase_state import require_phase_field
from nf_metro.layout.phases._common import (
    _fan_offsets,
    _grid_group_section_ids,
    _section_fan_trunk_lines,
    _section_lr_port_anchor_y,
    continuation_track_is_realizable,
    continuation_track_predecessors,
    grow_section_bbox_max_edge,
    grow_section_bbox_min_edge,
)
from nf_metro.layout.phases.planned_fans import (
    planned_fan_layout_station_ids,
    planned_fan_port_ids,
)
from nf_metro.layout.phases.ports import _entry_fan_trunk_station, _set_port_y
from nf_metro.parser.model import MetroGraph, PortSide, Section, Station


def _carry_full_bundle_continuations(graph: MetroGraph) -> None:
    """Restore sole-successor tracks moved by full-bundle column fanning.

    Fanning a full-bundle column spreads its members off the trunk row.  A
    station whose only predecessor is one of those members, and which is that
    member's only target, has no branch to peel off to, so leaving it on its own
    line's base track paints the in-section V-kink #977 forbids.  This copies
    the predecessor's settled lane back onto it wherever the target track is
    free.

    Deliberately outside :func:`~nf_metro.layout.engine._run_placement`: reading
    a predecessor's *current* coordinate is the whole operation, and a
    content-placement phase registered in ``CONTENT_PLACEMENT_PHASES`` must
    derive its answer from frozen anchors and structure alone.  It sits with the
    other coordinate-inheritance passes of Stage 6.7 (6.7b's symmetric-branch
    carry, 6.7c/6.7d's port centring), which are bare for the same reason.
    """
    for node, predecessor in continuation_track_predecessors(graph).items():
        if not continuation_track_is_realizable(graph, node, predecessor):
            continue
        graph.stations[node].y = graph.stations[predecessor].y


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
                pre = graph.station_for_edge_source(e2)
                if pre.is_port:
                    continue
                inbound[edge.target].add(e2.source)
        else:
            src = graph.station_for_edge_source(edge)
            if src.is_port:
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


def _trunkless_entry_fans(
    graph: MetroGraph,
) -> Iterator[tuple[str, Section, set[str]]]:
    """Yield ``(port_id, section, direct_targets)`` for trunkless entry fans.

    A qualifying entry port fans directly to three or more distinct in-section
    targets and has no unique trunk arm (:func:`_entry_fan_trunk_station` is
    ``None``).  A single-target port, or one whose targets carry a unique
    trunk, has a 1:1 crossing to align against and no fan to centre, so it is
    skipped.

    A two-target fan is skipped too: it is a minimal diamond whose symmetric
    layout the two-branch half-grid mechanism (``_section_symfan_uses_half_grid``)
    already owns, and forcing its midpoint here pulls a branch that continues as
    the section's on-grid inter-section trunk off that trunk.  The multi-target
    spread this yields is the shape with no such canonical single-branch trunk.
    """
    for section in graph.sections.values():
        for port_id in section.entry_ports:
            if _entry_fan_trunk_station(graph, port_id, section) is not None:
                continue
            targets = {
                st.id
                for edge in graph.edges_from(port_id)
                if not (st := graph.station_for_edge_target(edge)).is_port
                and st.section_id == section.id
            }
            if len(targets) >= 3:
                yield port_id, section, targets


def _station_rooted_fans(
    graph: MetroGraph,
) -> Iterator[tuple[str, Section, set[str]]]:
    """Yield ``(hub_id, section, direct_targets)`` for a station-rooted fan.

    The internal-station analogue of :func:`_trunkless_entry_fans`: a non-port,
    non-hidden station that fans directly to three or more distinct in-section
    targets.  Its reconvergence join is found by
    :func:`_fan_reconvergence_joins` exactly as an entry port's is, so a genuine
    diverge-then-reconverge shape rooted at an internal station - not at a
    section boundary port - is discovered too.  The three-target minimum matches
    :func:`_trunkless_entry_fans` and rests on the same backing: a two-branch
    fork, whichever kind of hub roots it, is compacted onto half-pitch offsets
    by :func:`_recenter_full_bundle_columns`, so leaving it out here defers to
    that mechanism rather than dropping it on the floor.  No trunk test is
    applied: a hub arm that continued past the merge instead of reconverging
    would leave the fan without a common join, which
    :func:`_fan_reconvergence_joins` already rejects.
    :func:`_symmetric_reconvergence_joins` keeps only the hidden-node joins
    these fans reach; a visible internal join is left to the general placement
    pipeline that already seats it.
    """
    for section in graph.sections.values():
        for sid in section.station_ids:
            if not _is_in_section_on_track(graph.stations.get(sid), section.id):
                continue
            targets = set(_in_section_ontrack_successors(graph, section, sid))
            if len(targets) >= 3:
                yield sid, section, targets


def _symmetric_reconvergence_joins(graph: MetroGraph) -> dict[str, list[str]]:
    """Return {join_id: [source_ids]} for a trunkless symmetric fan's reconvergence.

    The join of a trunkless fan is the station where *every* arm reconverges -
    reachable within the section from all of the fan's direct targets.  The
    exact fork-hub/join-source-set detection in
    :func:`_divergence_midpoint_targets` cannot see it because one arm reaches
    it through an extra internal hop, so the fan's direct-target set never
    equals the join's source set.  It is recorded here for
    :func:`_restore_convergence_midpoints` to seat on the fan midpoint alongside
    the ordinary convergences.  A local merge on only some of the arms is
    deliberately excluded: it is not the whole fan's centreline and recentring
    it perturbs the branches that do not pass through it.

    An entry-port fan's join is seated whether it is visible or a hidden merge
    node: the boundary crossing roots the fan off the section frame, not the
    fan's own centreline, so nothing else places it.  A station-rooted fan
    (:func:`_station_rooted_fans`) is only seated when its join is a hidden
    merge node.  A visible internal join already sits on its midpoint by the
    time the general placement pipeline settles, and re-seating it here from the
    grid-snap's intermediate coordinates displaces it; a hidden node carries no
    glyph and is skipped by every on-track placement mechanism, so it stays on
    its raw topological row unless seated here.  The reconvergence traversal
    keeps a hidden node in view to find it (see
    :func:`_in_section_track_or_hidden_successors`).

    Gated on ``graph.diamond_style == "symmetric"`` - the same author opt-in
    that scopes every other centreline compaction in this module; an ungated
    version would recentre unrelated joins across the corpus.  Line membership
    is deliberately not part of the gate: the qualifying fans carry more than
    one line, and a single-line gate would pass over them.
    """
    if graph.diamond_style != "symmetric":
        return {}
    joins: dict[str, list[str]] = {}
    for _port_id, section, direct_targets in _trunkless_entry_fans(graph):
        joins.update(_fan_reconvergence_joins(graph, section, direct_targets))
    for _hub_id, section, direct_targets in _station_rooted_fans(graph):
        joins.update(
            _fan_reconvergence_joins(graph, section, direct_targets, hidden_only=True)
        )
    return joins


def _fan_reconvergence_joins(
    graph: MetroGraph,
    section: Section,
    direct_targets: set[str],
    *,
    hidden_only: bool = False,
) -> dict[str, list[str]]:
    """Return {join_id: [source_ids]} for one trunkless entry fan's reconvergence.

    Shared by :func:`_symmetric_reconvergence_joins` (which seats the join on
    the fan midpoint) and :func:`_entry_fan_centre_ports` (which only centres a
    port whose fan reconverges on exactly one join).  Carries no
    ``diamond_style`` gate: it is pure reconvergence detection, and each caller
    applies its own opt-in.

    ``hidden_only`` keeps only a join that is a hidden merge node; a
    station-rooted fan passes it so a visible internal join, which the general
    placement pipeline already seats, is left alone (see
    :func:`_symmetric_reconvergence_joins`).
    """
    # Per-arm in-section descendant sets; their union bounds the fan and
    # their intersection names the stations every arm reaches.  A genuine
    # merge among those is one with two or more real predecessors - a
    # position-independent test, unlike an off-track flag that only settles
    # once the arms have been placed.  The fan's join is the terminal such
    # merge (no further common merge downstream), so a parallel mid-fan
    # branch flowing onward to the real join is not mistaken for it.
    reach = {
        tgt: _in_section_descendants(graph, tgt, section) for tgt in direct_targets
    }
    fan = set(direct_targets).union(*reach.values())
    candidates = set.intersection(*reach.values())
    preds_by_id = {cid: _nonport_real_predecessors(graph, cid) for cid in candidates}
    common = {cid for cid in candidates if len(preds_by_id[cid]) >= 2}
    joins: dict[str, list[str]] = {}
    for cand in common:
        if _in_section_descendants(graph, cand, section) & common:
            continue
        # An ancestor in ``common`` means the fan already fully reconverged
        # upstream; this candidate is a later partial re-merge after the
        # trunk re-diverged, not the fan's centreline.
        if _in_section_ancestors(graph, cand, section) & common:
            continue
        if hidden_only and not graph.stations[cand].is_hidden:
            continue
        preds = preds_by_id[cand]
        if cand not in preds and preds <= fan:
            joins[cand] = sorted(preds)
    return joins


def _nonport_real_predecessors(graph: MetroGraph, node_id: str) -> set[str]:
    """Real-station (non-port) predecessors of ``node_id``, seen through junctions.

    Wraps :func:`_real_predecessors` for a single node and drops any port so the
    count reflects merging branches rather than a boundary crossing.
    """
    return {
        pred_id
        for pred_id in _real_predecessors(graph, {node_id})
        if (pred_st := graph.stations.get(pred_id)) is not None and not pred_st.is_port
    }


def _entry_fan_centre_ports(graph: MetroGraph) -> dict[str, list[str]]:
    """Return {entry_port_id: [direct_target_ids]} for ``center_ports`` fan-in ports.

    A section boundary entry port that fans directly to two or more distinct
    internal targets with no unique trunk arm (:func:`_entry_fan_trunk_station`
    is ``None``) has no 1:1 crossing for ``center_ports`` to align.  Recording it
    here alongside the ordinary convergences seats it on the midpoint of the
    targets it serves, through the same protect-and-restore the grid snap already
    applies to a fan-in convergence.

    Restricted to a fan that reconverges on exactly one in-section join
    (:func:`_fan_reconvergence_joins`): only a genuine diverge-then-reconverge
    shape has a single well-defined centreline to seat the port on.  A trunkless
    fan whose arms never reconverge, or reconverge on several independent joins,
    has no such centre, so centring its port would drag it off the row's shared
    port lane for no geometric gain.

    Gated on ``graph.center_ports``, the opt-in for boundary-port centring: a
    map that only sets ``diamond_style`` keeps its entry ports where the
    boundary seating put them.
    """
    if not graph.center_ports:
        return {}
    return {
        port_id: sorted(direct_targets)
        for port_id, section, direct_targets in _trunkless_entry_fans(graph)
        if len(_fan_reconvergence_joins(graph, section, direct_targets)) == 1
    }


def _in_section_track_or_hidden_successors(
    graph: MetroGraph, section: Section, sid: str
) -> list[str]:
    """Direct in-section successors of ``sid``, keeping hidden merge stations.

    Like :func:`_in_section_ontrack_successors` but keeps a hidden station in
    the result, so a fan that reconverges onto a ``_``-prefixed hidden merge
    node (standing in for several converging arms, per the hidden-station
    convention) is discovered.  Ports, off-track stations, and anything outside
    ``section`` bound the walk.
    """
    return sorted(
        {
            e.target
            for e in graph.edges_from(sid)
            if _is_in_section_track_or_hidden(
                graph.station_for_edge_target(e), section.id
            )
        }
    )


def _in_section_track_or_hidden_predecessors(
    graph: MetroGraph, section: Section, sid: str
) -> list[str]:
    """Direct in-section predecessors of ``sid``, keeping hidden merge stations.

    The reverse-direction twin of
    :func:`_in_section_track_or_hidden_successors`.
    """
    return sorted(
        {
            e.source
            for e in graph.edges_to(sid)
            if _is_in_section_track_or_hidden(
                graph.station_for_edge_source(e), section.id
            )
        }
    )


def _in_section_descendants(
    graph: MetroGraph, start_id: str, section: Section
) -> set[str]:
    """In-section stations forward-reachable from ``start_id``.

    ``start_id`` itself is excluded.  Traversal follows
    :func:`_in_section_track_or_hidden_successors`, so ports, off-track
    stations, and anything outside ``section`` bound the walk, while a hidden
    merge node stays in it so a fan reconverging onto one is discovered.
    """
    seen: set[str] = set()
    queue = deque([start_id])
    while queue:
        node = queue.popleft()
        for succ in _in_section_track_or_hidden_successors(graph, section, node):
            if succ not in seen:
                seen.add(succ)
                queue.append(succ)
    return seen


def _in_section_ancestors(
    graph: MetroGraph, start_id: str, section: Section
) -> set[str]:
    """In-section stations backward-reachable from ``start_id``.

    The reverse-direction twin of :func:`_in_section_descendants`, walking
    :func:`_in_section_track_or_hidden_predecessors`; ``start_id`` itself is
    excluded.
    """
    seen: set[str] = set()
    queue = deque([start_id])
    while queue:
        node = queue.popleft()
        for pred in _in_section_track_or_hidden_predecessors(graph, section, node):
            if pred not in seen:
                seen.add(pred)
                queue.append(pred)
    return seen


def _divergence_target_successors(graph: MetroGraph) -> dict[str, list[str]]:
    """Return {hub_id: [target_station_ids]} for fan-out divergence anchors.

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
                post = graph.station_for_edge_target(e2)
                if post.is_port:
                    continue
                outbound[edge.source].add(e2.target)
        else:
            tgt = graph.station_for_edge_target(edge)
            if tgt.is_port:
                continue
            outbound[edge.source].add(tgt_id)

    anchors: dict[str, list[str]] = {}
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
        has_below = any(ty < sy - SAME_COORD_TOLERANCE for ty in tgt_ys)
        has_above = any(ty > sy + SAME_COORD_TOLERANCE for ty in tgt_ys)
        if has_below and has_above:
            anchors[src_id] = sorted(tgt_ids)
    return anchors


def _divergence_target_ys(graph: MetroGraph) -> set[str]:
    """Return station/port ids that are fan-out divergence anchors.

    See :func:`_divergence_target_successors` for the qualifying criteria.
    """
    return set(_divergence_target_successors(graph))


def _join_ids_by_branch_set(
    convergence_sources: dict[str, list[str]],
) -> dict[frozenset[str], str]:
    """Invert ``convergence_sources`` to ``{frozenset(source_ids): join_id}``.

    Shared by the fork side (:func:`_divergence_midpoint_targets`) and the
    ``#1595`` runtime guard: both ask "does this fork's target set exactly
    match some join's source set", i.e. do these branches diverge from one
    hub and reconverge on one join.
    """
    return {frozenset(srcs): join_id for join_id, srcs in convergence_sources.items()}


def _branches_fork_only_from(
    graph: MetroGraph, hub_id: str, branch_ids: Iterable[str]
) -> bool:
    """True when ``hub_id`` is the only non-port fork feeding every branch.

    A fork whose target set equals a join's source set
    (:func:`_join_ids_by_branch_set`) is one genuine diamond only when no branch
    is also fed by a second fork.  Two overlapping fans can share a join - one
    hub reaching every branch while another reaches a subset - so their target
    and source sets coincide without either hub being the diamond's single apex.
    Such a branch carries a non-port real-station predecessor besides ``hub_id``;
    the shared join then has no single fork centreline to agree with.
    """
    return all(
        _nonport_real_predecessors(graph, branch_id) <= {hub_id}
        for branch_id in branch_ids
    )


def _evenly_spaced_ys(ys: list[float]) -> list[float] | None:
    """Sorted distinct Ys from *ys* if every value is distinct and they are
    evenly spaced by one constant step; ``None`` otherwise.

    ``None`` covers two disqualifying shapes: two branches stacked on the
    same row (not distinct), and branches spread across rows at irregular
    gaps (distinct but not one constant step) - neither has a single
    well-defined pitch to centre a hub against.
    """
    rounded = [round(y, 3) for y in ys]
    distinct = sorted(set(rounded))
    if len(distinct) != len(rounded) or len(distinct) < 2:
        return None
    steps = {round(b - a, 3) for a, b in zip(distinct, distinct[1:])}
    return distinct if len(steps) == 1 else None


def _divergence_midpoint_targets(
    graph: MetroGraph, convergence_sources: dict[str, list[str]]
) -> dict[str, list[str]]:
    """Divergence anchors from :func:`_divergence_target_successors` narrowed
    to the fork side of a genuine fork/join diamond, already sitting at its
    targets' midpoint.

    Scoped to ``graph.diamond_style == "symmetric"``, matching every other
    half-grid compaction mechanism in this module - a map that never opted
    into symmetric styling keeps its fork hub wherever grid-snap put it.

    Within a symmetric-styled graph, two further conditions must both hold,
    so a fan-out that merely happens to sit between its targets numerically
    is not mistaken for a symmetric diamond:

    * The target set must exactly match a join's source set (see
      :func:`_join_ids_by_branch_set`) - i.e. every target reconverges on one
      shared downstream join, the way a diamond's branches do.  This excludes
      a fan-out where one target continues the trunk onward and another is an
      unrelated terminus: they never share a join, so they never match here.
    * The hub's own Y must already sit at the midpoint of its (pre-snap)
      target Ys, mirroring the symmetric guard :func:`_convergence_source_ys`
      applies on the converging side - a fan-out deliberately biased toward
      one branch is excluded even when it does feed a shared join.

    Target Ys read here are pre-snap and may carry sub-pixel jitter, so
    distinctness is checked directly rather than via :func:`_evenly_spaced_ys`
    (whose stricter constant-step requirement wants clean, grid-snapped
    input); that stricter check runs post-snap, in
    :func:`_restore_divergence_midpoints`.
    """
    if graph.diamond_style != "symmetric":
        return {}
    join_by_branch_set = _join_ids_by_branch_set(convergence_sources)
    centred: dict[str, list[str]] = {}
    for src_id, tgt_ids in _divergence_target_successors(graph).items():
        if frozenset(tgt_ids) not in join_by_branch_set:
            continue
        st = graph.stations.get(src_id)
        if st is None:
            continue
        tgt_ys = [graph.stations[tid].y for tid in tgt_ids if tid in graph.stations]
        if len({round(y, 3) for y in tgt_ys}) != len(tgt_ys):
            continue
        midpoint = (max(tgt_ys) + min(tgt_ys)) / 2.0
        if abs(st.y - midpoint) < 1.0:
            centred[src_id] = tgt_ids
    return centred


def _trunk_neighbours(graph: MetroGraph, node_id: str, forward: bool) -> set[str]:
    """Neighbours of *node_id* one step ``forward`` (or back), ports included.

    Junctions are transparent, so a connection routed through a bundle junction
    resolves to the station on its far side.  Ports stay in the result, unlike
    :func:`_divergence_target_successors` and :func:`_convergence_source_ys`
    which want only the fan's real branch stations: a section's own LR/RL port
    is a station on the trunk like any other, and a caller tracing a run along
    that trunk has to see it.
    """
    junction_ids = graph.junction_ids
    edges = graph.edges_from(node_id) if forward else graph.edges_to(node_id)
    neighbours: set[str] = set()
    for edge in edges:
        other = edge.target if forward else edge.source
        if other in junction_ids:
            hops = graph.edges_from(other) if forward else graph.edges_to(other)
            neighbours.update(e.target if forward else e.source for e in hops)
        else:
            neighbours.add(other)
    return neighbours


def _collinear_trunk_run(graph: MetroGraph, anchor_id: str, forward: bool) -> list[str]:
    """Stations and ports on an unbranched run out of *anchor_id*, at its Y.

    Walks away from the anchor while each successive node is a pass-through -
    exactly one connection back toward the anchor and at most one onward - and
    sits on the anchor's own Y.  Such a node carries the anchor's track and
    nothing else, so moving the anchor's centreline has to move it too or the
    run kinks.

    The walk stops at the first node that branches, that is fed from elsewhere,
    or that sits on a different Y: each of those has geometry of its own that a
    centreline shift would break rather than preserve.
    """
    anchor = graph.stations.get(anchor_id)
    if anchor is None:
        return []
    run: list[str] = []
    seen = {anchor_id}
    current = anchor_id
    onward = _trunk_neighbours(graph, current, forward)
    while len(onward) == 1:
        nxt = next(iter(onward))
        st = graph.stations.get(nxt)
        if nxt in seen or st is None or st.off_track or st.is_hidden:
            break
        if abs(st.y - anchor.y) > SAME_COORD_TOLERANCE:
            break
        if _trunk_neighbours(graph, nxt, not forward) != {current}:
            break
        onward = _trunk_neighbours(graph, nxt, forward)
        if len(onward) > 1:
            break
        run.append(nxt)
        seen.add(nxt)
        current = nxt
    return run


def _centreline_trunk_followers(
    graph: MetroGraph,
    divergence_targets: dict[str, list[str]],
    convergence_sources: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Return ``{fork_hub_id: [ids that must ride its centreline]}``.

    A fork hub :func:`_divergence_midpoint_targets` keeps centred, and the join
    closing the same diamond, both sit on one centreline.  The trunk reaching
    the hub from upstream and leaving the join downstream is on that centreline
    too - including the section's own LR/RL ports, which are just pass-throughs
    on it - so it belongs to the same rigid group (issue #1617).  Keyed by the
    fork hub because that is the station whose recentring decides where the
    centreline lands.
    """
    join_by_branch_set = _join_ids_by_branch_set(convergence_sources)
    followers: dict[str, list[str]] = {}
    for hub_id, tgt_ids in divergence_targets.items():
        chain = _collinear_trunk_run(graph, hub_id, forward=False)
        join_id = join_by_branch_set.get(frozenset(tgt_ids))
        if join_id is not None:
            chain += _collinear_trunk_run(graph, join_id, forward=True)
        if chain:
            followers[hub_id] = chain
    return followers


def _real_predecessors(graph: MetroGraph, target_ids: set[str]) -> set[str]:
    """Real-station predecessors of ``target_ids``, seen through junctions.

    A junction between a producer and the target is transparent: the producer
    one step further back is returned in its place, so a fan fed through a single
    bundle junction resolves to its source station.
    """
    preds: set[str] = set()
    for tid in target_ids:
        preds |= _trunk_neighbours(graph, tid, forward=False)
    return preds


def _redistribute_fanout_siblings(graph: MetroGraph, y_spacing: float) -> None:
    """Symmetrically distribute fan-out siblings around a trunk junction.

    Active when ``graph.center_ports`` is True.  For each LR/RL section
    in the grid, iterate by column: a column qualifies as a fan-out
    junction when it has exactly one station whose line set equals the
    section's full LEFT/RIGHT bundle (the trunk junction) AND at least
    one sibling whose line set is a strict subset of the bundle.

    In those columns, the trunk station is pinned at its current Y and
    the strict-subset siblings, ordered by their structural track, are
    redistributed in alternating slots ``+1, -1, +2, -2, ...`` at
    ``y_spacing`` pitch above and below it.  Ordering by track (rather
    than current Y) makes the slot assignment invariant under prior
    placement, so re-applying the phase is a no-op.

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
    planned_ids = planned_fan_layout_station_ids(graph)

    for section in graph.sections.values():
        if (
            section.id not in grid_sec_ids
            or section.direction not in ("LR", "RL")
            or section.bbox_h <= 0
        ):
            continue
        trunk = _section_fan_trunk_lines(graph, section)
        if not trunk:
            continue
        port_ids = section.port_ids

        # Group non-port, on-track stations by column x.  Off-track
        # stations (file inputs lifted above their consumer) are placed
        # by ``_lift_off_track_stations`` and must not occupy a column
        # slot here.
        cols: dict[float, list[str]] = defaultdict(list)
        for sid in section.station_ids:
            if sid in port_ids or sid in planned_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.off_track:
                continue
            cols[round(st.x, 3)].append(sid)

        for sids in cols.values():
            trunks = [s for s in sids if set(graph.station_lines(s)) >= trunk]
            if len(trunks) != 1:
                continue
            trunk_sid = trunks[0]
            port_trunk = _section_lr_port_anchor_y(graph, section)
            trunk_y = (
                port_trunk if port_trunk is not None else graph.stations[trunk_sid].y
            )
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
                and set(graph.station_lines(s)) < trunk
                and graph.edges_to(s)
            ]
            if not siblings:
                continue
            siblings.sort(key=lambda s: (graph.stations[s].track, s))
            for i, sid in enumerate(siblings, 1):
                k = (i + 1) // 2
                sign = 1 if (i % 2 == 1) else -1
                graph.stations[sid].y = trunk_y + sign * k * y_spacing


def _is_in_section_on_track(st: Station | None, section_id: str | None) -> bool:
    """True when ``st`` is a real on-track member of ``section_id``."""
    return (
        st is not None
        and not st.is_port
        and not st.is_hidden
        and not st.off_track
        and st.section_id == section_id
    )


def _is_in_section_track_or_hidden(st: Station | None, section_id: str | None) -> bool:
    """True when ``st`` is an on-track or hidden member of ``section_id``.

    A hidden merge node carries no glyph but is a real convergence point in the
    topology, so reconvergence traversal counts it while the rest of the layout
    (which keys off :func:`_is_in_section_on_track`) leaves it invisible.
    """
    return (
        st is not None
        and not st.is_port
        and not st.off_track
        and st.section_id == section_id
    )


def _symfan_branches_hub(
    graph: MetroGraph, section: Section
) -> tuple[list[Station], Station | None] | None:
    """Identify a section's 2-branch symmetric fan, if it has one.

    Returns ``(branches, hub)`` where ``branches`` are the two on-track
    branch stations sharing one X column and ``hub`` is the single in-section
    on-track source feeding both (or ``None`` for a fan with no in-section
    source, e.g. fed directly from the entry port).  Returns ``None`` when the
    section is not a clean 2-branch symfan.

    Two shapes qualify:

    - Exactly two branch stations sharing a column, with no in-section source
      among them.  Pure file endpoints are excluded from the branch count, but
      file-icon hubs remain structural fan members.
    - An in-section non-terminus source feeding exactly two equal-sibling
      branches (identical line sets): the source is excluded from the branch
      count as the hub.  The equal-sibling requirement keeps genuine
      trunk-continuation fans (one branch carrying the onward bundle, the other
      a strict subset) out of this path.

    ``hub`` is reported only when a single in-section on-track source feeds
    both equal-sibling branches, so callers can centre it between them.
    """
    port_ids = section.port_ids
    planned_ids = planned_fan_layout_station_ids(graph)
    nonterm: list[Station] = []
    has_off_track = False
    by_col: dict[float, int] = defaultdict(int)
    for sid in section.station_ids:
        if sid in port_ids or sid in planned_ids:
            continue
        st = graph.stations.get(sid)
        if st is None or st.is_port or st.is_hidden:
            continue
        if st.off_track:
            has_off_track = True
            continue
        if st.is_terminus and not graph.is_hub(sid):
            # Pure file endpoints are not branch participants; a source icon
            # is recovered as the hub below. File-icon hubs participate in the
            # section topology.
            continue
        nonterm.append(st)
        by_col[round(st.x, 3)] += 1
    if has_off_track:
        return None

    hub: Station | None = None
    branches = nonterm
    if len(nonterm) == 3:
        for cand in nonterm:
            others = [s for s in nonterm if s is not cand]
            if all(_real_predecessors(graph, {o.id}) == {cand.id} for o in others):
                hub = cand
                branches = others
                break
        if hub is None:
            return None

    if len(branches) != 2:
        return None
    if abs(branches[0].x - branches[1].x) >= SAME_COORD_TOLERANCE:
        return None
    # Reject a third branch sharing the branch column, or a hub sitting in it:
    # the section height must be bounded by the two straddling branches alone.
    if not all(count <= 2 for count in by_col.values()):
        return None

    # A hub is valid only between equal-sibling branches; that requirement also
    # excludes trunk-continuation fans where one branch carries the onward
    # bundle and the other a strict subset.
    lines_equal = set(graph.station_lines(branches[0].id)) == set(
        graph.station_lines(branches[1].id)
    )
    if hub is not None:
        if not lines_equal:
            return None
    elif lines_equal:
        # Promote a shared upstream in-section source (e.g. a terminus source
        # icon excluded from the branch count) to the hub.
        preds = _real_predecessors(graph, {branches[0].id})
        if len(preds) == 1 and preds == _real_predecessors(graph, {branches[1].id}):
            src = graph.stations.get(next(iter(preds)))
            if _is_in_section_on_track(src, section.id):
                hub = src

    return branches, hub


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
        result = _symfan_branches_hub(graph, section)
        if result is None:
            continue
        branches, hub = result

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

        # The fan's source hub (the station feeding both branches) sits on this
        # same local frame, so the row-grid snap must leave it there too rather
        # than dragging it onto a foreign row origin.  Restrict to in-section
        # branch predecessors: downstream terminus icons (file outputs) are off
        # the frame and snap normally.
        branch_ids = {b.id for b in branches}
        for src_id in _real_predecessors(graph, branch_ids):
            if src_id in branch_ids:
                continue
            if _is_in_section_on_track(graph.stations.get(src_id), section.id):
                graph.symfan_trunk_station_ids.add(src_id)

        # A single in-section source feeding both equal-sibling branches is
        # centred between them, so the fork is a balanced Y-split rather than
        # collinear with one branch while the other peels off.
        if hub is not None:
            hub.y = trunk_y

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
            if delta > SAME_COORD_TOLERANCE:
                section.bbox_y = new_top
                section.bbox_h = max(0.0, section.bbox_h - delta)


def _section_symfan_uses_half_grid(graph: MetroGraph, section: Section) -> bool:
    """Return True when a section's symfan should use half-pitch offsets.

    True when :func:`_symfan_branches_hub` classifies the section as a 2-branch
    symmetric fan.  The two branch stations are then placed at
    ``trunk_y +/- 0.5 * y_spacing`` instead of the default
    ``trunk_y +/- 1 * y_spacing``, so the section needs only one vertical grid
    unit instead of two.  The branches sit at half-pitch relative to the row
    grid; ``_snap_all_y_to_grid`` skips them via
    ``graph.half_grid_station_ids``.
    """
    return _symfan_branches_hub(graph, section) is not None


def _section_has_symmetric_entry_fork(graph: MetroGraph, section: Section) -> bool:
    """Return True when the section carries a ``diamond_style: symmetric``
    two-way fork whose branches share a column and never reconverge.

    This is the fork ``_recenter_full_bundle_columns`` compacts onto half-pitch
    (leaving the trunk row empty between the branches) and whose dead-end
    continuation ``_carry_symmetric_branch_continuations`` draws onto the branch
    track.  The non-reconverging requirement distinguishes it from an in-section
    fork-join diamond (whose branches mirror about their reconvergence station,
    not the empty trunk), which the per-diamond compaction handles instead.
    """
    if graph.diamond_style != "symmetric" or section.direction not in ("LR", "RL"):
        return False
    trunk = _section_fan_trunk_lines(graph, section)
    if not trunk:
        return False
    port_ids = section.port_ids
    cols: dict[float, list[str]] = defaultdict(list)
    for sid in section.station_ids:
        if sid in port_ids:
            continue
        st = graph.stations.get(sid)
        if st is None or st.off_track:
            continue
        cols[round(st.x, 3)].append(sid)
    for sids in cols.values():
        if len(sids) != 2 or not all(
            set(graph.station_lines(s)) >= trunk for s in sids
        ):
            continue
        if not _branches_share_fork_hub(graph, sids[0], sids[1]):
            # Two column-mates fed by different stations (e.g. a producer's
            # output file beside an unrelated one) are not a fork off a single
            # hub, so the empty-trunk-row compaction has no hub centreline to
            # mirror about.
            continue
        if _branches_reconverge(graph, section, sids[0], sids[1]):
            continue
        if _fork_hub_bypasses_trunk_to_exit(graph, section, sids[0], sids[1]):
            continue
        return True
    return False


def _fork_hubs(graph: MetroGraph, a: str, b: str) -> set[str]:
    """Direct predecessors shared by *a* and *b* - the fork hubs feeding both."""
    return {e.source for e in graph.edges_to(a)} & {e.source for e in graph.edges_to(b)}


def _branches_share_fork_hub(graph: MetroGraph, a: str, b: str) -> bool:
    """True when *a* and *b* have a common direct predecessor - one fork hub."""
    return bool(_fork_hubs(graph, a, b))


def _fork_hub_bypasses_trunk_to_exit(
    graph: MetroGraph, section: Section, a: str, b: str
) -> bool:
    """True when the branches' shared fork hub also runs a line straight down the
    trunk row, past the branch column, to a boundary exit of the section.

    The half-pitch compaction leaves the trunk row empty between the two branches
    so the bubble is one grid unit tall.  When the fork hub instead carries a line
    straight on down that trunk row to the section's LR exit -- an edge from the
    hub to the exit port itself, bypassing both branches -- the row is occupied,
    and compacting would crowd that bypass bundle between the branches.  Such a
    fork keeps full pitch.

    The signal is a *direct* hub-to-exit-port edge, which lands on the trunk row
    by construction; a hub output that instead threads through a third off-trunk
    fan branch on its way out is not trunk traffic and does not disqualify the
    pair.
    """
    exit_ports = {
        pid
        for pid in section.exit_ports
        if (port := graph.ports.get(pid)) is not None
        and port.side in (PortSide.LEFT, PortSide.RIGHT)
    }
    if not exit_ports:
        return False
    return any(
        edge.target in exit_ports
        for hub in _fork_hubs(graph, a, b)
        for edge in graph.edges_from(hub)
    )


def _branches_reconverge(graph: MetroGraph, section: Section, a: str, b: str) -> bool:
    """True when *a* and *b* reach a common station inside *section*."""

    def _reachable(start: str) -> set[str]:
        seen: set[str] = set()
        stack = [start]
        while stack:
            cur = stack.pop()
            for e in graph.edges_from(cur):
                t = graph.station_for_edge_target(e)
                if t.is_port or t.section_id != section.id or e.target in seen:
                    continue
                seen.add(e.target)
                stack.append(e.target)
        return seen

    return bool(_reachable(a) & _reachable(b))


def _iter_fork_join_diamonds(
    graph: MetroGraph,
) -> Iterator[tuple[Station, Station, Station, Station]]:
    """Yield ``(fork, branch, branch, join)`` for each 2-way fork-join
    diamond whose trunk runs straight through.

    A diamond is a fork F with exactly two successors B1, B2 that share F
    as their only predecessor and rejoin at a single common successor J,
    with neither F nor J a port and the trunk running straight through F
    and J on a single row.  The two branches are yielded in id order, not
    ordered by Y, and may be ports / hidden / off-track / column-mismatched;
    callers add whatever further filtering they need.

    The shared structural primitive behind both
    ``_guard_symmetric_diamond_branches_straddle_trunk`` (which guards every
    such diamond against collapse onto the trunk) and
    ``_iter_symmetric_diamonds`` (which narrows to clean column-aligned
    diamonds for the half-pitch compaction).
    """
    succ: dict[str, set[str]] = defaultdict(set)
    pred: dict[str, set[str]] = defaultdict(set)
    for edge in graph.edges:
        if edge.source in graph.stations and edge.target in graph.stations:
            succ[edge.source].add(edge.target)
            pred[edge.target].add(edge.source)
    tol = SAME_COORD_TOLERANCE
    for fork, branch_ids in succ.items():
        if len(branch_ids) != 2:
            continue
        fork_st = graph.stations[fork]
        if fork_st.is_port:
            continue
        b1, b2 = sorted(branch_ids)
        if pred[b1] != {fork} or pred[b2] != {fork}:
            continue
        joins = succ.get(b1, set())
        if len(joins) != 1 or joins != succ.get(b2, set()):
            continue
        join = next(iter(joins))
        join_st = graph.stations[join]
        if join_st.is_port:
            continue
        if abs(join_st.y - fork_st.y) > tol:
            continue
        yield fork_st, graph.stations[b1], graph.stations[b2], join_st


def _iter_symmetric_diamonds(
    graph: MetroGraph,
) -> Iterator[tuple[Station, Station, Station, Station]]:
    """Yield ``(fork, branch_lo, branch_hi, join)`` for each clean 2-way
    symmetric fork-join diamond confined to one section.

    Narrows :func:`_iter_fork_join_diamonds` to diamonds where B1, B2 are
    real (non-port, non-hidden, on-track) stations sharing one section with
    F and J and sharing an X column.  ``branch_lo`` and ``branch_hi`` are
    the two branches ordered by Y.

    Shared by the half-pitch compaction phase
    (``_apply_half_grid_symmetric_diamonds``) and the grid-snap invariant
    test so both agree on which branches are legitimately half-pitch.
    """
    tol = SAME_COORD_TOLERANCE
    planned_ids = planned_fan_layout_station_ids(graph)
    for fork_st, s1, s2, join_st in _iter_fork_join_diamonds(graph):
        if {fork_st.id, s1.id, s2.id, join_st.id}.intersection(planned_ids):
            continue
        if any(s.is_port or s.is_hidden or s.off_track for s in (s1, s2)):
            continue
        # Confine the diamond to one section so the trunk anchor (the fork
        # Y) belongs to the same trunk the branches straddle.
        sec_id = fork_st.section_id
        if sec_id is None or any(st.section_id != sec_id for st in (s1, s2, join_st)):
            continue
        # A clean horizontal diamond: the branches share an X column.
        if abs(s1.x - s2.x) >= tol:
            continue
        lo, hi = (s1, s2) if s1.y <= s2.y else (s2, s1)
        yield fork_st, lo, hi, join_st


def _apply_half_grid_symmetric_diamonds(graph: MetroGraph, y_spacing: float) -> None:
    """Compact each symmetric 2-way fork-join diamond onto half-pitch offsets.

    Under ``diamond_style='symmetric'`` a clean horizontal 2-way diamond
    (see :func:`_iter_symmetric_diamonds`) otherwise straddles the trunk
    at full pitch (``trunk_y +/- y_spacing``), making the diamond's bubble
    as tall as a 3-way fan with an empty trunk row between its branches.
    This places the two branches at ``trunk_y +/- 0.5 * y_spacing`` so the
    diamond reads as a tight bubble.

    Unlike ``_apply_half_grid_2branch_symfan`` (which fires only when the
    diamond is the section's sole fan and ``center_ports`` is on), the
    decision here is per-diamond: a diamond compacts even when it shares a
    section with a wider fan - which keeps its own full-pitch slots, so the
    section height stays bounded by that fan - and regardless of
    ``center_ports``.  The branch X column and the section bbox are left
    untouched; only the two branch Ys move inward.

    Branches are marked in ``graph.half_grid_station_ids`` so the
    subsequent grid snap leaves their half-pitch offsets intact.
    Placement is idempotent (it re-derives both branch Ys from the fork
    trunk each pass), so re-running over a diamond the ``center_ports``
    section pass already compacted re-affirms the same half-pitch offsets.
    """
    if y_spacing <= 0 or graph.diamond_style != "symmetric":
        return
    for fork_st, lo, hi, _join in _iter_symmetric_diamonds(graph):
        trunk_y = fork_st.y
        lo.y = trunk_y - 0.5 * y_spacing
        hi.y = trunk_y + 0.5 * y_spacing
        graph.half_grid_station_ids.update((lo.id, hi.id))


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
    planned_ids = planned_fan_layout_station_ids(graph)

    for section in graph.sections.values():
        if (
            section.id not in grid_sec_ids
            or section.direction not in ("LR", "RL")
            or section.bbox_h <= 0
        ):
            continue
        trunk = _section_fan_trunk_lines(graph, section)
        if not trunk:
            continue
        port_ids = section.port_ids

        cols: dict[float, list[str]] = defaultdict(list)
        for sid in section.station_ids:
            if sid in port_ids or sid in planned_ids:
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
            x: [s for s in sids if set(graph.station_lines(s)) >= trunk]
            for x, sids in cols.items()
        }
        lr_port_ys = [
            graph.ports[pid].y
            for pid in port_ids
            if graph.ports.get(pid) is not None
            and graph.ports[pid].side in (PortSide.LEFT, PortSide.RIGHT)
        ]
        port_ys = lr_port_ys

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
                and set(graph.station_lines(s)) < trunk
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
            # Strict all-full columns always fire.  A mixed column (full +
            # non-source siblings) fires when it carries its own trunk anchor
            # -- two or more full-bundle stations define the trunk locally --
            # and its non-full siblings are homogeneous (one shared line-set),
            # so the minor branch slots symmetrically without splitting the
            # column's convergence into ambiguous per-line channels.  A mixed
            # column with a single full-bundle station, or heterogeneous
            # subset siblings, is left for ``_redistribute_fanout_siblings``
            # unless a separate all-full column fixes the row anchor.
            all_full = not non_full
            subset_line_sets = {frozenset(graph.station_lines(s)) for s in non_full}
            own_trunk_anchor = len(full) >= 2 and len(subset_line_sets) <= 1
            should_fire = all_full or own_trunk_anchor or any_all_full_col
            if not should_fire:
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
                continue
            participants.sort(key=lambda s: (graph.stations[s].track, s))
            n = len(participants)
            offsets = _fan_offsets(n)
            for sid, off in zip(participants, offsets):
                graph.stations[sid].y = trunk_y + off * y_spacing


def _section_row_pitch(graph: MetroGraph, section_id: str, default: float) -> float:
    """The Y-grid pitch of the row ``section_id`` belongs to.

    Reads the frozen per-row grid info recorded by ``_align_row_y_grids``.
    A row whose widest bundle inflates the slot pitch past the base
    ``y_spacing`` keeps every section, port and inter-section trunk on
    that wider pitch; fanning content at the base pitch instead would
    leave re-fanned stations a fraction of a slot off the trunk line.
    Falls back to ``default`` for sections not in a multi-section row.
    """
    require_phase_field(graph, "_row_y_grid_info")
    grid_info = graph._row_y_grid_info
    for info in grid_info.values():
        if section_id in info["section_ids"]:
            return info["slot_spacing"]
    return default


def _recenter_full_bundle_columns(graph: MetroGraph, y_spacing: float) -> None:
    """Re-fan full-bundle station columns around the row's final trunk Y.

    Late-pass companion to ``_redistribute_full_bundle_columns`` when
    ``graph.center_ports`` is set.  The early pass uses the section's
    local LR port Y as the symmetric centre, which becomes stale when
    subsequent phases shift the section relative to the row trunk (e.g.
    terminal sections whose sole LR port doesn't match the bundle line
    entering from upstream).

    Also runs standalone (without an early pass) under
    ``graph.diamond_style == 'symmetric'``: a full-bundle column fed
    directly off a section's entry port has no settled trunk Y before
    row alignment, so the early pass would only place it correctly by
    chance -- this late pass, anchored on the final row trunk, is the
    only one it needs.

    For each LR/RL grid section, locate the inter-section bundle Y from
    the entry/exit port station Y (which by this point sits on the
    row's bundle Y after row alignment).  Then re-distribute each
    column of >=2 full-bundle stations around that anchor at
    ``y_spacing`` pitch, preserving the order produced by the first
    pass.

    A ``diamond_style: symmetric`` two-way entry fork is instead compacted
    onto half-pitch offsets (``trunk +/- 0.5 pitch``) and its branches marked
    half-grid.

    No-op when the existing layout is already symmetric around the anchor.
    """
    grid_sec_ids = _grid_group_section_ids(graph)
    if not grid_sec_ids:
        return
    planned_ids = planned_fan_layout_station_ids(graph)

    for section in graph.sections.values():
        if (
            section.id not in grid_sec_ids
            or section.direction not in ("LR", "RL")
            or section.bbox_h <= 0
        ):
            continue
        # Under ``diamond_style: symmetric`` (without ``center_ports``) this is
        # the sole pass, so it must touch only the two-way symmetric entry fork
        # it exists to compact.  Re-fanning every full-bundle column here spreads
        # unrelated columns (e.g. a 5-way fan-in) symmetrically about their
        # trunk and inflates the section, so all other columns are left as the
        # section layout placed them.
        if not graph.center_ports and not _section_has_symmetric_entry_fork(
            graph, section
        ):
            continue
        trunk = _section_fan_trunk_lines(graph, section)
        if not trunk:
            continue
        port_ids = section.port_ids

        cols: dict[float, list[str]] = defaultdict(list)
        for sid in section.station_ids:
            if sid in port_ids or sid in planned_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.off_track:
                continue
            cols[round(st.x, 3)].append(sid)

        def _has_pred(sid: str) -> bool:
            return bool(graph.edges_to(sid))

        full_by_col = {
            x: [s for s in sids if set(graph.station_lines(s)) >= trunk]
            for x, sids in cols.items()
        }

        # Trunk anchor: prefer the LR/RL entry (then exit) port station Y,
        # which after row alignment sits on the row's bundle line.  Fall back
        # to a single-station full-bundle column (natural pass-through), then
        # the median Y.
        anchor_y = _section_lr_port_anchor_y(graph, section)
        pitch = _section_row_pitch(graph, section.id, y_spacing)
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
                    and set(graph.station_lines(s)) < trunk
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
            participants.sort(key=lambda s: (graph.stations[s].track, s))
            n = len(participants)
            # A ``diamond_style: symmetric`` two-way fork compacts onto
            # half-pitch offsets (trunk +/- 0.5 pitch) so it consumes one grid
            # unit rather than straddling a full empty trunk row; the branches
            # are marked so the grid snap leaves the half-offsets intact.  A fork
            # whose hub runs a line straight down the trunk row to a section exit
            # keeps full pitch: that bypass bundle occupies the row the
            # compaction would empty, so squeezing the branches half a unit apart
            # would crowd it (see :func:`_fork_hub_bypasses_trunk_to_exit`).
            half = (
                graph.diamond_style == "symmetric"
                and n == 2
                and not _fork_hub_bypasses_trunk_to_exit(
                    graph, section, participants[0], participants[1]
                )
            )
            if half:
                # Orient so a branch bearing an off-track file above the trunk
                # fans to the bottom slot (and one below the trunk to the top).
                # The file is offset away from its producer, so an above-trunk
                # file on an up-fanned branch protrudes past the row's top band
                # and shifts the whole map off the trunk; fanning that branch
                # down keeps the file within the section's existing band.
                sides = [
                    _fork_offtrack_side(graph, section, p, anchor_y)
                    for p in participants
                ]
                if sides[0] == -1 or sides[1] == 1:
                    participants.reverse()
            scale = 0.5 if half else 1.0
            offsets = _fan_offsets(n)
            for sid, off in zip(participants, offsets):
                graph.stations[sid].y = anchor_y + off * scale * pitch
                if half:
                    graph.half_grid_station_ids.add(sid)


def _carry_symmetric_branch_continuations(
    graph: MetroGraph, section_y_padding: float = SECTION_Y_PADDING
) -> None:
    """Draw a fanned branch's in-section continuation onto the branch's track.

    Under ``diamond_style: symmetric`` the two-way fork's branch stations are
    fanned off the trunk (see :func:`_recenter_full_bundle_columns`), but a
    branch that continues to further stations *inside* the section leaves those
    successors on the trunk, so the branch humps out and immediately back.

    Walk each fanned branch's sole-successor chain and pull every in-section
    successor onto the branch's Y, so a dead-end branch stays fanned for its
    whole length.  The walk stops as soon as a station's forward path leaves the
    branch -- it gains a second predecessor, forks, or continues only through an
    exit port -- so a branch that exits the section is left to fall back to the
    trunk and meet its exit port there.
    """
    if graph.diamond_style != "symmetric":
        return
    planned_ids = planned_fan_layout_station_ids(graph)
    for section in graph.sections.values():
        if section.direction not in ("LR", "RL"):
            continue
        carried: list[str] = []
        for sid in section.station_ids:
            if sid not in graph.half_grid_station_ids or sid in planned_ids:
                continue
            branch = graph.stations.get(sid)
            if branch is None:
                continue
            carried.extend(_pull_continuation_onto(graph, section, branch))
        if not carried:
            continue
        graph.half_grid_station_ids.update(carried)
        content_ys = [
            graph.stations[s].y
            for s in section.station_ids
            if s in graph.stations and not graph.stations[s].is_port
        ]
        _grow_section_bbox_over_ys(graph, section, content_ys, section_y_padding)


def _section_lr_entry_port(graph: MetroGraph, section: Section) -> str | None:
    """The id of *section*'s first LEFT/RIGHT entry port, or ``None``."""
    return next(
        (
            pid
            for pid in section.entry_ports
            if (p := graph.ports.get(pid)) is not None
            and p.side in (PortSide.LEFT, PortSide.RIGHT)
        ),
        None,
    )


def _symfan_entry_port_feeder_y(
    graph: MetroGraph, section: Section
) -> tuple[str, float] | None:
    """``(entry_port_id, feeder_exit_y)`` for a symmetric entry fork whose LR
    entry port is fed by exactly one same-row section's exit port; else ``None``.

    Shared by the placement pass that slides the section onto that feeder's Y
    and the guard that checks it stayed there, so the two read one relation and
    can't drift.  A cross-row feeder (its bundle wraps between rows) is excluded:
    only a horizontal same-row run should pin the section's vertical position.
    """
    if section.direction not in ("LR", "RL") or not _section_has_symmetric_entry_fork(
        graph, section
    ):
        return None
    entry_port = _section_lr_entry_port(graph, section)
    if entry_port is None:
        return None
    feeder_ys = {
        round(src.y, 1)
        for e in graph.edges_to(entry_port)
        if (src := graph.station_for_edge_source(e)).is_port
        and (fsec := graph.sections.get(src.section_id or "")) is not None
        and fsec.grid_row == section.grid_row
        and src.id in fsec.exit_ports
    }
    if len(feeder_ys) != 1:
        return None
    return entry_port, feeder_ys.pop()


def _in_section_continuation_chain(
    graph: MetroGraph, section: Section, start_id: str
) -> list[str]:
    """The linear tail of on-track stations continuing *start_id*'s branch.

    Each step is the sole on-track in-section successor whose only in-section
    predecessor is the current station -- a branch that neither forks nor merges
    before leaving the section.  Off-track file successors are not part of the
    chain; the walk stops at the last on-track station.
    """

    def predecessors(sid: str) -> list[str]:
        return sorted(
            {
                e.source
                for e in graph.edges_to(sid)
                if not (s := graph.station_for_edge_source(e)).is_port
                and s.section_id == section.id
            }
        )

    chain: list[str] = []
    current = start_id
    seen = {start_id}
    while True:
        succ = [
            s
            for s in _in_section_ontrack_successors(graph, section, current)
            if s not in seen
        ]
        if len(succ) != 1 or predecessors(succ[0]) != [current]:
            break
        current = succ[0]
        chain.append(current)
        seen.add(current)
    return chain


def _in_section_ontrack_successors(
    graph: MetroGraph, section: Section, sid: str
) -> list[str]:
    """Deduped ids of *sid*'s on-track successors within *section*.

    A multi-line edge appears once per line, so the same successor is listed
    repeatedly; the set collapses those.
    """
    return sorted(
        {
            e.target
            for e in graph.edges_from(sid)
            if _is_in_section_on_track(graph.station_for_edge_target(e), section.id)
        }
    )


def _in_section_ontrack_predecessors(
    graph: MetroGraph, section: Section, sid: str
) -> list[str]:
    """Deduped ids of *sid*'s on-track predecessors within *section*.

    The reverse-direction counterpart of
    :func:`_in_section_ontrack_successors`, deduping multi-line edges the same
    way.
    """
    return sorted(
        {
            e.source
            for e in graph.edges_to(sid)
            if _is_in_section_on_track(graph.station_for_edge_source(e), section.id)
        }
    )


def _fork_offtrack_side(
    graph: MetroGraph, section: Section, branch_id: str, anchor_y: float
) -> int:
    """Which side of the trunk an off-track file on *branch_id*'s chain sits.

    Walks the branch's in-section continuation and inspects each station for an
    off-track file successor.  Returns ``-1`` when such a file sits above the
    trunk ``anchor_y``, ``+1`` when below, and ``0`` when the branch carries no
    off-track file.  Used to orient the symmetric fork so the file stays within
    the section's band.
    """
    for sid in [branch_id, *_in_section_continuation_chain(graph, section, branch_id)]:
        for e in graph.edges_from(sid):
            tgt = graph.station_for_edge_target(e)
            if tgt.off_track and tgt.section_id == section.id:
                return -1 if tgt.y < anchor_y else 1
    return 0


def _align_symfan_section_to_row_feeder(graph: MetroGraph) -> None:
    """Slide a symmetric-fork section onto its in-row feeder's exit line.

    Centering the fork on the entry port (see
    :func:`_recenter_full_bundle_columns`) can leave that port off the trunk of
    the same-row section feeding it -- the port settled on the fork midline
    rather than the horizontal bundle arriving from the left.  Translate the
    whole section (content, ports, bbox) so its entry port shares a Y with that
    feeder's exit port, straightening the inter-section run.
    """
    if graph.diamond_style != "symmetric":
        return
    for section in graph.sections.values():
        feeder = _symfan_entry_port_feeder_y(graph, section)
        if feeder is None:
            continue
        entry_port, feeder_y = feeder
        delta = feeder_y - graph.stations[entry_port].y
        if abs(delta) < 1.0:
            continue
        shift_section(graph, section, dy=delta)


def _entry_fork_join(
    graph: MetroGraph, section: Section, branches: list[str]
) -> Station | None:
    """The station a port-fed two-way fork cleanly reconverges at, or None.

    The port-fork counterpart of :func:`_iter_fork_join_diamonds`, which admits
    only station forks.  Requiring the two branches to be the join's *only*
    feeders keeps it the point where the whole bundle reunites, so no third
    feeder has a competing claim on its Y.
    """
    if len(branches) != 2:
        return None
    succs = [_in_section_ontrack_successors(graph, section, b) for b in branches]
    if succs[0] != succs[1] or len(succs[0]) != 1:
        return None
    join_id = succs[0][0]
    feeders = _in_section_ontrack_predecessors(graph, section, join_id)
    if set(feeders) != set(branches):
        return None
    return graph.stations[join_id]


def _center_lr_entry_ports_on_fork(graph: MetroGraph, y_spacing: float) -> None:
    """Centre an LR entry port, and the join it reconverges at, on its two-way fork.

    Under ``diamond_style: symmetric`` a section's LR entry port that fans into
    branches at exactly two distinct Ys should sit at their midpoint, so the
    fork reads symmetric about the incoming bundle and the run from the feeding
    section arrives straight.  Otherwise the port stays pinned to whichever
    branch the section layout seated it on (e.g. the top branch of a
    reconverging diamond), leaving the inter-section run kinked.

    When the fork reconverges at a clean in-section join, that join and its
    linear trunk continuation ride the same midpoint centreline, so the diamond
    closes symmetrically and the trunk leaves it straight.  A station fork gets
    this for free -- the join inherits the fork's own trunk track -- but a port
    fork has no station on the centreline to inherit from, so the join would
    otherwise stay on the track of whichever branch fed it first, turning the
    diamond into a lopsided fan.  The branch stations themselves keep their
    places either way.

    An off-grid midpoint (branches an odd number of slots apart) is only a
    valid seat when the fork reconverges: a non-reconverging dead-end fan
    instead keeps its port on the feeder trunk with the branches straddling
    half a slot either side, so seating the port on the off-grid midpoint there
    would drag the row's trunk off the inter-section run.
    """
    if graph.diamond_style != "symmetric":
        return
    planned_ports = planned_fan_port_ids(graph)
    for section in graph.sections.values():
        if section.direction not in ("LR", "RL"):
            continue
        pitch = _section_row_pitch(graph, section.id, y_spacing)
        for pid in section.entry_ports:
            if pid in planned_ports:
                continue
            port = graph.ports.get(pid)
            if port is None or port.side not in (PortSide.LEFT, PortSide.RIGHT):
                continue
            branches = _in_section_ontrack_successors(graph, section, pid)
            branch_ys = sorted({round(graph.stations[s].y, 1) for s in branches})
            if len(branch_ys) != 2 or pitch <= 0:
                continue
            slots = (branch_ys[1] - branch_ys[0]) / pitch
            off_grid = round(slots) % 2 != 0
            if off_grid and not _branches_reconverge(
                graph, section, branches[0], branches[1]
            ):
                continue
            midpoint = (branch_ys[0] + branch_ys[1]) / 2.0
            if abs(graph.stations[pid].y - midpoint) >= 1.0:
                _set_port_y(graph, pid, midpoint)
            join = _entry_fork_join(graph, section, branches)
            if join is not None and abs(join.y - midpoint) >= 1.0:
                join.y = midpoint
                _pull_continuation_onto(graph, section, join)
            if off_grid:
                # The port rides the off-grid midpoint centreline, so the grid
                # snap must treat it as half-grid, not re-seat it on a row.  The
                # spine it feeds is deliberately left out: the snap runs earlier
                # in the pipeline, so entries here would reach it only on a
                # subsequent layout pass, where a whole spine of them would
                # outvote the branch rows for the group's grid origin.
                graph.half_grid_station_ids.add(pid)


def _center_lr_exit_ports_on_join(graph: MetroGraph) -> None:
    """Centre an LR exit port on the two-way join that feeds it.

    The exit-side counterpart of :func:`_center_lr_entry_ports_on_fork`.  Two
    branches whose only successor is the section's flow-aligned exit port
    reunite *at* that port, so it carries the same centreline role the join
    station carries for an in-section reconvergence: seated at their midpoint,
    the diamond closes symmetrically and the run into the next section leaves
    straight.  Left unseated the port keeps whichever branch's track the
    section layout gave it -- or, when the branches hold half-pitch offsets,
    a track belonging to neither -- and both legs kink to reach it.

    Only a port seated *outside* its two feeders' span is moved.  There both
    legs leave the join turning the same way to reach it, so the port reads as
    an unexplained step above (or below) the whole join.  A port between its
    feeders is already straddled by them, and nudging it to the exact midpoint
    would trade a flat inter-section run for a step down onto the port and a
    step back up off it -- two direction changes bought with one.
    """
    if graph.diamond_style != "symmetric":
        return
    planned_ports = planned_fan_port_ids(graph)
    for section in graph.sections.values():
        direction = section.direction or "LR"
        if not lanes_run_along_y(direction):
            continue
        perpendicular = perpendicular_port_sides(direction)
        for pid in section.exit_ports:
            if pid in planned_ports:
                continue
            port = graph.ports.get(pid)
            if port is None or port.side in perpendicular:
                continue
            feeders = _exit_join_feeders(graph, section, pid)
            if feeders is None:
                continue
            low, high = sorted(graph.stations[f].y for f in feeders)
            port_y = graph.stations[pid].y
            if low - 1.0 <= port_y <= high + 1.0:
                continue
            _set_port_y(graph, pid, (low + high) / 2.0)


def _exit_join_feeders(
    graph: MetroGraph, section: Section, pid: str
) -> list[str] | None:
    """The two in-section stations that reunite at exit port *pid*, or None.

    Requiring the port to be each feeder's *only* successor keeps this the
    point where the whole bundle reunites, mirroring
    :func:`_entry_fork_join`'s claim on a join station.
    """
    feeders = _in_section_ontrack_predecessors(graph, section, pid)
    if len(feeders) != 2:
        return None
    for fid in feeders:
        if {e.target for e in graph.edges_from(fid)} != {pid}:
            return None
    return feeders


def _pull_continuation_onto(
    graph: MetroGraph, section: Section, branch: Station
) -> list[str]:
    """Set each in-section continuation station of *branch* to its Y; return them.

    Off-track file successors are not part of the continuation: they are placed
    by the off-track lift pass at an intentional offset from their producer, so
    carrying one onto the branch track would drop the file onto its producer.
    """
    carried = _in_section_continuation_chain(graph, section, branch.id)
    for sid in carried:
        graph.stations[sid].y = branch.y
    return carried


def _section_occupants(graph: MetroGraph, section: Section) -> list[Station]:
    """Stations or routed bypass lanes that can occupy a row slot.

    A hidden bypass helper has no marker, but its line is drawn through the
    helper coordinate.  That routed lane is a real mirror for a half-pitch
    station and prevents the visible branch from being expanded as an orphan.
    """
    return [
        st
        for sid in section.station_ids
        if (st := graph.stations.get(sid)) is not None
        and not st.is_port
        and (not st.is_hidden or st.bypasses_station_id is not None)
    ]


def _grow_section_bbox_over_ys(
    graph: MetroGraph, section: Section, ys: list[float], section_y_padding: float
) -> None:
    """Grow *section*'s bbox so *ys* sit inside it with padding on both edges."""
    if not ys:
        return
    grow_section_bbox_min_edge(graph, section, "y", min(ys) - section_y_padding)
    grow_section_bbox_max_edge(graph, section, "y", max(ys) + section_y_padding)


def _half_grid_frame(
    graph: MetroGraph, section: Section, y_spacing: float
) -> tuple[float, float] | None:
    """``(anchor_y, row_pitch)`` for *section*, or None when it has no frame.

    The anchor is the LR/RL port Y that the half-pitch passes fan about, so a
    vertical flow (which stacks its lines along X) and a section without an
    LR/RL port both have nothing to measure against.
    """
    if lanes_run_along_x(section.direction):
        return None
    anchor = _section_lr_port_anchor_y(graph, section)
    if anchor is None:
        return None
    pitch = _section_row_pitch(graph, section.id, y_spacing)
    return (anchor, pitch) if pitch > 0 else None


def _straddles_nothing(
    station: Station, anchor: float, pitch: float, occupants: list[Station]
) -> bool:
    """True when *station* sits half a pitch off *anchor* with its mirror empty.

    A half-pitch offset is meaningful only as one side of a pair straddling the
    anchor, so the slot at the mirrored offset has to be occupied for the offset
    to buy anything.  Stations a whole number of rows from the anchor are
    already on the grid and never qualify.
    """
    offset = station.y - anchor
    if abs(abs(offset) - 0.5 * pitch) > SAME_COORD_TOLERANCE:
        return False
    mirror = anchor - offset
    return not any(
        other is not station and abs(other.y - mirror) < SAME_COORD_TOLERANCE
        for other in occupants
    )


def _expand_orphaned_half_grid_stations(
    graph: MetroGraph,
    y_spacing: float,
    section_y_padding: float = SECTION_Y_PADDING,
) -> None:
    """Seat on a full row any half-pitch station whose pair partner moved away.

    ``_recenter_full_bundle_columns``, ``_apply_half_grid_2branch_symfan`` and
    ``_apply_half_grid_symmetric_diamonds`` place both members of a two-way
    fork at ``anchor +/- 0.5 * pitch`` together, and
    ``_carry_symmetric_branch_continuations`` marks each member's onward chain
    on that member's Y.  ``_align_terminus_to_upstream`` is entitled to pull a
    terminus member onto its producer's Y, and :func:`_straddles_nothing`
    detects the partner left behind.

    Seating derives the new Y from the row pitch rather than doubling the
    measured offset, so the branch lands exactly on the row even though it
    qualified within a tolerance band.

    An off-track icon counts as a straddle partner while never being seated
    itself: the off-track lift owns its Y, so it can hold a slot it must not be
    moved out of.

    A fork hub :func:`_restore_divergence_midpoints` centred on its targets'
    midpoint is a solo centreline anchor, not one side of a two-way pair - it
    has no mirror station to go looking for, so it is exempt here regardless
    of what ``_half_grid_frame`` reports for its section.  The trunk run that
    rides the same centreline (:func:`_centreline_trunk_followers`) is exempt
    for the same reason, and seating one of its members on a row would reopen
    the seam the centreline closed.
    """
    require_phase_field(graph, "half_grid_station_ids")
    half_grid = graph.half_grid_station_ids
    if not half_grid:
        return
    planned_ids = planned_fan_layout_station_ids(graph)
    convergence_sources = _convergence_source_ys(graph)
    divergence_targets = _divergence_midpoint_targets(graph, convergence_sources)
    centreline_ids = set(divergence_targets)
    for follower_ids in _centreline_trunk_followers(
        graph, divergence_targets, convergence_sources
    ).values():
        centreline_ids.update(follower_ids)
    for section in graph.sections.values():
        marked = [
            sid
            for sid in section.station_ids
            if sid in half_grid and sid not in centreline_ids and sid not in planned_ids
        ]
        if not marked:
            continue
        frame = _half_grid_frame(graph, section, y_spacing)
        if frame is None:
            continue
        anchor, pitch = frame
        occupants = _section_occupants(graph, section)
        moved_ys: list[float] = []
        for sid in marked:
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.off_track or st.is_hidden:
                continue
            if not _straddles_nothing(st, anchor, pitch, occupants):
                continue
            st.y = anchor + math.copysign(pitch, st.y - anchor)
            half_grid.discard(sid)
            moved_ys.append(st.y)
        if not moved_ys:
            continue
        # Only the expanded branch can have crossed an edge; sizing against the
        # whole section instead would hand the bbox slack that the row-compact
        # passes then take up by dragging unrelated content off its row.
        _grow_section_bbox_over_ys(graph, section, moved_ys, section_y_padding)
