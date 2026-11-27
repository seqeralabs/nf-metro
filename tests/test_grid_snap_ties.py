"""A row-group snaps onto its grid by relative position alone.

The grid origin is read as a residue ``y % pitch``, so moving a row vertically
can wrap that residue past a whole pitch and renumber every slot.  A station
exactly midway between two slots must land on the same one of them either way,
and stations tied together (an edge, or one hub's same-column fan) must all
take the same direction so the fan keeps its spacing.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from nf_metro.api import prepare_graph
from nf_metro.layout.phases import grid_snap
from nf_metro.layout.phases.canvas import translate_graph
from nf_metro.parser.model import MetroGraph

EXAMPLES = Path(__file__).parents[1] / "examples"

# Every corpus map whose Stage 6.4 snap meets a station midway between slots.
TIED_FIXTURES = (
    "genomic_pipeline.mmd",
    "riboseq_metro.mmd",
    "rnaseq_auto.mmd",
    "rnaseq_metro.mmd",
    "rnaseq_sections.mmd",
    "rnaseq_sections_manual.mmd",
    "topologies/multicarrier_offrow_exit_climb.mmd",
    "topologies/packed_cell_right_exit_left_entry_wrap.mmd",
    "topologies/rowmate_tb_side_entry_top_align.mmd",
    "topologies/stacked_split_left_entry_drop.mmd",
    "topologies/stacked_split_right_entry_drop.mmd",
)

# Fractions of the group pitch: each whole pitch renumbers every slot, and the
# fractional parts wrap the origin residue past zero for most origins.
OFFSET_PITCHES = (1.0, 1.5, 2.25, 3.0, -1.0, -2.75)
# Offsets off the 0.001 px lattice the group origin is rounded to, so each
# residue rounds differently from where it sat before the move.
OFFSETS_PX = (1e-4, 1 / 3, 0.1234567, 13.0001, 22.0005, -5.4321, 100 / 7, 333.3333333)
# A moved row snaps to an origin rounded afresh to 0.001 px.
ORIGIN_ROUNDING_PX = 1e-3


def _offsets(pitch: float) -> tuple[float, ...]:
    return (*(round(k * pitch, 1) for k in OFFSET_PITCHES), *OFFSETS_PX)


_SnapCall = tuple[MetroGraph, float, list[str], dict[str, list[str]], frozenset[str]]


def _group_snap_calls(text: str, source_dir: Path = EXAMPLES) -> list[_SnapCall]:
    """Each Stage 6.4 group snap's input, frozen as it reached the snap."""
    calls: list[_SnapCall] = []
    original = grid_snap._snap_group_to_grid

    def record(graph, pitch, sec_ids, convergence_sources, held_exits):
        calls.append(deepcopy((graph, pitch, sec_ids, convergence_sources, held_exits)))
        original(graph, pitch, sec_ids, convergence_sources, held_exits)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(grid_snap, "_snap_group_to_grid", record)
        prepare_graph(text, source_dir=str(source_dir), bare=True)
    return calls


def _group_station_ids(graph: MetroGraph, sec_ids: list[str]) -> list[str]:
    return sorted(
        sid
        for sec_id in sec_ids
        if (section := graph.sections.get(sec_id)) is not None
        for sid in section.station_ids
        if sid in graph.stations
    )


def _tied_ids(graph: MetroGraph, sec_ids: list[str], pitch: float) -> set[str]:
    """Group stations sitting midway between two slots of the group's grid."""
    origin_info = grid_snap._group_grid_origin(graph, sec_ids, pitch)
    if origin_info is None:
        return set()
    origin = origin_info[0]
    return {
        sid
        for sid in _group_station_ids(graph, sec_ids)
        if grid_snap._is_slot_tie(graph.stations[sid].y, origin, pitch)
    }


def _snapped(call: _SnapCall, dy: float) -> dict[str, float]:
    graph, pitch, sec_ids, convergence_sources, held_exits = deepcopy(call)
    translate_graph(graph, 0.0, dy)
    grid_snap._snap_group_to_grid(
        graph, pitch, sec_ids, convergence_sources, held_exits
    )
    return {
        sid: graph.stations[sid].y - dy for sid in _group_station_ids(graph, sec_ids)
    }


