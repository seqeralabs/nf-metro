"""Each entry port's lane order has exactly one owner within an offsets run.

The entry-port order family (7c-7f) runs before the approach-derived order
(7b') and claims the ports it orders or judges; 7b' refuses a re-deal that
would change a claimed port, sits in a section phase 10 reverses, would be
reordered by a phase 12 compaction seed, or disagrees with a geometry-owning
fan plan.  Every later phase then holds the order 7b' decided: phase 12 and
14b decline a write that would break it (counted), and every other phase
fails closed.  So one run of the phases settles every port.
"""

from __future__ import annotations

import sys
import warnings
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from nf_metro import candidate_executor
from nf_metro.api import render_string
from nf_metro.layout import routing as routing_package
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import offsets as offsets_module
from nf_metro.layout.routing.common import apply_route_offsets, graph_offset_step
from nf_metro.layout.routing.invariants import CurveInvariantError
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph
from nf_metro.render import svg

REGRESSIONS = Path(__file__).parent / "fixtures" / "regressions"
TOPOLOGIES = Path(__file__).parent.parent / "examples" / "topologies"
DECIDED_FIXTURE = REGRESSIONS / "approach_order_shared_line_feeders.mmd"
DECIDED_PORT = "sec2__entry_left_3"

# A 14b exchange inside sec2 carries the l1/l2 pair out through
# ``sec2__exit_right_2``; past ``__junction_11`` only l1 runs on into sec5,
# whose entry port holds a 7b' order.
SEAM_MAP = """\
%%metro title: Fuzz 8500838
%%metro style: nfcore
%%metro line: l0 | Merge_Sort | #e6194b
%%metro line: l1 | Export | #3cb44b
%%metro line: l2 | Normalize | #4363d8
%%metro line: l3 | Gather | #f58231
%%metro line: l4 | Stats | #911eb4
%%metro line: l5 | Normalize | #46f0f0

graph LR
    subgraph sec0 [Align 0]
        s0n0[align_bam]
        s0n1[merge_vcf_fastqc]
        s0n2[assemble_quant]
        s0n0 -->|l0| s0n1
        s0n0 -->|l4| s0n1
        s0n0 -->|l5| s0n1
        s0n1 -->|l0| s0n2
        s0n1 -->|l2| s0n2
        s0n1 -->|l4| s0n2
        s0n1 -->|l5| s0n2
    end
    subgraph sec1 [Bin_Bam_Bam 1]
        s1n0[assemble]
        s1n1[cluster_normalize]
        s1n2[bin_fastqc_call]
        s1n0 -->|l0| s1n1
        s1n0 -->|l5| s1n1
        s1n1 -->|l0| s1n2
        s1n1 -->|l5| s1n2
        s1n0 -->|l2| s1n2
        s1n0 -->|l4| s1n2
    end
    subgraph sec2 [Align 2]
        s2n0[merge_gather]
        s2n1[stats_sort_fastqc]
        s2n2[quant_report]
        s2n3[classify_qc]
        s2n0 -->|l0| s2n1
        s2n0 -->|l1| s2n1
        s2n0 -->|l2| s2n1
        s2n0 -->|l3| s2n1
        s2n0 -->|l4| s2n1
        s2n1 -->|l0| s2n2
        s2n1 -->|l1| s2n2
        s2n1 -->|l2| s2n2
        s2n1 -->|l3| s2n2
        s2n1 -->|l4| s2n2
        s2n1 -->|l4| s2n2
        s2n2 -->|l0| s2n3
        s2n2 -->|l1| s2n3
        s2n2 -->|l2| s2n3
        s2n2 -->|l3| s2n3
        s2n2 -->|l4| s2n3
        s2n0 -->|l5| s2n3
    end
    subgraph sec3 [Quant 3]
        s3n0[annotate]
        s3n1[stats_classify]
        s3n0 -->|l0| s3n1
        s3n0 -->|l1| s3n1
        s3n0 -->|l2| s3n1
        s3n0 -->|l3| s3n1
        s3n0 -->|l4| s3n1
        s3n0 -->|l4| s3n1
    end
    subgraph sec4 [Align_Filter 4]
        s4n0[DEDUP]
        s4n1[summarise]
        s4n2[gather]
        s4n0 -->|l1| s4n2
        s4n0 -->|l5| s4n1
        s4n1 -->|l5| s4n2
    end
    subgraph sec5 [Markdup_Quant 5]
        s5n0[VCF_QC_QUANT]
        s5n1[fastqc_index]
        s5n2[sort_quant]
        s5n1 -->|l0| s5n2
        s5n1 -->|l1| s5n2
        s5n1 -->|l2| s5n2
        s5n0 -->|l0| s5n2
        s5n0 -->|l5| s5n1
    end
    s0n2 -->|l0| s1n0
    s0n2 -->|l2| s1n0
    s0n2 -->|l4| s1n0
    s0n2 -->|l5| s1n0
    s1n2 -->|l0| s2n0
    s1n2 -->|l2| s2n0
    s1n2 -->|l4| s2n0
    s1n2 -->|l5| s2n0
    s2n3 -->|l0| s3n0
    s2n3 -->|l1| s3n0
    s2n3 -->|l2| s3n0
    s2n3 -->|l3| s3n0
    s2n3 -->|l4| s3n0
    s3n1 -->|l0| s4n2
    s4n2 -->|l0| s5n1
    s2n0 -->|l0| s5n1
    s2n0 -->|l1| s5n1
    s3n1 -->|l1| s4n0
    s3n1 -->|l2| s4n1
    s4n1 -->|l2| s5n1
    s0n1 -->|l4| s5n0
    s2n3 -->|l5| s4n0
    s4n2 -->|l5| s5n0
"""

