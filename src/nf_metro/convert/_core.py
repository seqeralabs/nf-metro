"""Format-neutral back end shared by the workflow-DAG converters.

Each front end (``nextflow``, ``snakemake``) parses its tool's export into a
:class:`ParsedDag` and picks the nodes worth keeping as stations;
:func:`dag_to_mmd` does the rest: reconnects edges around dropped nodes,
breaks cycles, maps groups to sections, assigns main/bypass/spur lines, and
emits a ``graph LR`` nf-metro ``.mmd`` file.
"""

from __future__ import annotations

import re
import warnings
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import pairwise

import networkx as nx

from nf_metro.graph_views import directed_graph, longest_path_layers

# ---------------------------------------------------------------------------
# Colour palette for auto-generated metro lines
# ---------------------------------------------------------------------------
LINE_COLORS = [
    "#2db572",  # green (main)
    "#0570b0",  # blue
    "#f5c542",  # yellow
    "#e63946",  # red
    "#9b59b6",  # purple
    "#ff9800",  # orange
    "#00bcd4",  # cyan
    "#795548",  # brown
]


FEEDBACK_COMMENT_HEADER = (
    "%% Feedback removed: the process graph loops back here, and nf-metro's",
    "%% layout requires a DAG, so these connections were left out of the map.",
    "%% Each one may run through channel or operator nodes rather than being",
    "%% a single declared edge.",
)
"""Preamble for the ``%%`` block listing the back edges the converter removed.

Plain ``%%`` lines are comments the nf-metro parser ignores, so the block
travels with the converted file without changing what it renders.
"""