def _tied_calls(fixture: str) -> list[_SnapCall]:
    path = EXAMPLES / fixture
    calls = _group_snap_calls(path.read_text(), path.parent)
    tied = [c for c in calls if _tied_ids(c[0], c[2], c[1])]
    assert tied, f"{fixture} no longer meets a half-pitch tie at Stage 6.4"
    return tied


@pytest.mark.parametrize("fixture", TIED_FIXTURES)
def test_tied_row_snaps_identically_after_vertical_offset(fixture: str) -> None:
    for call in _tied_calls(fixture):
        pitch = call[1]
        base = _snapped(call, 0.0)
        for dy in _offsets(pitch):
            moved = _snapped(call, dy)
            drift = {s: (base[s], moved[s]) for s in base if base[s] != moved[s]}
            assert moved == pytest.approx(base, abs=ORIGIN_ROUNDING_PX), (
                f"{fixture} group {call[2]} offset {dy}: {drift}"
            )


def _tied_fans(graph: MetroGraph, tied: set[str]) -> list[list[str]]:
    """Tied same-column stations that share one hub, per hub and column."""
    neighbours: dict[str, set[str]] = {}
    for edge in graph.edges:
        neighbours.setdefault(edge.source, set()).add(edge.target)
        neighbours.setdefault(edge.target, set()).add(edge.source)
    fans = []
    for _hub, nbrs in sorted(neighbours.items()):
        columns: dict[float, list[str]] = {}
        for sid in sorted(nbrs & tied):
            st = graph.stations[sid]
            if not st.is_port and not st.off_track:
                columns.setdefault(round(st.x, 3), []).append(sid)
        fans.extend(members for members in columns.values() if len(members) >= 2)
    return fans


@pytest.mark.parametrize("fixture", TIED_FIXTURES)
def test_tied_fan_moves_as_one(fixture: str) -> None:
    for call in _tied_calls(fixture):
        graph, pitch, sec_ids, *_ = call
        before = {
            sid: graph.stations[sid].y for sid in _group_station_ids(graph, sec_ids)
        }
        after = _snapped(call, 0.0)
        for fan in _tied_fans(graph, _tied_ids(graph, sec_ids, pitch)):
            shifts = {round(after[s] - before[s], 6) for s in fan}
            assert len(shifts) == 1, f"{fixture} fan {fan} split: {shifts}"


FAN = """\
%%metro line: a | A | #e6194b
%%metro line: b | B | #3cb44b
graph LR
    subgraph s [S]
        src[Source]
        hub[Hub]
        up[Upper]
        down[Lower]
        join[Join]
        sink[Sink]
        src -->|a,b| hub
        hub -->|a| up
        hub -->|b| down
        up -->|a| join
        down -->|b| join
        join -->|a,b| sink
    end
"""


def test_tied_two_branch_fan_keeps_its_spacing() -> None:
    """Both branches midway between slots snap the same way, one pitch apart."""
    (call,) = [c for c in _group_snap_calls(FAN) if "s" in c[2]]
    graph, _pitch, sec_ids, *_ = call
    pitch = 40.0
    trunk = 100.0
    for sid in ("src", "hub", "join", "sink"):
        graph.stations[sid].y = trunk
    graph.stations["up"].y = trunk - pitch / 2.0
    graph.stations["down"].y = trunk + pitch / 2.0
    graph.stations["down"].x = graph.stations["up"].x
    frozen = (graph, pitch, sec_ids, {}, frozenset())
    for dy in (0.0, *_offsets(pitch)):
        snapped = _snapped(frozen, dy)
        spacing = snapped["down"] - snapped["up"]
        assert spacing == pytest.approx(pitch, abs=ORIGIN_ROUNDING_PX), (dy, snapped)
        assert snapped["hub"] == pytest.approx(trunk, abs=ORIGIN_ROUNDING_PX)


TB_EXIT = Path(__file__).parent / "fixtures" / "tb_exit_terminal_on_carrier.mmd"


def _reflected(call: _SnapCall) -> _SnapCall:
    graph, pitch, sec_ids, convergence_sources, held_exits = deepcopy(call)
    for st in graph.stations.values():
        st.y = -st.y
    for port in graph.ports.values():
        port.y = -port.y
    return graph, pitch, sec_ids, convergence_sources, held_exits


