"""Graph-semantic validation of a parsed :class:`MetroGraph`.

These checks are invariants of the parsed graph itself (every edge must
reference a defined line; every section may only point at stations that
exist). They are independent of layout geometry, which is validated
separately in ``tests/layout_validator.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from nf_metro.parser.model import MetroGraph

ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class ValidationIssue:
    """A single graph-semantic finding.

    ``severity`` is one of :data:`ERROR` or :data:`WARNING`.
    """

    severity: str
    message: str


def validate_graph(graph: MetroGraph) -> list[ValidationIssue]:
    """Return graph-semantic findings for ``graph`` as structured data."""
    issues: list[ValidationIssue] = []

    for edge in graph.edges:
        if edge.line_id != "default" and edge.line_id not in graph.lines:
            issues.append(
                ValidationIssue(
                    ERROR,
                    f"Edge {edge.source} -> {edge.target} references "
                    f"undefined line '{edge.line_id}'",
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