class FeedbackEdgesDroppedWarning(UserWarning):
    """Warns that converting a cyclic workflow DAG had to remove edges.

    nf-metro's layout engine requires an acyclic graph, so a workflow whose
    step graph loops cannot be converted edge-for-edge. The converter
    removes a deterministic set of back edges; this warning names them, so the
    loop is reported to the caller instead of vanishing.

    ``connections`` carries the same list the message renders, so a caller can
    count or inspect them without parsing prose.
    """

    def __init__(self, message: str, connections: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.connections = connections


class ExperimentalInputWarning(UserWarning):
    """Warns that the input format's converter is experimental.

    Its output may change between releases, and complex workflows are likely
    to need hand-editing of the converted ``.mmd``.
    """


# ---------------------------------------------------------------------------
# Format-neutral intermediate DAG, filled by each front end
# ---------------------------------------------------------------------------
@dataclass
class DagNode:
    """A node parsed from a workflow manager's DAG export."""

    id: str
    label: str
    shape: str  # set by the front end, e.g. Nextflow "stadium" for a process
    subgraph: str | None = None  # named subgraph, None if outside or in " "
    display: str | None = None  # station label as-is; unset humanises ``label``


@dataclass
class DagGroup:
    """A named node group, such as a Nextflow subworkflow, that becomes a section."""

    full_name: str  # e.g. "NFCORE_RNASEQ:RNASEQ:PREPROCESS"
    short_name: str  # e.g. "PREPROCESS" (from [SHORT_NAME])
    node_ids: list[str] = field(default_factory=list)


@dataclass
class ParsedDag:
    """Intermediate representation of a parsed workflow DAG."""

    nodes: dict[str, DagNode] = field(default_factory=dict)
    edges: list[tuple[str, str]] = field(default_factory=list)
    subgraphs: dict[str, DagGroup] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Node classification and edge reconnection
# ---------------------------------------------------------------------------
@dataclass
class _Reconnection:
    """Edges stitched through dropped nodes, and the self-loops that leaves.

    ``edges`` is the kept-node graph every later pass consumes.
    ``self_loops`` holds the ``(node, node)`` pairs the stitching cannot
    express: a kept node that reaches itself through dropped nodes.
    They are returned rather than dropped, so a single-node feedback round
    is reported instead of disappearing.
    """

    edges: list[tuple[str, str]] = field(default_factory=list)
    self_loops: list[tuple[str, str]] = field(default_factory=list)


def _reconnect_edges(
    kept_ids: set[str],
    all_edges: list[tuple[str, str]],
) -> _Reconnection:
    """Reconnect edges through dropped nodes.

    For each kept node, BFS forward through dropped nodes to find
    reachable kept nodes, creating direct edges.
    """
    successors: dict[str, set[str]] = defaultdict(set)
    for src, tgt in all_edges:
        successors[src].add(tgt)

    new_edges: set[tuple[str, str]] = set()
    self_loops: set[tuple[str, str]] = set()

    for src in kept_ids:
        # BFS through dropped nodes
        visited: set[str] = set()
        queue = deque(successors.get(src, set()))
        while queue:
            node = queue.popleft()
            if node in visited:
                continue
            visited.add(node)
            if node in kept_ids:
                if node != src:
                    new_edges.add((src, node))
                else:  # a self-loop the metro graph has no way to hold
                    self_loops.add((src, src))
                # Don't continue through kept nodes
            else:
                queue.extend(successors.get(node, set()))

    return _Reconnection(edges=sorted(new_edges), self_loops=sorted(self_loops))


# ---------------------------------------------------------------------------
# Cycle breaking
# ---------------------------------------------------------------------------
@dataclass
class _CycleBreak:
    """The two halves of :func:`_break_cycles`: what survives and what does not.

    ``kept`` is the acyclic edge list every later pass consumes. ``removed``
    holds the back edges in ``edges`` order, which ``_reconnect_edges`` sorts
    by node id. Either list may hold an edge stitched through channel or
    operator nodes rather than one written in the pipeline source.
    """

    kept: list[tuple[str, str]] = field(default_factory=list)
    removed: list[tuple[str, str]] = field(default_factory=list)


def _break_cycles(nodes: set[str], edges: list[tuple[str, str]]) -> _CycleBreak:
    """Split ``edges`` into the acyclic remainder and the DFS back edges.

    The back edges are the deterministic set whose removal makes the graph a
    DAG. They are returned rather than discarded: nf-metro cannot lay out a
    cycle, but losing one without saying so hides a real feature of the
    pipeline.
    """
    graph = directed_graph(sorted(nodes), edges)
    active: set[str] = set()
    back_edges: set[tuple[str, str]] = set()
    for source, target, label in nx.dfs_labeled_edges(graph):
        if label == "forward":
            active.add(target)
        elif label == "reverse":
            active.discard(target)
        elif label == "nontree" and target in active:
            back_edges.add((source, target))
    return _CycleBreak(
        kept=[(s, t) for s, t in edges if (s, t) not in back_edges],
        removed=[(s, t) for s, t in edges if (s, t) in back_edges],
    )


# ---------------------------------------------------------------------------
# Section and line assignment
# ---------------------------------------------------------------------------
@dataclass
class _SectionAssignment:
    """Mapping of kept nodes to sections.

    The three fields are mutually consistent views of the same grouping:
    ``node_section[nid]`` is the section key for node ``nid``, ``names[key]``
    is that section's display name, and ``nodes[key]`` lists the node ids
    in that section.
    """

    node_section: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    nodes: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))


def _assign_sections(
    kept_ids: set[str], dag: ParsedDag, title: str
) -> _SectionAssignment:
    """Group kept nodes into sections.

    Nodes inside a named subgraph are assigned to that subgraph's section.
    Nodes outside any subgraph go into a synthesised section: ``__pipeline``
    (named after the title) when there are no subgraphs at all, otherwise
    ``__reporting``.
    """
    assignment = _SectionAssignment()

    for nid in sorted(kept_ids):
        node = dag.nodes[nid]
        if node.subgraph and node.subgraph in dag.subgraphs:
            sg = dag.subgraphs[node.subgraph]
            assignment.node_section[nid] = node.subgraph
            assignment.names[node.subgraph] = sg.short_name
            assignment.nodes[node.subgraph].append(nid)

    unassigned = sorted(nid for nid in kept_ids if nid not in assignment.node_section)
    if unassigned:
        if not assignment.names:
            auto_key, auto_name = "__pipeline", title or "Pipeline"
        else:
            auto_key, auto_name = "__reporting", "Reporting"
        assignment.names[auto_key] = auto_name
        for nid in unassigned:
            assignment.node_section[nid] = auto_key
            assignment.nodes[auto_key].append(nid)

    return assignment


