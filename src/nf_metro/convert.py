"""Convert Nextflow -with-dag mermaid output to nf-metro .mmd format.

Nextflow's ``-with-dag file.mmd`` produces a ``flowchart TB`` mermaid graph
with channel sources, operator nodes, and process nodes organized into
subworkflow subgraphs. This module parses that format, drops non-process
nodes, reconnects edges, maps subworkflows to sections, detects bypass
lines, and emits a ``graph LR`` nf-metro .mmd file.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import dataclass, field

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


# ---------------------------------------------------------------------------
# Internal data model for the Nextflow DAG
# ---------------------------------------------------------------------------
@dataclass
class _NfNode:
    """A node parsed from the Nextflow mermaid DAG."""

    id: str
    label: str
    shape: str  # "stadium" (process), "square" (value/channel), "circle" (operator)
    subgraph: str | None = None  # named subgraph, None if outside or in " "


@dataclass
class _NfSubgraph:
    """A subgraph parsed from the Nextflow mermaid DAG."""

    full_name: str  # e.g. "NFCORE_RNASEQ:RNASEQ:PREPROCESS"
    short_name: str  # e.g. "PREPROCESS" (from [SHORT_NAME])
    node_ids: list[str] = field(default_factory=list)


@dataclass
class _ParsedDag:
    """Intermediate representation of a parsed Nextflow DAG."""

    nodes: dict[str, _NfNode] = field(default_factory=dict)
    edges: list[tuple[str, str]] = field(default_factory=list)
    subgraphs: dict[str, _NfSubgraph] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Regex patterns for Nextflow mermaid format
# ---------------------------------------------------------------------------

# subgraph "FULL_NAME [SHORT_NAME]" or subgraph " "
_NF_SUBGRAPH = re.compile(
    r'^subgraph\s+"([^"]+)"'
    r"(?:\s*\[([^\]]+)\])?\s*$"
)

# Also handle unquoted: subgraph " "
_NF_SUBGRAPH_SPACE = re.compile(r'^subgraph\s+" "\s*$')

# Stadium node: v1(["LABEL"]) or v1([LABEL]).
# Nextflow <= 22 emitted quoted labels; 23+ emits unquoted ones.
_NF_STADIUM = re.compile(r'^(v\d+)\(\["?([^"\]]*?)"?\]\)\s*$')

# Square bracket node: v1["LABEL"] or v1[LABEL] (same quoting drift).
_NF_SQUARE = re.compile(r'^(v\d+)\["?([^"\]]*?)"?\]\s*$')

# Circle node: v1(( )) or v1(( label ))
_NF_CIRCLE = re.compile(r"^(v\d+)\(\(\s*(.*?)\s*\)\)\s*$")

# Edge: v1 --> v2
_NF_EDGE = re.compile(r"^(v\d+)\s*-->\s*(v\d+)\s*$")

# flowchart header
_NF_FLOWCHART = re.compile(r"^flowchart\s+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _parse_nextflow_mermaid(text: str) -> _ParsedDag:
    """Parse a Nextflow ``-with-dag`` mermaid file into an intermediate DAG."""
    dag = _ParsedDag()
    lines = text.strip().split("\n")

    current_subgraph: str | None = None  # key into dag.subgraphs, or None
    subgraph_counter = 0

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        # flowchart header
        if _NF_FLOWCHART.match(line):
            continue

        # subgraph end
        if line == "end":
            current_subgraph = None
            continue

        # subgraph start (quoted)
        m = _NF_SUBGRAPH.match(line)
        if m:
            full_name = m.group(1).strip()
            # Extract short name from "FULL [SHORT]" pattern
            short_name = m.group(2).strip() if m.group(2) else full_name
            # Also try extracting from the full_name itself if it has [SHORT]
            bracket_m = re.search(r"\[([^\]]+)\]", full_name)
            if bracket_m:
                short_name = bracket_m.group(1).strip()
                full_name = full_name[: bracket_m.start()].strip()

            # Skip space-only subgraphs (channel containers)
            if not full_name.strip() or full_name.strip() == " ":
                current_subgraph = None
                continue

            sg_key = f"sg_{subgraph_counter}"
            subgraph_counter += 1
            dag.subgraphs[sg_key] = _NfSubgraph(
                full_name=full_name, short_name=short_name
            )
            current_subgraph = sg_key
            continue

        # Also match subgraph " " (space-only, no bracket content)
        if _NF_SUBGRAPH_SPACE.match(line):
            current_subgraph = None
            continue

        # Stadium node (process)
        m = _NF_STADIUM.match(line)
        if m:
            node = _NfNode(id=m.group(1), label=m.group(2), shape="stadium")
            if current_subgraph:
                node.subgraph = current_subgraph
                dag.subgraphs[current_subgraph].node_ids.append(node.id)
            dag.nodes[node.id] = node
            continue

        # Square bracket node (value/channel)
        m = _NF_SQUARE.match(line)
        if m:
            node = _NfNode(id=m.group(1), label=m.group(2), shape="square")
            if current_subgraph:
                node.subgraph = current_subgraph
            dag.nodes[node.id] = node
            continue

        # Circle node (operator)
        m = _NF_CIRCLE.match(line)
        if m:
            node = _NfNode(id=m.group(1), label=m.group(2), shape="circle")
            if current_subgraph:
                node.subgraph = current_subgraph
            dag.nodes[node.id] = node
            continue

        # Edge
        m = _NF_EDGE.match(line)
        if m:
            dag.edges.append((m.group(1), m.group(2)))
            continue

    return dag


# ---------------------------------------------------------------------------
# Node classification and edge reconnection
# ---------------------------------------------------------------------------
def _reconnect_edges(
    kept_ids: set[str],
    all_edges: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Reconnect edges through dropped nodes.

    For each kept node, BFS forward through dropped nodes to find
    reachable kept nodes, creating direct edges.
    """
    successors: dict[str, set[str]] = defaultdict(set)
    for src, tgt in all_edges:
        successors[src].add(tgt)

    new_edges: set[tuple[str, str]] = set()

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
                if node != src:  # no self-loops
                    new_edges.add((src, node))
                # Don't continue through kept nodes
            else:
                queue.extend(successors.get(node, set()))

    return sorted(new_edges)


