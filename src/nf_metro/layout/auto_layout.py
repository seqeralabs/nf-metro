"""Auto-layout: infer section grid positions, directions, and port sides.

Runs BEFORE _resolve_sections() in the parser. Scans inter-section edges
(by comparing station.section_id) and fills in missing grid_overrides,
section.direction, section.entry_hints, and section.exit_hints.

Preserves any values explicitly set by %%metro directives.
"""

from __future__ import annotations

__all__ = ["infer_section_layout", "detect_serpentine_runs"]

from collections import defaultdict, deque

from nf_metro.parser.model import MetroGraph, PortSide, SectionDAG


def infer_section_layout(graph: MetroGraph, max_station_columns: int = 15) -> None:
    """Infer missing layout parameters for sections.

    Mutates graph in-place. Fills in missing grid_overrides,
    section.direction, section.entry_hints, and section.exit_hints.
    Preserves any values explicitly set by %%metro directives.

    max_station_columns: fold into a new row when the cumulative station
    layer count across sections in a row exceeds this threshold.
    """
    if len(graph.sections) <= 1:
        graph.section_dag = SectionDAG(successors={}, predecessors={}, edge_lines={})
        return

    successors, predecessors, edge_lines = _build_section_dag(graph)
    graph.section_dag = SectionDAG(
        successors=successors, predecessors=predecessors, edge_lines=edge_lines
    )

    # Only run grid/direction/port inference if there are inter-section edges
    if not successors and not predecessors:
        return

    fold_sections, below_fold_sections, convergence_sections = _assign_grid_positions(
        graph,
        successors,
        predecessors,
        max_station_columns,
    )
    _optimize_rowspans(graph, fold_sections, successors)
    _adjust_explicit_tb_sections(graph, successors, fold_sections)
    _infer_directions(
        graph, successors, predecessors, fold_sections, below_fold_sections
    )
    _optimize_colspans(graph, fold_sections, below_fold_sections, successors)
    _infer_port_sides(
        graph,
        successors,
        predecessors,
        edge_lines,
        fold_sections,
        convergence_sections,
    )


def _build_section_dag(
    graph: MetroGraph,
) -> tuple[
    dict[str, set[str]],
    dict[str, set[str]],
    dict[tuple[str, str], set[str]],
]:
    """Build section dependency DAG from inter-section edges.

    Returns:
        successors: section_id -> set of downstream section_ids
        predecessors: section_id -> set of upstream section_ids
        edge_lines: (src_section, tgt_section) -> set of line_ids
    """
    successors: dict[str, set[str]] = defaultdict(set)
    predecessors: dict[str, set[str]] = defaultdict(set)
    edge_lines: dict[tuple[str, str], set[str]] = defaultdict(set)

    for edge in graph.edges:
        src_sec = graph.section_for_station(edge.source)
        tgt_sec = graph.section_for_station(edge.target)
        if src_sec and tgt_sec and src_sec != tgt_sec:
            successors[src_sec].add(tgt_sec)
            predecessors[tgt_sec].add(src_sec)
            edge_lines[(src_sec, tgt_sec)].add(edge.line_id)

    return dict(successors), dict(predecessors), dict(edge_lines)


def _estimate_section_layers(graph: MetroGraph, section_id: str) -> int:
    """Estimate the number of station layers (horizontal span) for a section.

    Computes the longest path through internal edges via topological DP.
    Returns at least 1.
    """
    section = graph.sections[section_id]
    station_ids = set(section.station_ids)

    # Build adjacency for internal edges only
    adj: dict[str, set[str]] = defaultdict(set)
    has_pred: set[str] = set()
    for edge in graph.edges:
        if edge.source in station_ids and edge.target in station_ids:
            adj[edge.source].add(edge.target)
            has_pred.add(edge.target)

    if not adj:
        return max(len(station_ids), 1)

    # BFS longest path from roots
    roots = station_ids - has_pred
    if not roots:
        return len(station_ids)

    longest: dict[str, int] = {sid: 0 for sid in station_ids}
    queue: deque[str] = deque(roots)
    visited: set[str] = set()

    while queue:
        node = queue.popleft()
        if node in visited:
            continue
        visited.add(node)
        for succ in adj.get(node, set()):
            if longest[node] + 1 > longest[succ]:
                longest[succ] = longest[node] + 1
            queue.append(succ)

    return max(longest.values()) + 1  # +1: convert 0-indexed depth to layer count


