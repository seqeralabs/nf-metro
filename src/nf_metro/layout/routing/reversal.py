"""Reversed section detection for routing bundle ordering.

A section is "reversed" when it receives lines via a TB section's
exit port that reverses the bundle ordering. This affects Y offset
assignments in compute_station_offsets().
"""

from __future__ import annotations

from nf_metro.layout.geometry import AxisFrame, lanes_run_along_x
from nf_metro.layout.routing.common import (
    tb_right_entry_sections,
    vertical_flow_sections,
)
from nf_metro.parser.model import MetroGraph, Port, PortSide, Section


def _fed_by_bottom_exit_fold(
    graph: MetroGraph, port_id: str, junction_ids: set[str]
) -> bool:
    """Whether ``port_id`` is fed through a fold junction by a BOTTOM exit.

    The fold junction drops the incoming bundle vertically (BOTTOM exit
    -> junction) and turns it into the entry port; the down->turn
    concentric corner is what reverses the bundle ordering.
    """
    for edge in graph.edges_to(port_id):
        if edge.source not in junction_ids:
            continue
        for upstream in graph.edges_to(edge.source):
            src_port = graph.ports.get(upstream.source)
            if src_port and not src_port.is_entry and src_port.side == PortSide.BOTTOM:
                return True
    return False


def detect_reversed_sections(graph: MetroGraph) -> set[str]:
    """Find sections where incoming bundle ordering is reversed.

    A section is "reversed" when it receives lines via a TB section's
    exit port in a way that reverses the bundle ordering.  This happens
    in three cases:

    1. TOP entry fed by a TB BOTTOM exit:
       - Horizontal (LR/RL) receiver: always marked reversed so its
         in-section draw (``y + offset``) matches the dropped X positions.
       - Vertical (TB/BT) receiver: marked reversed only when the feeder
         TB section is itself positive_fan (RIGHT-entry or seam-free), so
         the receiver also draws on the ``+x`` side -- preserving the
         straight column continuation.

    2. LEFT/RIGHT entry fed by a TB section's LEFT/RIGHT exit: the
       concentric corner routing reverses the bundle ordering (outermost
       vertical line becomes outermost horizontal line), so the downstream
       section must use reversed Y ordering to match.

    3. RIGHT entry fed through a fold junction by a BOTTOM exit: the
       exit drops the bundle vertically (X offsets) and the fold
       junction turns it left into the RIGHT entry.  That down->left
       concentric corner reverses the bundle ordering, so the
       downstream section must use reversed Y ordering to match.

    Reversal propagates: if a reversed section exits to another section
    on the same row, that downstream section is also reversed so bundle
    ordering stays consistent along the return row.
    """
    tb_sections = vertical_flow_sections(graph)
    tb_right_entry = tb_right_entry_sections(graph)
    reversed_secs: set[str] = set()
    junction_ids = graph.junction_ids

    # TB sections that draw on the +x side without needing reversal context:
    # RIGHT-entry sections and seam-free sections.  These seed the positive_fan
    # status that must propagate to any TB section stacked below them.
    initial_tb_positive_fan = tb_right_entry | {
        sid for sid in tb_sections if _is_seam_free_section(graph.sections[sid])
    }
    _detect_tb_bottom_top_entries(
        graph, tb_sections, reversed_secs, initial_tb_positive_fan
    )
    _detect_fold_right_entries(graph, junction_ids, reversed_secs)

    sec_successors, horizontal_succ_pairs = _build_section_adjacency(graph)

    # Propagate Phase 1a reversals to horizontal successors
    # (e.g. stat_analysis -> reporting via LEFT exit -> RIGHT entry).
    _propagate_reversal_along_rows(
        graph, reversed_secs, tb_sections, sec_successors, horizontal_succ_pairs
    )

    _detect_tb_lr_exit_fed(
        graph,
        reversed_secs,
        tb_sections,
        tb_right_entry,
        junction_ids,
        sec_successors,
        horizontal_succ_pairs,
    )

    return reversed_secs


def _is_seam_free_section(section: Section) -> bool:
    """Whether *section* connects to no other section (no entry or exit ports).

    Its bundle has no seam to anchor a side, while auto-layout seats the trunk
    in the leftmost column and every fan branch in a ``+x`` column, so the
    bundle belongs on the ``+x`` side.
    """
    return not section.entry_ports and not section.exit_ports


