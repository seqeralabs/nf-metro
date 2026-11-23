"""A root fan keeps the source whose chain it continues on the trunk row.

Predecessor-less stations share a source column on one line: ``reads`` feeds
``trim``, which also carries a second line and so does not take the source's
track, while the other sources run straight to the merge.  The trunk
``reads -> trim -> align -> report`` must hold one row, with the other sources
one row to either side and the second line's ``fastqc`` branch one row off,
whichever source is declared first and in every orientation.
"""

from __future__ import annotations

import json
from itertools import permutations
from pathlib import Path

import pytest
from layout_validator import validate_layout
from orientation_transform import Orientation, all_orientations, transform_source

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph

FIXTURE = (
    Path(__file__).parent.parent
    / "examples"
    / "topologies"
    / "root_fan_trunk_feeder_row.mmd"
)
TRUNK = ("reads", "trim", "align", "report")
FEEDER = "genome"
BRANCH = "fastqc"
ROW = 40.0

_SOURCES = {
    "genome": ("genome[Genome]", "genome -->|main| report"),
    "reads": ("reads[Reads]", "reads -->|main| trim"),
    "index": ("index[Index]", "index -->|main| report"),
}
_INDENT = " " * 8


def _with_sources(*order: str) -> str:
    """The fixture with exactly *order*'s sources, declared in that order."""
    stations = {station for station, _ in _SOURCES.values()}
    edges = {edge for _, edge in _SOURCES.values()}
    out: list[str] = []
    for line in FIXTURE.read_text().splitlines(keepends=True):
        text = line.strip()
        if text in stations or text in edges:
            column = 0 if text in stations else 1
            if text == _SOURCES["reads"][column]:
                out += [f"{_INDENT}{_SOURCES[n][column]}\n" for n in order]
            continue
        out.append(line)
    return "".join(out)


def _layout(source: str) -> MetroGraph:
    graph = parse_metro_mermaid(source)
    compute_layout(graph)
    return graph


def _coords(graph: MetroGraph) -> dict[str, tuple[float, float]]:
    return {
        sid: (round(st.x, 3), round(st.y, 3))
        for sid, st in graph.stations.items()
        if not st.is_port
    }


def test_fixture_declares_genome_before_reads() -> None:
    assert _with_sources("genome", "reads") == FIXTURE.read_text()


def test_trunk_holds_one_row_with_feeder_and_branch_one_row_off() -> None:
    graph = _layout(FIXTURE.read_text())
    st = graph.stations
    trunk_y = st["reads"].y
    assert [st[s].y for s in TRUNK] == [trunk_y] * len(TRUNK)
    assert st[FEEDER].y == trunk_y - ROW
    assert st[BRANCH].y == trunk_y + ROW
    assert [st[s].track for s in TRUNK] == [0.0] * len(TRUNK)
    assert st[FEEDER].track == -1.0
    assert st[BRANCH].track == 1.0
    assert validate_layout(graph) == []


def test_trunk_source_shares_its_successor_row_at_track_placement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NF_METRO_PHASE_SNAPSHOTS", "1")
    monkeypatch.setenv("NF_METRO_PHASE_SNAPSHOT_DIR", str(tmp_path))
    _layout(FIXTURE.read_text())
    (snapshot,) = tmp_path.glob("*/2.1.json")
    stations = json.loads(snapshot.read_text())["stations"]
    assert stations["reads"]["y"] == stations["trim"]["y"]


def test_source_declaration_order_does_not_move_the_layout() -> None:
    assert _coords(_layout(_with_sources("reads", "genome"))) == _coords(
        _layout(FIXTURE.read_text())
    )


@pytest.mark.parametrize("orientation", list(all_orientations()), ids=lambda o: o.name)
def test_trunk_is_one_lane_in_every_orientation(orientation: Orientation) -> None:
    graph = _layout(transform_source(FIXTURE.read_text(), orientation))
    axis = 0 if orientation.swaps_axes else 1
    cross = {sid: xy[axis] for sid, xy in _coords(graph).items()}
    trunk = cross["reads"]
    assert [cross[s] for s in TRUNK] == [trunk] * len(TRUNK)
    lanes = sorted(set(cross.values()))
    assert len(lanes) == 3 and lanes[1] == trunk
    assert lanes[1] - lanes[0] == lanes[2] - lanes[1]
    assert {cross[FEEDER], cross[BRANCH]} == {lanes[0], lanes[2]}


def test_lone_source_keeps_the_trunk_row() -> None:
    st = _layout(_with_sources("reads")).stations
    trunk_y = st["reads"].y
    assert [st[s].y for s in TRUNK] == [trunk_y] * len(TRUNK)
    assert st[BRANCH].y == trunk_y + ROW


def test_three_source_fan_centred_on_trunk_seats_feeders_either_side() -> None:
    st = _layout(_with_sources("genome", "reads", "index")).stations
    trunk_y = st["reads"].y
    assert st[FEEDER].y == trunk_y - ROW
    assert st["index"].y == trunk_y + ROW


@pytest.mark.parametrize(
    "order", list(permutations(("genome", "reads", "index"))), ids="-".join
)
def test_three_source_fan_keeps_trunk_row_in_any_order(
    order: tuple[str, ...],
) -> None:
    graph = _layout(_with_sources(*order))
    st = graph.stations
    trunk_y = st["reads"].y
    assert [st[s].y for s in TRUNK] == [trunk_y] * len(TRUNK)
    assert {st[FEEDER].y, st["index"].y} == {trunk_y - ROW, trunk_y + ROW}
    assert st[BRANCH].y == trunk_y + ROW
    assert validate_layout(graph) == []
