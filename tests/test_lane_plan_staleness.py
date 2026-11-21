"""Tier-C oracle: the lane plan a layout decides still describes its placement.

The record is decided at the close of stage 6.12 (``LANE_PLAN_STAGE``) from the
placement it reads.  The box sizers after it (Stages 6.12a to 6.12c) grow boxes
and translate whole grid columns, which moves no record input relative to
another: rebuilding the record after each of them gives the decided record on
every curated map.  Row stacking (Stages 6.13 to 6.17) still moves positions
the record read on the maps ``STALE_ROWS`` lists, each with the first stage
whose placement plans a different record.  Those rows belong to #2171 (row
stacking keeps the alignment it is handed); the list only shrinks.
"""

from __future__ import annotations

import json
import warnings
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
STALE_ROWS_PATH = ROOT / "tests" / "data" / "lane_plan_stale_rows.json"
BOX_SIZERS = ("6.12a", "6.12b", "6.12c")
ROW_STACKING = ("6.13", "6.15a", "6.16", "6.17")
_DECIDED = CANONICAL_STAGE_ORDER.index(LANE_PLAN_STAGE)


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


@lru_cache(maxsize=None)
def _stale_stages(path: Path) -> tuple[frozenset[str], frozenset[str]]:
    """The box-sizer stages after which a rebuilt record differs from the
    decided one, and per layout the first later stage after which it does."""
    sizers: set[str] = set()
    first: dict[int, str] = {}
    real_snap = engine_module._snap
    build = lane_plan_module.build_lane_plan

    def snap(graph: MetroGraph, phase_id: str) -> None:
        real_snap(graph, phase_id)
        plan = graph._lane_plan
        if (
            plan is None
            or phase_id not in CANONICAL_STAGE_ORDER
            or CANONICAL_STAGE_ORDER.index(phase_id) <= _DECIDED
            or build(graph) == plan
        ):
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
    return frozenset(sizers), frozenset(first.values())


@lru_cache(maxsize=1)
def _rows() -> dict[str, list[str]]:
    return dict(json.loads(STALE_ROWS_PATH.read_text())["rows"])


def _write_rows() -> None:
    rows = {_rel(p): sorted(stale) for p in CURATED if (stale := _stale_stages(p)[1])}
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


@pytest.mark.parametrize("path", CURATED, ids=_rel)
def test_the_box_sizers_move_no_record_input(path: Path) -> None:
    assert _stale_stages(path)[0] == frozenset()


@pytest.mark.parametrize("path", CURATED, ids=_rel)
def test_the_decided_record_holds_outside_the_listed_rows(path: Path) -> None:
    assert sorted(_stale_stages(path)[1]) == _rows().get(_rel(path), [])


def test_the_listed_rows_are_row_stacking_repairs() -> None:
    assert all(set(stages) <= set(ROW_STACKING) for stages in _rows().values())


if __name__ == "__main__":
    _write_rows()
