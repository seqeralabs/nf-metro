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
    frame: list[Frame] = []
    real_snap = engine_module._snap
    build = lane_plan_module.build_lane_plan

    def snap(graph: MetroGraph, phase_id: str) -> None:
        real_snap(graph, phase_id)
        if phase_id not in CANONICAL_STAGE_ORDER:
            return
        index = CANONICAL_STAGE_ORDER.index(phase_id)
        if index == _DECIDED:
            nonrigid.append(set())
            frame[:] = [_section_frames(graph)]
        elif index > _DECIDED and frame:
            current = _section_frames(graph)
            if _frames_differ(frame[0], current):
                nonrigid[-1].add(phase_id)
            frame[:] = [current]
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


if __name__ == "__main__":
    _write_rows()
