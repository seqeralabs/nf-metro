"""Snap station and port Ys back to the row grid pitch after fractional shifts."""

from __future__ import annotations

import math
from collections import Counter

from nf_metro.layout.constants import (
    CANVAS_GRID_SHIFT_THRESHOLD,
    COORD_TOLERANCE_FINE,
    SAME_COORD_TOLERANCE,
)
from nf_metro.layout.geometry import lanes_run_along_y
from nf_metro.layout.phase_state import require_phase_field
from nf_metro.layout.phases.canvas import _canvas_top_preserved, translate_graph
from nf_metro.layout.phases.fan_bundles import (
    _centreline_trunk_followers,
    _convergence_source_ys,
    _divergence_midpoint_targets,
    _entry_fan_centre_ports,
    _evenly_spaced_ys,
    _fan_reconvergence_joins,
    _station_rooted_fans,
    _symmetric_reconvergence_joins,
)
from nf_metro.layout.phases.junctions import _position_junctions
from nf_metro.layout.phases.ports import _set_port_y
from nf_metro.parser.model import MetroGraph, PortSide


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
    targets above and below) are snapped to grid, then - for the narrower
    case of a ``diamond_style: symmetric`` diamond whose targets exactly
    match a join's source set - recentred on the post-snap midpoint of
    those targets by :func:`_restore_divergence_midpoints`, so the fork hub
    agrees with its join hub on one centreline. Unlike the convergence
    restore above, this recentring only fires when the targets are distinct
    and evenly spaced; a diamond that doesn't qualify keeps its hub on the
    grid-snapped slot. Between the snap and any recentring the hub briefly
    sits flat with whichever target it snapped nearest; the downstream
    column-centring pass recognises that flat connection as an artefact of
    the snap rather than a topological chain, and lets the target's column
    centre on its own merits.

    Groups with no on-grid majority are left untouched.
    """
    if y_spacing <= 0:
        return
    # Map each convergence station/port to the set of source Ys it
    # converges (recorded pre-snap so the midpoint can be restored
    # after sources move).
    convergence_sources = _convergence_source_ys(graph)
    # A trunkless symmetric entry fan's reconvergence join is a convergence the
    # midpoint restore must also protect and recentre, even though one arm's
    # extra hop keeps it out of the ordinary source-midpoint set above.
    for join_id, src_ids in _symmetric_reconvergence_joins(graph).items():
        convergence_sources.setdefault(join_id, src_ids)
    # Under center_ports a trunkless fan-in entry port rides the same
    # protect-and-restore onto the midpoint of the targets it serves.
    entry_fan_ports = _entry_fan_centre_ports(graph)
    for port_id, target_ids in entry_fan_ports.items():
        convergence_sources.setdefault(port_id, target_ids)
    # Same idea for the diverging side: record each fork hub's target set
    # pre-snap so its midpoint can be restored once the targets have moved.
    # Narrowed to hubs already centred pre-snap (see
    # ``_divergence_midpoint_targets``) so a fan-out deliberately biased
    # toward one branch is left on its snapped grid slot.
    divergence_targets = _divergence_midpoint_targets(graph, convergence_sources)
    # The trunk running into the fork hub and out of the join shares their
    # centreline pre-snap, and must be recorded before the snap rounds each
    # member to its own row and that co-linearity stops being readable.
    trunk_followers = _centreline_trunk_followers(
        graph, divergence_targets, convergence_sources
    )
    groups: dict[object, tuple[float, list[str]]] = {}
    grouped_ids: set[str] = set()
    require_phase_field(graph, "_row_y_grid_info")
    for row, info in (graph._row_y_grid_info or {}).items():
        pitch = info.get("slot_spacing", y_spacing)
        sec_ids = list(info.get("section_ids", []))
        groups[("row", row)] = (pitch, sec_ids)
        grouped_ids.update(sec_ids)
    for sec in graph.sections.values():
        if sec.id not in grouped_ids:
            groups[("solo", sec.id)] = (y_spacing, [sec.id])

    for pitch, sec_ids in groups.values():
        _snap_group_to_grid(graph, pitch, sec_ids, convergence_sources)

    section_pitch = {
        sec_id: pitch for pitch, sec_ids in groups.values() for sec_id in sec_ids
    }
    _restore_convergence_midpoints(graph, convergence_sources, section_pitch)
    _restore_divergence_midpoints(graph, divergence_targets, trunk_followers)
    _restore_partial_trunk_descents(graph)
    _register_half_grid_entry_fan_ports(graph, entry_fan_ports)


def _register_half_grid_reconvergence_branches(graph: MetroGraph) -> None:
    """Register a station-rooted reconvergence fan's half-pitch branch tracks.

    A fan rooted at an internal station reconverges onto a hidden merge node
    seated on the branch midpoint (:func:`_symmetric_reconvergence_joins`).
    When the section's trunk anchor port itself rides a half-grid centreline,
    that midpoint - and the whole branch column mirrored about it - sits half a
    pitch off the trunk grid, the same genuine half-pitch a fork hub takes.  The
    entry-port form of this fan records its centred port in
    ``graph.half_grid_station_ids`` (see
    :func:`_register_half_grid_entry_fan_ports`); the station-rooted form must
    likewise record its branches, so the grid-alignment invariants read those
    offsets as the fan's spine grid rather than stray off-grid placement.

    Runs after the branches have settled onto their spine, so it only sets a
    flag: a branch already at a half-pitch offset is recorded, none is moved.
    That is also why the record goes to
    ``graph.post_layout_half_grid_station_ids`` rather than the placement
    channel: every reader of ``half_grid_station_ids`` has already run, and one
    that keys a geometric decision off it - :func:`_off_track_output_below`
    treats a section whose every trunk station is marked as having a vacated
    trunk row - would give a different answer here than it gave while the
    section's content was being placed.
    """
    for _hub, section, targets in _station_rooted_fans(graph):
        anchor_id = next(
            (
                pid
                for pid in section.entry_ports
                if (p := graph.ports.get(pid)) is not None
                and p.side in (PortSide.LEFT, PortSide.RIGHT)
            ),
            None,
        )
        anchor_st = graph.stations.get(anchor_id) if anchor_id is not None else None
        if anchor_st is None:
            continue
        half_off_trunk = anchor_id in graph.half_grid_station_ids
        joins = _fan_reconvergence_joins(graph, section, targets, hidden_only=True)
        for src_ids in joins.values():
            branch_ys = _evenly_spaced_ys(
                [graph.stations[s].y for s in src_ids if s in graph.stations]
            )
            if branch_ys is None:
                continue
            pitch = branch_ys[1] - branch_ys[0]
            trunk_y = anchor_st.y + (0.5 * pitch if half_off_trunk else 0.0)
            for sid in src_ids:
                st = graph.stations.get(sid)
                if st is None:
                    continue
                if _off_grid_line(st.y, trunk_y, pitch):
                    graph.post_layout_half_grid_station_ids.add(sid)


def _register_half_grid_entry_fan_ports(
    graph: MetroGraph, entry_fan_ports: dict[str, list[str]]
) -> None:
    """Register a centred entry port that lands on a half-pitch centreline.

    An even branch count seats the port midway between two grid rows - the same
    genuine half-pitch offset a fork hub takes (see
    :func:`_restore_divergence_midpoints`).  Recording it in
    ``graph.half_grid_station_ids`` tells the grid-alignment invariants the
    section's real rows are the branches, half a slot from the port centreline
    the reconvergence join and any pass-through legitimately ride.
    """
    for port_id, target_ids in entry_fan_ports.items():
        port_st = graph.stations.get(port_id)
        if port_st is None:
            continue
        branch_ys = _evenly_spaced_ys(
            [graph.stations[t].y for t in target_ids if t in graph.stations]
        )
        if branch_ys is None:
            continue
        pitch = branch_ys[1] - branch_ys[0]
        if _off_grid_line(port_st.y, branch_ys[0], pitch):
            graph.half_grid_station_ids.add(port_id)


def _off_grid_line(y: float, origin: float, pitch: float, tol: float = 1.0) -> bool:
    """True when ``y`` sits more than ``tol`` from the nearest ``origin + k*pitch``."""
    residue = (y - origin) % pitch
    return min(residue, pitch - residue) > tol


_SNAP_TOLERANCE_PX: float = COORD_TOLERANCE_FINE
"""Distance within which two grid snap Ys count as coinciding.

