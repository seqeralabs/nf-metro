"""Tests for the bundle-order-preservation invariant.

Covers:

* Happy-path: every gallery fixture and example yields zero violations
  when passed through :func:`check_bundle_order_preserved`.
* Route-level negative: a synthetic ``RoutedPath`` pair with a
  hand-crafted flipped corner correctly surfaces as a violation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import COORD_TOLERANCE
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import (
    OffsetRegime,
    compute_station_offsets,
    route_edges,
)
from nf_metro.layout.routing.common import (
    Direction,
    RoutedPath,
    apply_route_offsets,
)
from nf_metro.layout.routing.invariants import (
    BundleOrderViolation,
    Side,
    check_bundle_order_preserved,
    check_exit_inherits_entry_bundle_order,
    check_shared_run_turn_preserves_bundle_order,
    check_tb_exit_corner_preserves_column_order,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"
TOPOLOGIES = FIXTURES / "topologies"
EXAMPLES = REPO_ROOT / "examples"


# ---------------------------------------------------------------------------
# Happy-path: every fixture and example must pass the invariant
# ---------------------------------------------------------------------------


def _gather_fixtures() -> list[Path]:
    paths: list[Path] = []
    paths.extend(sorted(FIXTURES.glob("*.mmd")))
    paths.extend(sorted(TOPOLOGIES.glob("*.mmd")))
    paths.extend(sorted(EXAMPLES.glob("*.mmd")))
    paths.extend(sorted((EXAMPLES / "topologies").glob("*.mmd")))
    return paths


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_no_bundle_order_violations_in_gallery(path: Path) -> None:
    """Every shipped topology and example must route without a
    bundle-order violation.

    This is the corpus-level happy-path check.  A regression to a
    routing handler that creates a flipped concentric bundle would
    cause exactly one fixture to start failing here.
    """
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    violations = check_bundle_order_preserved(routes)
    assert violations == [], (
        f"{path.name}: {len(violations)} bundle-order violation(s); "
        f"first: {violations[0].message() if violations else ''}"
    )


# ---------------------------------------------------------------------------
# Cross-column perpendicular-exit -> perpendicular-entry bundles
# ---------------------------------------------------------------------------

_CROSS_COL_PERP_ENTRY_FIXTURES = [
    "lr_perp_top_exit_perp_entry",
    "lr_perp_bottom_exit_perp_entry",
    "lr_perp_top_exit_perp_entry_diverging",
]


@pytest.mark.parametrize("stem", _CROSS_COL_PERP_ENTRY_FIXTURES)
def test_cross_column_perp_entry_preserves_bundle_order(stem: str) -> None:
    """A co-travelling bundle taken over the corridor from a perpendicular
    exit on one LR section into the perpendicular entry of another LR
    section in a different column keeps a single left/right order through
    the whole riser -> corridor -> entry-drop chain.

    The entry-drop leg's per-line channel order must agree with the
    corridor's descent order; a disagreement flips the bundle at the
    entry -> first-station corner, which both trips the runtime guard
    and renders a crossover at the drop.
    """
    path = EXAMPLES / "topologies" / f"{stem}.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    violations = check_bundle_order_preserved(routes)
    assert violations == [], (
        f"{stem}: {len(violations)} bundle-order violation(s); "
        f"first: {violations[0].message() if violations else ''}"
    )


# ---------------------------------------------------------------------------
# Left-entry wrap: bundle order preserved in both riser directions
# ---------------------------------------------------------------------------

_LEFT_ENTRY_WRAP = """\
%%metro title: Left-entry wrap {direction}
%%metro style: dark
%%metro line: w1 | W1 | #e63946
%%metro line: w2 | W2 | #0570b0
%%metro grid: left_tgt | 0,{tgt_row}
%%metro grid: right_src | 1,{src_row}
graph LR
    subgraph left_tgt [Left Target]
        %%metro entry: left | w1, w2
        lt1[Collect]
        lt2[Output]
        lt1 -->|w1,w2| lt2
    end
    subgraph right_src [Right Source]
        rs1[Input R]
        rs2[Hub R]
        rs1 -->|w1,w2| rs2
    end
    rs2 -->|w1,w2| lt1