def _assign_grid_positions(
    graph: MetroGraph,
    successors: dict[str, set[str]],
    predecessors: dict[str, set[str]],
    max_station_columns: int,
) -> tuple[set[str], set[str], set[str]]:
    """Assign grid (col, row) positions to sections without explicit grid overrides.

    When cumulative station columns in a row exceed the threshold, the
    overflowing topo column becomes a "fold section" - it stays at the
    right edge of the current row as a TB bridge. Subsequent topo columns
    go into a new row below.

    Returns (fold_sections, below_fold_sections). below_fold_sections are
    sections placed directly below a fold instead of on the return row,
    used when the fold has a single successor and the band has stacked
    sections (making a return row visually awkward).
    """
    section_ids = list(graph.sections.keys())

    # BFS topological sort for column assignment
    all_sections = set(section_ids)
    in_degree: dict[str, int] = {sid: 0 for sid in section_ids}
    adj: dict[str, list[str]] = {sid: [] for sid in section_ids}

    for src, targets in successors.items():
        for tgt in targets:
            if src in all_sections and tgt in all_sections:
                adj[src].append(tgt)
                in_degree[tgt] += 1

    col_assign: dict[str, int] = {}
    queue: deque[str] = deque()
    for sid in section_ids:
        if in_degree[sid] == 0:
            queue.append(sid)
            col_assign[sid] = 0

    while queue:
        sid = queue.popleft()
        for tgt in adj[sid]:
            new_col = col_assign[sid] + 1
            if tgt not in col_assign or new_col > col_assign[tgt]:
                col_assign[tgt] = new_col
            in_degree[tgt] -= 1
            if in_degree[tgt] == 0:
                queue.append(tgt)

    # Handle disconnected sections
    for sid in section_ids:
        if sid not in col_assign:
            col_assign[sid] = 0

    # Skip sections already in grid_overrides
    auto_sections = {sid for sid in section_ids if sid not in graph.grid_overrides}

    # Group auto sections by topo column
    col_groups: dict[int, list[str]] = defaultdict(list)
    for sid in section_ids:
        if sid in auto_sections:
            col_groups[col_assign[sid]].append(sid)

    # Sort within each column by definition order
    section_order = list(graph.sections.keys())
    for col in col_groups:
        col_groups[col].sort(key=lambda s: section_order.index(s))

    if not col_groups:
        return set(), set(), set()

    # Estimate station-layer width per topo column (max across stacked sections)
    topo_col_width: dict[int, int] = {}
    for col, sids in col_groups.items():
        topo_col_width[col] = max(_estimate_section_layers(graph, sid) for sid in sids)

    # --- Convergence-based row split ---
    # Detect sections with predecessors spanning 2+ non-adjacent topo columns.
    # These are natural "convergence points" that should drop to a return row
    # along with their downstream sections, instead of extending the top row.
    convergence_result = _detect_convergence_split(
        col_assign,
        col_groups,
        successors,
        predecessors,
    )
    if convergence_result is not None:
        return _place_with_convergence(
            graph,
            col_groups,
            topo_col_width,
            col_assign,
            successors,
            predecessors,
            convergence_result,
        )

    # Greedily pack topo columns into row bands.
    # When overflow is detected, the overflowing column becomes the fold
    # section (TB bridge) at the right edge of the current row. Subsequent
    # columns start a new row band below.
    sorted_cols = sorted(col_groups.keys())
    fold_sections: set[str] = set()
    below_fold_sections: set[str] = set()
    folded: dict[str, tuple[int, int]] = {}
    skip_topo_cols: set[int] = set()

    current_grid_col = 0
    col_step = 1  # +1 in first row (LR), -1 after a fold (RL)
    band_start_row = 0
    max_stack_in_band = 0  # tallest topo column (stacking) in this band
    cumulative_width = 0

    for topo_idx, topo_col in enumerate(sorted_cols):
        if topo_col in skip_topo_cols:
            continue

        sids = col_groups[topo_col]
        w = topo_col_width[topo_col]
        stack_size = len(sids)
        is_last_col = topo_idx == len(sorted_cols) - 1
        # Don't fold the final topo column: there are no further columns to
        # wrap into the next row, so a fold here only creates a spurious TB
        # bridge (and a negative grid column) instead of bending the chain.
        need_fold = (
            not is_last_col
            and cumulative_width > 0
            and cumulative_width + w > max_station_columns
        )

        if need_fold:
            fold_col = current_grid_col
            # This column is the fold point: place at right edge as TB bridge
            for i, sid in enumerate(sids):
                folded[sid] = (fold_col, band_start_row + i)
                fold_sections.add(sid)
            band_height = max(max_stack_in_band, stack_size)
            # Start new row band below all stacked rows in the current band
            band_start_row += max(band_height, 1)
            # Post-fold sections flow in the opposite direction, starting
            # one column past the fold (i.e. to its left on a return row).
            # Toggle: +1 -> -1 -> +1 (serpentine/zigzag layout).
            col_step = -col_step
            current_grid_col = fold_col + col_step
            cumulative_width = 0
            max_stack_in_band = 0

            # Below-fold placement: when the band has stacked sections
            # (band_height > 1), a return row would route backward over
            # that content. If every fold section has exactly one successor
            # and those successors are the only sections in the next topo
            # column, place them directly below the fold instead.
            if band_height > 1 and topo_idx + 1 < len(sorted_cols):
                next_topo = sorted_cols[topo_idx + 1]
                next_sids = col_groups[next_topo]
                fold_succs: set[str] = set()
                all_single = True
                for fs in sids:
                    fs_succs = successors.get(fs, set())
                    if len(fs_succs) != 1:
                        all_single = False
                        break
                    fold_succs.update(fs_succs)
                if all_single and fold_succs == set(next_sids):
                    for j, ns in enumerate(next_sids):
                        folded[ns] = (fold_col, band_start_row + j)
                        below_fold_sections.add(ns)
                    skip_topo_cols.add(next_topo)
                    # Don't increment band_start_row: below-fold sections
                    # are in the fold column, so return-row sections (in
                    # adjacent columns) can share the same rows.
        else:
            # Normal placement in current band
            for i, sid in enumerate(sids):
                folded[sid] = (current_grid_col, band_start_row + i)
            max_stack_in_band = max(max_stack_in_band, stack_size)
            current_grid_col += col_step
            cumulative_width += w

    # Boustrophedon normalization: a return row built by stepping left from
    # the fold bridge can run off the left edge into negative columns. Shift
    # the whole grid right so the leftmost column is 0, keeping every
    # section's relative position (and thus the serpentine read order).
    if folded:
        min_col = min(col for col, _ in folded.values())
        if min_col < 0:
            folded = {sid: (col - min_col, row) for sid, (col, row) in folded.items()}

    # Write results to grid_overrides and section fields
    for sid, (col, row) in folded.items():
        graph.grid_overrides[sid] = (col, row, 1, 1)
        graph.sections[sid].grid_col = col
        graph.sections[sid].grid_row = row

    return fold_sections, below_fold_sections, set()