# A 7b' re-deal here is refused by a phase 12 seed the re-deal's carry never
# reaches.  The map aborts later, in routing.
OFF_CARRY_SEED_MAP = """\
%%metro title: Fuzz 8400566
%%metro line: l0 | Stats | #e6194b
%%metro line: l1 | Quant | #3cb44b
%%metro line: l2 | Pca_Vcf_Report | #4363d8
%%metro line: l3 | Bin | #f58231
%%metro grid: sec0 | 0,0
%%metro grid: sec1 | 1,0
%%metro grid: sec2 | 2,0
%%metro grid: sec3 | 3,0
%%metro grid: sec4 | 0,1

graph LR
    subgraph sec0 [Polish_Trim 0]
        s0n0[normalize_pca]
        s0n1[markdup]
        s0n2[BAM]
        s0n3[MERGE_QUANT_FILTER]
        s0n0 -->|l2,l3| s0n1
        s0n1 -->|l2,l3| s0n2
        s0n2 -->|l2,l3| s0n3
    end
    subgraph sec1 [Stats_Fastqc 1]
        s1n0[vcf]
        s1n2[dedup_annotate_normalize]
        s1n3[count]
        s1n4[ASSEMBLE_GATHER]
        s1n0 -->|l2| s1n2
        s1n2 -->|l2| s1n3
        s1n3 -->|l2| s1n4
    end
    subgraph sec2 [Vcf 2]
        %%metro exit: top | l1, l2
        s2n0[export_vcf]
        s2n1[fastqc_bin_filter]
        s2n2[export_qc_gather]
        s2n3[call]
        s2n0 -->|l1| s2n1
        s2n1 -->|l1| s2n2
        s2n2 -->|l1,l2| s2n3
    end
    subgraph sec3 [Summarise 3]
        %%metro direction: TB
        %%metro entry: left | l1, l3
        s3n0[cluster]
        s3n1[polish]
        s3n2[QUANT_GATHER]
        s3n3[annotate]
        s3n4[normalize_quant]
        s3n0 -->|l1| s3n1
        s3n1 -->|l1| s3n2
        s3n2 -->|l1| s3n4
        s3n0 -->|l3| s3n2
        s3n2 -->|l3| s3n3
        s3n3 -->|l3| s3n4
    end
    subgraph sec4 [Count_Export_Sort 4]
        s4n0[fastqc]
        s4n1[classify_filter_pca]
        s4n2[dedup_pca]
        s4n3[quant]
        s4n0 -->|l0,l1,l2,l3| s4n1
        s4n1 -->|l0| s4n3
        s4n1 -->|l0,l2,l3| s4n2
        s4n2 -->|l2,l3| s4n3
    end
    s3n0 -->|l0| s4n0
    s3n0 -->|l0| s4n1
    s2n3 -->|l1| s3n0
    s2n2 -->|l1| s4n0
    s0n3 -->|l2| s1n0
    s1n4 -->|l2| s2n2
    s2n3 -->|l2| s4n0
    s0n3 -->|l3| s3n0
    s3n4 -->|l3| s4n0
"""