It decides which residues vote for one grid line, which Ys sit midway between
two slots, and when a tie's snap levels a relation.  The group origin is
rounded to 0.001 px, so it must comfortably exceed that rounding's 5e-4 px
error for these answers not to depend on where the row sits on the canvas.
"""

_UP = -1
_DOWN = 1


def _is_slot_tie(y: float, origin: float, pitch: float) -> bool:
    """True when ``y`` sits midway between two ``origin + n*pitch`` slots."""
    q = (y - origin) / pitch
    return abs(q - math.floor(q) - 0.5) * pitch <= _SNAP_TOLERANCE_PX


def _slot_snap(
    y: float, origin: float, pitch: float, half: float, tie_direction: int = _UP
) -> float:
    """Snap ``y`` to ``origin + n*pitch`` unless that shifts it over half a pitch.

    A ``y`` midway between two slots takes the one ``tie_direction`` names
    (:data:`_UP` or :data:`_DOWN`); any other ``y`` takes the nearest slot.
    """
    q = (y - origin) / pitch
    if _is_slot_tie(y, origin, pitch):
        n = math.floor(q) + (1 if tie_direction == _DOWN else 0)
    else:
        n = round(q)
    snapped = origin + n * pitch
    return snapped if abs(snapped - y) <= half + _SNAP_TOLERANCE_PX else y


def _group_grid_origin(
    graph: MetroGraph, sec_ids: list[str], pitch: float
) -> tuple[float, dict[str, set[str]]] | None:
    """Most-held grid line ``y % pitch`` of the group's on-grid stations, with port map.

    Returns ``(origin, per_section_ports)``, or None when the group holds no
    station to read a grid from.  Residues within :data:`_SNAP_TOLERANCE_PX` of
    each other, wrapping at the pitch, vote for one line, so float noise cannot
    split a line's vote; the origin is the line's first residue, rounded to
    0.001 px.  Off-track stations were lifted relative to their consumers; they
    snap to the same grid but don't influence the origin.

    Rows a section allocator spread to a pitch the grid does not divide carry
    one residue each and so name no mode.  Every candidate origin puts each of
    them within half a pitch of a slot, so the group anchors on the residue its
    first station holds; refusing the vote is the one outcome that leaves the
    rows off the grid the snap exists to restore them to.
    """
    line_residues: list[float] = []
    line_votes: list[int] = []
    per_section_ports: dict[str, set[str]] = {}
    require_phase_field(graph, "half_grid_station_ids")
    half_grid_ids = graph.half_grid_station_ids
    require_phase_field(graph, "symfan_trunk_station_ids")
    symfan_trunk_ids = graph.symfan_trunk_station_ids
    for sec_id in sec_ids:
        section = graph.sections.get(sec_id)
        if section is None or section.bbox_h <= 0:
            continue
        port_ids = section.port_ids
        per_section_ports[sec_id] = port_ids
        for sid in section.station_ids:
            if sid in port_ids:
                continue
            # Half-grid branches and the symfan source/trunk stations they share
            # a frame with carry the section's own local Y, not the row origin.
            if sid in half_grid_ids or sid in symfan_trunk_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.off_track:
                continue
            residue = st.y % pitch
            for i, line in enumerate(line_residues):
                gap = abs(residue - line)
                if min(gap, pitch - gap) <= _SNAP_TOLERANCE_PX:
                    line_votes[i] += 1
                    break
            else:
                line_residues.append(residue)
                line_votes.append(1)
    if not line_votes:
        return None
    winner = line_votes.index(max(line_votes))
    return round(line_residues[winner], 3), per_section_ports


def _snap_group_to_grid(
    graph: MetroGraph,
    pitch: float,
    sec_ids: list[str],
    convergence_sources: dict[str, list[str]],
) -> None:
    """Snap one row-group's stations and LEFT/RIGHT ports to its shared grid."""
    half = pitch / 2.0
    origin_info = _group_grid_origin(graph, sec_ids, pitch)
    if origin_info is None:
        return
    origin_r, per_section_ports = origin_info
    candidates = _snap_candidates(graph, per_section_ports, convergence_sources)
    group_ids = {
        sid
        for sec_id in per_section_ports
        for sid in graph.sections[sec_id].station_ids
        if sid in graph.stations
    }
    tie_directions = _tie_directions(
        graph, [sid for sid, _is_port in candidates], group_ids, origin_r, pitch
    )

    # Independent snapping can round two same-column stations onto one slot;
    # the later one keeps its pre-snap Y rather than collapsing.
    column_slots: dict[float, set[float]] = {}

    for sid, is_port in candidates:
        st = graph.stations[sid]
        target = _slot_snap(st.y, origin_r, pitch, half, tie_directions.get(sid, _UP))
        if is_port:
            _set_port_y(graph, sid, target)
            continue
        slots = column_slots.setdefault(round(st.x, 3), set())
        if round(target, 3) in slots:
            target = st.y
        slots.add(round(target, 3))
        st.y = target