def _cyclic_members(graph: nx.DiGraph[str]) -> set[str]:
    """Nodes of ``graph`` that lie on a cycle."""
    return {
        member
        for component in nx.strongly_connected_components(graph)
        if len(component) > 1
        for member in component
    }


def _unit(sections: _SectionAssignment, nid: str, split_secs: set[str]) -> str:
    sec = sections.node_section[nid]
    return f"node:{nid}" if sec in split_secs else f"section:{sec}"


def _unit_graph(
    sections: _SectionAssignment, edges: list[tuple[str, str]], split_secs: set[str]
) -> nx.DiGraph[str]:
    """Graph of whole sections plus the individual nodes of ``split_secs``.

    Edge weight ``w`` is 1 where the edge crosses a section boundary, else 0.
    """
    node_section = sections.node_section
    graph: nx.DiGraph[str] = nx.DiGraph()
    graph.add_nodes_from(
        sorted({_unit(sections, nid, split_secs) for nid in node_section})
    )
    for s, t in edges:
        u, v = _unit(sections, s, split_secs), _unit(sections, t, split_secs)
        if u != v:
            graph.add_edge(u, v, w=int(node_section[s] != node_section[t]))
    return graph


def _cyclic_sections(
    sections: _SectionAssignment, edges: list[tuple[str, str]], split_secs: set[str]
) -> set[str]:
    """Whole sections still on a cycle once ``split_secs`` are split apart."""
    return {
        unit.removeprefix("section:")
        for unit in _cyclic_members(_unit_graph(sections, edges, split_secs))
        if unit.startswith("section:")
    }


def _flow_depths(graph: nx.DiGraph[str], latest: bool) -> dict[str, int]:
    """Longest-path depth of each unit, earliest or latest feasible.

    Either choice makes depth strictly increase along every weight-1 edge and
    never decrease along a weight-0 one.
    """
    order = list(nx.lexicographical_topological_sort(graph))
    depth: dict[str, int] = {}
    if not latest:
        for unit in order:
            depth[unit] = max(
                (depth[p] + graph[p][unit]["w"] for p in graph.predecessors(unit)),
                default=0,
            )
        return depth
    height: dict[str, int] = {}
    for unit in reversed(order):
        height[unit] = max(
            (height[s] + graph[unit][s]["w"] for s in graph.successors(unit)),
            default=0,
        )
    top = max(height.values(), default=0)
    return {unit: top - height[unit] for unit in order}


def _split_cyclic_sections(
    sections: _SectionAssignment, edges: list[tuple[str, str]], dag: ParsedDag
) -> _SectionAssignment:
    """Split sections that form a cycle into acyclic groups by flow depth.

    Unassigned nodes share one section, and Nextflow flattens a parent
    subworkflow's own processes into a subgraph beside its children's, so a
    section can run both before and after another one. Sections are split
    outermost first: the unassigned section, then subgraphs by nesting depth
    of their full name, each tier only where a cycle remains. Sections not on
    a cycle stay whole.

    A split section's nodes are grouped by flow depth, the longest path
    counting one per section boundary crossed, so every inter-section edge
    strictly increases depth and the result is acyclic. Earliest and latest
    depths are both valid; whichever yields fewer groups is used. A split
    unassigned section is named ``Main`` up to its last group, ``Reporting``;
    a split subgraph keeps its name on every group.
    """
    if not _cyclic_sections(sections, edges, set()):
        return sections

    def tier(sec: str) -> int:
        if sec.startswith("__"):
            return 0
        return 1 + dag.subgraphs[sec].full_name.count(":")

    split_secs: set[str] = set()
    for level in sorted({tier(sec) for sec in sections.names}):
        split_secs |= {
            sec
            for sec in _cyclic_sections(sections, edges, split_secs)
            if tier(sec) == level
        }
    unit_graph = _unit_graph(sections, edges, split_secs)

    def group_count(depth: dict[str, int]) -> int:
        return len(
            {
                (sec, depth[f"node:{nid}"])
                for sec in split_secs
                for nid in sections.nodes[sec]
            }
        )

    depth = min(
        (_flow_depths(unit_graph, latest) for latest in (False, True)),
        key=group_count,
    )

    split = _SectionAssignment()
    for sec, name in sections.names.items():
        members = sections.nodes[sec]
        if sec not in split_secs:
            split.names[sec] = name
            split.nodes[sec] = list(members)
            for nid in members:
                split.node_section[nid] = sec
            continue
        depths = sorted({depth[_unit(sections, nid, split_secs)] for nid in members})
        for index, d in enumerate(depths):
            key = f"{sec}__{index}"
            if sec == "__reporting":
                split.names[key] = "Reporting" if d == depths[-1] else "Main"
            else:
                split.names[key] = name
            for nid in members:
                if depth[_unit(sections, nid, split_secs)] == d:
                    split.node_section[nid] = key
                    split.nodes[key].append(nid)
    return split


