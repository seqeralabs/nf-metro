"""Snakemake front end (experimental): parse ``--rulegraph`` / ``--dag`` output.

Snakemake prints its workflow graph with ``snakemake --rulegraph`` (one node
per rule) or ``--dag`` (one node per job), in DOT (the default) or with
``mermaid-js``. All four exports convert to the same map: job nodes collapse
onto their rule, so a rule appears as one station however many jobs it runs.

The graph export carries no grouping, so every rule lands in one section. The
``all`` target rule only gathers inputs and is dropped. Rule names are used in
full as station labels.

Lines come from the rule graph's shape (see :func:`branch_line_planner`): a
trunk into the main final rule, and a line for each other chain of rules.
"""

from __future__ import annotations

import re
import warnings

import networkx as nx

from nf_metro.convert._core import (
    DagNode,
    ExperimentalInputWarning,
    LineMode,
    ParsedDag,
    _break_cycles,
    _humanize_label,
    dag_to_mmd,
    line_planner,
)
from nf_metro.graph_views import directed_graph

_DOT_HEADER = re.compile(r"^\s*digraph\s+snakemake_dag\b", re.MULTILINE)
_DOT_NODE = re.compile(r'^\s*(\d+)\s*\[\s*label\s*=\s*"((?:[^"\\]|\\.)*)"')
_DOT_EDGE = re.compile(r"^\s*(\d+)\s*->\s*(\d+)\s*;?\s*$")

_MERMAID_FLOWCHART = re.compile(r"^\s*flowchart\s+", re.IGNORECASE | re.MULTILINE)
_MERMAID_NODE = re.compile(r"^\s*(id\d+)\[(.*)\]\s*$")
_MERMAID_EDGE = re.compile(r"^\s*(id\d+)\s*-->\s*(id\d+)\s*$")

# Rule names are Python identifiers; a job label appends its wildcards after
# them (``fastqc\nsample: s1`` in DOT, ``fastqc - sample: s1`` in mermaid-js).
_RULE_NAME = re.compile(r"[A-Za-z_]\w*")

_TARGET_RULE = "all"


def _rule_name(label: str) -> str:
    m = _RULE_NAME.match(label.strip())
    return m.group(0) if m else label.strip()


def _dialect(text: str) -> tuple[re.Pattern[str], re.Pattern[str]] | None:
    """The (node, edge) patterns for *text*'s export syntax, or ``None``."""
    if _DOT_HEADER.search(text):
        return _DOT_NODE, _DOT_EDGE
    if _MERMAID_FLOWCHART.search(text) and any(
        _MERMAID_NODE.match(line) for line in text.splitlines()
    ):
        return _MERMAID_NODE, _MERMAID_EDGE
    return None


def _parse_snakemake(text: str) -> ParsedDag:
    """Collect rule nodes and rule-to-rule edges from an export."""
    node_re, edge_re = _dialect(text) or (_MERMAID_NODE, _MERMAID_EDGE)
    dag = ParsedDag()
    job_rule: dict[str, str] = {}
    rule_node: dict[str, str] = {}

    for line in text.splitlines():
        m = node_re.match(line)
        if m:
            rule = _rule_name(m.group(2))
            if rule not in rule_node:
                nid = f"r{len(rule_node):04d}"
                rule_node[rule] = nid
                dag.nodes[nid] = DagNode(
                    id=nid,
                    label=rule,
                    shape="rule",
                    display=_humanize_label(rule, abbreviate=False),
                )
            job_rule[m.group(1)] = rule_node[rule]
            continue
        m = edge_re.match(line)
        if m and m.group(1) in job_rule and m.group(2) in job_rule:
            src, dst = job_rule[m.group(1)], job_rule[m.group(2)]
            # Two jobs of one rule chained together are not a rule-level loop.
            if src != dst:
                dag.edges.append((src, dst))
    return dag


def _drop_shortcut_edges(dag: ParsedDag) -> None:
    """Drop each edge ``a -> c`` that a longer path ``a -> ... -> c`` covers.

    A rule graph links a rule to every rule it reads from, so a rule that reads
    both an early output and a later one gets an edge past the rules between.
    On a one-line map that edge would be drawn on top of the longer path.
    Loop edges are kept as they are, for :func:`dag_to_mmd` to report.
    """
    cycle_break = _break_cycles(set(dag.nodes), dag.edges)
    reduced = nx.transitive_reduction(directed_graph(dag.nodes, cycle_break.kept))
    loop_edges = set(cycle_break.removed)
    dag.edges = [
        edge for edge in dag.edges if edge in loop_edges or reduced.has_edge(*edge)
    ]


def is_snakemake_dag(text: str) -> bool:
    """Check whether text looks like Snakemake ``--rulegraph``/``--dag`` output."""
    return _dialect(text) is not None


def convert_snakemake_dag(text: str, title: str = "", lines: LineMode = "graph") -> str:
    """Convert Snakemake ``--rulegraph`` or ``--dag`` output to nf-metro ``.mmd``.

    Accepts the DOT and ``mermaid-js`` forms of either export. *lines* chooses
    how lines are drawn (see :data:`LineMode`).

    Warns
    -----
    ExperimentalInputWarning
        Always: the Snakemake converter is experimental.
    FeedbackEdgesDroppedWarning
        When the rule graph loops (see :func:`dag_to_mmd`).

    Raises
    ------
    ValueError
        When *text* holds no Snakemake rules.
    """
    warnings.warn(
        ExperimentalInputWarning(
            "Snakemake input is experimental: the converted map may change "
            "between releases, and complex workflows are likely to need "
            "hand-editing of the converted .mmd."
        ),
        stacklevel=2,
    )
    dag = _parse_snakemake(text)
    if not dag.nodes:
        raise ValueError(
            "No Snakemake rules found. Expected the output of "
            "'snakemake --rulegraph' or '--dag', as DOT or mermaid-js."
        )
    _drop_shortcut_edges(dag)
    has_outgoing = {src for src, _ in dag.edges}
    kept_ids = {
        nid
        for nid, node in dag.nodes.items()
        if not (node.label == _TARGET_RULE and nid not in has_outgoing)
    }
    return dag_to_mmd(dag, kept_ids, title, line_planner(lines))
