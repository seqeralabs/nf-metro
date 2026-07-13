"""Off-track input placement and phantom pass-through insertion."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterator

from nf_metro.layout.constants import (
    DIAGONAL_RUN,
    EXIT_GAP_MULTIPLIER,
    ICON_CAPTION_FONT_HEIGHT,
    ICON_CAPTION_GAP,
    ICON_HALF_HEIGHT,
    LABEL_BBOX_MARGIN,
    MIN_STRAIGHT_EDGE,
    SAME_COORD_TOLERANCE,
    SECTION_Y_PADDING,
    TERMINUS_WIDTH,
    X_SPACING,
    resolve_offset_step,
)
from nf_metro.layout.geometry import AxisFrame, lanes_run_along_x
from nf_metro.layout.labels import _label_text_height, label_text_width
from nf_metro.layout.phases._common import (
    _content_station_ids,
    _section_bundle_lines,
    _section_lr_port_anchor_y,
    dominant_value,
    flow_axis_exit_ports,
    grow_section_bbox_max_edge,
    grow_section_bbox_min_edge,
    move_section_bbox_max_edge,
    move_section_bbox_min_edge,
    section_axes,
    section_cross_axis,
)
from nf_metro.parser.model import (
    Edge,
    MetroGraph,
    PortSide,
    Section,
    Station,
    is_bypass_v,
)

# A producer this far off the trunk's baseline (on the fan side of the cross
# axis) is treated as sitting on a downward branch, so its off-track output is
# offset away from the trunk rather than lifting back across it.  Sized below a
# full slot so it ignores offset/rounding noise on a straight trunk yet fires on
# any genuine one-slot branch column.
_DOWNWARD_BRANCH_SLOP: float = ICON_HALF_HEIGHT

# Flat run reserved on the output icon's side after the diagonal, before the
# icon.  Half a station gap: enough for the line to read as a settled
# horizontal approach into the icon, without stretching the section.
_OUTPUT_TAIL: float = X_SPACING / 2

# How far past its station the following producer must sit so the next
# output's divergence passes cleanly under this output's icon (the next
# diagonal dips beneath the icon rather than clearing its full width, so this
# is about one glyph-width, not the icon's whole extent).
_ICON_RIGHT_REACH: float = TERMINUS_WIDTH


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

    _materialize_pass_through_lines(graph, section, sub)


def _materialize_pass_through_lines(
    graph: MetroGraph,
    section: Section,
    sub: MetroGraph,
) -> None:
    """Give a full-bundle entry->exit pass-through its line membership in *sub*.

    A real station whose only edges are inter-section -- fed by an entry port
    and feeding an exit port, with no internal neighbours -- is isolated (and so
    lineless) in the port-stripped subgraph.  The track allocator then treats it
    as an orphan and drops it on a bottom track, letting an in-section dead-end
    chain claim the section trunk while this through-station -- the one that
    actually carries the bundle across the section -- fans off it.

    Attaching a hidden phantom successor carrying the station's exit-port lines
    restores that membership without adding an internal predecessor (so the
    station stays a layer-0 root beside its entry-fed siblings).  The track
    allocator then sees it as the line-superset trunk of its fan and pins it on
    the trunk, so the continuing chain runs straight and the dead-end peels off.

    Narrow by construction to that trunk-theft shape: the pass-through must carry
    the full section bundle (it is the real trunk), and the section must contain a
    competing *subset chain* -- an on-track station carrying a proper subset of
    the bundle that has its own in-section edge, i.e. the dead-end run that would
    otherwise seize the trunk.  Without a subset chain there is nothing stealing
    the trunk, so materialising the pass-through would only reshuffle an already
    sound fan (e.g. two equal full-bundle pass-throughs).
    """
    bundle = _section_bundle_lines(graph, section)
    if not bundle:
        return

    def _is_subset_chain(sid: str, st: Station) -> bool:
        if st.is_hidden or st.is_port or st.off_track:
            return False
        lines = set(graph.station_lines(sid))
        return (
            bool(lines)
            and lines < bundle
            and bool(sub.edges_from(sid) or sub.edges_to(sid))
        )

    if not any(_is_subset_chain(sid, st) for sid, st in sub.stations.items()):
        return

    exit_port_ids = set(section.exit_ports)
    for sid, station in list(sub.stations.items()):
        if station.is_hidden or station.is_port or station.off_track:
            continue
        if sub.edges_from(sid) or sub.edges_to(sid):
            continue
        if set(graph.station_lines(sid)) != bundle:
            continue
        fed_by_entry = any(e.source in section.entry_ports for e in graph.edges_to(sid))
        exit_lines = sorted(
            {e.line_id for e in graph.edges_from(sid) if e.target in exit_port_ids}
        )
        if not fed_by_entry or not exit_lines:
            continue
        phantom_id = f"_passthrough_{section.id}_{sid}"
        sub.add_station(
            Station(id=phantom_id, label="", section_id=section.id, is_hidden=True)
        )
        for line_id in exit_lines:
            sub.add_edge(Edge(source=sid, target=phantom_id, line_id=line_id))


def _off_track_output_lead(
    producer: Station,
    is_downward: bool,
) -> float:
    """Flat run reserved on the producer's side of an off-track output diagonal.

    An upward output rises away from the producer's name label, so it needs
    only the base straight edge before the diagonal -- a constant lead that
    keeps every upward output the same distance from its producer.  A downward
    drop turns toward the label, so it holds the diagonal past the label's far
    edge to clear the text.
    """
    lead = MIN_STRAIGHT_EDGE
    if is_downward and producer.label.strip():
        lead = max(lead, label_text_width(producer.label) / 2 + LABEL_BBOX_MARGIN)
    return lead


def _space_off_track_outputs(
    sub: MetroGraph,
    layers: dict[str, int],
    tracks: dict[str, float],
    x_spacing: float = X_SPACING,
) -> tuple[dict[str, float], dict[int, float]]:
    """Per-output X offset, plus a per-layer push to widen producer gaps.

    Each off-track output hangs off its producer with the same S-shape: a flat
    run (the producer-label clearance), a standard-slope diagonal, then a fixed
    flat tail (``_OUTPUT_TAIL``) into the icon.  The icon is placed at
    ``producer.x + lead + DIAGONAL_RUN + _OUTPUT_TAIL`` so the tail after the
    diagonal is exactly ``_OUTPUT_TAIL`` for every output.

    Returns ``(output_extra, layer_push)``:

    * ``output_extra`` maps each output station id to the X offset it gets on
      top of its layer base; the output sits at its producer's layer, so its
      base X equals the producer's X and the offset is the full run.
    * ``layer_push`` is a cumulative per-layer X push (analogous to the
      fork/join ``layer_extra``) that widens the trunk gap *after* a producer
      whose off-track output's horizontal extent (icon right edge plus margin)
      would otherwise overrun the normal one-pitch gap to the next station.
      Without it the output's up-right S-curve crosses the intervening trunk
      station and adjacent outputs bunch together.  The push is conditional:
      a producer whose output fits inside the normal gap adds nothing, so the
      trunk is never widened where no output needs the room.

    On-track stations keep their own columns; the output hangs in a row above
    or below the trunk.  ``output_extra`` is empty when no on-track station
    feeds an off-track output.
    """
    on_track_tracks = [
        t
        for sid, t in tracks.items()
        if not sub.stations[sid].off_track and not sub.stations[sid].is_hidden
    ]
    top_track = min(on_track_tracks) if on_track_tracks else 0.0

    output_extra: dict[str, float] = {}
    # Clearance each output demands before the next station: the full output
    # run out to its icon's right edge, so the following output's divergence
    # clears this icon rather than raking it.  The lead is constant for upward
    # outputs, so the demand is uniform and every divergence sits the same
    # distance before its successor.
    layer_demand: dict[int, float] = defaultdict(float)
    for sid, station in sub.stations.items():
        if station.off_track or station.is_hidden or sid not in layers:
            continue
        targets = [
            e.target
            for e in sub.edges_from(sid)
            if e.target in sub.stations and not sub.stations[e.target].is_hidden
        ]
        # A fork producer's branches already spread through its diverge gap, so
        # an output beside them needs no extra trunk room; only a linear
        # producer's single onward edge has to make space for its divergence.
        on_track_succ = sum(1 for t in targets if not sub.stations[t].off_track)
        for target_id in targets:
            target = sub.stations[target_id]
            if not target.off_track:
                continue
            is_downward = tracks.get(sid, top_track) > top_track
            lead = _off_track_output_lead(station, is_downward)
            output_extra[target_id] = lead + DIAGONAL_RUN + _OUTPUT_TAIL
            producer_layer = layers[sid]
            layers[target_id] = producer_layer
            if on_track_succ <= 1:
                clearance = lead + DIAGONAL_RUN + _OUTPUT_TAIL + _ICON_RIGHT_REACH
                layer_demand[producer_layer] = max(
                    layer_demand[producer_layer], clearance
                )

    layer_push: dict[int, float] = {}
    if layer_demand:
        max_layer = max(layers.values())
        cumulative = 0.0
        for layer in range(max_layer + 1):
            layer_push[layer] = cumulative
            # The producer's output occupies its own column gap; only the
            # excess beyond a normal pitch shifts the layers that follow.
            excess = layer_demand.get(layer, 0.0) - x_spacing
            if excess > 0:
                cumulative += excess

    return output_extra, layer_push


def _align_phantom_pass_throughs(
    sub: MetroGraph,
    tracks: dict[str, float],
) -> None:
    """Snap convergence nodes to their phantom pass-through's track.

    The phantom ensures a dedicated track for the bypassing line.
    Moving the convergence node (the phantom's sole successor) to that
    track keeps the trunk horizontal so the optional branch visually
    "bubbles" away from it.

    A convergence node fed by *several* phantoms -- one per line when a
    multi-line bundle enters a section and meets at one deep first
    station -- is the head of the section trunk, not a bubble. Snapping
    it to any single phantom's track would drag it off the trunk into a
    near-vertical onward climb, so such a node is left on its assigned
    trunk track.
    """
    phantom_track_of: dict[str, list[float]] = defaultdict(list)
    for sid, station in sub.stations.items():
        if not station.is_hidden or sid not in tracks:
            continue
        succs = {e.target for e in sub.edges_from(sid)}
        if len(succs) == 1:
            phantom_track_of[next(iter(succs))].append(tracks[sid])

    for succ, phantom_tracks in phantom_track_of.items():
        if len(phantom_tracks) == 1 and succ in tracks:
            tracks[succ] = phantom_tracks[0]


def _forced_branch_label_reach(
    layer: int,
    fork_layers: set[int],
    join_layers: set[int],
    out_targets: dict[str, set[str]],
    in_sources: dict[str, set[str]],
    layers: dict[str, int],
    tracks: dict[str, float],
    sub: MetroGraph,
    full_graph: MetroGraph | None,
    label_angle: float,
) -> float:
    """Horizontal reach of a two-branch bubble's forced-down branch label.

    In a two-branch bubble (``fork -> {top, bottom} -> join``) the top branch
    sits on the trunk track and normally labels above it, clear of the
    convergence/divergence diagonal.  An off-track output anchored at the bubble
    and lifted above the trunk blocks that upward flip, forcing the top branch's
    angled label down into the bubble where it rakes the bottom branch's
    diagonal.  Returns the forced label's tilted X-extent (lower-right corner of
    the rotated text box) so the caller can widen the bubble enough for the
    diagonal to start past the text; ``0.0`` when no branch label is forced
    down.
    """
    graph = full_graph or sub
    peer_map = out_targets if layer in fork_layers else in_sources
    fork_join_ids = [sid for sid, lyr in layers.items() if lyr == layer]
    rad = math.radians(label_angle)
    cos_a, sin_a = abs(math.cos(rad)), abs(math.sin(rad))

    reach = 0.0
    for fj_id in fork_join_ids:
        branches = peer_map.get(fj_id, set())
        branch_tracks = {tracks[b] for b in branches if b in tracks}
        if len(branch_tracks) != 2:
            continue
        top_track = min(branch_tracks)
        if not _bubble_anchor_has_lifted_output(graph, fj_id, branches):
            continue
        for branch_id in branches:
            if tracks.get(branch_id) != top_track:
                continue
            station = sub.stations.get(branch_id)
            if station and station.label.strip():
                footprint = (
                    label_text_width(station.label) * cos_a
                    + _label_text_height(station.label) * sin_a
                )
                reach = max(reach, footprint)
    return reach


def _bubble_anchor_has_lifted_output(
    graph: MetroGraph,
    fork_join_id: str,
    branches: set[str],
) -> bool:
    """Whether an off-track output is lifted above the bubble's top branch.

    The output is the producer-fed sink (no on-track consumer) that drops
    *above* the trunk, anchored at the fork/join station or at the bubble's
    top branch itself.  Such a sink occupies the column above the top branch
    and blocks that branch's angled label from flipping up.
    """
    below = _off_track_output_below(graph)
    anchor_of = _off_track_anchor_of(graph)
    candidate_anchors = {fork_join_id} | branches
    for off_id, anchor_id in anchor_of.items():
        if anchor_id not in candidate_anchors:
            continue
        off_st = graph.stations.get(off_id)
        if off_st is None or not off_st.off_track:
            continue
        if any(e.target == anchor_id for e in graph.edges_from(off_id)):
            continue
        if off_id in below:
            continue
        return True
    return False


def _fork_join_adjacency(
    sub: MetroGraph,
    full_graph: MetroGraph | None,
    section_station_ids: set[str] | None,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Successor/predecessor sets used for fork/join detection.

    With ``full_graph`` and ``section_station_ids`` the adjacency is built
    from every section-internal edge (including those touching exit/entry
    ports), so a station feeding both an internal successor and a port counts
    as a divergence.  Otherwise it falls back to ``sub``'s own edges.
    """
    out_targets: dict[str, set[str]] = defaultdict(set)
    in_sources: dict[str, set[str]] = defaultdict(set)
    if full_graph is not None and section_station_ids is not None:
        for edge in full_graph.edges:
            if (
                edge.source in section_station_ids
                and edge.target in section_station_ids
            ):
                out_targets[edge.source].add(edge.target)
                in_sources[edge.target].add(edge.source)
    else:
        for edge in sub.edges:
            out_targets[edge.source].add(edge.target)
            in_sources[edge.target].add(edge.source)
    return out_targets, in_sources


