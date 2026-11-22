"""A row-group snaps onto its grid by relative position alone.

The grid origin is read as a residue ``y % pitch``, so moving a row vertically
can wrap that residue past a whole pitch and renumber every slot.  A station
exactly midway between two slots must land on the same one of them either way,
and stations tied together (an edge, or one hub's same-column fan) must all
take the same direction so the fan keeps its spacing.
"""

from __future__ import annotations

import math
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

_SnapCall = tuple[MetroGraph, float, list[str], dict[str, list[str]]]


def _group_snap_calls(text: str, source_dir: Path = EXAMPLES) -> list[_SnapCall]:
    """Each Stage 6.4 group snap's input, frozen as it reached the snap."""
    calls: list[_SnapCall] = []
    original = grid_snap._snap_group_to_grid

    def record(graph, pitch, sec_ids, convergence_sources):
        calls.append(deepcopy((graph, pitch, sec_ids, convergence_sources)))
        original(graph, pitch, sec_ids, convergence_sources)

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
    tied = set()
    for sid in _group_station_ids(graph, sec_ids):
        q = (graph.stations[sid].y - origin) / pitch
        if abs(q - math.floor(q) - 0.5) <= 1e-6:
            tied.add(sid)
    return tied


def _snapped(call: _SnapCall, dy: float) -> dict[str, float]:
    graph, pitch, sec_ids, convergence_sources = deepcopy(call)
    translate_graph(graph, 0.0, dy)
    grid_snap._snap_group_to_grid(graph, pitch, sec_ids, convergence_sources)
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
        for k in OFFSET_PITCHES:
            dy = round(k * pitch, 1)
            moved = _snapped(call, dy)
            drift = {s: (base[s], moved[s]) for s in base if base[s] != moved[s]}
            assert moved == pytest.approx(base, abs=1e-6), (
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
        graph, pitch, sec_ids, _ = call
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
    """Both branches midway between slots: half-to-even would fold them onto one."""
    (call,) = [c for c in _group_snap_calls(FAN) if "s" in c[2]]
    graph, _pitch, sec_ids, _ = call
    pitch = 40.0
    trunk = 100.0
    for sid in ("src", "hub", "join", "sink"):
        graph.stations[sid].y = trunk
    graph.stations["up"].y = trunk - pitch / 2.0
    graph.stations["down"].y = trunk + pitch / 2.0
    graph.stations["down"].x = graph.stations["up"].x
    frozen = (graph, pitch, sec_ids, {})
    for k in (0.0, *OFFSET_PITCHES):
        snapped = _snapped(frozen, round(k * pitch, 1))
        assert snapped["down"] - snapped["up"] == pytest.approx(pitch), (k, snapped)
        assert snapped["hub"] == pytest.approx(trunk)
