"""Opposite-running vertical gap bundles keep separate corridors."""

from pathlib import Path

import pytest

from nf_metro.api import prepare_graph
from nf_metro.layout.constants import (
    BUNDLE_TO_BUNDLE_CLEARANCE,
    OFFSET_STEP,
)
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.pass_metrics import (
    min_corridor_y_overlap,
)
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import apply_route_offsets, gap_lo_for_x
from nf_metro.layout.routing.invariants import check_opposing_gap_channel_clearance
from nf_metro.parser.mermaid import parse_metro_mermaid

ROOT = Path(__file__).parent.parent
TOPOLOGIES = ROOT / "examples" / "topologies"
REPORT_HO = ROOT / "tests" / "fixtures" / "route_reservations" / "reportho.metro"


@pytest.mark.parametrize(
    "fixture",
    [
        "dogleg_exempt_distinct.mmd",
        "dogleg_exempt_sameline.mmd",
        "exit_run_three_drop_columns.mmd",
        "merge_around_below_leftmost.mmd",
        "merge_feeder_shared_channel_gap.mmd",
        "packed_cell_right_exit_left_entry_wrap.mmd",
    ],
)
def test_opposing_gap_bundles_are_separated(fixture: str) -> None:
    graph = parse_metro_mermaid((TOPOLOGIES / fixture).read_text())
    compute_layout(graph, validate=False)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    violations = check_opposing_gap_channel_clearance(graph, routes, offsets)

    assert not violations, "; ".join(v.message() for v in violations)

    if fixture != "packed_cell_right_exit_left_entry_wrap.mmd":
        return

    channels: list[tuple[object, float, float, float, bool]] = []
    for route in routes:
        if route.line_id not in {"assembled", "reference", "short"}:
            continue
        if not route.is_inter_section:
            continue
        points = apply_route_offsets(route, offsets)
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            if abs(x1 - x0) >= 0.1:
                continue
            if gap_lo_for_x(graph, x0, min(y0, y1), max(y0, y1)) == (1, 0):
                channels.append((route, x0, min(y0, y1), max(y0, y1), y1 > y0))

    downward = [channel for channel in channels if channel[-1]]
    upward = [channel for channel in channels if not channel[-1]]
    assert downward
    assert upward
    separations = [
        abs(down_x - up_x)
        for down_route, down_x, down_lo, down_hi, _down in downward
        for up_route, up_x, up_lo, up_hi, _up in upward
        if down_route.line_id != up_route.line_id
        and min(down_hi, up_hi) - max(down_lo, up_lo) > min_corridor_y_overlap()
    ]
    assert separations
    assert min(separations) >= (BUNDLE_TO_BUNDLE_CLEARANCE - 0.1)


def test_convergence_flanks_take_lanes_in_a_shared_column_gap() -> None:
    """Two convergence systems turning into one gap each get their own lane.

    Both trunks reach the same section, so each derives a flank column from its
    own geometry and lands near the middle of the gap in front of it. Those
    columns own their geometry, so the post-routing gap passes cannot lane them.
    """
    graph = prepare_graph(REPORT_HO.read_text(), source_dir=str(REPORT_HO.parent))
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    assert not check_opposing_gap_channel_clearance(graph, routes, offsets)

    columns: dict[tuple[str, bool], list[float]] = {}
    for route in routes:
        points = apply_route_offsets(route, offsets)
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            if abs(x1 - x0) >= 0.1 or abs(y1 - y0) < 0.1:
                continue
            if gap_lo_for_x(graph, x0, min(y0, y1), max(y0, y1)) != (4, 0):
                continue
            columns.setdefault((route.line_id, y1 > y0), []).append(x0)

    lanes = {key: xs for key, xs in columns.items() if len(set(xs)) == 1}
    descent = lanes[("report", True)][0]
    climb = lanes[("samplesheets", False)][0]
    carrier = lanes[("main", True)][0]

    assert abs(descent - climb) >= BUNDLE_TO_BUNDLE_CLEARANCE - 0.1
    assert abs(carrier - climb) >= BUNDLE_TO_BUNDLE_CLEARANCE - 0.1
    # The gap's third stream travels with the descent, so laning the two
    # counter-running streams may not park it on top of that descent.
    assert abs(carrier - descent) >= OFFSET_STEP - 0.1