def _classify_edges(
    edges: list[tuple[str, str]], node_section: dict[str, str]
) -> tuple[dict[str, list[tuple[str, str]]], list[tuple[str, str]]]:
    """Partition edges into per-section intra edges and inter-section edges.

    Edges whose endpoints lack a section assignment are dropped.
    """
    intra_edges: dict[str, list[tuple[str, str]]] = defaultdict(list)
    inter_edges: list[tuple[str, str]] = []

    for src, tgt in edges:
        src_sec = node_section.get(src)
        tgt_sec = node_section.get(tgt)
        if src_sec and tgt_sec:
            if src_sec == tgt_sec:
                intra_edges[src_sec].append((src, tgt))
            else:
                inter_edges.append((src, tgt))

    return intra_edges, inter_edges


def _declaration_topological_order(graph: nx.DiGraph[str]) -> list[str]:
    """Use declaration order for ties and append cycle-blocked nodes unchanged."""
    declaration_order = list(graph)
    rank = {node: index for index, node in enumerate(declaration_order)}
    try:
        return list(nx.lexicographical_topological_sort(graph, key=rank.__getitem__))
    except nx.NetworkXUnfeasible:
        cycle_nodes: set[str] = set()
        for component in nx.strongly_connected_components(graph):
            if len(component) > 1 or any(
                graph.has_edge(node, node) for node in component
            ):
                cycle_nodes.update(component)
        blocked = set(
            nx.multi_source_dijkstra_path_length(graph, cycle_nodes, weight=None)
        )
        sortable = graph.subgraph(
            node for node in declaration_order if node not in blocked
        )
        ordered = list(
            nx.lexicographical_topological_sort(sortable, key=rank.__getitem__)
        )
        ordered.extend(node for node in declaration_order if node in blocked)
        return ordered


def _order_section_nodes(
    section_nodes: dict[str, list[str]], edges: list[tuple[str, str]]
) -> dict[str, list[str]]:
    """Topologically order each section, breaking ready ties by declaration."""
    section_node_order: dict[str, list[str]] = {}
    for sec_key, nids in section_nodes.items():
        local_nodes = set(nids)
        local_edges = (
            (src, tgt)
            for src, tgt in edges
            if src in local_nodes and tgt in local_nodes
        )
        graph = directed_graph(nids, local_edges)
        section_node_order[sec_key] = _declaration_topological_order(graph)

    return section_node_order


def _topological_order(
    section_ids: list[str],
    edges: list[tuple[str, str]],
    node_section: dict[str, str],
) -> list[str]:
    """Topologically order sections, breaking ready ties by declaration."""
    section_edges: set[tuple[str, str]] = set()
    for src, tgt in edges:
        src_sec = node_section.get(src)
        tgt_sec = node_section.get(tgt)
        if src_sec and tgt_sec and src_sec != tgt_sec:
            section_edges.add((src_sec, tgt_sec))

    graph = directed_graph(section_ids, sorted(section_edges))
    return _declaration_topological_order(graph)


_MAX_LABEL_LEN = 16


def _display_label(node: DagNode) -> str:
    return node.display or _humanize_label(node.label)


