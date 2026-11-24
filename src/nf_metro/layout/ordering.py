"""Track-per-line vertical ordering.

Each metro line gets a dedicated horizontal track (Y position). Nodes on
the main path of a line snap to its base track. Short branches (nodes
whose predecessors are far from the line's base track) stay near their
predecessors instead of jumping to a distant track.
"""

from __future__ import annotations

__all__ = ["assign_tracks"]

from collections import defaultdict
from collections.abc import Iterable
from math import ceil
from statistics import median_low

import networkx as nx

from nf_metro.layout.constants import (
    COORD_TOLERANCE_FINE,
    DEFAULT_LINE_PRIORITY,
    DIAMOND_COMPRESSION,
    FANOUT_SPACING,
    LINE_GAP,
    SAME_COORD_TOLERANCE,
    SIDE_BRANCH_NUDGE,
)
from nf_metro.parser.model import (
    LineSpread,
    MetroGraph,
    agreed_lane_end,
    is_bypass_v,
)


def assign_tracks(
    graph: MetroGraph,
    layers: dict[str, int],
    line_gap: float = LINE_GAP,
    *,
    entry_top: bool = False,
    continuation_predecessors: dict[str, str] | None = None,
    terminal_nodes: frozenset[str] = frozenset(),
    exit_reaching: frozenset[str] = frozenset(),
    flow_horizontal: bool = True,
    priority_lane_sign: float | None = None,
) -> dict[str, float]:
    """Assign each station a track using the track-per-line strategy.

    Args:
        graph: The metro graph.
        layers: Layer assignment from assign_layers().
        line_gap: Fixed gap (in track units) between line base tracks.
        entry_top: When True, use asymmetric (downward) fan-out at the
            entry layer so the entry-connected station stays at the top.
        continuation_predecessors: Full-graph-proven continuation predecessor
            for each station that inherits a track. The section subgraph omits
            external edges, so the caller computes this relation once.
        terminal_nodes: Stations carrying only lines that do not leave the
            section, so their chain ends inside it (a terminal spur). Supplied
            by the caller because the section subgraph omits the exit-port
            edges that distinguish a spur from a through-line.
        exit_reaching: Stations with a forward path to a section exit port --
            the section's through-line. Supplied by the caller (the subgraph
            omits the exit-port edges), and used to keep the through-chain on
            the trunk when a short output spur shares its entry fan.
        flow_horizontal: Whether the section flows along X (LR/RL), so its trunk
            is a horizontal row a level exit run would cross. A dead-end output
            tail peels below that row only then; a vertical (TB/BT) section's
            exit leaves perpendicular and its tail continues along the flow.
        priority_lane_sign: Sign along the track axis toward which the section
            stacks its lanes in line-priority order, or ``None`` when a seam may
            impose another order.  Lets a bypass branch open toward its own
            lines' lanes (see :func:`_bypass_lane_side`).

    Returns a dict mapping station_id -> track (float).
    """
    if not graph.lines:
        return {sid: float(i) for i, sid in enumerate(graph.stations)}

    G = _station_digraph(graph)

    continuation_predecessors = continuation_predecessors or {}

    line_order = list(graph.lines.keys())
    line_priority = {lid: i for i, lid in enumerate(line_order)}

    # Step 1: Determine primary line for each node
    node_primary: dict[str, str | None] = {}
    for sid in graph.stations:
        node_lines = graph.station_lines(sid)
        if node_lines:
            node_primary[sid] = min(
                node_lines, key=lambda ln: line_priority.get(ln, DEFAULT_LINE_PRIORITY)
            )
        else:
            node_primary[sid] = None

    # Step 2: Fixed-gap base tracks per line.  In centred mode the bases
    # are symmetric about zero so the weave balances around the midline;
    # otherwise they stack downward from the top line.
    centered = graph.line_spread is LineSpread.CENTERED
    n_lines = len(line_order)
    line_base: dict[str, float] = {}
    for i, lid in enumerate(line_order):
        if centered:
            line_base[lid] = (i - (n_lines - 1) / 2) * line_gap
        else:
            line_base[lid] = i * line_gap

    # In centred mode a shared (multi-line) station should sit on the mean
    # of its lines' base tracks (the bundle midline) rather than snap to
    # its single highest-priority line's base, which would pull every trunk
    # station up to the top line.  Single-line stations keep their own
    # line's (now symmetric) base so exclusive callers fan above/below.
    station_base: dict[str, float] = {}
    for sid, primary in node_primary.items():
        if primary not in line_base:
            continue
        if centered:
            bases = [
                line_base[ln] for ln in graph.station_lines(sid) if ln in line_base
            ]
            station_base[sid] = sum(bases) / len(bases)
        else:
            station_base[sid] = line_base[primary]

    # Step 3: Group nodes by (layer, primary_line).  Off-track stations
    # are excluded from grouping: their Y is overwritten by the Stage 5.2
    # off-track lift (anchored to the consumer station), so letting them
    # participate in fan-out placement here would only distort the trunk
    # track assignment for siblings and downstream stations.
    layer_line_groups: dict[tuple[int, str | None], list[str]] = defaultdict(list)
    tracks: dict[str, float] = {}
    for sid, station in graph.stations.items():
        if station.off_track:
            tracks[sid] = 0.0
            continue
        layer_line_groups[(layers.get(sid, 0), node_primary[sid])].append(sid)

    max_layer = max(layers.values()) if layers else 0
    orphan_track = len(line_order) * line_gap

    diamond_members = _build_diamond_index(G, layers, graph)
    layer_occupancy: dict[int, dict[str, float]] = defaultdict(dict)

    contenders = _peel_contenders(G, graph, layers)
    seats_after: dict[int, list[str]] = defaultdict(list)
    for node in contenders:
        seats_after[_contender_seat_layer(node, G, layers)].append(node)

    for layer_idx in range(max_layer + 1):
        for lid in line_order:
            nodes = [
                n
                for n in layer_line_groups.get((layer_idx, lid), [])
                if n not in contenders
            ]
            if not nodes:
                continue

            base = line_base[lid]

            if len(nodes) == 1:
                tracks[nodes[0]] = _place_single_node(
                    nodes[0],
                    station_base[nodes[0]],
                    line_gap,
                    G,
                    tracks,
                    graph,
                    layers,
                    diamond_members=diamond_members,
                    layer_occupancy=layer_occupancy,
                    continuation_predecessors=continuation_predecessors,
                    line_base=line_base,
                    terminal_nodes=terminal_nodes,
                    exit_reaching=exit_reaching,
                    flow_horizontal=flow_horizontal,
                )
                layer_occupancy[layer_idx][nodes[0]] = tracks[nodes[0]]
            else:
                _place_fan_out(
                    nodes,
                    base,
                    line_gap,
                    G,
                    tracks,
                    straight_diamonds=graph.diamond_style == "straight",
                    layer_idx=layer_idx if entry_top else -1,
                    graph=graph,
                    exit_reaching=exit_reaching,
                    priority_lane_sign=priority_lane_sign,
                    station_base=station_base,
                    line_base=line_base,
                )
                for n in nodes:
                    layer_occupancy[layer_idx][n] = tracks[n]

        # Orphans (no line)
        orphans = layer_line_groups.get((layer_idx, None), [])
        for node in orphans:
            tracks[node] = orphan_track
            layer_occupancy[layer_idx][node] = orphan_track
            orphan_track += 1

        # Equalize cross-line fork groups at this layer so downstream
        # placement sees corrected positions.
        _equalize_fork_groups(
            layer_idx,
            layers,
            tracks,
            G,
            graph,
            node_primary,
            line_gap,
            line_base,
        )
        if flow_horizontal:
            _step_off_exit_rows(
                layer_idx, layers, tracks, G, exit_reaching, layer_occupancy
            )

        for node in sorted(
            seats_after.get(layer_idx, []), key=lambda n: station_base[n]
        ):
            column = layer_occupancy[layers.get(node, 0)]
            tracks[node] = _seat_contender(
                station_base[node],
                [tracks[sid] for sid in column],
                _span_tracks(node, layer_idx + 1, graph, layers, tracks),
                _far_sides(contenders[node], tracks),
                line_gap,
            )
            column[node] = tracks[node]

    return tracks


