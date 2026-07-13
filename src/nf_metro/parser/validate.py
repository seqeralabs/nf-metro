"""Graph-semantic validation of a parsed :class:`MetroGraph`.

These checks are invariants of the parsed graph itself (every edge must
reference a defined line; every section may only point at stations that
exist). They are independent of layout geometry, which is validated
separately in ``tests/layout_validator.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import networkx as nx

from nf_metro.parser.model import MetroGraph, UnresolvedEndpointError

ERROR = "error"
WARNING = "warning"


class CyclicGraphError(ValueError):
    """Raised when a graph the layout engine requires to be a DAG has a cycle."""


def require_resolved_edge_endpoints(graph: MetroGraph) -> None:
    """Enforce, once over the whole edge list, that every endpoint resolves.

    Called at the layout boundary alongside the DAG check so a malformed graph
    fails with the full list of offending edges before any handler runs, rather
    than each handler re-checking ``graph.stations.get(...)`` for ``None``.
    """
    dangling = [
        f"{edge.source} -> {edge.target} (line '{edge.line_id}')"
        for edge in graph.edges
        if edge.source not in graph.stations or edge.target not in graph.stations
    ]
    if dangling:
        raise UnresolvedEndpointError(
            "Edges reference unresolved station(s): " + "; ".join(dangling)
        )


def find_cycle(graph: MetroGraph) -> list[str] | None:
    """Return a node sequence witnessing a cycle, or ``None`` if acyclic.

    The returned path closes back on its first node (``["a", "b", "a"]`` for a
    two-node cycle, ``["a", "a"]`` for a self-loop) so it reads directly as
    ``a -> b -> a`` when joined.
    """
    g: nx.DiGraph[str] = nx.DiGraph()
    for edge in graph.edges:
        g.add_edge(edge.source, edge.target)
    if nx.is_directed_acyclic_graph(g):
        return None
    cycle_edges = nx.find_cycle(g)
    nodes = [source for source, _ in cycle_edges]
    nodes.append(cycle_edges[0][0])
    return nodes


def format_cycle_error(witness: list[str]) -> str:
    """Render a cycle witness path as a fatal error message."""
    return f"Graph contains a cycle: {' -> '.join(witness)}"


@dataclass(frozen=True)
class ValidationIssue:
    """A single graph-semantic finding.

    ``severity`` is one of :data:`ERROR` or :data:`WARNING`.
    ``line`` is the 1-based source line number where the problem originates,
    or ``None`` when the position is not tracked.
    """

    severity: str
    message: str
    line: int | None = None

    def format(self, path: Path | str | None = None) -> str:
        """Format as a compiler-style diagnostic: ``[path:]line: message``."""
        prefix = f"{path}:" if path else ""
        if self.line is not None:
            return f"{prefix}line {self.line}: {self.message}"
        return self.message


def validate_graph(graph: MetroGraph) -> list[ValidationIssue]:
    """Return graph-semantic findings for ``graph`` as structured data."""
    issues: list[ValidationIssue] = []

    witness = find_cycle(graph)
    if witness is not None:
        issues.append(ValidationIssue(ERROR, format_cycle_error(witness)))

    for edge in graph.edges:
        if edge.line_id != "default" and edge.line_id not in graph.lines:
            issues.append(
                ValidationIssue(
                    ERROR,
                    f"Edge {edge.source} -> {edge.target} references "
                    f"undefined line '{edge.line_id}'",
                    line=edge.source_line,
                )
            )
        for role, sid in (("source", edge.source), ("target", edge.target)):
            if sid not in graph.stations:
                issues.append(
                    ValidationIssue(
                        ERROR,
                        f"Edge {edge.source} -> {edge.target} references "
                        f"unknown {role} station '{sid}'",
                        line=edge.source_line,
                    )
                )

    for section in graph.sections.values():
        for sid in section.station_ids:
            if sid not in graph.stations:
                issues.append(
                    ValidationIssue(
                        ERROR,
                        f"Section '{section.name}' references unknown station '{sid}'",
                    )
                )

    return issues