def _snap_candidates(
    graph: MetroGraph,
    per_section_ports: dict[str, set[str]],
    convergence_sources: dict[str, list[str]],
) -> list[tuple[str, bool]]:
    """``(station id, is_port)`` for everything the group snap moves, in snap order."""
    require_phase_field(graph, "half_grid_station_ids")
    half_grid_ids = graph.half_grid_station_ids
    require_phase_field(graph, "symfan_trunk_station_ids")
    symfan_trunk_ids = graph.symfan_trunk_station_ids
    candidates: list[tuple[str, bool]] = []
    for sec_id, port_ids in per_section_ports.items():
        section = graph.sections.get(sec_id)
        if section is None:
            continue
        vertical_flow = not lanes_run_along_y(section.direction)
        for sid in section.station_ids:
            if sid in port_ids or sid in half_grid_ids or sid in symfan_trunk_ids:
                continue
            if sid not in graph.stations or sid in convergence_sources:
                continue
            candidates.append((sid, False))
        for pid in port_ids:
            port = graph.ports.get(pid)
            if port is None or pid not in graph.stations:
                continue
            if port.side not in (PortSide.LEFT, PortSide.RIGHT):
                continue
            # A vertical-flow section's perpendicular exit ports are anchored
            # to the downstream entry-port Y by _resolve_tb_exit_y; preserve
            # that alignment rather than snapping them to the row grid.
            if vertical_flow and not port.is_entry:
                continue
            if pid in convergence_sources:
                continue
            candidates.append((pid, True))
    return candidates