def tb_positive_fan_sections(graph: MetroGraph) -> set[str]:
    """TB sections whose bundle draws on the ``+x`` (feed) side, not rotation.

    A vertical-flow section normally rides the rotation ``x - offset`` (bundle
    left of the column).  A section whose bundle sits on the ``+x`` side instead
    draws ``x + offset`` so the seam corner nests as a rotation and the whole
    feed chain stays on one side: a RIGHT-entry section (the feed wraps in from
    the right), a reverse-flow section (the bundle returns up the right), or a
    seam-free section (no feeder anchors the side, so the internal fan to the
    ``+x`` columns settles it).
    """
    right_entry = tb_right_entry_sections(graph)
    reversed_secs = detect_reversed_sections(graph)
    return {
        sid
        for sid, section in graph.sections.items()
        if AxisFrame.axes_for_direction(section.direction)[0] == "y"
        and (
            sid in right_entry or sid in reversed_secs or _is_seam_free_section(section)
        )
    }


def _detect_tb_bottom_top_entries(
    graph: MetroGraph,
    tb_sections: set[str],
    reversed_secs: set[str],
    initial_tb_positive_fan: set[str] | None = None,
) -> None:
    """Phase 1a: mark sections whose TOP entry is fed by a TB BOTTOM exit.

    The rule depends on whether the receiver shares the TB flow axis:

    - **Horizontal (LR/RL) receiver**: marked reversed so its in-section draw
      (``y + offset``) matches the dropped X positions from the exit.

    - **Vertical (TB/BT) receiver**: a straight column continuation; both
      sections share the same rotation sign.  No marking is needed for a
      standard-sign (non-positive_fan) feeder.  But if the feeder is a
      positive_fan TB section (RIGHT entry or seam-free, per
      ``initial_tb_positive_fan``), the receiver must also be marked so its
      draw uses the same ``x + offset`` sign as the drop.
    """
    # Build per-section maps of BOTTOM→TOP connections for vertical receivers.
    # Horizontal receivers are marked immediately (always reversed).
    # Vertical receivers: mark only when the feeder is positive_fan, then
    # cascade: a newly-marked vertical section is itself positive_fan for its
    # downstream vertical receivers.
    tb_positive_fan: set[str] = set(initial_tb_positive_fan or ())
    # vertical_receivers[src_sec] = set of vertical-receiver sec_ids
    vertical_receivers: dict[str, set[str]] = {}
    for sec_id, section in graph.sections.items():
        for port_id in section.entry_ports:
            port = graph.ports.get(port_id)
            if not port or port.side != PortSide.TOP:
                continue
            for edge in graph.edges_to(port_id):
                src = graph.station_for_edge_source(edge)
                if not src.is_port:
                    continue
                src_port = graph.ports.get(edge.source)
                if not (
                    src_port
                    and not src_port.is_entry
                    and src_port.side == PortSide.BOTTOM
                ):
                    continue
                if lanes_run_along_x(section.direction):
                    # Vertical (TB/BT) receiver: a straight column continuation.
                    # Only a vertical-flow feeder seeds the positive_fan cascade;
                    # a horizontal-flow feeder drops in on the trunk column with no
                    # order flip, so it needs no marking.
                    if src.section_id in tb_sections:
                        vertical_receivers.setdefault(src.section_id, set()).add(sec_id)
                else:
                    # Horizontal (LR/RL) receiver: the BOTTOM-exit drop turns into
                    # the trunk through a corner that reverses bundle ordering,
                    # whatever the feeder's flow axis (a horizontal-flow feeder is
                    # the true-serpentine fold, an LR row folding into an RL row).
                    reversed_secs.add(sec_id)

    # BFS: propagate positive_fan through vertical chains.
    queue = list(tb_positive_fan)
    while queue:
        src_sec = queue.pop()
        for receiver in vertical_receivers.get(src_sec, ()):
            if receiver not in reversed_secs:
                reversed_secs.add(receiver)
                tb_positive_fan.add(receiver)
                queue.append(receiver)


def _detect_fold_right_entries(
    graph: MetroGraph, junction_ids: set[str], reversed_secs: set[str]
) -> None:
    """Phase 1c: mark RIGHT entries fed through a fold junction by a BOTTOM exit."""
    for sec_id, section in graph.sections.items():
        if sec_id in reversed_secs:
            continue
        for port_id in section.entry_ports:
            port = graph.ports.get(port_id)
            if not port or port.side != PortSide.RIGHT:
                continue
            if _fed_by_bottom_exit_fold(graph, port_id, junction_ids):
                reversed_secs.add(sec_id)
                break


