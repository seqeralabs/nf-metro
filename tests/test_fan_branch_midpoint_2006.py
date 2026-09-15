"""Regression for #2006: fan-branch side stations sit at their bend midpoint."""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid, parse_metro_mermaid_file

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "curve_invariant_repros"
    / "inter_row_corridor_overflow.mmd"
)
TOL = 1.0


def _layout_fixture():
    graph = parse_metro_mermaid(FIXTURE.read_text())
    graph.source_dir = str(FIXTURE.parent)
    compute_layout(graph)
    return graph


def _route_points(graph, source: str, target: str, line_id: str):
    routes = route_edges(graph, station_offsets=compute_station_offsets(graph))
    for route in routes:
        edge = route.edge
        if edge.source == source and edge.target == target and route.line_id == line_id:
            return route.points
    raise AssertionError(f"missing route for {source}->{target} ({line_id})")


@pytest.mark.parametrize(
    ("source", "station_id", "target", "line_id"),
    [
        ("step_a5", "step_a6", "step_a9", "la"),
        ("step_a5", "step_a8", "step_a9", "la"),
        ("step_f1", "step_f2", "step_f5", "la"),
    ],
    ids=["left-leaning-branch", "mirrored-vertical-branch", "right-leaning-branch"],
)
def test_fan_branch_station_centered_between_diagonal_bends(
    source: str, station_id: str, target: str, line_id: str
):
    graph = _layout_fixture()
    station = graph.stations[station_id]
    incoming = _route_points(graph, source, station_id, line_id)
    outgoing = _route_points(graph, station_id, target, line_id)

    incoming_bend = incoming[-2]
    outgoing_bend = outgoing[1]
    assert abs(incoming_bend[1] - station.y) <= TOL
    assert abs(outgoing_bend[1] - station.y) <= TOL

    midpoint = (incoming_bend[0] + outgoing_bend[0]) / 2.0
    assert abs(station.x - midpoint) <= TOL, (
        f"{station_id} x={station.x:.2f} should sit at midpoint {midpoint:.2f} "
        f"between bends x={incoming_bend[0]:.2f} and x={outgoing_bend[0]:.2f}"
    )


@pytest.mark.parametrize(
    ("fixture", "station_ids"),
    [
        (
            EXAMPLES_DIR / "riboseq_metro.mmd",
            ["sortmerna", "ribodetector", "bowtie2_rrna"],
        ),
        (
            EXAMPLES_DIR / "topologies" / "funcprofiler_upstream.mmd",
            [
                "humann3",
                "humann4",
                "fmhfunprofiler",
                "RGI",
                "mifaser",
                "diamond",
                "eggnog_mapper",
            ],
        ),
    ],
    ids=["riboseq_metro", "funcprofiler_upstream"],
)
def test_multiline_loop_column_mates_share_x(fixture: Path, station_ids: list[str]):
    """A multi-line loop column's on-trunk and off-trunk stations share an X.

    ``sortmerna``/``ribodetector``/``bowtie2_rrna`` fan out from ``bbsplit``
    on three co-travelling lines and rejoin at ``fastqc``; ``humann3`` and its
    six siblings do the same off ``merge``/``multiqc``. The on-trunk member of
    each column must land at the same X as its recentred off-trunk siblings.
    """
    graph = parse_metro_mermaid_file(fixture)
    graph.source_dir = str(fixture.parent)
    compute_layout(graph)

    xs = {sid: graph.stations[sid].x for sid in station_ids}
    assert len(set(round(x, 2) for x in xs.values())) == 1, xs
