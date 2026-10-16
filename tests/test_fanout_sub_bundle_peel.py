"""A fan-out branch that carries several lines peels off as one sub-bundle.

``bt_bottom_entry_rise_from_junction`` fans a TB section's RIGHT exit into two
same-row targets: ``m0`` and ``m2`` rise together into Zone 1's BOTTOM entry,
and ``m1`` runs on under Zone 1 to rise into Zone 2's LEFT entry.  Every branch
peels up off a run below the row, so the nearer sub-bundle has to ride the
run's upper lanes and the farther line its lowest one; any other order lays a
riser across a run that travels on.
"""

from __future__ import annotations

import itertools
import warnings
from pathlib import Path

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result
from nf_metro.layout.fan_ordering import fanout_divergence_peel_order
from nf_metro.parser.model import MetroGraph
from nf_metro.themes import resolve_theme

ROOT = Path(__file__).resolve().parent.parent
RISE = (
    ROOT
    / "tests"
    / "fixtures"
    / "curve_invariant_repros"
    / "bt_bottom_entry_rise_from_junction.mmd"
)
JUNCTION = "__junction_4"

Point = tuple[float, float]


def _render(path: Path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        plan = render_graph_result(
            graph, resolve_theme(None, graph, mode=None), RenderConfig()
        ).plan
    return graph, plan


def _proper_crossings(a: list[Point], b: list[Point]) -> int:
    def orient(p: Point, q: Point, r: Point) -> int:
        v = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return 0 if abs(v) < 1e-6 else (1 if v > 0 else -1)

    return sum(
        1
        for (p1, p2), (q1, q2) in itertools.product(
            itertools.pairwise(a), itertools.pairwise(b)
        )
        if orient(p1, p2, q1) * orient(p1, p2, q2) < 0
        and orient(q1, q2, p1) * orient(q1, q2, p2) < 0
    )


def _priority(graph: MetroGraph) -> dict[str, int]:
    return {line_id: rank for rank, line_id in enumerate(graph.lines)}


def test_rising_fan_orders_the_sub_bundle_inside_the_farther_line() -> None:
    graph, _plan = _render(RISE)
    assert fanout_divergence_peel_order(graph, JUNCTION, _priority(graph)) == [
        "m0",
        "m2",
        "m1",
    ]


def test_sub_bundle_peels_off_without_crossing_the_line_running_on() -> None:
    _graph, plan = _render(RISE)
    branches = [
        (route.line_id, [tuple(p) for p in points])
        for route, points in zip(plan.routes, plan.route_polylines, strict=True)
        if route.edge.source == JUNCTION
    ]
    assert {line_id for line_id, _ in branches} == {"m0", "m1", "m2"}
    crossings = [
        (a_id, b_id)
        for (a_id, a), (b_id, b) in itertools.combinations(branches, 2)
        if a_id != b_id and _proper_crossings(a, b)
    ]
    assert not crossings, crossings


def _arc_centres(points: list[Point], radii: tuple[float, ...]) -> list[Point]:
    centres = []
    for (a, p, b), r in zip(
        zip(points, points[1:], points[2:], strict=False), radii, strict=False
    ):
        u = _unit(a, p)
        v = _unit(p, b)
        centres.append((p[0] - r * u[0] + r * v[0], p[1] - r * u[1] + r * v[1]))
    return centres


def _unit(a: Point, b: Point) -> Point:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = (dx * dx + dy * dy) ** 0.5
    return dx / length, dy / length


def test_sub_bundle_steps_up_on_concentric_corners() -> None:
    """The sub-bundle's peel corner and the step that follows share centres."""
    _graph, plan = _render(RISE)
    centres = {
        route.line_id: _arc_centres([tuple(p) for p in points], route.curve_radii)
        for route, points in zip(plan.routes, plan.route_polylines, strict=True)
        if route.edge.source == JUNCTION and route.line_id in ("m0", "m2")
    }
    for inner, outer in zip(centres["m0"][:2], centres["m2"][:2], strict=True):
        assert abs(inner[0] - outer[0]) <= 0.5
        assert abs(inner[1] - outer[1]) <= 0.5
