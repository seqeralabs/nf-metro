"""Off-track input placement and phantom pass-through insertion."""

from __future__ import annotations

import math
from collections import defaultdict

from nf_metro.layout.constants import (
    DIAGONAL_RUN,
    EXIT_GAP_MULTIPLIER,
    ICON_HALF_HEIGHT,
    LABEL_BBOX_MARGIN,
    MIN_STRAIGHT_EDGE,
    OFFSET_STEP,
    SECTION_Y_PADDING,
    X_SPACING,
)
from nf_metro.layout.labels import label_text_width
from nf_metro.layout.phases._common import (
    _content_station_ys,
    _grow_section_bbox_downward,
    _grow_section_bbox_upward,
    _set_section_bbox_top,
)
from nf_metro.parser.model import Edge, MetroGraph, PortSide, Section, Station

# A producer this far below the section's top trunk row is treated as
# sitting on a downward branch, so its off-track output drops below it
# rather than lifting back across the trunk.  Sized below a full Y slot so
# it ignores offset/rounding noise on a flat trunk yet fires on any genuine
# one-slot downward branch.
_DOWNWARD_BRANCH_SLOP: float = ICON_HALF_HEIGHT

# Flat run reserved on the output icon's side after the diagonal, before the
# icon.  Half a station gap: enough for the line to read as a settled
# horizontal approach into the icon, without stretching the section.
_OUTPUT_TAIL: float = X_SPACING / 2


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


def _off_track_output_lead(
    producer: Station,
    n_out_targets: int,
    is_downward: bool,
) -> float:
    """Flat run reserved on the producer's side of an off-track output diagonal.

    Mirrors the straight-run length the router holds before the
    producer-to-output diagonal: a multi-target producer keeps the diagonal
    past half its name label, and a downward drop keeps it past the label's
    far edge (which sits below the producer, on the side the output drops
    toward).  A non-forking producer reserves only the base edge straight.
    """
    lead = MIN_STRAIGHT_EDGE
    if producer.label.strip():
        if n_out_targets > 1:
            lead = max(lead, label_text_width(producer.label) / 2)
        if is_downward:
            lead = max(lead, label_text_width(producer.label) / 2 + LABEL_BBOX_MARGIN)
    return lead


def _space_off_track_outputs(
    sub: MetroGraph,
    layers: dict[str, int],
    tracks: dict[str, float],
) -> dict[str, float]:
    """Per-output X offset so each off-track output hangs at a consistent run.

    Each off-track output hangs off its producer with the same S-shape: a flat
    run (the producer-label clearance), a standard-slope diagonal, then a fixed
    flat tail (``_OUTPUT_TAIL``) into the icon.  The icon is placed at
    ``producer.x + lead + DIAGONAL_RUN + _OUTPUT_TAIL`` so the tail after the
    diagonal is exactly ``_OUTPUT_TAIL`` for every output.

    Returns a map of each output station id to the X offset it gets on top of
    its layer base; the output sits at its producer's layer, so its base X
    equals the producer's X and the offset is the full run.  On-track stations
    keep their own columns -- the output hangs in a row above or below the
    trunk, so it shares X with the next station without overlapping it.  Empty
    when no on-track station feeds an off-track output.
    """
    on_track_tracks = [
        t
        for sid, t in tracks.items()
        if not sub.stations[sid].off_track and not sub.stations[sid].is_hidden
    ]
    top_track = min(on_track_tracks) if on_track_tracks else 0.0

    output_extra: dict[str, float] = {}
    for sid, station in sub.stations.items():
        if station.off_track or station.is_hidden or sid not in layers:
            continue
        targets = [
            e.target
            for e in sub.edges_from(sid)
            if e.target in sub.stations and not sub.stations[e.target].is_hidden
        ]
        for target_id in targets:
            target = sub.stations[target_id]
            if not target.off_track:
                continue
            is_downward = tracks.get(sid, top_track) > top_track
            lead = _off_track_output_lead(station, len(targets), is_downward)
            output_extra[target_id] = lead + DIAGONAL_RUN + _OUTPUT_TAIL
            layers[target_id] = layers[sid]

    return output_extra