def _detect_convergence_split(
    col_assign: dict[str, int],
    col_groups: dict[int, list[str]],
    successors: dict[str, set[str]],
    predecessors: dict[str, set[str]],
) -> set[str] | None:
    """Detect a convergence section and return the set of sections for a return row.

    A convergence section has predecessors from 2+ non-adjacent topo columns
    (spread >= 2). Returns the set of sections that should drop to row 1,
    or None if no convergence is detected.
    """
    # Find earliest convergence section (by topo col).
    # Skip terminal sections (no successors) - they are sinks that collect
    # from many upstream sections and don't benefit from a return row.
    convergence_sid = None
    for sid in sorted(col_assign, key=lambda s: col_assign[s]):
        if not successors.get(sid):
            continue
        pred_cols = set()
        for p in predecessors.get(sid, set()):
            if p in col_assign:
                pred_cols.add(col_assign[p])
        if len(pred_cols) >= 2 and max(pred_cols) - min(pred_cols) >= 2:
            convergence_sid = sid
            break

    if not convergence_sid:
        return None

    # Return set: convergence + transitive successors
    return_set = {convergence_sid} | _transitive_successors(convergence_sid, successors)

    # Companion migration: sections that feed ONLY into the return set
    # AND share a direct predecessor with the convergence section.
    # This catches "satellite" branches (e.g. ensembl_truth, which feeds
    # only into benchmarking, and shares predecessor "filtering" with it).
    # It avoids pulling in main-spine sections (e.g. in A->B->C->D with
    # bypass A->D, sec_c feeds sec_d but sec_c's predecessor sec_b does
    # NOT directly feed sec_d).
    convergence_preds = predecessors.get(convergence_sid, set())
    for sid in list(col_assign.keys()):
        if sid in return_set:
            continue
        sid_succs = successors.get(sid, set())
        if not sid_succs or not sid_succs.issubset(return_set):
            continue
        # Check: does this section share a predecessor with the convergence?
        sid_preds = predecessors.get(sid, set())
        if sid_preds & convergence_preds:
            return_set.add(sid)

    # Only split if the return set has enough sections to justify a second row.
    # A single section (e.g. a bypass target) doesn't warrant a row split.
    if len(return_set) < 2:
        return None

    return return_set


def _place_with_convergence(
    graph: MetroGraph,
    col_groups: dict[int, list[str]],
    topo_col_width: dict[int, int],
    col_assign: dict[str, int],
    successors: dict[str, set[str]],
    predecessors: dict[str, set[str]],
    return_set: set[str],
) -> tuple[set[str], set[str], set[str]]:
    """Place sections using convergence-based row split.

    Row 0 gets spine sections (LR, left to right).
    Row 1 gets return-set sections (RL, right to left), aligned below row 0.
    No TB bridge section is created.
    """
    sorted_cols = sorted(col_groups.keys())
    folded: dict[str, tuple[int, int]] = {}

    # Place row-0 sections (non-return) left to right
    grid_col = 0
    max_row0_col = 0
    for topo_col in sorted_cols:
        row0_sids = [s for s in col_groups[topo_col] if s not in return_set]
        if not row0_sids:
            continue
        for i, sid in enumerate(row0_sids):
            folded[sid] = (grid_col, i)
        max_row0_col = grid_col
        grid_col += 1

    # Place row-1 sections (return set) right to left
    # Sort by topo col ascending: lowest topo col = rightmost on return row
    return_sids = sorted(return_set, key=lambda s: col_assign[s])
    grid_col = max_row0_col
    for sid in return_sids:
        folded[sid] = (grid_col, 1)
        grid_col -= 1

    # Write results
    for sid, (col, row) in folded.items():
        graph.grid_overrides[sid] = (col, row, 1, 1)
        graph.sections[sid].grid_col = col
        graph.sections[sid].grid_row = row

    # Only the entry-point section of the return row (rightmost) needs
    # TOP entry override. Other RL sections use normal port inference
    # (defaulting to RIGHT entry for RL flow).
    entry_section = {return_sids[0]} if return_sids else set()
    return set(), set(), entry_section


