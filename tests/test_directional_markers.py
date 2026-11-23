"""Tests for static directional chevrons (``--directional``)."""

import math
import pathlib

import pytest

from nf_metro.api import RenderConfig, prepare_graph, render_graph, resolve_theme
from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid_file
from nf_metro.render.svg import render_svg
from nf_metro.render.validate import (
    _CHEVRON_CENTRE_TOL,
    StrokeTrace,
    _point_segment_distance,
    _runs_along,
    check_chevrons_on_stroke,
    parse_chevrons,
    parse_route_strokes,
)
from nf_metro.themes import NFCORE_DARK_THEME

EXAMPLES_DIR = pathlib.Path(__file__).parent.parent / "examples"

DIRECTION_FIXTURES = [
    "simple_pipeline",
    "rnaseq_auto",
    "genomic_pipeline",
    "longread_variant_calling",
]


def _laid_out(stem: str):
    graph = parse_metro_mermaid_file(EXAMPLES_DIR / f"{stem}.mmd")
    compute_layout(graph)
    return graph


def _render(stem: str, *, directional: bool) -> str:
    graph = _laid_out(stem)
    graph.directional = directional
    return render_svg(graph, NFCORE_DARK_THEME)


def test_directional_off_by_default_emits_no_chevrons():
    svg = _render("simple_pipeline", directional=False)
    assert "metro-direction-" not in svg


@pytest.mark.parametrize("stem", DIRECTION_FIXTURES)
def test_directional_flag_emits_chevrons(stem):
    svg = _render(stem, directional=True)
    assert "metro-direction-" in svg


# Tight rounded corners under a heavy stroke.
FLOATING_CHEVRONS = """\
%%metro title: Floating chevrons
%%metro line: a | Line A | #e6007e
%%metro line: b | Line B | #1f3a93
%%metro line: c | Line C | #29b6f6
%%metro stroke_scale: 2.5
%%metro grid: s1 | 0,0
%%metro grid: s2 | 0,1

graph LR
    subgraph s1 [Section 1]
        a1[A1]
        a2[A2]
        a3[A3]
        a1 -->|a,b,c| a2
        a2 -->|a,b,c| a3
    end
    subgraph s2 [Section 2]
        b1[B1]
        b2[B2]
        b3[B3]
        b1 -->|a,b,c| b2
        b2 -->|a,b,c| b3
    end
    a3 -->|a,b,c| b1
"""


def _example_text(rel: str, prefix: str = "") -> tuple[str, str]:
    path = EXAMPLES_DIR / rel
    return prefix + path.read_text(), str(path.parent)


# Rounded corners at several stroke scales, plus maps whose routes carry
# bridge gaps where a chevron would otherwise land.
DRAWN_STROKE_CASES = {
    "floating_chevrons": lambda: (FLOATING_CHEVRONS, ""),
    "rnaseq_sections_stroke_1_5": lambda: _example_text(
        "rnaseq_sections.mmd", "%%metro stroke_scale: 1.5\n"
    ),
    "genomic_pipeline": lambda: _example_text("genomic_pipeline.mmd"),
    "junction_entry_lane_rebase": lambda: _example_text(
        "topologies/junction_entry_lane_rebase.mmd"
    ),
}


def _directional_svg(text: str, source_dir: str) -> str:
    graph = prepare_graph(text, source_dir=source_dir)
    graph.directional = True
    return render_graph(graph, resolve_theme(None, graph), RenderConfig())


@pytest.mark.parametrize("case", sorted(DRAWN_STROKE_CASES))
def test_chevrons_ride_their_drawn_stroke(case):
    """Every chevron sits on, and points along, its own line's painted stroke.

    The stroke is read back from the SVG with its quadratic corners, so a
    chevron inside a rounded corner is measured against the curve, and one
    reaching into a bridge gap is caught as well.
    """
    svg = _directional_svg(*DRAWN_STROKE_CASES[case]())
    assert parse_chevrons(svg), f"{case} drew no chevrons"
    findings = check_chevrons_on_stroke(svg)
    assert not findings, [f.message for f in findings]


PUBLISHED_DIRECTIONAL = sorted(
    path.relative_to(EXAMPLES_DIR).as_posix()
    for path in EXAMPLES_DIR.rglob("*.mmd")
    if "%%metro directional: true" in path.read_text()
)

_JOIN_TOL = 0.01
_JOIN_HEADING_COS = 0.9999


