"""Reversed section detection for routing bundle ordering.

A section is "reversed" when it receives lines via a TB section's
exit port that reverses the bundle ordering. This affects Y offset
assignments in compute_station_offsets().
"""

from __future__ import annotations

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
    exit port that reverses the bundle ordering. This happens in two cases:

    1. TOP entry fed by a TB section's BOTTOM exit: the TB section reverses
       X offsets in the vertical bundle, so the downstream section must use
       reversed Y ordering to match.

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
    tb_sections = {sid for sid, s in graph.sections.items() if s.direction == "TB"}
    reversed_secs: set[str] = set()
    junction_ids = graph.junction_ids

    _detect_tb_bottom_top_entries(graph, tb_sections, reversed_secs)
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
        junction_ids,
        sec_successors,
        horizontal_succ_pairs,
    )

    return reversed_secs


def _detect_tb_bottom_top_entries(
    graph: MetroGraph, tb_sections: set[str], reversed_secs: set[str]
) -> None:
    """Phase 1a: mark sections whose TOP entry is fed by a TB BOTTOM exit."""
    for sec_id, section in graph.sections.items():
        for port_id in section.entry_ports:
            port = graph.ports.get(port_id)
            if not port or port.side != PortSide.TOP:
                continue
            for edge in graph.edges_to(port_id):
                src = graph.stations.get(edge.source)
                if not src or not src.is_port:
                    continue
                src_port = graph.ports.get(edge.source)
                if (
                    src_port
                    and not src_port.is_entry
                    and src_port.side == PortSide.BOTTOM
                    and src.section_id in tb_sections
                ):
                    reversed_secs.add(sec_id)


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


def _build_section_adjacency(
    graph: MetroGraph,
) -> tuple[dict[str, set[str]], set[tuple[str, str]]]:
    """Section successors plus the LEFT/RIGHT-to-LEFT/RIGHT continuation pairs.

    The horizontal pairs are those whose inter-section edge runs between
    LEFT/RIGHT ports on both sides (a direction-preserving continuation).
    """
    sec_successors: dict[str, set[str]] = {}
    horizontal_succ_pairs: set[tuple[str, str]] = set()
    for edge in graph.edges:
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if not src or not tgt:
            continue
        if src.section_id and tgt.section_id and src.section_id != tgt.section_id:
            sec_successors.setdefault(src.section_id, set()).add(tgt.section_id)
            src_port = graph.ports.get(edge.source)
            tgt_port = graph.ports.get(edge.target)
            if (
                src_port
                and not src_port.is_entry
                and src_port.side in (PortSide.LEFT, PortSide.RIGHT)
                and tgt_port
                and tgt_port.is_entry
                and tgt_port.side in (PortSide.LEFT, PortSide.RIGHT)
            ):
                horizontal_succ_pairs.add((src.section_id, tgt.section_id))
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
    port_obj: Port | None, tb_sections: set[str], reversed_secs: set[str]
) -> bool:
    """Check if port is an LR exit of a non-reversed TB section."""
    return (
        port_obj is not None
        and not port_obj.is_entry
        and port_obj.side in (PortSide.LEFT, PortSide.RIGHT)
        and port_obj.section_id in tb_sections
        and port_obj.section_id not in reversed_secs
    )


def _section_fed_by_tb_lr_exit(
    graph: MetroGraph,
    section: Section,
    tb_sections: set[str],
    junction_ids: set[str],
    reversed_secs: set[str],
) -> bool:
    """True if any LEFT/RIGHT entry of *section* is fed by a non-reversed TB LR exit."""
    for port_id in section.entry_ports:
        port = graph.ports.get(port_id)
        if not port or port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        for edge in graph.edges_to(port_id):
            src = graph.stations.get(edge.source)
            if not src:
                continue
            if edge.source in junction_ids:
                # Look through junction to find upstream exit port
                for e2 in graph.edges_to(edge.source):
                    s2 = graph.stations.get(e2.source)
                    if not s2 or not s2.is_port:
                        continue
                    s2_port = graph.ports.get(e2.source)
                    if _is_tb_lr_exit_nonreversed(s2_port, tb_sections, reversed_secs):
                        return True
            elif src.is_port:
                src_port = graph.ports.get(edge.source)
                if _is_tb_lr_exit_nonreversed(src_port, tb_sections, reversed_secs):
                    return True
    return False


def _detect_tb_lr_exit_fed(
    graph: MetroGraph,
    reversed_secs: set[str],
    tb_sections: set[str],
    junction_ids: set[str],
    sec_successors: dict[str, set[str]],
    horizontal_succ_pairs: set[tuple[str, str]],
) -> None:
    """Phase 1b + Phase 2: mark sections fed by TB LEFT/RIGHT exits, iteratively.

    The concentric corner reverses the bundle ordering ONLY when the TB
    section uses non-reversed internal offsets.  If the TB section is itself
    already reversed (e.g. via propagation from an earlier TB exit), its exit
    L-shape un-reverses back to standard, so the downstream section should
    NOT be marked reversed.

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
                graph, section, tb_sections, junction_ids, reversed_secs
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
