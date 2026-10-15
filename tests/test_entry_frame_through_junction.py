"""A section's entry frame runs on through the junction its exit feeds.

A section whose entry lanes shift to keep a feeder's lanes moves its flow-exit
port with them.  The divergence junction a few pixels past that port takes the
same lanes, and a section downstream of the junction reads its own frame off
the shifted exit, so every leg through the junction runs level.
"""

from __future__ import annotations

import warnings
from pathlib import Path

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result
from nf_metro.themes import resolve_theme

FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "examples"
    / "topologies"
    / "entry_frame_through_junction.mmd"
)


def _polylines() -> dict[tuple[str, str, str], list[tuple[float, float]]]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(FIXTURE.read_text(), source_dir=str(FIXTURE.parent))
        plan = render_graph_result(
            graph, resolve_theme(None, graph, "light"), RenderConfig()
        ).plan
    return {
        (route.edge.source, route.edge.target, route.line_id): list(points)
        for route, points in zip(plan.routes, plan.route_polylines)
    }


def test_shifted_exit_reaches_its_junction_level() -> None:
    legs = {
        key: points
        for key, points in _polylines().items()
        if key[0] == "s11__exit_right_2" and key[1].startswith("__junction")
    }
    assert {line_id for _src, _tgt, line_id in legs} == {"l0", "l3", "l4"}
    for key, points in legs.items():
        assert {y for _x, y in points} == {points[0][1]}, (key, points)


def test_straight_branch_runs_level_from_section_to_section() -> None:
    polylines = _polylines()
    chain = [
        points
        for (source, target, line_id), points in polylines.items()
        if line_id == "l3"
        and (source.startswith("s11__exit") or target.startswith("s14__entry"))
    ]
    assert len(chain) == 2
    assert {y for points in chain for _x, y in points} == {chain[0][0][1]}, chain