def _detect_fork_join_layers(
    out_targets: dict[str, set[str]],
    in_sources: dict[str, set[str]],
    layers: dict[str, int],
    tracks: dict[str, float],
) -> tuple[set[int], set[int]]:
    """Layers where tracks diverge (forks) or converge (joins).

    Only multi-track divergences/convergences count: a same-track fan-out
    needs no diagonal transition and so no extra room.  Port stations are
    absent from ``tracks`` and treated conservatively as possibly off-track.

    Forks in a single visible-track section are an exception: an exit-side
    port sits at the far boundary, so the diagonal already has ample room and
    a missing-track fork is skipped.  Join gaps are kept even then, because an
    entry port sits close to the first internal station.

    Bypass-V helpers (``__bypass_`` ids) are routing-only.  A V on its own
    off-trunk track must not flip an otherwise single-track section into
    multi-track, so its track is excluded and the owner's own track folded in,
    making a visible-vs-owner diagonal trigger a gap while a V-only off-trunk
    peer does not.
    """
    visible_tracks = {t for sid, t in tracks.items() if not is_bypass_v(sid)}
    is_single_track = len(visible_tracks) <= 1

    def _has_bypass(ids: set[str]) -> bool:
        return any(is_bypass_v(nid) for nid in ids)

    def _bypass_aware_tracks(ids: set[str], owner_sid: str) -> set[float]:
        """Visible peer tracks plus the owner's own track, V's removed."""
        result: set[float] = set()
        owner_track = tracks.get(owner_sid)
        if owner_track is not None:
            result.add(owner_track)
        for nid in ids:
            if is_bypass_v(nid):
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

    return fork_layers, join_layers