# ---------------------------------------------------------------------------
# Cycle breaking
# ---------------------------------------------------------------------------
def _break_cycles(
    nodes: set[str], edges: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """Remove back edges to break cycles (DFS-based)."""
    adj: dict[str, list[str]] = defaultdict(list)
    for src, tgt in edges:
        adj[src].append(tgt)

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {n: WHITE for n in nodes}
    back_edges: set[tuple[str, str]] = set()

    def dfs(u: str) -> None:
        color[u] = GRAY
        for v in adj[u]:
            if v not in color:
                continue
            if color[v] == WHITE:
                dfs(v)
            elif color[v] == GRAY:
                back_edges.add((u, v))
        color[u] = BLACK

    for n in sorted(nodes):
        if color[n] == WHITE:
            dfs(n)

    if not back_edges:
        return edges
    return [(s, t) for s, t in edges if (s, t) not in back_edges]


# ---------------------------------------------------------------------------
# Section and line assignment
# ---------------------------------------------------------------------------
@dataclass
class _SectionAssignment:
    """Mapping of kept process nodes to sections.

    The three fields are mutually consistent views of the same grouping:
    ``node_section[nid]`` is the section key for node ``nid``, ``names[key]``
    is that section's display name, and ``nodes[key]`` lists the node ids
    in that section.
    """

    node_section: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    nodes: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))


