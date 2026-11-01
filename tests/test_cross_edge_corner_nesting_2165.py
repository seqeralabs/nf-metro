"""Corners of distinct lines that bend together nest on one arc centre.

Two lines on unrelated edges can turn the same way a whole lane or two apart,
so the eye reads them as one bundle bending; their arcs must then share a
centre, the outer lane one offset step wider per lane.
:func:`check_cross_edge_corners_concentric` is the corpus oracle for that
property, and ``tests/data/cross_edge_corner_nesting_ratchet.txt`` names the
repository maps that fail it.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from functools import cache
from pathlib import Path
from unittest import mock

import cohort_shadow_oracle
import pytest

from nf_metro.api import render_string
from nf_metro.layout.constants import CURVE_RADIUS, OFFSET_STEP
from nf_metro.layout.routing.common import (
    OffsetRegime,
    RoutedPath,
    apply_route_offsets,
)
from nf_metro.layout.routing.corners import resolve_curve_radii
from nf_metro.layout.routing.invariants import (
    NonNestedCornerPair,
    check_cross_edge_corners_concentric,
)
from nf_metro.layout.routing.normalize import _rederive_semantic_end_corners
from nf_metro.parser.model import Edge, MetroGraph
from nf_metro.render import svg

ROOT = Path(__file__).resolve().parents[1]
RATCHET_PATH = ROOT / "tests" / "data" / "cross_edge_corner_nesting_ratchet.txt"

Point = tuple[float, float]


@cache
def _settled(relative_path: str) -> tuple[MetroGraph, list[RoutedPath], dict]:
    """The graph, routes and offsets the renderer draws for *relative_path*."""
    path = ROOT / relative_path
    captured: dict[str, object] = {}
    judge = svg._assert_settled_geometry_is_drawable

    def capture(graph, settled):  # type: ignore[no-untyped-def]
        captured["graph"], captured["settled"] = graph, settled
        return judge(graph, settled)

    with (
        warnings.catch_warnings(),
        mock.patch.object(svg, "_assert_settled_geometry_is_drawable", capture),
    ):
        warnings.simplefilter("ignore")
        render_string(path.read_text(), source_dir=str(path.parent))
    settled = captured["settled"]
    return (
        captured["graph"],  # type: ignore[return-value]
        list(settled.routes),  # type: ignore[attr-defined]
        dict(settled.station_offsets),  # type: ignore[attr-defined]
    )


def _non_nested(relative_path: str) -> list[NonNestedCornerPair]:
    graph, routes, offsets = _settled(relative_path)
    return check_cross_edge_corners_concentric(graph, routes, offsets)


def _radius_at(relative_path: str, line_id: str, corner: Point) -> float:
    """The drawn radius of *line_id*'s corner at *corner*, agreed by every overlay."""
    _graph, routes, offsets = _settled(relative_path)
    radii = set()
    for route in routes:
        if route.line_id != line_id:
            continue
        points = apply_route_offsets(route, offsets)
        resolved = resolve_curve_radii(points, route.curve_radii)
        radii.update(
            round(resolved[rank - 1], 2)
            for rank in range(1, len(points) - 1)
            if abs(points[rank][0] - corner[0]) < 0.5
            and abs(points[rank][1] - corner[1]) < 0.5
        )
    assert len(radii) == 1, f"{line_id} corners at {corner}: {sorted(radii)}"
    return radii.pop()


@pytest.mark.parametrize(
    ("relative_path", "radii"),
    [
        pytest.param(
            "examples/genomeassembly.mmd",
            {("hic_reads", (785.0, 243.0)): 14.0, ("assemblies", (781.0, 239.0)): 10.0},
            id="genomeassembly",
        ),
        pytest.param(
            "examples/topologies/exit_run_three_drop_columns.mmd",
            {("report", (814.0, 328.0)): 14.0, ("main", (818.0, 324.0)): 10.0},
            id="exit_run_three_drop_columns",
        ),
        pytest.param(
            "tests/fixtures/regressions/cross_column_perp_entry_overflow.mmd",
            {
                ("somatic", (578.0, 1557.4)): 18.0,
                ("tumor_only", (582.0, 1561.4)): 14.0,
                ("germline", (586.0, 1565.4)): 10.0,
            },
            id="cross_column_perp_entry_overflow",
        ),
    ],
)
def test_corners_bending_together_across_edges_nest(
    relative_path: str, radii: dict[tuple[str, Point], float]
) -> None:
    """Each lane of a cross-edge bend seats one offset step outside the next.

    The innermost lane keeps ``CURVE_RADIUS``, so the bend is concentric
    without widening the bundle's tightest arc.
    """
    drawn = {key: _radius_at(relative_path, *key) for key in radii}

    assert drawn == radii
    assert min(drawn.values()) == CURVE_RADIUS
    assert _non_nested(relative_path) == []