def _flow_axis_label_half(label: str, direction: str) -> float:
    """Half a label's extent along the section's flow axis.

    The fork/join gap reserves flow-axis room so the branch diagonal clears the
    fork/join station's label.  On an LR/RL trunk the label sits centred on the
    marker, so its width runs along the flow (X) axis.  On a TB/BT trunk the
    label sits beside the marker on the cross (X) axis -- opposite the branch
    diagonal -- so only its height falls on the flow (Y) axis.
    """
    if lanes_run_along_x(direction):
        return _label_text_height(label) / 2
    return label_text_width(label) / 2


def _fj_label_metrics(
    layer: int,
    layers: dict[str, int],
    tracks: dict[str, float],
    sub: MetroGraph,
    direction: str,
) -> tuple[float, set[float]]:
    """Widest half-label (along the flow axis) and occupied tracks on ``layer``."""
    fj_label_half = 0.0
    fj_tracks: set[float] = set()
    for sid, lyr in layers.items():
        if lyr == layer:
            station = sub.stations.get(sid)
            if station and station.label.strip():
                fj_label_half = max(
                    fj_label_half, _flow_axis_label_half(station.label, direction)
                )
            if sid in tracks:
                fj_tracks.add(tracks[sid])
    return fj_label_half, fj_tracks


def _loop_widening_for_label_half(label_half: float, x_spacing: float) -> float:
    """Extra loop room so a label of ``label_half`` half-width clears the fan's
    transition diagonals.  A whole pitch already sits between columns, so it is
    subtracted; the residual is scaled by an empirical ``1.5`` because the
    widening is applied to both the fork- and join-side gaps."""
    return max(0.0, (label_half * 2 + DIAGONAL_RUN - x_spacing) / 1.5)


def _wide_fan_bubble_extra(
    layer: int,
    fork_layers: set[int],
    join_layers: set[int],
    out_targets: dict[str, set[str]],
    in_sources: dict[str, set[str]],
    layers: dict[str, int],
    tracks: dict[str, float],
    sub: MetroGraph,
    x_spacing: float,
    fj_tracks: set[float],
    direction: str,
) -> float:
    """Extra bubble room for a wide (3+ branch) fan's off-track middle labels.

    For a multi-target fork / multi-source join the router skips bubble-station
    centring, so the flat run at the bubble end can be very short.  When a
    bubble station on the adjacent layer sits on a different track and carries a
    wide label, the flat run is widened to fit it.  The gap is added on both
    sides (after fork, before join), so each side contributes half the total.
    """
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

    bubble_label_half = 0.0
    if is_wide_fork:
        for sid, lyr in layers.items():
            if lyr == layer + 1 and sid in tracks and tracks[sid] not in fj_tracks:
                station = sub.stations.get(sid)
                if station and station.label.strip():
                    bubble_label_half = max(
                        bubble_label_half,
                        _flow_axis_label_half(station.label, direction),
                    )
    if is_wide_join:
        for sid, lyr in layers.items():
            if lyr == layer - 1 and sid in tracks and tracks[sid] not in fj_tracks:
                station = sub.stations.get(sid)
                if station and station.label.strip():
                    bubble_label_half = max(
                        bubble_label_half,
                        _flow_axis_label_half(station.label, direction),
                    )

    return _loop_widening_for_label_half(bubble_label_half, x_spacing)


def _iter_hub_branches(
    layer: int,
    fork_layers: set[int],
    join_layers: set[int],
    out_targets: dict[str, set[str]],
    in_sources: dict[str, set[str]],
    layers: dict[str, int],
) -> Iterator[tuple[str, set[str], int, bool]]:
    """Yield ``(hub, branch_ids, branch_layer, diverge)`` for each fork/join hub on
    ``layer``: a fork hub feeds the layer above, a join hub the layer below."""
    if layer in fork_layers:
        for hub, tgts in out_targets.items():
            if layers.get(hub) == layer:
                yield hub, tgts, layer + 1, True
    if layer in join_layers:
        for hub, srcs in in_sources.items():
            if layers.get(hub) == layer:
                yield hub, srcs, layer - 1, False


def _branch_tracks(
    branch_ids: set[str],
    branch_layer: int,
    layers: dict[str, int],
    tracks: dict[str, float],
) -> dict[str, float]:
    """Track of each branch station that actually sits on ``branch_layer``."""
    return {
        b: tracks[b]
        for b in branch_ids
        if b in tracks and layers.get(b) == branch_layer
    }


def _interior_branch_loop_floor(
    layer: int,
    fork_layers: set[int],
    join_layers: set[int],
    out_targets: dict[str, set[str]],
    in_sources: dict[str, set[str]],
    layers: dict[str, int],
    tracks: dict[str, float],
    sub: MetroGraph,
    x_spacing: float,
    direction: str,
) -> float:
    """Per-side loop gap so every interior branch label clears its diagonals.

    An interior branch has a fan sibling on a higher track and one on a lower
    track, so its label sits inside the loop with a diagonal on each side.  The
    loop must open wide enough for that label to clear those diagonals; the
    interior branch's own width - not the fork/join station's - sets how wide.
    Applied symmetrically as a floor on both the fork-side and join-side gap so
    the interior branch (which sits on the trunk and is not loop-recentred) stays
    centred - equal divergence and reconvergence runs.

    A thin (single-line) sibling bundle reconverges/diverges as one diagonal
    placed near the fork/join, so it never intrudes on the interior label; only a
    thick multi-line bundle starts its diagonal early and grazes it.  Each
    interior branch is sized independently and the widest requirement wins, so a
    narrow label fed by a thick bundle is not shadowed by a wide label fed by a
    thin one.
    """
    floor = 0.0
    for hub, branch_ids, branch_layer, diverge in _iter_hub_branches(
        layer, fork_layers, join_layers, out_targets, in_sources, layers
    ):
        btracks = _branch_tracks(branch_ids, branch_layer, layers, tracks)
        if len(btracks) < 3:
            continue
        top, bottom = min(btracks.values()), max(btracks.values())
        hub_edges = sub.edges_from(hub) if diverge else sub.edges_to(hub)
        sibling_lines = Counter((e.target if diverge else e.source) for e in hub_edges)
        for bid, bt in btracks.items():
            interior = top + SAME_COORD_TOLERANCE < bt < bottom - SAME_COORD_TOLERANCE
            station = sub.stations.get(bid)
            if not (interior and station and station.label.strip()):
                continue
            lines = max(
                (sibling_lines[sib] for sib in btracks if sib != bid), default=0
            )
            if lines < 2:
                continue
            half = _flow_axis_label_half(station.label, direction)
            floor = max(floor, _loop_widening_for_label_half(half, x_spacing))
    return floor


def _has_interior_branch(
    layer: int,
    fork_layers: set[int],
    join_layers: set[int],
    out_targets: dict[str, set[str]],
    in_sources: dict[str, set[str]],
    layers: dict[str, int],
    tracks: dict[str, float],
) -> bool:
    """Whether the fan puts a branch label inside the bubble.

    This needs three or more branches (two or more off-trunk tracks): the
    middle branch then has a diagonal on both sides.  A two-branch fork places
    both labels outside the bubble, so the convergence diagonal never crosses
    them.
    """
    if layer in fork_layers:
        for fsid, ftgts in out_targets.items():
            if layers.get(fsid) == layer and fsid in tracks:
                off = sum(1 for t in ftgts if t in tracks and tracks[t] != tracks[fsid])
                if off >= 2:
                    return True
    if layer in join_layers:
        for jsid, jsrcs in in_sources.items():
            if layers.get(jsid) == layer and jsid in tracks:
                off = sum(1 for s in jsrcs if s in tracks and tracks[s] != tracks[jsid])
                if off >= 2:
                    return True
    return False