def _transitive_successors(
    section_id: str,
    successors: dict[str, set[str]],
) -> set[str]:
    """Compute all transitive successors of a section in the DAG."""
    result: set[str] = set()
    queue = deque(successors.get(section_id, set()))
    while queue:
        sid = queue.popleft()
        if sid in result:
            continue
        result.add(sid)
        queue.extend(successors.get(sid, set()))
    return result


def _optimize_rowspans(
    graph: MetroGraph,
    fold_sections: set[str],
    successors: dict[str, set[str]],
) -> None:
    """Extend fold section rowspans to cover stacked sections in adjacent columns.

    For each fold section (TB bridge), check the column to its left for
    vertically stacked sections. Extend the fold section's rowspan to match
    the number of rows occupied by those adjacent sections.

    Sections that are transitive successors of the fold section (i.e. on
    the return row) are excluded from the rowspan calculation.
    """
    if not fold_sections:
        return

    # Group sections by column
    col_groups: dict[int, list[str]] = defaultdict(list)
    for sid, section in graph.sections.items():
        if section.grid_col >= 0:
            col_groups[section.grid_col].append(sid)

    for fold_sid in fold_sections:
        fold_sec = graph.sections[fold_sid]
        fold_col = fold_sec.grid_col
        fold_row = fold_sec.grid_row

        # Compute transitive successors of this fold section
        downstream = _transitive_successors(fold_sid, successors)

        # Look at the column to the left for stacked sections
        left_col = fold_col - 1
        if left_col not in col_groups:
            continue

        # Find the max row occupied by sections in the left column
        # that are at or below the fold section's row (same band),
        # excluding sections that are downstream of the fold (return row)
        max_row = fold_row
        for sid in col_groups[left_col]:
            if sid in downstream:
                continue
            sec = graph.sections[sid]
            if sec.grid_row >= fold_row:
                max_row = max(max_row, sec.grid_row)

        # Don't extend into rows occupied by other sections in the same column
        for sid in col_groups[fold_col]:
            if sid == fold_sid:
                continue
            sec = graph.sections[sid]
            if sec.grid_row > fold_row:
                max_row = min(max_row, sec.grid_row - 1)

        new_rowspan = max_row - fold_row + 1
        if new_rowspan > fold_sec.grid_row_span:
            fold_sec.grid_row_span = new_rowspan
            graph.grid_overrides[fold_sid] = (
                fold_col,
                fold_row,
                new_rowspan,
                fold_sec.grid_col_span,
            )


def _adjust_explicit_tb_sections(
    graph: MetroGraph,
    successors: dict[str, set[str]],
    fold_sections: set[str],
) -> None:
    """Extend rowspans and adjust successor rows for explicit TB sections.

    When a user sets ``%%metro direction: TB`` on a section, and the adjacent
    column has stacked sections, the TB section should span those rows (like a
    fold bridge). Successors to the right are then placed at the bottom of the
    span so that lines exit downward naturally.
    """
    # Find explicit TB sections that aren't already handled as fold sections
    explicit_tb = {
        sid
        for sid, sec in graph.sections.items()
        if sec.direction == "TB" and sid not in fold_sections and sec.grid_col >= 0
    }
    if not explicit_tb:
        return

    # Group sections by column
    col_groups: dict[int, list[str]] = defaultdict(list)
    for sid, sec in graph.sections.items():
        if sec.grid_col >= 0:
            col_groups[sec.grid_col].append(sid)

    for tb_sid in explicit_tb:
        tb_sec = graph.sections[tb_sid]
        tb_col = tb_sec.grid_col
        tb_row = tb_sec.grid_row

        # Check adjacent column (left) for stacked sections
        left_col = tb_col - 1
        if left_col in col_groups:
            downstream = _transitive_successors(tb_sid, successors)
            max_row = tb_row
            for sid in col_groups[left_col]:
                if sid in downstream:
                    continue
                sec = graph.sections[sid]
                if sec.grid_row >= tb_row:
                    max_row = max(max_row, sec.grid_row)

            # Don't extend into rows occupied by other sections in same column
            for sid in col_groups[tb_col]:
                if sid == tb_sid:
                    continue
                sec = graph.sections[sid]
                if sec.grid_row > tb_row:
                    max_row = min(max_row, sec.grid_row - 1)

            new_rowspan = max_row - tb_row + 1
            if new_rowspan > tb_sec.grid_row_span:
                tb_sec.grid_row_span = new_rowspan
                graph.grid_overrides[tb_sid] = (
                    tb_col,
                    tb_row,
                    new_rowspan,
                    tb_sec.grid_col_span,
                )

        # Move successors from the top of the span to the bottom
        if tb_sec.grid_row_span > 1:
            bottom_row = tb_row + tb_sec.grid_row_span - 1
            for succ_id in successors.get(tb_sid, set()):
                succ = graph.sections.get(succ_id)
                if not succ or succ.grid_row != tb_row:
                    continue
                # Only adjust auto-placed successors in columns to the right
                if succ.grid_col <= tb_col:
                    continue
                succ.grid_row = bottom_row
                graph.grid_overrides[succ_id] = (
                    succ.grid_col,
                    bottom_row,
                    succ.grid_row_span,
                    succ.grid_col_span,
                )


