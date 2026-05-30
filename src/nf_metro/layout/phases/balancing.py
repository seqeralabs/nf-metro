"""Centre section content around the trunk; balance feeders and loop stations."""

from __future__ import annotations

from collections import defaultdict

from nf_metro.layout.constants import (
    DIAGONAL_RUN,
    ICON_HALF_HEIGHT,
    SECTION_Y_PADDING,
    STATION_RADIUS_APPROX,
)
from nf_metro.layout.phases._common import _section_bundle_lines
from nf_metro.layout.phases.bbox import (
    _lift_would_cause_uturn,
    _loop_corner_x,
    _push_lower_rows_after_bbox_grow,
)
from nf_metro.layout.phases.ports import _set_port_y
from nf_metro.parser.model import MetroGraph, PortSide, Section, Station


def _snap_inter_section_port_pairs(graph: MetroGraph) -> None:
    """Snap exit/entry port pairs in the same row to a shared Y.

    For each LEFT/RIGHT exit port that connects (directly or via a
    junction) to a same-row LEFT/RIGHT entry port, picks the entry's Y
    as the shared anchor.  This eliminates the small Y kinks left by
    sections whose trunk Y couldn't be aligned via Stage 4.8 (e.g.
    row-spanning sections that the trunk aligner skips); the inside-
    section link from the internal source to the port may bend by a
    pixel or two but the inter-section bundle stays perfectly
    horizontal.

    Fan-in exits (two or more distinct internal source Ys) are skipped
    so the visual convergence into a single port stays meaningful.

    Returns True when at least one port was moved (caller may need to
    re-run junction positioning to pick up the new exit-port Y).

    Scoped to pipelines that use explicit ``%%metro grid:`` directives;
    auto-layout pipelines keep their existing inter-section routing so
    line-offset ordering tests stay stable.  The fan-in entry-port snap
    branch below runs unconditionally because it only moves the entry
    port (not the exit), preserving the auto-layout fan-in convergence.
    """
    explicit_grid = bool(graph._explicit_grid)
    junction_ids = graph.junction_ids

    for port_id, port in graph.ports.items():
        if port.is_entry:
            continue
        if port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        section = graph.sections.get(port.section_id)
        if section is None or section.direction not in ("LR", "RL"):
            continue
        port_st = graph.stations.get(port_id)
        if port_st is None:
            continue

        # Find downstream entry port(s) in the same row.
        targets: list[float] = []
        for edge in graph.edges_from(port_id):
            entry_candidates: list[str] = []
            tgt_port = graph.ports.get(edge.target)
            if tgt_port and tgt_port.is_entry:
                entry_candidates.append(edge.target)
            elif edge.target in junction_ids:
                for e2 in graph.edges_from(edge.target):
                    tp2 = graph.ports.get(e2.target)
                    if tp2 and tp2.is_entry:
                        entry_candidates.append(e2.target)
            for eid in entry_candidates:
                ep = graph.ports.get(eid)
                if ep is None or ep.side not in (PortSide.LEFT, PortSide.RIGHT):
                    continue
                ds_sec = graph.sections.get(ep.section_id)
                if ds_sec is None or ds_sec.grid_row != section.grid_row:
                    continue
                ep_st = graph.stations.get(eid)
                if ep_st is not None:
                    targets.append(ep_st.y)
        if not targets:
            continue
        target_y = min(targets, key=lambda y: abs(y - port_st.y))
        if abs(port_st.y - target_y) < 0.5:
            continue

        # Fan-in exits (multiple distinct internal source Ys) want to
        # keep their centred-midpoint convergence Y, so don't move the
        # exit port itself.  Instead, snap the downstream entry port
        # to the exit port's Y so the inter-section trunk stays flat.
        port_set = section.port_ids
        src_ys: set[float] = set()
        for edge in graph.edges_to(port_id):
            src = graph.stations.get(edge.source)
            if src and not src.is_port and edge.source not in port_set:
                src_ys.add(round(src.y, 1))
        if len(src_ys) >= 2:
            for edge in graph.edges_from(port_id):
                entry_candidates: list[str] = []
                tgt_port = graph.ports.get(edge.target)
                if tgt_port and tgt_port.is_entry:
                    entry_candidates.append(edge.target)
                elif edge.target in junction_ids:
                    for e2 in graph.edges_from(edge.target):
                        tp2 = graph.ports.get(e2.target)
                        if tp2 and tp2.is_entry:
                            entry_candidates.append(e2.target)
                for eid in entry_candidates:
                    ep = graph.ports.get(eid)
                    if ep is None or ep.side not in (PortSide.LEFT, PortSide.RIGHT):
                        continue
                    ds_sec = graph.sections.get(ep.section_id)
                    if ds_sec is None or ds_sec.grid_row != section.grid_row:
                        continue
                    ep_st = graph.stations.get(eid)
                    if ep_st is None or abs(ep_st.y - port_st.y) < 0.5:
                        continue
                    _set_port_y(graph, eid, port_st.y)
            continue

        if not explicit_grid:
            continue
        _set_port_y(graph, port_id, target_y)


