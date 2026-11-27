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

import re
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from itertools import combinations
from pathlib import Path
from typing import NamedTuple

import networkx as nx
import pytest
from conftest import render_with_downgrades
from layout_validator import _segments_cross, validate_layout
from orientation_transform import Orientation, all_orientations, transform_source

from nf_metro.layout.geometry import lanes_run_along_y, segment_intersects_bbox
from nf_metro.layout.ordering import _claimed_peel_lane, _seat_contender
from nf_metro.layout.phases._common import _station_marker_bbox
from nf_metro.layout.routing import compute_station_offsets
from nf_metro.parser import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph
from nf_metro.render.validate import drawn_segments, parse_route_polylines

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
    "fed_fork_compact": _fixture("source_vs_fed_fork_peel_lane_compact.mmd"),
    "fed_fork_across_sections": _fixture(
        "source_vs_fork_peel_lane_across_sections.mmd"
    ),
    "terminal_peel": _fixture("source_vs_terminal_peel_lane.mmd"),
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

_TB, _BT = ("m270", "r90"), ("m90", "r270")
_CORRIDOR = (
    "with its neighbour stacked along the flow, the bundle carried across "
    "leaves outside the inter-row corridor its reservation realises"
)
_STACKED_FEED = "lines fed from the stacked frame cross the trunk"
_CENTRED_TB_ABORT = (
    "the lane pin leaves the centred fork's planned fan off its boundary "
    "frame, and the render aborts in a top-to-bottom section"
)
_FED_SHAPES = [
    "fed_bundle_with_the_peel_line",
    "fed_bundle_with_the_peel_line_sibling_line_first",
    "fed_bundle_without_the_peel_line",
    "fed_bundle_with_the_contender_line",
]
_AUTO_GRID = "fed_bundle_with_the_peel_line_auto_grid"
_KNOWN_DEFECTS = {
    "test_no_guard_downgrade": {
        ("fed_fork_across_sections", o): _CORRIDOR for o in _TB + _BT
    },
    "test_no_line_crosses_another": {
        ("fed_fork_across_sections", o): _CORRIDOR for o in _TB
    },
    "test_contender_shapes_stay_clear": {
        **{
            (f"centered_{order}", o): _CENTRED_TB_ABORT
            for order in DECLARATION_ORDERS
            for o in _TB
        },
        **{
            (shape, o): _STACKED_FEED
            for shape in [
                *(f"flat_frame_{order}" for order in DECLARATION_ORDERS),
                *_FED_SHAPES,
                _AUTO_GRID,
            ]
            for o in _TB
        },
        **{
            (f"flat_frame_carried_{order}", o): _CORRIDOR
            for order in DECLARATION_ORDERS
            for o in _TB + _BT
        },
        **{(shape, o): _CORRIDOR for shape in _FED_SHAPES for o in _BT},
        **{
            (_AUTO_GRID, o): (
                "the bundle carried over the top keeps less canvas margin than "
                "it reserved"
            )
            for o in _BT
        },
        **{
            (_AUTO_GRID, o): "the inferred grid reverses the flow and aborts"
            for o in ("m0", "r180")
        },
    },
}


def _cases(test: str, keys: list[tuple[str, ...]]) -> list[object]:
    """*keys* as parameters, a known defect of *test* marked strictly expected."""
    known = _KNOWN_DEFECTS.get(test, {})
    return [
        pytest.param(
            *key,
            marks=pytest.mark.xfail(strict=True, reason=known[(key[0], key[-1].name)])
            if (key[0], key[-1].name) in known
            else (),
        )
        for key in keys
    ]


_FIXTURE_KEYS = [
    (fixture, order, orientation)
    for fixture in sorted(FIXTURES)
    for order in sorted(DECLARATION_ORDERS)
    for orientation in all_orientations()
]


def _fixture_cases(test: str) -> pytest.MarkDecorator:
    return pytest.mark.parametrize(
        ("fixture", "order", "orientation"),
        _cases(test, _FIXTURE_KEYS),
        ids=lambda v: v.name if isinstance(v, Orientation) else v,
    )


CASES = _fixture_cases("")


_SUBGRAPH_WITHOUT_DIRECTION = re.compile(
    r"^(\s*)(subgraph [^\n]*\n)(?!\s*%%metro direction:)", re.MULTILINE
)


def _oriented(source: str, orientation: Orientation) -> str:
    """*source* under *orientation*, every section given a direction to turn.

    :func:`transform_source` rotates only the direction directives a map
    declares, so a section left to the default LR would never turn.
    """
    declared = _SUBGRAPH_WITHOUT_DIRECTION.sub(
        r"\1\2\1    %%metro direction: LR\n", source
    )
    return transform_source(declared, orientation)