def _identity(point: Point) -> Point:
    return point


def _mirror_x(point: Point) -> Point:
    return (-point[0], point[1])


def _transpose(point: Point) -> Point:
    return (point[1], point[0])


def _transpose_mirror_y(point: Point) -> Point:
    return (point[1], -point[0])


@pytest.mark.parametrize(
    "orient",
    [
        pytest.param(_identity, id="LR"),
        pytest.param(_mirror_x, id="RL"),
        pytest.param(_transpose, id="TB"),
        pytest.param(_transpose_mirror_y, id="BT"),
    ],
)
def test_cross_edge_bend_nests_in_every_flow_direction(
    orient: Callable[[Point], Point],
) -> None:
    """Two lines on unrelated edges bending one lane apart share an arc centre.

    The pair is the same bend in each flow direction: the drawn radii must not
    depend on which axis the flow runs along or which way it points.
    """

    def route(line_id: str, source: str, target: str, points: list[Point]):
        return RoutedPath(
            edge=Edge(source=source, target=target, line_id=line_id),
            line_id=line_id,
            points=[orient(point) for point in points],
            is_inter_section=True,
            offset_regime=OffsetRegime.BAKED,
            curve_radii=[CURVE_RADIUS],
        )

    outer = route("outer", "a", "b", [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)])
    inner = route(
        "inner",
        "c",
        "d",
        [
            (0.0, OFFSET_STEP),
            (100.0 - OFFSET_STEP, OFFSET_STEP),
            (100.0 - OFFSET_STEP, 100.0),
        ],
    )
    routes = [outer, inner]
    assert check_cross_edge_corners_concentric(MetroGraph(), routes, {})

    _rederive_semantic_end_corners(
        routes,
        CURVE_RADIUS,
        {},
        respect_owned_corners=False,
        cross_edge_offset_step=OFFSET_STEP,
    )

    assert outer.curve_radii == [CURVE_RADIUS + OFFSET_STEP]
    assert inner.curve_radii == [CURVE_RADIUS]
    assert check_cross_edge_corners_concentric(MetroGraph(), routes, {}) == []


def _read_ratchet() -> tuple[str, ...]:
    return tuple(
        line.strip()
        for line in RATCHET_PATH.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    )


def _ordinary_render_fails(relative_path: str) -> bool:
    try:
        _settled(relative_path)
    except Exception:  # noqa: BLE001 - a rejected map carries no corners
        return True
    return False


@pytest.mark.parametrize("relative_path", cohort_shadow_oracle.repository_maps())
def test_non_nested_corner_maps_match_the_ratchet(relative_path: str) -> None:
    """Every repository map with a non-nested corner pair is listed, and no other.

    A listed map that starts nesting is removed from the list in the same
    change, so the list only shrinks.  A map the ordinary render rejects has no
    drawn corners to judge.
    """
    listed = relative_path in _read_ratchet()
    if _ordinary_render_fails(relative_path):
        assert not listed, f"{relative_path} no longer renders; unlist it"
        return
    pairs = _non_nested(relative_path)

    if listed:
        assert pairs, (
            f"{relative_path} nests every cross-edge bend; remove it from "
            f"{RATCHET_PATH.relative_to(ROOT)}"
        )
    else:
        assert not pairs, "\n".join(pair.message() for pair in pairs)


def test_nesting_ratchet_lists_only_repository_maps() -> None:
    listed = _read_ratchet()

    assert list(listed) == sorted(set(listed))
    assert set(listed) <= set(cohort_shadow_oracle.repository_maps())