def _humanize_label(name: str, abbreviate: bool = True) -> str:
    """Convert UPPER_SNAKE_CASE to Title Case, optionally abbreviating.

    STAR_ALIGN -> Star Align, FASTQC -> Fastqc, BWA_MEM -> Bwa Mem.
    Long labels (>_MAX_LABEL_LEN) are shortened by trimming the longest
    word repeatedly until the result fits.
    """
    words = [part.capitalize() for part in name.split("_")]
    label = " ".join(words)
    if not abbreviate or len(label) <= _MAX_LABEL_LEN:
        return label
    # Progressively trim the longest word
    while len(" ".join(words)) > _MAX_LABEL_LEN:
        lengths = [len(w) for w in words]
        longest_idx = lengths.index(max(lengths))
        if lengths[longest_idx] <= 3:
            break
        words[longest_idx] = words[longest_idx][:-1]
    return " ".join(words)


def _sanitize_id(name: str) -> str:
    """Convert a node or section name to a valid nf-metro station ID.

    Lowercase, replace non-alphanumeric with underscore.
    """
    return re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_")


def _allocate_unique_id(name: str, used: set[str], fallback: str) -> str:
    """Sanitise a display name and claim an unused ID in its namespace."""
    base = _sanitize_id(name) or fallback
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _allocate_station_ids(
    section_order: list[str],
    section_node_order: dict[str, list[str]],
    nodes: dict[str, DagNode],
) -> dict[str, str]:
    """Assign a unique station ID to every kept node.

    Two `DagNode`s can share a label (e.g. `SAMTOOLS_SORT` reused across
    several subworkflows). Sanitising the label alone collapses them onto
    one station ID, producing duplicate declarations and self-loop edges
    that crash layout. Walk sections in topological order and suffix
    subsequent occurrences (`samtools_sort`, `samtools_sort_2`, ...).
    """
    used: set[str] = set()
    station_ids: dict[str, str] = {}
    for sec_key in section_order:
        for nid in section_node_order.get(sec_key, []):
            station_ids[nid] = _allocate_unique_id(nodes[nid].label, used, "node")
    return station_ids


# ---------------------------------------------------------------------------
# Line assignment
# ---------------------------------------------------------------------------
@dataclass
class _LineAssignment:
    """Metro lines and the edge groupings that feed emission.

    ``main`` carries adjacent flow; ``bypass_lines`` keys a ``(src_sec,
    tgt_sec)`` section pair to its ``(id, name, color)``; ``spur`` (when
    present) carries dead-end branches; ``side`` lists the further lines a
    front end planned (see :data:`LinePlan`). ``edge_line`` maps every emitted
    edge to its line id; its insertion order is the emission order.
    """

    main: tuple[str, str, str]
    spur: tuple[str, str, str] | None
    bypass_lines: dict[tuple[str, str], tuple[str, str, str]]
    edge_line: dict[tuple[str, str], str]
    side: list[tuple[str, str, str]] = field(default_factory=list)


LinePlan = list[tuple[tuple[str, str, str], list[tuple[str, str]]]]
"""Lines a front end draws itself: each ``(id, name, colour)`` with its edges.

The first line is the trunk. Every edge must be on exactly one line.
"""


def _planned_lines(plan: LinePlan) -> _LineAssignment:
    (main, _) = plan[0]
    edge_line = {edge: line[0] for line, line_edges in plan for edge in line_edges}
    return _LineAssignment(
        main=main,
        spur=None,
        bypass_lines={},
        edge_line=edge_line,
        side=[line for line, _ in plan[1:]],
    )


