"""A hub that ends one bundle and starts another hands the trunk over flat.

Two lines cross into a section and run to a hub two stations in.  The hub ends
them and starts two lines of its own that carry on to the next station.  The
engine seats the ending bundle on the lanes it arrives on and seats the starting
bundle on its own lanes beside them, so both the approach into the hub and the
departure out of it draw as level runs with no mid-section step.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import RoutedPath, apply_route_offsets
from nf_metro.parser.mermaid import parse_metro_mermaid

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "examples" / "topologies" / "continuation_lane_step.mmd"

FLAT_RUNS = (
    ("salmon_quant", "quant__exit_right_0", "counts"),
    ("salmon_quant", "quant__exit_right_0", "norm"),
    ("quant__exit_right_0", "__junction_3", "counts"),
    ("quant__exit_right_0", "__junction_3", "norm"),
    ("__junction_3", "diff__entry_left_1", "counts"),
    ("__junction_3", "diff__entry_left_1", "norm"),
    ("diff__entry_left_1", "deseq2", "counts"),
    ("diff__entry_left_1", "deseq2", "norm"),
    ("deseq2", "results_hub", "counts"),
    ("deseq2", "results_hub", "norm"),
    ("results_hub", "publish", "tables"),
    ("results_hub", "publish", "plots"),
)


def _drawn_routes() -> dict[tuple[str, str, str], list[tuple[float, float]]]:
    graph = parse_metro_mermaid(FIXTURE.read_text())
    compute_layout(graph)
    offsets = dict(compute_station_offsets(graph))
    routes: list[RoutedPath] = route_edges(graph, station_offsets=offsets)
    return {
        (route.edge.source, route.edge.target, route.line_id): apply_route_offsets(
            route, offsets
        )
        for route in routes
    }


@pytest.mark.parametrize("run", FLAT_RUNS, ids=lambda run: "-".join(run))
def test_hub_hand_over_draws_flat(run: tuple[str, str, str]) -> None:
    """The approach into the hub and the departure out of it each run level."""
    points = _drawn_routes()[run]
    laterals = {round(y, 3) for _x, y in points}
    assert len(laterals) == 1, f"{run} is not level: {points}"


def test_hub_seats_both_bundles_on_contiguous_lanes() -> None:
    """The ending pair and the starting pair fill four adjacent lanes, no gap."""
    graph = parse_metro_mermaid(FIXTURE.read_text())
    compute_layout(graph)
    offsets = dict(compute_station_offsets(graph))
    hub = {
        line_id: offsets[("results_hub", line_id)]
        for line_id in ("counts", "norm", "tables", "plots")
    }
    ordered = sorted(hub.values())
    step = ordered[1] - ordered[0]
    assert step > 0
    assert all(
        right - left == pytest.approx(step) for left, right in zip(ordered, ordered[1:])
    ), hub
