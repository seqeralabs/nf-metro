"""Auto-layout: infer section grid positions, directions, and port sides.

Runs BEFORE _resolve_sections() in the parser. Scans inter-section edges
(by comparing station.section_id) and fills in missing grid_overrides,
section.direction, section.entry_hints, and section.exit_hints.

Preserves any values explicitly set by %%metro directives.
"""

from __future__ import annotations

__all__ = ["infer_section_layout", "infer_interchanges", "detect_serpentine_runs"]

from collections import defaultdict, deque
from collections.abc import Callable
from collections.abc import Set as AbstractSet

from nf_metro.layout.geometry import AxisFrame
from nf_metro.layout.layers import assign_layers
from nf_metro.parser.model import (
    Interchange,
    MetroGraph,
    PortSide,
    Section,
    SectionDAG,
)
from nf_metro.parser.validate import CyclicGraphError


def infer_interchanges(graph: MetroGraph) -> None:
    """Auto-detect cross-track interchanges, appending to ``graph.interchanges``.

    A station qualifies only when its lines are fully *parallel lanes*: every
    line through it has exactly one predecessor and one successor, and the
    predecessors are all distinct and the successors are all distinct.  Then the
    lines never share a track around the station, so converging them onto its
    single point is pure visual cost the interchange removes -- each line keeps
    its own rail straight through.

    The distinctness test deliberately abstains when any two lines share a
    neighbour (e.g. two callers that both feed one merge): there the convergence
    is doing real work and an interchange would only defer it, so those want the
    explicit ``%%metro interchange:`` directive (or nothing).  Author-written
    interchanges are left untouched.
    """
    explicit = [ic.node_id for ic in graph.interchanges]
    explicit_set = set(explicit)
    candidates: list[str] = []
    for sid, st in list(graph.stations.items()):
        if st.is_port or st.interchange_id is not None or st.is_terminus:
            continue
        if sid in explicit_set:
            continue
        # Rail sections already lay every line on its own rail and draw shared
        # stops as interchanges, so one here is redundant and would clash with
        # rail rendering/layout.
        if graph.is_rail_section(st.section_id):
            continue
        if not _is_parallel_lane_hub(graph, sid):
            continue
        candidates.append(sid)

    order = list(graph.lines.keys())

    # Author-pinned interchanges must keep their span clear under any reorder we
    # try; collect them so a track shuffle for an inferred hub can never push a
    # non-member rail under an explicit bar.
    committed = list(explicit)
    inferred: list[str] = []
    pending: list[str] = []
    for sid in candidates:
        if _rails_span_is_clear(graph, sid, order):
            committed.append(sid)
            inferred.append(sid)
        else:
            pending.append(sid)

    # A straddle is an artifact of lane order, which is free unless the author
    # fixed it with %%metro line_order:.  For each abstaining hub, try to pull
    # its member lines into a contiguous block (the minimal disturbance that
    # clears the bar) and keep the reorder only if every already-committed
    # interchange -- explicit or inferred -- stays clear under it.
    if pending and graph.line_order == "definition":
        layers = assign_layers(graph)
        for sid in pending:
            # Line order only governs the bar span when every member rail runs
            # straight through the hub.  A member line that enters or leaves via
            # a long edge (a neighbour more than one layer away) slopes off its
            # base track, so a contiguous lane order would not actually close the
            # bar -- reordering would only manufacture a straddle the runtime
            # check then trips on.  Skip those; abstaining stays correct.
            if not _member_lines_pass_straight(graph, sid, layers):
                continue
            new_order = _cluster_member_lines(graph, sid, order)
            if new_order is None or not _rails_span_is_clear(graph, sid, new_order):
                continue
            if not all(
                _rails_span_is_clear(graph, other, new_order) for other in committed
            ):
                continue
            order = new_order
            committed.append(sid)
            inferred.append(sid)

    if order != list(graph.lines.keys()):
        graph.lines = {lid: graph.lines[lid] for lid in order}

    for sid in inferred:
        lines = graph.station_lines(sid)
        ordered = [lid for lid in order if lid in lines]
        graph.interchanges.append(
            Interchange(node_id=sid, rails=[[lid] for lid in ordered], inferred=True)
        )