def peel_lane_ends(
    graph: MetroGraph,
    layers: dict[str, int],
    tracks: dict[str, float],
    priority_lane_sign: float | None,
) -> dict[str, float]:
    """Lines a section pins to an end of its lane order (``Section.lane_ends``).

    A peel contender's lines join the bundle from the side of the trunk it was
    seated on, so they take the lane-order end facing that side, the contender
    seated furthest out taking the outermost lane.  Each pinned line maps to its
    contender's seat distance from the join, signed toward the first lane
    (negative) or the last (positive).  *priority_lane_sign* names which end
    faces which side, so without it (a seam may impose the order) nothing is
    pinned.  A contender whose joins do not all lie on one side of it pins
    nothing, and a line two contenders would pin to opposite ends is left on
    its priority lane.
    """
    if priority_lane_sign is None:
        return {}
    G = _station_digraph(graph)
    lane_sign = 1.0 if priority_lane_sign > 0 else -1.0
    pins: dict[str, list[float]] = defaultdict(list)
    for node in _peel_contenders(G, graph, layers):
        offsets = [tracks[node] - tracks[c] for c in G.successors(node) if c in tracks]
        if offsets and (
            all(o > COORD_TOLERANCE_FINE for o in offsets)
            or all(o < -COORD_TOLERANCE_FINE for o in offsets)
        ):
            pin = min(offsets, key=abs) * lane_sign
            for lid in graph.station_lines(node):
                pins[lid].append(pin)
    agreed = {lid: agreed_lane_end(values) for lid, values in pins.items()}
    return {lid: pin for lid, pin in agreed.items() if pin is not None}


def _station_digraph(graph: MetroGraph) -> nx.DiGraph[str]:
    """Every station of *graph* as a node, every edge as a directed arc."""
    G: nx.DiGraph[str] = nx.DiGraph()
    for edge in graph.edges:
        G.add_edge(edge.source, edge.target)
    for sid in graph.stations:
        if sid not in G:
            G.add_node(sid)
    return G


def _step_off_exit_rows(
    layer_idx: int,
    layers: dict[str, int],
    tracks: dict[str, float],
    G: nx.DiGraph[str],
    exit_reaching: frozenset[str],
    layer_occupancy: dict[int, dict[str, float]],
) -> None:
    """Move this layer's dead-end stations off rows an exit run holds.

    A station whose flow leaves through the section's exit port, with no
    in-section successor carrying it there, is an exit carrier.  A sole
    carrier's run peels off its row ahead of everything downstream of the
    carrier, so only a station hanging off a different feeder is in its way.
    When several carriers converge on the exit, each rides its own row flat to
    the section edge, and any dead-end station seated on one of those rows in a
    later layer would have a run drawn straight through its marker.  Such a
    station peels downward, as other dead-end tails do, one sibling pitch at
    a time until it clears every blocking row and every station already at
    this layer.
    """
    placed = layer_occupancy.get(layer_idx, {})
    if not placed:
        return
    carriers = {
        s: tracks[s]
        for s in exit_reaching
        if s in tracks
        and layers.get(s, 0) < layer_idx
        and not any(n in exit_reaching for n in G.successors(s))
    }
    if not carriers:
        return
    distinct = sorted(set(tracks[n] for n in placed) | set(carriers.values()))
    gaps = [
        b - a for a, b in zip(distinct, distinct[1:]) if b - a > SAME_COORD_TOLERANCE
    ]
    pitch = min(gaps) if gaps else LINE_GAP

    for node in sorted(placed, key=lambda n: tracks[n]):
        if node in exit_reaching:
            continue
        upstream = nx.ancestors(G, node)
        foreign = [
            t for c, t in carriers.items() if len(carriers) > 1 or c not in upstream
        ]

        def on_foreign_row(t: float, rows: list[float] = foreign) -> bool:
            return any(abs(t - r) < SAME_COORD_TOLERANCE for r in rows)

        if not on_foreign_row(tracks[node]):
            continue
        new = tracks[node] + pitch
        while on_foreign_row(new) or _is_track_occupied_at_layer(
            new, layer_idx, layer_occupancy, node
        ):
            new += pitch
        tracks[node] = new
        placed[node] = new


def _build_diamond_index(
    G: nx.DiGraph[str],
    layers: dict[str, int],
    graph: MetroGraph | None = None,
) -> set[str]:
    """Pre-compute the set of nodes belonging to diamond (fork-join) patterns.

    A diamond exists when two or more nodes at the same layer share the
    same predecessors, have at least one common successor, and (when
    *graph* is provided) carry the same set of metro lines.

    Returns a set of station IDs that are diamond members, enabling O(1)
    membership checks instead of per-call layer scans.
    """
    diamond_members: set[str] = set()

    # Group nodes by layer
    layer_nodes: dict[int, list[str]] = defaultdict(list)
    for node, layer in layers.items():
        layer_nodes[layer].append(node)

    for nodes in layer_nodes.values():
        # Group by predecessor set (only nodes with both preds and succs)
        pred_groups: dict[frozenset[str], list[str]] = defaultdict(list)
        for node in nodes:
            preds = frozenset(G.predecessors(node))
            if preds and any(True for _ in G.successors(node)):
                pred_groups[preds].append(node)

        for group in pred_groups.values():
            if len(group) < 2:
                continue

            if graph is not None:
                # Sub-group by line set
                line_groups: dict[frozenset[str], list[str]] = defaultdict(list)
                for node in group:
                    lines = frozenset(graph.station_lines(node))
                    line_groups[lines].append(node)

                for line_group in line_groups.values():
                    if len(line_group) < 2:
                        continue
                    common_succs = set(G.successors(line_group[0]))
                    for node in line_group[1:]:
                        common_succs &= set(G.successors(node))
                    if common_succs:
                        diamond_members.update(line_group)
            else:
                common_succs = set(G.successors(group[0]))
                for node in group[1:]:
                    common_succs &= set(G.successors(node))
                if common_succs:
                    diamond_members.update(group)

    return diamond_members


def _is_track_occupied_at_layer(
    track: float,
    layer: int,
    layer_occupancy: dict[int, dict[str, float]],
    exclude_node: str,
    tolerance: float = SAME_COORD_TOLERANCE,
) -> bool:
    """Check if any already-placed station at this layer occupies the given track."""
    placed = layer_occupancy.get(layer, {})
    return not _clear_of(
        track, [t for sid, t in placed.items() if sid != exclude_node], tolerance
    )


def _clear_of(track: float, others: Iterable[float], tolerance: float) -> bool:
    """Whether *track* lies at least *tolerance* from every one of *others*."""
    return all(abs(track - t) >= tolerance for t in others)


def _find_free_nearby_track(
    pred_track: float,
    base: float,
    layer: int,
    layer_occupancy: dict[int, dict[str, float]],
    candidates: set[float],
    exclude_node: str,
    *,
    allow_flat: bool = False,
) -> float | None:
    """Find the nearest candidate track between *pred_track* and *base* that is
    free at *layer*.

    *candidates* is the pool of track values to consider (already-placed
    station tracks, and optionally the reserved per-line base tracks). Ranked
    by distance from *pred_track* so a branch stays as close to its
    predecessor as a free row allows. With *allow_flat*, the predecessor's own
    track is eligible (a straight run continuing the predecessor's row);
    otherwise it is skipped to avoid a flat edge onto the predecessor.

    Returns ``None`` when no suitable candidate exists (all occupied, or none
    lie between pred and base).
    """
    lo, hi = min(pred_track, base), max(pred_track, base)
    within = sorted({t for t in candidates if lo <= t <= hi})
    within.sort(key=lambda t: abs(t - pred_track))
    for t in within:
        if not allow_flat and abs(t - pred_track) < COORD_TOLERANCE_FINE:
            continue
        if not _is_track_occupied_at_layer(t, layer, layer_occupancy, exclude_node):
            return t
    return None


def _peel_below_trunk(
    trunk: float,
    line_gap: float,
    layer: int,
    layer_occupancy: dict[int, dict[str, float]] | None,
    node: str,
) -> float:
    """Track one lane below *trunk*, stepping further down while occupied.

    A dead-end output tail lands here so the through-route holding *trunk*
    stays clear of its marker; the step keeps it off any station already
    placed on the lane at this layer.
    """
    peel = trunk + line_gap
    while layer_occupancy is not None and _is_track_occupied_at_layer(
        peel, layer, layer_occupancy, node
    ):
        peel += line_gap
    return peel


def _primary_line(graph: MetroGraph, node: str) -> str | None:
    """The node's highest-priority (first-declared) line, or None."""
    node_lines = set(graph.station_lines(node))
    for lid in graph.lines:
        if lid in node_lines:
            return lid
    return None


