"""Invariant tests for whole-graph rail-mode inter-section connectors.

When two ``%%metro line_spread: rails`` sections are joined by an inter-section
edge, the dedicated rail router must connect them cleanly: the connector leaves
the upstream exit port outward (never backtracking into the section it just
left) and reaches the downstream entry port from outside, and co-travelling
lines keep their rail order so the connector adds no avoidable crossing.
"""

from __future__ import annotations

import pytest

from nf_metro.layout import compute_layout
from nf_metro.layout.routing.core import route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import LineSpread, PortSide

# Two stacked rail sections joined by an inter-section edge.  The multi-line
# case exercises connector crossings; the single-line case exercises the
# backtracking stub a lone line leaves at its ports.
_TWO_LINE = """\
%%metro line_spread: rails
%%metro line: a | A | #2db572
%%metro line: b | B | #f4a300

graph LR
    subgraph one [Section one]
        s1[Start]
        s2[Middle]
        s1 -->|a,b| s2
    end
    subgraph two [Section two]
        t1[Next]
        t2[End]
        t1 -->|a,b| t2
    end
    s2 -->|a,b| t1
"""

_ONE_LINE = """\
%%metro line_spread: rails
%%metro line: a | A | #2db572

graph LR
    subgraph one [Section one]
        s1[Start]
        s2[Middle]
        s1 -->|a| s2
    end
    subgraph two [Section two]
        t1[Next]
        t2[End]
        t1 -->|a| t2
    end
    s2 -->|a| t1
"""

_CASES = {"two_line": _TWO_LINE, "one_line": _ONE_LINE}


def _rail_graph(text: str):
    graph = parse_metro_mermaid(text)
    assert graph.line_spread is LineSpread.RAILS
    compute_layout(graph, validate=False)
    return graph


def _connector_routes(graph):
    """Routes whose endpoints are both boundary ports of different sections."""
    out = []
    for r in route_edges(graph):
        src = graph.ports.get(r.edge.source)
        tgt = graph.ports.get(r.edge.target)
        if src is None or tgt is None or src.section_id == tgt.section_id:
            continue
        out.append((r, src, tgt))
    return out


@pytest.mark.parametrize("name", list(_CASES))
def test_rail_connector_does_not_backtrack_at_its_ports(name):
    """The connector leaves a side port outward and reaches one from outside.

    A RIGHT exit port is left rightward (the next vertex is at or beyond the
    port X); a LEFT entry port is reached from the left (the prior vertex is at
    or before the port X).  The defective render instead ran the connector back
    *into* the section interior from the port, leaving a dangling stub.
    """
    graph = _rail_graph(_CASES[name])
    connectors = _connector_routes(graph)
    assert connectors, "expected at least one inter-section connector"
    tol = 1.0
    for route, src_port, tgt_port in connectors:
        pts = route.points
        ex, _ = pts[0]
        nx, _ = pts[1]
        if src_port.side is PortSide.RIGHT:
            assert nx >= ex - tol, (
                f"{route.line_id} connector backtracks left out of its RIGHT exit "
                f"port: leaves x={ex:.0f} toward x={nx:.0f}"
            )
        elif src_port.side is PortSide.LEFT:
            assert nx <= ex + tol, (
                f"{route.line_id} connector backtracks right out of its LEFT exit "
                f"port: leaves x={ex:.0f} toward x={nx:.0f}"
            )
        tnx, _ = pts[-1]
        pnx, _ = pts[-2]
        if tgt_port.side is PortSide.LEFT:
            assert pnx <= tnx + tol, (
                f"{route.line_id} connector reaches its LEFT entry port from the "
                f"right: arrives at x={tnx:.0f} from x={pnx:.0f}"
            )
        elif tgt_port.side is PortSide.RIGHT:
            assert pnx >= tnx - tol, (
                f"{route.line_id} connector reaches its RIGHT entry port from the "
                f"left: arrives at x={tnx:.0f} from x={pnx:.0f}"
            )


def test_rail_connector_adds_no_avoidable_crossing():
    """Co-travelling lines keep their rail order through the connector."""
    from layout_validator import Severity, validate_layout

    graph = _rail_graph(_TWO_LINE)
    crossings = [
        v
        for v in validate_layout(graph)
        if v.check == "route_segment_crossing" and v.severity is Severity.WARNING
    ]
    assert not crossings, "\n".join(v.message for v in crossings)
