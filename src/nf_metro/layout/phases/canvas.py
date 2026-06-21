"""Whole-graph canvas translation into view and section renumbering by reading order."""

from __future__ import annotations

from nf_metro.layout.phases.bbox import _min_section_bbox_top
from nf_metro.parser.model import MetroGraph, Section


def _renumber_sections_by_grid(graph: MetroGraph) -> None:
    """Renumber sections by visual reading order.

    Groups sections into flow sweeps separated by fold boundaries:
    each left-to-right (or right-to-left) run is one sweep, with
    TB fold sections belonging to the sweep they terminate.  Within
    each sweep, sections are numbered by (grid_row, grid_col) so
    rows go top-to-bottom and sections within a row go left-to-right.
    All numbers in sweep N+1 are greater than those in sweep N.
    """
    from collections import deque

    import networkx as nx

    dag: nx.DiGraph[str] = nx.DiGraph()
    for sid in graph.sections:
        dag.add_node(sid)
    if graph.section_dag:
        for src, tgt in graph.section_dag.section_edges:
            if src in graph.sections and tgt in graph.sections:
                dag.add_edge(src, tgt)

    secs = graph.sections

    def _is_direction_change(src: str, tgt: str) -> bool:
        """True when flow direction reverses between two sections."""
        sd, td = secs[src].direction, secs[tgt].direction
        # TB->LR/RL: only counts if the TB's predecessors flowed
        # the opposite way (i.e. TB is a fold boundary).
        if sd == "TB" and td in ("LR", "RL"):
            for pred in dag.predecessors(src):
                pd = secs[pred].direction
                if pd in ("LR", "RL") and pd != td:
                    return True
            return False
        if sd in ("LR", "RL") and td in ("LR", "RL") and sd != td:
            return True
        return False

    sweep: dict[str, int] = {}
    roots = [n for n in dag.nodes() if dag.in_degree(n) == 0]
    q: deque[str] = deque()
    for r in roots:
        sweep[r] = 0
        q.append(r)

    while q:
        node = q.popleft()
        for succ in dag.successors(node):
            new_depth = sweep[node]
            if _is_direction_change(node, succ):
                new_depth = sweep[node] + 1
            if succ not in sweep or new_depth < sweep[succ]:
                sweep[succ] = new_depth
                q.append(succ)

    for sid in graph.sections:
        if sid not in sweep:
            sweep[sid] = 0

    # Determine flow direction for each sweep: RL sweeps number
    # columns right-to-left (descending grid_col) to match the flow.
    sweep_is_rl: dict[int, bool] = {}
    for sid, s in graph.sections.items():
        sw = sweep[sid]
        if sw not in sweep_is_rl and s.direction == "RL":
            sweep_is_rl[sw] = True
        elif sw not in sweep_is_rl and s.direction == "LR":
            sweep_is_rl[sw] = False

    def _sort_key(s: Section) -> tuple[int, int, int]:
        sw = sweep[s.id]
        col = -s.grid_col if sweep_is_rl.get(sw, False) else s.grid_col
        return (sw, s.grid_row, col)

    sorted_sections = sorted(graph.sections.values(), key=_sort_key)
    for i, section in enumerate(sorted_sections, start=1):
        section.number = i


def _translate_graph_y(graph: MetroGraph, shift: float) -> None:
    """Shift every station, section bbox, and port down by ``shift``."""
    for st in graph.stations.values():
        st.y += shift
    for section in graph.sections.values():
        section.bbox_y += shift
    for port in graph.ports.values():
        port.y += shift


def _shift_graph_into_canvas(graph: MetroGraph, section_y_padding: float) -> None:
    """Shift the whole graph down if the topmost section is above the canvas.

    Keeps the topmost section's ``section_y_padding`` margin from the
    canvas edge.  No-op when all sections already sit inside.
    """
    min_top = _min_section_bbox_top(graph, section_y_padding)
    if min_top >= section_y_padding:
        return
    _translate_graph_y(graph, section_y_padding - min_top)