def _peel_leads(fork: str, G: nx.DiGraph[str], graph: MetroGraph) -> dict[str, str]:
    """Peel successors of *fork*, each mapped to the line it leads with.

    A peel successor drops the fork's primary line but keeps another of its
    lines, so its branch leaves the fork's row.  It leads with the
    first-declared line it shares with the fork; peels come in the order of
    their leads' declaration.
    """
    primary = _primary_line(graph, fork)
    if primary is None:
        return {}
    rank = {lid: i for i, lid in enumerate(graph.lines)}
    fork_lines = set(graph.station_lines(fork))
    leads: dict[str, str] = {}
    for succ in G.successors(fork):
        station = graph.stations.get(succ)
        if station is None or station.off_track:
            continue
        succ_lines = set(graph.station_lines(succ))
        shared = (fork_lines & succ_lines) - {primary}
        if primary not in succ_lines and shared:
            leads[succ] = min(shared, key=rank.__getitem__)
    return dict(sorted(leads.items(), key=lambda item: rank[item[1]]))


def _source_peel_lanes(
    fork: str,
    track: float,
    G: nx.DiGraph[str],
    graph: MetroGraph,
    line_base: dict[str, float],
    line_gap: float,
) -> dict[str, float]:
    """Lanes a predecessor-less *fork* seated on *track* claims for its peels.

    Each peel (:func:`_peel_leads`) opens toward the base lane of the line it
    leads with; peels opening to the same side take successive lanes outward
    in line order.  Returns peel successor -> claimed lane.
    """
    if G.in_degree(fork):
        return {}
    depth = {1.0: 0, -1.0: 0}
    lanes: dict[str, float] = {}
    for succ, lead in _peel_leads(fork, G, graph).items():
        side = 1.0 if line_base[lead] >= track else -1.0
        depth[side] += 1
        lanes[succ] = track + side * depth[side] * line_gap
    return lanes


def _foreign_claim_side(
    node: str,
    track: float,
    neighbours: dict[str, float],
    G: nx.DiGraph[str],
    graph: MetroGraph,
    line_base: dict[str, float],
    line_gap: float,
) -> float | None:
    """Side (+1/-1) of the peel claim covering *track* that bars *node*.

    *neighbours* maps the stations already seated in *node*'s column to their
    tracks.  A neighbour fork's peel lane bars *node* unless *node* feeds one of
    the fork's successors, in which case it joins the fork's fan anyway.
    Returns the side the claimed lane lies on relative to its fork, or ``None``
    when *track* is free of foreign claims.
    """
    succs = set(G.successors(node))
    for fork, fork_track in neighbours.items():
        if fork == node or not succs.isdisjoint(G.successors(fork)):
            continue
        for lane in _source_peel_lanes(
            fork, fork_track, G, graph, line_base, line_gap
        ).values():
            if abs(lane - track) < SAME_COORD_TOLERANCE:
                return 1.0 if lane > fork_track else -1.0
    return None


def _seat_source(
    node: str,
    base: float,
    layer: int,
    line_gap: float,
    G: nx.DiGraph[str],
    tracks: dict[str, float],
    graph: MetroGraph,
    layer_occupancy: dict[int, dict[str, float]],
    line_base: dict[str, float],
) -> float:
    """Track for predecessor-less *node*: its *base*, off any foreign peel lane.

    The seat steps away from the claiming fork and holds that direction, so
    opposing claims cannot bounce it back.
    """
    column = {
        sid: tracks[sid] for sid in layer_occupancy.get(layer, {}) if sid in tracks
    }
    seat, side = base, None
    while (
        hit := _foreign_claim_side(node, seat, column, G, graph, line_base, line_gap)
    ) is not None:
        side = side or hit
        seat += side * line_gap
    return seat


def _claimed_peel_lane(
    node: str,
    line_gap: float,
    G: nx.DiGraph[str],
    tracks: dict[str, float],
    graph: MetroGraph,
    line_base: dict[str, float],
) -> float | None:
    """Lane a placed source-fork predecessor claimed for peel branch *node*."""
    for pred in G.predecessors(node):
        if pred in tracks:
            lane = _source_peel_lanes(
                pred, tracks[pred], G, graph, line_base, line_gap
            ).get(node)
            if lane is not None:
                return lane
    return None


def _lone_line_stations(graph: MetroGraph, layers: dict[str, int]) -> list[str]:
    """On-track stations no other station shares a ``(layer, primary line)`` with.

    These are seated on their own rather than as members of a fan.
    """
    groups: dict[tuple[int, str], list[str]] = defaultdict(list)
    for sid, station in graph.stations.items():
        primary = _primary_line(graph, sid)
        if primary is not None and not station.off_track:
            groups[(layers.get(sid, 0), primary)].append(sid)
    return [nodes[0] for nodes in groups.values() if len(nodes) == 1]


def _contender_seat_layer(node: str, G: nx.DiGraph[str], layers: dict[str, int]) -> int:
    """Layer after which contender *node* is seated: the one before its first join.

    Every station along its run up to the join is then placed, so the seat can
    see each of them.
    """
    return min(layers.get(c, 0) for c in G.successors(node)) - 1


def _peel_contenders(
    G: nx.DiGraph[str],
    graph: MetroGraph,
    layers: dict[str, int],
) -> dict[str, list[tuple[str, str]]]:
    """Sources whose line runs past a fork's peel branch, with the peels they pass.

    A source contends for a peel when one of its runs strictly spans the peel's
    layer, it feeds none of the fork's successors (so it is not part of the
    fork's fan), and it is seated on its own rather than as a member of a fan.
    Its seat waits until the layer before its first join, and every passed
    peel must be placed by then.  Returns source -> ``(fork, peel)`` pairs.
    """
    peels = [
        (fork, peel)
        for fork in G
        if not graph.stations[fork].is_port
        for peel in _peel_leads(fork, G, graph)
    ]
    if not peels:
        return {}
    layer = layers.get
    contenders: dict[str, list[tuple[str, str]]] = {}
    for node in _lone_line_stations(graph, layers):
        station = graph.stations[node]
        succs = set(G.successors(node))
        if G.in_degree(node) or station.is_port or station.is_hidden or not succs:
            continue
        passed = [
            (fork, peel)
            for fork, peel in peels
            if fork != node
            and succs.isdisjoint(G.successors(fork))
            and any(layer(node, 0) < layer(peel, 0) < layer(c, 0) for c in succs)
        ]
        seat_layer = _contender_seat_layer(node, G, layers)
        if passed and all(layer(peel, 0) <= seat_layer for _, peel in passed):
            contenders[node] = passed
    return contenders


def _span_tracks(
    node: str,
    join: int,
    graph: MetroGraph,
    layers: dict[str, int],
    tracks: dict[str, float],
) -> list[float]:
    """Tracks of on-track stations in layers strictly between *node* and *join*."""
    start = layers.get(node, 0)
    return [
        track
        for sid, track in tracks.items()
        if start < layers.get(sid, 0) < join and not graph.stations[sid].off_track
    ]


def _far_sides(
    passed: list[tuple[str, str]], tracks: dict[str, float]
) -> list[tuple[float, float]]:
    """``(fork track, sign)`` half-lines a contender must seat strictly inside.

    Each passed fork bars the side its peel branch opens to, so the contender's
    run stays off the branch and its diagonals.
    """
    return [
        (tracks[fork], -1.0 if tracks[peel] > tracks[fork] else 1.0)
        for fork, peel in passed
        if abs(tracks[peel] - tracks[fork]) > COORD_TOLERANCE_FINE
    ]


def _seat_contender(
    base: float,
    column: list[float],
    span: list[float],
    far_sides: list[tuple[float, float]],
    line_gap: float,
) -> float:
    """Track for a peel contender: the row nearest *base* its run can hold.

    The row must clear every station in its *column* and along its run (*span*)
    and lie strictly on the far side of each passed fork (*far_sides*).  A row
    one pitch from a column station leaves no empty row in the column, so those
    rows win over any further out, which step out from the column a pitch at a
    time.  When no row lies on the far side of every passed fork (forks barring
    both sides, or a window between them too narrow for a row), the forks bar
    nothing and the station clearances alone pick the seat.

    Rows beyond the hull of base, column, span and far_sides clear every
    station and sit on the far side of every fork barring that direction, so
    ``reach`` stops two pitches past it.
    """
    lo, hi = (min(column), max(column)) if column else (base, base)
    rows = [(0, t) for c in column for t in (c - line_gap, c + line_gap)]
    if lo - line_gap <= base <= hi + line_gap:
        rows.append((0, base))
    marks = [base, *column, *span, *(t for t, _ in far_sides)]
    reach = ceil((max(marks) - min(marks)) / line_gap) + 2
    rows += [
        (step, t)
        for step in range(1, reach + 1)
        for t in (lo - step * line_gap, hi + step * line_gap)
    ]

    def barred(track: float) -> bool:
        return not all(
            (track - t) * sign > COORD_TOLERANCE_FINE for t, sign in far_sides
        )

    clear = [
        (step, t)
        for step, t in rows
        if _clear_of(t, column, SAME_COORD_TOLERANCE)
        and _clear_of(t, span, line_gap / 2)
    ]
    _, seat = min(
        clear, key=lambda row: (barred(row[1]), row[0], abs(row[1] - base), row[1])
    )
    return seat


