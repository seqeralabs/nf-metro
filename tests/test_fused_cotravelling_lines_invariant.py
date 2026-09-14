"""Tests for the fused co-travelling distinct-line invariant.

Two DIFFERENT lines running the same way along one corridor nest a full
``OFFSET_STEP`` apart, which is what leaves a hairline of background showing
between their strokes.  Closed to less than that they paint one two-tone
stripe and one of the two lines is not there to read.

The defect can appear in planning and again on the settled re-route, because
both stages place tracks inside shared reservation bands. The reported fixtures
are exercised through the render chokepoint rather than a single
``route_edges`` call.

The checker observes plan-owned tracks as well as tracks that can be re-seated.
An immutable violation is attributed to the route system and plans that should
have separated it before geometry froze.

Covers:

* Happy-path: every shipped topology and example routes with no fused pair.
* Targeted: the four corridors a reservation band pulled together
  (``rl_return_row_convergence``, ``convergence_fold_diamond``,
  ``seed72_cross_family_fan``, ``inter_row_corridor_overflow``) keep the
  full step on the settled geometry.
* Meaningfulness: on the fixtures whose bands leave the pair short of the step
  the tracks fuse once both separation stages are disabled, and settlement
  lands each pair exactly on the step rather than merely clear of the check.
* Corpus ratchet: the checker has no silent fused-pair exemptions.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

import nf_metro.layout.routing.convergences as convergences
import nf_metro.layout.routing.core as routing_core
import nf_metro.layout.routing.invariants as invariants
import nf_metro.layout.routing.member_geometry as member_geometry
from nf_metro.layout.constants import COORD_TOLERANCE, graph_offset_step
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import (
    compute_station_offsets,
    observe_route_edges,
    route_edges,
)
from nf_metro.layout.routing.common import (
    OffsetRegime,
    RoutedPath,
    apply_route_offsets,
)
from nf_metro.layout.routing.invariants import (
    _routes_crossings,
    check_no_fused_cotravelling_lines,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge, MetroGraph

REPO_ROOT = Path(__file__).resolve().parent.parent
TOPOLOGIES = REPO_ROOT / "tests" / "fixtures" / "topologies"
EXAMPLES = REPO_ROOT / "examples"
EXAMPLE_TOPOLOGIES = EXAMPLES / "topologies"
FIXTURES = REPO_ROOT / "tests" / "fixtures"
CURVE_REPROS = FIXTURES / "curve_invariant_repros"
REGRESSIONS = FIXTURES / "regressions"
THROUGH_SECTION = FIXTURES / "through_section"
FROZEN_FUZZ = FIXTURES / "hash_seed_determinism"

REPORTED = {
    CURVE_REPROS / "rl_return_row_convergence.mmd": frozenset(
        {("bam", "other", "Y"), ("bam", "snvvcf", "Y")}
    ),
    EXAMPLE_TOPOLOGIES / "convergence_fold_diamond.mmd": frozenset(
        {("left_path", "right_path", "X")}
    ),
    EXAMPLE_TOPOLOGIES / "seed72_cross_family_fan.mmd": frozenset(
        {("exempt", "normal", "X"), ("exempt", "normal", "Y")}
    ),
    CURVE_REPROS / "inter_row_corridor_overflow.mmd": frozenset({("la", "lb", "Y")}),
}

FUSED_WITHOUT_THE_PASS = {
    CURVE_REPROS / "rl_return_row_convergence.mmd": frozenset(
        {("bam", "other", "Y"), ("bam", "snvvcf", "Y")}
    ),
    REGRESSIONS / "entry_trunk_row_bow.mmd": frozenset({("l1", "l2", "Y")}),
}

# Pairs whose owner seats them on the pitch outright, so the separation stages
# have nothing left to repair.  Held as a ledger of its own rather than dropped:
# a pair that stops being correct by construction and starts depending on the
# repair again is a regression, and only a positive lock catches it.
SEATED_WITHOUT_THE_PASS = {
    EXAMPLE_TOPOLOGIES / "packed_multiline_serpentine_grid.mmd": frozenset(
        {("l1", "l2", "X")}
    ),
    THROUGH_SECTION / "riboseq_packed_lr.mmd": frozenset({("riboseq", "rnaseq", "X")}),
}


def _gather_fixtures() -> list[Path]:
    paths: list[Path] = []
    paths.extend(sorted(TOPOLOGIES.glob("*.mmd")))
    paths.extend(sorted(EXAMPLES.glob("*.mmd")))
    return paths


def _corpus() -> list[Path]:
    paths = sorted(EXAMPLES.rglob("*.mmd"))
    paths += sorted((REPO_ROOT / "tests" / "fixtures").rglob("*.mmd"))
    paths += sorted((REPO_ROOT / "tests" / "fixtures").rglob("*.metro"))
    return paths


_CORPUS = _corpus()

_ExemptPair = tuple[str, str, str, str, float, float]

# The live checker has no semantic exemptions. The empty ledger makes any silent
# corpus pair a test failure.
EXEMPT_FUSED_PAIRS: frozenset[_ExemptPair] = frozenset()


def _route(path: Path):
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return graph, routes, offsets


def _settled(path: Path, monkeypatch: pytest.MonkeyPatch):
    """The geometry the renderer draws, plus the violations the chokepoint saw.

    The check is replaced by a recording stand-in that reports nothing, so the
    render runs to completion on a fixture carrying the defect and the test can
    measure its final geometry rather than only catch the abort.
    The collinearity guard is suppressed because it detects the same induced
    bad geometry first and would prevent that measurement.
    """
    from nf_metro.api import prepare_graph, resolve_theme
    from nf_metro.render.svg import build_observed_render_plan

    final: list[tuple] = []

    def spy(graph, routes, offsets):
        found = check_no_fused_cotravelling_lines(graph, routes, offsets)
        final.clear()
        final.append((graph, routes, offsets, found))
        return []

    monkeypatch.setattr(invariants, "check_no_fused_cotravelling_lines", spy)
    monkeypatch.setattr(
        invariants, "check_collinear_distinct_lines", lambda *_args, **_kwargs: []
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        build_observed_render_plan(graph, resolve_theme(None, graph))
    assert final, "the render chokepoint never ran the check"
    return final[0]


def _disable_separation_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        routing_core,
        "_separate_fused_cotravelling_runs",
        lambda routes, ctx, **kwargs: None,
    )
    monkeypatch.setattr(
        member_geometry,
        "_separate_fused_cotravelling_runs",
        lambda routes, ctx, **kwargs: None,
    )


def _pair_separations(routes, offsets) -> dict[tuple[str, str, str], float]:
    """Lateral separation of every co-travelling distinct-line track pair."""
    from nf_metro.layout.routing.common import (
        apply_route_offsets,
        corridor_lanes,
        corridor_runs,
    )

    lanes = corridor_lanes(
        run
        for rp in routes
        if rp.is_inter_section
        for run in corridor_runs(rp, apply_route_offsets(rp, offsets))
    )
    out: dict[tuple[str, str, str], float] = {}
    for i, first in enumerate(lanes):
        for second in lanes[i + 1 :]:
            if first.axis != second.axis or first.sign != second.sign:
                continue
            if first.line_id == second.line_id:
                continue
            if not any(
                max(mine.span[0], theirs.span[0]) < min(mine.span[1], theirs.span[1])
                for mine in first.runs
                for theirs in second.runs
            ):
                continue
            axis = "X" if first.axis == 0 else "Y"
            key = tuple(sorted((first.line_id, second.line_id))) + (axis,)
            separation = abs(first.coord - second.coord)
            if key not in out or separation < out[key]:
                out[key] = separation  # type: ignore[index]
    return out  # type: ignore[return-value]


def _longest_horizontal_run(
    points: list[tuple[float, float]],
) -> tuple[float, float]:
    horizontal = [
        (abs(end[0] - start[0]), start[1])
        for start, end in zip(points, points[1:], strict=False)
        if start[1] == end[1]
    ]
    assert horizontal, "route has no horizontal run"
    return max(horizontal, key=lambda item: item[0])


def _pair_identity(
    axis: str, first: tuple[str, float], second: tuple[str, float]
) -> tuple[str, str, str, float, float]:
    """One fused pair, ordered by line id so either scan order names it alike."""
    low, high = sorted((first, second))
    return (axis, low[0], high[0], round(low[1], 4), round(high[1], 4))


def _unreported_fused_pairs(path: Path) -> set[tuple[str, str, str, float, float]]:
    """The fused pairs the checker sees at *path*'s chokepoint and does not report.

    Measured as the difference between every co-travelling distinct-line pair
    drawn within one nesting step and the pairs the checker returns, so what is
    pinned is the checker's own silence rather than a restatement of the
    predicate producing it.  Read on the geometry the chokepoint is handed, which
    a fixture reaches whether or not the render then aborts on another guard.
    """
    from nf_metro.layout.routing.common import (
        apply_route_offsets,
        corridor_lanes,
        corridor_runs,
    )

    found: set[tuple[str, str, str, float, float]] = set()
    real = invariants.check_no_fused_cotravelling_lines

    def spy(graph, routes, offsets):
        step = graph_offset_step(graph)
        lanes = corridor_lanes(
            run
            for rp in routes
            if rp.is_inter_section
            for run in corridor_runs(rp, apply_route_offsets(rp, offsets))
        )
        fused = {
            _pair_identity(
                "X" if first.axis == 0 else "Y",
                (first.line_id, first.coord),
                (second.line_id, second.coord),
            )
            for i, first in enumerate(lanes)
            for second in lanes[i + 1 :]
            if first.fused_span(second, step) is not None
        }
        violations = real(graph, routes, offsets)
        found.update(
            fused
            - {
                _pair_identity(
                    item.axis,
                    (item.first_line, item.first_coord),
                    (item.second_line, item.second_coord),
                )
                for item in violations
            }
        )
        return violations

    from nf_metro.api import prepare_graph, resolve_theme
    from nf_metro.render.svg import build_observed_render_plan

    invariants.check_no_fused_cotravelling_lines = spy
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
            build_observed_render_plan(graph, resolve_theme(None, graph))
    except Exception:  # noqa: BLE001 - the check ran before any abort
        pass
    finally:
        invariants.check_no_fused_cotravelling_lines = real
    return found


@pytest.mark.parametrize(
    "path", _CORPUS, ids=[str(p.relative_to(REPO_ROOT)) for p in _CORPUS]
)
def test_the_fused_pairs_the_checker_exempts_are_the_recorded_ones(path: Path) -> None:
    """The population the plan-owned exemption hides does not grow unnoticed.

    The checker's guarantee is "no two distinct lines are drawn as one stroke",
    minus the pairs it cannot get repaired.  Left unenumerated, that subtraction
    can absorb new fused pairs while the suite stays green and the guarantee
    reads as unconditional.
    """
    rel = str(path.relative_to(REPO_ROOT))
    found = {(rel, *item) for item in _unreported_fused_pairs(path)}
    expected = {item for item in EXEMPT_FUSED_PAIRS if item[0] == rel}
    assert found == expected, (
        "the fused pairs the checker declines to report are not the ones "
        f"recorded: unrecorded {sorted(found - expected)}, recorded but now "
        f"separated {sorted(expected - found)}. A new one is two lines drawn "
        "over each other that no guard will mention; one that separated means "
        "dropping its EXEMPT_FUSED_PAIRS entry"
    )


def test_every_recorded_exempt_pair_names_a_corpus_fixture() -> None:
    """A stale entry would silently excuse a fixture that no longer exists."""
    corpus = {str(item.relative_to(REPO_ROOT)) for item in _CORPUS}
    named = {item[0] for item in EXEMPT_FUSED_PAIRS}
    assert named <= corpus, named - corpus


def test_seed_41_plan_owned_trunks_keep_the_nesting_step() -> None:
    """Global convergence settlement separates independently planned trunks."""
    path = REPO_ROOT / "tests" / "fixtures" / "hash_seed_determinism" / "seed_41.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    observed = observe_route_edges(graph, station_offsets=offsets)
    separations = _pair_separations(observed.routes, offsets)

    assert separations[("l0", "l2", "Y")] >= graph_offset_step(graph)


def test_seed_15_preserves_its_stable_render_exception() -> None:
    from nf_metro.api import prepare_graph, resolve_theme
    from nf_metro.render.svg import build_observed_render_plan

    path = FROZEN_FUZZ / "seed_15.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))

    with pytest.raises(invariants.CurveInvariantError, match="bundle order"):
        build_observed_render_plan(graph, resolve_theme(None, graph))


@pytest.mark.parametrize(
    "path",
    tuple(sorted(FROZEN_FUZZ.glob("seed_*.mmd"))),
    ids=lambda path: path.stem,
)
def test_stable_generated_inputs_have_no_plan_owned_fused_pair(path: Path) -> None:
    """The frozen fuzz seeds bound the live invariant's generated-input rate."""
    graph, routes, offsets = _route(path)
    violations = check_no_fused_cotravelling_lines(graph, routes, offsets)
    plan_owned = [item for item in violations if item.plan_ids]
    assert not plan_owned, (
        "generated input contains a plan-owned fused pair:\n"
        + "\n".join(item.message() for item in plan_owned)
    )