def _is_parallel_lane_hub(graph: MetroGraph, sid: str) -> bool:
    """True when every line through *sid* is its own parallel lane.

    Each line must enter from one predecessor and leave to one successor, all
    in *sid*'s section (no port = no boundary crossing), with the predecessors
    mutually distinct and the successors mutually distinct.  Two lines sharing a
    neighbour means they genuinely converge here, so this returns False.
    """
    lines = graph.station_lines(sid)
    if len(lines) < 2:
        return False
    section_id = graph.stations[sid].section_id
    ins = graph.edges_to(sid)
    outs = graph.edges_from(sid)
    preds: set[str] = set()
    succs: set[str] = set()
    for lid in lines:
        li = [e for e in ins if e.line_id == lid]
        lo = [e for e in outs if e.line_id == lid]
        if len(li) != 1 or len(lo) != 1:
            return False
        pst = graph.stations.get(li[0].source)
        sst = graph.stations.get(lo[0].target)
        if pst is None or sst is None or pst.is_port or sst.is_port:
            return False
        if pst.section_id != section_id or sst.section_id != section_id:
            return False
        preds.add(pst.id)
        succs.add(sst.id)
    return len(preds) == len(lines) and len(succs) == len(lines)


def _rails_span_is_clear(graph: MetroGraph, sid: str, order: list[str]) -> bool:
    """True when the interchange's rails would form a contiguous track block.

    The connector bar spans from the topmost member rail to the bottommost, so
    if a non-member line's rail falls between them its stations sit under the
    bar (a station-as-elbow violation).  Track order follows *order* (the line
    ordering tracks will be assigned from) within a section, so require the
    member lines to be a contiguous run among the section's lines -- no other
    line interleaved.
    """
    section_id = graph.stations[sid].section_id
    section = graph.sections.get(section_id) if section_id is not None else None
    if section is None:
        return True
    present = {
        lid
        for mid in section.station_ids
        if (m := graph.stations.get(mid)) is not None and not m.is_port
        for lid in graph.station_lines(mid)
    }
    section_order = [lid for lid in order if lid in present]
    member_idx = sorted(section_order.index(lid) for lid in graph.station_lines(sid))
    return member_idx == list(range(member_idx[0], member_idx[-1] + 1))


def _member_lines_pass_straight(
    graph: MetroGraph, sid: str, layers: dict[str, int]
) -> bool:
    """True when every line through *sid* enters and leaves at adjacent layers.

    A parallel-lane hub's rails only sit on their line base tracks -- the
    assumption :func:`_rails_span_is_clear` makes when it reads lane order as
    track order -- while each line runs as a straight horizontal segment past
    the hub.  A line whose predecessor or successor is more than one layer away
    travels a sloping long edge instead, dragging its rail off that track, so
    lane order then fails to predict the real bar span.
    """
    hub_layer = layers.get(sid)
    if hub_layer is None:
        return False
    for lid in graph.station_lines(sid):
        for e in graph.edges_to(sid):
            if e.line_id == lid and layers.get(e.source) != hub_layer - 1:
                return False
        for e in graph.edges_from(sid):
            if e.line_id == lid and layers.get(e.target) != hub_layer + 1:
                return False
    return True


def _cluster_member_lines(
    graph: MetroGraph, sid: str, order: list[str]
) -> list[str] | None:
    """Return *order* with *sid*'s member lines pulled into a contiguous block.

    The block keeps the members' relative order and is seated where the first
    member currently sits, so every non-member lane keeps its relative position
    (the smallest reshuffle that closes the straddle).  Returns ``None`` when
    the node carries fewer than two lines (nothing to bridge).
    """
    members = graph.station_lines(sid)
    member_block = [lid for lid in order if lid in members]
    if len(member_block) < 2:
        return None
    first_idx = order.index(member_block[0])
    rest = [lid for lid in order if lid not in members]
    n_before = sum(1 for lid in order[:first_idx] if lid not in members)
    return rest[:n_before] + member_block + rest[n_before:]


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


