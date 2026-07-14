"""Tests for the exit-row bypass early-up-step invariant (#1215).

A same-row inter-section bypass whose source already sits below the
intervening sections that classified it as a bypass must run straight along
its exit row and turn up once at the target.  Stepping up to a mid-height lane
at the exit, running the long traverse there, then up again into the target is
an avoidable kink whenever the exit row threads clear across the span.

Covers:

* Happy-path: every gallery example, showcase fixture, and topology fixture
  routes without an exit-row bypass stepping up over a clear corridor.
* Regression: the reported ``seqinspector`` map routes the
  FASTQ-Files line to MultiQC straight along its exit row.
* Meaningfulness: the checker fires on the reported up-step geometry and stays
  silent on the straight-run geometry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.invariants import (
    check_exit_row_bypass_no_early_upstep,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
SHOWCASE = EXAMPLES / "showcase"
EXAMPLE_TOPOLOGIES = EXAMPLES / "topologies"
FIXTURE_TOPOLOGIES = REPO_ROOT / "tests" / "fixtures" / "topologies"

REPORTED_FIXTURE = SHOWCASE / "seqinspector.mmd"


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
    return graph, routes


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_no_exit_row_bypass_early_upstep_in_gallery(path: Path) -> None:
    """Every shipped fixture routes same-row bypasses straight along a clear
    exit row, never stepping up to a mid-lane over the corridor."""
    graph, routes = _route(path)
    violations = check_exit_row_bypass_no_early_upstep(graph, routes)
    assert not violations, "\n".join(v.message() for v in violations)


def test_reported_fastq_line_runs_along_its_exit_row() -> None:
    """The FASTQ-Files line to MultiQC runs its long traverse on the exit row.

    The exit/convergence sits at the FASTQ-Files trunk Y; the BAM-Files box
    below drops clear of that row, so the traverse to MultiQC must stay on the
    exit row and climb only at the end -- no mid-lane up-step at the exit.
    """
    graph, routes = _route(REPORTED_FIXTURE)
    src = graph.stations["__junction_5"]
    target = "multiqc__entry_left_4"
    matches = [
        r for r in routes if r.edge.source == "__junction_5" and r.edge.target == target
    ]
    assert matches, "expected a routed FASTQ-Files line into MultiQC"
    route = matches[0]

    longest_dx = 0.0
    traverse_y = src.y
    for (x0, y0), (x1, y1) in zip(route.points, route.points[1:]):
        if abs(y1 - y0) <= 1.0 and abs(x1 - x0) > longest_dx:
            longest_dx = abs(x1 - x0)
            traverse_y = y0
    assert traverse_y == pytest.approx(src.y, abs=1.0), (
        f"traverse ran at y={traverse_y:.1f}, not the exit row y={src.y:.1f}"
    )


def _build_graph():
    graph = parse_metro_mermaid(REPORTED_FIXTURE.read_text())
    compute_layout(graph)
    return graph


_EDGE = Edge("__junction_5", "multiqc__entry_left_4", "fastq_files")

# The reported up-step: leave the exit at y=360, climb to a mid-lane y=275
# (just below the intervening Run-Folder box), run the long traverse there,
# then climb again into MultiQC.
_UP_STEP = [
    (474.0, 360.0),
    (492.0, 275.0),
    (865.0, 275.0),
    (879.0, 178.0),
    (904.0, 164.0),
]
# The straight run: stay on the exit row y=360 across the span, turn up once.
_STRAIGHT = [
    (474.0, 360.0),
    (865.0, 360.0),
    (879.0, 178.0),
    (904.0, 164.0),
]


def _route_with(points: list[tuple[float, float]]) -> list[RoutedPath]:
    return [
        RoutedPath(
            edge=_EDGE,
            line_id="fastq_files",
            points=points,
            is_inter_section=True,
        )
    ]


def test_checker_flags_exit_row_up_step() -> None:
    """The checker fires when the same-row bypass steps up over a clear row."""
    graph = _build_graph()
    violations = check_exit_row_bypass_no_early_upstep(graph, _route_with(_UP_STEP))
    assert violations, "expected an early-up-step violation over the clear corridor"
    assert violations[0].source == "__junction_5"


def test_checker_passes_straight_exit_row_run() -> None:
    """The checker stays silent when the bypass runs straight along its row."""
    graph = _build_graph()
    violations = check_exit_row_bypass_no_early_upstep(graph, _route_with(_STRAIGHT))
    assert not violations, "a straight exit-row run must not flag"
