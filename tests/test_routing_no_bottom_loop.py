"""Routing invariant: a downward cross-column feeder must not loop to the
canvas bottom and climb back up to reach its consumer.

Regression lock for the `needs_bypass` misfire where an inter-section edge
from a row-0 source to a consumer one row down and 2+ columns across was
classified as a bypass and routed below the target section
(`_route_bypass`) instead of dropping straight into it.
"""

from __future__ import annotations

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.svg import compute_station_offsets


def _pipeline_with_qc_at(col: int, n_top: int) -> str:
    """Top row of ``n_top`` sections; a QC section one row down at ``col``,
    fed by a downward cross-column edge from the row-0 input."""
    tops = ["input", "alignment", "quant", "annot"][:n_top]
    grids = "\n".join(f"%%metro grid: {s} | {i},0" for i, s in enumerate(tops))
    grids += f"\n%%metro grid: qc | {col},1"
    top_blocks = ""
    for i, s in enumerate(tops):
        entry = "" if i == 0 else "        %%metro entry: left | align\n"
        exit_ = "        %%metro exit: right | align\n" if i < n_top - 1 else ""
        extra = "        %%metro exit: bottom | qc\n" if i == 0 else ""
        top_blocks += (
            f"    subgraph {s} [{s.title()}]\n{entry}{exit_}{extra}"
            f"        {s}_a[{s} A]\n        {s}_b[{s} B]\n"
            f"        {s}_a -->|align| {s}_b\n    end\n"
        )
    chain = "\n".join(
        f"    {tops[i]}_b -->|align| {tops[i + 1]}_a" for i in range(n_top - 1)
    )
    return (
        "%%metro line: align | Align | #3b82f6\n"
        "%%metro line: qc | QC | #e6842a\n"
        f"{grids}\n\ngraph LR\n{top_blocks}"
        "    subgraph qc [QC]\n"
        "        %%metro entry: left | qc\n"
        "        qc_a[FastQC]\n        qc_b[MultiQC]\n"
        "        qc_a -->|qc| qc_b\n    end\n"
        f"{chain}\n"
        "    input_a -->|qc| qc_a\n"
    )


@pytest.mark.parametrize(
    "col,n_top",
    [(2, 3), (3, 4)],
    ids=["qc_col2_of_3", "qc_col3_of_4"],
)
def test_downward_feeder_does_not_dip_below_consumer(col, n_top):
    graph = parse_metro_mermaid(_pipeline_with_qc_at(col, n_top))
    compute_layout(graph)
    routes = route_edges(graph, station_offsets=compute_station_offsets(graph))

    qc = graph.sections["qc"]
    qc_bottom = qc.bbox_y + qc.bbox_h

    qc_routes = [
        r
        for r in routes
        if r.is_inter_section and str(r.edge.target).startswith("qc__entry")
    ]
    assert qc_routes, "expected an inter-section route into the QC entry port"

    for r in qc_routes:
        max_y = max(y for _, y in r.points)
        assert max_y <= qc_bottom + 1.0, (
            f"route {r.edge.source}->{r.edge.target} dips to y={max_y:.0f}, "
            f"below QC section bottom={qc_bottom:.0f} (canvas-bottom loop)"
        )
