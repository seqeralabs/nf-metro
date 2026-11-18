"""No lane a section hands its stations may be reserved for nobody.

A level no line of the section rides at any of its stations reserves nothing, so
no marker should stretch to span it.  Scanned over the whole ``.mmd`` corpus
rather than one fixture, because the lane a line lands on is decided from the
section's line set and so every section is a candidate.

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

from pathlib import Path

import pytest

from nf_metro.api import prepare_graph
from nf_metro.layout.constants import (
    COORD_TOLERANCE_FINE,
    graph_offset_step,
)
from nf_metro.layout.routing import compute_station_offsets
from nf_metro.layout.routing.invariants import (
    distinct_offset_levels,
    max_interior_offset_gap,
)
from nf_metro.parser.model import MetroGraph

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

# seed_15 keeps two lanes at n8_0 that only lines ending inside s8 ride (#2041).
RESERVED_UNRIDDEN = {
    "tests/fixtures/hash_seed_determinism/seed_15.mmd": [
        ("s8", "n8_0", 4.0),
        ("s8", "n8_0", 12.0),
    ],
}


def _lane_graph(path: Path):
    """The settled graph and its lane map, or ``None`` when lanes do not apply."""
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    if graph.compact_offsets:
        return None
    return graph, compute_station_offsets(graph)


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


@pytest.mark.parametrize(("fixture_id", "path"), CORPUS, ids=CORPUS_IDS)
def test_no_section_reserves_an_unridden_lane(fixture_id, path):
    """No station stretches its marker over a lane no section-mate rides."""
    loaded = _lane_graph(path)
    if loaded is None:
        pytest.skip("compact mode does not hold a lane per line per section")
    graph, offsets = loaded
    assert unridden_lanes(graph, offsets) == RESERVED_UNRIDDEN.get(fixture_id, [])
