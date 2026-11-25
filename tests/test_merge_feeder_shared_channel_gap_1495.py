"""Fused merge feeders keep a truthful gap-slot declaration (#1495).

When two feeders reach one merge junction and a packed cell-mate pushes their
shared descent into an inter-column gap, :func:`_coincide_same_line_tracks`
fuses the branch feeder's opening descent onto the trunk feeder's descent
column.  The relocation crossed a gap boundary but left the branch feeder's
:class:`GapSlot` declared at its pre-fusion column, so
:func:`check_gap_channels_materialized` saw the relocated leg in a gap with no
matching slot and aborted the render.

The repro is a valid map: two fan sources (``src_fanA``/``src_fanB``) each feed
both ``target`` and ``side_a``; a packed ``left_mate`` cell-mate reshapes the
columns so the shared merge channel lands inside a gap.  The remaining fixtures
are gallery topologies with their own merge-feeder descents, so the invariant
generalises beyond the repro.
"""

from __future__ import annotations

import glob
from pathlib import Path

import pytest

from nf_metro.layout.constants import CURVE_RADIUS
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.layout.routing.common import Edge, RoutedPath
from nf_metro.layout.routing.corners import resolve_curve_radii
from nf_metro.layout.routing.invariants import (
    check_gap_channels_materialized,
    check_orthogonal_turns_form_curves,
)
from nf_metro.parser.mermaid import parse_metro_mermaid_file

ROOT = Path(__file__).resolve().parent.parent
TOPOLOGIES = ROOT / "examples" / "topologies"

FIXTURES = [
    TOPOLOGIES / "merge_feeder_shared_channel_gap.mmd",
    TOPOLOGIES / "wide_fan_in.mmd",
    TOPOLOGIES / "merge_trunk_out_of_range_section.mmd",
]
IDS = [p.stem for p in FIXTURES]

CORPUS = sorted(glob.glob(str(ROOT / "examples" / "**" / "*.mmd"), recursive=True))
CORPUS_IDS = [Path(p).stem for p in CORPUS]


def _route(path: Path) -> tuple:
    graph = parse_metro_mermaid_file(path)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    return graph, routes


@pytest.mark.parametrize("path", FIXTURES, ids=IDS)
def test_no_undeclared_gap_channel(path: Path) -> None:
    graph, routes = _route(path)
    violations = check_gap_channels_materialized(graph, routes)
    assert not violations, "\n".join(v.message() for v in violations)


def test_merge_feeder_down_turn_forms_full_curve() -> None:
    """Fan Source B's down-arm turns onto the descent trunk with full runway.

    The fork at ``__junction_5`` sends line ``a`` down a shared descent trunk;
    the turn from its horizontal lead onto that trunk must clear the source's
    exit by a full curve radius so the corner rounds rather than kinking.
    """
    graph, routes = _route(TOPOLOGIES / "merge_feeder_shared_channel_gap.mmd")
    trunk = next(
        rp
        for rp in routes
        if rp.edge.source == "__junction_5" and rp.edge.target == "__merge_2"
    )
    junction_x = graph.stations["__junction_5"].x
    lead_x, _lead_y = trunk.points[1]
    assert abs(lead_x - junction_x) >= CURVE_RADIUS - 0.5, (
        f"down-turn runway {abs(lead_x - junction_x):.1f}px < curve radius"
    )
    radii = resolve_curve_radii(trunk.points, trunk.curve_radii)
    assert radii[0] >= CURVE_RADIUS - 0.5, f"down-turn radius {radii[0]:.1f} too tight"


def test_merge_feeder_branch_tail_terminates_on_trunk() -> None:
    """The fused branch feeder's tail stops on the trunk, not past its turn.

    ``__junction_4``'s branch fuses onto the trunk's descent column; its tail
    must terminate within the trunk's horizontal traverse rather than
    overshooting the trunk's turn-down as a dead stub.
    """
    graph, routes = _route(TOPOLOGIES / "merge_feeder_shared_channel_gap.mmd")
    trunk = next(
        rp
        for rp in routes
        if rp.edge.source == "__junction_5" and rp.edge.target == "__merge_2"
    )
    branch = next(
        rp
        for rp in routes
        if rp.edge.source == "__junction_4" and rp.edge.target == "__merge_2"
    )
    # The trunk's turn-down column is the western end of its y-traverse; the
    # branch tail must not extend west of it.
    descent_x = branch.points[2][0]
    traverse_y = branch.points[2][1]
    trunk_turn_x = min(x for x, y in trunk.points if abs(y - traverse_y) < 1.0)
    branch_tail_x = branch.points[-1][0]
    assert branch_tail_x >= trunk_turn_x - 0.5, (
        f"branch tail terminates at x={branch_tail_x:.1f}, west of the trunk's "
        f"turn at x={trunk_turn_x:.1f} -- a dangling stub"
    )
    assert branch_tail_x <= descent_x + 0.5


@pytest.mark.parametrize("path", CORPUS, ids=CORPUS_IDS)
def test_orthogonal_turns_form_curves_corpus(path: str) -> None:
    graph, routes = _route(Path(path))
    violations = check_orthogonal_turns_form_curves(graph, routes)
    assert not violations, "\n".join(v.message() for v in violations)


def test_orthogonal_turn_guard_fires_on_starved_corner() -> None:
    """A horizontal lead shorter than a curve radius trips the guard."""
    edge = Edge(source="a", target="b", line_id="x")
    starved = RoutedPath(
        edge=edge,
        line_id="x",
        points=[(0.0, 0.0), (2.0, 0.0), (2.0, 100.0), (50.0, 100.0)],
        is_inter_section=True,
        curve_radii=[CURVE_RADIUS, CURVE_RADIUS],
    )
    violations = check_orthogonal_turns_form_curves(None, [starved])  # type: ignore[arg-type]
    assert violations, "guard missed a starved orthogonal turn"
    assert violations[0].effective < 3.0

    roomy = RoutedPath(
        edge=edge,
        line_id="x",
        points=[(0.0, 0.0), (40.0, 0.0), (40.0, 100.0), (90.0, 100.0)],
        is_inter_section=True,
        curve_radii=[CURVE_RADIUS, CURVE_RADIUS],
    )
    assert not check_orthogonal_turns_form_curves(None, [roomy])  # type: ignore[arg-type]