def _angled_interior_reach(
    layer: int,
    fork_layers: set[int],
    layers: dict[str, int],
    tracks: dict[str, float],
    sub: MetroGraph,
    fj_tracks: set[float],
    label_angle: float,
) -> float:
    """Tilted-label horizontal reach on the adjacent bubble layer.

    Angled labels hang to the lower-right of each fan caller; with a branch
    label inside the bubble the convergence diagonal must start past the tilted
    text or it rakes the label.
    """
    cos_a = abs(math.cos(math.radians(label_angle)))
    adj_layer = layer + 1 if layer in fork_layers else layer - 1
    angled_reach = 0.0
    for sid, lyr in layers.items():
        if lyr == adj_layer and sid in tracks and tracks[sid] not in fj_tracks:
            station = sub.stations.get(sid)
            if station and station.label.strip():
                angled_reach = max(
                    angled_reach, label_text_width(station.label) * cos_a
                )
    return angled_reach


def _off_track_branch_needs_loop_room(
    layer: int,
    fork_layers: set[int],
    join_layers: set[int],
    out_targets: dict[str, set[str]],
    in_sources: dict[str, set[str]],
    layers: dict[str, int],
    tracks: dict[str, float],
    sub: MetroGraph,
) -> bool:
    """Whether an off-track branch dips through this fork/join and needs loop room.

    An off-track branch leaves the hub's track on a transition diagonal, so it
    needs the fork/join label's straight-run reservation kept for its own flat run
    (and to keep its dip centred).  A join's off-track source always dips up to the
    internal join station, so it always qualifies.  A fork's off-track target only
    qualifies if it reconverges at an internal station (an onward edge to a real
    station); a target that merely exits the section through a port - or a diamond
    whose branches all terminate at the fork - has no dip to keep room for, so the
    column packs tight.

    Requires a genuine fork/join (two or more targets/sources) whose branches span
    a transition diagonal: they must not all sit within ``SAME_COORD_TOLERANCE`` of
    the hub's own track.
    """
    for hub, branch_ids, branch_layer, diverge in _iter_hub_branches(
        layer, fork_layers, join_layers, out_targets, in_sources, layers
    ):
        if len(branch_ids) < 2:
            continue
        btracks = _branch_tracks(branch_ids, branch_layer, layers, tracks)
        hub_track = tracks.get(hub)
        diagonal_tracks = list(btracks.values())
        if hub_track is not None:
            diagonal_tracks.append(hub_track)
        if not diagonal_tracks:
            continue
        if max(diagonal_tracks) - min(diagonal_tracks) <= SAME_COORD_TOLERANCE:
            continue
        for bid, btrack in btracks.items():
            station = sub.stations.get(bid)
            if not (station and station.label.strip()):
                continue
            if (
                hub_track is not None
                and abs(btrack - hub_track) <= SAME_COORD_TOLERANCE
            ):
                continue
            if not diverge:
                return True
            if any(
                (nxt := sub.stations.get(e.target)) is not None and not nxt.is_port
                for e in sub.edges_from(bid)
            ):
                return True
    return False


def _layer_gap_for(
    layer: int,
    fork_layers: set[int],
    join_layers: set[int],
    out_targets: dict[str, set[str]],
    in_sources: dict[str, set[str]],
    layers: dict[str, int],
    tracks: dict[str, float],
    sub: MetroGraph,
    full_graph: MetroGraph | None,
    x_spacing: float,
    base_gap: float,
    label_angle: float,
    direction: str,
) -> float:
    """Column gap reserved at one fork/join layer.

    The gap must be large enough that the diagonal transition starts past the
    (possibly tilted) fork/join label and still has room for the transition.
    The router reserves the fork/join label's flow-axis half-extent as straight
    run but abandons it when the column gap can't also fit the diagonal run plus
    the branch-side minimum straight; the ``routing_clearance`` term reserves a
    gap sized to preserve that clearance.  A whole pitch already sits between
    columns, so it is subtracted; the 1px cushion absorbs float rounding in the
    router's drop test.
    """
    fj_label_half, fj_tracks = _fj_label_metrics(layer, layers, tracks, sub, direction)
    bubble_extra = _wide_fan_bubble_extra(
        layer,
        fork_layers,
        join_layers,
        out_targets,
        in_sources,
        layers,
        tracks,
        sub,
        x_spacing,
        fj_tracks,
        direction,
    )
    if label_angle:
        if _has_interior_branch(
            layer, fork_layers, join_layers, out_targets, in_sources, layers, tracks
        ):
            bubble_extra = max(
                bubble_extra,
                _angled_interior_reach(
                    layer, fork_layers, layers, tracks, sub, fj_tracks, label_angle
                ),
            )
        else:
            # An off-track output above the top branch forces that branch's
            # angled label down into the bubble, where it rakes the other
            # branch's diagonal; widen by the forced label's horizontal reach.
            bubble_extra = max(
                bubble_extra,
                _forced_branch_label_reach(
                    layer,
                    fork_layers,
                    join_layers,
                    out_targets,
                    in_sources,
                    layers,
                    tracks,
                    sub,
                    full_graph,
                    label_angle,
                ),
            )

    interior_floor = 0.0
    if not label_angle:
        interior_floor = _interior_branch_loop_floor(
            layer,
            fork_layers,
            join_layers,
            out_targets,
            in_sources,
            layers,
            tracks,
            sub,
            x_spacing,
            direction,
        )

    # The bare label-half over-reserves by x_spacing - routing_clearance; keep it
    # only where an off-track branch dips through the loop and needs that room.
    dip_label_half = (
        fj_label_half
        if _off_track_branch_needs_loop_room(
            layer,
            fork_layers,
            join_layers,
            out_targets,
            in_sources,
            layers,
            tracks,
            sub,
        )
        else 0.0
    )

    routing_clearance = (
        fj_label_half + DIAGONAL_RUN + MIN_STRAIGHT_EDGE - x_spacing + 1.0
    )
    return max(
        base_gap,
        routing_clearance + bubble_extra,
        interior_floor,
        dip_label_half,
    )


def _compute_fork_join_gaps(
    sub: MetroGraph,
    layers: dict[str, int],
    tracks: dict[str, float],
    x_spacing: float,
    full_graph: MetroGraph | None = None,
    section_station_ids: set[str] | None = None,
    direction: str = "LR",
) -> dict[int, float]:
    """Compute extra flow-axis offset per layer at fork/join points.

    Adds a fractional gap after fork layers (where tracks diverge) and
    before join layers (where tracks converge) so labels aren't obscured
    by diagonal crossings.  ``x_spacing`` is the flow-axis pitch and
    ``direction`` selects the flow axis; both default to the LR regime.

    When full_graph and section_station_ids are provided, fork/join
    detection uses all edges within the section (including port-touching
    edges). This catches divergences where a station connects to both
    internal stations and exit ports.

    In single-track sections (all stations on the same Y), port-bound
    divergences are suppressed because there are no diagonal transitions
    and the extra spacing is purely wasteful.
    """
    label_angle = (full_graph or sub).label_angle or 0.0
    out_targets, in_sources = _fork_join_adjacency(sub, full_graph, section_station_ids)
    fork_layers, join_layers = _detect_fork_join_layers(
        out_targets, in_sources, layers, tracks
    )

    if not fork_layers and not join_layers:
        return {}

    max_layer = max(layers.values()) if layers else 0
    base_gap = x_spacing * EXIT_GAP_MULTIPLIER

    layer_gap = {
        layer: _layer_gap_for(
            layer,
            fork_layers,
            join_layers,
            out_targets,
            in_sources,
            layers,
            tracks,
            sub,
            full_graph,
            x_spacing,
            base_gap,
            label_angle,
            direction,
        )
        for layer in fork_layers | join_layers
    }

    # A vertical (TB/BT) section is snapped to a whole-row grid, so a fractional
    # fork/join gap rounds unevenly across the diamond and tilts it.  Quantise
    # each gap to a whole flow-pitch row so equal fork- and join-side gaps stay
    # equal after the snap and the branch peels and reconverges symmetrically.
    if lanes_run_along_x(direction) and x_spacing > 0:
        layer_gap = {
            layer: round(gap / x_spacing) * x_spacing
            for layer, gap in layer_gap.items()
        }

    cumulative = 0.0
    layer_extra: dict[int, float] = {}
    for layer in range(max_layer + 1):
        if layer in join_layers:
            cumulative += layer_gap.get(layer, base_gap)
        layer_extra[layer] = cumulative
        if layer in fork_layers:
            cumulative += layer_gap.get(layer, base_gap)

    return layer_extra