# The l1/l3 pair a 14b exchange moves enters a section holding a decided entry
# order together.  The map aborts later, in routing.
TOGETHER_MAP = """\
%%metro title: Fuzz 8401653
%%metro line: l0 | Align | #e6194b
%%metro line: l1 | Cluster | #3cb44b
%%metro line: l2 | Summarise_Classify | #4363d8
%%metro line: l3 | Cluster | #f58231
%%metro line: l4 | Call_Markdup_Merge | #911eb4

graph LR
    subgraph sec0 [Fastqc 0]
        s0n0[export_summarise]
        s0n1[trim_quant_vcf]
        s0n2[vcf]
        s0n3[index_classify]
        s0n4[sort_annotate]
        s0n0 -->|l1,l2,l3| s0n1
        s0n1 -->|l1,l2| s0n2
        s0n2 -->|l1| s0n4
        s0n2 -->|l2| s0n3
        s0n3 -->|l2,l3| s0n4
        s0n1 -->|l3| s0n3
    end
    subgraph sec1 [Dedup 1]
        s1n0[COUNT_INDEX_POLISH]
        s1n1[classify]
        s1n0 -->|l1| s1n1
    end
    subgraph sec2 [Gather_Index 2]
        %%metro entry: left | l1
        %%metro entry: top | l3
        %%metro exit: right | l0, l1, l3
        s2n0[classify]
        s2n1[report_vcf_dedup]
        s2n2[vcf_sort]
        s2n3[summarise]
        s2n0 -->|l0,l1,l3| s2n1
        s2n1 -->|l1,l3| s2n2
        s2n2 -->|l1,l3| s2n3
    end
    subgraph sec3 [Markdup 3]
        s3n0[markdup]
        s3n1[trim]
        s3n2[classify_sort_sort]
        s3n0 -->|l0,l1,l2| s3n1
        s3n1 -->|l0,l1,l2,l3| s3n2
    end
    subgraph sec4 [Sort_Trim_Align 4]
        s4n0[polish_merge_gather]
        s4n1[dedup_classify_cluster]
        s4n2[report]
        s4n3[gather]
        s4n0 -->|l0,l2,l3,l4| s4n1
        s4n1 -->|l0,l3,l4| s4n2
        s4n2 -->|l0,l3,l4| s4n3
        s4n1 -->|l2| s4n3
    end
    subgraph sec5 [Quant_Index 5]
        %%metro entry: top | l0, l1, l2, l3
        %%metro entry: bottom | l4
        s5n0[dedup]
        s5n1[pca]
        s5n0 -->|l0,l1,l2,l3| s5n1
    end
    s2n1 -->|l0| s3n0
    s3n2 -->|l0,l2,l3| s4n0
    s4n3 -->|l0,l2,l3,l4| s5n0
    s4n2 -->|l0,l4| s5n1
    s4n1 -->|l0| s5n1
    s0n4 -->|l1,l3| s1n0
    s1n1 -->|l1| s2n0
    s2n3 -->|l1| s3n0
    s3n2 -->|l1| s5n0
    s0n4 -->|l2| s3n0
    s1n0 -->|l3| s2n0
    s2n3 -->|l3| s3n1
"""

Lanes = Mapping[tuple[str, str], float]
Point = tuple[float, float]


def _laid_out(text: str) -> MetroGraph:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = parse_metro_mermaid(text)
        compute_layout(graph)
    return graph


def _run_once(graph: MetroGraph) -> Any:
    return offsets_module._run_offset_phases(graph, graph_offset_step(graph))