class Rendered(NamedTuple):
    fixture: Fixture
    graph: MetroGraph
    svg: str
    downgrades: list[str]


@cache
def _render(fixture: str, order: str, orientation: Orientation) -> Rendered:
    spec = FIXTURES[fixture]
    source = _oriented(DECLARATION_ORDERS[order](spec.path.read_text()), orientation)
    return Rendered(spec, *render_with_downgrades(source))


def _side(graph: MetroGraph, fork: str, ahead: str, sid: str) -> float:
    """Signed side of *sid* relative to the trunk run from *fork* to *ahead*."""
    f, a, station = (graph.stations[n] for n in (fork, ahead, sid))
    flow = (a.x - f.x, a.y - f.y)
    rel = (station.x - f.x, station.y - f.y)
    return flow[0] * rel[1] - flow[1] * rel[0]


def _side_of_trunk(r: Rendered, sid: str) -> float:
    """Signed side of *sid* relative to the trunk run leaving the fork."""
    return _side(r.graph, r.fixture.fork, r.fixture.trunk_next, sid)


def _marker_hits(
    graph: MetroGraph, svg: str, only_line: str | None = None
) -> list[tuple[str, str]]:
    """``(line, station)`` pairs where drawn ink crosses a station it skips.

    With *only_line*, just that line's ink is checked.
    """
    offsets = compute_station_offsets(graph)
    segments = [
        seg
        for seg in drawn_segments(parse_route_polylines(svg))
        if only_line is None or seg[0] == only_line
    ]
    hits = []
    for sid, station in graph.stations.items():
        if station.is_port:
            continue
        bbox = _station_marker_bbox(graph, sid, offsets=offsets)
        if bbox is None:
            continue
        lines = set(graph.station_lines(sid))
        hits += [
            (line_id, sid)
            for line_id, p, q in segments
            if line_id not in lines
            and segment_intersects_bbox(p[0], p[1], q[0], q[1], bbox)
        ]
    return sorted(set(hits))


def _ink_crossings(svg: str) -> list[tuple[str, str, object]]:
    """Pairs of distinct lines whose drawn ink crosses, with the crossing point."""
    segments = drawn_segments(parse_route_polylines(svg))
    return [
        (a[0], b[0], point)
        for a, b in combinations(segments, 2)
        if a[0] != b[0] and (point := _segments_cross(a[1], a[2], b[1], b[2]))
    ]


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


@_fixture_cases("test_no_guard_downgrade")
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
    hits = _marker_hits(r.graph, r.svg, only_line=r.fixture.sibling_line)
    assert not hits, f"lines cross markers they skip: {hits}"


@_fixture_cases("test_no_line_crosses_another")
def test_no_line_crosses_another(
    fixture: str, order: str, orientation: Orientation
) -> None:
    """No two lines cross, including beside a station they both stop at.

    ``validate_layout`` forgives a crossing within a quarter row of a station,
    which is exactly where a lane crossover at a join sits, so the drawn ink is
    checked pairwise as well.
    """
    r = _render(fixture, order, orientation)
    crossings = _ink_crossings(r.svg)
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
        f"{lines}%%metro grid: u | 0,0\n%%metro grid: p | 1,0\ngraph LR\n"
        "    subgraph u [Up]\n        x -->|main| a\n    end\n"
        f"    subgraph p [P]\n{body}    end\n"
        "    a -->|main| s\n"
    )


def _framed_carried(lines: str) -> str:
    """The contender's line carried on with the trunk into a section beside it."""
    return (
        f"{lines}%%metro grid: p | 0,0\n%%metro grid: w | 1,0\ngraph LR\n"
        f"    subgraph p [P]\n{_P3_BODY}    end\n"
        "    subgraph w [Down]\n        z[Z]\n    end\n"
        "    m -->|main,c| z\n"
    )


