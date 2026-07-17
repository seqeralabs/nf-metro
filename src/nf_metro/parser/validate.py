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

from nf_metro.errors import NfMetroError
from nf_metro.parser.model import (
    Edge,
    MetroGraph,
    Port,
    UnresolvedEndpointError,
    UnresolvedPortSectionError,
    format_unresolved_endpoint,
    format_unresolved_port_section,
)

ERROR = "error"
WARNING = "warning"


class CyclicGraphError(NfMetroError, ValueError):
    """Raised when a graph the layout engine requires to be a DAG has a cycle."""


def find_unresolved_endpoints(graph: MetroGraph) -> list[tuple[Edge, list[str]]]:
    """Return each edge with an endpoint id absent from ``graph.stations``.

    Detector shared by the layout-boundary raise and the soft ``validate_graph``
    path, mirroring :func:`find_cycle` / :func:`format_cycle_error`.
    """
    return [
        (edge, missing)
        for edge in graph.edges
        if (
            missing := [
                sid for sid in (edge.source, edge.target) if sid not in graph.stations
            ]
        )
    ]


def require_resolved_edge_endpoints(graph: MetroGraph) -> None:
    """Raise at the layout boundary if any edge endpoint fails to resolve."""
    unresolved = find_unresolved_endpoints(graph)
    if unresolved:
        raise UnresolvedEndpointError(
            "; ".join(
                format_unresolved_endpoint(edge, missing)
                for edge, missing in unresolved
            )
        )


def find_unresolved_port_sections(graph: MetroGraph) -> list[Port]:
    """Return each port whose ``section_id`` is absent from ``graph.sections``.

    Detector shared by the layout-boundary raise and the soft ``validate_graph``
    path, mirroring :func:`find_unresolved_endpoints`.
    """
    return [
        port for port in graph.ports.values() if port.section_id not in graph.sections
    ]


def require_resolved_port_sections(graph: MetroGraph) -> None:
    """Raise at the layout boundary if any port's section fails to resolve."""
    unresolved = find_unresolved_port_sections(graph)
    if unresolved:
        raise UnresolvedPortSectionError(
            "; ".join(format_unresolved_port_section(port) for port in unresolved)
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

    for edge, missing in find_unresolved_endpoints(graph):
        issues.append(
            ValidationIssue(
                ERROR, format_unresolved_endpoint(edge, missing), line=edge.source_line
            )
        )

    for port in find_unresolved_port_sections(graph):
        issues.append(ValidationIssue(ERROR, format_unresolved_port_section(port)))

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
