"""Tests for the direction-agnostic section lane arranger.

The arranger reduces a section's *boundary configuration* -- the order in which
lines cross its determining edge -- to a lane order, so that a line crossing at
edge-slot ``k`` rides lane ``k`` and the bundle runs parallel by construction.
Two LR/RL passes feed it today: fan-out divergence reads the EXIT edge, and
reconvergence reads the ENTRY edge.

The unit tests pin the reduction itself; the fixture tests prove the reduction
is wired into the pipeline and drives the lane order of real shipped diagrams.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets
from nf_metro.layout.routing.arranger import BoundaryConfig, BoundaryEdge, lane_order
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_TOPOLOGIES = REPO_ROOT / "examples" / "topologies"


# ---------------------------------------------------------------------------
# Unit tests: the order-to-lanes reduction
# ---------------------------------------------------------------------------

PRIORITY = {"a": 0, "b": 1, "c": 2, "d": 3}


@pytest.mark.parametrize("edge", [BoundaryEdge.ENTRY, BoundaryEdge.EXIT])
def test_determining_order_takes_the_front_lanes(edge: BoundaryEdge) -> None:
    """Lines crossing the determining edge ride the front lanes in edge order,
    independent of which edge they were read from."""
    config = BoundaryConfig(present=("a", "b", "c"), determining=("c", "a"), edge=edge)
    assert lane_order(config, PRIORITY) == ("c", "a", "b")


def test_unconstrained_lines_fall_to_the_back_in_priority_order() -> None:
    """Lines the determining edge does not pin are appended by priority, not by
    their position in *present*."""
    config = BoundaryConfig(
        present=("d", "c", "b", "a"), determining=("d",), edge=BoundaryEdge.EXIT
    )
    assert lane_order(config, PRIORITY) == ("d", "a", "b", "c")


def test_returns_none_when_already_priority_order() -> None:
    """A determining order that reproduces the plain priority order needs no
    re-slot, signalled by ``None``."""
    config = BoundaryConfig(
        present=("a", "b", "c"), determining=("a", "b"), edge=BoundaryEdge.EXIT
    )
    assert lane_order(config, PRIORITY) is None


def test_determining_lines_absent_from_present_are_ignored() -> None:
    """An edge order naming a line the section does not carry is filtered out
    before it can claim a lane."""
    config = BoundaryConfig(
        present=("a", "b"), determining=("c", "b"), edge=BoundaryEdge.EXIT
    )
    assert lane_order(config, PRIORITY) == ("b", "a")


def test_missing_priority_defaults_to_zero() -> None:
    """Among unconstrained lines, one absent from the priority map sorts as 0,
    ahead of a line with a positive priority."""
    config = BoundaryConfig(
        present=("p", "q", "z"), determining=("z",), edge=BoundaryEdge.EXIT
    )
    # 'z' leads (determining); 'q' (default 0) precedes 'p' (priority 3).
    assert lane_order(config, {"p": 3}) == ("z", "q", "p")


# ---------------------------------------------------------------------------
# Fixture tests: the reduction drives real layout
# ---------------------------------------------------------------------------


def _section_lane_order(path: Path, sec_id: str) -> list[str]:
    """Lines of *sec_id* in lane order (ascending stored offset).

    Reads a representative multi-line station of the section after layout, so
    the order reflects the offsets the arranger assigned.
    """
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    for sid, station in graph.stations.items():
        if station.is_port or station.section_id != sec_id:
            continue
        lines = list(graph.station_lines(sid))
        if len(lines) >= 2:
            return sorted(lines, key=lambda lid: offsets.get((sid, lid), 0.0))
    raise AssertionError(f"no multi-line station found in section {sec_id!r}")


@pytest.mark.parametrize(
    ("fixture", "sec_id", "expected"),
    [
        ("dogleg_twoline_fanout.mmd", "left_tgt", ["to_new", "to_src"]),
        ("clear_channel_target_aware_push.mmd", "src", ["rna", "dna"]),
        ("reconverge_reversed_fold.mmd", "preprocessing", ["rna", "atac", "protein"]),
        ("reconverge_reversed_fold.mmd", "integration", ["rna", "atac", "protein"]),
    ],
)
def test_fanout_source_section_leaves_in_peel_order(
    fixture: str, sec_id: str, expected: list[str]
) -> None:
    """A section feeding a shared fan-out leaves its bundle in the peel order
    the arranger reads off the EXIT edge, so the lines descend without
    crossing."""
    assert _section_lane_order(EXAMPLE_TOPOLOGIES / fixture, sec_id) == expected