def _line_crossed_file_icon_sinks(graph: MetroGraph) -> set[str]:
    """Leaf file-icon sinks whose icon a non-terminating line rakes across.

    Runs against a laid-out graph: routes the edges, builds each terminus
    icon's drawn bbox, and returns the id of every leaf file-icon sink
    (``is_terminus`` with an in-edge and no out-edge) whose box is crossed
    by a line segment that neither starts nor ends at that station and is
    not one of the station's own lines.

    These are the sinks an auto-lift must take off the trunk: leaving them
    on a line-track row puts the icon under a passing line.  A sink whose
    icon no line crosses is left alone, so an end-of-chain terminus that
    already sits clear is never lifted.
    """
    from nf_metro.layout.geometry import segment_intersects_bbox
    from nf_metro.layout.routing import (
        apply_route_offsets,
        compute_station_offsets,
        route_edges,
    )
    from nf_metro.render.svg import _icon_obstacles_by_station
    from nf_metro.themes import THEMES

    offsets = compute_station_offsets(graph)
    icon_boxes = _icon_obstacles_by_station(graph, THEMES["nfcore"], offsets)
    leaf_sinks = {
        sid
        for sid in icon_boxes
        if (st := graph.stations.get(sid)) is not None
        and not st.off_track
        and graph.edges_to(sid)
        and not graph.edges_from(sid)
    }
    if not leaf_sinks:
        return set()

    # A line leaving the sink's own vertical-flow (TB/BT) section through a
    # flow-axis (LEFT/RIGHT) exit port is the section's exit corridor, which
    # ``_resolve_tb_exit_y`` already seats below any terminus icon hanging
    # into the exit row.  That corridor is not a passing line raking the icon,
    # so it must not trigger an off-track lift -- the corridor moves, not the
    # station.  Collect each such sink's exit ports so the crossing scan skips
    # segments that arrive at (or leave from) them.
    exit_corridor_ports: dict[str, set[str]] = {}
    for sid in leaf_sinks:
        sec_id = graph.stations[sid].section_id
        sec = graph.sections.get(sec_id) if sec_id else None
        if sec is None or not lanes_run_along_x(sec.direction or "LR"):
            continue
        ports = flow_axis_exit_ports(sec, graph)
        if ports:
            exit_corridor_ports[sid] = ports

    try:
        routes = route_edges(graph, station_offsets=offsets)
    except Exception:  # noqa: BLE001 - routing failure surfaces elsewhere
        return set()

    crossed: set[str] = set()
    for r in routes:
        pts = apply_route_offsets(r, offsets)
        src, tgt = r.edge.source, r.edge.target
        for sid in leaf_sinks - crossed:
            if src == sid or tgt == sid:
                continue
            corridor = exit_corridor_ports.get(sid)
            if corridor and (src in corridor or tgt in corridor):
                continue
            bbox = icon_boxes[sid]
            for k in range(len(pts) - 1):
                p1, p2 = pts[k], pts[k + 1]
                if segment_intersects_bbox(p1[0], p1[1], p2[0], p2[1], bbox):
                    crossed.add(sid)
                    break
    return crossed


def _off_track_anchor_of(graph: MetroGraph) -> dict[str, str]:
    """Map each off-track station to the on-track same-section station it
    hangs next to.

    For an off-track *input*, the anchor is its consumer (the on-track
    station it feeds via an out-edge).  For an off-track *output* (a
    producer-fed sink with no on-track consumer), the anchor is its
    producer (the on-track station feeding it via an in-edge).  Both are
    placed *above* their anchor by the placement pass.

    Off-track stations with neither an on-track consumer nor producer get
    no entry; the caller falls back to the section's topmost on-track
    station.
    """
    junction_ids = graph.junction_ids

    def _on_track_neighbour(off_st: Station, edges: list[Edge]) -> str | None:
        for edge in edges:
            other_id = edge.target if edge.source == off_st.id else edge.source
            other = graph.stations.get(other_id)
            if other is None or other.is_port or other.off_track:
                continue
            if other.id in junction_ids or other.section_id != off_st.section_id:
                continue
            return other.id
        return None

    anchor_of: dict[str, str] = {}
    for sid, off_st in graph.stations.items():
        if not off_st.off_track or off_st.is_port or sid in junction_ids:
            continue
        if not off_st.section_id:
            continue
        consumer = _on_track_neighbour(off_st, graph.edges_from(sid))
        if consumer is not None:
            anchor_of[sid] = consumer
            continue
        producer = _on_track_neighbour(off_st, graph.edges_to(sid))
        if producer is not None:
            anchor_of[sid] = producer
    return anchor_of


def _iter_trunk_stations(
    graph: MetroGraph, section: Section, junction_ids: set[str]
) -> Iterator[Station]:
    """Yield a section's real on-track (trunk) stations.

    Skips ports, hidden phantoms, junctions, and off-track artefacts, leaving
    the visible stations that define the trunk.
    """
    for sid in section.station_ids:
        st = graph.stations.get(sid)
        if (
            st is not None
            and not st.is_port
            and not st.off_track
            and not st.is_hidden
            and sid not in junction_ids
        ):
            yield st


def _off_track_output_below(graph: MetroGraph) -> set[str]:
    """Off-track outputs whose producer sits on a branch off the trunk.

    An off-track output (a producer-fed sink) is normally offset to the *lift*
    side of its producer (:func:`_off_track_lift_sign`) -- above an LR trunk,
    beside a TB one.  When the producer sits off the trunk's baseline on the
    *fan* side of the cross axis -- i.e. it is on a branch column -- offsetting
    the output to the lift side forces it back across the trunk.  Such an output
    is instead offset the other way so it runs straight out.

    The comparison is on the section's cross axis (:func:`section_cross_axis`):
    Y for an LR/RL trunk, X for a TB/BT one.  The baseline is the cross
    coordinate the through-trunk runs on -- the column shared by the most
    on-track stations (:func:`dominant_value`), not the lift-most extreme, which
    an asymmetric fork branch overshooting the trunk toward the lift side would
    hijack.  A producer more than one slot onto the fan side of the baseline is a
    branch.  The slot floor keeps a straight trunk -- every on-track station on
    one cross coordinate -- inferring no branch outputs, so a TB trunk's normal
    descent along the flow axis is not misread as one.

    Only outputs (sinks) are considered; off-track inputs always lift toward
    their consumer's lift side.
    """
    junction_ids = graph.junction_ids
    anchor_of = _off_track_anchor_of(graph)

    # Per section: the cross axis, its fan sign, and the trunk baseline as a
    # signed coordinate (``fan_sign * cross``) anchored to the trunk column.
    section_baseline: dict[str, tuple[str, float, float]] = {}
    for section in graph.sections.values():
        cross = section_cross_axis(section)
        fan_sign = AxisFrame.secondary_sign_for(section.direction or "LR")
        trunk_sts = list(_iter_trunk_stations(graph, section, junction_ids))
        # A symmetric entry fork empties the trunk row -- both branches sit at
        # +/- half pitch, so the dominant station column is a branch, not the
        # trunk.  When every trunk station is a fanned branch, anchor the
        # baseline on the LR port instead, so an off-track output on a
        # below-trunk branch is recognised and dropped clear rather than lifted
        # back across the empty trunk.
        on_trunk = [s for s in trunk_sts if s.id not in graph.half_grid_station_ids]
        if trunk_sts and not on_trunk and cross == "y":
            port_y = _section_lr_port_anchor_y(graph, section)
            if port_y is not None:
                section_baseline[section.id] = (cross, fan_sign, fan_sign * port_y)
                continue
        signed = [fan_sign * getattr(st, cross) for st in trunk_sts]
        if signed:
            baseline_signed = dominant_value(round(v, 1) for v in signed)
            section_baseline[section.id] = (cross, fan_sign, baseline_signed)

    below: set[str] = set()
    for off_id, anchor_id in anchor_of.items():
        off_st = graph.stations.get(off_id)
        anchor_st = graph.stations.get(anchor_id)
        if off_st is None or anchor_st is None or not off_st.section_id:
            continue
        # Inputs feed their anchor; only producer-fed sinks may drop down.
        if any(e.target == anchor_id for e in graph.edges_from(off_id)):
            continue
        baseline = section_baseline.get(off_st.section_id)
        if baseline is None:
            continue
        cross, fan_sign, baseline_signed = baseline
        if (
            fan_sign * getattr(anchor_st, cross) - baseline_signed
            > _DOWNWARD_BRANCH_SLOP
        ):
            below.add(off_id)
    return below


