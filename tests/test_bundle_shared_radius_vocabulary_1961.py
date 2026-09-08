"""A concentric bundle fan anchors its innermost lane at ``CURVE_RADIUS``.

Issue #1961.  Two nesting passes each sized an interior concentric corner's
radius reference from the wrong cohort -- ``_restack_htrunk`` from the global
packed-band track index, ``_flanking_reference_radii`` from the max across all
bundle members -- rather than the lines that actually co-turn at that corner.
Both inflate the whole fan uniformly: it stays concentric (the lanes share an
arc centre) but the innermost lane seats one or more steps above
``CURVE_RADIUS`` instead of exactly at it.

The fix re-anchors every wholesale-translated concentric fan so its innermost
lane sits at ``CURVE_RADIUS`` and each outer lane one ``OFFSET_STEP`` wider.
These tests pin the two regression targets to their exact radius sets and the
corpus-wide oracle to the tightened ``== CURVE_RADIUS`` property.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import CURVE_RADIUS
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import apply_route_offsets
from nf_metro.layout.routing.corners import resolve_curve_radii
from nf_metro.layout.routing.invariants import (
    _fan_corner_observations,
    check_bundle_corner_radius_floor,
)
from nf_metro.layout.routing.normalize import _reanchor_concentric_corner_fans
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
FIXTURES = REPO_ROOT / "tests" / "fixtures"


def _route(path: Path):
    graph = parse_metro_mermaid(path.read_text(), max_station_columns=15)
    graph.source_dir = str(path.parent)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return graph, routes, offsets


def _resolved_by_line(path: Path, source: str, target: str) -> dict[str, list[float]]:
    _graph, routes, offsets = _route(path)
    members = [
        route
        for route in routes
        if route.edge.source == source and route.edge.target == target
    ]
    return {
        route.line_id: [
            round(r, 1)
            for r in resolve_curve_radii(
                apply_route_offsets(route, offsets), route.curve_radii
            )
        ]
        for route in members
    }


@pytest.mark.parametrize(
    ("fixture", "source", "target", "expected"),
    [
        pytest.param(
            "examples/topologies/disjoint_sameline_trunks.mmd",
            "secC__exit_right_2",
            "secE__entry_left_7",
            {"a": [14.0, 18.0, 10.0, 14.0], "b": [10.0, 22.0, 14.0, 10.0]},
            id="disjoint_sameline_trunks",
        ),
        pytest.param(
            "examples/topologies/fanout_bundle_plus_spurs.mmd",
            "__junction_4",
            "branch__entry_left_1",
            {
                "a": [18.0, 22.0, 10.0, 10.0],
                "b": [14.0, 18.0, 14.0, 14.0],
                "c": [10.0, 14.0, 18.0, 18.0],
            },
            id="fanout_bundle_plus_spurs",
        ),
    ],
)
def test_concentric_fan_anchors_innermost_at_curve_radius(
    fixture: str, source: str, target: str, expected: dict[str, list[float]]
) -> None:
    """Each interior concentric corner seats its innermost lane at the floor.

    The defect inflates the shared reference so the whole fan nests one or more
    steps too wide; the corrected geometry seats the innermost lane at
    ``CURVE_RADIUS`` and steps each outer lane one ``OFFSET_STEP`` further.
    """
    resolved = _resolved_by_line(REPO_ROOT / fixture, source, target)
    assert resolved == expected


@pytest.mark.parametrize(
    "fixture",
    [
        "examples/topologies/disjoint_sameline_trunks.mmd",
        "examples/topologies/fanout_bundle_plus_spurs.mmd",
        "examples/topologies/packed_multiline_serpentine_grid.mmd",
    ],
)
def test_reanchor_leaves_both_exit_turn_flanking_corners_to_the_plan(
    fixture: str,
) -> None:
    """The re-anchor pass never re-derives a plan-owned exit-turn corner.

    ``_settled_exit_turns`` seats both waypoints flanking the exit-turn segment
    -- ``exit_turn_segment_rank`` and ``exit_turn_segment_rank + 1``.  Neither may
    appear as a fan observation, and a second re-anchoring pass must not move
    either radius.
    """
    _graph, routes, offsets = _route(REPO_ROOT / fixture)
    settled = [
        route
        for route in routes
        if route.exit_turn_segment_rank is not None and route.curve_radii is not None
    ]
    assert settled, "fixture exercises no settled exit turn"

    for route in settled:
        rank = route.exit_turn_segment_rank
        observed = {
            obs.rank
            for obs in _fan_corner_observations(
                route, apply_route_offsets(route, offsets)
            )
        }
        assert rank not in observed and rank + 1 not in observed

    before = {
        id(route): route.curve_radii[
            route.exit_turn_segment_rank - 1 : route.exit_turn_segment_rank + 1
        ]
        for route in settled
    }
    _reanchor_concentric_corner_fans(routes, offsets, CURVE_RADIUS)
    for route in settled:
        rank = route.exit_turn_segment_rank
        assert route.curve_radii[rank - 1 : rank + 1] == before[id(route)]


def _corpus_fixtures() -> list[Path]:
    paths: list[Path] = []
    paths.extend(sorted(EXAMPLES.glob("*.mmd")))
    paths.extend(sorted((EXAMPLES / "topologies").glob("*.mmd")))
    paths.extend(sorted((FIXTURES / "topologies").glob("*.mmd")))
    return paths


@pytest.mark.parametrize("fixture", _corpus_fixtures(), ids=lambda p: p.stem)
def test_no_shipped_fixture_inflates_a_concentric_fan(fixture: Path) -> None:
    """No shipped map seats a concentric fan's innermost lane off ``CURVE_RADIUS``.

    Parametrised over the whole corpus so a cross-edge fan (whose true innermost
    lane lives on a sibling edge) is judged against its full population rather
    than a per-edge fragment.
    """
    graph, routes, offsets = _route(fixture)
    violations = check_bundle_corner_radius_floor(graph, routes, offsets)
    assert violations == [], "\n".join(v.message() for v in violations)
