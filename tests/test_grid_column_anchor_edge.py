"""A grid column's boxes meet on each X edge their content is anchored to.

A grid row's sections share a trunk Y, so levelling their bbox tops always lines
up something a viewer reads, and it is the header badge -- text -- that rides the
box top.  A grid column's sections share no trunk X and neither X edge carries
anything comparable, so both are levelled: the space between a box edge and the
content nearest it is that section's runway on that side, and two column mates
only mean the same thing by it when that content stands at one X.
``_level_column_anchor_edges`` (Stage 3.6) levels exactly those, growing the
shorter boxes outward so interiors and the opposite edge stay put.  Column mates
whose content starts at different X keep their own edges -- levelling those would
buy an aligned edge at the price of an empty band the width of the difference.

Covers:

* Corpus: within every shared-runway run, each member either sits on the run's
  edge for that side or a neighbour in its own row band explains the shortfall.
* Corpus: the runway spread within a grid column never exceeds what the stage's
  own inputs already had, so levelling can only equalise interior space.
* Meaningfulness: shipped fixtures hold runs the stage actually levelled, and
  column mates it declined because their content starts at different X, so
  neither the property nor the restriction is vacuous; and both X sides occur,
  so the right-edge half is not vacuous either.
* Regression: ``convergence_fold_diamond``'s two mirror branches end on one left
  edge with equal runways, while the ``finish`` box below them -- whose content
  starts 90px further left relative to its box -- keeps its own edge.
* Regression: ``foldback_exit_peeloff`` and ``fold_bypass_creep``, columns whose
  RL member has section placement seat them on the right, end on one right edge.
* Regression: ``foldback_exit_peeloff``'s ``reporting`` box, whose content starts
  197.5px right of its column mate's, keeps its own left edge.
* The neighbour-corridor bound, on a hand-built grid: no shipped fixture stacks a
  wide left-column section beside a narrow column mate.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest

from nf_metro.layout.constants import MIN_INTER_SECTION_GAP, SAME_COORD_TOLERANCE
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases._common import (
    _column_contiguous_row_groups,
    _content_station_ids,
    section_anchor_edge,
)
from nf_metro.layout.phases.bbox import (
    COLUMN_ANCHOR_SIGNS,
    _column_neighbour_anchor_limit,
    _level_column_anchor_edges,
    _shared_anchor_runway_runs,
)
from nf_metro.layout.phases.guards import _column_run_anchor_shortfalls
from nf_metro.layout.routing.invariants import CurveInvariantError
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, Section

REPO_ROOT = Path(__file__).resolve().parent.parent


def _gather_fixtures() -> list[Path]:
    paths: list[Path] = []
    for rel in (
        "tests/fixtures/topologies",
        "tests/fixtures",
        "examples",
        "examples/topologies",
    ):
        paths.extend(sorted((REPO_ROOT / rel).glob("*.mmd")))
    return paths


def _runway(graph: MetroGraph, section: Section, sign: float = 1.0) -> float | None:
    """Distance from the *sign*-anchored box edge to the content nearest it."""
    xs = [graph.stations[sid].x for sid in _content_station_ids(graph, section)]
    if not xs:
        return None
    nearest = min(xs) if sign > 0 else max(xs)
    return (nearest - section_anchor_edge(section, "x", sign)) * sign


def _unexplained_short_edges(graph: MetroGraph) -> list[tuple[str, float]]:
    """``(section_id, shortfall)`` for every box short of its run's anchored
    edge that the neighbour-corridor bound does not account for.

    Reads the scan the guard raises from, so the two cannot disagree about what
    counts as a shortfall.
    """
    return [
        (short.section.id, short.shortfall)
        for short in _column_run_anchor_shortfalls(graph)
    ]


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_shared_runway_runs_share_an_anchored_edge(path: Path) -> None:
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    unexplained = _unexplained_short_edges(graph)
    assert not unexplained, (
        f"{path.name}: sections sit short of the anchored edge of the run they "
        f"share a runway with, with no neighbour to explain it: {unexplained}"
    )


def _column_runway_spreads(graph: MetroGraph) -> dict[tuple[int, float], float]:
    """Widest minus narrowest content runway, per grid column and X side."""
    by_col: dict[tuple[int, float], list[float]] = defaultdict(list)
    for section in graph.sections.values():
        if section.bbox_w <= 0 or section.grid_col < 0:
            continue
        for sign in COLUMN_ANCHOR_SIGNS:
            runway = _runway(graph, section, sign)
            if runway is not None:
                by_col[(section.grid_col, sign)].append(runway)
    return {
        key: max(runways) - min(runways)
        for key, runways in by_col.items()
        if len(runways) >= 2
    }


def test_levelling_never_spreads_a_column_runway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No grid column comes out of Stage 3.6 with its runways further apart.

    The stage exists to equalise the space in front of a column's content, so
    running it must not leave any column's runways more unequal than they were
    when it started -- the failure mode of levelling boxes whose content starts
    at different X.  Compares every corpus fixture against the same layout with
    the stage stubbed out.
    """
    worse: list[tuple[str, tuple[int, float], float, float]] = []
    for path in _gather_fixtures():
        text = path.read_text()
        with monkeypatch.context() as patched:
            patched.setattr(
                "nf_metro.layout.engine._level_column_anchor_edges",
                lambda graph: None,
            )
            without = parse_metro_mermaid(text)
            compute_layout(without)
        graph = parse_metro_mermaid(text)
        compute_layout(graph)
        before = _column_runway_spreads(without)
        after = _column_runway_spreads(graph)
        for key, spread in after.items():
            if spread > before.get(key, spread) + SAME_COORD_TOLERANCE:
                worse.append((path.name, key, before[key], spread))
    assert not worse, f"columns left with wider-spread runways: {worse}"


