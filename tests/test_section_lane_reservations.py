"""No lane a section hands its stations may be reserved for nobody.

A level no line of the section rides at any of its stations reserves nothing, so
no marker should stretch to span it.  Scanned over the whole ``.mmd`` corpus
rather than one fixture, because the lane a line lands on is decided from the
section's line set and so every section is a candidate.

A lane left empty because a line turned off before a straight seam may stay
empty past it: the lines the seam carries straight either side of it keep their
lanes through the straight run rather than step across into it.

A line cut mid-section and rejoining on a different lane is deliberately *not*
asserted against: the alternative is to hold its far lane at every stop, which
reserves a level the nearer stations do not need and grows their markers, so the
step is the cheaper of the two and not a defect on its own.

``compact_offsets`` graphs are out of scope: that mode sizes each station's
bundle from the station's own line count, so a line there legitimately rides
different lanes at different stations.  Rail-laid sections are out of scope too
-- they draw from absolute rail coordinates, not lanes.
"""

from __future__ import annotations

import contextlib
import warnings
from pathlib import Path

import pytest

from nf_metro.api import render_string
from nf_metro.layout.constants import (
    COORD_TOLERANCE_FINE,
    graph_offset_step,
)
from nf_metro.layout.routing.invariants import (
    distinct_offset_levels,
    max_interior_offset_gap,
)
from nf_metro.parser.model import MetroGraph
from nf_metro.render import svg

_ROOT = Path(__file__).resolve().parents[1]

# Directories holding inputs that are not laid out as-is: the deliberately
# invalid fixtures abort in layout, and the Nextflow DAGs need conversion first.
_SKIP_DIRS = frozenset({"invalid", "nextflow"})


def section_node_lines(graph: MetroGraph, sec_id: str) -> list[tuple[str, str]]:
    """Every ``(node_id, line_id)`` lane slot a section owns, ports included."""
    section = graph.sections[sec_id]
    return [
        (sid, lid)
        for sid in section.station_ids
        if sid in graph.stations
        for lid in graph.station_lines(sid)
    ]


def _corpus() -> list[tuple[str, Path]]:
    """``(fixture_id, path)`` for every laid-out ``.mmd`` in the repository."""
    cases: list[tuple[str, Path]] = []
    for base in ("examples", "tests/fixtures", "docs"):
        for path in sorted((_ROOT / base).rglob("*.mmd")):
            if _SKIP_DIRS & set(path.relative_to(_ROOT).parts):
                continue
            cases.append((str(path.relative_to(_ROOT)), path))
    return cases


CORPUS = _corpus()
CORPUS_IDS = [fixture_id for fixture_id, _ in CORPUS]


def _lane_graph(path: Path):
    """The rendered graph and the lane map its drawing settles (permissive, so
    a map that aborts on a guard still shows its drawing), or ``None`` when
    lanes do not apply or nothing is drawn."""
    settled: list = []
    real = svg._settle_render_geometry

    def settle(graph, *args, **kwargs):
        result = real(graph, *args, **kwargs)
        settled.append((graph, result.station_offsets))
        return result

    with pytest.MonkeyPatch.context() as mp, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        mp.setattr(svg, "_settle_render_geometry", settle)
        with contextlib.suppress(Exception):
            render_string(
                "%%metro permissive: true\n" + path.read_text(),
                source_dir=str(path.parent),
            )
    if not settled or settled[-1][0].compact_offsets:
        return None
    return settled[-1]


def _lane_stacked_sections(graph) -> list[str]:
    """The sections whose geometry is drawn from per-line lanes.

    A rail-laid section draws from absolute rail coordinates instead, so its
    entries in the lane map never reach the canvas.
    """
    return [sec_id for sec_id in graph.sections if not graph.is_rail_section(sec_id)]


def unridden_lanes(graph, offsets) -> list[tuple[str, str, float]]:
    """Lane levels a section's markers span that no line of the section rides."""
    step = graph_offset_step(graph)
    found: list[tuple[str, str, float]] = []
    for sec_id in _lane_stacked_sections(graph):
        section = graph.sections[sec_id]
        ridden = distinct_offset_levels(
            offsets.get(key, 0.0) for key in section_node_lines(graph, sec_id)
        )
        for sid in section.station_ids:
            station = graph.stations.get(sid)
            if station is None or station.is_port:
                continue
            lanes = distinct_offset_levels(
                offsets.get((sid, lid), 0.0) for lid in graph.station_lines(sid)
            )
            if max_interior_offset_gap(lanes, step) is None:
                continue
            slots = int(round((lanes[-1] - lanes[0]) / step))
            for i in range(1, slots):
                lane = lanes[0] + i * step
                if any(abs(lane - lvl) <= COORD_TOLERANCE_FINE for lvl in ridden):
                    continue
                found.append((sec_id, sid, round(lane, 2)))
    return sorted(found)


def held_across_a_straight_seam(graph, offsets, sec_id, sid, lane) -> bool:
    """Whether *lane* at *sid* lies between two lines one straight seam into
    *sec_id* holds level from one exit, where no line crossing the seam rides
    the lane it carries straight across: a line turning off before the seam
    left it."""
    plan = graph._lane_plan
    if plan is None:
        return False
    step = graph_offset_step(graph)
    at_sid = {lid: offsets.get((sid, lid), 0.0) for lid in graph.station_lines(sid)}
    held = [
        (entry_id, exit_id, lid)
        for (entry_id, lid), exit_id in plan.seams.pins.items()
        if graph.ports[entry_id].section_id == sec_id and lid in at_sid
    ]
    for entry_id, exit_id, below in held:
        at_exit = {
            lid: plan.tracks.lanes.get((exit_id, lid))
            for lid in graph.station_lines(entry_id)
        }
        there = at_exit[below] + round((lane - at_sid[below]) / step, 3)
        if there not in at_exit.values() and any(
            (entry_id, exit_id) == (e, x)
            and at_sid[below] < lane < at_sid[above]
            and at_exit[above] - at_exit[below]
            == round((at_sid[above] - at_sid[below]) / step, 3)
            for e, x, above in held
        ):
            return True
    return False


@pytest.mark.parametrize(("fixture_id", "path"), CORPUS, ids=CORPUS_IDS)
def test_no_section_reserves_an_unridden_lane(fixture_id, path):
    """No station stretches its drawn marker over a lane no section-mate rides,
    unless a line turning off before a straight seam left it."""
    loaded = _lane_graph(path)
    if loaded is None:
        pytest.skip("compact mode, or nothing drawn")
    graph, offsets = loaded
    assert [
        found
        for found in unridden_lanes(graph, offsets)
        if not held_across_a_straight_seam(graph, offsets, *found)
    ] == []