def _align_phantom_pass_throughs(
    sub: MetroGraph,
    tracks: dict[str, float],
) -> None:
    """Snap convergence nodes to their phantom pass-through's track.

    The phantom ensures a dedicated track for the bypassing line.
    Moving the convergence node (the phantom's sole successor) to that
    track keeps the trunk horizontal so the optional branch visually
    "bubbles" away from it.
    """
    for sid, station in sub.stations.items():
        if not station.is_hidden or sid not in tracks:
            continue
        succs = {e.target for e in sub.edges_from(sid)}
        if len(succs) == 1:
            succ = next(iter(succs))
            if succ in tracks:
                tracks[succ] = tracks[sid]


def _compute_fork_join_gaps(
    sub: MetroGraph,
    layers: dict[str, int],
    tracks: dict[str, float],
    x_spacing: float,
    full_graph: MetroGraph | None = None,
    section_station_ids: set[str] | None = None,
) -> dict[int, float]:
    """Compute extra X offset per layer at fork/join points.

    Adds a fractional gap after fork layers (where tracks diverge) and
    before join layers (where tracks converge) so labels aren't obscured
    by diagonal crossings.

    When full_graph and section_station_ids are provided, fork/join
    detection uses all edges within the section (including port-touching
    edges). This catches divergences where a station connects to both
    internal stations and exit ports.

    In single-track sections (all stations on the same Y), port-bound
    divergences are suppressed because there are no diagonal transitions
    and the extra spacing is purely wasteful.
    """
    label_angle = (full_graph or sub).label_angle or 0.0

    out_targets: dict[str, set[str]] = defaultdict(set)
    in_sources: dict[str, set[str]] = defaultdict(set)

    # Use full graph edges for fork/join detection when available,
    # so that edges to/from port stations are counted as divergences.
    if full_graph is not None and section_station_ids is not None:
        for edge in full_graph.edges:
            src_in = edge.source in section_station_ids
            tgt_in = edge.target in section_station_ids
            if src_in and tgt_in:
                out_targets[edge.source].add(edge.target)
                in_sources[edge.target].add(edge.source)
    else:
        for edge in sub.edges:
            out_targets[edge.source].add(edge.target)
            in_sources[edge.target].add(edge.source)

    # Only count forks/joins that span multiple tracks (requiring a
    # diagonal routing transition).  Same-track fan-outs (e.g. a station
    # connecting to both an internal successor and an exit port on the
    # same Y) don't need extra horizontal room.
    #
    # Port stations aren't in ``tracks`` (they're positioned later), so
    # treat them conservatively: if any participant is missing from
    # tracks, assume it may be on a different track and count the
    # fork/join.
    #
    # Exception for **forks** in single-track sections: exit-side ports
    # sit at the far section boundary, so the diagonal from the fork
    # station has ample horizontal room without extra layer spacing.
    # Join gaps are kept even in single-track sections because entry
    # ports are close to the first internal station, and the diagonal
    # from a different-Y entry needs the extra room.
    # Bypass V helpers (id prefix ``__bypass_``) are routing-only.  A
    # V on its own off-trunk track must not flip an otherwise
    # single-track section into "multi-track", or it would turn
    # port-bound divergences into fork gaps that shift visible stations
    # rightward.  Specifically when a V is one of the fork/join peers:
    # exclude its track AND fold the owner's own track into the visible
    # set so that visible-vs-owner diagonals still trigger a gap, but a
    # V-only off-trunk peer (e.g. ``trim -> {align, V}`` in the 05 guide
    # family) does not.  When no V is involved, fall back to the original
    # peer-set track count so non-bypass topologies stay byte-identical.
    visible_tracks = {t for sid, t in tracks.items() if not sid.startswith("__bypass_")}
    is_single_track = len(visible_tracks) <= 1

    def _has_bypass(ids: set[str]) -> bool:
        return any(nid.startswith("__bypass_") for nid in ids)

    def _bypass_aware_tracks(ids: set[str], owner_sid: str) -> set[float]:
        """Visible peer tracks plus the owner's own track, V's removed."""
        result: set[float] = set()
        owner_track = tracks.get(owner_sid)
        if owner_track is not None:
            result.add(owner_track)
        for nid in ids:
            if nid.startswith("__bypass_"):
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

    if not fork_layers and not join_layers:
        return {}

    max_layer = max(layers.values()) if layers else 0
    base_gap = x_spacing * EXIT_GAP_MULTIPLIER

    # Compute per-layer gap scaled by label width at fork/join stations.
    # The gap must be large enough that the diagonal transition starts
    # past the label text and still has room for the transition itself.
    #
    # For multi-target forks / multi-source joins, bubble station
    # centering is skipped in routing, so the flat run at the bubble
    # end can be very short.  When bubble stations sit on different
    # tracks from the fork/join and have wide labels, add extra space
    # so the flat run accommodates them.
    layer_gap: dict[int, float] = {}
    for layer in fork_layers | join_layers:
        fj_label_half = 0.0
        fj_tracks: set[float] = set()
        for sid, lyr in layers.items():
            if lyr == layer:
                station = sub.stations.get(sid)
                if station and station.label.strip():
                    label_half = label_text_width(station.label) / 2
                    fj_label_half = max(fj_label_half, label_half)
                if sid in tracks:
                    fj_tracks.add(tracks[sid])

        # Check adjacent bubble layer for off-track stations with
        # wide labels.  Only applies for wide fan-outs (3+ off-track
        # targets/sources) where bubble station centering is skipped
        # in routing and middle stations must have inside labels.
        bubble_label_half = 0.0
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
        if is_wide_fork:
            for sid, lyr in layers.items():
                if lyr == layer + 1 and sid in tracks and tracks[sid] not in fj_tracks:
                    station = sub.stations.get(sid)
                    if station and station.label.strip():
                        bubble_label_half = max(
                            bubble_label_half, label_text_width(station.label) / 2
                        )
        if is_wide_join:
            for sid, lyr in layers.items():
                if lyr == layer - 1 and sid in tracks and tracks[sid] not in fj_tracks:
                    station = sub.stations.get(sid)
                    if station and station.label.strip():
                        bubble_label_half = max(
                            bubble_label_half, label_text_width(station.label) / 2
                        )

        # The bubble station is centered on its flat run.  The total
        # space needed is 2 * label_half + DIAGONAL_RUN, but the gap
        # is added on BOTH sides (after fork, before join), so each
        # side contributes half the total requirement.
        bubble_extra = max(
            0.0, (bubble_label_half * 2 + DIAGONAL_RUN - x_spacing) / 1.5
        )

        # Angled labels (label_angle) hang to the lower-right of each fan
        # caller; the fan-out/fan-in diagonal on that side must start past
        # the tilted text or it rakes the label.  Reserve the caller
        # label's rightward reach (the narrow rotated footprint, not the
        # full horizontal width) as extra column room.  Unlike the
        # horizontal-label case above this applies to any multi-track fan,
        # not only wide (3+) ones, since a single hanging name is enough to
        # clash with the convergence diagonal.
        if label_angle:
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
            bubble_extra = max(bubble_extra, angled_reach)

        layer_gap[layer] = max(base_gap, fj_label_half + bubble_extra)

    cumulative = 0.0
    layer_extra: dict[int, float] = {}
    for layer in range(max_layer + 1):
        # Add gap before join layers
        if layer in join_layers:
            cumulative += layer_gap.get(layer, base_gap)
        layer_extra[layer] = cumulative
        # Add gap after fork layers
        if layer in fork_layers:
            cumulative += layer_gap.get(layer, base_gap)

    return layer_extra


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