def _off_track_groups(
    graph: MetroGraph,
) -> dict[str, tuple[str, dict[str, list[Station]]]]:
    """Group off-track stations by section and anchor.

    Returns a mapping ``section_id -> (fallback_anchor_id, groups)`` where
    ``groups`` maps anchor-station-id (or ``""`` for off-track stations
    with neither an on-track consumer nor producer) to a list of off-track
    stations hanging off that anchor.  An input's anchor is its consumer; a
    producer-fed sink's anchor is its producer (see
    :func:`_off_track_anchor_of`).  ``fallback_anchor_id`` is the topmost
    on-track station in the section, used for the ``""`` bucket.
    """
    junction_ids = graph.junction_ids

    by_section: dict[str, list[Station]] = defaultdict(list)
    for sid, st in graph.stations.items():
        if not st.off_track or st.is_port or sid in junction_ids:
            continue
        if not st.section_id:
            continue
        by_section[st.section_id].append(st)

    anchor_of = _off_track_anchor_of(graph)

    result: dict[str, tuple[str, dict[str, list[Station]]]] = {}
    for sec_id, off_stations in by_section.items():
        section = graph.sections.get(sec_id)
        if not section:
            continue
        anchor_pairs = [
            (st.y, st.id) for st in _iter_trunk_stations(graph, section, junction_ids)
        ]
        if not anchor_pairs:
            continue
        fallback_id = min(anchor_pairs)[1]
        groups: dict[str, list[Station]] = defaultdict(list)
        for st in off_stations:
            groups[anchor_of.get(st.id, "")].append(st)
        result[sec_id] = (fallback_id, groups)
    return result


def _section_distinct_trunk_cross_coords(
    graph: MetroGraph,
    section: Section,
    junction_ids: set[str],
) -> set[float]:
    """Distinct cross-axis coords of a section's on-track (trunk) stations.

    The cross axis is the one lines stack along: Y for an LR/RL section, X
    for a TB/BT one (:func:`section_cross_axis`).  On-track means a real,
    visible station: not a port, hidden phantom, junction, or off-track
    artefact.  Used to detect single-trunk sections (one distinct cross
    coord), which carry no parallel tracks.
    """
    axis = section_cross_axis(section)
    return {
        round(getattr(st, axis), 1)
        for st in _iter_trunk_stations(graph, section, junction_ids)
    }


def _is_single_trunk_section(
    graph: MetroGraph,
    section: Section | None,
    junction_ids: set[str],
) -> bool:
    """A section laid out as one trunk line (no parallel tracks), any flow."""
    return (
        section is not None
        and len(_section_distinct_trunk_cross_coords(graph, section, junction_ids)) == 1
    )


def _off_track_lift_step(
    graph: MetroGraph,
    section: Section | None,
    junction_ids: set[str],
    y_spacing: float,
) -> float:
    """Per-section cross-axis step for offsetting off-track stations.

    An off-track icon is offset from its anchor along the section's cross axis
    (:func:`section_cross_axis`): Y for an LR/RL trunk, X for a TB/BT one.

    A vertical-flow (TB/BT) section offsets along X by the resolved column
    pitch; a TB section suppresses diagonal labels, so that pitch is never
    widened and needs no base-pitch reduction.

    A horizontal-flow section offsets along Y by ``y_spacing``.  When it is a
    single horizontal trunk (no parallel tracks) the diagonal-label band that
    widened the graph-wide ``y_spacing`` is wasted vertical room that would
    strand the icon far above the trunk, so it lifts by the base content pitch
    (``graph._base_y_spacing``) instead.  Multi-track sections, and any section
    with no recorded base pitch, keep ``y_spacing``.  The base only applies when
    strictly smaller, so an explicit ``y_spacing`` below the base is never
    widened by this path.
    """
    if section is not None and section_cross_axis(section) == "x":
        return graph._resolved_x_spacing or X_SPACING
    base = graph._base_y_spacing
    if base is None or base >= y_spacing:
        return y_spacing
    if not _is_single_trunk_section(graph, section, junction_ids):
        return y_spacing
    return base


def _off_track_lift_sign(section: Section | None) -> float:
    """Cross-axis direction an off-track input is offset from its anchor.

    An LR/RL input lifts *above* the trunk (toward smaller Y); a TB input sits
    *beside* it.  The offset runs opposite the lane-fan direction
    (:attr:`AxisFrame.secondary_sign`) -- lanes fan down (+Y) on LR so inputs
    lift up (-Y); lanes fan left (-X) on TB so inputs sit right (+X).  A
    producer-fed sink on a downward branch is offset the other way (see
    :func:`_off_track_output_below`).
    """
    direction = section.direction if section is not None else "LR"
    return -AxisFrame.secondary_sign_for(direction or "LR")


def _per_column_stack_steps(
    innermost_first: list[Station], flow_axis: str
) -> dict[str, int]:
    """Stack rank (1 = innermost, nearest the anchor) keyed per column.

    ``innermost_first`` lists the off-track stations sharing one anchor in
    order of increasing distance from it.  A "column" is a shared flow-axis
    coordinate (X for an LR/RL trunk, Y for a TB/BT one); each column counts
    its own stack independently, so two stations sharing an anchor but sitting
    in different columns each start at rank 1 rather than towering one above
    the other.
    """
    steps: dict[str, int] = {}
    per_col: dict[float, int] = defaultdict(int)
    for st in innermost_first:
        col = round(getattr(st, flow_axis), 1)
        per_col[col] += 1
        steps[st.id] = per_col[col]
    return steps