def _assign_lines(
    kept_ids: set[str],
    edges: list[tuple[str, str]],
    intra_edges: dict[str, list[tuple[str, str]]],
    inter_edges: list[tuple[str, str]],
    node_section: dict[str, str],
    section_names: dict[str, str],
    section_order: list[str],
) -> _LineAssignment:
    """Build metro lines and map each edge to its line.

    Dead-end nodes (mid-pipeline nodes with no successors, excluding
    the last section) branch off onto a ``spur`` line. Inter-section edges
    spanning two or more sections become per-pair ``bypass`` lines; adjacent
    inter-section edges stay on ``main``.
    """
    section_rank = {sid: i for i, sid in enumerate(section_order)}

    successors_map: dict[str, set[str]] = defaultdict(set)
    for src, tgt in edges:
        successors_map[src].add(tgt)

    last_section = section_order[-1] if section_order else None
    dead_ends: set[str] = set()
    for nid in kept_ids:
        if not successors_map.get(nid) and node_section.get(nid) != last_section:
            dead_ends.add(nid)

    spur_edges: set[tuple[str, str]] = set()
    for src, tgt in edges:
        if tgt in dead_ends:
            spur_edges.add((src, tgt))

    bypass_groups: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    main_inter_edges: list[tuple[str, str]] = []
    for src, tgt in inter_edges:
        if (src, tgt) in spur_edges:
            continue
        src_sec = node_section[src]
        tgt_sec = node_section[tgt]
        span = abs(section_rank.get(tgt_sec, 0) - section_rank.get(src_sec, 0))
        if span >= 2:
            bypass_groups[(src_sec, tgt_sec)].append((src, tgt))
        else:
            main_inter_edges.append((src, tgt))

    color_idx = 0
    main_line_id = "main"
    main = (main_line_id, "Main", LINE_COLORS[color_idx % len(LINE_COLORS)])
    color_idx += 1

    bypass_lines: dict[tuple[str, str], tuple[str, str, str]] = {}
    used_line_ids = {main_line_id, "spur"}
    for src_sec, tgt_sec in sorted(bypass_groups.keys()):
        src_name = _humanize_label(section_names[src_sec], abbreviate=False)
        tgt_name = _humanize_label(section_names[tgt_sec], abbreviate=False)
        line_id = _allocate_unique_id(
            f"{section_names[src_sec]}_{section_names[tgt_sec]}",
            used_line_ids,
            "bypass",
        )
        line_name = f"{src_name} - {tgt_name}"
        line_color = LINE_COLORS[color_idx % len(LINE_COLORS)]
        color_idx += 1
        bypass_lines[(src_sec, tgt_sec)] = (line_id, line_name, line_color)

    spur: tuple[str, str, str] | None = None
    spur_line_id = ""
    if spur_edges:
        spur_line_id = "spur"
        spur = (spur_line_id, "Spur", LINE_COLORS[color_idx % len(LINE_COLORS)])
        color_idx += 1

    edge_line: dict[tuple[str, str], str] = {}
    for _sec_key, sec_edges in intra_edges.items():
        for e in sec_edges:
            edge_line[e] = spur_line_id if e in spur_edges else main_line_id
    for e in main_inter_edges:
        edge_line[e] = main_line_id
    for (src_sec, tgt_sec), bp_edges in bypass_groups.items():
        line_id = bypass_lines[(src_sec, tgt_sec)][0]
        for e in bp_edges:
            edge_line[e] = line_id
    for e in sorted(spur_edges):
        if e not in edge_line:
            edge_line[e] = spur_line_id

    return _LineAssignment(
        main=main,
        spur=spur,
        bypass_lines=bypass_lines,
        edge_line=edge_line,
    )


# ---------------------------------------------------------------------------
# Title inference and emission
# ---------------------------------------------------------------------------
def _infer_title(section_names: dict[str, str], section_order: list[str]) -> str:
    """Derive a pipeline title from the section names."""
    if len(section_names) == 1 and "__pipeline" in section_names:
        return "Pipeline"
    named_secs = list(
        dict.fromkeys(
            _humanize_label(section_names[k])
            for k in section_order
            if not k.startswith("__")
        )
    )
    if named_secs:
        return " / ".join(named_secs) + " Pipeline"
    return "Pipeline"


