"""A separate source's line stays clear of the lane a fork peels a branch into.

A fork on the trunk peels its second line off to a branch station, and an
unrelated source starts a third line whose run passes that branch's layer on its
way to a later trunk station.  The fork may itself be a source sharing the other
source's column, or sit one or two stations down the trunk.  Either way the
source must seat on the side of the trunk away from the branch, in the nearest
row that leaves no empty row in its column, so that its line never runs through
the branch marker or crosses another line, and each station's lanes stack in the
order their lines arrive and leave.  The outcome must not depend on whether the
source's line is declared before or after the peeled line, nor on orientation.
"""

from __future__ import annotations

import warnings
from functools import cache
from itertools import combinations
from pathlib import Path
from typing import NamedTuple

import pytest
from layout_validator import _segments_cross, validate_layout
from orientation_transform import Orientation, all_orientations, transform_source

from nf_metro.api import RenderConfig, prepare_graph, render_graph
from nf_metro.layout.engine import _station_marker_bbox
from nf_metro.layout.geometry import lanes_run_along_y, segment_intersects_bbox
from nf_metro.layout.phases.guards import PermissiveGuardWarning
from nf_metro.layout.routing import compute_station_offsets
from nf_metro.parser.model import MetroGraph
from nf_metro.render.validate import drawn_segments, parse_route_polylines
from nf_metro.themes import THEMES

TOPOLOGIES = Path(__file__).parent.parent / "examples" / "topologies"


class Fixture(NamedTuple):
    path: Path
    sibling: str
    sibling_line: str
    fork: str
    trunk_next: str
    peel: str


def _fixture(name: str) -> Fixture:
    return Fixture(TOPOLOGIES / name, "genome", "ref", "reads", "trim", "qc")


FIXTURES = {
    "source_fork": _fixture("source_sibling_vs_fork_peel_lane.mmd"),
    "fed_fork": _fixture("source_vs_fed_fork_peel_lane.mmd"),
    "chain_fed_fork": _fixture("source_vs_chain_fed_fork_peel_lane.mmd"),
}

_QC_DECL = "%%metro line: qc | QC | #0570b0\n"
_REF_DECL = "%%metro line: ref | Reference | #f5c542\n"


def _sibling_declared_first(source: str) -> str:
    assert _QC_DECL + _REF_DECL in source
    return source.replace(_QC_DECL + _REF_DECL, _REF_DECL + _QC_DECL)


DECLARATION_ORDERS = {
    "peel_line_first": lambda s: s,
    "sibling_line_first": _sibling_declared_first,
}

CASES = pytest.mark.parametrize(
    ("fixture", "order", "orientation"),
    [
        (fixture, order, orientation)
        for fixture in sorted(FIXTURES)
        for order in sorted(DECLARATION_ORDERS)
        for orientation in all_orientations()
    ],
    ids=lambda v: v.name if isinstance(v, Orientation) else v,
)


class Rendered(NamedTuple):
    fixture: Fixture
    graph: MetroGraph
    svg: str
    downgrades: list[str]