def _tie_directions(
    graph: MetroGraph,
    candidate_ids: list[str],
    group_ids: set[str],
    origin: float,
    pitch: float,
) -> dict[str, int]:
    """One snap direction for each connected set of stations tied between slots.

    The grid origin is a residue of ``y % pitch``, so how a row's slots are
    numbered depends on where the row sits on the canvas; ties are therefore
    resolved from the row's relative geometry alone.  Tied stations joined by
    an edge, or sharing one hub in the same column (a fan's branches), form a
    component that moves as one, so neither a fan nor a run is split across two
    slots.  A component takes the direction that levels more of its relations
    to the group's other stations (see :func:`_tie_relations`); failing that,
    the direction its feeders sit in, then its consumers; then the direction
    whose levelled neighbours carry more of its lines (see
    :func:`_levelled_line_counts`); failing that, up.
    """
    ys = {sid: graph.stations[sid].y for sid in candidate_ids}
    tied = sorted(sid for sid, y in ys.items() if _is_slot_tie(y, origin, pitch))
    if not tied:
        return {}
    half = pitch / 2.0
    tied_set = set(tied)
    feeders: dict[str, set[str]] = {}
    consumers: dict[str, set[str]] = {}
    for edge in graph.edges:
        if edge.source in tied_set or edge.target in tied_set:
            consumers.setdefault(edge.source, set()).add(edge.target)
            feeders.setdefault(edge.target, set()).add(edge.source)
    partner_ys = {sid: graph.stations[sid].y for sid in group_ids}
    partner_ys.update(
        (sid, _slot_snap(y, origin, pitch, half)) for sid, y in ys.items()
    )

    directions: dict[str, int] = {}
    for component in _tie_components(graph, tied, feeders, consumers):
        feeder_relations = _tie_relations(component, feeders, ys, partner_ys, pitch)
        consumer_relations = _tie_relations(component, consumers, ys, partner_ys, pitch)
        line_counts = _levelled_line_counts(
            graph, component, group_ids, ys, partner_ys, pitch
        )
        direction = _component_tie_direction(
            feeder_relations, consumer_relations, half, line_counts
        )
        for sid in component:
            directions[sid] = direction
    return directions


