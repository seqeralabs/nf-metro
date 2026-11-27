"""Tier-C oracles: the lane plan a layout decides describes its placement.

The record is decided at the close of stage 6.12 (``LANE_PLAN_STAGE``) from the
placement it reads.  The box sizers after it (Stages 6.12a to 6.12c) grow boxes
and translate whole grid columns, which moves no record input relative to
another: rebuilding the record after each of them gives the decided record on
every curated map.  Row stacking (Stages 6.13 to 6.17) moves positions the
record read on the maps ``STALE_ROWS_PATH`` lists, each with the first stage whose
placement plans a different record.  Each rows file names the work that owns
its rows; the lists only shrink.

Every stage that moves a station within its section runs before the seal, so
from the close of 6.12 to the end of the settled pass each section's stations
move only by one common translation.  ``RIGID_ROWS_PATH`` lists the maps where a
stage after the seal moves a station relative to its section mates, each with
the stages that do.  The witnesses under
``tests/data/lane_plan_witnesses`` are maps no curated map stands in for, and
hold both oracles with no row.

``ANCHOR_WITNESS`` is a map whose orphaned half-grid expansion reads, at the
seal, a frame anchor a later stage moves relative to its section: a strict
xfail until that anchor is placed before the seal.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pytest

import nf_metro.layout.lane_plan as lane_plan_module
from nf_metro.layout import engine as engine_module
from nf_metro.layout.lane_plan import LANE_PLAN_STAGE
from nf_metro.layout.phase_state import CANONICAL_STAGE_ORDER
from nf_metro.layout.phases._common import _section_lr_port_anchor_y
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph

ROOT = Path(__file__).resolve().parents[1]
CURATED = sorted(
    p
    for p in [
        *(ROOT / "examples").rglob("*.mmd"),
        *(ROOT / "tests" / "fixtures").rglob("*.mmd"),
    ]
    if "invalid" not in p.parts
)
WITNESSES = sorted((ROOT / "tests" / "data" / "lane_plan_witnesses").glob("*.mmd"))
STALE_ROWS_PATH = ROOT / "tests" / "data" / "lane_plan_stale_rows.json"
RIGID_ROWS_PATH = ROOT / "tests" / "data" / "post_seal_rigidity_rows.json"
ANCHOR_WITNESS = ROOT / "tests" / "data" / "orphan_expansion_anchor" / "w2_00263.mmd"
BOX_SIZERS = ("6.12a", "6.12b", "6.12c")
ROW_STACKING = ("6.13", "6.15a", "6.16", "6.17")
PLANNED_FAN_REAPPLY = ("6.17",)
_DECIDED = CANONICAL_STAGE_ORDER.index(LANE_PLAN_STAGE)
_MOVED = 0.01

Frame = dict[str, dict[str, tuple[float, float]]]


@dataclass(frozen=True)
class _Probe:
    sizers: frozenset[str]
    stale: frozenset[str]
    nonrigid: frozenset[str]


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def _section_frames(graph: MetroGraph) -> Frame:
    """Each section's stations relative to its first, ports excluded."""
    frames: Frame = {}
    for sid, section in graph.sections.items():
        ids = sorted(
            s
            for s in section.station_ids
            if s in graph.stations
            and s not in graph.ports
            and not graph.stations[s].is_port
        )
        if len(ids) < 2:
            continue
        origin = graph.stations[ids[0]]
        frames[sid] = {
            s: (graph.stations[s].x - origin.x, graph.stations[s].y - origin.y)
            for s in ids
        }
    return frames


def _frames_differ(before: Frame, after: Frame) -> bool:
    return any(
        abs(after[sid][s][0] - xy[0]) > _MOVED or abs(after[sid][s][1] - xy[1]) > _MOVED
        for sid, frame in before.items()
        if sid in after
        for s, xy in frame.items()
        if s in after[sid]
    )


@lru_cache(maxsize=None)
def _probe(path: Path) -> _Probe:
    """The box-sizer stages after which a rebuilt record differs from the
    decided one, per layout the first later stage after which it does, and the
    stages of the settled pass after which a section's stations moved relative
    to one another."""
    sizers: set[str] = set()
    first: dict[int, str] = {}
    nonrigid: list[set[str]] = []
    previous: Frame | None = None
    real_snap = engine_module._snap
    build = lane_plan_module.build_lane_plan

    def snap(graph: MetroGraph, phase_id: str) -> None:
        nonlocal previous
        real_snap(graph, phase_id)
        if phase_id not in CANONICAL_STAGE_ORDER:
            return
        index = CANONICAL_STAGE_ORDER.index(phase_id)
        if index == _DECIDED:
            nonrigid.append(set())
            previous = _section_frames(graph)
        elif index > _DECIDED and previous is not None:
            current = _section_frames(graph)
            if _frames_differ(previous, current):
                nonrigid[-1].add(phase_id)
            previous = current
        plan = graph._lane_plan
        if plan is None or index <= _DECIDED or build(graph) == plan:
            return
        if phase_id in BOX_SIZERS:
            sizers.add(phase_id)
        else:
            first.setdefault(id(plan), phase_id)

    with warnings.catch_warnings(), pytest.MonkeyPatch.context() as mp:
        warnings.simplefilter("ignore")
        mp.setattr(engine_module, "_snap", snap)
        try:
            graph = parse_metro_mermaid(path.read_text())
            engine_module.compute_layout(graph)
        except Exception:
            pass
    return _Probe(
        frozenset(sizers),
        frozenset(first.values()),
        frozenset(nonrigid[-1] if nonrigid else ()),
    )


