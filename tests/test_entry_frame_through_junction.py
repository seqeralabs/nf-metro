"""A section's entry frame runs on through the junction its exit feeds.

A section whose entry lanes shift to keep a feeder's lanes moves its flow-exit
port with them.  The divergence junction a few pixels past that port takes the
same lanes, and a section downstream of the junction reads its own frame off
the shifted exit, so every leg through the junction runs level.  A section fed
straight on that cannot take the shift leaves the junction on its own lanes.
"""

from __future__ import annotations

import warnings
from pathlib import Path

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result
from nf_metro.themes import resolve_theme

FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "examples"
    / "topologies"
    / "entry_frame_through_junction.mmd"
)


STRANDED_BRANCH_MMD = """\
%%metro line: l0 | Cluster | #e6194b
%%metro line: l1 | Merge_Index | #3cb44b
%%metro line: l2 | Classify_Trim | #4363d8
%%metro line: l3 | Quant | #f58231

graph LR
    subgraph sec0 [Count_Trim 0]
        s0n0 -->|l0,l2| s0n2
    end
    subgraph sec1 [Sort_Vcf 1]
        s1n0 -->|l3| s1n1
    end
    subgraph sec2 [Cluster_Fastqc 2]
        s2n1[polish_index]
        s2n0 -->|l3| s2n4
    end
    subgraph sec3 [Gather_Index_Vcf 3]
        s3n1[align]
    end
    s0n2 -->|l0,l2| s2n0
    s1n1 -->|l1| s2n1
    s1n1 -->|l1| s3n1
    s0n2 -->|l3| s1n0
"""


def _polylines(
    text: str | None = None,
) -> dict[tuple[str, str, str], list[tuple[float, float]]]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = (
            prepare_graph(FIXTURE.read_text(), source_dir=str(FIXTURE.parent))
            if text is None
            else prepare_graph(text)
        )
        plan = render_graph_result(
            graph, resolve_theme(None, graph, "light"), RenderConfig()
        ).plan
    return {
        (route.edge.source, route.edge.target, route.line_id): list(points)
        for route, points in zip(plan.routes, plan.route_polylines)
    }


def test_shifted_exit_reaches_its_junction_level() -> None:
    legs = {
        key: points
        for key, points in _polylines().items()
        if key[0] == "s11__exit_right_2" and key[1].startswith("__junction")
    }
    assert {line_id for _src, _tgt, line_id in legs} == {"l0", "l3", "l4"}
    for key, points in legs.items():
        assert {y for _x, y in points} == {points[0][1]}, (key, points)


def test_straight_branch_runs_level_from_section_to_section() -> None:
    polylines = _polylines()
    chain = [
        points
        for (source, target, line_id), points in polylines.items()
        if line_id == "l3"
        and (source.startswith("s11__exit") or target.startswith("s14__entry"))
    ]
    assert len(chain) == 2
    assert {y for points in chain for _x, y in points} == {chain[0][0][1]}, chain


def test_junction_keeps_its_lanes_where_a_straight_branch_cannot_follow() -> None:
    """Sec1's frame stops at its junction: sec2's entry also seats sec0's bypass.

    Carried past the junction, the shift would put ``l1`` on the lane sec2's
    entry gives the climbing ``l0`` until it stepped across, so the straight
    branch into sec2 runs level off the junction's own lanes instead.
    """
    legs = [
        points
        for (source, target, _line_id), points in _polylines(
            STRANDED_BRANCH_MMD
        ).items()
        if source.startswith("__junction")
        and target.startswith("sec2__entry")
        and len(points) == 2
    ]
    assert legs
    for points in legs:
        assert {y for _x, y in points} == {points[0][1]}, points
