"""A partial row-mate hands its line to an adjacent carrier flat (#1844).

When a section carries only part of a row's through-bundle and hands one of
those lines straight to an adjacent carrier -- port to port, no junction and no
cell-mate forcing a bypass -- the connector runs level instead of jogging
diagonally into the shared port.  The lane plan owns the line's lane across
the hand-over and ``row_align`` owns the section's box: where a fan plan's
carrier slot fixes the line's lane at the carrier's entry, the line rides that
lane through the partial (a fan seam), and the partial's box stays where the
row alignment puts it.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from nf_metro.layout import engine
from nf_metro.layout.constants import graph_offset_step
from nf_metro.layout.lane_plan import ForcedCrossing
from nf_metro.layout.phases._common import _section_trunk_y
from nf_metro.layout.phases.guards import (
    PhaseInvariantError,
    _guard_corridor_fed_solo_rides_trunk,
)
from nf_metro.layout.routing import compute_station_offsets
from nf_metro.layout.routing.invariants import (
    check_convergence_shallow_feeder_concentric,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render import build_render_plan, emit_render_plan
from nf_metro.render.validate import parse_route_polylines
from nf_metro.themes import THEMES

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "examples" / "variantbenchmarking_auto.mmd"

PARTIAL = "ensembl_truth"
PARTIAL_EXIT = "ensembl_truth__exit_left_4"

# A carrier whose exit port hands its side-branch lines to a partial one column
# away: the partial stays on the carrier's trunk.
SIDE_BRANCH_FIXTURE = (
    REPO / "examples" / "topologies" / "side_branch_ascent_label_strike.mmd"
)
SIDE_BRANCH_CARRIER = "up"
SIDE_BRANCH_PARTIAL = "down"

# Under a symmetric diamond style the fan plan seats Line C on carrier slot 2 at
# Report's entry, two lanes off Report's trunk; Call carries only Line C and
# hands it to Report across a straight seam, while the a/b risers climb into the
# same entry from Align.
WITNESS = REPO / "examples" / "topologies" / "disjoint_sameline_trunks.mmd"
WITNESS_DIRECTIVE = "%%metro diamond_style: symmetric\n"
CALL, REPORT, ALIGN = "secD", "secE", "secC"
CALL_STATION, CALL_ENTRY, CALL_EXIT = (
    "d_node",
    "secD__entry_left_6",
    "secD__exit_right_3",
)
REPORT_ENTRY, ALIGN_EXIT = "secE__entry_left_7", "secC__exit_right_2"
SLOT = 2


def _layout():
    graph = parse_metro_mermaid(FIXTURE.read_text())
    engine.compute_layout(graph, validate=True)
    return graph


def _witness(row_align: str = "content", *, validate: bool = False):
    graph = parse_metro_mermaid(WITNESS_DIRECTIVE + WITNESS.read_text())
    graph.row_align = row_align
    engine.compute_layout(graph, validate=validate)
    return graph, compute_station_offsets(graph)


def _svg(graph) -> str:
    return emit_render_plan(build_render_plan(graph, THEMES["default"]))


def test_partial_exit_port_rides_its_own_trunk():
    """The handover exit port sits on the partial's trunk, so its leg is flat."""
    graph = _layout()
    partial_trunk = _section_trunk_y(graph, graph.sections[PARTIAL])
    exit_st = graph.stations[PARTIAL_EXIT]
    assert abs(exit_st.y - partial_trunk) < 1.0, (
        f"exit port {PARTIAL_EXIT} at {exit_st.y} must ride the partial trunk "
        f"{partial_trunk} for a flat handover"
    )


def test_partial_handover_connector_is_flat():
    """The rendered truth line runs level out of the partial's exit port."""
    graph = _layout()
    svg = _svg(graph)
    exit_x = round(graph.stations[PARTIAL_EXIT].x, 1)
    exit_y = round(graph.stations[PARTIAL_EXIT].y, 1)
    flat_run = None
    for line_id, subpaths in parse_route_polylines(svg):
        if line_id != "truth":
            continue
        for sub in subpaths:
            pts = [(round(x, 1), round(y, 1)) for x, y in sub]
            if any(abs(px - exit_x) < 1.0 and abs(py - exit_y) < 1.0 for px, py in pts):
                flat_run = pts
    assert flat_run is not None, "no truth subpath starts at the partial exit port"
    ys = {y for _, y in flat_run}
    assert len(ys) == 1, (
        f"truth handover connector jogs across Ys {sorted(ys)}; expected one flat lane"
    )


