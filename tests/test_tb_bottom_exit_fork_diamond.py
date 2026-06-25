"""A junction-fed TOP entry directly below its feeder must drop straight in.

When a TB section's BOTTOM exit forks to two downstream TOP-entry sections in
different grid rows, a fork junction lands in the inter-row gap. The leg into
the nearer section's TOP entry is built by the TOP-entry L-shape handler, whose
junction-source fallback injects a horizontal lead-in. When the junction is fed
straight from directly above (a vertical feeder) and the entry sits straight
below at the same X, that lead-in is a spurious lateral jog: the route departs
the junction sideways, drops, then jogs back onto the port marker, reversing
lateral direction at the section boundary (#1058). The drop must instead stay
in the column.

Encoded two ways: the strict ``compute_layout(validate=True)`` guard
(``_guard_perp_entry_boundary_consistent``) must accept the targeted fixture,
and across every topology fixture each TOP/BOTTOM entry port a single line both
approaches and departs must be crossed at one consistent X.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import check_perp_entry_boundary_consistent
from nf_metro.parser.mermaid import parse_metro_mermaid

TOPOLOGIES_DIR = Path(__file__).parent.parent / "examples" / "topologies"
TOPOLOGY_FILES = sorted(TOPOLOGIES_DIR.glob("*.mmd"))
TOPOLOGY_IDS = [f.stem for f in TOPOLOGY_FILES]


def test_tb_bottom_exit_fork_diamond_renders() -> None:
    """The strict perp-entry boundary guard accepts the diamond fork."""
    graph = parse_metro_mermaid(
        (TOPOLOGIES_DIR / "tb_bottom_exit_fork_diamond.mmd").read_text()
    )
    # Raises PhaseInvariantError if a fork leg reverses laterally at the boundary.
    compute_layout(graph, validate=True)


def test_tb_bottom_exit_fork_leg_drops_straight() -> None:
    """The leg into the nearer TOP entry drops in its column, no lateral jog."""
    graph = parse_metro_mermaid(
        (TOPOLOGIES_DIR / "tb_bottom_exit_fork_diamond.mmd").read_text()
    )
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    entry = graph.stations["mid__entry_top_4"]
    leg = next(
        r
        for r in routes
        if r.edge.target == "mid__entry_top_4"
        and r.edge.source.startswith("__junction")
    )
    xs = {round(x, 1) for x, _ in leg.points}
    assert xs == {round(entry.x, 1)}, (
        f"junction->mid fork leg wanders off the entry column x={entry.x:.1f}: "
        f"visits xs={sorted(xs)}"
    )


@pytest.mark.parametrize("path", TOPOLOGY_FILES, ids=TOPOLOGY_IDS)
def test_perp_entry_boundary_consistent_across_fixtures(path: Path) -> None:
    """No line reverses lateral direction crossing a perp entry port boundary."""
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    violations = check_perp_entry_boundary_consistent(graph, routes)
    assert not violations, "; ".join(v.message() for v in violations)