def _fan_free_content_upward(
    graph: MetroGraph, section_y_padding: float, y_spacing: float
) -> None:
    """Fill empty top space by fanning trunk-candidate siblings up.

    When ``_compact_row_content_to_bbox_top`` is bounded by a row-mate
    section (e.g. one with off-track inputs), other sections in the
    group may keep visible empty space above their topmost station.
    For each LR/RL section with no off-track stations and at least one
    ``y_spacing`` slot of top slack, redistribute the section's
    trunk-candidate siblings (stations carrying the full bundle in the
    same entry column) into the empty top space.

    The topmost trunk candidate stays pinned (preserves the row trunk
    Y); the rest are stacked above it at ``y_spacing`` pitch.  Stations
    in later columns are not moved here so downstream chains keep their
    relative position to the trunk station they branch from.

    Scoped to pipelines using explicit ``%%metro grid:`` directives so
    the auto-layout path is unaffected.
    """
    if not graph._explicit_grid:
        return
    for section in graph.sections.values():
        if section.bbox_h <= 0 or section.direction not in ("LR", "RL"):
            continue
        if any(
            getattr(graph.stations.get(sid), "off_track", False)
            for sid in section.station_ids
        ):
            continue
        port_set = section.port_ids
        internal_ids = [
            sid
            for sid in section.station_ids
            if sid not in port_set
            and sid in graph.stations
            and not graph.stations[sid].is_port
        ]
        if not internal_ids:
            continue
        ys = [graph.stations[sid].y for sid in internal_ids]
        top_y = min(ys)
        slack = top_y - section.bbox_y - section_y_padding
        if slack < y_spacing - 0.5:
            continue
        slots = int(slack // y_spacing)
        if slots <= 0:
            continue

        bundle = _section_bundle_lines(graph, section)
        if not bundle:
            continue

        # Trunk candidates: stations in the entry column carrying the
        # full bundle.  Sort by current Y; topmost stays pinned, others
        # stack above at y_spacing pitch.
        xs = sorted({round(graph.stations[sid].x, 3) for sid in internal_ids})
        if not xs:
            continue
        entry_x = xs[0] if section.direction == "LR" else xs[-1]
        trunk_candidates = [
            sid
            for sid in internal_ids
            if round(graph.stations[sid].x, 3) == entry_x
            and set(graph.station_lines(sid)) == bundle
        ]
        if len(trunk_candidates) < 2:
            continue
        # Skip if every station in the entry column carries the full
        # bundle (no unique trunk): ``_redistribute_full_bundle_columns``
        # has already symmetrically fanned them around the section's
        # port Y and we must not collapse that into a one-sided stack.
        entry_col_all = [
            sid for sid in internal_ids if round(graph.stations[sid].x, 3) == entry_x
        ]
        if len(trunk_candidates) == len(entry_col_all) and graph.center_ports:
            continue
        trunk_candidates.sort(key=lambda s: graph.stations[s].y)
        pinned = trunk_candidates[0]
        anchor_y = graph.stations[pinned].y
        to_lift = [
            sid
            for sid in trunk_candidates[1 : 1 + slots]
            if not _lift_would_cause_uturn(graph, sid, section.id, anchor_y)
        ]
        for i, sid in enumerate(to_lift, 1):
            graph.stations[sid].y = anchor_y - i * y_spacing


def _shift_linear_consumer_chain(
    graph: MetroGraph,
    src: str,
    delta: float,
    internal_ids: set[str],
    *,
    cycle_guard: bool = False,
) -> None:
    """Walk a strictly-linear consumer chain and shift each station Y by *delta*.

    Each step requires the current station to have a single outbound edge,
    the next link to live inside *internal_ids* with a single inbound edge,
    and an unchanged line-set across the hop.  Stops at the first hop that
    fails any of those conditions.  Pass ``cycle_guard=True`` to additionally
    bail out when the walk revisits a station.
    """
    cur = src
    src_lines = set(graph.station_lines(src))
    visited: set[str] = {src} if cycle_guard else set()
    while True:
        outs = {e.target for e in graph.edges_from(cur)}
        if len(outs) != 1:
            return
        nxt = next(iter(outs))
        if nxt not in internal_ids or (cycle_guard and nxt in visited):
            return
        if len({e.source for e in graph.edges_to(nxt)}) != 1:
            return
        if set(graph.station_lines(nxt)) != src_lines:
            return
        graph.stations[nxt].y += delta
        if cycle_guard:
            visited.add(nxt)
        cur = nxt


def _fan_source_inputs_upward(graph: MetroGraph, y_spacing: float) -> None:
    """Fill empty top space by lifting source-input chains above the trunk.

    Companion to ``_fan_free_content_upward`` for sections whose entry
    column contains a single full-bundle trunk station plus subset-bundle
    sources (file inputs with no inbound edges).  The trunk-candidate
    path skips these (it requires >=2 full-bundle candidates), leaving
    every source stacked at or below the trunk and the bbox top empty.

    For each qualifying LR/RL grid section, sort sources by current Y
    (closest to trunk first) and lift up to ``slack // y_spacing`` of
    them above the trunk at ``y_spacing`` pitch.  Each lifted source
    drags its linear consumer chain (one inbound edge, identical line
    set, strictly inside the section) so per-line tracks stay straight.

    Scoped to explicit ``%%metro grid:`` pipelines so the auto-layout
    path is unaffected.  U-turn risk is nil because sources have no
    upstream feeders by definition.
    """
    if not graph._explicit_grid:
        return

    for section in graph.sections.values():
        if section.bbox_h <= 0 or section.direction not in ("LR", "RL"):
            continue
        if any(
            getattr(graph.stations.get(sid), "off_track", False)
            for sid in section.station_ids
        ):
            continue
        port_set = section.port_ids
        internal_ids = [
            sid
            for sid in section.station_ids
            if sid not in port_set
            and sid in graph.stations
            and not graph.stations[sid].is_port
        ]
        if not internal_ids:
            continue
        bundle = _section_bundle_lines(graph, section)
        if not bundle:
            continue

        xs = sorted({round(graph.stations[sid].x, 3) for sid in internal_ids})
        entry_x = xs[0] if section.direction == "LR" else xs[-1]
        entry_col = [
            sid for sid in internal_ids if round(graph.stations[sid].x, 3) == entry_x
        ]
        trunks = [s for s in entry_col if set(graph.station_lines(s)) == bundle]
        if len(trunks) != 1:
            continue
        trunk_sid = trunks[0]
        trunk_y = graph.stations[trunk_sid].y

        sources = [
            s
            for s in entry_col
            if s != trunk_sid
            and graph.station_lines(s)
            and set(graph.station_lines(s)) < bundle
            and not graph.edges_to(s)
            and graph.stations[s].y > trunk_y - 0.5
        ]
        if len(sources) < 2:
            continue

        # Reserve icon_half when any source renders as a file icon so the
        # icon's vertical extent stays inside the bbox.
        any_terminus = any(graph.stations[s].is_terminus for s in sources)
        top_margin = y_spacing / 4 + (ICON_HALF_HEIGHT if any_terminus else 0.0)
        slack = trunk_y - section.bbox_y - top_margin
        slots = int((slack + 0.5) // y_spacing)
        if slots < 1:
            continue
        # Keep at least half the sources at or below the trunk so the
        # section stays bottom-weighted when only a couple fit above.
        n_lift = min(slots, len(sources) // 2)
        if n_lift == 0:
            continue

        sources.sort(key=lambda s: graph.stations[s].y)
        internal_set = set(internal_ids)

        # Drag each strictly-linear consumer chain so per-line tracks stay
        # straight from icon to trunk junction.
        for i, src in enumerate(sources[:n_lift], 1):
            new_y = trunk_y - i * y_spacing
            delta = new_y - graph.stations[src].y
            if abs(delta) < 0.5:
                continue
            graph.stations[src].y = new_y
            _shift_linear_consumer_chain(graph, src, delta, internal_set)

        # Compact the remaining below-trunk sources upward to fill the
        # rows their predecessors vacated.  Without this step, lifted
        # sources leave a multi-slot gap between the trunk and the
        # first below-trunk source (e.g. trunk -> empty -> empty -> Affy
        # row when GTF and Matrix were lifted).  Place the i-th
        # below-trunk source at ``trunk_y + i * y_spacing``.
        for i, src in enumerate(sources[n_lift:], 1):
            new_y = trunk_y + i * y_spacing
            delta = new_y - graph.stations[src].y
            if abs(delta) < 0.5:
                continue
            graph.stations[src].y = new_y
            _shift_linear_consumer_chain(graph, src, delta, internal_set)


def _balance_direct_external_feeder_ys(
    graph: MetroGraph, station_id: str, section_id: str
) -> list[float]:
    """Return Ys of the candidate station's per-line external feeders.

    Walks edges INTO ``station_id`` by line: for each inbound (src, lid)
    pair, traverse the (src, lid) chain through junctions and ports
    until reaching the first non-port, non-junction station.  Filtering
    by line means transit-only stations feeding the same shared port
    on a different line are not counted.
    """
    junction_ids = graph.junction_ids
    feeder_ys: list[float] = []
    seen: set[tuple[str, str]] = set()
    stack: list[tuple[str, str]] = [
        (edge.source, edge.line_id) for edge in graph.edges_to(station_id)
    ]
    while stack:
        cur_id, lid = stack.pop()
        if (cur_id, lid) in seen:
            continue
        seen.add((cur_id, lid))
        if cur_id in junction_ids:
            for edge in graph.edges_to(cur_id):
                if edge.line_id == lid:
                    stack.append((edge.source, lid))
            continue
        src = graph.stations.get(cur_id)
        if src is None:
            continue
        if src.is_port:
            for edge in graph.edges_to(cur_id):
                if edge.line_id == lid:
                    stack.append((edge.source, lid))
            continue
        if src.section_id == section_id:
            continue
        feeder_ys.append(src.y)
    return feeder_ys


def _balance_section_content_around_trunk(
    graph: MetroGraph, section_y_padding: float, y_spacing: float
) -> None:
    """Rebalance fan-out siblings to fill empty bands above the trunk.

    Runs after fan-upward and re-centering finalises the trunk Y.
    For sections whose final layout still leaves a >= 1 * ``y_spacing``
    empty band above the topmost station while more siblings sit below
    the trunk than above, either:

    - LIFTS the bottommost (or topmost, depending on line homogeneity)
      below-trunk movable sibling to one slot above the topmost station
      when the bbox has room for the marker plus its above-marker
      label.  The lifted station's linear consumer chain follows so
      per-line tracks stay straight.

    - SWAPS the bottommost below-trunk movable with the topmost above-
      trunk station when the bbox has no headroom for an extra slot.

    Scoped to explicit ``%%metro grid:`` pipelines.  Line-aware U-turn
    safety prevents lifts that would force the line to climb past the
    trunk and double back.
    """
    if not graph._explicit_grid:
        return
    if not graph.center_ports:
        return

    for section in graph.sections.values():
        if section.bbox_h <= 0 or section.direction not in ("LR", "RL"):
            continue
        bundle = _section_bundle_lines(graph, section)
        if not bundle:
            continue
        port_set = section.port_ids
        internal_ids = [
            sid
            for sid in section.station_ids
            if sid not in port_set
            and sid in graph.stations
            and not graph.stations[sid].is_port
            and not graph.stations[sid].is_hidden
            and not graph.stations[sid].off_track
        ]
        if not internal_ids:
            continue

        trunk_y: float | None = None
        for pid in section.entry_ports + section.exit_ports:
            port = graph.ports.get(pid)
            st = graph.stations.get(pid)
            if port and st and port.side in (PortSide.LEFT, PortSide.RIGHT):
                trunk_y = st.y
                break
        if trunk_y is None:
            full_ys = sorted(
                graph.stations[s].y
                for s in internal_ids
                if set(graph.station_lines(s)) == bundle
            )
            if not full_ys:
                continue
            trunk_y = full_ys[len(full_ys) // 2]

        cols: dict[float, list[str]] = defaultdict(list)
        for sid in internal_ids:
            cols[round(graph.stations[sid].x, 3)].append(sid)

        section_top_y = min(graph.stations[s].y for s in internal_ids)
        top_band = section_top_y - section.bbox_y
        if top_band <= y_spacing + 0.5:
            continue

        movable: list[str] = []
        for x, sids in cols.items():
            trunks_in_col = [s for s in sids if set(graph.station_lines(s)) == bundle]
            if not trunks_in_col:
                continue
            for s in sids:
                if s in trunks_in_col:
                    continue
                lines = set(graph.station_lines(s))
                if not lines or not (lines < bundle):
                    continue
                movable.append(s)

        if not movable:
            continue

        ys = [graph.stations[s].y for s in movable]
        above_count = sum(1 for y in ys if y < trunk_y - 0.5)
        below_count = sum(1 for y in ys if y > trunk_y + 0.5)
        if below_count <= above_count:
            continue

        section_internal_set = set(internal_ids)

        # Ys of every other station (incl. off-track icons) at ``col_x``.
        # Lift candidates must avoid these slots or the lifted marker
        # overlaps an existing marker / icon at render time.
        def _column_occupied_ys(col_x: float, skip_sid: str) -> list[float]:
            occ: list[float] = []
            for sid2 in section.station_ids:
                if sid2 == skip_sid:
                    continue
                st2 = graph.stations.get(sid2)
                if st2 is None or st2.is_port or st2.is_hidden:
                    continue
                if abs(st2.x - col_x) > 0.5:
                    continue
                occ.append(st2.y)
            return occ

        max_iters = len(movable)
        for _ in range(max_iters):
            section_top_y = min(graph.stations[s].y for s in internal_ids)
            top_band = section_top_y - section.bbox_y
            if top_band <= y_spacing + 0.5:
                break
            ys = {s: graph.stations[s].y for s in movable}
            above = [s for s, y in ys.items() if y < trunk_y - 0.5]
            below = [s for s, y in ys.items() if y > trunk_y + 0.5]
            if len(below) <= len(above):
                break
            line_sets = {frozenset(graph.station_lines(s)) for s in movable}
            if len(line_sets) == 1:
                below.sort(key=lambda s: graph.stations[s].y, reverse=True)
            else:
                below.sort(key=lambda s: graph.stations[s].y)
            # First below-trunk candidate whose lift Y doesn't collide
            # with another station/icon already occupying the same column.
            candidate = None
            new_y = section_top_y - y_spacing
            for cand in below:
                col_x = graph.stations[cand].x
                occ = _column_occupied_ys(col_x, cand)
                if any(abs(oy - new_y) < y_spacing - 0.5 for oy in occ):
                    continue
                candidate = cand
                break
            if candidate is None:
                break
            st = graph.stations.get(candidate)
            has_above_label = bool(st and st.label and st.label.strip())
            label_clearance = y_spacing / 2 if has_above_label else 0.0
            # Off-track file icons reach ~16 px above centre; on-track
            # markers reach ~9.5 px.  Use the wider reach when relevant.
            marker_clearance = 16.0 if (st and st.off_track) else 9.5
            min_y = section.bbox_y + max(label_clearance, marker_clearance)
            if new_y < min_y - 0.5:
                if not above:
                    break
                above.sort(key=lambda s: graph.stations[s].y)
                top_above = above[0]
                ya = graph.stations[top_above].y
                yc = graph.stations[candidate].y
                if candidate != below[0]:
                    break
                graph.stations[candidate].y = ya
                graph.stations[top_above].y = yc
                _shift_linear_consumer_chain(
                    graph, candidate, ya - yc, section_internal_set
                )
                _shift_linear_consumer_chain(
                    graph, top_above, yc - ya, section_internal_set
                )
                break
            ext_feeders = _balance_direct_external_feeder_ys(
                graph, candidate, section.id
            )
            if len(ext_feeders) >= 2 and all(fy >= new_y - 0.5 for fy in ext_feeders):
                break
            delta = new_y - graph.stations[candidate].y
            graph.stations[candidate].y = new_y
            _shift_linear_consumer_chain(graph, candidate, delta, section_internal_set)

        # Below-trunk compaction: when the first row below the trunk is
        # empty but content sits two or more slots below, lift all
        # below-trunk stations up by one ``y_spacing`` so they pack
        # against the trunk row.  Honours the existing column-occupied
        # guard so off-track icons and marker clearance aren't violated.
        _compact_below_trunk_band(graph, section, trunk_y, y_spacing)


def _compact_below_trunk_band(
    graph: MetroGraph,
    section: Section,
    trunk_y: float,
    y_spacing: float,
) -> None:
    """Shift the entire below-trunk stack up by one y_spacing slot.

    Fires when the first below-trunk slot (``trunk_y + y_spacing``) is
    empty for non-port, non-hidden content while there is at least one
    station two or more slots below the trunk.  All non-port, non-hidden
    stations strictly below the trunk are shifted up by ``y_spacing``,
    including off-track inputs and their consumers.  The bbox is left
    alone: bottom shrink in :func:`_shrink_bboxes_to_content_bottom`
    will collapse the freed bottom space afterward.

    Symmetric counterpart to the above-trunk auto-balance: lift the
    bottom stack against the trunk when there's wasted space, instead
    of leaving an empty row directly below the trunk.
    """
    if y_spacing <= 0:
        return
    movables: list[str] = []
    for sid in section.station_ids:
        st = graph.stations.get(sid)
        if st is None or st.is_port or st.is_hidden:
            continue
        if st.y > trunk_y + 0.5:
            movables.append(sid)
    if not movables:
        return
    # Is there an empty row right below trunk?  Define "empty" as: no
    # station at trunk_y + y_spacing within half a slot.
    first_row_y = trunk_y + y_spacing
    has_first_row = any(
        abs(graph.stations[s].y - first_row_y) < y_spacing / 2 - 0.5 for s in movables
    )
    if has_first_row:
        return
    # Confirm there's content further below (otherwise nothing to lift).
    deeper = [s for s in movables if graph.stations[s].y >= trunk_y + 1.5 * y_spacing]
    if not deeper:
        return
    # Collision check: ensure shifting up by y_spacing doesn't collide
    # with any non-moving station in the same column.  Build a set of
    # non-moving Ys per column.
    cols_nonmovable: dict[float, set[float]] = defaultdict(set)
    movable_set = set(movables)
    for sid in section.station_ids:
        if sid in movable_set:
            continue
        st = graph.stations.get(sid)
        if st is None or st.is_hidden:
            continue
        cols_nonmovable[round(st.x, 3)].add(round(st.y, 3))
    for sid in movables:
        st = graph.stations[sid]
        new_y = st.y - y_spacing
        col_x = round(st.x, 3)
        if any(
            abs(oy - new_y) < y_spacing / 2 - 0.5
            for oy in cols_nonmovable.get(col_x, set())
        ):
            return  # collision; abort
    # Apply uniform shift to every below-trunk movable.
    for sid in movables:
        graph.stations[sid].y -= y_spacing


def _recenter_loop_side_stations(graph: MetroGraph) -> None:
    """Reposition fan-out side stations on the centre of their loop run.

    A "loop side station" is an off-trunk station fed by exactly one
    on-trunk predecessor and feeding exactly one on-trunk successor,
    forming a diamond loop with the trunk.  ``propd``, ``dream`` and
    ``DESeq2`` in the differential section are the canonical example:
    each takes the trunk bundle into limma's column, runs horizontally
    off the trunk for one slot, then rejoins limma's outgoing trunk at
    ``annotate``.

    Layer-based X placement puts these stations at ``layer * x_spacing``
    relative to the section's entry, ignoring asymmetry in the routing
    diagonals.  When the source-side diagonal is shorter than the
    target-side diagonal (e.g. wide join-station labels widen the
    target-side gap), the side station appears visibly biased toward
    the source.  Recompute its X as the midpoint of the loop's two
    diagonal corner Xs so it sits centred on the horizontal run.

    Honours the same constraints as the routing pass:
    - ``MIN_STRAIGHT_PORT`` / ``MIN_STRAIGHT_EDGE`` at endpoints.
    - Source label clearance at the fork station.
    - Target label clearance at the join station.
    - ``DIAGONAL_RUN`` length for the 45-degree transition.

    A second pass aligns trunk-Y stations that share the same
    ``(predecessor, successor)`` column with off-trunk siblings (e.g.
    ``limma`` shares its column with ``DESeq2``, ``dream`` and
    ``propd``) so column-mates land at the same X regardless of
    whether they sit on or off the trunk row.

    No-op for any station that doesn't form a clean two-edge loop, and
    skipped when shifting would leave fewer than ``DIAGONAL_RUN`` worth
    of horizontal room on either side.
    """
    # Single pass over graph.edges to accumulate fork/join sets.
    # (Adjacency itself is served by graph.edges_from / edges_to.)
    fork_targets: dict[str, set[str]] = defaultdict(set)
    join_sources: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges:
        fork_targets[e.source].add(e.target)
        join_sources[e.target].add(e.source)
    fork_stations = {sid for sid, t in fork_targets.items() if len(t) > 1}
    join_stations = {sid for sid, s in join_sources.items() if len(s) > 1}

    # Minimum recenter delta below which we'd be moving the station
    # without enough visual benefit to justify breaking any incidental
    # column alignment with stacked siblings.  Anything smaller is in
    # the noise where the layer-X placement already reads as centred.
    min_recenter_delta = DIAGONAL_RUN / 3.0

    # Pass 1: re-centre off-trunk loop side stations using the diagonal
    # corner geometry.
    for section in graph.sections.values():
        if section.bbox_h <= 0 or section.direction not in ("LR", "RL"):
            continue
        port_ids = section.port_ids
        # Each side station: not a port, not hidden, has exactly one
        # incoming edge and one outgoing edge, both endpoints sit on
        # the section trunk Y (single source / single target on-trunk).
        for sid in section.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.is_hidden:
                continue
            ins = graph.edges_to(sid)
            outs = graph.edges_from(sid)
            if len(ins) != 1 or len(outs) != 1:
                continue
            src_id = ins[0].source
            tgt_id = outs[0].target
            src = graph.stations.get(src_id)
            tgt = graph.stations.get(tgt_id)
            if src is None or tgt is None:
                continue
            # Side station must sit OFF the trunk Y of its source and
            # target (a vertical hop is needed at both endpoints).
            if abs(src.y - tgt.y) > 0.5:
                # Source and target aren't on the same trunk row, so
                # this isn't a simple horizontal loop side station.
                continue
            trunk_y = src.y
            if abs(st.y - trunk_y) < 0.5:
                continue  # Already on trunk, no loop.
            # Both endpoints must lie strictly to opposite sides of the
            # side station (a real horizontal loop, not a U-turn).
            if not ((src.x < st.x < tgt.x) or (tgt.x < st.x < src.x)):
                continue
            # Require at least one OFF-TRUNK sibling sharing the same
            # single src and tgt: this is what makes the station part
            # of a genuine parallel fan-out where the column is owned
            # by the loop, not by an unrelated trunk station that just
            # happens to share the layer-X.  Single side branches (e.g.
            # ``search`` paired with the trunk continuation ``align``)
            # carry no fan, and recentering them off the column where
            # the trunk station sits visibly breaks the layout.
            has_off_trunk_sibling = False
            for other_sid in section.station_ids:
                if other_sid == sid:
                    continue
                other = graph.stations.get(other_sid)
                if other is None or other.is_port or other.is_hidden:
                    continue
                if abs(other.y - trunk_y) < 0.5:
                    continue  # on-trunk co-loopers don't establish a fan
                other_ins = graph.edges_to(other_sid)
                other_outs = graph.edges_from(other_sid)
                other_srcs = {e.source for e in other_ins}
                other_tgts = {e.target for e in other_outs}
                if other_srcs == {src_id} and other_tgts == {tgt_id}:
                    has_off_trunk_sibling = True
                    break
            if not has_off_trunk_sibling:
                continue
            # Compute the two diagonal corner Xs using routing's
            # placement rule (see _compute_diagonal_placement).
            corner_left = _loop_corner_x(
                src, st, fork_stations, join_stations, role="src"
            )
            corner_right = _loop_corner_x(
                st, tgt, fork_stations, join_stations, role="tgt"
            )
            if corner_left is None or corner_right is None:
                continue
            # Ensure room on both sides for the horizontal run after
            # the new midpoint.  Skip if the move would push the
            # station past either corner.
            midpoint = (corner_left + corner_right) / 2.0
            if not (
                min(corner_left, corner_right)
                <= midpoint
                <= max(corner_left, corner_right)
            ):
                continue
            # Skip moves smaller than the minimum visual-benefit
            # threshold: they trade an imperceptible re-centre for a
            # visible column break against on-trunk co-loopers (e.g.
            # rnaseq_lite ``star_align`` ↔ ``hisat_align``).
            if abs(midpoint - st.x) < min_recenter_delta:
                continue
            st.x = midpoint

    # Pass 2: snap loop-column-mate stations (trunk-row station and any
    # off-trunk siblings whose multi-edge topology disqualified them
    # from pass 1) to the column X defined by pass-1's clean siblings.
    for section in graph.sections.values():
        if section.bbox_h <= 0 or section.direction not in ("LR", "RL"):
            continue
        port_ids = section.port_ids
        # Determine the section's trunk Y from a horizontal port.
        trunk_y: float | None = None
        for pid in section.entry_ports + section.exit_ports:
            ps = graph.stations.get(pid)
            port = graph.ports.get(pid)
            if (
                ps is not None
                and port is not None
                and port.side in (PortSide.LEFT, PortSide.RIGHT)
            ):
                trunk_y = ps.y
                break
        if trunk_y is None:
            continue

        # Visible trunk-Y predecessor/successor X-extent for a station.
        # Returns ``None`` when the station has no trunk-Y neighbour on
        # one side, or when any visible neighbour sits off the trunk
        # row (off-track inputs anchor the station elsewhere).
        def _column_key(sid: str) -> tuple[float, float] | None:
            pred_x: float | None = None
            succ_x: float | None = None
            for e in graph.edges_to(sid):
                p = graph.stations.get(e.source)
                if p is None or p.is_hidden:
                    continue
                if abs(p.y - trunk_y) > 0.5:
                    return None
                if (
                    pred_x is None
                    or (section.direction == "LR" and p.x > pred_x)
                    or (section.direction == "RL" and p.x < pred_x)
                ):
                    pred_x = p.x
            for e in graph.edges_from(sid):
                t = graph.stations.get(e.target)
                if t is None or t.is_hidden:
                    continue
                if abs(t.y - trunk_y) > 0.5:
                    return None
                if (
                    succ_x is None
                    or (section.direction == "LR" and t.x < succ_x)
                    or (section.direction == "RL" and t.x > succ_x)
                ):
                    succ_x = t.x
            if pred_x is None or succ_x is None:
                return None
            return (round(pred_x, 3), round(succ_x, 3))

        columns: dict[tuple[float, float], list[str]] = defaultdict(list)
        for sid in section.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.is_hidden:
                continue
            key = _column_key(sid)
            if key is None:
                continue
            # Station must sit strictly between its trunk-Y neighbours
            # for the column to be a meaningful horizontal extent.
            pred_x, succ_x = key
            lo, hi = min(pred_x, succ_x), max(pred_x, succ_x)
            if not (lo < st.x < hi):
                continue
            columns[key].append(sid)

        for key, members in columns.items():
            if len(members) < 2:
                continue
            trunk_members: list[str] = []
            anchor_xs: list[float] = []
            for sid in members:
                st = graph.stations[sid]
                if abs(st.y - trunk_y) <= 0.5:
                    trunk_members.append(sid)
                    continue
                # Anchor X must come from a station pass-1 already
                # placed at the loop midpoint; restrict to the same
                # single-in/single-out filter pass-1 uses.
                visible_ins = [
                    e
                    for e in graph.edges_to(sid)
                    if (
                        (gs := graph.stations.get(e.source)) is not None
                        and not gs.is_hidden
                    )
                ]
                visible_outs = [
                    e
                    for e in graph.edges_from(sid)
                    if (
                        (gs := graph.stations.get(e.target)) is not None
                        and not gs.is_hidden
                    )
                ]
                if len(visible_ins) == 1 and len(visible_outs) == 1:
                    anchor_xs.append(st.x)
            if not trunk_members or not anchor_xs:
                continue
            target_x = sum(anchor_xs) / len(anchor_xs)
            pred_x, succ_x = key
            lo, hi = min(pred_x, succ_x), max(pred_x, succ_x)
            if not (lo <= target_x <= hi):
                continue
            for sid in trunk_members:
                graph.stations[sid].x = target_x


def _shift_and_propagate_loop_stations(
    graph: MetroGraph,
    y_spacing: float,
    section_y_padding: float,
    section_y_gap: float,
) -> None:
    """Shift sparse loop-side stations onto a half-pitch Y, then
    propagate any bbox growth to lower rows.

    Two-phase unified helper.  Phase 1 shifts sparse single-line loop
    stations clear of busier siblings' inbound bundles (and may grow
    the section bbox downward).  Phase 2 propagates any bbox growth
    to lower rows so ``section_y_gap`` is preserved; it is a no-op
    when phase 1 didn't grow anything.
    """
    _shift_sparse_loop_stations_to_clear_bundle(graph, y_spacing, section_y_padding)
    _push_lower_rows_after_bbox_grow(graph, section_y_gap)


def _shift_sparse_loop_stations_to_clear_bundle(
    graph: MetroGraph,
    y_spacing: float,
    section_y_padding: float = SECTION_Y_PADDING,
) -> None:
    """Phase 1 of :func:`_shift_and_propagate_loop_stations`.

    Shift single-line loop side stations onto a half-pitch Y when
    their full-row Y collides with a busier sibling's inbound bundle.

    The bypass virtual station mechanism (``_insert_bypass_stations``)
    covers the pred -> exit_port case (e.g. ``annotate`` between limma
    and the section exit).  It does not cover the case where a sparse
    consumer S sits in the same section column band as a busier
    sibling T and shares S's row Y, so the lines bound for T cross S's
    marker bbox on the way in (the ``grea`` / ``decoupler`` pattern).

    For each loop side station S in an LR/RL section -- one incoming
    edge, one outgoing edge, both endpoints on the section trunk Y --
    that:

      * consumes strictly fewer lines than at least one same-row
        sibling T in the same section, and
      * shares the same Y row as that sibling,

    shift S vertically by one full ``y_spacing`` away from the trunk
    on the side it already sits on, so its marker bbox sits clear of
    the sibling's bundle Y range.  The section bbox is grown when
    necessary; phase 2 then closes the row gap that growth opened.
    """
    if y_spacing <= 0:
        return

    def _consumed_lines(sid: str) -> set[str]:
        return {e.line_id for e in graph.edges_to(sid)}

    for section in graph.sections.values():
        if section.bbox_h <= 0 or section.direction not in ("LR", "RL"):
            continue
        port_ids = section.port_ids
        # Trunk Y from the LR/RL ports.
        trunk_y: float | None = None
        for pid in section.entry_ports + section.exit_ports:
            port = graph.ports.get(pid)
            ps = graph.stations.get(pid)
            if (
                port is not None
                and ps is not None
                and port.side in (PortSide.LEFT, PortSide.RIGHT)
            ):
                trunk_y = ps.y
                break
        if trunk_y is None:
            continue

        for sid in section.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.is_hidden or st.off_track:
                continue
            ins = graph.edges_to(sid)
            outs = graph.edges_from(sid)
            if len(ins) != 1 or len(outs) != 1:
                continue
            src = graph.stations.get(ins[0].source)
            tgt = graph.stations.get(outs[0].target)
            if src is None or tgt is None:
                continue
            if abs(src.y - trunk_y) > 0.5 or abs(tgt.y - trunk_y) > 0.5:
                continue
            # S must sit clearly off the trunk and share its Y row
            # with a same-section sibling whose inbound bundle is
            # busier than S's.
            dy = st.y - trunk_y
            if abs(dy) < 0.5:
                continue
            s_lines = _consumed_lines(sid)
            sibling: Station | None = None
            for sib_id in section.station_ids:
                if sib_id == sid or sib_id in port_ids:
                    continue
                sib = graph.stations.get(sib_id)
                if (
                    sib is None
                    or sib.is_port
                    or sib.is_hidden
                    or sib.off_track
                    or sib.is_terminus
                ):
                    continue
                if abs(sib.y - st.y) > 0.5:
                    continue
                sib_lines = _consumed_lines(sib_id)
                if len(sib_lines) > len(s_lines):
                    sibling = sib
                    break
            if sibling is None:
                continue
            # Shift S one full ``y_spacing`` further FROM the trunk on
            # the side S is already on.  This lifts S clear of the
            # busier sibling's bundle Y range (which is centred at
            # the row Y with up to ``max_offset`` of extra height for
            # the line stack).  A half-pitch shift would leave S on a
            # half-grid Y; the half-grid offset is reserved for the
            # 2-branch symmetric fan case, so single sparse-loop
            # stations must land on a full grid row.
            shift = y_spacing if dy > 0 else -y_spacing
            new_y = st.y + shift
            # Grow the section bbox so the standard ``section_y_padding``
            # sits between the shifted station's marker edge and the
            # bbox edge.  The earlier ``+ STATION_RADIUS_APPROX`` -only
            # buffer kept the validator happy but left the bbox flush
            # against the station marker, breaking the visual padding
            # invariant other sections satisfy after
            # ``_shrink_bboxes_to_content_bottom``.
            edge_pad = STATION_RADIUS_APPROX + section_y_padding
            sec_top = section.bbox_y
            sec_bottom = section.bbox_y + section.bbox_h
            if new_y < sec_top + edge_pad:
                grow = sec_top + edge_pad - new_y
                section.bbox_y -= grow
                section.bbox_h += grow
            elif new_y > sec_bottom - edge_pad:
                grow = new_y - (sec_bottom - edge_pad)
                section.bbox_h += grow
            st.y = new_y
