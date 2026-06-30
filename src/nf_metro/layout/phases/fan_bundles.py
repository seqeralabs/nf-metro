"""Symmetric fan-out/fan-in bundle distribution and half-grid placement."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator

from nf_metro.layout.constants import (
    SAME_COORD_TOLERANCE,
    SECTION_Y_PADDING,
)
from nf_metro.layout.phase_state import require_phase_field
from nf_metro.layout.phases._common import (
    _fan_offsets,
    _grid_group_section_ids,
    _section_bundle_lines,
    _section_lr_port_anchor_y,
)
from nf_metro.parser.model import MetroGraph, PortSide, Section, Station


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
                pre = graph.stations.get(e2.source)
                if pre is None or pre.is_port:
                    continue
                inbound[edge.target].add(e2.source)
        else:
            src = graph.stations.get(src_id)
            if src is None or src.is_port:
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


def _divergence_target_ys(graph: MetroGraph) -> set[str]:
    """Return station/port ids that are fan-out divergence anchors.

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
                post = graph.stations.get(e2.target)
                if post is None or post.is_port:
                    continue
                outbound[edge.source].add(e2.target)
        else:
            tgt = graph.stations.get(tgt_id)
            if tgt is None or tgt.is_port:
                continue
            outbound[edge.source].add(tgt_id)

    anchors: set[str] = set()
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
            anchors.add(src_id)
    return anchors