def _emit_mmd(
    title: str,
    section_order: list[str],
    section_names: dict[str, str],
    section_node_order: dict[str, list[str]],
    intra_edges: dict[str, list[tuple[str, str]]],
    lines: _LineAssignment,
    station_ids: dict[str, str],
    nodes: dict[str, DagNode],
    dropped_feedback: list[tuple[str, str]] | None = None,
) -> str:
    """Assemble the nf-metro ``.mmd`` text from the resolved layout.

    ``dropped_feedback`` carries the back edges :func:`_break_cycles` removed.
    They are written as a trailing ``%%`` comment block: the parser ignores
    plain comments, so the file still renders, and the loop stays visible to
    whoever hand-tunes the output.
    """
    main_line_id, main_line_name, main_color = lines.main

    out: list[str] = []
    out.append(f"%%metro title: {title}")
    out.append("%%metro style: dark")
    out.append("%%metro line_order: span")
    out.append(f"%%metro line: {main_line_id} | {main_line_name} | {main_color}")
    if lines.spur:
        spur_id, spur_name, spur_color = lines.spur
        out.append(f"%%metro line: {spur_id} | {spur_name} | {spur_color}")
    for lid, lname, lcolor in lines.side:
        out.append(f"%%metro line: {lid} | {lname} | {lcolor}")
    for (_src_sec, _tgt_sec), (lid, lname, lcolor) in sorted(
        lines.bypass_lines.items(), key=lambda x: x[1][0]
    ):
        out.append(f"%%metro line: {lid} | {lname} | {lcolor}")
    out.append("")
    out.append("graph LR")

    used_section_ids: set[str] = set()
    for sec_key in section_order:
        sec_name = section_names[sec_key]
        sec_id = _allocate_unique_id(sec_name, used_section_ids, "section")
        display = _humanize_label(sec_name, abbreviate=False)
        ordered_nodes = section_node_order.get(sec_key, [])

        out.append(f"    subgraph {sec_id} [{display}]")

        for nid in ordered_nodes:
            label = _display_label(nodes[nid])
            out.append(f"        {station_ids[nid]}([{label}])")

        sec_edges = intra_edges.get(sec_key, [])
        if sec_edges:
            out.append("")
            for src, tgt in sec_edges:
                lid = lines.edge_line.get((src, tgt), main_line_id)
                out.append(f"        {station_ids[src]} -->|{lid}| {station_ids[tgt]}")

        out.append("    end")
        out.append("")

    intra = {edge for sec_edges in intra_edges.values() for edge in sec_edges}
    all_inter = [edge for edge in lines.edge_line if edge not in intra]

    if all_inter:
        out.append("    %% Inter-section edges")
        for src, tgt in all_inter:
            lid = lines.edge_line.get((src, tgt), main_line_id)
            out.append(f"    {station_ids[src]} -->|{lid}| {station_ids[tgt]}")

    if dropped_feedback:
        out.append("")
        out.extend(f"    {line}" for line in FEEDBACK_COMMENT_HEADER)

        def describe(node: str) -> str:
            return f"{station_ids[node]} ({_display_label(nodes[node])})"

        for src, tgt in dropped_feedback:
            out.append(f"    %%   {_format_feedback_pair(src, tgt, describe)}")

    return "\n".join(out) + "\n"


def _format_feedback_pair(
    source: str, target: str, describe: Callable[[str], str]
) -> str:
    """Render one removed connection, naming a self-loop as such.

    ``describe(node) -> describe(node)`` reads as though two nodes were
    involved, so a node that feeds itself is spelled out instead.
    """
    if source == target:
        return f"{describe(source)} -> itself"
    return f"{describe(source)} -> {describe(target)}"


def _warn_feedback_edges_dropped(
    dropped: list[tuple[str, str]], nodes: dict[str, DagNode]
) -> None:
    """Report the back edges removed to make the kept graph acyclic."""

    def describe(node: str) -> str:
        return _display_label(nodes[node])

    connections = tuple(
        _format_feedback_pair(src, tgt, describe) for src, tgt in dropped
    )
    warnings.warn(
        FeedbackEdgesDroppedWarning(
            f"{len(connections)} feedback connection(s) removed to make the "
            f"graph acyclic, which nf-metro's layout requires: "
            f"{', '.join(connections)}. The converted .mmd lists them in a "
            "%% comment block.",
            connections,
        ),
        stacklevel=4,
    )


def _longest_path(
    graph: nx.DiGraph[str],
    key: Callable[[str], tuple[str, str]],
    end: str | None = None,
) -> list[str]:
    """The longest path in *graph*, ending at *end* if given, ties by *key*."""
    layers = longest_path_layers(graph, sorted(graph, key=key))
    if end is None:
        end = min(layers, key=lambda n: (-layers[n], key(n)))
    path = [end]
    while preds := [
        p for p in graph.predecessors(path[-1]) if layers[p] == layers[path[-1]] - 1
    ]:
        path.append(min(preds, key=key))
    return path[::-1]


