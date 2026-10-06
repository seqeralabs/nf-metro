"""Whole-graph canvas translation into view and section renumbering by reading order."""

from __future__ import annotations

from collections.abc import Callable
from functools import cache

from nf_metro.layout.auto_layout import _transitive_successors
from nf_metro.layout.constants import (
    TITLE_BAND_CLEARANCE,
    TITLE_BAND_OVERLAP_FLOOR,
)
from nf_metro.layout.phases.bbox import (
    _min_drawn_section_bbox_top,
    _min_section_bbox_top,
)
from nf_metro.parser.model import MetroGraph


def _section_row_directions(
    graph: MetroGraph, section_edges: set[tuple[str, str]]
) -> dict[int, bool]:
    rows: dict[int, list[str]] = {}
    for sid, section in graph.sections.items():
        rows.setdefault(section.grid_row, []).append(sid)

    flow_scores = {row: 0 for row in rows}
    for source, target in section_edges:
        source_section = graph.sections[source]
        target_section = graph.sections[target]
        if source_section.grid_row == target_section.grid_row:
            flow_scores[source_section.grid_row] += (
                target_section.grid_col - source_section.grid_col
            )

    result: dict[int, bool] = {}
    for row, row_ids in rows.items():
        flow_score = flow_scores[row]
        if flow_score == 0:
            flow_score = sum(
                1 if graph.sections[sid].direction == "LR" else -1
                for sid in row_ids
                if graph.sections[sid].direction in ("LR", "RL")
            )
        result[row] = flow_score < 0
    return result


def _parallel_branch_sets(
    graph: MetroGraph,
    outgoing: dict[str, set[str]],
    descendants: Callable[[str], frozenset[str]],
) -> tuple[dict[str, set[str]], set[str]]:
    parallel_peers: dict[str, set[str]] = {sid: set() for sid in graph.sections}
    parallel_joins: set[str] = set()
    for targets in outgoing.values():
        targets_by_stage: dict[int, set[str]] = {}
        for target in targets:
            targets_by_stage.setdefault(graph.sections[target].grid_col, set()).add(
                target
            )
        for peers in targets_by_stage.values():
            if len(peers) < 2 or any(
                other in descendants(peer) for peer in peers for other in peers - {peer}
            ):
                continue
            branch_reach = [descendants(peer) for peer in peers]
            common = set(branch_reach[0])
            for reachable in branch_reach[1:]:
                common.intersection_update(reachable)
            for peer in peers:
                parallel_peers[peer].update(peers - {peer})
            parallel_joins.update(common)
    return parallel_peers, parallel_joins