def _carrier_exit_stays_on_row(
    carrier: str,
    G: nx.DiGraph[str],
    graph: MetroGraph,
    exit_reaching: frozenset[str],
    line_base: dict[str, float] | None,
) -> bool:
    """Whether *carrier*'s exit-bound continuation stays on the carrier's row.

    A dead-end tail is peeled off the carrier's row only when a same-row exit
    route would otherwise run flat across its marker.  That route exists only
    when the carrier's continuation toward the exit rides the carrier's own row:
    if the exit-reaching successor sits on a different line base (the carrier is
    a below-trunk fork whose continuation rises back to the trunk), the exit
    route turns off the row before the tail's column and never crosses it, so the
    tail is already clear where it is.  A carrier whose exit leaves through the
    (subgraph-stripped) exit port has no in-section exit successor and rides its
    own row straight out, so it stays on-row.  Compared via line base rather
    than placed track so the answer does not depend on placement order.
    """
    if line_base is None:
        return True
    carrier_base = line_base.get(_primary_line(graph, carrier) or "")
    if carrier_base is None:
        return True
    for succ in G.successors(carrier):
        if succ not in exit_reaching:
            continue
        succ_base = line_base.get(_primary_line(graph, succ) or "")
        if (
            succ_base is not None
            and abs(carrier_base - succ_base) > COORD_TOLERANCE_FINE
        ):
            return False
    return True


def _predecessor_avg(
    node: str, G: nx.DiGraph[str], tracks: dict[str, float]
) -> float | None:
    """Average track position of a node's already-placed predecessors."""
    preds = [p for p in G.predecessors(node) if p in tracks]
    if not preds:
        return None
    return sum(tracks[p] for p in preds) / len(preds)


def _median_pred_track(preds: list[str], tracks: dict[str, float]) -> float | None:
    """Median track of a node's already-placed predecessors, or None.

    ``median_low`` returns an actual feeder track, so a merge anchored here
    stays on-grid even for an even feeder count (a mean would land on a
    half-track).
    """
    pred_tracks = [tracks[p] for p in preds if p in tracks]
    return median_low(pred_tracks) if pred_tracks else None


def _place_single_node(
    node: str,
    base: float,
    line_gap: float,
    G: nx.DiGraph[str],
    tracks: dict[str, float],
    graph: MetroGraph | None = None,
    layers: dict[str, int] | None = None,
    *,
    diamond_members: set[str] | None = None,
    layer_occupancy: dict[int, dict[str, float]] | None = None,
    continuation_predecessors: dict[str, str] | None = None,
    line_base: dict[str, float] | None = None,
    terminal_nodes: frozenset[str] = frozenset(),
    exit_reaching: frozenset[str] = frozenset(),
    flow_horizontal: bool = True,
) -> float:
    """Place a single node, choosing between line base track and predecessor proximity.

    At divergence points (predecessor has more lines than this node),
    snap to the line's base track so diverging branches fan out properly.
    Exception: diamond (fork-join) patterns stay compact near the trunk.

    Otherwise, if predecessors are close, snap to base. If far (a
    side-branch deep in the graph), stay near predecessors.

    A source (no predecessors) sits on its base unless that is a lane a sibling
    source fork claims for a peel branch (:func:`_source_peel_lanes`); a peel
    branch of a source fork takes the lane its fork claimed for it.
    """
    node_layer = layers.get(node, 0) if layers else 0
    pred_avg = _predecessor_avg(node, G, tracks)
    if pred_avg is None:
        if graph is None or line_base is None or layer_occupancy is None:
            return base
        return _seat_source(
            node,
            base,
            node_layer,
            line_gap,
            G,
            tracks,
            graph,
            layer_occupancy,
            line_base,
        )

    def _track_is_inheritable(track: float) -> bool:
        return layer_occupancy is None or not _is_track_occupied_at_layer(
            track, node_layer, layer_occupancy, node
        )

    if graph is not None and line_base is not None:
        peel_lane = _claimed_peel_lane(node, line_gap, G, tracks, graph, line_base)
        if peel_lane is not None and _track_is_inheritable(peel_lane):
            return peel_lane

    inherited_predecessor = (continuation_predecessors or {}).get(node)
    if inherited_predecessor in tracks and _track_is_inheritable(
        tracks[inherited_predecessor]
    ):
        return tracks[inherited_predecessor]

    # Detect divergence: predecessor has more lines than this node
    if graph is not None:
        preds = list(G.predecessors(node))
        node_lines = set(graph.station_lines(node))
        pred_lines: set[str] = set()
        for p in preds:
            pred_lines.update(graph.station_lines(p))
        if len(pred_lines) > len(node_lines):
            # An in-section merge carries its only successor on the merged track.
            if len(preds) == 1 and list(G.successors(preds[0])) == [node]:
                if G.in_degree(preds[0]) > 1:
                    return pred_avg
            # Check if this is a diamond (temporary fork-join)
            if diamond_members is not None and node in diamond_members:
                # Diamond: compress toward trunk for compact visual
                return pred_avg + (base - pred_avg) * DIAMOND_COMPRESSION
            else:
                # Terminal bundle peel-off: one line diverges from a
                # multi-line predecessor to visit its own short chain, ending
                # within the section.  Its formulaic base track is set by line
                # declaration order and can push the spur off the carrier's row
                # -- skipping a vacant row, or just dropping one row for no
                # reason -- so land instead on the nearest row (a reserved
                # per-line base or an already-placed track) between the
                # predecessor and the base that no station occupies at this
                # layer, preferring the carrier's own row when it is free.  The
                # spur's file-icon successor inherits this row for a straight
                # connector.
                #
                # A through-line diverging here continues to a section exit and
                # keeps its own base lane, so it is excluded (terminal_nodes)
                # and does not contend for the inner rows near the bundle.
                #
                # A dead-end tail that only feeds off-track outputs is not a
                # genuine terminus: if its carrier continues to a section exit
                # along the carrier's own row, landing it flat there would run
                # that exit route across its marker.
                if (
                    flow_horizontal
                    and len(preds) == 1
                    and preds[0] in exit_reaching
                    and _leads_only_to_off_track_output(node, G, graph)
                    and _carrier_exit_stays_on_row(
                        preds[0], G, graph, exit_reaching, line_base
                    )
                ):
                    return _peel_below_trunk(
                        pred_avg, line_gap, node_layer, layer_occupancy, node
                    )
                if (
                    len(preds) == 1
                    and node in terminal_nodes
                    and abs(base - pred_avg) > COORD_TOLERANCE_FINE
                    and layers is not None
                ):
                    candidates = set(tracks.values())
                    if line_base is not None:
                        candidates |= set(line_base.values())
                    candidate = _find_free_nearby_track(
                        tracks[preds[0]],
                        base,
                        node_layer,
                        layer_occupancy if layer_occupancy is not None else {},
                        candidates,
                        node,
                        allow_flat=True,
                    )
                    if candidate is not None:
                        return candidate
                # Permanent divergence: snap to base track
                return base

        # Detect convergence: node has more lines than its largest
        # predecessor (lines merging from different tracks). Snap to
        # base track so the main bundle stays compact and downstream
        # stations don't zigzag between the merged and base positions.
        if len(preds) > 1:
            pred_line_sets = [set(graph.station_lines(p)) for p in preds]
            max_pred_lines = max(len(pls) for pls in pred_line_sets)
            if len(node_lines) > max_pred_lines:
                # Genuine multi-track convergence: no single predecessor
                # carries the full bundle.  The base track is the first-
                # declared line's, often an extreme of the feeder spread, so
                # snapping there forces every other feeder into a longer
                # detour.  In symmetric mode anchor on the median feeder track,
                # minimising total feeder bend.
                if graph.diamond_style == "symmetric":
                    median = _median_pred_track(preds, tracks)
                    if median is not None:
                        return median
                return base

            # Trunk junction: at least one predecessor already carries the
            # full node-line bundle, so side branches (subset preds) merge
            # into the existing trunk here.  Anchor on the trunk's primary
            # line track instead of the predecessor centroid so the bundle
            # stays straight through the junction.
            if len(node_lines) >= 2 and any(
                pls == node_lines for pls in pred_line_sets
            ):
                return base

        # Diamond merge: when straight diamonds are active, snap the
        # join node back to the base track so lines return to the trunk
        # after an asymmetric fork-join.  The line's nominal base is its
        # trunk only when it is free here; a wide fan-out can push another
        # line's station onto that base track, so snapping would stack the
        # join on top of it.  When contested, keep the join with its fork
        # branches (their median track) instead of returning across the row.
        if (
            graph.diamond_style == "straight"
            and diamond_members is not None
            and len(preds) > 1
            and any(p in diamond_members for p in preds)
        ):
            base_contested = (
                layer_occupancy is not None
                and _is_track_occupied_at_layer(base, node_layer, layer_occupancy, node)
            )
            if not base_contested:
                return base
            median = _median_pred_track(preds, tracks)
            if median is not None:
                return median

        # Dead-end output tail at a through-station: this node carries a line
        # that leaves the section (so a same-line exit route runs along its
        # lane) yet its own forward path sinks only into off-track outputs, and
        # a predecessor continues to an exit.  Inheriting that predecessor's
        # trunk would leave the exit route running flat across this station's
        # marker, reading as if the tail feeds downstream.  A terminal tail
        # (carrying only lines that never leave the section) has no such
        # through-route and keeps the section's own trunk.
        carrier = next((p for p in preds if p in exit_reaching), None)
        if (
            flow_horizontal
            and node not in exit_reaching
            and node not in terminal_nodes
            and carrier is not None
            and _leads_only_to_off_track_output(node, G, graph)
            and _carrier_exit_stays_on_row(carrier, G, graph, exit_reaching, line_base)
        ):
            trunk = tracks.get(carrier, pred_avg)
            return _peel_below_trunk(trunk, line_gap, node_layer, layer_occupancy, node)

    # Direct single-predecessor alignment: when a node has exactly one
    # predecessor on the same line(s), snap to the predecessor's track
    # so the edge runs horizontally (important for terminus -> station).
    preds_list = list(G.predecessors(node))
    if len(preds_list) == 1 and preds_list[0] in tracks:
        pred = preds_list[0]
        if (
            graph is not None
            and _shares_line_set(pred, node, graph)
            and _track_is_inheritable(tracks[pred])
        ):
            return tracks[pred]

    distance = abs(base - pred_avg)
    if distance <= line_gap:
        # Close enough - snap to base track
        return base
    else:
        # Side-branch: stay near predecessors, nudge toward base
        direction = 1.0 if base > pred_avg else -1.0
        return pred_avg + direction * SIDE_BRANCH_NUDGE