def _fed(lines: str, carried: str = "main,b", *, gridded: bool = True) -> str:
    """The fork's trunk fed from a neighbouring section carrying *carried*."""
    body = _P3_BODY.replace("        a -->|main| s\n", "")
    if "c" in carried.split(","):
        body += "        s -->|c| k\n"
    elif carried != "main,b":
        body = "        r -->|b| s\n" + body.replace("|main|", f"|{carried}|")
    grid = "%%metro grid: u | 0,0\n%%metro grid: p | 1,0\n" if gridded else ""
    return (
        f"{lines}{grid}graph LR\n"
        f"    subgraph u [Up]\n        x -->|{carried}| a\n    end\n"
        f"    subgraph p [P]\n{body}    end\n"
        f"    a -->|{carried}| s\n"
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
    "flat_frame_carried_peel_line_first": _framed_carried(_LINES_BC),
    "flat_frame_carried_sibling_line_first": _framed_carried(_LINES_CB),
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
    "two_joins_with_bypassed_run": _one_section(
        _LINES_BC, _P3_BODY + "        f -->|c| j\n"
    ),
    "peel_ending_beside_the_join": _one_section(
        _LINES_BC,
        "        a -->|main| s\n        s -->|main| d\n"
        "        s -->|b| g\n        f -->|c| d\n",
    ),
    "two_bypassed_runs_to_successive_joins": _one_section(
        _LINES_BC, _P3_BODY + "        m -->|main| n\n        f -->|c| n\n"
    ),
    "fed_bundle_with_the_peel_line": _fed(_LINES_BC),
    "fed_bundle_with_the_peel_line_sibling_line_first": _fed(_LINES_CB),
    "fed_bundle_with_the_peel_line_auto_grid": _fed(_LINES_BC, gridded=False),
    "fed_bundle_without_the_peel_line": _fed(_LINES_BC + _LINE_E, "main,e"),
    "fed_bundle_with_the_contender_line": _fed(_LINES_BC, "main,b,c"),
    "contender_starts_the_peel_line": _one_section(
        _LINES_BC.replace(_LINES_BC.splitlines(True)[2], ""),
        _P3_BODY.replace("f -->|c| m", "f -->|b| m"),
    ),
    "contender_starts_the_peel_line_carried_on": _one_section(
        _LINES_BC.replace(_LINES_BC.splitlines(True)[2], ""),
        _P3_BODY.replace("f -->|c| m", "f -->|b| m") + "        m -->|main,b| n\n",
    ),
}


@pytest.mark.parametrize(
    ("shape", "orientation"),
    _cases(
        "test_contender_shapes_stay_clear",
        [(shape, o) for shape in sorted(SHAPES) for o in all_orientations()],
    ),
    ids=lambda v: v.name if isinstance(v, Orientation) else v,
)
def test_contender_shapes_stay_clear(shape: str, orientation: Orientation) -> None:
    """Other spreads, offset modes, frames and contender mixes hold the rule.

    The contender ``f`` seats on the far side of the trunk from the peel ``g``,
    its line runs through no station it does not serve, and no two lines cross.
    """
    graph, svg, downgrades = render_with_downgrades(
        _oriented(SHAPES[shape], orientation)
    )
    assert not downgrades, downgrades
    assert _side(graph, "s", "d", "f") * _side(graph, "s", "d", "g") < 0, (
        "'f' sits on the peel's side of the trunk"
    )
    hits = _marker_hits(graph, svg)
    assert not hits, f"lines cross markers they skip: {hits}"
    crossings = _ink_crossings(svg)
    assert not crossings, f"lines cross: {crossings}"


_BUNDLE_TO_ONE_JOIN = _one_section(
    "".join(
        f"%%metro line: l{i} | l{i} | #{c}\n"
        for i, c in enumerate(
            ("e41a1c", "377eb8", "4daf4a", "984ea3", "ff7f00", "a65628")
        )
    ),
    "".join(
        f"        {a} -->|{lid}| {b}\n"
        for a, lid, b in (
            ("n1", "l0", "n2"),
            ("n2", "l0", "n3"),
            ("n3", "l0", "n4"),
            ("n4", "l0", "n5"),
            ("n6", "l1,l2", "n5"),
            ("n5", "l1,l2", "n7"),
            ("n1", "l3", "n10"),
            ("n10", "l3", "n5"),
            ("n5", "l3", "n11"),
            ("n12", "l4", "n13"),
            ("n13", "l4", "n5"),
            ("n14", "l5", "n15"),
            ("n15", "l5", "n16"),
        )
    ),
    "%%metro line_spread: centered\n%%metro compact_offsets: true\n",
)


@pytest.mark.parametrize("orientation", list(all_orientations()), ids=lambda o: o.name)
def test_contender_lines_to_one_join_share_a_row(orientation: Orientation) -> None:
    """A contender's lines bound for the same join stay one bundle past the peel.

    Each line goes around a station it skips through its own bypass helper, so
    the bundle holds only while the two lines' helpers pair up position for
    position.
    """
    graph, _, downgrades = render_with_downgrades(
        _oriented(_BUNDLE_TO_ONE_JOIN, orientation)
    )
    assert not downgrades, downgrades
    helpers = [st for sid, st in graph.stations.items() if "_n6_" in sid]
    spots = {(round(st.x), round(st.y)) for st in helpers}
    assert 2 * len(spots) == len(helpers), (
        f"'n6' lines split across rows: {sorted(spots)}"
    )