def _renumber_sections_by_route(graph: MetroGraph) -> None:
    """Renumber sections by connected route continuity and visual reading order.

    Each disconnected flow is numbered fully before the next.  Within a flow,
    numbering follows connected sections along the nearest visual lane.  Parallel
    branches and independent merge inputs are completed before their join, while
    a dominant visual route may finish before a secondary route rejoins it.
    Authored numbers are reserved, and automatic sections receive the lowest
    unused positive numbers.
    """
    from nf_metro.layout.section_placement import _weakly_connected_components

    section_rank = {sid: rank for rank, sid in enumerate(graph.sections)}
    dag = graph.section_dag
    section_edges = dag.section_edges if dag else set()
    outgoing = {
        sid: dag.successors.get(sid, set()) if dag else set() for sid in graph.sections
    }
    incoming = {
        sid: dag.predecessors.get(sid, set()) if dag else set()
        for sid in graph.sections
    }

    row_is_rl = _section_row_directions(graph, section_edges)

    def stage_coordinate(sid: str) -> int:
        return graph.sections[sid].grid_col

    def lane_coordinate(sid: str) -> int:
        return graph.sections[sid].grid_row

    @cache
    def descendants(sid: str) -> frozenset[str]:
        return frozenset({sid} | _transitive_successors(sid, outgoing))

    @cache
    def ancestors(sid: str) -> frozenset[str]:
        return frozenset({sid} | _transitive_successors(sid, incoming))

    parallel_peers, parallel_joins = _parallel_branch_sets(graph, outgoing, descendants)

    def visual_key(sid: str) -> tuple[int, int, int]:
        section = graph.sections[sid]
        column = -section.grid_col if row_is_rl[section.grid_row] else section.grid_col
        return section.grid_row, column, section_rank[sid]

    def continuation_key(
        source: str, target: str
    ) -> tuple[bool, int, tuple[int, int, int]]:
        source_section = graph.sections[source]
        target_section = graph.sections[target]
        same_lane = lane_coordinate(source) == lane_coordinate(target)
        distance = abs(source_section.grid_col - target_section.grid_col) + abs(
            source_section.grid_row - target_section.grid_row
        )
        return not same_lane, distance, visual_key(target)

    components = sorted(
        _weakly_connected_components(graph, section_edges),
        key=lambda component: min(visual_key(sid) for sid in component),
    )

    ordered_ids: list[str] = []
    visited: set[str] = set()
    visit_index: dict[str, int] = {}

    def merge_is_blocked(target: str) -> bool:
        seen = incoming[target] & visited
        unseen = incoming[target] - visited
        return bool(unseen) and (
            target in parallel_joins
            or any(
                lane_coordinate(source) == lane_coordinate(target) for source in unseen
            )
            or any(
                ancestors(left).isdisjoint(ancestors(right))
                for left in seen
                for right in unseen
            )
            or any(
                stage_coordinate(left) == stage_coordinate(right)
                for left in seen
                for right in unseen
            )
        )

    for component in components:
        roots = [sid for sid in component if not (incoming[sid] & component)]
        current = min(roots or component, key=visual_key)
        deferred_targets: list[str] = []
        remaining = set(component)
        while True:
            visited.add(current)
            visit_index[current] = len(ordered_ids)
            ordered_ids.append(current)
            remaining.remove(current)
            if not remaining:
                break

            peer_candidates = [
                target
                for target in parallel_peers[current] & remaining
                if not merge_is_blocked(target)
            ]
            if peer_candidates:
                deferred_targets.extend(
                    target
                    for target in sorted(
                        outgoing[current] & remaining,
                        key=lambda target: continuation_key(current, target),
                    )
                    if target not in peer_candidates
                    and target not in deferred_targets
                    and not merge_is_blocked(target)
                )
                current = min(peer_candidates, key=visual_key)
                continue

            deferred_candidates = [
                target
                for target in deferred_targets
                if target in remaining and not merge_is_blocked(target)
            ]
            if deferred_candidates:
                current = deferred_candidates[0]
                deferred_targets.remove(current)
                continue

            direct = [
                target
                for target in outgoing[current] & remaining
                if not merge_is_blocked(target)
            ]
            if direct:
                current = min(
                    direct,
                    key=lambda target: continuation_key(current, target),
                )
                continue

            frontier = [
                target
                for target in remaining
                if incoming[target] & visited and not merge_is_blocked(target)
            ]
            if frontier:
                current = min(
                    frontier,
                    key=lambda target: (
                        -max(
                            visit_index[source] for source in incoming[target] & visited
                        ),
                        visual_key(target),
                    ),
                )
                continue

            remaining_roots = [
                sid for sid in remaining if not (incoming[sid] & remaining)
            ]
            current = min(remaining_roots or remaining, key=visual_key)

    reserved = {
        section.number_override
        for section in graph.sections.values()
        if section.number_override is not None
    }
    next_number = 1
    for sid in sorted(ordered_ids, key=lambda sid: not graph.sections[sid].draws_box):
        section = graph.sections[sid]
        if section.number_override is not None:
            section.number = section.number_override
            continue
        while next_number in reserved:
            next_number += 1
        section.number = next_number
        next_number += 1