def _off_track_output_below(graph: MetroGraph) -> set[str]:
    """Off-track outputs whose producer sits on a downward branch.

    An off-track output (a producer-fed sink) is normally lifted *above*
    its producer.  When the producer sits below the section's top trunk
    row -- i.e. it is on a downward branch off the trunk -- lifting the
    output up forces it back across the trunk to sit above it.  Such an
    output is instead dropped *below* its producer so a downward-branch
    output runs straight down.

    The trunk row is taken to be the topmost on-track station Y in the
    section (the same fallback anchor :func:`_off_track_groups` uses).  A
    producer more than one ``y_spacing``-equivalent slot below that row is
    treated as a downward branch; the slot floor keeps a flat trunk (every
    on-track station at one Y) inferring no downward outputs.

    Only outputs (sinks) are considered; off-track inputs always lift above
    their consumer.
    """
    junction_ids = graph.junction_ids
    anchor_of = _off_track_anchor_of(graph)

    section_top_y: dict[str, float] = {}
    for section in graph.sections.values():
        ys = [
            graph.stations[sid].y
            for sid in section.station_ids
            if sid in graph.stations
            and not graph.stations[sid].is_port
            and not graph.stations[sid].off_track
            and sid not in junction_ids
        ]
        if ys:
            section_top_y[section.id] = min(ys)

    below: set[str] = set()
    for off_id, anchor_id in anchor_of.items():
        off_st = graph.stations.get(off_id)
        anchor_st = graph.stations.get(anchor_id)
        if off_st is None or anchor_st is None or not off_st.section_id:
            continue
        # Inputs feed their anchor; only producer-fed sinks may drop down.
        if any(e.target == anchor_id for e in graph.edges_from(off_id)):
            continue
        top_y = section_top_y.get(off_st.section_id)
        if top_y is None:
            continue
        if anchor_st.y > top_y + _DOWNWARD_BRANCH_SLOP:
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
            (graph.stations[sid].y, sid)
            for sid in section.station_ids
            if sid in graph.stations
            and not graph.stations[sid].is_port
            and not graph.stations[sid].off_track
            and sid not in junction_ids
        ]
        if not anchor_pairs:
            continue
        fallback_id = min(anchor_pairs)[1]
        groups: dict[str, list[Station]] = defaultdict(list)
        for st in off_stations:
            groups[anchor_of.get(st.id, "")].append(st)
        result[sec_id] = (fallback_id, groups)
    return result