def branch_line_planner(
    nodes: dict[str, DagNode], edges: list[tuple[str, str]]
) -> LinePlan:
    """Plan lines from the graph's shape rather than from its sections.

    The trunk, ``main``, is the longest chain into the main sink: the sink with
    the most direct inputs, ties going to the one with more upstream nodes,
    then to the first by label. Each remaining chain, longest first, becomes
    its own line, named after its first node not already on a line. Ties
    break on node labels, then ids, so the plan does not depend on hash order.
    """
    if not edges:
        return []

    def key(nid: str) -> tuple[str, str]:
        return nodes[nid].label, nid

    graph = directed_graph(sorted({n for edge in edges for n in edge}), edges)
    sink = min(
        (n for n in graph if graph.out_degree(n) == 0),
        key=lambda n: (-graph.in_degree(n), -len(nx.ancestors(graph, n)), key(n)),
    )
    trunk = _longest_path(graph, key, sink)
    lines: LinePlan = [(("main", "Main", LINE_COLORS[0]), list(pairwise(trunk)))]
    used_ids = {"main"}
    on_line = set(trunk)
    while True:
        graph.remove_edges_from(lines[-1][1])
        graph.remove_nodes_from(list(nx.isolates(graph)))
        if not graph:
            return lines
        path = _longest_path(graph, key)
        first = next((n for n in path if n not in on_line), path[-1])
        line_id = _allocate_unique_id(nodes[first].label, used_ids, "line")
        color = LINE_COLORS[len(lines) % len(LINE_COLORS)]
        lines.append(
            ((line_id, _display_label(nodes[first]), color), list(pairwise(path)))
        )
        on_line.update(path)


def dag_to_mmd(
    dag: ParsedDag,
    kept_ids: set[str],
    title: str = "",
    plan_lines: Callable[[dict[str, DagNode], list[tuple[str, str]]], LinePlan]
    | None = None,
) -> str:
    """Convert a parsed DAG to nf-metro ``.mmd`` text, keeping only *kept_ids*.

    Edges that ran through dropped nodes are reconnected between the kept
    nodes they linked. Kept nodes inside a named group become that group's
    section; the rest share one synthesised section.

    *plan_lines*, when given, receives the nodes and the final acyclic edges
    and returns the map's lines, such as :func:`branch_line_planner`. Without
    it, or when it plans none, edges go on ``main`` with ``spur`` and
    ``bypass`` lines derived from the sections.

    Warns
    -----
    FeedbackEdgesDroppedWarning
        When the kept graph loops. nf-metro's layout engine requires a DAG, so
        a deterministic set of back edges is removed; the warning and a
        trailing ``%%`` comment block in the output both name them.
    """
    if not kept_ids:
        return "%%metro title: Empty Pipeline\n\ngraph LR\n"

    reconnected = _reconnect_edges(kept_ids, dag.edges)
    cycle_break = _break_cycles(kept_ids, reconnected.edges)
    edges = cycle_break.kept
    dropped_feedback = reconnected.self_loops + cycle_break.removed

    sections = _split_cyclic_sections(
        _assign_sections(kept_ids, dag, title), edges, dag
    )
    section_order = _topological_order(
        list(sections.names.keys()), edges, sections.node_section
    )

    intra_edges, inter_edges = _classify_edges(edges, sections.node_section)
    plan = plan_lines(dag.nodes, edges) if plan_lines else []
    lines = (
        _planned_lines(plan)
        if plan
        else _assign_lines(
            kept_ids,
            edges,
            intra_edges,
            inter_edges,
            sections.node_section,
            sections.names,
            section_order,
        )
    )

    section_node_order = _order_section_nodes(sections.nodes, edges)
    station_ids = _allocate_station_ids(section_order, section_node_order, dag.nodes)

    if not title:
        title = _infer_title(sections.names, section_order)

    if dropped_feedback:
        _warn_feedback_edges_dropped(dropped_feedback, dag.nodes)

    return _emit_mmd(
        title,
        section_order,
        sections.names,
        section_node_order,
        intra_edges,
        lines,
        station_ids,
        dag.nodes,
        dropped_feedback,
    )