@cache
def _render(fixture: str, order: str, orientation: Orientation) -> Rendered:
    spec = FIXTURES[fixture]
    source = transform_source(
        DECLARATION_ORDERS[order](spec.path.read_text()), orientation
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        graph = prepare_graph(source)
        svg = render_graph(graph, THEMES["nfcore"], RenderConfig())
    downgrades = [
        str(w.message) for w in caught if issubclass(w.category, PermissiveGuardWarning)
    ]
    return Rendered(spec, graph, svg, downgrades)


def _side_of_trunk(r: Rendered, sid: str) -> float:
    """Signed side of *sid* relative to the trunk run leaving the fork."""
    fork, ahead, station = (
        r.graph.stations[n] for n in (r.fixture.fork, r.fixture.trunk_next, sid)
    )
    flow = (ahead.x - fork.x, ahead.y - fork.y)
    rel = (station.x - fork.x, station.y - fork.y)
    return flow[0] * rel[1] - flow[1] * rel[0]


def _column_gaps(r: Rendered) -> list[tuple[str, str, float]]:
    """Empty rows between consecutive stations in the sibling's column.

    Measured in the section's own frame: a column is the stations sharing the
    sibling's coordinate along the flow, and a row is one peel pitch (the
    trunk-to-branch distance across the flow).
    """
    graph, spec = r.graph, r.fixture
    section_id = graph.stations[spec.sibling].section_id
    assert section_id is not None
    horizontal = lanes_run_along_y(graph.sections[section_id].direction)

    def along(sid: str) -> float:
        station = graph.stations[sid]
        return station.x if horizontal else station.y

    def across(sid: str) -> float:
        station = graph.stations[sid]
        return station.y if horizontal else station.x

    pitch = abs(across(spec.peel) - across(spec.fork))
    column = sorted(
        (
            sid
            for sid, station in graph.stations.items()
            if not station.is_port and abs(along(sid) - along(spec.sibling)) < 1.0
        ),
        key=across,
    )
    return [
        (a, b, (across(b) - across(a)) / pitch)
        for a, b in zip(column, column[1:])
        if across(b) - across(a) > 1.5 * pitch
    ]


@CASES
def test_no_guard_downgrade(fixture: str, order: str, orientation: Orientation) -> None:
    r = _render(fixture, order, orientation)
    assert not r.downgrades, r.downgrades


@CASES
def test_sibling_seats_across_trunk_without_empty_row(
    fixture: str, order: str, orientation: Orientation
) -> None:
    r = _render(fixture, order, orientation)
    spec = r.fixture
    assert _side_of_trunk(r, spec.sibling) * _side_of_trunk(r, spec.peel) < 0, (
        f"{spec.sibling!r} sits on the same side of the trunk as {spec.peel!r}"
    )
    gaps = _column_gaps(r)
    assert not gaps, f"empty rows in {spec.sibling!r}'s column (rows apart): {gaps}"


@CASES
def test_sibling_line_clears_non_consumer_markers(
    fixture: str, order: str, orientation: Orientation
) -> None:
    r = _render(fixture, order, orientation)
    graph, line = r.graph, r.fixture.sibling_line
    offsets = compute_station_offsets(graph)
    segments = [
        (p, q)
        for line_id, p, q in drawn_segments(parse_route_polylines(r.svg))
        if line_id == line
    ]
    for sid, station in graph.stations.items():
        if station.is_port or line in graph.station_lines(sid):
            continue
        bbox = _station_marker_bbox(graph, sid, offsets=offsets)
        if bbox is None:
            continue
        hits = [
            (p, q)
            for p, q in segments
            if segment_intersects_bbox(p[0], p[1], q[0], q[1], bbox)
        ]
        assert not hits, f"line {line!r} crosses {sid!r} marker {bbox}: {hits}"


@CASES
def test_no_line_crosses_another(
    fixture: str, order: str, orientation: Orientation
) -> None:
    """No two lines cross, including beside a station they both stop at.

    ``validate_layout`` forgives a crossing within a quarter row of a station,
    which is exactly where a lane crossover at a join sits, so the drawn ink is
    checked pairwise as well.
    """
    r = _render(fixture, order, orientation)
    segments = drawn_segments(parse_route_polylines(r.svg))
    crossings = [
        (a[0], b[0], point)
        for a, b in combinations(segments, 2)
        if a[0] != b[0] and (point := _segments_cross(a[1], a[2], b[1], b[2]))
    ]
    assert not crossings, f"lines cross: {crossings}"
    flagged = [
        v.message
        for v in validate_layout(r.graph)
        if v.check == "route_segment_crossing"
    ]
    assert not flagged, flagged


_P3_BODY = """\
        a -->|main| s
        s -->|main| d
        d -->|main| j
        j -->|main| m
        s -->|b| g
        g -->|b| j
        f -->|c| m
"""
_LINES_BC = """\
%%metro line: main | Main | #2db572
%%metro line: b | B | #0570b0
%%metro line: c | C | #f5c542
"""
_LINES_CB = """\
%%metro line: main | Main | #2db572
%%metro line: c | C | #f5c542
%%metro line: b | B | #0570b0
"""
_LINE_E = "%%metro line: e | E | #e41a1c\n"


def _one_section(lines: str, body: str, header: str = "") -> str:
    return f"{header}{lines}graph LR\n    subgraph p [P]\n{body}    end\n"


def _framed(lines: str) -> str:
    """The fork's trunk fed flat from a neighbouring section."""
    body = _P3_BODY.replace("        a -->|main| s\n", "")
    return (
        f"{lines}graph LR\n"
        "    subgraph u [Up]\n        x -->|main| a\n    end\n"
        f"    subgraph p [P]\n{body}    end\n"
        "    a -->|main| s\n"
    )


SHAPES = {
    "centered_peel_line_first": _one_section(
        _LINES_BC, _P3_BODY, "%%metro line_spread: centered\n"
    ),
    "centered_sibling_line_first": _one_section(
        _LINES_CB, _P3_BODY, "%%metro line_spread: centered\n"
    ),
    "compact": _one_section(_LINES_BC, _P3_BODY, "%%metro compact_offsets: true\n"),
    "flat_frame_peel_line_first": _framed(_LINES_BC),
    "flat_frame_sibling_line_first": _framed(_LINES_CB),
    "two_contenders": _one_section(
        _LINES_BC + _LINE_E, _P3_BODY + "        h -->|e| m\n"
    ),
    "station_along_run": _one_section(
        _LINES_BC + _LINE_E,
        _P3_BODY
        + "".join(
            f"        {a} -->|e| {b}\n"
            for a, b in (("h", "y"), ("y", "z"), ("z", "w"), ("w", "v"), ("v", "m"))
        ),
    ),
}


@pytest.mark.parametrize("orientation", list(all_orientations()), ids=lambda o: o.name)
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_contender_shapes_stay_clear(shape: str, orientation: Orientation) -> None:
    """Other spreads, offset modes, frames and contender mixes hold the rule.

    The contender ``f`` seats on the far side of the trunk from the peel ``g``,
    its line runs through no station it does not serve, and no two lines cross.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        graph = prepare_graph(transform_source(SHAPES[shape], orientation))
        svg = render_graph(graph, THEMES["nfcore"], RenderConfig())
    downgrades = [
        str(w.message) for w in caught if issubclass(w.category, PermissiveGuardWarning)
    ]
    assert not downgrades, downgrades

    def side(sid: str) -> float:
        fork, ahead, station = (graph.stations[n] for n in ("s", "d", sid))
        flow = (ahead.x - fork.x, ahead.y - fork.y)
        return flow[0] * (station.y - fork.y) - flow[1] * (station.x - fork.x)

    assert side("f") * side("g") < 0, "'f' sits on the peel's side of the trunk"

    offsets = compute_station_offsets(graph)
    segments = drawn_segments(parse_route_polylines(svg))
    for sid, station in graph.stations.items():
        if station.is_port:
            continue
        bbox = _station_marker_bbox(graph, sid, offsets=offsets)
        lines = set(graph.station_lines(sid))
        hits = [
            line_id
            for line_id, p, q in segments
            if bbox is not None
            and line_id not in lines
            and segment_intersects_bbox(p[0], p[1], q[0], q[1], bbox)
        ]
        assert not hits, f"lines {sorted(set(hits))} cross {sid!r}'s marker"
    crossings = [
        (a[0], b[0], point)
        for a, b in combinations(segments, 2)
        if a[0] != b[0] and (point := _segments_cross(a[1], a[2], b[1], b[2]))
    ]
    assert not crossings, f"lines cross: {crossings}"