def _tie_components(
    graph: MetroGraph,
    tied: list[str],
    feeders: dict[str, set[str]],
    consumers: dict[str, set[str]],
) -> list[list[str]]:
    """Tied stations joined by an edge or by one hub's same-column fan."""
    tied_set = set(tied)
    links: dict[str, set[str]] = {sid: set() for sid in tied}

    def link(a: str, b: str) -> None:
        links[a].add(b)
        links[b].add(a)

    for sid in tied:
        for other in consumers.get(sid, set()) & tied_set:
            link(sid, other)
    for neighbours in (feeders, consumers):
        for hub in sorted(neighbours):
            fan_ids = neighbours[hub] & tied_set
            if len(fan_ids) < 2:
                continue
            columns: dict[float, list[str]] = {}
            for sid in sorted(fan_ids):
                st = graph.stations[sid]
                if not st.is_port and not st.off_track:
                    columns.setdefault(round(st.x, 3), []).append(sid)
            for fan in columns.values():
                for sid in fan[1:]:
                    link(fan[0], sid)
    components: list[list[str]] = []
    seen: set[str] = set()
    for root in tied:
        if root in seen:
            continue
        seen.add(root)
        component, frontier = [], [root]
        while frontier:
            sid = frontier.pop()
            component.append(sid)
            for other in sorted(links[sid] - seen):
                seen.add(other)
                frontier.append(other)
        components.append(sorted(component))
    return components


def _tie_relations(
    component: list[str],
    neighbours: dict[str, set[str]],
    ys: dict[str, float],
    partner_ys: dict[str, float],
    pitch: float,
) -> list[tuple[float, float]]:
    """``(component y, partner y)`` pairs along one side of a tied component.

    Partners are the snap group's stations outside the component, keyed in
    ``partner_ys`` by the Y they hold once snapped.  A partner
    shared by several members (the hub of a tied fan) is compared with the
    members' centre; a member with several partners on this side is compared
    with their centre.  Partners more than one pitch away say nothing about
    which neighbouring slot keeps the member level, and are dropped.
    """
    members = set(component)

    def partners_of(sid: str) -> list[str]:
        return sorted(
            p
            for p in neighbours.get(sid, set())
            if p not in members and p in partner_ys
        )

    members_by_partner: dict[str, list[str]] = {}
    for sid in component:
        for partner in partners_of(sid):
            members_by_partner.setdefault(partner, []).append(sid)
    shared = {p: ms for p, ms in members_by_partner.items() if len(ms) >= 2}

    relations = [
        (_centre([ys[sid] for sid in ms]), partner_ys[p]) for p, ms in shared.items()
    ]
    for sid in component:
        own = [p for p in partners_of(sid) if p not in shared]
        if own:
            relations.append((ys[sid], _centre([partner_ys[p] for p in own])))
    return [(a, b) for a, b in relations if abs(b - a) <= pitch + _SNAP_TOLERANCE_PX]


def _centre(ys: list[float]) -> float:
    return (max(ys) + min(ys)) / 2.0


def _component_tie_direction(
    feeder_relations: list[tuple[float, float]],
    consumer_relations: list[tuple[float, float]],
    half: float,
    line_counts: dict[int, int],
) -> int:
    """The direction levelling more relations; else toward feeders, consumers.

    Failing those, the direction whose levelled neighbours carry more of the
    component's lines (``line_counts``); failing that, up.
    """

    def levelled(direction: int) -> int:
        return sum(
            abs(own + direction * half - partner) < _SNAP_TOLERANCE_PX
            for own, partner in feeder_relations + consumer_relations
        )

    up, down = levelled(_UP), levelled(_DOWN)
    if up != down:
        return _UP if up > down else _DOWN
    for relations in (feeder_relations, consumer_relations):
        net = sum(partner - own for own, partner in relations)
        if abs(net) > _SNAP_TOLERANCE_PX:
            return _UP if net < 0 else _DOWN
    if line_counts[_UP] != line_counts[_DOWN]:
        return _UP if line_counts[_UP] > line_counts[_DOWN] else _DOWN
    return _UP


