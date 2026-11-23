"""Nextflow front end: parse ``-with-dag`` mermaid output.

Nextflow's ``-with-dag file.mmd`` produces a ``flowchart TB`` mermaid graph
with channel sources, operator nodes, and process nodes organised into
subworkflow subgraphs. Process nodes become stations and subworkflows become
sections.
"""

from __future__ import annotations

import re

from nf_metro.convert._core import _DagGroup, _DagNode, _ParsedDag, dag_to_mmd

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
            dag.subgraphs[sg_key] = _DagGroup(
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
            node = _DagNode(id=m.group(1), label=m.group(2), shape="stadium")
            if current_subgraph:
                node.subgraph = current_subgraph
                dag.subgraphs[current_subgraph].node_ids.append(node.id)
            dag.nodes[node.id] = node
            continue

        # Square bracket node (value/channel)
        m = _NF_SQUARE.match(line)
        if m:
            node = _DagNode(id=m.group(1), label=m.group(2), shape="square")
            if current_subgraph:
                node.subgraph = current_subgraph
            dag.nodes[node.id] = node
            continue

        # Circle node (operator)
        m = _NF_CIRCLE.match(line)
        if m:
            node = _DagNode(id=m.group(1), label=m.group(2), shape="circle")
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


def process_node_labels(text: str) -> list[str]:
    """Labels of the process nodes in a Nextflow ``-with-dag`` flowchart.

    Nextflow renders processes as stadium nodes; channel and operator nodes are
    excluded. Deduplicated and sorted. Used by the live-progress check-mapping
    linter to learn which processes a pipeline actually runs.
    """
    dag = _parse_nextflow_mermaid(text)
    return sorted({n.label for n in dag.nodes.values() if n.shape == "stadium"})


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

    Warns
    -----
    FeedbackEdgesDroppedWarning
        When the pipeline's process graph loops (see :func:`dag_to_mmd`).
    """
    dag = _parse_nextflow_mermaid(text)
    kept_ids = {nid for nid, node in dag.nodes.items() if node.shape == "stadium"}
    return dag_to_mmd(dag, kept_ids, title)


def is_nextflow_dag(text: str) -> bool:
    """Check whether text looks like a Nextflow ``-with-dag`` mermaid file."""
    stripped = text.strip()
    return stripped.startswith("flowchart ")