def _section_distinct_trunk_ys(
    graph: MetroGraph,
    section: Section,
    junction_ids: set[str],
) -> set[float]:
    """Distinct Y values of a section's on-track (trunk) stations.

    On-track means a real, visible station: not a port, hidden phantom,
    junction, or off-track artefact.  Used to detect single-trunk sections
    (one distinct Y), which carry no parallel tracks.
    """
    return {
        round(st.y, 1)
        for sid in section.station_ids
        if (st := graph.stations.get(sid)) is not None
        and not st.is_port
        and not st.is_hidden
        and not st.off_track
        and sid not in junction_ids
    }


def _is_single_trunk_lr_section(
    graph: MetroGraph,
    section: Section | None,
    junction_ids: set[str],
) -> bool:
    """An LR/RL section laid out as one horizontal trunk (no parallel tracks)."""
    return (
        section is not None
        and section.direction in ("LR", "RL")
        and len(_section_distinct_trunk_ys(graph, section, junction_ids)) == 1
    )


def _off_track_lift_step(
    graph: MetroGraph,
    section: Section | None,
    junction_ids: set[str],
    y_spacing: float,
) -> float:
    """Per-section vertical step for lifting off-track stations.

    A section that is a single horizontal trunk has no parallel tracks, so the
    diagonal-label band that widened the graph-wide ``y_spacing`` is wasted
    vertical room here: it would strand the off-track icon far above the trunk.
    Such a section lifts by the base content pitch (``graph._base_y_spacing``)
    instead.

    Multi-track sections, and any section with no recorded base pitch, keep the
    passed-in ``y_spacing``.  The base only applies when strictly smaller, so an
    explicit ``y_spacing`` below the base is never widened by this path.
    """
    base = graph._base_y_spacing
    if base is None or base >= y_spacing:
        return y_spacing
    if not _is_single_trunk_lr_section(graph, section, junction_ids):
        return y_spacing
    return base


