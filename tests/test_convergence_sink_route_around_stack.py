"""A convergence-sink feeder folded below stacked branch sections must route
around the intervening boxes, never straight down through them (#1148).

Under a tightened ``fold_threshold`` the branches of a shared-sink topology
stack into one grid column and the sink folds onto a lower row, fed through a
TOP entry port.  Each upper branch's BOTTOM-exit feeder then has to reach that
port past the branch sections stacked between it and the sink; a straight
vertical drop at the exit column ploughs through those boxes.  The feeder must
divert into a clear inter-column gap channel instead, entering boxes only at
declared ports.
"""

from __future__ import annotations

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.normalize import _v_segment_crosses_other_section
from nf_metro.parser.mermaid import parse_metro_mermaid

FIXTURE = "examples/topologies/shared_sink_parallel.mmd"


def _fold(text: str, n: int) -> str:
    return text.replace("graph LR", f"%%metro fold_threshold: {n}\ngraph LR", 1)


def _routes(graph):
    offsets = compute_station_offsets(graph)
    return route_edges(graph, station_offsets=offsets)


@pytest.mark.parametrize("fold", [1, 2, 3])
def test_folded_convergence_sink_validates(fold: int) -> None:
    """The folded shared-sink map lays out without a route-through-section guard
    firing -- the feeders route around the stacked branches."""
    text = _fold(open(FIXTURE).read(), fold)
    # Raises PhaseInvariantError on a feeder that crosses an intervening box.
    compute_layout(parse_metro_mermaid(text), validate=True)


@pytest.mark.parametrize("fold", [1, 2, 3])
def test_folded_feeders_clear_intervening_boxes(fold: int) -> None:
    """No feeder reaching the folded sink's TOP entry has a vertical segment
    that penetrates a section it does not connect to."""
    text = _fold(open(FIXTURE).read(), fold)
    graph = parse_metro_mermaid(text)
    compute_layout(graph, validate=False)
    routes = _routes(graph)
    sink_feeders = [r for r in routes if r.edge.target.startswith("sink__entry")]
    assert sink_feeders, "expected feeders into the folded sink"
    for rp in sink_feeders:
        exclude = {
            sid
            for sid in (
                graph.stations[rp.edge.source].section_id,
                graph.stations[rp.edge.target].section_id,
            )
            if sid
        }
        for (x1, y1), (x2, y2) in zip(rp.points, rp.points[1:]):
            if abs(x1 - x2) > 1.0:
                continue  # not a vertical segment
            assert not _v_segment_crosses_other_section(graph, x1, y1, y2, exclude), (
                f"{rp.edge.source}->{rp.edge.target} drops through a section box"
            )


def test_committed_fixture_validates() -> None:
    """The committed single-line fold fixture (the gallery render) lays out
    without a route-through-section guard firing."""
    text = open("examples/topologies/convergence_sink_fold.mmd").read()
    compute_layout(parse_metro_mermaid(text), validate=True)


def test_adjacent_feeder_stays_straight() -> None:
    """The fix is narrow: the bottommost branch, adjacent to the sink with no
    intervening box, keeps its clean straight drop rather than diverting."""
    text = _fold(open(FIXTURE).read(), 3)
    graph = parse_metro_mermaid(text)
    compute_layout(graph, validate=False)
    adjacent = [
        r
        for r in _routes(graph)
        if r.edge.source.startswith("branch_c__exit")
        and r.edge.target.startswith("sink__entry")
    ]
    assert adjacent, "expected branch_c feeders"
    for rp in adjacent:
        xs = {round(x, 1) for x, _y in rp.points}
        assert len(xs) == 1, f"branch_c feeder diverted: xs={sorted(xs)}"