@lru_cache(maxsize=None)
def _rows(path: Path) -> dict[str, list[str]]:
    return dict(json.loads(path.read_text())["rows"])


def _write_rows() -> None:
    rows = {_rel(p): sorted(stale) for p in CURATED if (stale := _probe(p).stale)}
    STALE_ROWS_PATH.write_text(
        json.dumps(
            {
                "owner": "#2171",
                "replacement": (
                    "row stacking translates a feeder, its entry port and the "
                    "junctions between them as one, so no stage after 6.12 "
                    "re-decides an alignment the lane plan read"
                ),
                "rows": rows,
            },
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )


@pytest.mark.parametrize("path", CURATED + WITNESSES, ids=_rel)
def test_the_box_sizers_move_no_record_input(path: Path) -> None:
    assert _probe(path).sizers == frozenset()


@pytest.mark.parametrize("path", CURATED + WITNESSES, ids=_rel)
def test_the_decided_record_holds_outside_the_listed_rows(path: Path) -> None:
    assert sorted(_probe(path).stale) == _rows(STALE_ROWS_PATH).get(_rel(path), [])


def test_the_listed_rows_are_row_stacking_repairs() -> None:
    rows = _rows(STALE_ROWS_PATH).values()
    assert all(set(stages) <= set(ROW_STACKING) for stages in rows)


@pytest.mark.parametrize("path", CURATED + WITNESSES, ids=_rel)
def test_no_station_moves_within_its_section_after_the_seal(path: Path) -> None:
    assert sorted(_probe(path).nonrigid) == _rows(RIGID_ROWS_PATH).get(_rel(path), [])


def test_the_listed_rigidity_rows_are_planned_fan_reapplies() -> None:
    rows = _rows(RIGID_ROWS_PATH).values()
    assert all(set(stages) <= set(PLANNED_FAN_REAPPLY) for stages in rows)


def test_the_witness_directory_is_not_empty() -> None:
    assert WITNESSES


def _expansion_anchors(path: Path) -> dict[str, tuple[float, float]]:
    """Per section the orphaned half-grid expansion moves a station in, its
    frame anchor's offset from that station when the expansion reads it and
    at the end of the layout."""
    read: dict[str, tuple[str, float]] = {}
    real = engine_module._expand_orphaned_half_grid_stations

    def expand(graph: MetroGraph, *args: object) -> None:
        read.clear()
        before = {sid: st.y for sid, st in graph.stations.items()}
        anchors = {
            sid: _section_lr_port_anchor_y(graph, section)
            for sid, section in graph.sections.items()
        }
        real(graph, *args)  # type: ignore[arg-type]
        for sid, section in graph.sections.items():
            moved = sorted(
                s
                for s in section.station_ids
                if s in before
                and not graph.stations[s].is_port
                and abs(graph.stations[s].y - before[s]) > _MOVED
            )
            anchor = anchors[sid]
            if moved and anchor is not None:
                read[sid] = (moved[0], anchor - graph.stations[moved[0]].y)

    with warnings.catch_warnings(), pytest.MonkeyPatch.context() as mp:
        warnings.simplefilter("ignore")
        mp.setattr(engine_module, "_expand_orphaned_half_grid_stations", expand)
        graph = parse_metro_mermaid(path.read_text())
        engine_module.compute_layout(graph)
    final: dict[str, tuple[float, float]] = {}
    for sid, (station, offset) in read.items():
        anchor = _section_lr_port_anchor_y(graph, graph.sections[sid])
        assert anchor is not None
        final[sid] = (offset, anchor - graph.stations[station].y)
    return final


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Stage 6.16's TB-entry clamp pulls the exit port "
        "anchoring the expansion onto its topmost feeder after the seal"
    ),
)
def test_the_orphan_expansion_reads_a_final_anchor() -> None:
    """The anchor each section's half-grid expansion reads at the seal lies
    where it ends, relative to the stations it moves."""
    anchors = _expansion_anchors(ANCHOR_WITNESS)
    assert anchors
    for sid, (seal, final) in anchors.items():
        assert abs(seal - final) <= _MOVED, (sid, seal, final)


if __name__ == "__main__":
    _write_rows()
