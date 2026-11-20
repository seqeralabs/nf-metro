"""Flow-axis re-orientation of a folded section against an inferred LR flow.

#1298 -- a section that declares a flow-axis entry on one side and an exit on
the opposite side reads as reversed even when one of those ports feeds (or is
fed by) the internal flow extreme, so the port does not itself double back.
``_reanchor_flow_axis_ports`` must re-orient the whole section rather than
re-anchor a single port and leave the flow pointing the wrong way.
"""

from __future__ import annotations

from orientation_transform import transformable_reason

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.provenance import DecisionReason, DecisionState

REVERSED_FLOW = "examples/topologies/rl_entry_right_exit_left.mmd"
MULTILINE_REVERSED_FLOW = "examples/topologies/rl_entry_runway.mmd"


def _graph(path: str, *, validate: bool):
    graph = parse_metro_mermaid(open(path).read())
    compute_layout(graph, validate=validate)
    return graph


def test_entry_right_exit_left_reorients_to_rl() -> None:
    """A section fed from the right that exits left resolves to RL flow (#1298).

    The right entry feeds the section's internal flow-sink, so on the inferred
    LR reading it does not double back.  Re-orientation is gated on no flow-axis
    port running with the inferred flow (rather than on every port doubling
    back), so the section resolves to RL and lays out without raising.
    """
    graph = _graph(REVERSED_FLOW, validate=True)
    decision = graph.layout_provenance.direction_decision("mid")

    assert graph.sections["mid"].direction == "RL"
    assert decision is not None
    assert decision.state is DecisionState.INFERRED_THEN_PINNED
    assert decision.reason is DecisionReason.FLOW_REORIENTED_DIRECTION
    assert transformable_reason(graph) == (
        "inferred flow direction: ['feed', 'mid', 'sink']"
    )


def test_reoriented_exit_bundle_keeps_its_feeders_lanes() -> None:
    """A re-oriented section's exit bundle stays in its feeders' lane frame.

    ``src_sec`` flips to RL and both lines leave by its left exit from
    different stations, ``l2`` from a row below ``l1``.  The exit port carries
    each line on the lane its feeder holds it on, so the bundle keeps one frame
    rather than re-centring on the port-nearest line.
    """
    graph = parse_metro_mermaid(open(MULTILINE_REVERSED_FLOW).read())
    compute_layout(graph, validate=True)
    assert graph.layout_provenance.direction_has_reason(
        "src_sec", DecisionReason.FLOW_REORIENTED_DIRECTION
    )
    geometry = {
        station_id: (station.x, station.y)
        for station_id, station in graph.stations.items()
    }
    exit_port = next(
        port_id
        for port_id, port in graph.ports.items()
        if port.section_id == "src_sec" and not port.is_entry
    )
    offsets = compute_station_offsets(graph)
    feeders = {edge.line_id: edge.source for edge in graph.edges_to(exit_port)}

    assert feeders == {"l1": "s2", "l2": "s3"}
    assert {line_id: offsets[(exit_port, line_id)] for line_id in feeders} == {
        "l1": 0.0,
        "l2": 4.0,
    }
    assert {line_id: offsets[(exit_port, line_id)] for line_id in feeders} == {
        line_id: offsets[(source, line_id)] for line_id, source in feeders.items()
    }
    assert {
        station_id: (station.x, station.y)
        for station_id, station in graph.stations.items()
    } == geometry