@pytest.mark.parametrize("with_sibling", [True, False])
def test_a_fork_claims_a_peel_lane_only_beside_a_sibling_source(
    with_sibling: bool,
) -> None:
    fixture = FIXTURES["source_fork"]
    graph = parse_metro_mermaid(fixture.path.read_text())
    section = nx.DiGraph(
        (e.source, e.target)
        for e in graph.edges
        if graph.stations[e.source].section_id == "analysis"
        and graph.stations[e.target].section_id == "analysis"
    )
    if not with_sibling:
        section.remove_node(fixture.sibling)
    line_base = {"main": 0.0, "qc": 1.0, "ref": 2.0}

    lane = _claimed_peel_lane(
        fixture.peel, 1.0, section, {fixture.fork: 0.0}, graph, line_base
    )

    assert lane == (1.0 if with_sibling else None)


@contextmanager
def _deadline(seconds: int) -> Iterator[None]:
    """Fail the enclosing block if it runs longer than *seconds*."""

    def _expire(*_: object) -> None:
        raise TimeoutError(f"exceeded {seconds}s")

    previous = signal.signal(signal.SIGALRM, _expire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def test_seat_without_a_row_between_the_forks_is_left_to_the_ordinary_rule() -> None:
    """Forks barring both sides with no row between them give no contender seat.

    The source is then seated as any other source would be.
    """
    with _deadline(5):
        seat = _seat_contender(0.0, [], [], [(0.0, 1.0), (1.0, -1.0)], 1.0)
    assert seat is None


_LINES_L = "".join(
    f"%%metro line: l{i} | l{i} | {colour}\n"
    for i, colour in enumerate(
        ("#e41a1c", "#377eb8", "#4daf4a", "#984ea3", "#ff7f00", "#a65628")
    )
)
_LINE_W = "%%metro line: w | W | #984ea3\n"
_LINE_Y = "%%metro line: y | Y | #0570b0\n"


class EdgeShape(NamedTuple):
    source: str
    no_crossings: bool


EDGE_SHAPES = {
    "forks_bar_both_sides_without_a_row_between": EdgeShape(
        _one_section(
            _LINES_L,
            "".join(
                f"        {a} -->|{lid}| {b}\n"
                for a, lid, b in (
                    ("n1", "l0,l5", "n2"),
                    ("n2", "l0", "n3"),
                    ("n3", "l0", "n4"),
                    ("n1", "l1", "n3"),
                    ("n3", "l1", "n5"),
                    ("n8", "l2", "n3"),
                    ("n3", "l2", "n9"),
                    ("n9", "l2", "n6"),
                    ("n2", "l3", "n10"),
                    ("n11", "l4", "n6"),
                )
            ),
            "%%metro line_spread: centered\n",
        ),
        no_crossings=False,
    ),
    "peel_past_the_first_join": EdgeShape(
        _one_section(
            _LINES_BC + _LINE_E,
            _P3_BODY.replace("        f -->|c| m\n", "")
            + "        z0 -->|e| z1\n"
            + "        z1 -->|e| x\n"
            + "        f -->|c| x\n"
            + "        f -->|c| m\n",
        ),
        no_crossings=False,
    ),
    "two_trunks_peeling_apart": EdgeShape(
        _one_section(
            "%%metro line: u | U | #e41a1c\n"
            "%%metro line: x | X | #2db572\n"
            + _LINE_Y
            + _LINE_W
            + "%%metro line: c | C | #f5c542\n",
            "".join(
                f"        {a} -->|{lid}| {b}\n"
                for a, lid, b in (
                    ("a", "x,u", "s"),
                    ("s", "x", "s2"),
                    ("s2", "x", "s3"),
                    ("s3", "x", "m"),
                    ("s", "u", "pu"),
                    ("pu", "u", "s3"),
                    ("b", "y,w", "r"),
                    ("r", "y", "r2"),
                    ("r2", "y", "r3"),
                    ("r3", "y", "m"),
                    ("r", "w", "pw"),
                    ("pw", "w", "r3"),
                    ("f", "c", "m"),
                )
            ),
        ),
        no_crossings=True,
    ),
}


@pytest.mark.parametrize("orientation", list(all_orientations()), ids=lambda o: o.name)
@pytest.mark.parametrize("shape", sorted(EDGE_SHAPES))
def test_contender_edge_shapes_render(shape: str, orientation: Orientation) -> None:
    """Shapes at the edge of the contender rule render promptly and unbroken.

    Where no line crosses another with the source seated by the ordinary rule,
    none crosses with it seated as a contender either.
    """
    spec = EDGE_SHAPES[shape]
    with _deadline(60):
        _, svg, downgrades = render_with_downgrades(_oriented(spec.source, orientation))
    assert not downgrades, downgrades
    if spec.no_crossings:
        crossings = _ink_crossings(svg)
        assert not crossings, f"lines cross: {crossings}"