def _real_predecessors(graph: MetroGraph, target_ids: set[str]) -> set[str]:
    """Real-station predecessors of ``target_ids``, seen through junctions.

    A junction between a producer and the target is transparent: the producer
    one step further back is returned in its place, so a fan fed through a single
    bundle junction resolves to its source station.
    """
    junction_ids = graph.junction_ids
    preds: set[str] = set()
    for tid in target_ids:
        for edge in graph.edges_to(tid):
            src_id = edge.source
            if src_id in junction_ids:
                for e2 in graph.edges_to(src_id):
                    preds.add(e2.source)
            else:
                preds.add(src_id)
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

    for section in graph.sections.values():
        if (
            section.id not in grid_sec_ids
            or section.direction not in ("LR", "RL")
            or section.bbox_h <= 0
        ):
            continue
        bundle = _section_bundle_lines(graph, section)
        if not bundle:
            continue
        port_ids = section.port_ids

        # Group non-port, on-track stations by column x.  Off-track
        # stations (file inputs lifted above their consumer) are placed
        # by ``_lift_off_track_stations`` and must not occupy a column
        # slot here.
        cols: dict[float, list[str]] = defaultdict(list)
        for sid in section.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.off_track:
                continue
            cols[round(st.x, 3)].append(sid)

        for sids in cols.values():
            # Identify trunk station in this column: lines == bundle, unique.
            trunks = [s for s in sids if set(graph.station_lines(s)) == bundle]
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
                and set(graph.station_lines(s)) < bundle
                and graph.edges_to(s)
            ]
            if not siblings:
                continue
            siblings.sort(key=lambda s: (graph.stations[s].track, s))
            for i, sid in enumerate(siblings, 1):
                k = (i + 1) // 2
                sign = 1 if (i % 2 == 1) else -1
                graph.stations[sid].y = trunk_y + sign * k * y_spacing


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

    - Exactly two non-terminus branch stations sharing a column, with no
      in-section non-terminus source among them.  The fan source is upstream
      (an entry port, or an in-section terminus source icon excluded from the
      branch count).
    - An in-section non-terminus source feeding exactly two equal-sibling
      branches (identical line sets): the source is excluded from the branch
      count as the hub.  The equal-sibling requirement keeps genuine
      trunk-continuation fans (one branch carrying the onward bundle, the other
      a strict subset) out of this path.

    ``hub`` is reported only when a single in-section on-track source feeds
    both equal-sibling branches, so callers can centre it between them.
    """
    port_ids = section.port_ids
    nonterm: list[Station] = []
    has_off_track = False
    by_col: dict[float, int] = defaultdict(int)
    for sid in section.station_ids:
        if sid in port_ids:
            continue
        st = graph.stations.get(sid)
        if st is None or st.is_port or st.is_hidden:
            continue
        if st.off_track:
            has_off_track = True
            continue
        if st.is_terminus:
            # Terminus icons (file outputs / source icons) are not branch
            # participants; a source icon is recovered as the hub below.
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
    if not all(count <= 2 for count in by_col.values()):
        return None

    lines_equal = set(graph.station_lines(branches[0].id)) == set(
        graph.station_lines(branches[1].id)
    )
    if hub is not None and not lines_equal:
        return None
    if hub is None and lines_equal:
        preds = _real_predecessors(graph, {branches[0].id})
        if len(preds) == 1 and preds == _real_predecessors(graph, {branches[1].id}):
            src = graph.stations.get(next(iter(preds)))
            if (
                src is not None
                and not src.is_port
                and not src.is_hidden
                and not src.off_track
                and src.section_id == section.id
            ):
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
            src = graph.stations.get(src_id)
            if (
                src is None
                or src.is_port
                or src.is_hidden
                or src.off_track
                or src.section_id != section.id
                or src_id in branch_ids
            ):
                continue
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

    True when :func:`_symfan_branches_hub` identifies a 2-branch symmetric fan:
    two on-track branch stations sharing one X column, no off-track stations,
    no other multi-branch column, fed either from upstream (entry port or
    terminus source icon) or from a single in-section equal-sibling source.

    When the trigger fires the two branch stations are placed at
    ``trunk_y +/- 0.5 * y_spacing`` instead of the default
    ``trunk_y +/- 1 * y_spacing``, so the section needs only one vertical grid
    unit instead of two.  The branches sit at half-pitch relative to the row
    grid; ``_snap_all_y_to_grid`` skips them via
    ``graph.half_grid_station_ids``.
    """
    return _symfan_branches_hub(graph, section) is not None


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
    for fork_st, s1, s2, join_st in _iter_fork_join_diamonds(graph):
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

    for section in graph.sections.values():
        if (
            section.id not in grid_sec_ids
            or section.direction not in ("LR", "RL")
            or section.bbox_h <= 0
        ):
            continue
        bundle = _section_bundle_lines(graph, section)
        if not bundle:
            continue
        port_ids = section.port_ids

        cols: dict[float, list[str]] = defaultdict(list)
        for sid in section.station_ids:
            if sid in port_ids:
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
            x: [s for s in sids if set(graph.station_lines(s)) == bundle]
            for x, sids in cols.items()
        }
        # Snapshot pre-fan Ys so iteration order of columns doesn't
        # drift the trunk reference: a later column must not see an
        # earlier column's already-fanned positions.
        pre_fan_y = {
            sid: graph.stations[sid].y for sids in cols.values() for sid in sids
        }
        port_ys = [
            graph.ports[pid].y
            for pid in port_ids
            if graph.ports.get(pid) is not None
            and graph.ports[pid].side in (PortSide.LEFT, PortSide.RIGHT)
        ]

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
                and set(graph.station_lines(s)) < bundle
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
            # Strict all-full columns always fire (the original
            # behaviour).  Mixed columns (full + non-source siblings)
            # only fire when another column in the section is
            # all-full, so we have a consistent trunk_y for the row
            # and don't accidentally fan single trunk + 1 sibling
            # cases that belong to ``_redistribute_fanout_siblings``.
            all_full = not non_full
            if not all_full and not any_all_full_col:
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
                others = sorted(
                    pre_fan_y[s]
                    for ox, sids in full_by_col.items()
                    if ox != x
                    for s in sids
                )
                if not others:
                    continue
                trunk_y = others[len(others) // 2]
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

    Late-pass companion to ``_redistribute_full_bundle_columns``.  The
    early pass uses the section's local LR port Y as the symmetric
    centre, which becomes stale when subsequent phases shift the
    section relative to the row trunk (e.g. terminal sections whose
    sole LR port doesn't match the bundle line entering from upstream).

    For each LR/RL grid section, locate the inter-section bundle Y from
    the entry/exit port station Y (which by this point sits on the
    row's bundle Y after row alignment).  Then re-distribute each
    column of >=2 full-bundle stations around that anchor at
    ``y_spacing`` pitch, preserving the order produced by the first
    pass.

    No-op when the existing layout is already symmetric around the
    anchor; bbox heights are not adjusted because earlier compaction
    already sized the band to fit two slots either side of the trunk.
    """
    grid_sec_ids = _grid_group_section_ids(graph)
    if not grid_sec_ids:
        return

    for section in graph.sections.values():
        if (
            section.id not in grid_sec_ids
            or section.direction not in ("LR", "RL")
            or section.bbox_h <= 0
        ):
            continue
        bundle = _section_bundle_lines(graph, section)
        if not bundle:
            continue
        port_ids = section.port_ids

        cols: dict[float, list[str]] = defaultdict(list)
        for sid in section.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.off_track:
                continue
            cols[round(st.x, 3)].append(sid)

        def _has_pred(sid: str) -> bool:
            return bool(graph.edges_to(sid))

        full_by_col = {
            x: [s for s in sids if set(graph.station_lines(s)) == bundle]
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
                    and set(graph.station_lines(s)) < bundle
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
            offsets = _fan_offsets(n)
            for sid, off in zip(participants, offsets):
                graph.stations[sid].y = anchor_y + off * pitch