def _internal_station_depths(
    graph: MetroGraph, section_id: str
) -> dict[str, int] | None:
    """Longest-path depth of each station through a section's internal edges.

    Returns ``None`` when the section has no internal edges, so callers fall
    back to a station count for the size estimate.

    The graph is assumed acyclic: cyclic graphs are rejected by
    ``compute_layout`` and reported by ``validate_graph`` before layout
    inference reaches this estimator. A section whose internal edges form a
    cycle would leave no root to rank from, so that case raises loudly rather
    than falling back to a distorted station-count estimate.
    """
    section = graph.sections[section_id]
    station_ids = set(section.station_ids)

    adj: dict[str, set[str]] = defaultdict(set)
    has_pred: set[str] = set()
    for edge in graph.edges:
        if edge.source in station_ids and edge.target in station_ids:
            adj[edge.source].add(edge.target)
            has_pred.add(edge.target)

    if not adj:
        return None
    roots = station_ids - has_pred
    if not roots:
        raise CyclicGraphError(
            f"Section '{section_id}' has cyclic internal edges: "
            f"{', '.join(sorted(station_ids))}"
        )

    depth: dict[str, int] = {sid: 0 for sid in station_ids}
    queue: deque[str] = deque(roots)
    visited: set[str] = set()
    while queue:
        node = queue.popleft()
        if node in visited:
            continue
        visited.add(node)
        for succ in adj.get(node, set()):
            if depth[node] + 1 > depth[succ]:
                depth[succ] = depth[node] + 1
            queue.append(succ)
    return depth


def _estimate_section_layers(graph: MetroGraph, section_id: str) -> int:
    """Estimate the number of station layers (horizontal span) for a section.

    Computes the longest path through internal edges via topological DP.
    Returns at least 1.
    """
    depth = _internal_station_depths(graph, section_id)
    if depth is None:
        return max(len(graph.sections[section_id].station_ids), 1)
    return max(depth.values()) + 1  # +1: convert 0-indexed depth to layer count


def _estimate_section_height(graph: MetroGraph, section_id: str) -> int:
    """Estimate a section's vertical extent as its widest internal layer.

    Counts the stations sharing the busiest longest-path depth (the broadest
    fan), the orthogonal counterpart to ``_estimate_section_layers``. Returns
    at least 1.
    """
    depth = _internal_station_depths(graph, section_id)
    if depth is None:
        return max(len(graph.sections[section_id].station_ids), 1)
    per_layer: dict[int, int] = defaultdict(int)
    for d in depth.values():
        per_layer[d] += 1
    return max(per_layer.values())


# A section qualifies as a tall anchor when its fan is at least this many
# stations deep and at least this many times taller than it is wide. The fan
# then has enough vertical room to host the downstream chain stacked beside it.
TALL_ANCHOR_MIN_HEIGHT = 10
TALL_ANCHOR_ASPECT_RATIO = 2.0


def _detect_tall_anchor_chain(graph: MetroGraph) -> str | None:
    """Return the section to anchor a vertical-stack layout, or ``None``.

    Qualifies when the section meta-graph is a single-source/single-sink chain
    whose unique tallest section is a dominant tall-narrow fan (a mid-spine
    section with both upstream and downstream neighbours). The narrow tail
    downstream of that anchor can then stack vertically in its shadow.
    """
    dag = graph.section_dag
    if dag is None or len(graph.sections) < 3:
        return None
    # A single explicit pin is manual layout intent for the whole map; the
    # vertical-stack packer owns every section's cell, so it must not run
    # alongside hand-placed grids.
    if graph._explicit_grid:
        return None
    successors, predecessors = dag.successors, dag.predecessors
    section_ids = list(graph.sections)

    sources = [s for s in section_ids if not predecessors.get(s)]
    sinks = [s for s in section_ids if not successors.get(s)]
    if len(sources) != 1 or len(sinks) != 1:
        return None

    heights = {s: _estimate_section_height(graph, s) for s in section_ids}
    widths = {s: _estimate_section_layers(graph, s) for s in section_ids}
    anchor = max(section_ids, key=lambda s: heights[s])

    if heights[anchor] < TALL_ANCHOR_MIN_HEIGHT:
        return None
    if heights[anchor] < TALL_ANCHOR_ASPECT_RATIO * widths[anchor]:
        return None
    # A unique tall section: a second comparably tall fan would want its own
    # column, which the single-anchor stack cannot express.
    if any(s != anchor and heights[s] >= TALL_ANCHOR_MIN_HEIGHT for s in section_ids):
        return None
    # The anchor must sit mid-spine: a non-empty head (upstream) to form the
    # header row and a non-empty tail (downstream) to stack beside it.
    if not predecessors.get(anchor) or not successors.get(anchor):
        return None

    return anchor