def _holds(offsets: Lanes, port_id: str, order: Sequence[str]) -> bool:
    lanes = [offsets[(port_id, lid)] for lid in order]
    return all(b > a for a, b in zip(lanes, lanes[1:]))


def _swap_first_decided_pair(ctx: Any) -> None:
    port_id, order = next(iter(ctx.approach_orders.items()))
    a, b = (port_id, order[0]), (port_id, order[1])
    ctx.offsets[a], ctx.offsets[b] = ctx.offsets[b], ctx.offsets[a]


def _callers(monkeypatch: pytest.MonkeyPatch, name: str) -> list[str]:
    """Record the name of each function that calls ``offsets.<name>``."""
    seen: list[str] = []
    original = getattr(offsets_module, name)

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(sys._getframe(1).f_code.co_name)
        return original(*args, **kwargs)

    monkeypatch.setattr(offsets_module, name, spy)
    return seen


class _Render:
    """One render's offsets runs, offsets calls, and drawn routes."""

    def __init__(self) -> None:
        self.ctxs: list[Any] = []
        self.offsets_calls = 0
        self.drawn: tuple[MetroGraph, list[Any], Lanes] | None = None

    def drawn_ctx(self) -> Any:
        assert self.drawn is not None
        offsets = self.drawn[2]
        return next(ctx for ctx in reversed(self.ctxs) if ctx.offsets == offsets)


def _render(
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    layout_options: dict[str, Any] | None = None,
    aborts: type[Exception] | None = None,
) -> _Render:
    record = _Render()
    run = offsets_module._run_offset_phases

    def spy_run(*args: Any) -> Any:
        ctx = run(*args)
        record.ctxs.append(ctx)
        return ctx

    compute = offsets_module.compute_station_offsets

    def spy_compute(*args: Any, **kwargs: Any) -> Any:
        record.offsets_calls += 1
        return compute(*args, **kwargs)

    check = svg.assert_render_curve_invariants

    def capture(graph: MetroGraph, routes: list[Any], offsets: Lanes) -> None:
        record.drawn = (graph, routes, offsets)
        check(graph, routes, offsets)

    monkeypatch.setattr(offsets_module, "_run_offset_phases", spy_run)
    for module in (offsets_module, routing_package, svg, candidate_executor):
        monkeypatch.setattr(module, "compute_station_offsets", spy_compute)
    monkeypatch.setattr(svg, "assert_render_curve_invariants", capture)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            render_string(text, layout_options=layout_options)
        except Exception as exc:
            if aborts is None or not isinstance(exc, aborts):
                raise
    return record


# -- drawn-route seam check -------------------------------------------------

_INWARD = {"LEFT": (1.0, 0.0), "RIGHT": (-1.0, 0.0), "TOP": (0.0, 1.0)}
_INWARD["BOTTOM"] = (0.0, -1.0)
_TOL = 0.5


def _left_of(direction: Point, point: Point) -> float:
    return direction[0] * point[1] - direction[1] * point[0]


def _segments_cross(p1: Point, p2: Point, p3: Point, p4: Point) -> bool:
    def orient(a: Point, b: Point, c: Point) -> int:
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 0 if abs(v) < 1e-6 else (1 if v > 0 else -1)

    return (
        orient(p1, p2, p3) * orient(p1, p2, p4) < 0
        and orient(p3, p4, p1) * orient(p3, p4, p2) < 0
    )


def _polylines_cross(a: Sequence[Point], b: Sequence[Point]) -> bool:
    return any(
        _segments_cross(a[i], a[i + 1], b[j], b[j + 1])
        for i in range(len(a) - 1)
        for j in range(len(b) - 1)
    )


def _pair_defects(
    lines: Mapping[str, list[Point]], start: Point, end: Point
) -> Iterator[tuple[str, str, str]]:
    """Line pairs whose left-of-travel order differs between the two ends of
    their runs, or whose runs cross."""
    ids = sorted(lid for lid in lines if lines[lid])
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            pa, pb = lines[a], lines[b]
            first = _left_of(start, pa[0]) - _left_of(start, pb[0])
            last = _left_of(end, pa[-1]) - _left_of(end, pb[-1])
            if (first > _TOL and last < -_TOL) or (first < -_TOL and last > _TOL):
                yield "order", a, b
            if _polylines_cross(pa, pb):
                yield "cross", a, b