def _place_off_track_relative_to_anchors(
    graph: MetroGraph,
    y_spacing: float,
    section_id: str,
    fallback_consumer_id: str,
    by_consumer: dict[str, list[Station]],
    below: set[str] | None = None,
) -> tuple[float | None, float | None]:
    """Place each off-track station ``n*y_spacing`` from its anchor.

    Stations not in ``below`` lift *above* their anchor (smaller Y);
    those in ``below`` -- producer-fed outputs on a downward branch --
    drop *below* it (larger Y) so they run straight down instead of
    crossing back over the trunk.

    Multiple stations sharing an anchor stack in ``y_spacing`` steps away
    from it.  When the natural slot would put the icon on top of another
    trunk station's line band in the same column (e.g. ``net_in`` at the
    gsea-trunk Y when decoupler sits one slot below gsea at non-savepoint
    params), the slot is bumped further from the anchor by additional
    ``y_spacing`` steps until the icon's vertical bbox clears every
    line-bearing track in its column and every sibling off-track already
    placed in the same column.

    Returns ``(highest_y, lowest_y)`` -- the topmost above-anchor Y and
    the bottommost below-anchor Y -- with ``None`` for a direction that
    placed no stations.
    """
    section = graph.sections.get(section_id)
    sec_dir = section.direction if section is not None else "LR"
    junction_ids = graph.junction_ids
    below = below or set()

    step = _off_track_lift_step(graph, section, junction_ids, y_spacing)

    # Track already-placed off-track Ys per column so a bumped icon
    # doesn't crash into a sibling off-track already at the desired Y.
    used_ys_per_col: dict[float, list[float]] = defaultdict(list)

    # Iterate consumers bottom-up (largest consumer Y first).  The
    # bumping mechanism only pushes upward, so placing the bottommost
    # consumer's icon first lets subsequent (higher-consumer) icons
    # stack above it.  The resulting visual order matches the consumer
    # Y order: an upper consumer gets an upper icon, a lower consumer
    # gets a lower icon, regardless of edge declaration order in the mmd.
    def _consumer_anchor_y(item: tuple[str, list[Station]]) -> float:
        cid = item[0] if item[0] else fallback_consumer_id
        a = graph.stations.get(cid)
        return a.y if a is not None else 0.0

    ordered_consumers = sorted(
        by_consumer.items(), key=_consumer_anchor_y, reverse=True
    )

    highest_y: float | None = None
    lowest_y: float | None = None
    for consumer_id, stations in ordered_consumers:
        anchor_id = consumer_id if consumer_id else fallback_consumer_id
        anchor = graph.stations.get(anchor_id)
        if anchor is None:
            continue
        consumer_y = anchor.y
        # Preserve original Y order: station closest to the trunk stays
        # innermost in the stack.
        stations.sort(key=lambda s: s.y)
        up_stations = [s for s in stations if s.id not in below]
        down_stations = [s for s in stations if s.id in below]

        n_up = len(up_stations)
        for i, st in enumerate(up_stations):
            base_step = n_up - i
            candidate_y = consumer_y - base_step * step
            if section is not None and sec_dir in ("LR", "RL"):
                candidate_y = _bump_off_track_clear_of_trunks(
                    graph,
                    st,
                    candidate_y,
                    step,
                    section,
                    junction_ids,
                    sibling_ys=used_ys_per_col[round(st.x, 1)],
                    direction=-1,
                )
            st.y = candidate_y
            used_ys_per_col[round(st.x, 1)].append(st.y)
            if highest_y is None or st.y < highest_y:
                highest_y = st.y

        for i, st in enumerate(down_stations):
            base_step = i + 1
            candidate_y = consumer_y + base_step * step
            if section is not None and sec_dir in ("LR", "RL"):
                candidate_y = _bump_off_track_clear_of_trunks(
                    graph,
                    st,
                    candidate_y,
                    step,
                    section,
                    junction_ids,
                    sibling_ys=used_ys_per_col[round(st.x, 1)],
                    direction=1,
                )
            st.y = candidate_y
            used_ys_per_col[round(st.x, 1)].append(st.y)
            if lowest_y is None or st.y > lowest_y:
                lowest_y = st.y
    return highest_y, lowest_y