def test_side_branch_partial_stays_on_the_carrier_trunk():
    """A partial whose hand-over needs no lane step sits on its carrier's trunk."""
    graph = parse_metro_mermaid(SIDE_BRANCH_FIXTURE.read_text())
    engine.compute_layout(graph, validate=True)
    carrier = _section_trunk_y(graph, graph.sections[SIDE_BRANCH_CARRIER])
    partial = _section_trunk_y(graph, graph.sections[SIDE_BRANCH_PARTIAL])
    assert carrier is not None and partial is not None
    assert abs(partial - carrier) < 1e-6


@pytest.mark.parametrize("row_align", ["content", "top"])
def test_the_witness_validates_clean(row_align: str) -> None:
    _witness(row_align, validate=True)


@pytest.mark.parametrize("row_align", ["content", "top"])
def test_the_hand_over_into_the_fan_is_flat(row_align: str) -> None:
    graph, offsets = _witness(row_align)
    step = graph_offset_step(graph)

    def drawn(node: str) -> float:
        return graph.stations[node].y + offsets[(node, "c")]

    assert offsets[(REPORT_ENTRY, "c")] == SLOT * step
    assert drawn(CALL_ENTRY) == drawn(CALL_STATION) == drawn(CALL_EXIT)
    assert drawn(CALL_EXIT) == drawn(REPORT_ENTRY)


@pytest.mark.parametrize("row_align", ["content", "top"])
def test_the_call_box_stays_level_with_its_row(row_align: str) -> None:
    graph, _offsets = _witness(row_align)
    tops = {graph.sections[s].bbox_y for s in (ALIGN, CALL, REPORT)}
    bottoms = {
        graph.sections[s].bbox_y + graph.sections[s].bbox_h
        for s in (ALIGN, CALL, REPORT)
    }
    assert len(tops) == 1 and len(bottoms) == 1


def test_both_row_alignments_draw_the_witness_alike() -> None:
    digests = {
        hashlib.md5(_svg(_witness(mode)[0]).encode()).hexdigest()
        for mode in ("content", "top")
    }
    assert len(digests) == 1


def test_the_record_fixes_line_c_by_its_fan_seam() -> None:
    graph, _offsets = _witness()
    seams = graph._lane_plan.tracks.fan_seams
    assert {seams.get((n, "c")) for n in (CALL_ENTRY, CALL_STATION, CALL_EXIT)} == {
        SLOT
    }


def _segments(svg: str, line_id: str) -> list[tuple[tuple[float, float], ...]]:
    return [
        (a, b)
        for lid, subpaths in parse_route_polylines(svg)
        if lid == line_id
        for sub in subpaths
        for a, b in zip(sub, sub[1:])
    ]


def _cross(p, q) -> bool:
    (a, b), (c, d) = p, q

    def side(o, u, v) -> float:
        return (u[0] - o[0]) * (v[1] - o[1]) - (u[1] - o[1]) * (v[0] - o[0])

    return side(a, b, c) * side(a, b, d) < 0 and side(c, d, a) * side(c, d, b) < 0


def test_line_c_crosses_the_risers_where_the_record_says() -> None:
    graph, _offsets = _witness()
    assert (
        ForcedCrossing(REPORT, None, ("c", "a", "b"), REPORT_ENTRY, ALIGN_EXIT)
        in graph._lane_plan.tracks.forced
    )
    svg = _svg(graph)
    lo = graph.stations[CALL_EXIT].x
    hi = graph.stations[REPORT_ENTRY].x
    c_legs = [s for s in _segments(svg, "c") if lo <= min(s[0][0], s[1][0]) < hi]
    for riser in ("a", "b"):
        assert any(_cross(c, r) for c in c_legs for r in _segments(svg, riser))


def test_the_shallow_feeder_check_exempts_only_recorded_pairs() -> None:
    graph, offsets = _witness()
    assert check_convergence_shallow_feeder_concentric(graph, offsets) == []
    plan = graph._lane_plan
    kept = tuple(
        ForcedCrossing(REPORT, None, ("c", "a"), REPORT_ENTRY, ALIGN_EXIT)
        if f.port == REPORT_ENTRY and f.across == ALIGN_EXIT
        else f
        for f in plan.tracks.forced
    )
    graph._lane_plan = replace(plan, tracks=replace(plan.tracks, forced=kept))
    found = check_convergence_shallow_feeder_concentric(graph, offsets)
    assert len(found) == 1 and REPORT_ENTRY in found[0]


def test_the_solo_guard_holds_the_line_to_its_planned_lane() -> None:
    graph, offsets = _witness()
    step = graph_offset_step(graph)
    _guard_corridor_fed_solo_rides_trunk(graph, "test", offsets=offsets)
    for off in (0.0, (SLOT + 1) * step):
        moved = {**offsets, (CALL_ENTRY, "c"): off}
        with pytest.raises(PhaseInvariantError):
            _guard_corridor_fed_solo_rides_trunk(graph, "test", offsets=moved)