def _is_diamond_fanout(nodes: list[str], G: nx.DiGraph[str]) -> bool:
    """Check if fan-out nodes form a diamond (shared preds and common successors).

    A diamond fan-out is a fork-join where all nodes share exactly the same
    predecessors and converge to at least one common successor.
    """
    if len(nodes) < 2:
        return False
    first_preds = set(G.predecessors(nodes[0]))
    if not first_preds:
        return False
    for node in nodes[1:]:
        if set(G.predecessors(node)) != first_preds:
            return False
    common_succs = set(G.successors(nodes[0]))
    for node in nodes[1:]:
        common_succs &= set(G.successors(node))
    return len(common_succs) > 0


def _uneven_reconverging_branches(
    nodes: list[str], G: nx.DiGraph[str], graph: MetroGraph | None
) -> dict[str, int] | None:
    """Return per-branch chain lengths when *nodes* form an uneven reconverging diamond.

    A reconverging diamond rejoins at a shared node, but (unlike a classic
    ``_is_diamond_fanout``) that node need not be an immediate successor: each
    branch may run as a simple chain through several stations before merging.
    When the branches reach that single shared reconvergence point in differing
    numbers of hops, the fork is *uneven*: the short branch should hold the
    trunk while the longer branch drops below it.

    Detection requires that every branch is a linear chain converging on one
    common merge node, and that the branch nodes, their shared predecessors,
    and the merge's feeders are all real (visible, non-port) stations.  Forks
    whose branches fan out further, or whose members are synthetic port/
    junction/phantom nodes, keep their existing placement.

    Returns a mapping ``branch -> chain length`` when the fork qualifies,
    otherwise ``None``.
    """
    if graph is None or len(nodes) < 2:
        return None

    def _is_real(sid: str) -> bool:
        st = graph.stations.get(sid)
        return st is not None and not st.is_port and not st.is_hidden

    if not all(_is_real(n) for n in nodes):
        return None

    shared_preds = set(G.predecessors(nodes[0]))
    for node in nodes[1:]:
        shared_preds &= set(G.predecessors(node))
    if not shared_preds or not all(_is_real(p) for p in shared_preds):
        return None

    immediate_common = set(G.successors(nodes[0]))
    for node in nodes[1:]:
        immediate_common &= set(G.successors(node))
    if immediate_common:
        return None

    chains = {
        node: chain
        for node in nodes
        if (chain := _linear_chain_to_merge(node, G)) is not None
    }
    if len(chains) != len(nodes):
        return None

    merges = {chain[-1] for chain in chains.values()}
    if len(merges) != 1:
        return None
    merge = merges.pop()
    if not all(_is_real(p) for p in G.predecessors(merge)):
        return None

    hops = {node: len(chain) for node, chain in chains.items()}
    if len(set(hops.values())) < 2:
        return None
    return hops


def _linear_chain_to_merge(branch: str, G: nx.DiGraph[str]) -> list[str] | None:
    """Return the linear chain from *branch* up to its reconvergence node.

    A branch qualifies when it runs as a simple chain -- one successor per
    step and (past the fork) one predecessor per step -- until it reaches a
    node with more than one predecessor (the reconvergence point).  The
    returned list runs from *branch* through to that reconvergence node.

    Returns ``None`` when the branch forks, dead-ends, or loops before
    reconverging.
    """
    chain = [branch]
    node = branch
    seen = {branch}
    while True:
        succs = list(G.successors(node))
        if len(succs) != 1:
            return None
        nxt = succs[0]
        if nxt in seen:
            return None
        chain.append(nxt)
        if G.in_degree(nxt) > 1:
            return chain
        seen.add(nxt)
        node = nxt


def _trunk_fanout_node(nodes: list[str], graph: MetroGraph | None) -> str | None:
    """Return the unique fan-out node carrying a strict superset of all siblings.

    When one sibling's line set strictly contains every other sibling's
    line set, it represents the trunk bundle while the others are
    branches.  Anchoring the trunk keeps the bundle straight through
    the fan-out.  Returns ``None`` if no such unique trunk exists.
    """
    if graph is None or len(nodes) < 2:
        return None
    # The trunk, if any, must be the node with the largest line set;
    # check only that candidate against the rest.
    line_sets = [(n, set(graph.station_lines(n))) for n in nodes]
    trunk, trunk_lines = max(line_sets, key=lambda nl: len(nl[1]))
    if all(
        other_lines < trunk_lines for other, other_lines in line_sets if other != trunk
    ):
        return trunk
    return None


def _shares_line_set(pred: str, node: str, graph: MetroGraph) -> bool:
    """Whether *node* carries exactly *pred*'s non-empty line set."""
    pred_lines = set(graph.station_lines(pred))
    return bool(pred_lines) and pred_lines == set(graph.station_lines(node))


def _root_fan_trunk(
    nodes: list[str],
    anchor: float,
    G: nx.DiGraph[str],
    graph: MetroGraph | None,
    station_base: dict[str, float],
    line_base: dict[str, float],
) -> tuple[str, float] | None:
    """The source-fan member whose chain the fan continues, and its siblings' side.

    Returns the lone predecessor-less member whose only successor it alone
    feeds, carries more lines, and is based on the anchor, with the track-axis
    sign its siblings open toward: away from the base of the lines the
    successor adds.  ``None`` when no single member qualifies or the side is
    not definite.
    """
    if graph is None or any(G.in_degree(node) for node in nodes):
        return None
    qualifying: list[tuple[str, str]] = []
    for member in nodes:
        successors = list(G.successors(member))
        if len(successors) != 1:
            continue
        (successor,) = successors
        if list(G.predecessors(successor)) == [member] and not _shares_line_set(
            member, successor, graph
        ):
            qualifying.append((member, successor))
    if len(qualifying) != 1:
        return None
    member, successor = qualifying[0]
    successor_base = station_base.get(successor)
    if successor_base is None or abs(successor_base - anchor) > COORD_TOLERANCE_FINE:
        return None
    added = set(graph.station_lines(successor)) - set(graph.station_lines(member))
    added_bases = [line_base[lid] for lid in added if lid in line_base]
    if not added_bases:
        return None
    lean = sum(added_bases) / len(added_bases) - anchor
    if abs(lean) <= COORD_TOLERANCE_FINE:
        return None
    return member, -1.0 if lean > 0 else 1.0