def _levelled_line_counts(
    graph: MetroGraph,
    component: list[str],
    group_ids: set[str],
    ys: dict[str, float],
    partner_ys: dict[str, float],
    pitch: float,
) -> dict[int, int]:
    """Per direction, the lines passing through the neighbours it levels with.

    A member's neighbour counts each line that reaches it from the member along
    the group's own edges, directly or via other group stations, so the station
    where a member's bundle reunites outweighs a one-line detour.
    """
    members = set(component)
    consumers_by_line: dict[tuple[str, str], set[str]] = {}
    feeders_by_line: dict[tuple[str, str], set[str]] = {}
    for edge in graph.edges:
        if edge.source in group_ids and edge.target in group_ids:
            consumers_by_line.setdefault((edge.line_id, edge.source), set()).add(
                edge.target
            )
            feeders_by_line.setdefault((edge.line_id, edge.target), set()).add(
                edge.source
            )
    line_ids = sorted({line_id for line_id, _sid in consumers_by_line})
    half = pitch / 2.0
    counts = {_UP: 0, _DOWN: 0}
    for sid in component:
        for step in (consumers_by_line, feeders_by_line):
            adjacent = {
                p for line_id in line_ids for p in step.get((line_id, sid), ())
            } - members
            for line_id in line_ids:
                for p in _line_reach(sid, line_id, step, members) & adjacent:
                    for direction in (_UP, _DOWN):
                        gap = ys[sid] + direction * half - partner_ys[p]
                        if abs(gap) < _SNAP_TOLERANCE_PX:
                            counts[direction] += 1
    return counts


def _line_reach(
    start: str,
    line_id: str,
    step: dict[tuple[str, str], set[str]],
    blocked: set[str],
) -> set[str]:
    """Stations ``line_id`` reaches from ``start`` without entering ``blocked``."""
    reached: set[str] = set()
    frontier = list(step.get((line_id, start), ()))
    while frontier:
        sid = frontier.pop()
        if sid in reached or sid in blocked:
            continue
        reached.add(sid)
        frontier.extend(step.get((line_id, sid), ()))
    return reached


def _restore_partial_trunk_descents(graph: MetroGraph) -> None:
    """Re-seat each partial row-mate a handover lane below its carrier.

    Stage 4.8 seats a partial row-mate one lane deeper than the row carrier so a
    direct port-to-port connector runs flat.  When carrier and partial share a
    row grid the group snap rounds that sub-grid offset onto the carrier's slot;
    on an explicit-grid solo section it leaves it in place.  Either way the target
    is the same: the partial's handover port sits ``descent`` below the carrier
    port's post-snap Y.  Shifting the whole section by the gap to that target --
    every internal station, every LR/RL port, the bbox growing downward -- lands
    it right in both cases and is idempotent (a re-run finds zero gap), so a snap
    that did not collapse the descent is not double-counted.
    """
    require_phase_field(graph, "_partial_trunk_descents")
    for sec_id, record in graph._partial_trunk_descents.items():
        section = graph.sections.get(sec_id)
        partial_st = graph.stations.get(record.partial_port)
        carrier_st = graph.stations.get(record.carrier_port)
        if section is None or partial_st is None or carrier_st is None:
            continue
        delta = (carrier_st.y + record.descent) - partial_st.y
        if delta < SAME_COORD_TOLERANCE:
            continue
        port_ids = section.port_ids
        for pid in port_ids:
            port_st = graph.stations.get(pid)
            if port_st is not None:
                _set_port_y(graph, pid, port_st.y + delta)
        for sid in section.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is not None:
                st.y += delta
        section.bbox_h += delta


