"""Routing invariant: a RIGHT entry fed from the left reaches its descent
corridor at the top corner, not partway down the descent.

When a source sits LEFT of a RIGHT entry port one or more rows below and the
descent corridor just past the target's right edge admits a clean straight drop
from the source row, the line must step onto that corridor X at the top corner
and descend straight. Stepping onto the corridor X mid-descent leaves a visible
lateral kink the straight drop-in would have avoided.

The invariant is gated on the corridor being clear: a wrap whose straight drop
is obstructed genuinely needs its inter-row staging channel and is skipped.

Regression lock for #1178 (the `qc` align feed into the Report section on
`fold_bypass_creep.mmd` jogging 14px sideways a third of the way down a shared
x=230 descent corridor).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import (
    check_right_entry_corridor_descent_no_jog,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

TOPOLOGIES_DIR = Path(__file__).parent.parent / "examples" / "topologies"

# Fixtures exercising cross-row RIGHT-entry wraps.
_FIXTURES = [
    "fold_bypass_creep",
    "convergence_stacked_sink",
    "right_entry_wrap_no_fan",
]


def _corridor_jog(mmd: str):
    """First RIGHT-entry corridor-descent jog over a clear drop, or ``None``."""
    graph = parse_metro_mermaid(mmd)
    compute_layout(graph)
    routes = route_edges(graph, station_offsets=compute_station_offsets(graph))
    violations = check_right_entry_corridor_descent_no_jog(graph, routes)
    return violations[0] if violations else None


@pytest.mark.parametrize("stem", _FIXTURES)
def test_right_entry_corridor_descent_has_no_mid_run_jog(stem):
    path = TOPOLOGIES_DIR / f"{stem}.mmd"
    jog = _corridor_jog(path.read_text())
    assert jog is None, jog.message() if jog else ""


def test_fold_bypass_creep_qc_feed_descends_straight():
    """The qc align feed into Report descends its corridor as one straight run.

    It steps to the corridor X at the source lead-in Y and holds that X all the
    way to the entry turn, so the shared descent reads as a single vertical line.
    """
    graph = parse_metro_mermaid((TOPOLOGIES_DIR / "fold_bypass_creep.mmd").read_text())
    compute_layout(graph)
    routes = route_edges(graph, station_offsets=compute_station_offsets(graph))

    feed = next(
        r
        for r in routes
        if r.line_id == "qc"
        and "junction" in r.edge.source
        and r.edge.target == "report__entry_right_3"
    )
    pts = feed.points
    descent_x = pts[-2][0]
    lead_in_y = pts[0][1]
    # The route reaches the corridor X at the top corner (the second point) and
    # every interior point up to the entry turn sits on it: one straight run.
    assert pts[1][0] == pytest.approx(descent_x, abs=1.0)
    assert pts[1][1] == pytest.approx(lead_in_y, abs=1.0)
    interior_xs = [x for x, _ in pts[1:-1]]
    assert all(x == pytest.approx(descent_x, abs=1.0) for x in interior_xs), pts