def _seam_defects(
    graph: MetroGraph, routes: Sequence[Any], offsets: Lanes
) -> list[tuple[str, ...]]:
    """Order changes and crossings across each exit-to-entry connector, on the
    in-section hop beside each port, and between arrival and departure at a
    port, read on the drawn routes."""
    polylines: dict[tuple[str, str, str], list[Point]] = {}
    targets: dict[tuple[str, str], list[str]] = defaultdict(list)
    for route in routes:
        key = (route.edge.source, route.edge.target, route.line_id)
        if key not in polylines:
            polylines[key] = [
                (float(x), float(y)) for x, y in apply_route_offsets(route, offsets)
            ]
            targets[(route.edge.source, route.line_id)].append(route.edge.target)

    def passes_through(sid: str) -> bool:
        station = graph.stations.get(sid)
        return (
            station is not None
            and sid not in graph.ports
            and (sid in graph.junctions or station.section_id is None)
        )

    defects: list[tuple[str, ...]] = []
    for exit_id, exit_port in graph.ports.items():
        if exit_port.is_entry:
            continue
        connectors: dict[str, dict[str, list[Point]]] = defaultdict(dict)
        for lid in graph.station_lines(exit_id):
            stack: list[tuple[str, list[Point]]] = [(exit_id, [])]
            walked: set[tuple[str, str]] = set()
            while stack:
                cur, points = stack.pop()
                for tgt in targets.get((cur, lid), ()):
                    if (cur, tgt) in walked:
                        continue
                    walked.add((cur, tgt))
                    run = points + polylines[(cur, tgt, lid)]
                    tgt_port = graph.ports.get(tgt)
                    if tgt_port is not None and tgt_port.is_entry:
                        connectors[tgt].setdefault(lid, run)
                    elif passes_through(tgt):
                        stack.append((tgt, run))
        outward = tuple(-c for c in _INWARD[exit_port.side.name])
        for entry_id, lines in connectors.items():
            inward = _INWARD[graph.ports[entry_id].side.name]
            for kind, a, b in _pair_defects(lines, outward, inward):  # type: ignore[arg-type]
                defects.append(("connector", kind, exit_id, entry_id, a, b))

    for port_id, port in graph.ports.items():
        inward = _INWARD[port.side.name]
        travel = inward if port.is_entry else (-inward[0], -inward[1])
        hops: dict[str, dict[str, list[Point]]] = defaultdict(dict)
        for (src, tgt, lid), points in polylines.items():
            if port.is_entry and src == port_id and tgt not in graph.ports:
                hops[tgt][lid] = points
            if not port.is_entry and tgt == port_id and src not in graph.ports:
                hops[src][lid] = points
        for other, lines in hops.items():
            for kind, a, b in _pair_defects(lines, travel, travel):
                defects.append(("hop", kind, port_id, other, a, b))

        lateral = 1 if port.side.name in ("LEFT", "RIGHT") else 0
        arrive = {
            lid: pts[-1][lateral]
            for (_s, tgt, lid), pts in polylines.items()
            if tgt == port_id and pts
        }
        depart = {
            lid: pts[0][lateral]
            for (src, _t, lid), pts in polylines.items()
            if src == port_id and pts
        }
        common = [lid for lid in arrive if lid in depart]
        if len(common) >= 2 and sorted(common, key=arrive.__getitem__) != sorted(
            common, key=depart.__getitem__
        ):
            defects.append(("port", "flip", port_id))
    return defects


# -- tests ------------------------------------------------------------------