def _restore_convergence_midpoints(
    graph: MetroGraph,
    convergence_sources: dict[str, list[str]],
    section_pitch: dict[str, float],
) -> None:
    """Re-centre each fan-in target on its post-snap source midpoint.

    Sources an odd number of slots apart put the midpoint half a pitch off the
    section's grid; the target is then registered in ``graph.half_grid_station_ids``
    so the grid-alignment invariants read it as an intentional centreline, as
    :func:`_restore_divergence_midpoints` does for a fork hub.
    """
    for target_id, src_ids in convergence_sources.items():
        st = graph.stations.get(target_id)
        if st is None or st.off_track:
            continue
        new_src_ys = [graph.stations[sid].y for sid in src_ids if sid in graph.stations]
        if len(set(round(y, 3) for y in new_src_ys)) < 2:
            continue
        midpoint = (max(new_src_ys) + min(new_src_ys)) / 2.0
        _set_port_y(graph, target_id, midpoint)
        pitch = section_pitch.get(st.section_id or "")
        if pitch and _off_grid_line(midpoint, min(new_src_ys), pitch):
            graph.half_grid_station_ids.add(target_id)


def _restore_divergence_midpoints(
    graph: MetroGraph,
    divergence_targets: dict[str, list[str]],
    trunk_followers: dict[str, list[str]],
) -> None:
    """Re-centre each fan-out hub on its post-snap successor midpoint.

    An even successor count leaves the midpoint at a genuine half-pitch
    offset from the successors' own grid; when that happens the hub is
    registered in ``graph.half_grid_station_ids`` so the grid-alignment
    invariants recognise it as intentional, the same way the 2-branch
    symmetric fan's hub already is.  An odd count lands the midpoint back
    on-grid (it coincides with the middle successor), so no registration
    is needed there.

    ``trunk_followers`` names the pass-through run that shared the hub's Y
    before the snap (see :func:`_centreline_trunk_followers`): the section's own
    LR/RL ports and the single-line trunk stations beyond them.  They move onto
    the restored centreline with the hub, so the boundary run leaves the section
    as a straight riser and the neighbouring trunk stays flat through it; when
    the centreline is half-pitch they are registered alongside the hub, since
    they ride the same off-grid track.

    Skipped when the successors' grid-snapped Ys are not distinct and evenly
    spaced - two lines sharing one row while a third sits alone on the next
    has no single well-defined half-pitch centreline, so the hub is left on
    its own grid-snapped slot, and its trunk with it.
    """
    for hub_id, tgt_ids in divergence_targets.items():
        st = graph.stations.get(hub_id)
        if st is None or st.off_track:
            continue
        new_tgt_ys = _evenly_spaced_ys(
            [graph.stations[sid].y for sid in tgt_ids if sid in graph.stations]
        )
        if new_tgt_ys is None:
            continue
        local_pitch = new_tgt_ys[1] - new_tgt_ys[0]
        midpoint = (new_tgt_ys[0] + new_tgt_ys[-1]) / 2.0
        residue = (midpoint - new_tgt_ys[0]) % local_pitch
        half_pitch = min(residue, local_pitch - residue) > 1.0
        for sid in (hub_id, *trunk_followers.get(hub_id, ())):
            _set_port_y(graph, sid, midpoint)
            if half_pitch:
                graph.half_grid_station_ids.add(sid)


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
    require_phase_field(graph, "half_grid_station_ids")
    half_grid_ids = graph.half_grid_station_ids
    convergence_sources = _convergence_source_ys(graph)
    # An entry-fan reconvergence join and a center_ports fan-in entry port are
    # both held off the grid on the fan centreline; they must stay excluded from
    # the residue vote on this second pass too, or the pass reads them as
    # off-grid and pulls them back onto a slot.
    for join_id, src_ids in _symmetric_reconvergence_joins(graph).items():
        convergence_sources.setdefault(join_id, src_ids)
    for port_id, target_ids in _entry_fan_centre_ports(graph).items():
        convergence_sources.setdefault(port_id, target_ids)
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
    shift_down = -mode_residue
    shift_up = y_spacing - mode_residue
    candidates: list[float] = []
    if _canvas_top_preserved(graph, section_y_padding, shift_down):
        candidates.append(shift_down)
    if _canvas_top_preserved(graph, section_y_padding, shift_up):
        candidates.append(shift_up)
    if not candidates:
        # Neither preserves the margin; pick the up-shift since
        # shifting down would clip the canvas.
        candidates.append(shift_up)
    shift = min(candidates, key=abs)
    if abs(shift) < 1e-6:
        return
    translate_graph(graph, 0.0, shift)
    # Junctions ride the same shift via _position_junctions, which keys
    # off the (now-shifted) exit/entry port Ys.
    _position_junctions(graph)