def _seat_around_trunk(
    nodes: list[str],
    trunk: str,
    away: float,
    anchor: float,
    line_gap: float,
    tracks: dict[str, float],
) -> None:
    """Pin *trunk* at *anchor* and alternate the other *nodes* outward from it.

    Siblings take whole *line_gap* steps, the first on the *away* side.  A
    fractional step would round onto a row, or collide with the trunk within
    one.
    """
    tracks[trunk] = anchor
    others = [node for node in nodes if node != trunk]
    for i, node in enumerate(others):
        tracks[node] = anchor + away * (-1) ** i * (i // 2 + 1) * line_gap


def _bypass_lane_side(
    trunk: str,
    branch: str,
    G: nx.DiGraph[str],
    graph: MetroGraph,
    priority_lane_sign: float | None,
) -> float:
    """The side of *trunk*'s track a fan *branch* takes: ``+1.0`` or ``-1.0``.

    A bypass -- a branch fed from the trunk's lone feeder that runs as a chain
    back into the trunk downstream -- rides the fork's bundle until it peels
    off, so it opens toward the side its lines stack on relative to the lines
    staying on the trunk; the other side sends both its legs across them.  Any
    other branch, a tie in line rank, or a section whose lanes may not follow
    line priority takes the allocator's default ``+1.0``.
    """
    if priority_lane_sign is None:
        return 1.0
    feeders = set(G.predecessors(trunk))
    if len(feeders) != 1 or set(G.predecessors(branch)) != feeders:
        return 1.0
    chain = _linear_chain_to_merge(branch, G)
    if chain is None or chain[-1] not in nx.descendants(G, trunk):
        return 1.0
    branch_lines = set(graph.station_lines(branch))
    staying = set(graph.station_lines(trunk)) - branch_lines
    if not branch_lines or not staying:
        return 1.0
    rank = {lid: i for i, lid in enumerate(graph.lines)}

    def mean_rank(lines: set[str]) -> float:
        return sum(rank.get(lid, len(rank)) for lid in lines) / len(lines)

    rank_gap = mean_rank(branch_lines) - mean_rank(staying)
    if rank_gap == 0.0:
        return 1.0
    return priority_lane_sign if rank_gap > 0 else -priority_lane_sign


def _phantom_trunk_node(nodes: list[str], graph: MetroGraph | None) -> str | None:
    """Return the lone entry-runway phantom in a fan-out group, if any.

    A phantom pass-through (inserted so a deep-entering line keeps its own
    early-layer track) represents that line's through-trunk into the
    convergence target.  When it shares a layer with real same-line
    stations, those reals are fan-in branches merging onto the trunk -- so
    the phantom, not a real station, should hold the anchor track and the
    branches fan off it.  Without this the symmetric fan-out splits the
    phantom and the branch evenly, dragging the trunk off-axis into a
    zig-zag (#420).
    """
    if graph is None:
        return None
    phantoms = [
        n
        for n in nodes
        if (st := graph.stations.get(n)) is not None
        and st.is_hidden
        and n.startswith("_phantom_")
    ]
    if len(phantoms) == 1 and len(nodes) > 1:
        return phantoms[0]
    return None


def _is_output_node(node: str, graph: MetroGraph) -> bool:
    """Whether *node* is a file-icon terminus or an off-track output station.

    A file-icon leaf is only flagged ``off_track`` on the engine's re-run pass;
    on the first pass it reads as a terminus, so both are treated as outputs.
    """
    st = graph.stations.get(node)
    return st is not None and (st.off_track or st.is_terminus)


def _leads_only_to_off_track_output(
    node: str, G: nx.DiGraph[str], graph: MetroGraph
) -> bool:
    """Whether *node*'s whole forward path terminates at output file(s).

    True when *node* has at least one descendant and every descendant is a
    file-icon terminus or an off-track station -- i.e. it feeds only output
    files and continues no on-track chain.  Such a node is a short output spur,
    not the section's through-line, so at a fan-out it should peel off the
    trunk rather than take it.  A node with no descendants (a plain terminal
    marker) or with any on-track continuation is not a spur.

    Under ``diamond_style: straight`` a multi-hop dead-end chain (relay stations
    feeding an output, e.g. ``split -> depth -> coverage``) also counts: only the
    chain's sinks must be outputs, the on-track relays between are part of the
    spur.  This lets the continuing pass-through hold the straight trunk while
    the coverage chain peels off.  Symmetric mode keeps the stricter all-
    descendants test, since its fork-straddle centering owns that placement.
    """
    descendants = nx.descendants(G, node)
    if not descendants:
        return False

    if graph.diamond_style == "straight":
        sinks = [d for d in descendants if G.out_degree(d) == 0]
        return bool(sinks) and all(_is_output_node(d, graph) for d in sinks)
    return all(_is_output_node(d, graph) for d in descendants)


def _shared_trunk_pred(
    nodes: list[str],
    preds_by_node: dict[str, list[str]],
    exit_reaching: frozenset[str],
) -> str | None:
    """Return the fork's sole trunk-riding predecessor, if it has one.

    A fork contests the section trunk when every member is fed from the same
    single predecessor and that predecessor reaches a section exit -- so it
    carries the trunk into the fork.  Returns that predecessor, or ``None``
    when the members have differing or multiple predecessors, or the shared
    one does not reach an exit.  Predecessor membership is compared as a set, so
    ordering in *preds_by_node* does not matter.
    """
    first = set(preds_by_node[nodes[0]])
    if len(first) != 1 or any(set(preds_by_node[n]) != first for n in nodes[1:]):
        return None
    pred = next(iter(first))
    return pred if pred in exit_reaching else None


def split_output_spur_fan(
    candidates: list[str],
    exit_reaching: frozenset[str],
    G: nx.DiGraph[str],
    graph: MetroGraph,
    *,
    include_leaf_outputs: bool = False,
) -> tuple[list[str], list[str]] | None:
    """Split a fan into through-chain mains and off-track output spurs.

    Returns ``(mains, spurs)`` when every candidate is either a through-line
    reaching a section exit or a short spur dead-ending at an off-track output,
    with both groups non-empty; otherwise ``None``.  The section trunk belongs
    to the mains so the through-chain rides it and the spurs peel off; a fan
    with no genuine through-line (every branch ends in an output file) has no
    trunk to award and yields ``None``.

    With *include_leaf_outputs*, a candidate that is itself an output leaf (a
    file terminus with no descendants, not merely one relaying to such a leaf)
    also counts as a spur, so a fork forking straight into an output file is
    recognised alongside one forking into a relay chain that ends in output.
    """

    def _is_spur(node: str) -> bool:
        if node in exit_reaching:
            return False
        if include_leaf_outputs and G.out_degree(node) == 0:
            return _is_output_node(node, graph)
        return _leads_only_to_off_track_output(node, G, graph)

    mains = [node for node in candidates if node in exit_reaching]
    spurs = [node for node in candidates if _is_spur(node)]
    if mains and spurs and len(mains) + len(spurs) == len(candidates):
        return mains, spurs
    return None


def _place_fan_out(
    nodes: list[str],
    base: float,
    line_gap: float,
    G: nx.DiGraph[str],
    tracks: dict[str, float],
    *,
    straight_diamonds: bool = False,
    layer_idx: int = -1,
    graph: MetroGraph | None = None,
    exit_reaching: frozenset[str] = frozenset(),
    priority_lane_sign: float | None = None,
    station_base: dict[str, float],
    line_base: dict[str, float],
) -> None:
    """Place multiple nodes in the same layer+line, centered around an anchor.

    The anchor is the line's base track if predecessors are nearby,
    or the predecessor average if they're far away (fan-out from a branch).

    When *straight_diamonds* is True, diamond (fork-join) patterns use
    asymmetric placement: the first node stays at the anchor
    (straight-through) and only the alternative branch(es) fan out below.

    When *layer_idx* is 1, predecessors are at the entry layer (layer 0),
    so asymmetric (downward) placement is used to keep the entry station
    at the top of the section (#165).
    """
    # Compute barycenters for ordering
    bary: dict[str, float] = {}
    pred_avgs: list[float] = []
    for node in nodes:
        avg = _predecessor_avg(node, G, tracks)
        if avg is not None:
            bary[node] = avg
            pred_avgs.append(avg)
        else:
            bary[node] = base

    nodes.sort(key=lambda n: bary.get(n, base))

    # Decide anchor: base track or predecessor center
    overall_pred_avg = sum(pred_avgs) / len(pred_avgs) if pred_avgs else base
    anchor = base if abs(base - overall_pred_avg) <= line_gap else overall_pred_avg

    # Use sub-linear scaling so fan-outs don't grow proportionally:
    # per-item spacing = FANOUT_SPACING * n^(p-1) with p=0.8,
    # giving total spread ≈ FANOUT_SPACING * n^0.8.  Using n (not
    # n-1) as the base ensures 2-node fan-outs also get reduced
    # spacing, keeping per-option gaps consistent across fan sizes.
    n = len(nodes)
    fan_spacing = FANOUT_SPACING * n ** (0.8 - 1) if n > 1 else FANOUT_SPACING

    # Predecessor-snapping: when each node has exactly one predecessor
    # at a distinct track, snap to the predecessor's track so single-line
    # connections stay horizontal instead of slanting.
    preds_by_node = {node: list(G.predecessors(node)) for node in nodes}

    pred_snap: dict[str, float] = {}
    for node in nodes:
        preds = preds_by_node[node]
        if len(preds) == 1 and preds[0] in tracks:
            pred_snap[node] = tracks[preds[0]]
    if len(pred_snap) == n and len(set(pred_snap.values())) == n:
        for node in nodes:
            tracks[node] = pred_snap[node]
        return

    # Use asymmetric (downward) placement when:
    # - straight diamonds with diamond fan-out, OR
    # - predecessors are at the entry layer (layer 0) with a simple
    #   binary fan-out (2 nodes), so the entry station stays at the
    #   top of the section (#165).  Skip for larger fan-outs where
    #   symmetric placement avoids line crossings.
    use_asymmetric = False
    if straight_diamonds and _is_diamond_fanout(nodes, G):
        use_asymmetric = True
    elif layer_idx == 1 and n == 2:
        use_asymmetric = True

    # Phantom-trunk placement: an entry-runway phantom is the line's
    # through-trunk into a convergence target, so pin it at anchor and
    # fan the real fan-in branches above it -- the phantom enters from the
    # section's left edge at the trunk track, so the merging branches read
    # cleanest dropping in from above (note this fans the OPPOSITE way to
    # the line-superset trunk_node block below, which fans branches down by
    # default).  Keeps the trunk straight through the junction (#420).
    phantom_trunk = _phantom_trunk_node(nodes, graph)
    if phantom_trunk is not None:
        tracks[phantom_trunk] = anchor
        others = [n for n in nodes if n != phantom_trunk]
        for i, node in enumerate(others, 1):
            tracks[node] = anchor - i * fan_spacing
        return

    # Uneven reconverging diamond: branches rejoin at a shared descendant
    # after differing numbers of hops.  Keep the shortest branch on the
    # trunk and drop the longer branches below, so the short branch reads
    # as the through-line rather than floating on a symmetric loop.
    uneven_hops = (
        _uneven_reconverging_branches(nodes, G, graph) if straight_diamonds else None
    )
    if uneven_hops is not None:
        ordered = sorted(
            nodes, key=lambda node: (uneven_hops[node], bary.get(node, base))
        )
        tracks[ordered[0]] = anchor
        for i, node in enumerate(ordered[1:], 1):
            tracks[node] = anchor + i * fan_spacing
        return

    # Off-track-output spur peel: at the section entry, the fan of root
    # stations (no in-section predecessor, each fed straight from the entry
    # port) competes for the trunk.  When one root is the section's through-line
    # (reaches an exit port) and another dead-ends at an off-track output, keep
    # the through-root(s) centred on the anchor (the section trunk) and peel the
    # output spur(s) below them.  Otherwise the short spur takes the trunk and
    # the main chain is pushed onto a diagonal that crosses it (#1487).
    #
    # Gated tightly: only the root fan (a deeper fan-out sits on a placed
    # predecessor track and has no trunk to contest), and only when a genuine
    # through-line exists -- a terminal section whose branches all end in output
    # files has no trunk to award, so its fan keeps the default placement.
    if graph is not None and not pred_avgs:
        split = split_output_spur_fan(nodes, exit_reaching, G, graph)
        if split is not None:
            mains, spurs = split
            m = len(mains)
            for i, node in enumerate(mains):
                tracks[node] = anchor + (i - (m - 1) / 2) * fan_spacing
            bottom = max(tracks[node] for node in mains)
            for i, node in enumerate(spurs, 1):
                tracks[node] = bottom + i * fan_spacing
            return

    # Trunk-riding output-spur peel: a fork of a single trunk-riding
    # predecessor (one reaching a section exit) into exactly one continuing
    # through-station and one or more dead-end output spurs contests the trunk
    # just as the root fan above contests the section entry.  A default fan
    # splits the pair symmetrically and a later phase snaps the output terminus
    # back onto the trunk, leaving the through-station stranded off it; pin the
    # through-station to the trunk instead and peel the spur(s) off, so the
    # same-line exit route holds the trunk straight through its own station.
    # A member forking straight into an output file counts as a spur, not only
    # one relaying to one.
    #
    # Restricted to a lone real through-station carrying the predecessor's whole
    # line bundle: two or more through-branches already straddle the trunk
    # cleanly under the default fan; a hidden bypass phantom is not the visible
    # through-line; and a through-branch on a subset of the bundle is a branch
    # peeling off, not the trunk -- in each case the output terminus is the
    # trunk's natural continuation and must keep it.
    #
    # The spur peels above the trunk, the side the default fan already spreads
    # the through-station toward, so pinning it to the trunk leaves the
    # section's vertical extent where the unpinned fan would have put it rather
    # than opening a fresh lane below.  Its final off-trunk position is settled
    # by the off-track lift.
    if graph is not None and pred_avgs:
        trunk_pred = _shared_trunk_pred(nodes, preds_by_node, exit_reaching)
        if trunk_pred is not None and trunk_pred in tracks:
            split = split_output_spur_fan(
                nodes, exit_reaching, G, graph, include_leaf_outputs=True
            )
            # Only direct output leaves are peeled here.  The stranding this
            # corrects happens when an output terminus snaps back onto the trunk;
            # a spur that relays on to its output (a chain, not a leaf) does not
            # snap, and pinning its through-station instead disturbs bundle
            # geometry a later routing pass depends on.
            if (
                split is not None
                and len(split[0]) == 1
                and all(G.out_degree(spur) == 0 for spur in split[1])
            ):
                main = split[0][0]
                main_station = graph.stations.get(main)
                if (
                    main_station is not None
                    and not main_station.is_hidden
                    and _shares_line_set(trunk_pred, main, graph)
                ):
                    trunk = tracks[trunk_pred]
                    tracks[main] = trunk
                    for i, node in enumerate(split[1], 1):
                        tracks[node] = trunk - i * line_gap
                    return

    # Trunk-anchored placement: when one node carries a strict superset
    # of every sibling's line set, it's the bundle trunk.  Pin it at
    # anchor and fan the side branches off it so the trunk stays straight
    # through the junction: below, unless a branch is a bypass whose lines
    # stack on the other side.
    trunk_node = _trunk_fanout_node(nodes, graph)
    root_trunk = _root_fan_trunk(nodes, anchor, G, graph, station_base, line_base)
    if trunk_node is not None and graph is not None:
        tracks[trunk_node] = anchor
        depth = {-1.0: 0, 1.0: 0}
        for node in nodes:
            if node == trunk_node:
                continue
            side = _bypass_lane_side(trunk_node, node, G, graph, priority_lane_sign)
            depth[side] += 1
            tracks[node] = anchor + side * depth[side] * fan_spacing
    elif root_trunk is not None:
        _seat_around_trunk(nodes, *root_trunk, anchor, line_gap, tracks)
    elif use_asymmetric:
        # First node stays at anchor, others fan out below.
        tracks[nodes[0]] = anchor
        for i, node in enumerate(nodes[1:], 1):
            tracks[node] = anchor + i * fan_spacing
    else:
        # Symmetric fan-out centered around the anchor.
        for i, node in enumerate(nodes):
            offset = (i - (n - 1) / 2) * fan_spacing
            tracks[node] = anchor + offset


def _equalize_fork_groups(
    layer: int,
    layers: dict[str, int],
    tracks: dict[str, float],
    G: nx.DiGraph[str],
    graph: MetroGraph,
    node_primary: dict[str, str | None],
    line_gap: float,
    line_base: dict[str, float],
) -> None:
    """Redistribute cross-line fork siblings to equidistant spacing.

    When multiple stations at the same layer diverge from a common
    predecessor (or are root nodes entering from the same port),
    per-line base track assignment can create uneven spacing -- especially
    when one station carries more lines than its siblings, pushing the
    next sibling further away.

    This function detects such groups and compacts them to consecutive
    positions (one *line_gap* apart), preserving their track ordering.
    Groups where all members share the same primary line (diamonds /
    fan-outs already handled by ``_place_fan_out``) are skipped.

    In centred mode, a fork group whose members are
    all single-line exclusive stations is instead pinned to each member's
    own line's symmetric base track.  Consecutive repacking would collapse
    those exclusive runs toward the trunk midline (e.g. the bottom line's
    run snapping up onto the centre), defeating the balanced weave; pinning
    to the line base keeps each exclusive run on its own rail above/below
    the shared trunk.
    """
    layer_nodes = [sid for sid, lyr in layers.items() if lyr == layer and sid in tracks]
    if len(layer_nodes) < 2:
        return

    # Group stations by their predecessor set (fork siblings)
    pred_groups: dict[frozenset[str], list[str]] = defaultdict(list)
    for sid in layer_nodes:
        preds = frozenset(G.predecessors(sid))
        pred_groups[preds].append(sid)

    # Also group stations by their successor set (convergent siblings
    # fanning in to the same target, e.g. multiple inputs -> hidden hub)
    succ_groups: dict[frozenset[str], list[str]] = defaultdict(list)
    for sid in layer_nodes:
        succs = frozenset(G.successors(sid))
        if succs:
            succ_groups[succs].append(sid)

    # Merge both groupings, deduplicating by sorted member tuple.  A
    # convergent (successor) group that is a proper subset of a common-
    # predecessor fork is dropped: the larger fork already spaces all its
    # branches, and repacking the subset consecutively would ignore the
    # intervening siblings (longer branches that reach the shared sink via
    # extra stations) and collapse the subset onto their tracks.
    pred_member_sets = [set(g) for g in pred_groups.values() if len(g) >= 2]
    all_groups: dict[tuple[str, ...], list[str]] = {}
    for group in pred_groups.values():
        all_groups.setdefault(tuple(sorted(group)), group)
    for group in succ_groups.values():
        members = set(group)
        if any(members < fork for fork in pred_member_sets):
            continue
        all_groups.setdefault(tuple(sorted(group)), group)

    for group in all_groups.values():
        if len(group) < 2:
            continue

        # Skip groups where all members share the same primary line
        # (these are diamond / fan-out groups already well-placed).
        primaries = {node_primary.get(sid) for sid in group}
        primaries.discard(None)
        if len(primaries) < 2:
            continue

        # Centred mode: when every member is a single-line exclusive
        # station, pin each to its own line's symmetric base rail rather
        # than repacking them consecutively (which would drag exclusive
        # runs toward the trunk midline and unbalance the weave).
        centered = graph.line_spread is LineSpread.CENTERED
        if centered and all(len(graph.station_lines(sid)) == 1 for sid in group):
            for sid in group:
                primary = node_primary.get(sid)
                if primary is not None and primary in line_base:
                    tracks[sid] = line_base[primary]
            continue

        # Sort by primary line order first, then by current track position
        # within each line.  This keeps same-line siblings together.
        line_order_map = {lid: i for i, lid in enumerate(graph.lines)}
        reals = sorted(
            (sid for sid in group if not is_bypass_v(sid)),
            key=lambda sid: (
                line_order_map.get(node_primary.get(sid) or "", len(line_order_map)),
                tracks[sid],
            ),
        )
        # A hidden bypass-V carries one line of the exit-bound bundle around a
        # station that line skips.  Seat it beside the real station on its own
        # carried line, on the group's trunk-facing side (a sibling in the upper
        # half takes the helper just below it, one in the lower half just
        # above), so the bundle helpers gather around the feeder in the middle.
        v_helpers = [sid for sid in group if is_bypass_v(sid)]
        ordered = list(reals)
        for v in v_helpers:
            v_line = node_primary.get(v)
            same_line_reals = [r for r in reals if node_primary.get(r) == v_line]
            if not same_line_reals:
                ordered.append(v)
                continue
            upper_half = reals.index(same_line_reals[0]) < len(reals) / 2
            if upper_half:
                ordered.insert(ordered.index(same_line_reals[-1]) + 1, v)
            else:
                ordered.insert(ordered.index(same_line_reals[0]), v)
        group[:] = ordered

        # Compute current spacings between consecutive members
        spacings = [
            tracks[group[i + 1]] - tracks[group[i]] for i in range(len(group) - 1)
        ]

        # Check whether equalization is needed:
        #   2 stations  - gap exceeds line_gap (multi-line station padding)
        #   3+ stations - spacing is uneven
        if len(group) == 2:
            if spacings[0] <= line_gap + COORD_TOLERANCE_FINE:
                continue
        else:
            if max(spacings) - min(spacings) < COORD_TOLERANCE_FINE:
                continue

        # Distribute as signed offsets around an anchor so the column
        # stays centred on the trunk feeding the fork.  Anchor key:
        # most lines first (the in-column trunk), then closest to the
        # mean predecessor track, then lowest current track.  Source
        # columns (no predecessors) have no trunk to centre on, so the
        # anchor falls to group[0] (the topmost station), keeping
        # hidden hubs and exit ports at the section top rather than
        # drifting to the column centre.
        # Consecutive through-bundle bypass-V helpers belong to one trunk that
        # the offset engine spreads by the small track_gap; collapse each run of
        # them into a single fan slot so the bundle stays tight rather than
        # splaying its carried lines a full line_gap apart.
        slot_of: list[int] = []
        slot = 0
        prev_is_v = False
        for j, sid in enumerate(group):
            is_v = is_bypass_v(sid)
            continues_bypass_run = is_v and prev_is_v
            if j and not continues_bypass_run:
                slot += 1
            slot_of.append(slot)
            prev_is_v = is_v

        pred_tracks = [
            tracks[p] for sid in group for p in G.predecessors(sid) if p in tracks
        ]
        anchor_track: float
        anchor_slot: float
        if pred_tracks:
            pred_mean = sum(pred_tracks) / len(pred_tracks)

            # A bypass-V fan with no distinguished trunk (every member carries
            # the same line count) has no member to centre on: anchoring on one
            # would stack the rest to one side and drop the feeding source off
            # the fan midline (a deep U-well loop).  Anchor on the feeder track
            # at the group midpoint so the source stays on the midline.
            if v_helpers and len({len(graph.station_lines(s)) for s in group}) == 1:
                anchor_track = pred_mean
                anchor_slot = slot_of[-1] / 2
            else:

                def _anchor_key(sid: str) -> tuple[int, float, float]:
                    t = tracks[sid]
                    return (-len(graph.station_lines(sid)), abs(t - pred_mean), t)

                idx = min(range(len(group)), key=lambda i: _anchor_key(group[i]))
                anchor_track = tracks[group[idx]]
                anchor_slot = slot_of[idx]
        else:
            anchor_track = tracks[group[0]]
            anchor_slot = slot_of[0]
            if not any(G.in_degree(sid) for sid in group):
                slot_of = _source_slots_clear_of_peel_lanes(
                    group, slot_of, anchor_track, G, graph, line_base, line_gap
                )

        for i, sid in enumerate(group):
            tracks[sid] = anchor_track + (slot_of[i] - anchor_slot) * line_gap


def _source_slots_clear_of_peel_lanes(
    group: list[str],
    slot_of: list[int],
    anchor_track: float,
    G: nx.DiGraph[str],
    graph: MetroGraph,
    line_base: dict[str, float],
    line_gap: float,
) -> list[int]:
    """Push each source in a packed source column past claimed peel lanes.

    *group* packs top-down from *anchor_track* (slot 0).  A member whose slot
    lands on a lane claimed by a fork packed above it, and which does not feed
    that fork's fan, moves down a slot, carrying every later member with it.
    """
    packed: dict[str, float] = {}
    slots: list[int] = []
    shift = 0
    for sid, slot in zip(group, slot_of):
        slot += shift
        while (
            _foreign_claim_side(
                sid,
                anchor_track + slot * line_gap,
                packed,
                G,
                graph,
                line_base,
                line_gap,
            )
            is not None
        ):
            slot += 1
            shift += 1
        packed[sid] = anchor_track + slot * line_gap
        slots.append(slot)
    return slots


def _reorder_by_span(graph: MetroGraph, line_order: list[str]) -> list[str]:
    """Reorder lines by section span (descending).

    Lines that span more sections get earlier (inner) tracks.
    Ties are broken by preserving the original definition order.
    """
    if not graph.sections:
        return line_order

    # For each line, count how many distinct sections it touches
    line_sections: dict[str, set[str]] = {lid: set() for lid in line_order}
    for edge in graph.edges:
        lid = edge.line_id
        if lid not in line_sections:
            continue
        src, tgt = graph.edge_endpoints(edge)
        if src.section_id:
            line_sections[lid].add(src.section_id)
        if tgt.section_id:
            line_sections[lid].add(tgt.section_id)

    # Stable sort: descending by section count, preserving original order for ties
    return sorted(
        line_order,
        key=lambda lid: (-len(line_sections.get(lid, set())), line_order.index(lid)),
    )
