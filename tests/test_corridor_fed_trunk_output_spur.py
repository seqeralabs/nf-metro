"""A corridor-fed section entry must ride its through-chain, not an output spur.

When a LEFT/RIGHT entry port fans directly to a through-chain arm (a station
that reaches the section's exit) and a short off-track ``%%metro file:`` output
spur, the entry port must sit flush with the through-chain arm so the section's
main line rides the entry trunk and the spur peels off below. The single-line
case has no line-superset to distinguish the trunk, so the through-chain arm
(exit-reaching) is what disambiguates it.

An entry that lands off the through-chain has two outward symptoms beyond the
misplaced port: the upstream same-row feed kinks down onto the port instead of
running straight in, and the section can be dragged off its row-mates' baseline.
This is the corridor-fed (cross-row vertical-drop) counterpart of the same-row
trunk selection ``section_trunk_short_output_branch.mmd`` locks (#1487); the
same-row fixture is exercised here too so the invariant generalises across both
entry geometries (#1497).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases.single_section import _exit_reaching_nodes
from nf_metro.layout.routing import route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import PortSide

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

TOL = 2.0


def _layout(name: str):
    graph = parse_metro_mermaid((EXAMPLES / "topologies" / name).read_text())
    compute_layout(graph)
    return graph


def _lr_entry_port(graph, section):
    ports = [
        pid
        for pid in section.entry_ports
        if (port := graph.ports.get(pid))
        and port.side in (PortSide.LEFT, PortSide.RIGHT)
    ]
    assert len(ports) == 1, ports
    return ports[0]


@pytest.mark.parametrize(
    ("fixture", "section_id"),
    [
        ("corridor_fed_trunk_output_spur.mmd", "main_sec"),
        ("section_trunk_short_output_branch.mmd", "main_sec"),
    ],
)
def test_entry_port_rides_through_chain_not_output_spur(fixture, section_id):
    graph = _layout(fixture)
    section = graph.sections[section_id]
    port_id = _lr_entry_port(graph, section)
    port_y = graph.stations[port_id].y

    exit_reaching = _exit_reaching_nodes(graph, section)
    targets = [
        graph.station_for_edge_target(e).id
        for e in graph.edges_from(port_id)
        if not graph.station_for_edge_target(e).is_port
    ]
    through = [t for t in targets if t in exit_reaching]
    spurs = [t for t in targets if t not in exit_reaching]

    assert through, f"no through-chain target among {targets}"
    assert spurs, f"no output-spur target among {targets}"

    for t in through:
        assert abs(graph.stations[t].y - port_y) < TOL, (
            f"through-chain {t} y={graph.stations[t].y} not on entry trunk y={port_y}"
        )
    for s in spurs:
        assert abs(graph.stations[s].y - port_y) > TOL, (
            f"output spur {s} y={graph.stations[s].y} shares the entry trunk "
            f"y={port_y} instead of peeling off"
        )


def test_corridor_same_row_feed_runs_straight_into_port():
    graph = _layout("corridor_fed_trunk_output_spur.mmd")
    section = graph.sections["main_sec"]
    port_id = _lr_entry_port(graph, section)

    same_row_feeds = [
        r
        for r in route_edges(graph)
        if r.edge.target == port_id
        and (src_port := graph.ports.get(r.edge.source)) is not None
        and (src_sec := graph.sections.get(src_port.section_id)) is not None
        and src_sec.grid_row == section.grid_row
    ]
    assert len(same_row_feeds) == 1, same_row_feeds
    ys = [round(p[1], 1) for p in same_row_feeds[0].points]
    assert max(ys) - min(ys) < TOL, (
        f"same-row feed into entry port is not a straight run: ys={ys}"
    )


def test_corridor_section_box_aligns_with_feeder_row_mate():
    graph = _layout("corridor_fed_trunk_output_spur.mmd")
    main = graph.sections["main_sec"]
    feeder = graph.sections["feed_sec"]
    assert main.grid_row == feeder.grid_row
    assert abs(main.bbox_y - feeder.bbox_y) < TOL, (
        f"main section box-top y={main.bbox_y} dragged off its row-mate "
        f"feeder box-top y={feeder.bbox_y}"
    )