def translate_graph(graph: MetroGraph, dx: float, dy: float) -> None:
    """Move every absolute coordinate the graph carries by ``(dx, dy)``.

    The laid-out graph states its geometry in canvas coordinates, and some of
    that lives outside the stations, ports, and section boxes: a rail section's
    per-line centrelines and the obstacle boxes the router steers bypasses
    around both name a canvas position.  Leaving any of them behind splits the
    graph across two coordinate frames, so a caller moving the map has to move
    the whole set together.

    An authored legend pin (``%%metro legend: x,y``) is not part of that set: it
    places a block on the canvas, not a point in the map, and stays where the
    author put it.
    """
    if not dx and not dy:
        return
    for st in graph.stations.values():
        st.x += dx
        st.y += dy
        if st.rail_top_y is not None:
            st.rail_top_y += dy
        if st.rail_bottom_y is not None:
            st.rail_bottom_y += dy
        st.rail_used_ys = [y + dy for y in st.rail_used_ys]
    for section in graph.sections.values():
        section.bbox_x += dx
        section.bbox_y += dy
    for port in graph.ports.values():
        port.x += dx
        port.y += dy
    graph.bypass_label_obstacles = {
        sid: (x0 + dx, y0 + dy, x1 + dx, y1 + dy)
        for sid, (x0, y0, x1, y1) in graph.bypass_label_obstacles.items()
    }
    graph._rail_y = {
        sid: {line_id: y + dy for line_id, y in rails.items()}
        for sid, rails in graph._rail_y.items()
    }
    graph._placement_ref_y = {sid: y + dy for sid, y in graph._placement_ref_y.items()}
    graph._placement_ref_bbox_top = {
        sid: y + dy for sid, y in graph._placement_ref_bbox_top.items()
    }


def _canvas_top_shortfall(graph: MetroGraph, section_y_padding: float) -> float:
    """Downward shift needed so the graph clears both top floors (0 if none).

    Containment: every section must sit ``section_y_padding`` below the canvas
    top.  Title: when the map is titled *and* ``graph.reserve_title_band`` (set
    by :func:`~nf_metro.api.prepare_graph` -- false for ``--bare`` or a logo
    folded into the legend, cases where render draws nothing up there), a
    *drawn* section whose header badge overlaps the title band (box top above
    ``TITLE_BAND_OVERLAP_FLOOR``) is lifted clear to ``TITLE_BAND_CLEARANCE``
    -- a map already clearing the title, and implicit holders (which draw no
    badge), are left untouched.
    """
    min_all = _min_section_bbox_top(graph, section_y_padding)
    shortfall = max(0.0, section_y_padding - min_all)
    if graph.title and graph.reserve_title_band:
        min_drawn = _min_drawn_section_bbox_top(graph)
        if min_drawn is not None and min_drawn < TITLE_BAND_OVERLAP_FLOOR:
            shortfall = max(shortfall, TITLE_BAND_CLEARANCE - min_drawn)
    return shortfall


def _canvas_top_preserved(
    graph: MetroGraph, section_y_padding: float, shift: float
) -> bool:
    """True if ``shift`` keeps every section within its top floor.

    The containment floor (``section_y_padding``) bounds all sections; the
    no-overlap floor (``TITLE_BAND_OVERLAP_FLOOR``) bounds drawn sections on a
    titled map whose title band will actually be drawn (see
    ``graph.reserve_title_band``).  Lets the grid snap reject a candidate that
    would lift the top above a floor.
    """
    min_all = _min_section_bbox_top(graph, section_y_padding)
    if min_all + shift < section_y_padding - 1e-6:
        return False
    if graph.title and graph.reserve_title_band:
        min_drawn = _min_drawn_section_bbox_top(graph)
        if (
            min_drawn is not None
            and min_drawn + shift < TITLE_BAND_OVERLAP_FLOOR - 1e-6
        ):
            return False
    return True


def _shift_graph_into_canvas(graph: MetroGraph, section_y_padding: float) -> None:
    """Shift the whole graph down if the topmost section is above the canvas.

    Lifts the graph by ``_canvas_top_shortfall`` so it clears the canvas-top
    margin and, on a titled map, the title band.  No-op when it already does.
    """
    shortfall = _canvas_top_shortfall(graph, section_y_padding)
    if shortfall > 0:
        translate_graph(graph, 0.0, shortfall)