def test_corpus_levels_shared_runways_and_leaves_the_rest() -> None:
    """The property, the restriction and both anchor sides are all exercised."""
    levelled = 0
    declined = 0
    signs: set[float] = set()
    for path in _gather_fixtures():
        graph = parse_metro_mermaid(path.read_text())
        try:
            compute_layout(graph)
        except CurveInvariantError:
            continue
        for group in _column_contiguous_row_groups(graph):
            for sign in COLUMN_ANCHOR_SIGNS:
                runs = _shared_anchor_runway_runs(graph, group, sign)
                if runs:
                    signs.add(sign)
                levelled += sum(
                    1
                    for run in runs
                    if len({round(section_anchor_edge(s, "x", sign), 1) for s in run})
                    == 1
                )
                declined += len(group) - sum(len(run) for run in runs)
    assert levelled > 0, "no shipped fixture has a levelled grid column"
    assert declined > 0, "no shipped column mate is held out of the levelling"
    assert signs == {1.0, -1.0}, (
        f"only anchor sides {sorted(signs)} are levelled; the property is half-vacuous"
    )


def _stacked_column_graph() -> MetroGraph:
    """A column whose lower cell is narrower than its upper, with a wide section
    in the previous column beside the lower cell only."""
    graph = MetroGraph()
    specs = [
        ("upper", 0, 1, 300.0, 0.0, 200.0),
        ("lower", 1, 1, 380.0, 200.0, 120.0),
        ("blocker", 1, 0, 0.0, 200.0, 330.0),
    ]
    for sid, row, col, x, y, w in specs:
        graph.sections[sid] = Section(
            id=sid,
            name=sid,
            grid_row=row,
            grid_col=col,
            bbox_x=x,
            bbox_y=y,
            bbox_w=w,
            bbox_h=100.0,
        )
    return graph