def test_a_split_exchange_keeps_every_seam_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 14b exchanges that restore sec1 -> sec2's order land, the l1/l2
    exchange stops short of sec5's decided port, and no connector, hop or port
    changes order or crosses on the drawn routes."""
    record = _render(monkeypatch, SEAM_MAP)

    assert record.drawn is not None
    graph, routes, offsets = record.drawn
    assert _seam_defects(graph, routes, offsets) == []
    ctx = record.drawn_ctx()
    order = ctx.approach_orders["sec5__entry_left_9"]
    assert _holds(offsets, "sec5__entry_left_9", order)
    assert offsets[("s2n0", "l2")] == 4.0


def test_exchange_does_not_enter_a_decided_section_one_line_reaches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the junction only l1 runs on into sec5, so with sec5's entry order
    decided the walk leaves sec5 alone; with nothing decided it takes it."""
    record = _render(monkeypatch, SEAM_MAP)
    ctx = record.drawn_ctx()
    graph = ctx.graph
    sec5 = set(graph.sections["sec5"].station_ids)

    def exchanged(approach_orders: dict[str, tuple[str, ...]]) -> set[str]:
        trial = replace(ctx, offsets=dict(ctx.offsets), approach_orders=approach_orders)
        offsets_module._exchange_pair_beyond_exit(
            trial, graph.sections["sec2"], "sec2__exit_right_2", "l2", "l1"
        )
        return {
            sid
            for (sid, lid), off in trial.offsets.items()
            if off != ctx.offsets[(sid, lid)]
        }

    assert "sec5__entry_left_9" in ctx.approach_orders
    assert not exchanged(dict(ctx.approach_orders)) & sec5
    assert exchanged({}) & sec5


def test_each_offsets_call_runs_the_phases_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _render(
        monkeypatch,
        (REGRESSIONS / "approach_order_overridden_later.mmd").read_text(),
        {"line_order": "span", "row_align": "top"},
    )

    assert record.offsets_calls > 0
    assert len(record.ctxs) == record.offsets_calls


def test_phase_12_seeds_come_from_one_candidate_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """7b' judges a re-deal by phase 12's own seeds, not a copy of them."""
    seen = _callers(monkeypatch, "_fan_port_compaction_seeds")
    _run_once(_laid_out(DECIDED_FIXTURE.read_text()))

    assert set(seen) == {
        "_recompact_fan_port_bordering_stations",
        "_compaction_seed_reordering",
    }


def test_a_seed_off_the_carry_refuses_a_re_deal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every phase 12 seed is judged on the re-deal, including one the
    re-deal's carry never reaches."""
    off_carry: list[tuple[str, str]] = []
    judge = offsets_module._trial_overrides_an_owner
    reorders = offsets_module._compaction_seed_reordering
    touched_now: list[Sequence[str]] = []

    def spy_judge(ctx: Any, port: Any, touched: Sequence[str], *args: Any) -> Any:
        touched_now.append(touched)
        return judge(ctx, port, touched, *args)

    def spy_reorders(*args: Any) -> Any:
        seed = reorders(*args)
        if seed is not None and seed[1] not in touched_now[-1]:
            off_carry.append(seed)
        return seed

    monkeypatch.setattr(offsets_module, "_trial_overrides_an_owner", spy_judge)
    monkeypatch.setattr(offsets_module, "_compaction_seed_reordering", spy_reorders)
    _render(monkeypatch, OFF_CARRY_SEED_MAP, aborts=CurveInvariantError)

    assert off_carry


_FAIL_CLOSED = [
    ("8", "_reconcile_horizontal_offsets", 1),
    ("8b", "_align_flat_tb_exit_to_entry", 1),
    ("9", "_recenter_partial_fan_branches", 1),
    ("10", "_reverse_near_vertical_junction_right_entry_offsets", 1),
    ("12a", "_restore_fanout_peel_order", 1),
    ("13", "_reconcile_horizontal_offsets", 2),
    ("14", "_center_rail_boundary_port_bundles", 1),
    ("14a", "_recenter_single_line_corridor_entry", 2),
    ("14c", "_close_section_dead_lanes", 1),
    ("14d", "_apply_planned_fan_offsets", 1),
    ("14e", "_deal_perp_entry_columns", 1),
    ("15", "_materialize_linear_entry_frames", 1),
]


