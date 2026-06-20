"""Inter-row bypass reservation in ``_tighten_lower_rows_after_shrink`` (#665).

``_tighten_lower_rows_after_shrink`` pulls lower-row sections up to close the
slack a bbox shrink reveals.  When reserving inter-row space for a row's bypass
routes it must column-filter each bypass span against the lower section it would
crowd -- exactly as its downward sibling ``_push_lower_rows_after_bbox_grow``
does -- so a bypass running over one set of columns does not hold down a stacked
section sitting in a column the bypass never runs over.

The bug is hard to surface through a full render: in a single upper row every
section is a row-mate, so a tall one keeps the others tall and the leak is
masked.  These tests drive the pass directly: lay a real graph out, push the
lower row down to manufacture the slack a shrink would have revealed, run the
tighten pass, and assert the lower section ends at the floor its own column
justifies.
"""

from __future__ import annotations

import pytest

from nf_metro.layout.constants import BYPASS_CLEARANCE, SECTION_Y_GAP
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases.bbox import (
    _aggregate_bypass_spans,
    _tighten_lower_rows_after_shrink,
)
from nf_metro.layout.section_placement import _inter_row_routing_minimums
from nf_metro.parser.mermaid import parse_metro_mermaid


def _target_gap(graph) -> float:
    """The bbox-to-bbox gap the tighten pass targets between rows 0 and 1:
    ``SECTION_Y_GAP`` unless an inter-row routing run reserves more."""
    return max(SECTION_Y_GAP, _inter_row_routing_minimums(graph).get((0, 1), 0.0))


# A bypass over columns 0..2 (src -> tgt across an intervening mid section)
# and a single stacked row-1 section whose column is a parameter.  Fed from
# tgt (to its left) so its LEFT entry is a plain L-drop, reserving no
# inter-row wrap band that would otherwise dominate the gap.
_MMD = """\
%%metro title: Tighten bypass column filter
%%metro line: l1 | L1 | #e63946
%%metro line: l2 | L2 | #0570b0
%%metro line: l3 | L3 | #2db572

%%metro grid: src_sec | 0,0
%%metro grid: mid_sec | 1,0
%%metro grid: tgt_sec | 2,0
%%metro grid: low_sec | {low_col},1

graph LR
    subgraph src_sec [Source]
        %%metro exit: right | l1, l2
        s1[Start]
        s2[Out]
        s1 -->|l1,l2| s2
    end

    subgraph mid_sec [Middle]
        m1[Mid]
        m2[MidOut]
        m1 -->|l1| m2
    end

    subgraph tgt_sec [Target]
        %%metro entry: left | l1, l2
        %%metro exit: right | l3
        t1[Process]
        t2[End]
        t1 -->|l1,l2| t2
    end

    subgraph low_sec [Lower]
        lo1[LowIn]
        lo2[LowOut]
        lo1 -->|l3| lo2
    end

    s2 -->|l1| m1
    s2 -->|l1,l2| t1
    t2 -->|l3| lo1
"""


def _layout(low_col: int):
    graph = parse_metro_mermaid(_MMD.format(low_col=low_col))
    compute_layout(graph)
    return graph


def _push_lower_row_down(graph, delta: float) -> None:
    """Manufacture the post-shrink slack a tall-then-shrunk upper row leaves."""
    for s in graph.sections.values():
        if s.grid_row < 1:
            continue
        s.bbox_y += delta
        for stid in s.station_ids:
            st = graph.stations.get(stid)
            if st is not None:
                st.y += delta


def test_bypass_span_aggregated_over_intervening_section():
    """Guard the precondition: the topology really does generate a bypass over
    columns (0, 2) that dips ``BYPASS_CLEARANCE`` below the intervening row."""
    graph = _layout(low_col=3)
    row0 = [s for s in graph.sections.values() if s.grid_row == 0 and s.bbox_h > 0]
    spans = _aggregate_bypass_spans(graph, row0)
    assert (0, 2) in spans
    row0_bot = max(s.bbox_y + s.bbox_h for s in row0)
    assert spans[(0, 2)] == pytest.approx(row0_bot + BYPASS_CLEARANCE, abs=1.0)


def test_tighten_ignores_bypass_over_non_overlapping_column():
    """A stacked section in a column outside every bypass span is pulled up to
    ``SECTION_Y_GAP`` above the row-ending extent -- it must not retain the
    bypass clearance reserved for a different column."""
    graph = _layout(low_col=3)
    row0 = [s for s in graph.sections.values() if s.grid_row == 0 and s.bbox_h > 0]
    row0_bot = max(s.bbox_y + s.bbox_h for s in row0)
    target_gap = _target_gap(graph)

    _push_lower_row_down(graph, delta=40.0)
    _tighten_lower_rows_after_shrink(graph, SECTION_Y_GAP)

    low = graph.sections["low_sec"]
    assert low.bbox_y == pytest.approx(row0_bot + target_gap, abs=1.0)


def test_tighten_keeps_bypass_clearance_for_overlapping_column():
    """The fix is narrow: a stacked section whose column *is* under the bypass
    keeps the ``BYPASS_CLEARANCE`` reservation -- it would otherwise sit in the
    bypass route's path."""
    graph = _layout(low_col=1)
    row0 = [s for s in graph.sections.values() if s.grid_row == 0 and s.bbox_h > 0]
    spans = _aggregate_bypass_spans(graph, row0)
    bypass_bot = spans[(0, 2)]
    target_gap = _target_gap(graph)

    _push_lower_row_down(graph, delta=40.0)
    _tighten_lower_rows_after_shrink(graph, SECTION_Y_GAP)

    low = graph.sections["low_sec"]
    assert low.bbox_y == pytest.approx(bypass_bot + target_gap, abs=1.0)
