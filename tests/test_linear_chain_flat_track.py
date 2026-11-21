"""Frozen seed contracts for straight chains and ordered fan-out turns."""

from __future__ import annotations

import warnings
from pathlib import Path

from nf_metro.api import prepare_graph, resolve_theme
from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.svg import build_observed_render_plan

SEED_72 = Path(__file__).parent / "fixtures" / "hash_seed_determinism" / "seed_72.mmd"
BYPASS_TWO_LINE = (
    Path(__file__).parent.parent
    / "examples"
    / "topologies"
    / "bypass_left_entry_from_right.mmd"
)


def _laid_out_seed_72():
    graph = parse_metro_mermaid(SEED_72.read_text())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        compute_layout(graph)
    return graph


def _observed(path: Path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        return build_observed_render_plan(graph, resolve_theme(None, graph))


def _route(observed, source: str, target: str, line_id: str):
    return next(
        route
        for route in observed.plan.routes
        if route.edge.source == source
        and route.edge.target == target
        and route.edge.line_id == line_id
    )


def _route_from(observed, source: str, line_id: str):
    return next(
        route
        for route in observed.plan.routes
        if route.edge.source == source and route.edge.line_id == line_id
    )


def _first_vertical_x(points: tuple[tuple[float, float], ...]) -> float:
    return next(
        start[0]
        for start, end in zip(points, points[1:])
        if start[0] == end[0] and start[1] != end[1]
    )


def test_seed_72_linear_chains_hold_one_track() -> None:
    graph = _laid_out_seed_72()

    assert [graph.stations[sid].y for sid in ("n3_0", "n3_1", "n3_2")] == [
        506.0,
        506.0,
        506.0,
    ]
    assert [graph.stations[sid].y for sid in ("n7_0", "n7_1", "n7_2", "n7_3")] == [
        656.0,
        656.0,
        656.0,
        656.0,
    ]


def test_seed_72_s2_exit_and_junction_keep_distinct_level_frames() -> None:
    observed = _observed(SEED_72)
    exit_port = "s2__exit_right_1"
    junction = "__junction_15"
    expected = {"l0": 0.0, "l3": 4.0, "l6": 8.0}

    assert {
        line_id: (
            observed.plan.station_offsets[(exit_port, line_id)],
            observed.plan.station_offsets[(junction, line_id)],
        )
        for line_id in expected
    } == {line_id: (offset, offset) for line_id, offset in expected.items()}

    stubs = {
        line_id: _route(observed, exit_port, junction, line_id).points
        for line_id in expected
    }
    assert stubs == {
        line_id: ((530.0, 324.0 + offset), (540.0, 324.0 + offset))
        for line_id, offset in expected.items()
    }
    assert stubs["l0"] != stubs["l3"]


def test_seed_72_s5_source_order_maps_to_descending_turn_columns() -> None:
    observed = _observed(SEED_72)
    exit_port = "s5__exit_right_3"
    junction = "__junction_16"
    lines = ("l1", "l2", "l3")
    source_y = {
        line_id: _route(observed, exit_port, junction, line_id).points[0][1]
        for line_id in lines
    }
    turn_x = {
        line_id: _first_vertical_x(_route_from(observed, junction, line_id).points)
        for line_id in lines
    }

    assert source_y == {"l1": 324.0, "l3": 328.0, "l2": 332.0}
    assert turn_x == {"l1": 850.0, "l3": 846.0, "l2": 842.0}


def test_two_line_off_row_fan_keeps_peel_order() -> None:
    """``main`` leaves the fork on the upper lane and peels off first, so it
    descends one lane outside ``side``, in the order the two left the fork."""
    observed = _observed(BYPASS_TWO_LINE)
    junction = "__junction_3"
    lanes = {
        line_id: (
            _route_from(observed, junction, line_id).points[0][1],
            _first_vertical_x(_route_from(observed, junction, line_id).points),
        )
        for line_id in ("main", "side")
    }

    assert lanes["side"][0] - lanes["main"][0] == 4.0
    assert lanes["side"][1] - lanes["main"][1] == 4.0