def _bump_off_track_clear_of_trunks(
    graph: MetroGraph,
    off_st: Station,
    candidate_y: float,
    step: float,
    section: Section,
    junction_ids: set[str],
    sibling_ys: list[float] | None = None,
    direction: int = -1,
) -> float:
    """Return ``candidate_y`` shifted so the off-track icon clears any
    trunk line track passing through the icon's X column.

    The renderer places an off-track icon at the station's Y with file-
    icon half-height ~16 px; a trunk station's line tracks run at
    ``trunk.y + offset(line)`` for each line on the trunk.  When a
    trunk station downstream of the icon (LR: higher X; RL: lower X)
    has tracks at Y values inside ``[candidate_y - icon_half,
    candidate_y + icon_half]``, the segment from the section's entry
    port to that trunk crosses the icon.  Bump away from the anchor
    (``direction`` -1 = up, +1 = down) by ``step`` increments until the
    band clears.

    ``sibling_ys`` is a list of Ys already taken by other off-track
    inputs in the same column - the bump must also clear those (within
    one ``step`` slot) so two icons don't end up in the same row.

    Capped at six steps to avoid runaway lifts.
    """
    if step <= 0:
        return candidate_y

    # Match the renderer's terminus icon height and add a small margin
    # so the icon's stroke doesn't touch a track.
    MARGIN = 2.0
    # Limit lift attempts so a pathological column doesn't pull the
    # icon off-canvas.
    MAX_STEPS = 6

    # Find trunk stations in the same section whose row-bundle crosses
    # the icon's X column.
    trunk_offsets_at_x: list[float] = []
    for sid in section.station_ids:
        st2 = graph.stations.get(sid)
        if st2 is None or st2.is_port or st2.is_hidden:
            continue
        if st2.id == off_st.id or sid in junction_ids:
            continue
        if st2.off_track or st2.is_terminus:
            continue
        # Only stations on the OTHER side of the icon (i.e. the trunk
        # the entry port feeds) have tracks crossing the icon's column.
        if section.direction == "LR" and st2.x <= off_st.x + 0.5:
            continue
        if section.direction == "RL" and st2.x >= off_st.x - 0.5:
            continue
        # Collect the line-track band Y range at the icon's column.
        # Tracks run horizontally so each line's Y here equals
        # st2.y + offset(line); offsets aren't computed at this phase
        # but they're bounded by ``(n_lines - 1) * OFFSET_STEP`` total
        # spread (centred on st2.y).  Use line-track extents only - no
        # marker radius - because the icon is at a different X from st2
        # so st2's pill doesn't intersect the icon's column.
        lines = graph.station_lines(sid)
        n_lines = len(lines)
        if n_lines == 0:
            continue
        half_span = (n_lines - 1) * OFFSET_STEP / 2
        trunk_offsets_at_x.append(st2.y - half_span)
        trunk_offsets_at_x.append(st2.y + half_span)

    sib_ys = list(sibling_ys or [])

    if not trunk_offsets_at_x and not sib_ys:
        return candidate_y

    def _overlaps(y: float) -> bool:
        top = y - ICON_HALF_HEIGHT - MARGIN
        bot = y + ICON_HALF_HEIGHT + MARGIN
        for tl_y_lo, tl_y_hi in zip(trunk_offsets_at_x[::2], trunk_offsets_at_x[1::2]):
            if not (bot < tl_y_lo or tl_y_hi < top):
                return True
        # Sibling clearance: keep at least 2 * ICON_HALF_HEIGHT + MARGIN between
        # icon centres in the same column so the icon bboxes don't
        # touch.
        for sy in sib_ys:
            if abs(sy - y) < 2 * ICON_HALF_HEIGHT + MARGIN:
                return True
        return False

    y = candidate_y
    steps = 0
    while _overlaps(y) and steps < MAX_STEPS:
        y += direction * step
        steps += 1
    return y


