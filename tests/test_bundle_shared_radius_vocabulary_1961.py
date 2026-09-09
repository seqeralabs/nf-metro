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

from collections.abc import Mapping
from pathlib import Path

import pytest

from nf_metro.layout.constants import CURVE_RADIUS
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import apply_route_offsets
from nf_metro.layout.routing.corners import resolve_curve_radii
from nf_metro.layout.routing.invariants import (
    check_bundle_corner_radius_floor,
    concentric_corner_fans,
)
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
            {"a": [14.0, 10.0, 10.0, 14.0], "b": [10.0, 14.0, 14.0, 10.0]},
            id="disjoint_sameline_trunks",
        ),
        pytest.param(
            "examples/topologies/fanout_bundle_plus_spurs.mmd",
            "__junction_4",
            "branch__entry_left_1",
            {
                "a": [18.0, 18.0, 10.0, 10.0],
                "b": [14.0, 14.0, 14.0, 14.0],
                "c": [10.0, 10.0, 18.0, 18.0],
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


def _resolved(route, offsets: Mapping[tuple[str, str], float]) -> list[float]:
    return [
        round(r, 1)
        for r in resolve_curve_radii(
            apply_route_offsets(route, offsets), route.curve_radii
        )
    ]


def test_exit_turn_corner_is_anchored_apart_while_its_neighbour_re_anchors() -> None:
    """An exit-turn corner is excluded from the fan; the corner beside it belongs to it.

    On ``secC__exit_right_2 -> secE__entry_left_7`` lines ``a`` and ``b`` each own
    an exit turn at their first corner (``exit_turn_segment_rank == 1``), seated by
    ``_settled_exit_turns``.  That corner is excluded from every concentric fan and
    keeps its plan radius (``a`` 14, ``b`` 10).  The neighbouring corner owns no
    exit turn, so it is a fan member and re-anchors to seat the fan's innermost lane
    at ``CURVE_RADIUS`` (``a`` 10, ``b`` 14).  An exclusion wide enough to also drop
    that neighbour -- matching ``rank in (etsr - 1, etsr)`` rather than
    ``rank == etsr`` -- would strand it above the floor.
    """
    path = REPO_ROOT / "examples/topologies/disjoint_sameline_trunks.mmd"
    _graph, routes, offsets = _route(path)
    owners = [
        route
        for route in routes
        if route.edge.source == "secC__exit_right_2"
        and route.edge.target == "secE__entry_left_7"
    ]
    assert {route.line_id for route in owners} == {"a", "b"}
    assert all(route.exit_turn_segment_rank == 1 for route in owners)

    fans = concentric_corner_fans(routes, offsets)
    owner_ids = {id(route) for route in owners}
    observed = [
        (obs.route.line_id, obs.rank)
        for fan in fans
        for obs in fan
        if id(obs.route) in owner_ids
    ]
    assert all(rank != 1 for _line, rank in observed), (
        f"exit-turn corner joined a fan: {sorted(observed)}"
    )
    assert any(rank == 2 for _line, rank in observed), (
        f"exit-turn neighbour dropped from its fan: {sorted(observed)}"
    )

    resolved = {route.line_id: _resolved(route, offsets) for route in owners}
    assert resolved["a"][0] == 14.0 and resolved["b"][0] == 10.0
    assert resolved["a"][1] == 10.0 and resolved["b"][1] == 14.0
    assert min(resolved["a"][1], resolved["b"][1]) == CURVE_RADIUS


def test_mixed_exit_turn_owner_and_plain_sibling_share_one_re_anchored_fan() -> None:
    """A fan mixing an exit-turn owner with a plain sibling floors its inner lane.

    On ``longread_variant_calling`` concentric fans group ``bam``/``svvcf`` (each of
    which owns an exit turn elsewhere on its own route) with ``snvvcf``/``other`` (no
    exit turn) at corners none of them own.  Every such shared fan re-anchors so its
    innermost lane sits exactly at ``CURVE_RADIUS``, and no route's own exit-turn
    corner is ever a fan member.
    """
    _graph, routes, offsets = _route(
        REPO_ROOT / "examples/longread_variant_calling.mmd"
    )
    fans = concentric_corner_fans(routes, offsets)

    mixed = [
        fan
        for fan in fans
        if {obs.route.exit_turn_segment_rank is None for obs in fan} == {True, False}
    ]
    assert mixed, "expected a fan mixing an exit-turn owner with a plain sibling"

    for fan in mixed:
        innermost = min(
            round(_resolved(obs.route, offsets)[obs.rank - 1], 1) for obs in fan
        )
        assert innermost == CURVE_RADIUS

    assert not any(
        obs.rank == obs.route.exit_turn_segment_rank for fan in fans for obs in fan
    ), "an exit-turn corner joined a concentric fan"


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