def _entry_ports_through_junctions(
    graph: MetroGraph, junction_id: str, junction_ids: set[str]
) -> set[str]:
    """Entry ports reachable from *junction_id*, hopping through chained junctions.

    A peel-off junction fans one exit bundle to several downstream sections, so
    the exit reaches each section through the junction rather than a direct
    port-to-port edge.  Walk the junction's outgoing edges (through any further
    junctions) to collect the entry ports the bundle lands on.
    """
    entries: set[str] = set()
    seen: set[str] = set()
    stack = [junction_id]
    while stack:
        jid = stack.pop()
        if jid in seen:
            continue
        seen.add(jid)
        for edge in graph.edges_from(jid):
            if edge.target in junction_ids:
                stack.append(edge.target)
                continue
            port = graph.ports.get(edge.target)
            if port and port.is_entry:
                entries.add(edge.target)
    return entries


def _build_section_adjacency(
    graph: MetroGraph,
) -> tuple[dict[str, set[str]], set[tuple[str, str]]]:
    """Section successors plus the LEFT/RIGHT-to-LEFT/RIGHT continuation pairs.

    The horizontal pairs are those whose inter-section edge runs between
    LEFT/RIGHT ports on both sides (a direction-preserving continuation).  An
    exit feeding a peel-off junction is followed through the junction so its
    downstream sections register even though no direct port-to-port edge joins
    them.
    """
    sec_successors: dict[str, set[str]] = {}
    horizontal_succ_pairs: set[tuple[str, str]] = set()
    junction_ids = graph.junction_ids
    walked_junctions: set[tuple[str, str]] = set()

    def _record(src_id: str, src_sec: str, tgt_id: str, tgt_sec: str) -> None:
        sec_successors.setdefault(src_sec, set()).add(tgt_sec)
        src_port = graph.ports.get(src_id)
        tgt_port = graph.ports.get(tgt_id)
        if (
            src_port
            and not src_port.is_entry
            and src_port.side in (PortSide.LEFT, PortSide.RIGHT)
            and tgt_port
            and tgt_port.is_entry
            and tgt_port.side in (PortSide.LEFT, PortSide.RIGHT)
        ):
            horizontal_succ_pairs.add((src_sec, tgt_sec))

    for edge in graph.edges:
        src, tgt = graph.edge_endpoints(edge)
        if src.section_id and tgt.section_id and src.section_id != tgt.section_id:
            _record(edge.source, src.section_id, edge.target, tgt.section_id)
        elif src.section_id and edge.target in junction_ids:
            # A junction fed by an N-line bundle yields N identical exit-to-
            # junction edges from one section; walk its subtree once per source.
            if (src.section_id, edge.target) in walked_junctions:
                continue
            walked_junctions.add((src.section_id, edge.target))
            for entry_id in _entry_ports_through_junctions(
                graph, edge.target, junction_ids
            ):
                entry = graph.stations.get(entry_id)
                if entry and entry.section_id and entry.section_id != src.section_id:
                    _record(edge.source, src.section_id, entry_id, entry.section_id)
    return sec_successors, horizontal_succ_pairs


def _propagate_reversal_along_rows(
    graph: MetroGraph,
    reversed_secs: set[str],
    tb_sections: set[str],
    sec_successors: dict[str, set[str]],
    horizontal_succ_pairs: set[tuple[str, str]],
) -> bool:
    """Propagate reversal to horizontal successors.

    Propagates when the successor is on the same row or is reached via a
    direct horizontal port connection (LEFT/RIGHT exit to LEFT/RIGHT entry),
    which is effectively a straight continuation with no direction change.

    Returns True if any new sections were added.
    """
    added_any = False
    changed = True
    while changed:
        changed = False
        for sec_id in list(reversed_secs):
            section = graph.sections.get(sec_id)
            if not section:
                continue
            # TB sections transform ordering in their exit L-shape; don't
            # propagate reversal through them to downstream sections (the
            # exit already un-reverses the bundle).
            if sec_id in tb_sections:
                continue
            for succ_id in sec_successors.get(sec_id, set()):
                if succ_id in reversed_secs:
                    continue
                succ = graph.sections.get(succ_id)
                if not succ:
                    continue
                if (
                    succ.grid_row == section.grid_row
                    or (sec_id, succ_id) in horizontal_succ_pairs
                ):
                    reversed_secs.add(succ_id)
                    changed = True
                    added_any = True
    return added_any


