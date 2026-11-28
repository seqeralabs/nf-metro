"""A per-section rail panel keeps the interior its rail owner laid.

The rail owner lays a rail section's stations, rails, rail spans and box size,
and seats its boundary ports.  Every later stage, and every layout pass, may
move the panel only rigidly with its box: the lane plan sealed at Stage 6.12
and the rail router read that interior.  The sweep measures each panel
independently of the engine's own record, from the stage its stations reach the
graph (2.1) and its ports are seated (3.1) to the end of layout.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

import nf_metro.api as api
from nf_metro.layout import engine as engine_module
from nf_metro.layout.lane_plan import build_lane_plan
from nf_metro.layout.phases.canvas import translate_graph
from nf_metro.layout.phases.guards import (
    PhaseInvariantError,
    _guard_rail_interior_as_laid,
)
from nf_metro.layout.rail_mode import position_rail_section_ports
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, PortSide
from nf_metro.parser.provenance import DecisionReason

ROOT = Path(__file__).resolve().parents[1]
RAIL_FIXTURES = (
    "examples/line_spread.mmd",
    "examples/rail_section.mmd",
    "examples/sarek_metro.mmd",
    "examples/topologies/rail_boundary_bundle_fan.mmd",
    "examples/topologies/rail_horizontal_labels.mmd",
    "examples/topologies/rail_symmetric_fork_join_spans.mmd",
    "tests/fixtures/rail_marked_single_line.mmd",
    "tests/fixtures/rail_pitch_vs_labels.mmd",
    "tests/data/rail_witnesses/rail_section_fed_by_a_trunk.mmd",
)
OPTION_SETS: dict[str, dict[str, object]] = {
    "default": {},
    "centred_symmetric": {"center_ports": True, "diamond_style": "symmetric"},
    "folded_wide_rows": {
        "center_ports": True,
        "fold_threshold": 8,
        "section_y_gap": 80.0,
    },
    "span_content_tight": {
        "line_order": "span",
        "row_align": "content",
        "x_spacing": 40.0,
    },
    "pinned_y_spacing": {"y_spacing": 60.0},
    "top_rows_thin": {
        "section_x_gap": 80.0,
        "row_align": "top",
        "stroke_scale": 0.6,
        "compact_offsets": False,
    },
    "tight_rows_untracked": {
        "stroke_scale": 1.5,
        "section_y_gap": 20.0,
        "track_gap": 0.0,
        "x_spacing": 40.0,
    },
    "centred_untracked_wide": {
        "track_gap": 0.0,
        "center_ports": True,
        "x_spacing": 80.0,
    },
    "pinned_folded_compact": {
        "y_spacing": 60.0,
        "compact_offsets": True,
        "fold_threshold": 6,
        "stroke_scale": 2.0,
    },
}
_MOVED = 0.01

Panel = dict[str, tuple[float, ...]]


def _panel(graph: MetroGraph, sid: str, *, ports: bool) -> Panel:
    sec = graph.sections[sid]
    bx, by = sec.bbox_x, sec.bbox_y
    out: Panel = {"<size>": (sec.bbox_w, sec.bbox_h)}
    for lid, y in graph._rail_y.get(sid, {}).items():
        out[f"<rail {lid}>"] = (y - by,)
    for nid in (*sec.station_ids, *sorted(sec.port_ids)):
        st = graph.stations.get(nid)
        if st is None:
            continue
        if (nid in graph.ports) != ports:
            continue
        spans = (st.rail_top_y, st.rail_bottom_y, *st.rail_used_ys)
        out[nid] = (
            st.x - bx,
            st.y - by,
            *(float("nan") if y is None else y - by for y in spans),
        )
    return out


def _departures(before: Panel, after: Panel) -> list[str]:
    out = []
    for key in sorted(set(before) | set(after)):
        a, b = before.get(key), after.get(key)
        same = (
            a is not None
            and b is not None
            and len(a) == len(b)
            and all((u != u and v != v) or abs(u - v) <= _MOVED for u, v in zip(a, b))
        )
        if not same:
            out.append(f"{key}: {a} -> {b}")
    return out


def _lay_out(rel: str, opts: dict[str, object], monkeypatch) -> tuple:
    """Lay *rel* out, capturing each rail panel at 2.1 and its ports at 3.1
    on the last positioning pass."""
    seen: dict[str, dict[str, Panel]] = {}
    real = engine_module._snap

    def snap(graph: MetroGraph, phase_id: str) -> None:
        real(graph, phase_id)
        rails = [s for s in graph.sections if graph.is_rail_section(s)]
        if phase_id == "2.1":
            seen.clear()
            seen["stations"] = {s: _panel(graph, s, ports=False) for s in rails}
        elif phase_id == "3.1":
            seen["ports"] = {s: _panel(graph, s, ports=True) for s in rails}

    monkeypatch.setattr(engine_module, "_snap", snap)
    path = ROOT / rel
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = api.prepare_graph(
            path.read_text(),
            layout_options=opts or None,
            source_dir=str(path.parent),
        )
    return graph, seen


@pytest.mark.parametrize("opts_id", sorted(OPTION_SETS))
@pytest.mark.parametrize("rel", RAIL_FIXTURES)
def test_rail_panel_moves_only_rigidly_after_it_is_laid(rel, opts_id, monkeypatch):
    graph, seen = _lay_out(rel, OPTION_SETS[opts_id], monkeypatch)
    rails = [s for s in graph.sections if graph.is_rail_section(s)]
    assert rails and set(seen["stations"]) == set(rails)
    for sid in rails:
        for kind, ports in (("stations", False), ("ports", True)):
            moved = _departures(seen[kind][sid], _panel(graph, sid, ports=ports))
            assert not moved, f"{rel} [{opts_id}] {sid} {kind}: " + "; ".join(moved[:4])


@pytest.mark.parametrize("rel", RAIL_FIXTURES)
def test_the_lane_plan_describes_the_rail_panel_drawn(rel, monkeypatch):
    graph, _seen = _lay_out(rel, {}, monkeypatch)
    assert build_lane_plan(graph) == graph._lane_plan


@pytest.mark.parametrize("rel", RAIL_FIXTURES)
def test_a_rail_sections_lanes_are_its_rails(rel, monkeypatch):
    """Each line's lane in a rail section is its rail's index, top rail
    first, as the rail owner assigned it, never a lane derived from
    placement."""
    graph, _seen = _lay_out(rel, {}, monkeypatch)
    lanes = graph._lane_plan.tracks.lanes
    checked = 0
    for sid in graph.sections:
        if not graph.is_rail_section(sid):
            continue
        rails = graph._rail_y[sid]
        order = sorted(rails, key=lambda lid: (rails[lid], lid))
        rank = {lid: i for i, lid in enumerate(order)}
        for node in graph.sections[sid].station_ids:
            if node in graph.ports or graph.stations[node].is_port:
                continue
            for lid in graph.station_lines(node):
                assert lanes[(node, lid)] == rank[lid], (rel, node, lid)
                checked += 1
    assert checked


def _sarek() -> MetroGraph:
    path = ROOT / "examples" / "sarek_metro.mmd"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return api.prepare_graph(path.read_text(), source_dir=str(path.parent))


def test_the_guard_accepts_a_rigid_move_of_the_whole_map():
    graph = _sarek()
    translate_graph(graph, 37.0, -11.0)
    _guard_rail_interior_as_laid(graph, "test")


@pytest.mark.parametrize("field", ["station", "rail", "span", "size"])
def test_the_guard_rejects_a_reshaped_panel(field):
    graph = _sarek()
    sec = graph.sections["calling"]
    st = next(
        graph.stations[s]
        for s in sec.station_ids
        if len(graph.stations[s].rail_used_ys) > 1
    )
    if field == "station":
        st.x += 5.0
    elif field == "rail":
        lid = next(iter(graph._rail_y["calling"]))
        graph._rail_y["calling"][lid] += 5.0
    elif field == "span":
        st.rail_used_ys = [y + 5.0 for y in st.rail_used_ys]
    else:
        sec.bbox_h += 5.0
    with pytest.raises(PhaseInvariantError, match="departs from its rail lay"):
        _guard_rail_interior_as_laid(graph, "test")


def test_the_guard_rejects_a_rail_section_its_owner_never_laid():
    graph = _sarek()
    graph._rail_lay.clear()
    with pytest.raises(PhaseInvariantError, match="carries no rail lay"):
        _guard_rail_interior_as_laid(graph, "test")


def test_under_validate_the_guard_names_the_stage_that_moved_a_rail_station(
    monkeypatch,
):
    real = engine_module._recenter_loop_side_stations

    def nudging(graph: MetroGraph) -> None:
        real(graph)
        sec = graph.sections.get("calling")
        if sec is not None:
            graph.stations[sec.station_ids[0]].x += 3.0

    monkeypatch.setattr(engine_module, "_recenter_loop_side_stations", nudging)
    graph = parse_metro_mermaid((ROOT / "examples" / "sarek_metro.mmd").read_text())
    with (
        warnings.catch_warnings(),
        pytest.raises(PhaseInvariantError, match=r"after Stage 6\.12: rail section"),
    ):
        warnings.simplefilter("ignore")
        engine_module.compute_layout(graph, validate=True)


WITNESSES = ROOT / "tests" / "data" / "rail_witnesses"


def _witness(name: str, **opts: object) -> MetroGraph:
    path = WITNESSES / name
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return api.prepare_graph(path.read_text(), layout_options=opts or None)


def _rail_mean(graph: MetroGraph, port_id: str) -> float:
    rails = graph._rail_y[graph.ports[port_id].section_id]
    ys = [rails[lid] for lid in graph.station_lines_ordered(port_id)]
    return sum(ys) / len(ys)


def test_a_fold_entry_clamp_leaves_the_rail_exit_feeding_it_on_its_rails():
    """The clamp seating a TB entry above its first station pulls a feeder up
    to keep the run straight, but a rail section's exit port is the rail
    owner's.  The render of this witness aborts later on its fold threshold,
    so the property is held at layout level."""
    graph = _witness("rail_exit_feeds_fold_entry.mmd", fold_threshold=4)
    assert graph.sections["s3"].direction == "TB"
    exit_id = next(iter(graph.sections["s2"].exit_ports))
    assert graph.stations[exit_id].y == pytest.approx(_rail_mean(graph, exit_id))


def test_a_rail_section_on_a_fold_column_keeps_left_to_right_flow():
    """Grid inference would turn this section into a TB fold bridge; rails
    run left to right, so its direction is locked before inference and its
    ports face opposite ends of the rails."""
    text = (WITNESSES / "rail_section_on_fold_column.mmd").read_text()
    plain = parse_metro_mermaid(text.replace("%%metro line_spread: rails | s8\n", ""))
    assert plain.sections["s8"].direction == "TB"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = parse_metro_mermaid(text)
    decision = graph.layout_provenance.direction_decision("s8")
    assert graph.sections["s8"].direction == "LR"
    assert decision.locked and decision.reason is DecisionReason.RAIL_SECTION_DIRECTION
    sides = {graph.ports[p].side for p in graph.sections["s8"].port_ids}
    assert sides == {PortSide.LEFT, PortSide.RIGHT}


@pytest.mark.parametrize("side", ["TOP", "BOTTOM"])
def test_a_rail_port_off_the_rail_ends_is_refused(side):
    graph = _witness("rail_exit_feeds_fold_entry.mmd", fold_threshold=4)
    section = graph.sections["s2"]
    graph.ports[next(iter(section.exit_ports))].side = PortSide[side]
    with pytest.raises(ValueError, match="rails run left to right"):
        position_rail_section_ports(graph, section)


def test_the_rail_lay_writes_nothing_onto_the_graph_before_stage_2_1(monkeypatch):
    """Until Stage 2.1 every section's graph stations hold the previous pass;
    the rail lay keeps its local coordinates in the subgraph it returns."""
    real = engine_module.lay_section_rails
    unchanged: list[bool] = []

    def state(graph: MetroGraph, sid: str) -> tuple:
        sec = graph.sections[sid]
        return (
            dict(graph._rail_y),
            [
                (st.x, st.y, st.rail_top_y, st.rail_bottom_y, tuple(st.rail_used_ys))
                for st in map(graph.stations.__getitem__, sec.station_ids)
            ],
        )

    def lay(graph: MetroGraph, section, **kw):
        before = state(graph, section.id)
        sub = real(graph, section, **kw)
        unchanged.append(state(graph, section.id) == before)
        return sub

    monkeypatch.setattr(engine_module, "lay_section_rails", lay)
    _sarek()
    assert unchanged and all(unchanged)