"""


@pytest.mark.parametrize(
    ("direction", "tgt_row", "src_row"),
    [("up", 0, 1), ("down", 1, 0)],
)
def test_left_entry_wrap_preserves_bundle_order(
    direction: str, tgt_row: int, src_row: int
) -> None:
    """A multi-line bundle wrapping into a LEFT entry keeps one left/right
    order around the wrap, whether the riser climbs (source below the
    target) or descends (source above it).

    Both wrap runs go rightward, so the order is fixed by the port-offset
    stacking rather than the riser's vertical direction.
    """
    text = _LEFT_ENTRY_WRAP.format(
        direction=direction, tgt_row=tgt_row, src_row=src_row
    )
    graph = parse_metro_mermaid(text)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    violations = check_bundle_order_preserved(routes)
    assert violations == [], (
        f"left-entry {direction}-wrap: {len(violations)} bundle-order "
        f"violation(s); first: {violations[0].message() if violations else ''}"
    )


# ---------------------------------------------------------------------------
# TB exit-corner column-order preservation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_no_tb_exit_corner_column_flips_in_gallery(path: Path) -> None:
    """No shipped fixture turns a TB section's bundle out through a LEFT/RIGHT
    exit in an order that disagrees with its in-section vertical column.

    A disagreement crosses the two lines at the feeder station marker.  A
    regression to the exit-corner drop X, the exit-port Y order, or the
    downstream reversal flag would surface as exactly one fixture failing here.
    """
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    violations = check_tb_exit_corner_preserves_column_order(graph, routes, offsets)
    assert violations == [], (
        f"{path.name}: {len(violations)} TB exit-corner column flip(s); "
        f"first: {violations[0].message() if violations else ''}"
    )


_TB_EXIT_CORNER_FIXTURES = [
    EXAMPLES / "topologies" / "junction_entry_reversed_fold.mmd",
    EXAMPLES / "guide" / "04_directions.mmd",
]


@pytest.mark.parametrize("path", _TB_EXIT_CORNER_FIXTURES, ids=lambda p: p.stem)
def test_tb_exit_corner_continues_column_and_keeps_bundle_order(path: Path) -> None:
    """A TB-section bundle exiting LEFT/RIGHT keeps a single order through the
    reversal corner: it continues the in-section column (no crossing at the
    feeder station) and stays bundle-order consistent through every turn.

    Both fixtures route a multi-line bundle out of a TB section through a
    reversal corner into a downstream section, the case where the exit-corner
    drop X, the exit-port Y order, and the downstream reversal flag must agree.
    """
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    corner = check_tb_exit_corner_preserves_column_order(graph, routes, offsets)
    assert corner == [], (
        f"{path.name}: {len(corner)} TB exit-corner column flip(s); "
        f"first: {corner[0].message() if corner else ''}"
    )
    bundle = check_bundle_order_preserved(routes)
    assert bundle == [], (
        f"{path.name}: {len(bundle)} bundle-order violation(s); "
        f"first: {bundle[0].message() if bundle else ''}"
    )


# ---------------------------------------------------------------------------
# Shared-run turn order preservation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_no_shared_run_turn_flips_in_gallery(path: Path) -> None:
    """No shipped fixture turns lines off one shared run onto crossing columns.

    Lines leaving a source bundled on one horizontal run are one bundle while
    they share it, so the column each turns down at must keep that bundle's
    order even where the routes head for different targets.  A regression to the
    gap-bundle seating, to a coincidence fusion's reference column, or to a
    port's lane order would surface as exactly one fixture failing here.
    """
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    violations = check_shared_run_turn_preserves_bundle_order(routes, offsets)
    assert violations == [], (
        f"{path.name}: {len(violations)} shared-run turn flip(s); "
        f"first: {violations[0].message() if violations else ''}"
    )


def test_exit_run_drop_columns_nest_across_three_handlers() -> None:
    """Three lines leaving one exit port turn onto correctly nested columns.

    The bundle out of section C's right exit carries ``main`` on top, then
    ``report``, then ``sheets``, and the three are routed onward by three
    different inter-section handlers -- a direct inter-row L-shape and two
    bypass-trunk feeders.  A rightward run turning down is concentric, so the
    line highest on the run turns at the largest x; nothing reconciling the
    three handlers' column choices lets ``main`` and ``report`` transpose and
    cross twice, once inside the exit arc and again entering the target.

    ``report`` reaches this gap twice -- once on a long bypass whose handler owns
    its own channel, once as a feeder off the exit junction -- and a coincidence
    fusion later pulls the feeder onto the bypass's column.  Seating the bundle
    on that owned column is what keeps ``main`` clear of it.
    """
    path = EXAMPLES / "topologies" / "exit_run_three_drop_columns.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    turns = {
        rp.line_id: (pts[0][1], pts[1][0])
        for rp in routes
        if rp.edge.source == "__junction_9"
        and len(pts := apply_route_offsets(rp, offsets)) >= 3
        and abs(pts[1][1] - pts[0][1]) <= COORD_TOLERANCE
        and abs(pts[2][0] - pts[1][0]) <= COORD_TOLERANCE
        and pts[2][1] > pts[1][1]
    }
    assert {"main", "report"} <= set(turns), turns
    run_y_main, turn_x_main = turns["main"]
    run_y_report, turn_x_report = turns["report"]
    assert run_y_main < run_y_report, turns
    assert turn_x_main > turn_x_report, (
        f"main runs above report (y={run_y_main} vs {run_y_report}) so it must "
        f"turn down at the larger x, but turned at {turn_x_main} vs {turn_x_report}"
    )

    flips = check_shared_run_turn_preserves_bundle_order(routes, offsets)
    at_exit = [v for v in flips if v.source_id == "__junction_9"]
    assert at_exit == [], (
        f"{len(at_exit)} shared-run turn flip(s) at the exit junction; first: "
        f"{at_exit[0].message() if at_exit else ''}"
    )


def test_riboseq_u_turn_corridor_is_accepted() -> None:
    """A shared run that U-turns onto a reversed corridor is well-nested.

    At riboseq's ``__junction_13`` two lines leave one rightward run, drop, and
    turn back onto a leftward corridor before peeling into different downstream
    sections.  Across that U-turn ``riboseq`` stays above ``rnaseq`` on both the
    run and the corridor, so their order is preserved: whichever crossing the
    shape forces is inherent to the topology, not a fixable bundle-order flip.

    The single opening corner alone would read this as a flip -- the upper line
    turns at the smaller x -- but that proxy is only valid for a terminal or
    same-direction turn, not a corridor that reverses the frame.
    """
    path = (
        REPO_ROOT
        / "tests"
        / "fixtures"
        / "curve_invariant_repros"
        / "riboseq_inter_row_corridor.mmd"
    )
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    flips = check_shared_run_turn_preserves_bundle_order(routes, offsets)
    at_junction = [v for v in flips if v.source_id == "__junction_13"]
    assert at_junction == [], (
        f"{len(at_junction)} shared-run turn flip(s) at the U-turn junction; "
        f"first: {at_junction[0].message() if at_junction else ''}"
    )


def test_translated_u_turn_fold_is_exempt() -> None:
    """Two congruent U-turn brackets offset the same way are a forced crossing.

    ``A`` sits above ``B`` on the run and stays above it on the reversed
    corridor, so their U's are congruent brackets translated down-right together;
    congruent brackets offset that way intersect wherever they are placed.  The
    crossing is topologically forced, not an ordering bug, so the fold is exempt.
    """
    a = _synthetic_route(
        "A", [(0.0, 100.0), (200.0, 100.0), (200.0, 396.0), (50.0, 396.0)], "__hub__"
    )
    b = _synthetic_route(
        "B", [(0.0, 104.0), (204.0, 104.0), (204.0, 400.0), (60.0, 400.0)], "__hub__"
    )
    assert check_shared_run_turn_preserves_bundle_order([a, b], {}) == []


def test_concentric_u_turn_fold_with_wrong_turn_column_is_caught() -> None:
    """A nestable fold whose risers cross is an avoidable bundle flip.

    ``A``'s run and corridor both enclose ``B``'s (higher run, lower corridor),
    so the two U's are concentric about one centre and could nest cleanly.  But
    ``A`` turns at the smaller x, threading its riser inside ``B``'s: the outer
    bracket crosses the inner one at the corner.  The exemption is scoped to
    translated folds, so this concentric crossing is surfaced.
    """
    a = _synthetic_route(
        "A", [(0.0, 100.0), (200.0, 100.0), (200.0, 400.0), (50.0, 400.0)], "__hub__"
    )
    b = _synthetic_route(
        "B", [(0.0, 104.0), (204.0, 104.0), (204.0, 396.0), (60.0, 396.0)], "__hub__"
    )
    violations = check_shared_run_turn_preserves_bundle_order([a, b], {})
    assert [(v.line_a, v.line_b) for v in violations] == [("A", "B")], (
        f"expected one A/B shared-run flip at the concentric fold's corner; "
        f"got {[(v.line_a, v.line_b) for v in violations]}"
    )


def _synthetic_route(
    line_id: str, points: list[tuple[float, float]], source: str = "__src__"
) -> RoutedPath:
    """Build a ``RoutedPath`` from a points list for testing.

    ``source`` and the target ``'__tgt__'`` default to fixed ids so a pair
    shares a bundle key; :func:`check_shared_run_turn_preserves_bundle_order`
    groups routes by ``source`` alone, so a shared source with differing targets
    stands in for a bundle that peels to different sections.  The ``Edge`` carries
    the line id; the rest of the routing metadata is irrelevant to the checks.
    """
    return RoutedPath(
        edge=Edge(source=source, target="__tgt__", line_id=line_id),
        line_id=line_id,
        points=points,
        is_inter_section=True,
        offset_regime=OffsetRegime.BAKED,
    )


def test_check_skips_clean_bundle() -> None:
    """Two paths that share waypoints exactly produce zero violations:
    a coincident path-pair has nothing to compare on either side.
    """
    pts = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (200.0, 100.0)]
    routes = [_synthetic_route("A", pts), _synthetic_route("B", pts)]
    assert check_bundle_order_preserved(routes) == []


def test_check_skips_single_line_bundle() -> None:
    """A bundle with only one line has no pairs to compare; no
    violation is possible.
    """
    pts = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (200.0, 100.0)]
    routes = [_synthetic_route("A", pts)]
    assert check_bundle_order_preserved(routes) == []


def test_synthetic_flipped_corner_is_caught() -> None:
    """A hand-crafted bundle with a deliberate flip at a near-shared
    corner surfaces as a :class:`BundleOrderViolation`.

    Two L-shape routes whose elbows are half a pixel apart on both
    axes: A is on the LEFT of B going east, then on the RIGHT going
    south.  LEFT -> RIGHT is exactly the flip the invariant exists to
    catch.
    """
    a_pts = [
        (0.0, 100.0),
        (100.0, 100.0),
        (100.0, 200.0),
    ]
    b_pts = [
        (0.0, 100.5),
        (100.5, 100.5),
        (100.5, 200.0),
    ]
    routes = [_synthetic_route("A", a_pts), _synthetic_route("B", b_pts)]
    violations = check_bundle_order_preserved(routes)
    assert violations, "expected a synthetic bundle-order violation; got an empty list"
    v = violations[0]
    assert v.line_a == "A" and v.line_b == "B"
    assert v.in_tangent is Direction.R
    assert v.out_tangent is Direction.D
    assert {v.before, v.after} == {Side.LEFT, Side.RIGHT}, v.message()


def test_violation_message_self_describing() -> None:
    """The violation's ``message()`` includes the corner xy, line ids,
    tangent directions, and the offending before/after sides - the
    fields downstream callers (the engine guard and CI logs) rely on
    for diagnosis.
    """
    v = BundleOrderViolation(
        edge_source="src",
        edge_target="tgt",
        line_a="alpha",
        line_b="beta",
        corner_xy=(100.0, 200.0),
        in_tangent=Direction.D,
        out_tangent=Direction.L,
        before=Side.LEFT,
        after=Side.RIGHT,
    )
    msg = v.message()
    assert "100.0" in msg and "200.0" in msg
    assert "alpha" in msg and "beta" in msg
    assert "D" in msg and "L" in msg
    assert "LEFT" in msg and "RIGHT" in msg


def test_disjoint_runs_sharing_a_name_are_free_to_differ_at_the_two_ends() -> None:
    """An inherited exit slot binds a stroke that spans the section.

    In this section the entry bundle terminates inside and separate runs of the
    same lines leave it, so the two ends share names but no stroke: holding the
    exit to the entry's order would constrain geometry that is not connected.
    Chaining the two halves gives each name one stroke end to end, and the same
    offsets then are a violation.

    This fixture's entry and exit also sit on one materialized linear-entry
    frame, which threads first/second/third identically end to end for an
    unrelated, separately-tested reason (see
    ``test_linear_entry_cohort_keeps_one_lane_frame``): the offsets
    :func:`compute_station_offsets` hands back naturally agree at both ports.
    The exit port's offsets are overridden below (reversing the entry order)
    so this test exercises the disjoint-run exemption on its own, independent
    of that separate frame invariant.
    """
    path = EXAMPLES / "topologies" / "target_lane_transition.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    offsets = dict(compute_station_offsets(graph))
    entry_id = "source__entry_left_2"
    exit_id = "source__exit_right_1"
    shared = ("first", "second", "third")

    entry_order = [offsets[(entry_id, line_id)] for line_id in shared]
    exit_keys = ((exit_id, line_id) for line_id in shared)
    offsets.update(zip(exit_keys, reversed(entry_order)))

    assert [offsets[(entry_id, line_id)] for line_id in shared] != [
        offsets[(exit_id, line_id)] for line_id in shared
    ]
    assert not check_exit_inherits_entry_bundle_order(graph, offsets)

    for line_id in shared:
        graph.add_edge(Edge("enter", "leave", line_id))

    violations = check_exit_inherits_entry_bundle_order(graph, offsets)
    assert [item.exit_port for item in violations] == [exit_id]


def test_exit_lane_rise_keeps_the_entry_bundle_order() -> None:
    """A bundle climbing to an off-trunk exit port keeps its entry order.

    ``mid`` is taller than the section it feeds, so its RIGHT exit port sits a
    lane above the trunk and the bundle climbs to reach it.  The two lines
    reach the single entry port from different rows, which the entry-port
    phases re-slot after the exit port has already taken an order; the climb
    must carry the re-slot through rather than crossing the pair on the rise.
    """
    path = EXAMPLES / "topologies" / "exit_lane_rise_bundle_order.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)

    assert check_exit_inherits_entry_bundle_order(graph, offsets) == []
    compute_layout(parse_metro_mermaid(path.read_text()), validate=True)
