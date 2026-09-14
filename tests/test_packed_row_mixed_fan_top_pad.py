"""Row-mate top-padding fairness for a packed row with a mixed fan-in column.

A section's mixed full-bundle + homogeneous-subset fan-in column must receive
the same symmetric top padding as an all-full row-mate, and the shared
inter-section trunk lane must stay fixed for every row-mate in the row.

The shared-boundary-port continuation that keeps those columns' feeders on one
track must also decline to pull a feeder onto a lane its entry port already
occupies: a feeder between that port and its consumer on the port's lane forces
the port's line to bow around it instead of running flat.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from riboseq_map import RIBOSEQ_MMD

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases.fan_bundles import _entry_fan_centre_ports
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import PortSide

_EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


_TOL = 1.0


@pytest.fixture(scope="module")
def graph():
    laid_out = parse_metro_mermaid(RIBOSEQ_MMD)
    compute_layout(laid_out, validate=False)
    return laid_out


@pytest.fixture(scope="module")
def variantbenchmarking_graph():
    text = (_EXAMPLES / "variantbenchmarking.mmd").read_text()
    laid_out = parse_metro_mermaid(text)
    compute_layout(laid_out, validate=False)
    return laid_out


def _top_pad(graph, section_id: str) -> float:
    section = graph.sections[section_id]
    internal = [
        st.y
        for st in graph.stations.values()
        if st.section_id == section_id and not st.is_port and not st.off_track
    ]
    return min(internal) - section.bbox_y


def test_mixed_fan_in_column_gets_row_mate_top_padding(graph):
    orf_pad = _top_pad(graph, "orf_calling")
    te_pad = _top_pad(graph, "te")
    # orf_calling's mixed fan-in must not carry a row-pitch of dead band that
    # its all-full row-mate te does not: their top padding matches.
    assert abs(orf_pad - te_pad) < _TOL, (orf_pad, te_pad)


def test_continuation_stays_on_its_fanned_predecessors_track(graph):
    # star_hybrid -> ribocode is a direct edge; ribocode also takes annotation
    # from the shared entry port, yet must ride star_hybrid's lifted track.
    assert abs(graph.stations["star_hybrid"].y - graph.stations["ribocode"].y) < _TOL


def test_shared_row_trunk_lane_is_preserved(graph):
    # Packed-column row-mates share one inter-section trunk lane. The single
    # exception is a port that center_ports seats on its own fan's vertical
    # midpoint (_entry_fan_centre_ports): its fan geometry outranks the shared
    # lane. Such a port is excluded from the lane set; every other port must
    # coincide on the one shared lane.
    centred = set(_entry_fan_centre_ports(graph))
    lanes = set()
    for sid in ("orf_calling", "psite_id", "te", "reporting"):
        section = graph.sections[sid]
        for pid in section.port_ids:
            if pid in centred:
                continue
            port = graph.ports.get(pid)
            st = graph.stations.get(pid)
            if port and st and port.side in (PortSide.LEFT, PortSide.RIGHT):
                lanes.add(round(st.y, 1))
    assert len(lanes) == 1, lanes
    orf_entry = "orf_calling__entry_left_7"
    assert orf_entry in centred
    assert round(graph.stations[orf_entry].y, 1) not in lanes


def test_feeder_sharing_its_entry_port_lane_keeps_its_own_track(
    variantbenchmarking_graph,
):
    graph = variantbenchmarking_graph
    entry = graph.stations["preprocess__entry_left_6"]
    subsample = graph.stations["subsample"]
    liftover = graph.stations["liftover"]
    # subsample sits on its entry port's lane, between that port and liftover:
    # the collision setup the shared-port continuation must not walk into.
    assert abs(entry.y - subsample.y) < _TOL, (entry.y, subsample.y)
    assert entry.x < subsample.x < liftover.x, (entry.x, subsample.x, liftover.x)
    # liftover must keep its own lane rather than inherit subsample's; pulling it
    # onto the port lane bows the port's line around subsample between them.
    assert abs(liftover.y - subsample.y) > _TOL, (liftover.y, subsample.y)
