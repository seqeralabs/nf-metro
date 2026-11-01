"""A bundle re-slot carried onto a station where its lines rejoin the run.

The walk carrying a re-slot along a bundle follows each line along its own
edges.  A re-slotted line can leave the walked run and reach a station on it
again from elsewhere; at that station it must take its re-slotted lane too, or
the lines that did travel with the run land on lanes that cross or coincide
with it.
"""

from __future__ import annotations

import warnings
from collections.abc import Mapping

import pytest

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result
from nf_metro.parser.model import MetroGraph, PortSide
from nf_metro.render.plan import RenderPlan
from nf_metro.themes import resolve_theme

# ``quant`` (l1) runs sec2 -> s3n0 -> s3n3 on the row; ``annotate`` (l3) parts
# from it at s2n1, bows through s3n2 off the row, and rejoins it at s3n3.
REJOIN_OFF_ROW = """\
%%metro line: l0 | Index | #e6194b
%%metro line: l1 | Quant | #3cb44b
%%metro line: l2 | Sort_Summarise_Export | #4363d8
%%metro line: l3 | Annotate | #f58231
graph LR
    subgraph sec1 [Classify 1]
        s1n2 -->|l1| s1n3
        s1n3 -->|l1| s1n4
    end
    subgraph sec2 [Count 2]
        s2n0 -->|l1,l3| s2n1
    end
    subgraph sec3 [Qc_Count 3]
        s3n0 -->|l1| s3n3
        s3n2 -->|l3| s3n3
    end
    s1n4 -->|l0,l1| s2n0
    s0n3 -->|l1| s1n2
    s2n1 -->|l1| s3n0
    s1n2 -->|l2| s2n0
    s0n3 -->|l3| s2n0
    s2n1 -->|l3| s3n2
"""

# Only ``l1`` reaches sec3's left entry along the re-slotted run; ``l0`` and
# ``l2`` rejoin it there from the junctions they parted at upstream.
REJOIN_AT_ENTRY = """\
%%metro line: l0 | Bin | #e6194b
%%metro line: l1 | Index_Cluster | #3cb44b
%%metro line: l2 | Summarise | #4363d8
%%metro line: l3 | Merge_Gather_Merge | #f58231
graph LR
    subgraph sec0 [Count 0]
        s0n0 -->|l3| s0n1
        s0n1 -->|l1| s0n2
        s0n3 -->|l3| s0n4
        s0n1 -->|l3| s0n3
    end
    subgraph sec1 [Summarise 1]
        s1n0 -->|l1| s1n1
        s1n1 -->|l1| s1n2
    end
    subgraph sec3 [Qc_Vcf 3]
        s3n0 -->|l1| s3n1
    end
    s0n3 -->|l0| s3n0
    s0n3 -->|l0| s2n1
    s0n4 -->|l2| s1n0
    s1n2 -->|l1| s2n1
    s2n1 -->|l1| s3n0
    s1n2 -->|l2| s3n0
    s0n0 -->|l2| s2n0
    s0n4 -->|l3| s2n0
"""


def _render(
    text: str, layout_options: Mapping[str, object] | None
) -> tuple[MetroGraph, RenderPlan]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(text, layout_options=layout_options)
        result = render_graph_result(
            graph, resolve_theme(None, graph, "light"), RenderConfig()
        )
    return graph, result.plan


@pytest.mark.parametrize(
    "layout_options",
    [
        None,
        {"track_gap": 0.5, "section_x_gap": 20.0, "x_spacing": 60.0},
        {"row_align": "top", "x_spacing": 60.0},
    ],
    ids=["default", "narrow", "row_top"],
)
def test_line_rejoining_off_row_keeps_the_runs_order(
    layout_options: Mapping[str, object] | None,
) -> None:
    _graph, plan = _render(REJOIN_OFF_ROW, layout_options)
    offsets = plan.station_offsets
    assert offsets[("s3n3", "l1")] < offsets[("s3n3", "l3")]


def test_lines_rejoining_at_an_entry_take_distinct_lanes() -> None:
    graph, plan = _render(
        REJOIN_AT_ENTRY,
        {
            "section_x_gap": 80.0,
            "section_y_gap": 20.0,
            "diamond_style": "symmetric",
            "x_spacing": 60.0,
        },
    )
    entry = next(
        port_id
        for port_id, port in graph.ports.items()
        if port.section_id == "sec3" and port.is_entry and port.side is PortSide.LEFT
    )
    assert sorted(graph.station_lines(entry)) == ["l0", "l1", "l2"]
    lanes = [plan.station_offsets[(entry, lid)] for lid in graph.station_lines(entry)]
    assert len(set(lanes)) == len(lanes)