def _place_with_tall_anchor(
    graph: MetroGraph,
    col_assign: dict[str, int],
    anchor: str,
) -> tuple[set[str], set[str], set[str]]:
    """Place a tall-anchor chain: header row, tall anchor, stacked tail.

    The sections upstream of the anchor occupy row 0 (a header that spans the
    grid width); the anchor sits below in column 0 spanning the tail's rows;
    the downstream sections stack vertically in column 1, ordered by topo
    column. No TB bridge is created -- every section keeps horizontal flow and
    the inter-section router carriage-returns each stacked connection.
    """
    anchor_col = col_assign[anchor]
    head = sorted(
        (s for s in col_assign if col_assign[s] < anchor_col),
        key=lambda s: col_assign[s],
    )
    tail = sorted(
        (s for s in col_assign if col_assign[s] > anchor_col),
        key=lambda s: col_assign[s],
    )

    folded: dict[str, tuple[int, int, int, int]] = {}

    # Header row: one section per upstream column, left to right.
    for col, sid in enumerate(head):
        folded[sid] = (col, 0, 1, 1)
    grid_width = max(len(head), 2)

    # Tail stacks in the last column on rows below the header.
    tail_col = grid_width - 1
    for row, sid in enumerate(tail, start=1):
        folded[sid] = (tail_col, row, 1, 1)

    # Anchor sits in column 0, spanning every tail row.
    folded[anchor] = (0, 1, max(len(tail), 1), 1)

    # A lone header section spans the full width so its column is not inflated
    # by the wide upstream block (the narrow anchor/tail columns set the width).
    if len(head) == 1:
        col, row, _rspan, _cspan = folded[head[0]]
        folded[head[0]] = (col, row, 1, grid_width)

    for sid, (col, row, rspan, cspan) in folded.items():
        graph.grid_overrides[sid] = (col, row, rspan, cspan)
        section = graph.sections[sid]
        section.grid_col = col
        section.grid_row = row
        section.grid_row_span = rspan
        section.grid_col_span = cspan

    # Pin the anchor and its stacked tail to horizontal flow: each connects to
    # the section below via a carriage-return wrap, so direction inference must
    # not reorient a single-successor-below section to TB (which would force a
    # perpendicular TOP entry on the LR sink). Registering them as fixed
    # directions makes _infer_directions leave them LR.
    for sid in [anchor, *tail]:
        graph.sections[sid].direction = "LR"
        graph._explicit_directions.add(sid)

    return set(), set(), set()


def _bfs_section_columns(
    section_ids: list[str], successors: dict[str, set[str]]
) -> dict[str, int]:
    """Longest-path topo column per section (disconnected sections get 0)."""
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

    for sid in section_ids:
        if sid not in col_assign:
            col_assign[sid] = 0
    return col_assign


def _pack_topo_columns(
    col_groups: dict[int, list[str]],
    topo_col_width: dict[int, int],
    successors: dict[str, set[str]],
    max_station_columns: int,
) -> tuple[dict[str, tuple[int, int]], set[str], set[str]]:
    """Greedily pack topo columns into serpentine row bands.

    When a column would overflow the current row it becomes a fold section
    (TB bridge) at the right edge, and subsequent columns start a new row band
    below flowing in the opposite direction.  Returns ``(folded, fold_sections,
    below_fold_sections)``.
    """
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

        # Folding a branch column (several sections in one topo column) makes
        # one member a TB bridge and strands the others' successors on the
        # backward-flowing return row, behind their producers. Defer such a
        # fold to the next spine column (accepting a wider row) unless every
        # member's successor drops straight below the bridge.
        if need_fold and stack_size > 1:
            next_sids = _next_col_sids(col_groups, sorted_cols, topo_idx)
            if not _below_fold_drop_applies(sids, successors, next_sids):
                need_fold = False

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

            _maybe_place_below_fold(
                col_groups,
                successors,
                sorted_cols,
                topo_idx,
                sids,
                fold_col,
                band_start_row,
                band_height,
                folded,
                below_fold_sections,
                skip_topo_cols,
            )
        else:
            # Normal placement in current band
            for i, sid in enumerate(sids):
                folded[sid] = (current_grid_col, band_start_row + i)
            max_stack_in_band = max(max_stack_in_band, stack_size)
            current_grid_col += col_step
            cumulative_width += w

    return folded, fold_sections, below_fold_sections


def _next_col_sids(
    col_groups: dict[int, list[str]],
    sorted_cols: list[int],
    topo_idx: int,
) -> list[str]:
    """Sections in the topo column after ``topo_idx`` (empty past the last)."""
    nxt = topo_idx + 1
    return col_groups[sorted_cols[nxt]] if nxt < len(sorted_cols) else []