def _seat_one_station(graph: MetroGraph, section_id: str, x: float) -> None:
    """Give ``section_id`` a single content station at ``x``, so the section can
    join a shared-runway run."""
    from nf_metro.parser.model import Station

    sid = f"{section_id}_station"
    section = graph.sections[section_id]
    graph.stations[sid] = Station(
        id=sid, label=sid, section_id=section_id, x=x, y=section.bbox_y + 50.0
    )
    section.station_ids.append(sid)


def test_neighbour_limit_keeps_the_inter_column_corridor() -> None:
    """A left neighbour sharing the row band reserves the routing corridor."""
    graph = _stacked_column_graph()
    assert _column_neighbour_anchor_limit(graph, graph.sections["lower"], 1.0) == (
        330.0 + MIN_INTER_SECTION_GAP
    )
    assert _column_neighbour_anchor_limit(graph, graph.sections["upper"], 1.0) == float(
        "-inf"
    )


def test_alignment_stops_at_the_neighbour_corridor() -> None:
    """The levelling stops short of a left neighbour rather than growing over it."""
    graph = _stacked_column_graph()
    for section_id in ("upper", "lower"):
        _seat_one_station(graph, section_id, 450.0)
    _level_column_anchor_edges(graph)
    lower = graph.sections["lower"]
    assert lower.bbox_x == 330.0 + MIN_INTER_SECTION_GAP
    assert lower.bbox_x + lower.bbox_w == 500.0


def test_column_mates_with_different_content_columns_keep_their_edges() -> None:
    """Content starting at different X splits the column into separate runs."""
    graph = _stacked_column_graph()
    _seat_one_station(graph, "upper", 450.0)
    _seat_one_station(graph, "lower", 500.0)
    upper, lower = graph.sections["upper"], graph.sections["lower"]
    assert _shared_anchor_runway_runs(graph, [upper, lower], 1.0) == []
    _level_column_anchor_edges(graph)
    assert lower.bbox_x == 380.0


def test_mirror_branches_share_a_left_edge_and_runway() -> None:
    """``convergence_fold_diamond``'s two branches level onto one edge."""
    path = REPO_ROOT / "examples" / "topologies" / "convergence_fold_diamond.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    left = graph.sections["branch_left"]
    right = graph.sections["branch_right"]
    assert abs(left.bbox_x - right.bbox_x) <= SAME_COORD_TOLERANCE
    assert abs(_runway(graph, left) - _runway(graph, right)) <= SAME_COORD_TOLERANCE
    finish = graph.sections["finish"]
    assert finish.bbox_x < left.bbox_x - SAME_COORD_TOLERANCE, (
        "the branches were levelled onto the box below them, whose content starts "
        "further left relative to its edge"
    )


@pytest.mark.parametrize(
    ("fixture", "section_ids"),
    [
        ("foldback_exit_peeloff", ("preprocessing", "reporting")),
        ("fold_bypass_creep", ("prep", "report")),
    ],
)
def test_right_anchored_column_shares_its_right_edge(
    fixture: str, section_ids: tuple[str, ...]
) -> None:
    """A column an RL member anchors on the right ends on one right edge."""
    path = REPO_ROOT / "examples" / "topologies" / f"{fixture}.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    rights = {
        round(graph.sections[sid].bbox_x + graph.sections[sid].bbox_w, 1)
        for sid in section_ids
    }
    assert len(rights) == 1, f"{fixture}: right edges {sorted(rights)} disagree"


def test_peeloff_reporting_box_keeps_its_own_left_edge() -> None:
    """A column mate whose content starts far right keeps its unanchored edge."""
    path = REPO_ROOT / "examples" / "topologies" / "foldback_exit_peeloff.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    preprocessing = graph.sections["preprocessing"]
    reporting = graph.sections["reporting"]
    assert reporting.bbox_x > preprocessing.bbox_x + SAME_COORD_TOLERANCE
    assert (
        abs(_runway(graph, reporting) - _runway(graph, preprocessing))
        <= SAME_COORD_TOLERANCE
    )