def _assign_sections(
    kept_ids: set[str], dag: _ParsedDag, title: str
) -> _SectionAssignment:
    """Group kept process nodes into sections.

    Processes inside a named subgraph are assigned to that subgraph's section.
    Processes outside any subgraph go into a synthesised section: ``__pipeline``
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
            auto_key = "__pipeline"
            assignment.names[auto_key] = title or "Pipeline"
        else:
            auto_key = "__reporting"
            assignment.names[auto_key] = "Reporting"
        for nid in unassigned:
            assignment.node_section[nid] = auto_key
            assignment.nodes[auto_key].append(nid)

    return assignment


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


def _order_section_nodes(
    section_nodes: dict[str, list[str]], edges: list[tuple[str, str]]
) -> dict[str, list[str]]:
    """Topologically order the nodes within each section (Kahn's algorithm).

    Ordering uses every edge whose endpoints both fall inside the section
    (intra and inter alike). Nodes not reachable by the sort are appended in
    their original order.
    """
    section_node_order: dict[str, list[str]] = {}
    for sec_key, nids in section_nodes.items():
        local_nodes = set(nids)
        local_adj: dict[str, list[str]] = {n: [] for n in nids}
        local_in: dict[str, int] = {n: 0 for n in nids}

        for src, tgt in edges:
            if src in local_nodes and tgt in local_nodes:
                local_adj[src].append(tgt)
                local_in[tgt] += 1

        q = deque(n for n in nids if local_in[n] == 0)
        ordered: list[str] = []
        while q:
            n = q.popleft()
            ordered.append(n)
            for succ in local_adj[n]:
                local_in[succ] -= 1
                if local_in[succ] == 0:
                    q.append(succ)
        for n in nids:
            if n not in ordered:
                ordered.append(n)
        section_node_order[sec_key] = ordered

    return section_node_order


def _topological_order(
    section_ids: list[str],
    edges: list[tuple[str, str]],
    node_section: dict[str, str],
) -> list[str]:
    """Topological ordering of sections based on inter-section edges."""
    # Build section-level DAG
    sec_adj: dict[str, set[str]] = defaultdict(set)
    in_degree: dict[str, int] = {sid: 0 for sid in section_ids}

    for src, tgt in edges:
        src_sec = node_section.get(src)
        tgt_sec = node_section.get(tgt)
        if src_sec and tgt_sec and src_sec != tgt_sec:
            if tgt_sec not in sec_adj[src_sec]:
                sec_adj[src_sec].add(tgt_sec)
                in_degree[tgt_sec] = in_degree.get(tgt_sec, 0) + 1

    # Kahn's algorithm
    queue = deque(sid for sid in section_ids if in_degree.get(sid, 0) == 0)
    result: list[str] = []
    while queue:
        sid = queue.popleft()
        result.append(sid)
        for tgt in sorted(sec_adj.get(sid, set())):
            in_degree[tgt] -= 1
            if in_degree[tgt] == 0:
                queue.append(tgt)

    # Add any remaining (disconnected) sections
    for sid in section_ids:
        if sid not in result:
            result.append(sid)

    return result


_MAX_LABEL_LEN = 16


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
    """Convert a process/section name to a valid nf-metro station ID.

    Lowercase, replace non-alphanumeric with underscore.
    """
    return re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_")


def _allocate_station_ids(
    section_order: list[str],
    section_node_order: dict[str, list[str]],
    nodes: dict[str, _NfNode],
) -> dict[str, str]:
    """Assign a unique station ID to every kept Nextflow node.

    Two `_NfNode`s can share a label (e.g. `SAMTOOLS_SORT` reused across
    several subworkflows). Sanitising the label alone collapses them onto
    one station ID, producing duplicate declarations and self-loop edges
    that crash layout. Walk sections in topological order and suffix
    subsequent occurrences (`samtools_sort`, `samtools_sort_2`, ...).
    """
    used: set[str] = set()
    station_ids: dict[str, str] = {}
    for sec_key in section_order:
        for nid in section_node_order.get(sec_key, []):
            base = _sanitize_id(nodes[nid].label) or "node"
            candidate = base
            suffix = 2
            while candidate in used:
                candidate = f"{base}_{suffix}"
                suffix += 1
            used.add(candidate)
            station_ids[nid] = candidate
    return station_ids


# ---------------------------------------------------------------------------
# Line assignment
# ---------------------------------------------------------------------------
@dataclass
class _LineAssignment:
    """Metro lines and the edge groupings that feed emission.

    ``main`` carries adjacent flow; ``bypass_lines`` keys a ``(src_sec,
    tgt_sec)`` section pair to its ``(id, name, color)``; ``spur`` (when
    present) carries dead-end branches. ``edge_line`` maps every emitted edge
    to its line id, and ``main_inter_edges`` / ``bypass_groups`` partition the
    inter-section edges for emission order.
    """

    main: tuple[str, str, str]
    spur: tuple[str, str, str] | None
    bypass_lines: dict[tuple[str, str], tuple[str, str, str]]
    edge_line: dict[tuple[str, str], str]
    main_inter_edges: list[tuple[str, str]]
    bypass_groups: dict[tuple[str, str], list[tuple[str, str]]]


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

    Dead-end processes (mid-pipeline processes with no successors, excluding
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
    for src_sec, tgt_sec in sorted(bypass_groups.keys()):
        src_name = _humanize_label(section_names[src_sec], abbreviate=False)
        tgt_name = _humanize_label(section_names[tgt_sec], abbreviate=False)
        line_id = _sanitize_id(f"{section_names[src_sec]}_{section_names[tgt_sec]}")
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
    for e in spur_edges:
        if e not in edge_line:
            edge_line[e] = spur_line_id

    return _LineAssignment(
        main=main,
        spur=spur,
        bypass_lines=bypass_lines,
        edge_line=edge_line,
        main_inter_edges=main_inter_edges,
        bypass_groups=bypass_groups,
    )


# ---------------------------------------------------------------------------
# Title inference and emission
# ---------------------------------------------------------------------------
def _infer_title(section_names: dict[str, str], section_order: list[str]) -> str:
    """Derive a pipeline title from the section names."""
    if len(section_names) == 1 and "__pipeline" in section_names:
        return "Pipeline"
    named_secs = [
        _humanize_label(section_names[k])
        for k in section_order
        if not k.startswith("__")
    ]
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
    nodes: dict[str, _NfNode],
) -> str:
    """Assemble the nf-metro ``.mmd`` text from the resolved layout."""
    main_line_id, main_line_name, main_color = lines.main

    out: list[str] = []
    out.append(f"%%metro title: {title}")
    out.append("%%metro style: dark")
    out.append("%%metro line_order: span")
    out.append(f"%%metro line: {main_line_id} | {main_line_name} | {main_color}")
    if lines.spur:
        spur_id, spur_name, spur_color = lines.spur
        out.append(f"%%metro line: {spur_id} | {spur_name} | {spur_color}")
    for (_src_sec, _tgt_sec), (lid, lname, lcolor) in sorted(
        lines.bypass_lines.items(), key=lambda x: x[1][0]
    ):
        out.append(f"%%metro line: {lid} | {lname} | {lcolor}")
    out.append("")
    out.append("graph LR")

    for sec_key in section_order:
        sec_name = section_names[sec_key]
        sec_id = _sanitize_id(sec_name)
        display = _humanize_label(sec_name, abbreviate=False)
        ordered_nodes = section_node_order.get(sec_key, [])

        out.append(f"    subgraph {sec_id} [{display}]")

        for nid in ordered_nodes:
            label = _humanize_label(nodes[nid].label)
            out.append(f"        {station_ids[nid]}([{label}])")

        sec_edges = intra_edges.get(sec_key, [])
        if sec_edges:
            out.append("")
            for src, tgt in sec_edges:
                lid = lines.edge_line.get((src, tgt), main_line_id)
                out.append(f"        {station_ids[src]} -->|{lid}| {station_ids[tgt]}")

        out.append("    end")
        out.append("")

    all_inter = lines.main_inter_edges[:]
    for bp_edges in lines.bypass_groups.values():
        all_inter.extend(bp_edges)

    if all_inter:
        out.append("    %% Inter-section edges")
        for src, tgt in all_inter:
            lid = lines.edge_line.get((src, tgt), main_line_id)
            out.append(f"    {station_ids[src]} -->|{lid}| {station_ids[tgt]}")

    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def convert_nextflow_dag(text: str, title: str = "") -> str:
    """Convert a Nextflow ``-with-dag`` mermaid file to nf-metro ``.mmd`` format.

    Parameters
    ----------
    text:
        Contents of a ``.mmd`` file produced by ``nextflow -with-dag file.mmd``.
    title:
        Optional pipeline title. If empty, inferred from subgraph names.

    Returns
    -------
    str
        nf-metro ``.mmd`` text ready for ``nf-metro render``.
    """
    dag = _parse_nextflow_mermaid(text)

    # Keep process (stadium) nodes, drop everything else
    kept_ids = {nid for nid, node in dag.nodes.items() if node.shape == "stadium"}
    if not kept_ids:
        return "%%metro title: Empty Pipeline\n\ngraph LR\n"

    edges = _reconnect_edges(kept_ids, dag.edges)
    edges = _break_cycles(kept_ids, edges)

    sections = _assign_sections(kept_ids, dag, title)
    section_order = _topological_order(
        list(sections.names.keys()), edges, sections.node_section
    )

    intra_edges, inter_edges = _classify_edges(edges, sections.node_section)
    lines = _assign_lines(
        kept_ids,
        edges,
        intra_edges,
        inter_edges,
        sections.node_section,
        sections.names,
        section_order,
    )

    section_node_order = _order_section_nodes(sections.nodes, edges)
    station_ids = _allocate_station_ids(section_order, section_node_order, dag.nodes)

    if not title:
        title = _infer_title(sections.names, section_order)

    return _emit_mmd(
        title,
        section_order,
        sections.names,
        section_node_order,
        intra_edges,
        lines,
        station_ids,
        dag.nodes,
    )


def is_nextflow_dag(text: str) -> bool:
    """Check whether text looks like a Nextflow ``-with-dag`` mermaid file."""
    stripped = text.strip()
    return stripped.startswith("flowchart ")