def _place_off_track_relative_to_anchors(
    graph: MetroGraph,
    y_spacing: float,
    section_id: str,
    fallback_consumer_id: str,
    by_consumer: dict[str, list[Station]],
    below: set[str] | None = None,
) -> tuple[float | None, float | None]:
    """Offset each off-track station ``n`` steps from its anchor on the cross axis.

    The cross axis is the one the section stacks lines along
    (:func:`section_cross_axis`): Y for an LR/RL trunk, X for a TB/BT one.  The
    off-track station keeps its own flow-axis coordinate (its layer) and only
    its cross coordinate is set here, so an input sits beside where its data is
    read whatever the section's flow direction.

    Stations not in ``below`` are offset toward the lift side
    (:func:`_off_track_lift_sign`) -- *above* an LR trunk, *beside* a TB one;
    those in ``below`` -- producer-fed outputs on a downward branch -- are
    offset the opposite way so they run straight out instead of crossing back
    over the trunk.

    Multiple stations sharing an anchor stack in ``step`` increments away from
    it.  When the natural slot would put the icon on top of another trunk
    station's line band in the same column, the slot is bumped further from the
    anchor by additional ``step`` increments until the icon clears every
    line-bearing track in its column and every sibling off-track already placed
    in the same column.

    Returns ``(lift_extreme, opp_extreme)`` -- the cross coordinate of the
    station placed furthest toward the lift side, and furthest opposite it --
    with ``None`` for a side that placed no stations.
    """
    section = graph.sections.get(section_id)
    junction_ids = graph.junction_ids
    below = below or set()

    flow_axis, cross_axis = section_axes(section)
    lift_sign = _off_track_lift_sign(section)
    step = _off_track_lift_step(graph, section, junction_ids, y_spacing)

    # Track already-placed off-track cross coords per column so a bumped icon
    # doesn't crash into a sibling off-track already at the desired slot.
    used_cross_per_col: dict[float, list[float]] = defaultdict(list)

    # Iterate consumers from the side furthest opposite the lift first.  The
    # bumping mechanism only pushes toward the lift side, so placing the
    # furthest-opposite consumer's icon first lets subsequent icons stack past
    # it.  The resulting visual order matches the consumer order regardless of
    # edge declaration order in the mmd.
    def _consumer_anchor_cross(item: tuple[str, list[Station]]) -> float:
        cid = item[0] if item[0] else fallback_consumer_id
        a = graph.stations.get(cid)
        return lift_sign * getattr(a, cross_axis) if a is not None else 0.0

    ordered_consumers = sorted(by_consumer.items(), key=_consumer_anchor_cross)

    lift_extreme: float | None = None
    opp_extreme: float | None = None

    def _place(st: Station, candidate: float, bump_dir: float) -> None:
        nonlocal lift_extreme, opp_extreme
        if section is not None:
            candidate = _bump_off_track_clear_of_trunks(
                graph,
                st,
                candidate,
                step,
                section,
                junction_ids,
                sibling_cross=used_cross_per_col[round(getattr(st, flow_axis), 1)],
                direction=bump_dir,
            )
        setattr(st, cross_axis, candidate)
        used_cross_per_col[round(getattr(st, flow_axis), 1)].append(candidate)
        signed = lift_sign * candidate
        if bump_dir == lift_sign:
            if lift_extreme is None or signed > lift_sign * lift_extreme:
                lift_extreme = candidate
        elif opp_extreme is None or signed < lift_sign * opp_extreme:
            opp_extreme = candidate

    for consumer_id, stations in ordered_consumers:
        anchor_id = consumer_id if consumer_id else fallback_consumer_id
        anchor = graph.stations.get(anchor_id)
        if anchor is None:
            continue
        anchor_cross = getattr(anchor, cross_axis)
        # Preserve original order: station closest to the trunk stays innermost
        # in the stack.
        stations.sort(key=lambda s: (getattr(s, cross_axis), s.id))
        up_stations = [s for s in stations if s.id not in below]
        down_stations = [s for s in stations if s.id in below]

        up_steps = _per_column_stack_steps(list(reversed(up_stations)), flow_axis)
        down_steps = _per_column_stack_steps(down_stations, flow_axis)
        for st in up_stations:
            candidate = anchor_cross + lift_sign * up_steps[st.id] * step
            _place(st, candidate, lift_sign)
        for st in down_stations:
            candidate = anchor_cross - lift_sign * down_steps[st.id] * step
            _place(st, candidate, -lift_sign)
    return lift_extreme, opp_extreme


def _bump_off_track_clear_of_trunks(
    graph: MetroGraph,
    off_st: Station,
    candidate: float,
    step: float,
    section: Section,
    junction_ids: set[str],
    sibling_cross: list[float] | None = None,
    direction: float = -1.0,
) -> float:
    """Return *candidate* shifted so the off-track icon clears any trunk line
    track passing through the icon's column.

    The icon is offset from its anchor along the section's cross axis
    (:func:`section_cross_axis`).  A trunk station downstream of the icon in
    flow carries a band of line tracks spread across the cross axis; when that
    band overlaps the icon's cross extent, the run into that trunk crosses the
    icon.  Bump away from the anchor (``direction`` = the signed cross-step
    that offsets the icon toward *candidate*) by ``step`` increments until it
    clears.

    ``sibling_cross`` lists cross coords already taken by other off-track icons
    in the same column - the bump must also clear those (within one ``step``
    slot) so two icons don't end up in the same slot.

    Capped at six steps to avoid runaway offsets.
    """
    if step <= 0:
        return candidate

    flow_axis, cross_axis = section_axes(section)
    flow_sign = AxisFrame.flow_sign(section.direction or "LR")
    off_flow = getattr(off_st, flow_axis)
    # The icon's half-extent along the cross axis: file-icon half-height on Y,
    # half-width on X.
    icon_half = ICON_HALF_HEIGHT if cross_axis == "y" else TERMINUS_WIDTH / 2

    # A small margin so the icon's stroke doesn't touch a track.
    MARGIN = 2.0
    # Limit attempts so a pathological column doesn't pull the icon off-canvas.
    MAX_STEPS = 6

    offset_step = resolve_offset_step(graph.track_gap)

    # Find trunk stations in the same section whose line bundle crosses the
    # icon's column (they lie downstream of the icon in flow, so the run
    # feeding them passes the icon's flow coordinate).
    trunk_bands: list[float] = []
    for sid in section.station_ids:
        st2 = graph.stations.get(sid)
        if st2 is None or st2.is_port or st2.is_hidden:
            continue
        if st2.id == off_st.id or sid in junction_ids:
            continue
        if st2.off_track or st2.is_terminus:
            continue
        if flow_sign * (getattr(st2, flow_axis) - off_flow) <= SAME_COORD_TOLERANCE:
            continue
        # The line-track band spread across the cross axis at this trunk
        # station: each line sits at ``st2_cross + offset(line)``, bounded by
        # ``(n_lines - 1) * OFFSET_STEP`` centred on the station's cross coord.
        n_lines = len(graph.station_lines(sid))
        if n_lines == 0:
            continue
        half_span = (n_lines - 1) * offset_step / 2
        st2_cross = getattr(st2, cross_axis)
        trunk_bands.append(st2_cross - half_span)
        trunk_bands.append(st2_cross + half_span)

    sibs = list(sibling_cross or [])

    if not trunk_bands and not sibs:
        return candidate

    # A captioned icon's drawn box reaches past its centre along the flow axis
    # (the caption hangs beneath the icon), so it eats into a same-column
    # sibling's clearance only when the sibling stacks along that same axis --
    # i.e. on a horizontal (cross=Y) section.  On a vertical section the icons
    # stack along X while captions grow along Y, so the caption never encroaches.
    caption_reach = (
        ICON_CAPTION_GAP + ICON_CAPTION_FONT_HEIGHT
        if cross_axis == "y" and off_st.terminus_names and any(off_st.terminus_names)
        else 0.0
    )
    sibling_clearance = 2 * icon_half + caption_reach + MARGIN

    def _overlaps(c: float) -> bool:
        lo = c - icon_half - MARGIN
        hi = c + icon_half + MARGIN
        for band_lo, band_hi in zip(trunk_bands[::2], trunk_bands[1::2]):
            if not (hi < band_lo or band_hi < lo):
                return True
        for sc in sibs:
            if abs(sc - c) < sibling_clearance:
                return True
        return False

    steps = 0
    while _overlaps(candidate) and steps < MAX_STEPS:
        candidate += direction * step
        steps += 1
    return candidate