@pytest.mark.parametrize(
    ("phase", "name", "nth"), _FAIL_CLOSED, ids=[p[0] for p in _FAIL_CLOSED]
)
def test_a_later_phase_that_breaks_a_decided_order_fails_closed(
    monkeypatch: pytest.MonkeyPatch, phase: str, name: str, nth: int
) -> None:
    graph = _laid_out(DECIDED_FIXTURE.read_text())
    original = getattr(offsets_module, name)
    calls = [0]

    def breaks_the_order(ctx: Any, *args: Any) -> Any:
        result = original(ctx, *args)
        calls[0] += 1
        if calls[0] == nth:
            _swap_first_decided_pair(ctx)
        return result

    monkeypatch.setattr(offsets_module, name, breaks_the_order)
    with pytest.raises(offsets_module.DecidedLaneOrderError) as raised:
        _run_once(graph)

    assert raised.value.phase == phase
    assert raised.value.port_id == DECIDED_PORT


@pytest.mark.parametrize(
    ("site", "name"),
    [("12", "_recompact_seed"), ("14b", "_slot_one_entry_arrival")],
)
def test_a_write_that_breaks_a_decided_order_is_declined(
    monkeypatch: pytest.MonkeyPatch, site: str, name: str
) -> None:
    """Phase 12 and 14b keep the decided order: the write is not committed and
    the decline is counted."""
    graph = _laid_out(DECIDED_FIXTURE.read_text())
    original = getattr(offsets_module, name)
    live: list[bool] = []

    def breaks_the_order(ctx: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(ctx, *args, **kwargs)
        if ctx.approach_orders and live:
            _swap_first_decided_pair(ctx)
        return result

    phase_body = {
        "12": "_recompact_fan_port_bordering_stations",
        "14b": "_slot_entry_arrivals_on_their_approach_lane",
    }[site]
    body = getattr(offsets_module, phase_body)

    def in_phase(*args: Any) -> Any:
        live.append(True)
        try:
            return body(*args)
        finally:
            live.clear()

    monkeypatch.setattr(offsets_module, name, breaks_the_order)
    monkeypatch.setattr(offsets_module, phase_body, in_phase)
    ctx = _run_once(graph)

    order = ctx.approach_orders[DECIDED_PORT]
    assert _holds(ctx.offsets, DECIDED_PORT, order)
    assert ctx.decided_order_declines[site] > 0


def test_top_descent_claims_come_from_7e_own_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A port 7e judges is claimed through 7e's own domain and ladder."""
    text = (TOPOLOGIES / "top_descent_over_left_entry.mmd").read_text()
    seen = _callers(monkeypatch, "_top_descent_order")
    ctx = _run_once(_laid_out(text))
    assert set(seen) == {"_order_top_descent_over_left_entry"}
    claimed = set(ctx.port_order_claims)
    assert claimed

    monkeypatch.setattr(offsets_module, "_top_descent_order", lambda *a: None)
    without = _run_once(_laid_out(text))
    assert claimed - set(without.port_order_claims)


def test_phase_10_and_its_refusal_share_one_reversed_section_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _callers(monkeypatch, "_sections_downstream_of")
    _run_once(_laid_out(DECIDED_FIXTURE.read_text()))
    _run_once(_laid_out((TOPOLOGIES / "near_vertical_junction_hook.mmd").read_text()))

    assert set(seen) == {
        "_reverse_offsets_from_roots",
        "_order_entry_ports_by_approach",
    }


def test_claims_reuse_7c_own_same_row_bypass_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The routing-context predicate behind 7c's same-row slotting is
    evaluated once per port, by 7c, never again to record a claim."""
    seen = _callers(monkeypatch, "same_row_bypass_entry")
    _run_once(_laid_out(DECIDED_FIXTURE.read_text()))

    assert seen
    assert set(seen) == {"_slot_same_row_bypass_entry"}


def test_together_exchange_into_a_decided_section_is_declined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where the exchanged pair enters a section holding a decided port
    together, 14b declines the exchange whole and counts it."""
    record = _render(monkeypatch, TOGETHER_MAP, aborts=CurveInvariantError)

    assert any(ctx.decided_order_declines["14b"] for ctx in record.ctxs)
