"""Track-per-line vertical ordering.

Each metro line gets a dedicated horizontal track (Y position). Nodes on
the main path of a line snap to its base track. Short branches (nodes
whose predecessors are far from the line's base track) stay near their
predecessors instead of jumping to a distant track.
"""

from __future__ import annotations

__all__ = ["assign_tracks"]

from collections import defaultdict
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
from nf_metro.parser.model import LineSpread, MetroGraph


def assign_tracks(
    graph: MetroGraph,
    layers: dict[str, int],
    line_gap: float = LINE_GAP,
    *,
    entry_top: bool = False,
    continuation_nodes: frozenset[str] = frozenset(),
    terminal_nodes: frozenset[str] = frozenset(),
    exit_reaching: frozenset[str] = frozenset(),
) -> dict[str, float]:
    """Assign each station a track using the track-per-line strategy.

    Args:
        graph: The metro graph.
        layers: Layer assignment from assign_layers().
        line_gap: Fixed gap (in track units) between line base tracks.
        entry_top: When True, use asymmetric (downward) fan-out at the
            entry layer so the entry-connected station stays at the top.
        continuation_nodes: Stations that are the clean sole continuation of
            a line-shedding predecessor (whose only forward path is this
            node), which must hold the predecessor's track rather than drop
            to a line base. Computed with full-graph awareness by the caller,
            since this graph is a section subgraph blind to a predecessor's
            section-exit edges.
        terminal_nodes: Stations carrying only lines that do not leave the
            section, so their chain ends inside it (a terminal spur). Supplied
            by the caller because the section subgraph omits the exit-port
            edges that distinguish a spur from a through-line.
        exit_reaching: Stations with a forward path to a section exit port --
            the section's through-line. Supplied by the caller (the subgraph
            omits the exit-port edges), and used to keep the through-chain on
            the trunk when a short output spur shares its entry fan.

    Returns a dict mapping station_id -> track (float).
    """
    if not graph.lines:
        return {sid: float(i) for i, sid in enumerate(graph.stations)}

    G: nx.DiGraph[str] = nx.DiGraph()
    for edge in graph.edges:
        G.add_edge(edge.source, edge.target)
    for sid in graph.stations:
        if sid not in G:
            G.add_node(sid)

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
    node_base: dict[str, float] = {}
    if centered:
        for sid in graph.stations:
            node_lines = graph.station_lines(sid)
            bases = [line_base[ln] for ln in node_lines if ln in line_base]
            if bases:
                node_base[sid] = sum(bases) / len(bases)

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

    for layer_idx in range(max_layer + 1):
        for lid in line_order:
            nodes = layer_line_groups.get((layer_idx, lid), [])
            if not nodes:
                continue

            base = line_base[lid]

            if len(nodes) == 1:
                # Centred mode: a shared (multi-line) station anchors on the
                # mean of its lines' bases so the trunk stays on the midline.
                single_base = node_base.get(nodes[0], base) if centered else base
                tracks[nodes[0]] = _place_single_node(
                    nodes[0],
                    single_base,
                    line_gap,
                    G,
                    tracks,
                    graph,
                    layers,
                    diamond_members=diamond_members,
                    layer_occupancy=layer_occupancy,
                    continuation_nodes=continuation_nodes,
                    line_base=line_base,
                    terminal_nodes=terminal_nodes,
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
            line_base=line_base if centered else None,
        )

    return tracks


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
    for sid, sid_track in placed.items():
        if sid != exclude_node and abs(sid_track - track) < tolerance:
            return True
    return False


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
    continuation_nodes: frozenset[str] = frozenset(),
    line_base: dict[str, float] | None = None,
    terminal_nodes: frozenset[str] = frozenset(),
) -> float:
    """Place a single node, choosing between line base track and predecessor proximity.

    At divergence points (predecessor has more lines than this node),
    snap to the line's base track so diverging branches fan out properly.
    Exception: diamond (fork-join) patterns stay compact near the trunk.

    Otherwise, if predecessors are close, snap to base. If far (a
    side-branch deep in the graph), stay near predecessors.
    """
    pred_avg = _predecessor_avg(node, G, tracks)
    if pred_avg is None:
        return base

    # Detect divergence: predecessor has more lines than this node
    if graph is not None:
        preds = list(G.predecessors(node))
        node_layer = layers.get(node, 0) if layers else 0
        node_lines = set(graph.station_lines(node))
        pred_lines: set[str] = set()
        for p in preds:
            pred_lines.update(graph.station_lines(p))
        if len(pred_lines) > len(node_lines):
            # Linear trunk continuation: this node carries fewer lines because
            # some of its predecessor's lines ended at the predecessor, not
            # because it is a branch peeling off a fork.  When the predecessor's
            # only forward path is this node, there is no sibling branch to fan
            # toward, so continue the predecessor's track and keep the chain
            # flat instead of dropping onto a line base track.  Either the
            # predecessor is itself an in-section merge (#946) or full-graph
            # analysis flagged it as a clean sole continuation (#977); the
            # latter is needed because this subgraph cannot see a predecessor's
            # section-exit edge that would route a line around this node.
            if len(preds) == 1 and list(G.successors(preds[0])) == [node]:
                if G.in_degree(preds[0]) > 1 or node in continuation_nodes:
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

    # Direct single-predecessor alignment: when a node has exactly one
    # predecessor on the same line(s), snap to the predecessor's track
    # so the edge runs horizontally (important for terminus -> station).
    preds_list = list(G.predecessors(node))
    if len(preds_list) == 1 and preds_list[0] in tracks:
        pred = preds_list[0]
        if graph is not None:
            pred_line_set = set(graph.station_lines(pred))
            node_line_set = set(graph.station_lines(node))
            if pred_line_set and pred_line_set == node_line_set:
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

    A file-icon leaf is only flagged ``off_track`` on the engine's re-run pass;
    on the first pass it reads as a terminus, so both are treated as outputs
    here to catch the spur before any crossing forces that re-run.
    """
    descendants = nx.descendants(G, node)
    if not descendants:
        return False
    return all(
        (st := graph.stations.get(d)) is not None and (st.off_track or st.is_terminus)
        for d in descendants
    )


def split_output_spur_fan(
    candidates: list[str],
    exit_reaching: frozenset[str],
    G: nx.DiGraph[str],
    graph: MetroGraph,
) -> tuple[list[str], list[str]] | None:
    """Split a root fan into through-chain mains and off-track output spurs.

    Returns ``(mains, spurs)`` when every candidate is either a through-line
    reaching a section exit or a short spur dead-ending at an off-track output,
    with both groups non-empty; otherwise ``None``.  The section trunk belongs
    to the mains so the through-chain rides it and the spurs peel off; a fan
    with no genuine through-line (every branch ends in an output file) has no
    trunk to award and yields ``None``.
    """
    mains = [node for node in candidates if node in exit_reaching]
    spurs = [
        node
        for node in candidates
        if node not in exit_reaching and _leads_only_to_off_track_output(node, G, graph)
    ]
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
    if pred_avgs:
        overall_pred_avg = sum(pred_avgs) / len(pred_avgs)
        if abs(base - overall_pred_avg) <= line_gap:
            anchor = base
        else:
            anchor = overall_pred_avg
    else:
        anchor = base

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
    pred_snap: dict[str, float] = {}
    for node in nodes:
        preds = list(G.predecessors(node))
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
    # the line-superset trunk_node block below, which fans branches down).
    # Keeps the trunk straight through the junction (#420).
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

    # Trunk-anchored placement: when one node carries a strict superset
    # of every sibling's line set, it's the bundle trunk.  Pin it at
    # anchor and fan the side branches below so the trunk stays straight
    # through the junction.
    trunk_node = _trunk_fanout_node(nodes, graph)
    if trunk_node is not None:
        tracks[trunk_node] = anchor
        others = [n for n in nodes if n != trunk_node]
        for i, node in enumerate(others, 1):
            tracks[node] = anchor + i * fan_spacing
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
    *,
    line_base: dict[str, float] | None = None,
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

    In centred mode (*line_base* supplied), a fork group whose members are
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
        if line_base is not None and all(
            len(graph.station_lines(sid)) == 1 for sid in group
        ):
            for sid in group:
                primary = node_primary.get(sid)
                if primary is not None and primary in line_base:
                    tracks[sid] = line_base[primary]
            continue

        # Sort by primary line order first, then by current track position
        # within each line.  This keeps same-line siblings together.
        line_order_map = {lid: i for i, lid in enumerate(graph.lines)}
        group.sort(
            key=lambda sid: (
                line_order_map.get(node_primary.get(sid) or "", len(line_order_map)),
                tracks[sid],
            )
        )

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
        pred_tracks = [
            tracks[p] for sid in group for p in G.predecessors(sid) if p in tracks
        ]
        if pred_tracks:
            pred_mean = sum(pred_tracks) / len(pred_tracks)

            def _anchor_key(sid: str) -> tuple[int, float, float]:
                t = tracks[sid]
                return (-len(graph.station_lines(sid)), abs(t - pred_mean), t)

            anchor_idx = min(range(len(group)), key=lambda i: _anchor_key(group[i]))
        else:
            anchor_idx = 0

        anchor_track = tracks[group[anchor_idx]]
        for i, sid in enumerate(group):
            tracks[sid] = anchor_track + (i - anchor_idx) * line_gap


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
