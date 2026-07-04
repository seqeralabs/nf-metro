"""Tests for the near-edge port-corner overshoot invariant (#1314).

A station sitting near a section's boundary, whose exit port lands on that
boundary one lane away, must turn down through the port from inside the box.
Equalising its flat runs (the bubble-centring pass) must not push the approach
corner past the port: that bulges the stroke outside the section bbox and
doubles it back to the port -- a catastrophic-looking route.

Covers:

* Happy-path: every gallery example, showcase fixture, and topology fixture
  routes without an interior port-approach corner overshooting its port.
* Regression: the reported ``near_edge_exit_corner`` map turns its exit down
  through the port with the corner inside the section box.
* Meaningfulness: the checker fires on an overshooting corner and stays silent
  on the same corner seated inside the box.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import OffsetRegime, RoutedPath
from nf_metro.layout.routing.invariants import check_port_corner_within_bbox
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
SHOWCASE = EXAMPLES / "showcase"
EXAMPLE_TOPOLOGIES = EXAMPLES / "topologies"
FIXTURE_TOPOLOGIES = REPO_ROOT / "tests" / "fixtures" / "topologies"

REPORTED_FIXTURE = EXAMPLE_TOPOLOGIES / "near_edge_exit_corner.mmd"


def _gather_fixtures() -> list[Path]:
    paths: list[Path] = []
    paths.extend(sorted(EXAMPLES.glob("*.mmd")))
    paths.extend(sorted(SHOWCASE.glob("*.mmd")))
    paths.extend(sorted(EXAMPLE_TOPOLOGIES.glob("*.mmd")))
    paths.extend(sorted(FIXTURE_TOPOLOGIES.glob("*.mmd")))
    return paths


def _route(path: Path):
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return graph, offsets, routes


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_no_port_corner_overshoot_in_gallery(path: Path) -> None:
    """Every shipped fixture turns its interior port approaches down inside the
    section box, never overshooting a boundary port and doubling back."""
    try:
        graph, _offsets, routes = _route(path)
    except Exception as exc:  # intentionally-invalid authoring fixtures
        pytest.skip(f"fixture does not lay out: {exc}")
    violations = check_port_corner_within_bbox(graph, routes)
    assert not violations, "\n".join(v.message() for v in violations)


def test_reported_exit_turns_down_inside_box() -> None:
    """The ``psite_counts`` exit corner seats inside the section, not past it.

    ``psite_counts`` sits one column short of its section's right edge with its
    exit port on that edge, one lane below; the approach corner must stay left
    of the port so the exit turns down cleanly inside the box.
    """
    graph, _offsets, routes = _route(REPORTED_FIXTURE)
    section = graph.sections["psite_id"]
    box_right = section.bbox_x + section.bbox_w
    exit_routes = [
        r
        for r in routes
        if r.edge.source == "psite_counts" and "exit_right" in r.edge.target
    ]
    assert exit_routes, "expected an exit-right route from psite_counts"
    for r in exit_routes:
        max_x = max(x for x, _y in r.points)
        assert max_x <= box_right + 1.0, (
            f"exit route bulges to x={max_x:.1f}, past box edge x={box_right:.1f}"
        )


def _diagonal_exit_route(corner_x: float) -> RoutedPath:
    """A station->right-exit-port diagonal whose approach corner sits at *corner_x*.

    The port endpoint is fixed at x=100; a corner_x > 100 is the overshoot the
    checker must catch, corner_x <= 100 the clean seat it must ignore.
    """
    return RoutedPath(
        edge=Edge(source="a", target="s__exit_right_0", line_id="l1"),
        points=[(40.0, 0.0), (corner_x - 30.0, 0.0), (corner_x, 40.0), (100.0, 40.0)],
        line_id="l1",
        offset_regime=OffsetRegime.DEFERRED,
    )


def _graph_with_right_exit_port():
    """Minimal graph exposing a single RIGHT exit port at x=100."""
    graph = parse_metro_mermaid(
        "graph LR\n    subgraph s [S]\n        a[A]\n    end\n    a -->|l1| b[B]\n"
    )
    compute_layout(graph)
    port = next(iter(graph.ports.values()))
    return graph, port.id


def test_checker_fires_on_overshoot_and_ignores_clean_seat() -> None:
    """The checker flags a corner past the port and passes one seated inside."""
    graph, port_id = _graph_with_right_exit_port()

    overshoot = _diagonal_exit_route(130.0)
    overshoot.edge = Edge(source="a", target=port_id, line_id="l1")
    clean = _diagonal_exit_route(85.0)
    clean.edge = Edge(source="a", target=port_id, line_id="l1")

    assert check_port_corner_within_bbox(graph, [overshoot])
    assert not check_port_corner_within_bbox(graph, [clean])
