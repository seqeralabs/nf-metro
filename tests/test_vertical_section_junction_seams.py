"""Inter-section seams of vertical-flow sections reached through a junction.

* A bundle rising out of a junction into a BOTTOM entry keeps its lines in
  order through every turn, and a source already standing below the target's
  channel turns into the port once instead of climbing a riser too short for
  its corners.
* A vertical-flow section's LEFT/RIGHT exit sits at or past every station
  feeding it, in flow order, whether its consumer is fed directly or through a
  junction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.api import render_string
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.geometry import AxisFrame, lanes_run_along_x
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import check_bundle_order_preserved
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, PortSide

REPO_ROOT = Path(__file__).resolve().parent.parent
REPROS = REPO_ROOT / "tests" / "fixtures" / "curve_invariant_repros"
RISE_FROM_JUNCTION = REPROS / "bt_bottom_entry_rise_from_junction.mmd"
SOURCE_BELOW_CHANNEL = REPROS / "bt_bottom_entry_source_below_channel.mmd"
EXIT_THROUGH_JUNCTION = REPROS / "tb_side_exit_through_junction_into_tb_side_entry.mmd"

CORPUS = sorted(
    p
    for pattern in (
        "examples/*.mmd",
        "examples/topologies/*.mmd",
        "examples/guide/*.mmd",
        "tests/fixtures/*.mmd",
        "tests/fixtures/*/*.mmd",
    )
    for p in REPO_ROOT.glob(pattern)
    if "nextflow" not in p.parts
)


def _laid_out(path: Path) -> MetroGraph:
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    return graph


@pytest.mark.parametrize(
    "path",
    [RISE_FROM_JUNCTION, SOURCE_BELOW_CHANNEL, EXIT_THROUGH_JUNCTION],
    ids=lambda p: p.stem,
)
def test_render_completes(path: Path) -> None:
    render_string(path.read_text(), source_dir=str(path.parent))


def test_bundle_rising_into_bottom_entry_keeps_its_order() -> None:
    graph = _laid_out(RISE_FROM_JUNCTION)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    violations = check_bundle_order_preserved(routes)
    assert violations == [], violations[0].message()


def test_source_below_channel_turns_into_bottom_entry_once() -> None:
    graph = _laid_out(SOURCE_BELOW_CHANNEL)
    offsets = compute_station_offsets(graph)
    routes = [
        route
        for route in route_edges(graph, station_offsets=offsets)
        if route.edge.target == "z1__entry_bottom_2"
        and route.edge.source in graph.junction_ids
    ]
    assert routes
    for route in routes:
        assert len(route.points) == 3, (route.line_id, route.points)


def _exits_upstream_of_feeders(graph: MetroGraph) -> list[str]:
    out = []
    for section in graph.sections.values():
        if not lanes_run_along_x(section.direction):
            continue
        flow = AxisFrame.flow_sign(section.direction)
        for port_id in section.exit_ports:
            port = graph.ports.get(port_id)
            if port is None or port.side not in (PortSide.LEFT, PortSide.RIGHT):
                continue
            exit_y = graph.stations[port_id].y
            for edge in graph.edges_to(port_id):
                feeder = graph.stations.get(edge.source)
                if feeder is None or feeder.is_port:
                    continue
                if (exit_y - feeder.y) * flow < -0.5:
                    out.append(
                        f"{port_id} y={exit_y:.1f} upstream of {edge.source} "
                        f"y={feeder.y:.1f} in {section.direction} {section.id}"
                    )
    return out


@pytest.mark.parametrize("path", CORPUS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_vertical_side_exit_sits_past_its_feeders(path: Path) -> None:
    try:
        graph = parse_metro_mermaid(path.read_text())
        compute_layout(graph)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"does not lay out: {type(exc).__name__}")
    assert _exits_upstream_of_feeders(graph) == []