def _lift_off_track_stations(
    graph: MetroGraph,
    y_spacing: float,
    section_y_padding: float,
) -> None:
    """Offset off_track stations clear of their consumer's trunk slot.

    Off-track stations are file-input/output nodes that should not consume a
    line-track slot.  Each marked station is offset one step along its
    section's cross axis from its consumer (the on-track station it feeds) --
    above an LR/RL trunk, beside a TB/BT one -- so the input sits adjacent to
    where its data is read rather than at a uniform section band.  When several
    off-track inputs feed the same consumer, they stack in ``step`` increments.

    If an off-track station has no on-track consumer in the same section, it
    falls back to the section's topmost on-track station as its anchor.  After
    placement, the section bbox grows along the cross axis to fit the band, and
    same-side ports are nudged back to the new edge.

    Caller is responsible for invoking ``_shift_graph_into_canvas``
    afterwards: the bbox growth here can push the topmost section above the
    canvas top margin set by Stage 1.5.
    """
    groups = _off_track_groups(graph)
    if not groups:
        return

    below = _off_track_output_below(graph)
    for sec_id, (fallback_id, by_consumer) in groups.items():
        section = graph.sections.get(sec_id)
        if section is None:
            continue
        lift_extreme, opp_extreme = _place_off_track_relative_to_anchors(
            graph, y_spacing, sec_id, fallback_id, by_consumer, below
        )
        _grow_off_track_band_edges(
            graph,
            section,
            lift_extreme,
            opp_extreme,
            section_y_padding,
            fit_lift_edge=False,
        )


def _cross_bbox_edges(section: Section, axis: str) -> tuple[float, float]:
    """The section's ``(cross_min, cross_max)`` bbox edges on *axis*."""
    if axis == "x":
        return section.bbox_x, section.bbox_x + section.bbox_w
    return section.bbox_y, section.bbox_y + section.bbox_h


def _off_track_fit_edge(
    graph: MetroGraph,
    section: Section,
    axis: str,
    extreme: float,
    padding: float,
    *,
    toward_min: bool,
) -> float:
    """Cross-axis bbox edge giving the off-track band one full padding band.

    Returns ``extreme -/+ padding`` (minus toward the cross-min edge, plus
    toward the cross-max edge) clamped so the refit never clips other content
    sitting past the band, nor strands a port on the far side of the new edge.
    Ports on the edge being moved follow it and impose no bound.  Used by the
    reversible off-track reanchor: unlike the grow-only edge helpers, the
    caller applies this in both directions so a stale too-large box is
    reclaimed.
    """
    sign = -1.0 if toward_min else 1.0
    combine = min if toward_min else max
    target = extreme + sign * padding
    for cid in _content_station_ids(graph, section):
        target = combine(target, getattr(graph.stations[cid], axis) + sign * padding)
    moved_side = (
        (PortSide.TOP if axis == "y" else PortSide.LEFT)
        if toward_min
        else (PortSide.BOTTOM if axis == "y" else PortSide.RIGHT)
    )
    # Ports off the moved edge bound the fit so they aren't stranded past the
    # new edge; ports on that edge follow it.  This port clamp is deliberately
    # narrower than the bbox helpers' all-port clamp, so only the content set
    # is shared, not the port handling.
    for pid in section.entry_ports + section.exit_ports:
        port = graph.ports.get(pid)
        port_st = graph.stations.get(pid)
        if not port or not port_st or port.side == moved_side:
            continue
        target = combine(target, getattr(port_st, axis))
    return target


def _grow_off_track_band_edges(
    graph: MetroGraph,
    section: Section,
    lift_extreme: float | None,
    opp_extreme: float | None,
    padding: float,
    *,
    fit_lift_edge: bool,
) -> None:
    """Extend a section's bbox to fit its placed off-track band on the cross axis.

    ``lift_extreme`` / ``opp_extreme`` are the extreme cross coordinates from
    :func:`_place_off_track_relative_to_anchors`.  The lift side is the cross-min
    edge when inputs lift toward smaller coords (LR/RL up) or the cross-max edge
    when they sit toward larger coords (TB right); the opposite side is the other
    edge.  ``fit_lift_edge`` makes the lift-side edge reversible (grow **or**
    shrink to hug the band, for the reanchor passes); the opposite edge always
    grows only.
    """
    axis = section_cross_axis(section)
    lift_toward_min = _off_track_lift_sign(section) < 0

    def _fit_edge(coord: float | None, toward_min: bool, reversible: bool) -> None:
        if coord is None:
            return
        if not reversible:
            grow = (
                grow_section_bbox_min_edge if toward_min else grow_section_bbox_max_edge
            )
            grow(
                graph, section, axis, coord - padding if toward_min else coord + padding
            )
            return
        target = _off_track_fit_edge(
            graph, section, axis, coord, padding, toward_min=toward_min
        )
        edge = _cross_bbox_edges(section, axis)[0 if toward_min else 1]
        if abs(target - edge) > SAME_COORD_TOLERANCE:
            move = (
                move_section_bbox_min_edge if toward_min else move_section_bbox_max_edge
            )
            move(graph, section, axis, target)

    _fit_edge(lift_extreme, lift_toward_min, fit_lift_edge)
    _fit_edge(opp_extreme, not lift_toward_min, reversible=False)
    _grow_flow_edges_for_off_track_icons(graph, section)


def _grow_flow_edges_for_off_track_icons(graph: MetroGraph, section: Section) -> None:
    """Grow the flow-axis bbox edges to contain each off-track icon's extent.

    An off-track input sits a layer *back* along the flow axis from its
    consumer (the section's data enters before its first processing step), so
    its file icon can reach past the flow-back bbox edge -- the top of a TB
    section, whose inputs stack above the first station.  A horizontal section
    already reserves this flow-back room in its entry column, so the grow-only
    edges are a no-op there.
    """
    from nf_metro.layout.phases.single_section import _terminus_y_overhang

    flow, _cross = section_axes(section)
    junction_ids = graph.junction_ids
    for sid in section.station_ids:
        st = graph.stations.get(sid)
        if st is None or not st.off_track or st.is_port or sid in junction_ids:
            continue
        coord = getattr(st, flow)
        if flow == "y":
            above, below = _terminus_y_overhang(st, section.direction or "LR", graph)
        else:
            above = below = TERMINUS_WIDTH / 2
        grow_section_bbox_min_edge(graph, section, flow, coord - above)
        grow_section_bbox_max_edge(graph, section, flow, coord + below)


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

    **Precondition** (``graph._consumers_grid_snapped``): on-track
    consumers must already be grid-snapped (Stage 6.4).  Re-anchoring
    against non-final consumer Ys lands the icon off-grid, so the pass
    raises :class:`PhaseInvariantError` rather than depending on its
    call position to guarantee snapped consumers.

    The section top is then recomputed **to fit** the off-track band:
    it grows when the band rises above the current top minus padding and
    shrinks when an earlier (now stale) bbox top sits too tall, so the
    result is order-independent.  Same-section TOP ports follow the new
    top edge.

    Caller is responsible for invoking ``_shift_graph_into_canvas``
    afterwards: the upward bbox growth here can push the topmost
    section above the canvas top margin (mirrors the same caller
    contract as ``_lift_off_track_stations``).
    """
    # Function-local: a module-level import would close the cycle
    # off_track -> guards -> single_section -> off_track.
    from nf_metro.layout.phases.guards import PhaseInvariantError

    if not graph._consumers_grid_snapped:
        raise PhaseInvariantError(
            "_reanchor_off_track_to_consumer requires grid-snapped consumers "
            "(graph._consumers_grid_snapped); it must run after the Stage 6.4 "
            "snap, otherwise off-track icons re-anchor to non-final Ys"
        )
    groups = _off_track_groups(graph)
    if not groups:
        return

    below = _off_track_output_below(graph)
    for sec_id, (fallback_id, by_consumer) in groups.items():
        lift_extreme, opp_extreme = _place_off_track_relative_to_anchors(
            graph, y_spacing, sec_id, fallback_id, by_consumer, below
        )
        section = graph.sections.get(sec_id)
        if section is None:
            continue
        _grow_off_track_band_edges(
            graph,
            section,
            lift_extreme,
            opp_extreme,
            section_y_padding,
            fit_lift_edge=True,
        )