def _below_fold_drop_applies(
    sids: list[str],
    successors: dict[str, set[str]],
    next_sids: list[str],
) -> bool:
    """True when a fold's sections can drop their successors straight below it.

    Every fold section must have exactly one successor and those successors
    together must be exactly the next topo column, so they seat in the fold
    column under the bridge rather than on a backward-flowing return row.
    """
    fold_succs: set[str] = set()
    for fs in sids:
        fs_succs = successors.get(fs, set())
        if len(fs_succs) != 1:
            return False
        fold_succs.update(fs_succs)
    return bool(next_sids) and fold_succs == set(next_sids)


def _maybe_place_below_fold(
    col_groups: dict[int, list[str]],
    successors: dict[str, set[str]],
    sorted_cols: list[int],
    topo_idx: int,
    sids: list[str],
    fold_col: int,
    band_start_row: int,
    band_height: int,
    folded: dict[str, tuple[int, int]],
    below_fold_sections: set[str],
    skip_topo_cols: set[int],
) -> None:
    """Place a fold's single successors directly below it instead of on a return row.

    When the band has stacked sections (``band_height > 1``) a return row would
    route backward over that content.  If every fold section has exactly one
    successor and those successors are the only sections in the next topo
    column, seat them in the fold column below the bridge.
    """
    if band_height <= 1 or topo_idx + 1 >= len(sorted_cols):
        return
    next_sids = _next_col_sids(col_groups, sorted_cols, topo_idx)
    if _below_fold_drop_applies(sids, successors, next_sids):
        for j, ns in enumerate(next_sids):
            folded[ns] = (fold_col, band_start_row + j)
            below_fold_sections.add(ns)
        skip_topo_cols.add(sorted_cols[topo_idx + 1])
        # Don't increment band_start_row: below-fold sections are in the fold
        # column, so return-row sections (in adjacent columns) share the rows.


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

    col_assign = _bfs_section_columns(section_ids, successors)

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

    # --- Tall-anchor vertical stack ---
    # Stack the narrow downstream chain in the shadow of one dominant tall-
    # narrow fan instead of giving every section its own topological column.
    anchor = _detect_tall_anchor_chain(graph)
    if anchor is not None:
        return _place_with_tall_anchor(graph, col_assign, anchor)

    # --- Convergence-based row split ---
    # Detect sections with predecessors spanning 2+ non-adjacent topo columns.
    # These are natural "convergence points" that should drop to a return row
    # along with their downstream sections, instead of extending the top row.
    #
    # A purely linear spine (one section per topo column) that fits the fold
    # threshold stays unsplit: splitting it only bends the flow into a
    # backward-reading serpentine for no readability gain.
    single_row_width = sum(topo_col_width.values())
    is_linear_spine = all(len(sids) == 1 for sids in col_groups.values())
    chain_fits_one_row = is_linear_spine and single_row_width <= max_station_columns
    if not chain_fits_one_row:
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

    folded, fold_sections, below_fold_sections = _pack_topo_columns(
        col_groups, topo_col_width, successors, max_station_columns
    )

    # Boustrophedon normalization: a return row built by stepping left from
    # the fold bridge can run off the left edge into negative columns. Shift
    # the whole grid right so the leftmost column is 0, keeping every
    # section's relative position (and thus the serpentine read order).
    if folded:
        min_col = min(col for col, _ in folded.values())
        if min_col < 0:
            folded = {sid: (col - min_col, row) for sid, (col, row) in folded.items()}

    # A chain judged to fit one row must not have folded: the packer's
    # overflow test uses the same threshold, so a multi-row result means the
    # fit decision and the packer disagree.
    if chain_fits_one_row and any(row != 0 for _, row in folded.values()):
        from nf_metro.layout.phases.guards import PhaseInvariantError

        rows = sorted({row for _, row in folded.values()})
        raise PhaseInvariantError(
            f"linear section spine fits one row (width {single_row_width} "
            f"<= {max_station_columns}) but packed across rows {rows}"
        )

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

    # Migration below tests membership against this frozen base, never the set
    # it is growing: ``col_assign``'s key order derives from a BFS over
    # set-valued successor maps and so varies with ``PYTHONHASHSEED``, so an
    # order-dependent inclusion would make grid placement hash-seed dependent.
    base_return_set = frozenset(
        {convergence_sid} | _transitive_successors(convergence_sid, successors)
    )
    return_set = set(base_return_set)

    # Both migrations only ever pull in a section that feeds ONLY into the base
    # return set; they differ in the second test they then apply.
    candidates = [
        sid
        for sid in sorted(col_assign)
        if sid not in base_return_set
        and successors.get(sid, set())
        and successors[sid].issubset(base_return_set)
    ]

    # Companion migration: sections that feed ONLY into the return set
    # AND share a direct predecessor with the convergence section.
    # This catches "satellite" branches (e.g. ensembl_truth, which feeds
    # only into benchmarking, and shares predecessor "filtering" with it).
    # It avoids pulling in main-spine sections (e.g. in A->B->C->D with
    # bypass A->D, sec_c feeds sec_d but sec_c's predecessor sec_b does
    # NOT directly feed sec_d).
    convergence_preds = predecessors.get(convergence_sid, set())
    for sid in candidates:
        if predecessors.get(sid, set()) & convergence_preds:
            return_set.add(sid)

    # Stacked-sibling migration: a section that feeds ONLY into the return
    # set and would otherwise be a lone stacked sibling in the row-0 spine
    # band (its topo column also holds a section that stays in row 0) is
    # pulled onto the return row. Leaving it stacked forces an extra spine
    # row occupied by a single small section, with a large empty band beside
    # it (issue #484: tr_calling stacked under small_variants).
    for sid in candidates:
        topo_col = col_assign[sid]
        has_spine_sibling = any(
            other != sid
            and col_assign.get(other) == topo_col
            and other not in base_return_set
            for other in col_groups.get(topo_col, [])
        )
        if has_spine_sibling:
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

    # Place row-0 sections (non-return) left to right. A topo column with
    # multiple spine sections stacks them downward (rows 0, 1, ...), so track
    # the tallest stack to know which row the return band must clear.
    grid_col = 0
    max_row0_col = 0
    row0_height = 1
    for topo_col in sorted_cols:
        row0_sids = [s for s in col_groups[topo_col] if s not in return_set]
        if not row0_sids:
            continue
        for i, sid in enumerate(row0_sids):
            folded[sid] = (grid_col, i)
        row0_height = max(row0_height, len(row0_sids))
        max_row0_col = grid_col
        grid_col += 1

    # Place return-set sections right to left on the first row clear of the
    # row-0 stack. Hardcoding row 1 collided with a stacked spine sibling
    # whenever a row-0 column held 2+ sections (issue #484).
    return_row = row0_height
    # Sort by topo col ascending: lowest topo col = rightmost on return row
    return_sids = sorted(return_set, key=lambda s: col_assign[s])
    grid_col = max_row0_col
    for sid in return_sids:
        folded[sid] = (grid_col, return_row)
        grid_col -= 1

    # Boustrophedon normalization: a return row longer than the row-0 spine
    # steps past column 0 into negative columns. Shift the whole grid right
    # so the leftmost column is 0, preserving every section's relative
    # position (mirrors the serpentine packer; issue #484).
    if folded:
        min_col = min(col for col, _ in folded.values())
        if min_col < 0:
            folded = {sid: (col - min_col, row) for sid, (col, row) in folded.items()}

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
    below_fold_sections: AbstractSet[str] = frozenset(),
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
    below_fold_sections: AbstractSet[str] = frozenset(),
) -> None:
    """Infer section flow direction (LR/RL/TB) from grid positions.

    Only modifies sections NOT in graph._explicit_directions.
    Fold sections are forced to TB (they bridge between row bands).
    Sections whose predecessors are all to the right get RL.

    Explicitly-gridded sections are skipped: their grid position is manual
    layout intent, not a flow-direction signal (#446). They also still read
    grid_col == -1 here (the override is applied later in section_placement),
    so without the skip the -1 fires spurious RL/TB against auto-placed
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

        # TB: vertical bridge/serpentine turn. Only when every successor sits
        # in the row directly below the section's bottom and no further right
        # than the adjacent column. Successors to the left or directly below
        # are a same-column drop or a return-row turn that TB serves cleanly;
        # a successor down-and-to-the-right is instead reached by a flow-aligned
        # exit plus an inter-section across+down L-shape, so the section stays
        # horizontal. Successors a row gap away are likewise left to LR + drop.
        my_bottom = my_row + section.grid_row_span - 1
        if succ_rows and all(
            sr == my_bottom + 1 and sc <= my_col + 1
            for sc, sr in zip(succ_cols, succ_rows)
        ):
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


def _has_predecessor_above(
    graph: MetroGraph,
    sec_id: str,
    my_row: int,
    predecessors: dict[str, set[str]],
    predicate: Callable[[str, Section], bool] | None = None,
) -> bool:
    """True when a predecessor's bottom row sits above ``my_row``.

    ``predicate`` optionally restricts the test to predecessors it accepts,
    receiving each ``(src_id, src_section)`` pair.
    """
    for src in predecessors.get(sec_id, set()):
        src_sec = graph.sections.get(src)
        if not src_sec:
            continue
        _src_col, src_row, src_row_span, _src_col_span = _effective_grid_pos(graph, src)
        if (src_row + src_row_span - 1) < my_row and (
            predicate is None or predicate(src, src_sec)
        ):
            return True
    return False


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
    return _has_predecessor_above(
        graph,
        sec_id,
        my_row,
        predecessors,
        lambda src, src_sec: src_sec.direction == "TB" or src in fold_sections,
    )


def _is_foldback_drop_corner(
    graph: MetroGraph,
    sec_id: str,
    my_row: int,
    predecessors: dict[str, set[str]],
) -> bool:
    """True for a horizontal fold corner whose flow-aligned entry hits its exit.

    On an explicit-grid fold the return-row section keeps its flow-aligned
    orientation, so its leading-edge entry lands on the same side as its
    backward exit: the feed jogs into that port and immediately folds back out
    to reach the station.  When the section is fed from the row above, a
    straight vertical drop onto its TOP edge reads cleaner than the jog.  A
    genuine carriage-return wrap continues the flow (its exit is on the trailing
    edge), so its flow-aligned entry never collides and it is left untouched.

    The caller gates this on the section being horizontal (LR/RL, non-fold).
    """
    section = graph.sections[sec_id]
    entry_aligned, _exit_aligned = _flow_aligned_sides(section.direction)
    if not any(side == entry_aligned for side, _lines in section.exit_hints):
        return False
    return _has_predecessor_above(graph, sec_id, my_row, predecessors)


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
                    _infer_flow_exit_hints_with_drops(
                        graph, sec_id, successors, edge_lines, exit_aligned
                    )
                else:
                    _compute_exit_hints_by_side(graph, sec_id, successors, edge_lines)

        # Infer entry hints (only if section has no explicit entry_hints)
        if not section.entry_hints and sec_id in predecessors:
            all_entry_lines: set[str] = set()
            for src in predecessors[sec_id]:
                all_entry_lines.update(edge_lines.get((src, sec_id), set()))

            foldback_corner = horizontal and _is_foldback_drop_corner(
                graph, sec_id, my_row, predecessors
            )
            if foldback_corner:
                if all_entry_lines:
                    section.entry_hints.append((PortSide.TOP, sorted(all_entry_lines)))
            elif flow_aligned_entry:
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


def _flow_edge_sides(direction: str) -> tuple[PortSide, PortSide]:
    """``(flow-start side, flow-end side)`` for a section direction.

    Derived from the :class:`AxisFrame` primitive rather than a per-direction
    branch: the flow axis (X for LR/RL, Y for TB/BT) fixes the edge pair
    (LEFT/RIGHT or TOP/BOTTOM), and the flow sign fixes which is the start.
    """
    on_x = AxisFrame.axes_for_direction(direction)[0] == "x"
    low, high = (
        (PortSide.LEFT, PortSide.RIGHT) if on_x else (PortSide.TOP, PortSide.BOTTOM)
    )
    return (low, high) if AxisFrame.flow_sign(direction) > 0 else (high, low)


def _same_cell_side(graph: MetroGraph, sec_id: str, other: str) -> PortSide:
    """Side of ``sec_id`` that a grid-cell co-tenant ``other`` faces.

    Sections packed into one grid cell pack along the flow axis, so their order
    is the dataflow order: a co-tenant that feeds ``sec_id`` is upstream and
    sits on its flow-start edge (LEFT for a left-to-right cell); a co-tenant
    ``sec_id`` feeds sits on the flow-end edge.  Co-tenants the section DAG does
    not order keep the flow-end default, matching :func:`_relative_side`'s
    same-position fallback.

    A section with no internal flow (a pure fan target with no internal edges)
    has no flow-start edge for the co-tenant to align to, so the co-tenant keeps
    the same-position default and the section's entry follows its dominant
    external feed instead.
    """
    dag = graph.section_dag
    if dag is not None and graph.sections[sec_id].internal_edges:
        start, end = _flow_edge_sides(graph.sections[sec_id].direction)
        if other in dag.predecessors.get(sec_id, set()):
            return start
        if other in dag.successors.get(sec_id, set()):
            return end
    return PortSide.RIGHT


def _neighbour_side_votes(
    graph: MetroGraph,
    sec_id: str,
    neighbours: AbstractSet[str],
    edge_lines: dict[tuple[str, str], set[str]],
    *,
    edge_key: Callable[[str], tuple[str, str]],
    skip_unplaced: bool = False,
) -> dict[PortSide, int]:
    """Tally which side of ``sec_id`` each neighbour faces, weighted by lines.

    Each neighbour votes for the :func:`_relative_side` its grid cell falls on
    relative to ``sec_id``, weighted by the number of lines on the connecting
    edge (``edge_lines[edge_key(neighbour)]``).  ``edge_key`` orients the lookup
    for either feed direction: ``(neighbour, sec_id)`` for predecessors,
    ``(sec_id, neighbour)`` for successors.

    Neighbours absent from ``graph.sections`` are skipped.  ``skip_unplaced``
    additionally drops neighbours whose effective grid column is ``-1`` (an
    unassigned position during the parser phase), leaving the post-tally
    selection policy to each caller.
    """
    my_col, my_row, _my_row_span, my_col_span = _effective_grid_pos(graph, sec_id)

    votes: dict[PortSide, int] = defaultdict(int)
    for other in neighbours:
        if other not in graph.sections:
            continue
        other_col, other_row, _other_row_span, other_col_span = _effective_grid_pos(
            graph, other
        )
        if skip_unplaced and other_col < 0:
            continue
        if (other_col, other_row) == (my_col, my_row):
            side = _same_cell_side(graph, sec_id, other)
        else:
            side = _relative_side(
                my_col, my_row, other_col, other_row, my_col_span, other_col_span
            )
        votes[side] += len(edge_lines.get(edge_key(other), set()))
    return votes


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
    _my_col, my_row, my_row_span, _my_col_span = _effective_grid_pos(graph, sec_id)

    side_votes = _neighbour_side_votes(
        graph,
        sec_id,
        successors.get(sec_id, set()),
        edge_lines,
        edge_key=lambda tgt: (sec_id, tgt),
    )

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


def _infer_flow_exit_hints_with_drops(
    graph: MetroGraph,
    sec_id: str,
    successors: dict[str, set[str]],
    edge_lines: dict[tuple[str, str], set[str]],
    exit_aligned: PortSide,
) -> None:
    """Infer exit hints for a horizontal-flow section, dropping where natural.

    A line whose target is a TB/BT section stacked directly in an adjacent row
    of an overlapping column exits perpendicular (BOTTOM if below, TOP if
    above) so it drops straight into that section's TOP/BOTTOM entry instead of
    carriage-returning around.  Every other line keeps the flow-aligned exit.
    """
    my_col, my_row, my_row_span, my_col_span = _effective_grid_pos(graph, sec_id)
    my_bottom_row = my_row + my_row_span - 1

    drop_side_lines: dict[PortSide, set[str]] = defaultdict(set)
    flow_lines: set[str] = set()
    for tgt in successors.get(sec_id, set()):
        tgt_sec = graph.sections.get(tgt)
        lines = edge_lines.get((sec_id, tgt), set())
        side = None
        if tgt_sec is not None and tgt_sec.direction in ("TB", "BT"):
            tgt_col, tgt_row, _trs, tgt_col_span = _effective_grid_pos(graph, tgt)
            rel = _relative_side(
                my_col, my_row, tgt_col, tgt_row, my_col_span, tgt_col_span
            )
            if rel is PortSide.BOTTOM and tgt_row == my_bottom_row + 1:
                side = PortSide.BOTTOM
            elif rel is PortSide.TOP and tgt_row + 1 == my_row:
                side = PortSide.TOP
        if side is not None:
            drop_side_lines[side].update(lines)
        else:
            flow_lines.update(lines)

    section = graph.sections[sec_id]
    if flow_lines:
        section.exit_hints.append((exit_aligned, sorted(flow_lines)))
    for side, lines in sorted(drop_side_lines.items(), key=lambda x: x[0].value):
        if lines:
            section.exit_hints.append((side, sorted(lines)))


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