def test_novel_plan_owned_corridor_is_settled_before_freeze(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gallery reproducer needs pre-freeze planning to remain renderable."""
    path = EXAMPLE_TOPOLOGIES / "plan_owned_distinct_lane_separation.mmd"
    monkeypatch.setattr(
        convergences,
        "_separate_distinct_cotravelling_trunks",
        lambda plans, graph, member_runs: plans,
    )
    graph, routes, offsets = _route(path)
    violations = check_no_fused_cotravelling_lines(graph, routes, offsets)
    assert any(
        {item.first_line, item.second_line} == {"primary", "secondary"}
        and item.separation == pytest.approx(0.0)
        for item in violations
    )

    monkeypatch.undo()
    graph, routes, offsets = _route(path)
    assert not check_no_fused_cotravelling_lines(graph, routes, offsets)
    assert _pair_separations(routes, offsets)[("primary", "secondary", "Y")] == (
        pytest.approx(graph_offset_step(graph))
    )


@pytest.mark.parametrize(
    ("path", "primary_source", "secondary_sources"),
    (
        (
            EXAMPLE_TOPOLOGIES / "plan_owned_distinct_lane_separation.mmd",
            "__junction_8",
            ("secondary_near__exit_left_1", "secondary_far__exit_left_2"),
        ),
        (
            REGRESSIONS / "plan_owned_distinct_lane_separation_reordered.mmd",
            "__junction_9",
            ("secondary_near__exit_left_0", "secondary_far__exit_left_1"),
        ),
    ),
    ids=("authored-order", "reordered-edges"),
)
def test_plan_owned_distinct_lanes_minimize_crossings_independent_of_edge_order(
    path: Path,
    primary_source: str,
    secondary_sources: tuple[str, str],
) -> None:
    graph, routes, offsets = _route(path)
    primary_routes = tuple(
        route
        for route in routes
        if route.line_id == "primary"
        and route.edge.source == primary_source
        and route.edge.target in {"__merge_2", "__merge_3"}
    )
    secondary_routes = tuple(
        route
        for route in routes
        if route.line_id == "secondary" and route.edge.source in secondary_sources
    )
    assert len(primary_routes) == len(secondary_routes) == 2

    for primary in primary_routes:
        primary_points = apply_route_offsets(primary, offsets)
        primary_trunk_y = _longest_horizontal_run(primary_points)[1]
        for secondary in secondary_routes:
            secondary_points = apply_route_offsets(secondary, offsets)
            secondary_trunk_y = _longest_horizontal_run(secondary_points)[1]
            assert secondary_trunk_y < primary_trunk_y
            assert not tuple(_routes_crossings(primary_points, secondary_points))


def test_analytic_candidate_segments_match_the_plan_mover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Analytic scoring prices exactly the geometry the selected move emits."""
    from nf_metro.api import prepare_graph, resolve_theme
    from nf_metro.render.svg import build_observed_render_plan

    real = convergences._crossing_minimal_lane
    checked = 0
    mismatches: list[tuple[Path, float]] = []
    current_path: Path | None = None

    def checking_lane(plan, run, neighbours, clearance):
        nonlocal checked
        candidates = convergences._clear_lane_candidates(
            tuple(neighbour.coordinate for neighbour in neighbours), clearance
        )
        for candidate in candidates:
            checked += 1
            analytic = convergences._plan_segments(plan, candidate)
            moved = convergences._plan_segments(
                convergences._move_trunk_axis(plan, candidate)
            )
            if analytic != moved:
                assert current_path is not None
                mismatches.append((current_path, candidate))
        return real(plan, run, neighbours, clearance)

    monkeypatch.setattr(convergences, "_crossing_minimal_lane", checking_lane)
    for path in _CORPUS:
        current_path = path
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
                build_observed_render_plan(graph, resolve_theme(None, graph))
        except Exception:  # noqa: BLE001 - invalid fixtures may abort after scoring
            pass

    assert checked > 0
    assert not mismatches


@pytest.mark.parametrize(
    ("first_points", "second_points", "axis"),
    (
        (
            [(0.0, -20.0), (0.0, 0.0), (100.0, 0.0), (100.0, 20.0)],
            [(0.0, -16.0), (0.0, 2.0), (100.0, 2.0), (100.0, 24.0)],
            "Y",
        ),
        (
            [(100.0, -20.0), (100.0, 0.0), (0.0, 0.0), (0.0, 20.0)],
            [(100.0, -16.0), (100.0, 2.0), (0.0, 2.0), (0.0, 24.0)],
            "Y",
        ),
        (
            [(-20.0, 0.0), (0.0, 0.0), (0.0, 100.0), (20.0, 100.0)],
            [(-16.0, 0.0), (2.0, 0.0), (2.0, 100.0), (24.0, 100.0)],
            "X",
        ),
        (
            [(-20.0, 100.0), (0.0, 100.0), (0.0, 0.0), (20.0, 0.0)],
            [(-16.0, 100.0), (2.0, 100.0), (2.0, 0.0), (24.0, 0.0)],
            "X",
        ),
    ),
    ids=("right", "left", "down", "up"),
)
def test_plan_owned_fused_lanes_are_attributed_in_every_direction(
    first_points: list[tuple[float, float]],
    second_points: list[tuple[float, float]],
    axis: str,
) -> None:
    """The live check reports immutable fused lanes on either routing axis."""

    def planned_route(
        line_id: str,
        points: list[tuple[float, float]],
        plan_id: str,
    ) -> RoutedPath:
        return RoutedPath(
            Edge(f"{line_id}_source", f"{line_id}_target", line_id),
            line_id,
            points,
            is_inter_section=True,
            offset_regime=OffsetRegime.BAKED,
            route_system_id="route-system:test",
            convergence_plan_id=plan_id,
            convergence_owned_segment_ranks=(1,),
        )

    violations = check_no_fused_cotravelling_lines(
        MetroGraph(),
        [
            planned_route("first", first_points, "convergence-plan:first"),
            planned_route("second", second_points, "convergence-plan:second"),
        ],
        {},
    )

    assert len(violations) == 1
    assert violations[0].axis == axis
    message = violations[0].message()
    assert "route-system:test" in message
    assert "convergence-plan:first" in message
    assert "convergence-plan:second" in message


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_no_fused_cotravelling_lines_in_gallery(path: Path) -> None:
    """No shipped topology or example paints two distinct lines as one stroke."""
    graph, routes, offsets = _route(path)
    violations = check_no_fused_cotravelling_lines(graph, routes, offsets)
    assert not violations, "\n".join(v.message() for v in violations)


@pytest.mark.parametrize("path", REPORTED, ids=lambda p: p.stem)
def test_reported_corridors_keep_the_nesting_step(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The corridors a reservation band pulled together keep the full step."""
    graph, routes, offsets, _violations = _settled(path, monkeypatch)
    separations = _pair_separations(routes, offsets)
    for pair in REPORTED[path]:
        assert pair in separations, f"{pair} no longer shares a corridor"
        assert separations[pair] >= graph_offset_step(graph)


def test_inter_row_corridor_seats_its_shared_destination_pair_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A three-track inter-row channel keeps its two co-arriving lines adjacent.

    ``inter_row_corridor_overflow``'s row-0/row-1 channel carries three trunks:
    ``la`` and ``lc`` cross it together to reach one section, and ``lb`` crosses
    it to another.  Drawn with ``lb`` between the pair, ``lb`` has to cut across
    both of them where it peels off; the channel has room to seat it outside the
    pair instead, which costs no crossing at all.

    Reaching that arrangement needs the reservation covering the pair to state
    the width of the whole three-track stack it is drawn inside.  Stated at the
    pair's own two tracks, the channel publishes room for half the stack that
    stands in it, no reordering of the three fits inside what is claimed, and
    the interloper stays between them.

    Rendered twice: once under ``strict``, where the settlement, collinearity
    and fused-pair guards all have to pass on the arrangement, and once through
    the measuring chokepoint, which reports the drawn lane separations the
    ordering is read from.
    """
    from nf_metro.api import prepare_graph, resolve_theme
    from nf_metro.render.svg import build_observed_render_plan

    path = CURVE_REPROS / "inter_row_corridor_overflow.mmd"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        graph.strict = True
        build_observed_render_plan(graph, resolve_theme(None, graph))

    graph, routes, offsets, _violations = _settled(path, monkeypatch)
    step = graph_offset_step(graph)
    separations = _pair_separations(routes, offsets)
    assert separations[("la", "lc", "Y")] == pytest.approx(step)
    assert separations[("la", "lb", "Y")] == pytest.approx(step)
    assert separations[("lb", "lc", "Y")] == pytest.approx(2 * step)


def test_cross_row_corridor_nests_lines_arriving_from_different_grid_rows() -> None:
    """Distinct lines sharing an inter-row corridor keep the nesting step even
    when they enter it from different grid rows.

    On the riboseq map three corridor routes realise one reserved band from
    separate reservation groups: ``rnaseq`` crosses from the alignment row to
    ``te``, ``annotation`` from the transcript-discovery row to ``orf_calling``,
    and ``riboseq`` runs its own trunk between them.  No group's own lane
    allocation sees the others, so they settle onto one lane and paint over each
    other -- ``rnaseq`` and ``annotation`` at the same coordinate, ``riboseq``
    within one step of both.
    """
    path = CURVE_REPROS / "riboseq_inter_row_corridor.mmd"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph, routes, offsets = _route(path)
    violations = check_no_fused_cotravelling_lines(graph, routes, offsets)
    assert not violations, "\n".join(v.message() for v in violations)


def _junction_traverse_band(
    routes, offsets, source: str, target: str
) -> tuple[float, tuple[float, float]] | None:
    """The Y and X-span of the widest horizontal leg *source*->*target* draws."""
    for rp in routes:
        if rp.edge.source != source or rp.edge.target != target:
            continue
        pts = list(apply_route_offsets(rp, offsets))
        best: tuple[float, tuple[float, float]] | None = None
        width = -1.0
        for start, end in zip(pts, pts[1:], strict=False):
            if (
                abs(start[1] - end[1]) <= COORD_TOLERANCE
                and abs(start[0] - end[0]) > COORD_TOLERANCE
            ):
                span = (min(start[0], end[0]), max(start[0], end[0]))
                if span[1] - span[0] > width:
                    width = span[1] - span[0]
                    best = (start[1], span)
        return best
    return None


def test_same_line_riboseq_junction_traverses_draw_as_one_stroke() -> None:
    """Two same-line branches leaving one junction share their corridor as one stroke.

    On the riboseq map ``riboseq`` leaves ``__junction_13`` on two inter-section
    edges -- one convergence-plan-owned trunk to ``__merge_2`` and one fan-owned
    branch to ``psite_id``'s left entry.  Their vertical arms already drop the
    same column, but the horizontal corridor they turn onto settles on two bands
    a couple of pixels apart, so the shared run reads as one line smeared across
    two strokes.  A single line must draw as one stroke over the stretch its
    branches co-travel, splitting only where the fan-owned branch peels off.
    """
    path = CURVE_REPROS / "riboseq_inter_row_corridor.mmd"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph, routes, offsets = _route(path)
    merge = _junction_traverse_band(routes, offsets, "__junction_13", "__merge_2")
    psite = _junction_traverse_band(
        routes, offsets, "__junction_13", "psite_id__entry_left_9"
    )
    assert merge is not None, "merge trunk leg not found"
    assert psite is not None, "psite branch leg not found"
    merge_y, merge_span = merge
    psite_y, psite_span = psite
    overlap = min(merge_span[1], psite_span[1]) - max(merge_span[0], psite_span[0])
    assert overlap > 100.0, f"branches barely share a corridor: {overlap:.1f}px"
    assert abs(merge_y - psite_y) <= COORD_TOLERANCE, (
        f"same-line riboseq traverses draw {abs(merge_y - psite_y):.1f}px apart over "
        f"a {overlap:.1f}px shared corridor -- one line as two strokes"
    )


def test_riboseq_corridor_bundle_holds_a_uniform_nesting_pitch() -> None:
    """The three distinct lines sharing the inter-row corridor nest at one pitch.

    ``annotation``, ``riboseq`` and ``rnaseq`` co-travel the corridor back toward
    the ``te``/``reporting`` columns.  The separation cascade clears their
    fusions but can strand the outermost movable line a full step wide when the
    obstacle it stepped clear of then relocates past the pinned trunk, leaving a
    4px/6px stack that reads as a bundle with one widened gap.  A bundle must
    read at one pitch, so both adjacent gaps hold exactly one ``OFFSET_STEP``.
    """
    path = CURVE_REPROS / "riboseq_inter_row_corridor.mmd"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph, routes, offsets = _route(path)
    step = graph_offset_step(graph)
    separations = _pair_separations(routes, offsets)
    for pair in (("annotation", "riboseq", "Y"), ("riboseq", "rnaseq", "Y")):
        assert pair in separations, f"{pair} no longer shares the corridor"
        assert separations[pair] == pytest.approx(step), (
            f"{pair[0]}/{pair[1]} nest {separations[pair]:.1f}px apart, not one "
            f"{step:.1f}px step -- the corridor bundle carries a widened gap"
        )


def _peeloff_riser_xs(
    routes: list[RoutedPath], offsets, port_id: str
) -> dict[str, float]:
    """Drawn riser X per line on the peel-off tails arriving at *port_id*."""
    from dataclasses import replace

    from nf_metro.layout.routing.common import port_peeloff_tail

    out: dict[str, float] = {}
    for rp in routes:
        if rp.edge.target != port_id:
            continue
        drawn = replace(rp, points=list(apply_route_offsets(rp, offsets)))
        tail = port_peeloff_tail(drawn)
        if tail is not None:
            out.setdefault(rp.line_id, tail.peel_x)
    return out


def test_inter_row_corridor_descends_into_its_port_on_the_nesting_pitch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two lines peeling off one trunk descend a bundle-pitch band, not a wider one.

    ``inter_row_corridor_overflow``'s ``la``/``lc`` pair leaves a three-line
    junction, so the fan that emits them reserves a middle lane for the third
    line.  That line runs straight on and never enters the descent into
    ``calling``'s left entry port, so the pair owns the descent alone and must
    close up to one ``OFFSET_STEP``; held at the fan's width they descend
    ``2 * OFFSET_STEP`` apart with a visible gap between two strokes that
    co-travel the whole leg.
    """
    path = CURVE_REPROS / "inter_row_corridor_overflow.mmd"
    graph, routes, offsets, _violations = _settled(path, monkeypatch)
    risers = _peeloff_riser_xs(routes, offsets, "calling__entry_left_8")

    assert set(risers) == {"la", "lc"}, risers
    assert abs(risers["la"] - risers["lc"]) == pytest.approx(graph_offset_step(graph))


def _overwide_peeloff_bands(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, list[tuple[str, float, int]]]:
    """Peel-off bundles measured at *path*, and those spanning over one width."""
    from nf_metro.layout.routing.common import iter_port_peeloff_bundles

    graph, routes, offsets, _violations = _settled(path, monkeypatch)
    step = graph_offset_step(graph)
    measured = 0
    wide: list[tuple[str, float, int]] = []
    for bundle in iter_port_peeloff_bundles(routes, graph, step):
        risers = _peeloff_riser_xs(routes, offsets, bundle.port_id)
        drawn = [risers[line_id] for line_id in bundle.per_line if line_id in risers]
        if len(drawn) < 2:
            continue
        measured += 1
        span = max(drawn) - min(drawn)
        if span > (len(drawn) - 1) * step + COORD_TOLERANCE:
            wide.append((bundle.port_id, span, len(drawn)))
    return measured, wide


def test_peeloff_risers_descend_on_the_nesting_pitch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No corpus fixture descends a peel-off bundle wider than its own line count.

    A peel-off bundle's risers share one vertical corridor into a single entry
    port, so ``n`` of them occupy ``(n - 1) * OFFSET_STEP``.  A wider band is a
    lane held for a line that is not in the corridor, drawn as a gap between
    strokes that arrive together.

    Inputs the engine rejects outright are skipped: they never reach a settled
    geometry to measure, and their rejection is pinned elsewhere.
    """
    wide: dict[str, list[tuple[str, float, int]]] = {}
    measured = 0
    for path in _CORPUS:
        try:
            seen, found = _overwide_peeloff_bands(path, monkeypatch)
        except Exception:  # noqa: BLE001 - deliberately defective fixtures abort
            continue
        measured += seen
        if found:
            wide[str(path.relative_to(REPO_ROOT))] = found
    assert measured >= 20, (
        f"only {measured} peel-off bundles measured across the corpus: the sweep "
        "has stopped seeing the geometry it exists to check, so its silence is "
        "not evidence. The floor is deliberately far below the count the corpus "
        "yields, to catch that collapse without pinning an exact tally"
    )
    assert not wide, (
        "peel-off risers descend wider than their own bundle: "
        + "; ".join(
            f"{rel} {port_id} spans {span:.1f}px on {count} lines"
            for rel, found in sorted(wide.items())
            for port_id, span, count in found
        )
    )


@pytest.mark.parametrize("path", SEATED_WITHOUT_THE_PASS, ids=lambda p: p.stem)
def test_seated_pairs_hold_the_pitch_without_the_separation_stages(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """These pairs hold the nesting pitch with both separation stages disabled."""
    _disable_separation_stages(monkeypatch)
    graph, routes, offsets, _violations = _settled(path, monkeypatch)
    step = graph_offset_step(graph)
    separations = _pair_separations(routes, offsets)
    for pair in SEATED_WITHOUT_THE_PASS[path]:
        assert pair in separations, f"{pair} no longer shares a corridor"
        assert separations[pair] >= step
        assert separations[pair] % step == pytest.approx(0.0, abs=1e-6)


@pytest.mark.parametrize("path", FUSED_WITHOUT_THE_PASS, ids=lambda p: p.stem)
def test_tracks_fuse_without_the_separation_stages(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disabling both separation stages reproduces the fused pair."""
    _disable_separation_stages(monkeypatch)
    graph, routes, offsets, _violations = _settled(path, monkeypatch)
    step = graph_offset_step(graph)
    separations = _pair_separations(routes, offsets)
    for pair in FUSED_WITHOUT_THE_PASS[path]:
        assert pair in separations, f"{pair} no longer shares a corridor"
        assert separations[pair] < step


@pytest.mark.parametrize("path", FUSED_WITHOUT_THE_PASS, ids=lambda p: p.stem)
def test_separated_pairs_land_on_the_nesting_pitch(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each settled pair lands on the nesting pitch, not an accidental gap."""
    _disable_separation_stages(monkeypatch)
    graph, routes, offsets, _violations = _settled(path, monkeypatch)
    step = graph_offset_step(graph)
    separations = _pair_separations(routes, offsets)
    fused = FUSED_WITHOUT_THE_PASS[path]
    for pair in fused:
        assert pair in separations, f"{pair} no longer shares a corridor"
        assert separations[pair] < step

    monkeypatch.undo()
    graph, routes, offsets, _violations = _settled(path, monkeypatch)
    separations = _pair_separations(routes, offsets)
    step = graph_offset_step(graph)
    for pair in fused:
        assert pair in separations, f"{pair} no longer shares a corridor"
        assert separations[pair] >= step
        assert separations[pair] % step == pytest.approx(0.0, abs=1e-6)