def _is_tb_lr_exit_nonreversed(
    port_obj: Port | None,
    tb_sections: set[str],
    tb_right_entry: set[str],
    reversed_secs: set[str],
) -> bool:
    """Check if a non-reversed TB LR exit reverses the bundle into its consumer.

    The exit-port order matches the column (raw priority for a RIGHT-entry
    section, its reverse otherwise) for a LEFT exit and reverses it for a RIGHT
    exit.  The downstream section must use reversed Y only when the exit-port
    ends up in reverse-of-priority order, i.e. when the exit side and the
    entry side agree (RIGHT exit + RIGHT entry, or LEFT exit + non-right entry);
    when they differ the corner lands the bundle back in priority order and the
    consumer stays non-reversed.
    """
    if (
        port_obj is None
        or port_obj.is_entry
        or port_obj.side not in (PortSide.LEFT, PortSide.RIGHT)
        or port_obj.section_id in reversed_secs
        or port_obj.section_id not in tb_sections
    ):
        return False
    right_exit = port_obj.side == PortSide.RIGHT
    right_entry = port_obj.section_id in tb_right_entry
    return right_exit == right_entry


def _section_fed_by_tb_lr_exit(
    graph: MetroGraph,
    section: Section,
    tb_sections: set[str],
    tb_right_entry: set[str],
    junction_ids: set[str],
    reversed_secs: set[str],
) -> bool:
    """True if any LEFT/RIGHT entry of *section* is fed by a non-reversed TB LR exit."""
    for port_id in section.entry_ports:
        port = graph.ports.get(port_id)
        if not port or port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        for edge in graph.edges_to(port_id):
            src = graph.station_for_edge_source(edge)
            if edge.source in junction_ids:
                # Look through junction to find upstream exit port
                for e2 in graph.edges_to(edge.source):
                    s2 = graph.station_for_edge_source(e2)
                    if not s2.is_port:
                        continue
                    s2_port = graph.ports.get(e2.source)
                    if _is_tb_lr_exit_nonreversed(
                        s2_port, tb_sections, tb_right_entry, reversed_secs
                    ):
                        return True
            elif src.is_port:
                src_port = graph.ports.get(edge.source)
                if _is_tb_lr_exit_nonreversed(
                    src_port, tb_sections, tb_right_entry, reversed_secs
                ):
                    return True
    return False


def _detect_tb_lr_exit_fed(
    graph: MetroGraph,
    reversed_secs: set[str],
    tb_sections: set[str],
    tb_right_entry: set[str],
    junction_ids: set[str],
    sec_successors: dict[str, set[str]],
    horizontal_succ_pairs: set[tuple[str, str]],
) -> None:
    """Phase 1b + Phase 2: mark sections fed by TB LEFT/RIGHT exits, iteratively.

    The concentric corner reverses the bundle ordering ONLY when the feeding TB
    section is RIGHT-entry (its column runs in raw priority order) and is not
    itself already reversed.  A non-right-entry TB section runs its column
    reversed, so the exit corner flips it back to standard and the downstream
    section stays non-reversed; if the TB section is itself already reversed
    (e.g. via propagation from an earlier TB exit), its exit L-shape likewise
    un-reverses, so the downstream section should NOT be marked reversed.

    Process one TB exit at a time: add the downstream section, propagate along
    rows (which may mark the next TB section as reversed), then re-scan.  This
    ensures propagation from an earlier TB exit's downstream is visible when
    checking later TB exits (e.g. calling -> hard_filter -> ... -> integration).
    """
    stable = False
    while not stable:
        stable = True
        for sec_id, section in graph.sections.items():
            if sec_id in reversed_secs:
                continue
            if _section_fed_by_tb_lr_exit(
                graph, section, tb_sections, tb_right_entry, junction_ids, reversed_secs
            ):
                reversed_secs.add(sec_id)
                _propagate_reversal_along_rows(
                    graph,
                    reversed_secs,
                    tb_sections,
                    sec_successors,
                    horizontal_succ_pairs,
                )
                stable = False
                break  # restart outer scan