def _lift_off_track_stations(
    graph: MetroGraph,
    y_spacing: float,
    section_y_padding: float,
) -> None:
    """Lift off_track stations to the row above their consumer station.

    Off-track stations are file-input nodes that should not consume a
    line-track Y slot.  Each marked station is placed one ``y_spacing``
    row above its consumer (the on-track station it feeds), so the
    input sits adjacent to where its data is read rather than at a
    uniform top-of-section band.  When several off-track inputs feed
    the same consumer, they stack upward in ``y_spacing`` steps.

    If an off-track station has no on-track consumer in the same
    section, it falls back to the section's topmost on-track station
    as its anchor.  After placement, the section bbox grows upward to
    fit the highest lifted input, and same-section TOP ports are
    nudged back to the new top edge.

    Caller is responsible for invoking ``_shift_graph_into_canvas``
    afterwards: the upward bbox growth here can push the topmost
    section above the canvas top margin set by Stage 1.5.
    """
    groups = _off_track_groups(graph)
    if not groups:
        return

    below = _off_track_output_below(graph)
    for sec_id, (fallback_id, by_consumer) in groups.items():
        section = graph.sections.get(sec_id)
        if section is None:
            continue
        highest_y, lowest_y = _place_off_track_relative_to_anchors(
            graph, y_spacing, sec_id, fallback_id, by_consumer, below
        )
        if highest_y is not None:
            new_bbox_top = highest_y - section_y_padding
            if new_bbox_top < section.bbox_y:
                _grow_section_bbox_upward(graph, section, new_bbox_top)
        if lowest_y is not None:
            _grow_section_bbox_downward(graph, section, lowest_y + section_y_padding)


def _off_track_fit_top(
    graph: MetroGraph,
    section: Section,
    highest_off_track_y: float,
    section_y_padding: float,
) -> float:
    """Bbox top that gives the off-track band one full padding band.

    Returns ``highest_off_track_y - section_y_padding`` clamped so the
    refit never clips other content sitting above the band, nor strands
    a non-TOP port above the new top (TOP ports follow the edge, so they
    impose no bound).  Used by the reversible off-track reanchor: unlike
    the grow-only :func:`_grow_section_bbox_upward`, the caller applies
    this in both directions so a stale too-tall box is reclaimed.
    """
    target = highest_off_track_y - section_y_padding
    for y in _content_station_ys(graph, section):
        target = min(target, y - section_y_padding)
    # Non-TOP ports bound the fit so they aren't stranded above the new
    # top; TOP ports follow the edge and impose no bound.  This port clamp
    # is deliberately narrower than the bbox helpers' all-port clamp, so
    # only the content set is shared, not the port handling.
    for pid in section.entry_ports + section.exit_ports:
        port = graph.ports.get(pid)
        port_st = graph.stations.get(pid)
        if not port or not port_st or port.side == PortSide.TOP:
            continue
        target = min(target, port_st.y)
    return target


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
        highest_y, lowest_y = _place_off_track_relative_to_anchors(
            graph, y_spacing, sec_id, fallback_id, by_consumer, below
        )
        section = graph.sections.get(sec_id)
        if section is None:
            continue
        if highest_y is not None:
            desired_top = _off_track_fit_top(
                graph, section, highest_y, section_y_padding
            )
            if abs(desired_top - section.bbox_y) > 0.5:
                _set_section_bbox_top(graph, section, desired_top)
        if lowest_y is not None:
            _grow_section_bbox_downward(graph, section, lowest_y + section_y_padding)