def _optimize_colspans(
    graph: MetroGraph,
    fold_sections: set[str],
    below_fold_sections: set[str] = frozenset(),
    successors: dict[str, set[str]] | None = None,
) -> None:
    """Optimize column spans to reduce dead space from oversized sections.

    Targets columns where one section inflates the width beyond what the
    other sections need. This includes fold columns (where a wide section
    shares with a narrow TB bridge) and columns where a wide RL return-row
    section shares with narrower LR sections. Spanning the wider section
    leftward lets the column width be determined by the narrower sections.
    """
    # Group sections by column
    col_groups: dict[int, list[str]] = defaultdict(list)
    for sid, section in graph.sections.items():
        if section.grid_col >= 0:
            col_groups[section.grid_col].append(sid)

    if not any(len(sids) >= 2 for sids in col_groups.values()):
        return

    # Estimate layers per section
    section_layers: dict[str, int] = {}
    for sid in graph.sections:
        section_layers[sid] = _estimate_section_layers(graph, sid)

    # Compute max estimated layers per column
    col_max_layers: dict[int, int] = {}
    for col, sids in col_groups.items():
        col_max_layers[col] = max(section_layers[sid] for sid in sids)

    # Build a map of occupied (col, row) cells so we can avoid collisions
    occupied: dict[tuple[int, int], str] = {}
    for sid, section in graph.sections.items():
        for c in range(section.grid_col, section.grid_col + section.grid_col_span):
            for r in range(section.grid_row, section.grid_row + section.grid_row_span):
                occupied[(c, r)] = sid

    for col, sids in sorted(col_groups.items()):
        if len(sids) < 2:
            continue

        is_fold_column = any(s in fold_sections for s in sids)

        for sid in sids:
            # Don't span fold sections themselves (they're the narrow ones)
            if sid in fold_sections:
                continue

            # Skip below-fold sections that have downstream sections.
            # Expanding their colspan would push successors further away.
            # Leaf below-fold sections (no successors) still get colspan.
            if sid in below_fold_sections and successors and successors.get(sid):
                continue

            section = graph.sections[sid]

            # Only optimize fold columns (original) or RL return-row sections
            if not is_fold_column and section.direction != "RL":
                continue

            # Check if this section inflates the column width
            other_max = max(section_layers[s] for s in sids if s != sid)
            if section_layers[sid] <= other_max:
                continue
            sec_rows = range(section.grid_row, section.grid_row + section.grid_row_span)

            # Span leftward until accumulated width reaches ~2/3 of this
            # section's layers.  The remaining deficit is distributed by
            # section_placement, so we don't need full coverage - just
            # enough to prevent the home column from being grossly inflated.
            target = section_layers[sid]
            threshold = max(target * 2 // 3, other_max + 1)
            accumulated = other_max  # column's width from other sections
            start_col = col
            colspan = 1

            for left_col in range(col - 1, -1, -1):
                if left_col not in col_max_layers:
                    break
                # Check for row conflicts in the target column
                conflict = False
                for r in sec_rows:
                    occupant = occupied.get((left_col, r))
                    if occupant is not None and occupant != sid:
                        conflict = True
                        break
                if conflict:
                    break
                accumulated += col_max_layers[left_col]
                start_col = left_col
                colspan += 1
                if accumulated >= threshold:
                    break

            if colspan > 1:
                # Update occupied map
                for c in range(start_col, start_col + colspan):
                    for r in sec_rows:
                        occupied[(c, r)] = sid
                section.grid_col = start_col
                section.grid_col_span = colspan
                graph.grid_overrides[sid] = (
                    start_col,
                    section.grid_row,
                    section.grid_row_span,
                    colspan,
                )


def _infer_directions(
    graph: MetroGraph,
    successors: dict[str, set[str]],
    predecessors: dict[str, set[str]],
    fold_sections: set[str],
    below_fold_sections: set[str] = frozenset(),
) -> None:
    """Infer section flow direction (LR/RL/TB) from grid positions.

    Only modifies sections NOT in graph._explicit_directions.
    Fold sections are forced to TB (they bridge between row bands).
    Sections whose predecessors are all to the right get RL.

    Explicitly-gridded sections keep the default LR unless they carry an
    explicit %%metro direction. Their grid position is the author's manual
    layout intent, not a flow-direction signal: inferring RL/TB from it
    reorients serpentine-stacked sections vertically (see #446). Skipping
    them also avoids the stale grid_col == -1 these sections read during
    auto-layout, which previously fired spurious RL/TB against auto-placed
    neighbours in mixed grids.
    """
    for sec_id, section in graph.sections.items():
        if sec_id in graph._explicit_directions:
            continue

        if sec_id in graph._explicit_grid:
            continue

        # Fold sections are always TB (vertical bridge between rows)
        if sec_id in fold_sections:
            section.direction = "TB"
            continue

        my_col = section.grid_col
        my_row = section.grid_row

        # Get successor positions
        succ_cols = []
        succ_rows = []
        for tgt in successors.get(sec_id, set()):
            tgt_sec = graph.sections.get(tgt)
            if tgt_sec and tgt_sec.grid_col >= 0:
                succ_cols.append(tgt_sec.grid_col)
                succ_rows.append(tgt_sec.grid_row)

        # Get predecessor positions
        pred_cols = []
        pred_rows = []
        for src in predecessors.get(sec_id, set()):
            src_sec = graph.sections.get(src)
            if src_sec and src_sec.grid_col >= 0:
                pred_cols.append(src_sec.grid_col)
                pred_rows.append(src_sec.grid_row)

        # RL: all successors to the left (unless they're all strictly
        # below, which is better handled as TB -- but below-fold sections
        # keep RL even when successors are below, since they're on a
        # return row routing leftward)
        if succ_cols and all(c < my_col for c in succ_cols):
            if not all(r > my_row for r in succ_rows) or sec_id in below_fold_sections:
                section.direction = "RL"
                continue

        # RL: leaf section (no successors) and all predecessors are
        # to the right or same column, with at least one strictly to
        # the right or above (post-fold return row or below-fold).
        if not succ_cols and pred_cols:
            if all(c >= my_col for c in pred_cols) and (
                any(r < my_row for r in pred_rows) or any(c > my_col for c in pred_cols)
            ):
                section.direction = "RL"
                continue

        # TB: all successors are below
        if succ_rows and all(r > my_row for r in succ_rows):
            section.direction = "TB"
            continue

        # Default: LR
        section.direction = "LR"


def _flow_aligned_sides(direction: str) -> tuple[PortSide, PortSide]:
    """Return ``(entry_side, exit_side)`` aligned with a section's flow.

    A left-to-right section enters on the LEFT and exits on the RIGHT; a
    right-to-left section is mirrored.  Horizontal-flow sections always
    present flow-aligned ports regardless of where their neighbours sit
    on the grid; the inter-section router carriage-returns the connection
    (right -> down -> left -> down -> right) for a same-column stack.
    """
    if direction == "RL":
        return PortSide.RIGHT, PortSide.LEFT
    return PortSide.LEFT, PortSide.RIGHT


def _feeds_from_vertical_drop_above(
    graph: MetroGraph,
    sec_id: str,
    my_row: int,
    predecessors: dict[str, set[str]],
    fold_sections: set[str],
) -> bool:
    """True when a vertically-exiting predecessor (a TB or fold section) sits above.

    A TB/fold section exits BOTTOM and drops straight down, so the section it
    feeds should accept that drop on its TOP edge; forcing a flow-aligned side
    there bends the vertical drop into a diagonal.  Horizontal-flow (LR/RL)
    predecessors instead exit sideways and wrap to a flow-aligned entry
    (a carriage-return chain or an around-section wrap), so they don't block it.
    """
    for src in predecessors.get(sec_id, set()):
        src_sec = graph.sections.get(src)
        if not src_sec:
            continue
        _src_col, src_row, src_row_span, _src_col_span = _effective_grid_pos(graph, src)
        is_vertical = src_sec.direction == "TB" or src in fold_sections
        if is_vertical and (src_row + src_row_span - 1) < my_row:
            return True
    return False


def _infer_port_sides(
    graph: MetroGraph,
    successors: dict[str, set[str]],
    predecessors: dict[str, set[str]],
    edge_lines: dict[tuple[str, str], set[str]],
    fold_sections: set[str],
    convergence_sections: set[str] | None = None,
) -> None:
    """Infer entry/exit port sides from section flow and grid positions.

    Horizontal-flow (LR/RL) sections present flow-aligned ports: entry on
    the leading edge, exit on the trailing edge, regardless of neighbour
    position.  A same-column stack of LR sections therefore connects via a
    carriage-return wrap rather than a TOP/BOTTOM hop.

    Fold sections (TB bridges) get entry LEFT, exit BOTTOM.
    Remaining TB sections use _relative_side to derive sides from grid
    positions; convergence return-row sections get TOP entry for above-row
    predecessors.
    """
    if convergence_sections is None:
        convergence_sections = set()
    for sec_id, section in graph.sections.items():
        my_col, my_row, _row_span, my_col_span = _effective_grid_pos(graph, sec_id)
        horizontal = section.direction in ("LR", "RL") and sec_id not in fold_sections
        entry_aligned, exit_aligned = _flow_aligned_sides(section.direction)

        # A horizontal section exits flow-aligned (LR -> RIGHT, RL -> LEFT)
        # regardless of neighbour position; the router carriage-returns.
        flow_aligned_exit = horizontal

        # Entry is flow-aligned unless a vertically-exiting (TB/fold)
        # predecessor sits above: that drops straight down, so the
        # relative-side branch's TOP entry reads cleaner than a wrap.
        flow_aligned_entry = horizontal and not _feeds_from_vertical_drop_above(
            graph, sec_id, my_row, predecessors, fold_sections
        )

        # Infer exit hints (only if section has no explicit exit_hints)
        if not section.exit_hints and sec_id in successors:
            all_exit_lines: set[str] = set()
            for tgt in successors[sec_id]:
                lines = edge_lines.get((sec_id, tgt), set())
                all_exit_lines.update(lines)

            if all_exit_lines:
                if sec_id in fold_sections:
                    exit_side = _compute_fold_exit_side(
                        graph, sec_id, successors, edge_lines
                    )
                    section.exit_hints.append((exit_side, sorted(all_exit_lines)))
                elif flow_aligned_exit:
                    section.exit_hints.append((exit_aligned, sorted(all_exit_lines)))
                else:
                    _compute_exit_hints_by_side(graph, sec_id, successors, edge_lines)

        # Infer entry hints (only if section has no explicit entry_hints)
        if not section.entry_hints and sec_id in predecessors:
            all_entry_lines: set[str] = set()
            for src in predecessors[sec_id]:
                all_entry_lines.update(edge_lines.get((src, sec_id), set()))

            if flow_aligned_entry:
                if all_entry_lines:
                    section.entry_hints.append((entry_aligned, sorted(all_entry_lines)))
            else:
                side_lines: dict[PortSide, set[str]] = defaultdict(set)
                for src in predecessors[sec_id]:
                    src_sec = graph.sections.get(src)
                    if not src_sec or src not in graph.grid_overrides:
                        continue
                    src_col, src_row, src_row_span, src_col_span = _effective_grid_pos(
                        graph, src
                    )
                    lines = edge_lines.get((src, sec_id), set())
                    # Convergence return-row sections: predecessors on a row
                    # above should use TOP entry for clean vertical
                    # connections.
                    src_bottom_row = src_row + src_row_span - 1
                    if sec_id in convergence_sections and src_bottom_row < my_row:
                        side = PortSide.TOP
                    else:
                        side = _relative_side(
                            my_col,
                            my_row,
                            src_col,
                            src_row,
                            my_col_span,
                            src_col_span,
                        )
                    side_lines[side].update(lines)

                for side, lines in sorted(side_lines.items(), key=lambda x: x[0].value):
                    if lines:
                        section.entry_hints.append((side, sorted(lines)))


def _compute_fold_exit_side(
    graph: MetroGraph,
    sec_id: str,
    successors: dict[str, set[str]],
    edge_lines: dict[tuple[str, str], set[str]],
) -> PortSide:
    """Compute exit side for a fold section from successor positions.

    Post-fold successors are typically to the left (return row), so the
    exit is LEFT. For multi-row spans where all successors are below,
    uses BOTTOM so lines continue their vertical flow.
    """
    my_col, my_row, my_row_span, my_col_span = _effective_grid_pos(graph, sec_id)

    side_votes: dict[PortSide, int] = defaultdict(int)
    for tgt in successors.get(sec_id, set()):
        tgt_sec = graph.sections.get(tgt)
        if not tgt_sec:
            continue
        tgt_col, tgt_row, _tgt_row_span, tgt_col_span = _effective_grid_pos(graph, tgt)
        lines = edge_lines.get((sec_id, tgt), set())
        side = _relative_side(
            my_col,
            my_row,
            tgt_col,
            tgt_row,
            my_col_span,
            tgt_col_span,
        )
        side_votes[side] += len(lines)

    if not side_votes:
        return PortSide.BOTTOM

    dominant = max(side_votes, key=lambda s: side_votes[s])

    # Override for multi-row spans: if all successors are below the fold
    # span, use BOTTOM exit so lines continue their vertical flow.
    if my_row_span > 1:
        fold_bottom_row = my_row + my_row_span - 1
        all_below = all(
            _effective_grid_pos(graph, tgt)[1] > fold_bottom_row
            for tgt in successors.get(sec_id, set())
            if tgt in graph.sections
        )
        if all_below and dominant in (PortSide.LEFT, PortSide.RIGHT):
            dominant = PortSide.BOTTOM

    return dominant


def _compute_exit_hints_by_side(
    graph: MetroGraph,
    sec_id: str,
    successors: dict[str, set[str]],
    edge_lines: dict[tuple[str, str], set[str]],
) -> None:
    """Compute exit hints grouped by the side toward each target section.

    Creates one exit hint per side, collecting all lines that exit
    toward that side.
    """
    my_col, my_row, _my_row_span, my_col_span = _effective_grid_pos(graph, sec_id)

    side_exit_lines: dict[PortSide, set[str]] = defaultdict(set)
    for tgt in successors.get(sec_id, set()):
        tgt_sec = graph.sections.get(tgt)
        if not tgt_sec or tgt not in graph.grid_overrides:
            continue
        tgt_col, tgt_row, _tgt_row_span, tgt_col_span = _effective_grid_pos(graph, tgt)
        lines = edge_lines.get((sec_id, tgt), set())
        side = _relative_side(
            my_col,
            my_row,
            tgt_col,
            tgt_row,
            my_col_span,
            tgt_col_span,
        )
        side_exit_lines[side].update(lines)

    section = graph.sections[sec_id]
    if side_exit_lines:
        for side, lines in sorted(side_exit_lines.items(), key=lambda x: x[0].value):
            if lines:
                section.exit_hints.append((side, sorted(lines)))


def _relative_side(
    my_col: int,
    my_row: int,
    other_col: int,
    other_row: int,
    my_col_span: int = 1,
    other_col_span: int = 1,
) -> PortSide:
    """Determine which side of 'my' section faces 'other' section.

    Horizontal (LEFT/RIGHT) is preferred when sections are in different
    columns, since pipeline flow is primarily horizontal. Vertical
    (TOP/BOTTOM) is used when sections share a column or when their
    column spans overlap (e.g. a colspan-2 section sitting below a
    section that occupies one of its spanned columns).
    """
    # Check if column ranges overlap (accounts for colspan)
    my_col_end = my_col + my_col_span - 1
    other_col_end = other_col + other_col_span - 1
    cols_overlap = my_col <= other_col_end and other_col <= my_col_end

    if not cols_overlap:
        # No column overlap: prefer horizontal direction (pipeline flow)
        dcol = other_col - my_col
        if dcol > 0:
            return PortSide.RIGHT
        elif dcol < 0:
            return PortSide.LEFT

    # Columns overlap (or same column): use vertical direction
    drow = other_row - my_row
    if drow > 0:
        return PortSide.BOTTOM
    elif drow < 0:
        return PortSide.TOP

    return PortSide.RIGHT  # same position, default right


def _effective_grid_pos(graph: MetroGraph, sec_id: str) -> tuple[int, int, int, int]:
    """Return (col, row, row_span, col_span) for a section during inference.

    Explicit grid overrides are applied to ``section.grid_col`` only later in
    section_placement, so during auto-layout they read -1.  Prefer the value
    recorded in ``graph.grid_overrides`` when present.
    """
    if sec_id in graph.grid_overrides:
        return graph.grid_overrides[sec_id]
    section = graph.sections[sec_id]
    return (
        section.grid_col,
        section.grid_row,
        section.grid_row_span,
        section.grid_col_span,
    )


def detect_serpentine_runs(
    graph: MetroGraph,
    successors: dict[str, set[str]],
    predecessors: dict[str, set[str]],
) -> list[list[str]]:
    """Find runs of vertically-stacked single-cell sections forming a chain.

    A serpentine run is a maximal sequence of sections that

    * occupy a single grid cell (row_span == col_span == 1),
    * share a grid column,
    * sit on consecutive grid rows, and
    * form a simple chain: the section on row N feeds exactly one section
      in the same column (the one on row N+1), and that lower section is
      fed from the column only by the upper one.

    Returns runs as lists of section ids ordered top-to-bottom.  Runs of
    length < 2 are omitted.  Used to serpentine the effective flow
    direction of stacked same-direction sections.
    """
    # Group single-cell sections by grid column, keyed by row.  A column with
    # two sections claiming one (col, row) cell is malformed and is skipped.
    col_rows: dict[int, dict[int, str]] = defaultdict(dict)
    collided_cols: set[int] = set()
    for sec_id in graph.sections:
        col, row, row_span, col_span = _effective_grid_pos(graph, sec_id)
        if col < 0 or row < 0 or row_span != 1 or col_span != 1:
            continue
        if row in col_rows[col]:
            collided_cols.add(col)
        col_rows[col][row] = sec_id

    runs: list[list[str]] = []
    for col, rows in col_rows.items():
        if col in collided_cols:
            continue
        ordered_rows = sorted(rows)
        in_column = set(rows.values())
        i = 0
        while i < len(ordered_rows):
            run = [rows[ordered_rows[i]]]
            j = i
            while j + 1 < len(ordered_rows):
                upper_row = ordered_rows[j]
                lower_row = ordered_rows[j + 1]
                if lower_row != upper_row + 1:
                    break
                upper = rows[upper_row]
                lower = rows[lower_row]
                # upper must feed lower, and lower must be the only in-column
                # successor of upper (a clean chain, not a fan-out).
                upper_col_succ = successors.get(upper, set()) & in_column
                lower_col_pred = predecessors.get(lower, set()) & in_column
                if (
                    lower in successors.get(upper, set())
                    and upper_col_succ == {lower}
                    and lower_col_pred == {upper}
                ):
                    run.append(lower)
                    j += 1
                else:
                    break
            if len(run) >= 2:
                runs.append(run)
            i = j + 1
    return runs