@pytest.mark.parametrize("reflect", [False, True], ids=["as-drawn", "reflected"])
def test_unlevelled_tie_rides_with_the_station_more_lines_reach(reflect: bool) -> None:
    """The TE entry port sits midway between its two consumers: one takes
    riboseq, the other rnaseq and, through the first, riboseq too.  Neither
    side levels more relations, so the port snaps level with the consumer
    carrying both lines, in either orientation."""
    call = next(
        c
        for c in _group_snap_calls(TB_EXIT.read_text(), TB_EXIT.parent)
        if c[2] == ["te"]
    )
    assert "te__entry_left_7" in _tied_ids(call[0], call[2], call[1])
    frozen = _reflected(call) if reflect else call
    for dy in (0.0, *_offsets(call[1])):
        snapped = _snapped(frozen, dy)
        assert snapped["te__entry_left_7"] == pytest.approx(
            snapped["te_counts_prep"], abs=ORIGIN_ROUNDING_PX
        ), (dy, snapped["te__entry_left_7"], snapped["te_counts_prep"])


DRAFT_OFF_LANE = (
    Path(__file__).parent
    / "fixtures"
    / "regressions"
    / "approach_order_draft_off_lane.mmd"
)


def _draft_off_lane_call() -> _SnapCall:
    call = next(
        c
        for c in _group_snap_calls(DRAFT_OFF_LANE.read_text(), DRAFT_OFF_LANE.parent)
        if c[2] == ["sec1"]
    )
    assert {"s1n0", "sec1__entry_left_2"} <= _tied_ids(call[0], call[2], call[1])
    return call


def _level_run_vote(call: _SnapCall, component: list[str]) -> int:
    graph, pitch, sec_ids, *_ = call
    group_ids = set(_group_station_ids(graph, sec_ids))
    return grid_snap._level_run_direction(
        graph, component, group_ids, _tied_ids(graph, sec_ids, pitch)
    )


@pytest.mark.parametrize("reflect", [False, True], ids=["as-drawn", "reflected"])
def test_an_unlevelled_tie_carries_on_the_level_run_reaching_it(reflect: bool) -> None:
    """Sec1's entry and first station sit midway between slots, level with
    the fan-out junction below sec0's bottom exit, whose feed descends into
    that row.  Nothing else decides the tie, so the pair snaps on down (up,
    reflected) and the feed crosses rows once rather than turning back."""
    call = _draft_off_lane_call()
    frozen = _reflected(call) if reflect else call
    pitch = call[1]
    tied_y = frozen[0].stations["s1n0"].y
    onward = tied_y + (-pitch if reflect else pitch) / 2.0
    for dy in (0.0, *_offsets(pitch)):
        snapped = _snapped(frozen, dy)
        assert snapped["s1n0"] == pytest.approx(onward, abs=ORIGIN_ROUNDING_PX), dy
        assert snapped["sec1__entry_left_2"] == pytest.approx(
            onward, abs=ORIGIN_ROUNDING_PX
        ), dy


def test_a_junction_its_tied_anchor_carries_gives_no_vote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """__junction_4 holds its row only while the exit port it is derived from
    does not snap with the tie; anchored on a tied station of the group, it
    moves with the snap and the run through it votes for nothing."""
    call = _draft_off_lane_call()
    component = ["s1n0", "sec1__entry_left_2"]
    assert _level_run_vote(call, component) == grid_snap._DOWN
    monkeypatch.setattr(
        grid_snap, "junction_anchor_port", lambda _graph, _jid: "sec1__entry_left_2"
    )
    assert _level_run_vote(call, component) == 0


def test_a_merge_junction_on_a_tied_entry_gives_no_vote() -> None:
    """genomic_pipeline's Reporting entry ties level with the merge junctions
    on its own lead-in, which follow the entry through the snap."""
    path = EXAMPLES / "genomic_pipeline.mmd"
    call = next(
        c
        for c in _group_snap_calls(path.read_text(), path.parent)
        if c[2] == ["reporting"]
    )
    graph = call[0]
    assert "reporting__entry_left_7" in _tied_ids(graph, call[2], call[1])
    merges = {
        e.source
        for e in graph.edges
        if e.target == "reporting__entry_left_7" and e.source in graph.junction_ids
    }
    assert merges
    assert _level_run_vote(call, ["reporting__entry_left_7"]) == 0