class _Trace:
    """A drawn stroke with the arc length at the start of each chord."""

    def __init__(self, trace: StrokeTrace) -> None:
        self.line_id = trace.line_id
        self.chords = [(a, b) for a, b in trace.chords if math.dist(a, b) > 1e-9]
        self.starts: list[float] = []
        travelled = 0.0
        pen = self.chords[0][0]
        for a, b in self.chords:
            travelled += math.dist(pen, a)
            self.starts.append(travelled)
            travelled += math.dist(a, b)
            pen = b
        self.length = travelled

    @staticmethod
    def _unit(a, b):
        length = math.dist(a, b)
        return ((b[0] - a[0]) / length, (b[1] - a[1]) / length)

    @property
    def head(self):
        return self.chords[0][0], self._unit(*self.chords[0])

    @property
    def tail(self):
        return self.chords[-1][1], self._unit(*self.chords[-1])

    def arc_of(self, centre, heading) -> float | None:
        """Arc position of a chevron lying on this stroke, else None."""
        best: tuple[float, float] | None = None
        for (a, b), start in zip(self.chords, self.starts, strict=True):
            distance = _point_segment_distance(centre, a, b)
            if distance > _CHEVRON_CENTRE_TOL or not _runs_along(heading, a, b):
                continue
            length = math.dist(a, b)
            along = (
                (centre[0] - a[0]) * (b[0] - a[0]) + (centre[1] - a[1]) * (b[1] - a[1])
            ) / length
            if best is None or distance < best[0]:
                best = (distance, start + min(max(along, 0.0), length))
        return None if best is None else best[1]


def _joins(traces: list[_Trace]) -> dict[int, int]:
    """Trace index -> the one trace that continues it end to end, same heading."""
    nxt: dict[int, int] = {}
    for i, a in enumerate(traces):
        end, end_dir = a.tail
        enders = [
            j
            for j, t in enumerate(traces)
            if t.line_id == a.line_id and math.dist(t.tail[0], end) <= _JOIN_TOL
        ]
        starters = [
            j
            for j, t in enumerate(traces)
            if t.line_id == a.line_id and math.dist(t.head[0], end) <= _JOIN_TOL
        ]
        if len(enders) != 1 or len(starters) != 1 or starters[0] == i:
            continue
        start_dir = traces[starters[0]].head[1]
        if end_dir[0] * start_dir[0] + end_dir[1] * start_dir[1] >= _JOIN_HEADING_COS:
            nxt[i] = starters[0]
    return nxt


def chevron_run_gaps(svg: str) -> list[tuple[float, str, tuple[float, float]]]:
    """Gap between consecutive same-line chevrons along every end-to-end run.

    A run is a chain of drawn strokes meeting end to start at one point with
    one heading, so chevrons on coincident ink are measured on a common axis.
    Each entry is ``(gap, line_id, centre of the later chevron)``.
    """
    traces = [
        _Trace(t)
        for t in parse_route_strokes(svg)
        if any(math.dist(a, b) > 1e-9 for a, b in t.chords)
    ]
    chevrons = parse_chevrons(svg)
    nxt = _joins(traces)
    continued = set(nxt.values())
    gaps: list[tuple[float, str, tuple[float, float]]] = []
    for head in (i for i in range(len(traces)) if i not in continued):
        chain: list[tuple[int, float]] = []
        offset = 0.0
        i: int | None = head
        while i is not None and i not in (c for c, _ in chain):
            chain.append((i, offset))
            offset += traces[i].length
            i = nxt.get(i)
        line_id = traces[head].line_id
        placed: list[tuple[float, int]] = []
        for index, chevron in enumerate(chevrons):
            if chevron.line_id != line_id:
                continue
            for trace_index, base in chain:
                arc = traces[trace_index].arc_of(chevron.centre, chevron.heading)
                if arc is not None:
                    placed.append((base + arc, index))
        placed.sort()
        for (a, ia), (b, ib) in zip(placed, placed[1:], strict=False):
            if ia != ib:
                gaps.append((b - a, line_id, chevrons[ib].centre))
    return gaps


@pytest.mark.parametrize("stem", PUBLISHED_DIRECTIONAL)
def test_same_line_chevrons_form_one_evenly_spaced_set(stem):
    """Coincident same-line routes do not stack interleaved chevron sets.

    Consecutive chevrons of one line, measured along each end-to-end run of its
    strokes, are never closer than half the marker spacing.
    """
    text, source_dir = _example_text(stem)
    svg = _directional_svg(text, source_dir)
    graph = prepare_graph(text, source_dir=source_dir)
    spacing = resolve_theme(None, graph).directional_marker_spacing
    doubled = [
        f"line {line_id!r} near ({x:.1f},{y:.1f}): {gap:.2f}px"
        for gap, line_id, (x, y) in chevron_run_gaps(svg)
        if gap < spacing / 2 - 0.5
    ]
    assert not doubled, doubled
