"""The corridor-cohort compile reads the geometry the post-emission passes draw.

After emission, ``_materialize_trunk_slots`` restacks every trunk band,
``_reconcile_port_peeloff_risers`` re-seats every peel-off riser the freeze
leaves unowned onto the slot its settled trunk depth earns, the fan-out
traverse passes fuse and nest the corridor runs, and
``_separate_fused_cotravelling_runs``,
``_align_merge_fed_confluence_to_band`` and
``_hold_runs_in_corridor_clearance`` settle them.  The compile reads a
snapshot in which those rules have already run, on copies, so the coordinates
it accepts are the ones drawn while no live route moves before a plan is
published.
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import cohort_shadow_oracle
import pytest
from conftest import curve_abort_softened, is_curve_abort_fixture

from nf_metro.api import render_string
from nf_metro.layout.routing import corridor_cohort_integration as cci
from nf_metro.layout.routing import member_geometry
from nf_metro.layout.routing.common import RoutedPath, port_peeloff_tail
from nf_metro.render import svg

ROOT = Path(__file__).resolve().parents[1]

EdgeKey = tuple[str, str, str]

# Two lines leave sec2 through one row-gap corridor whose clearance band sits
# below where the fused-run separation leaves them: only the post-emission
# hold seats them, a step deeper.
CLEARANCE_HELD_CORRIDOR = """\
%%metro title: Fuzz 8400125
%%metro line: l0 | Markdup_Cluster | #e6194b
%%metro line: l1 | Fastqc | #3cb44b

graph LR
    subgraph sec1 [Count 1]
        %%metro direction: TB
        s1n0[filter]
    end
    subgraph sec2 [Classify 2]
        s2n0[trim_call]
        s2n1[filter]
        s2n2[POLISH]
        s2n3[report_trim]
        s2n4[bin_cluster_sort]
        s2n0 -->|l0,l1| s2n1
        s2n1 -->|l0| s2n3
        s2n3 -->|l0,l1| s2n4
        s2n1 -->|l1| s2n2
        s2n2 -->|l1| s2n3
    end
    subgraph sec3 [Summarise 3]
        %%metro direction: BT
        s3n0[gather]
        s3n1[summarise]
        s3n2[polish_polish]
        s3n0 -->|l0| s3n2
        s3n1 -->|l1| s3n2
    end
    subgraph sec4 [Assemble 4]
        %%metro direction: TB
        s4n0[bam]
        s4n1[cluster]
        s4n0 -->|l0| s4n1
    end
    s2n4 -->|l0| s3n0
    s3n2 -->|l0| s4n0
    s1n0 -->|l1| s2n0
    s2n0 -->|l1| s3n1
    s1n0 -->|l1| s3n1
"""

# A top-entry bundle into sec2 whose declared trunk slot restacks it before
# the corridor-clearance hold shifts it back: the two moves cancel in the
# drawn geometry, so the compile must not read the hold's shift alone.
TRUNK_SLOT_RESTACKED_BUNDLE = """\
%%metro title: Fuzz 8402588
%%metro style: seqera
%%metro line: l0 | Count | #e6194b
%%metro line: l1 | Polish | #3cb44b
%%metro line: l2 | Bam | #4363d8
%%metro line: l3 | Call_Classify | #f58231
%%metro line: l4 | Markdup | #911eb4

graph LR
    subgraph sec0 [Bin 0]
        s0n0[pca]
        s0n1[call]
        s0n0 -->|l0,l1,l2| s0n1
    end
    subgraph sec1 [Export 1]
        s1n0[vcf]
        s1n1[polish_qc]
        s1n2[stats_classify]
        s1n0 -->|l0,l2,l4| s1n1
        s1n1 -->|l0,l2,l4| s1n2
    end
    subgraph sec2 [Qc_Summarise_Summarise 2]
        %%metro entry: top | l0, l1, l2, l3, l4
        s2n0[cluster]
        s2n1[CLASSIFY]
        s2n2[cluster]
        s2n3[classify_vcf]
        s2n0 -->|l0,l1,l2| s2n1
        s2n1 -->|l0| s2n3
        s2n1 -->|l1,l4| s2n2
        s2n2 -->|l1,l4| s2n3
    end
    s0n1 -->|l0,l2| s1n0
    s1n2 -->|l0,l1,l2| s2n0
    s0n1 -->|l1| s1n2
    s0n1 -->|l3| s2n3
    s1n2 -->|l4| s2n1
"""


def _map_path(source: str, tmp_path: Path) -> Path:
    """A repository-relative map path, or *source* written out as a map."""
    if source.endswith(".mmd"):
        return ROOT / source
    path = tmp_path / "map.mmd"
    path.write_text(source)
    return path


def _edge_key(route: RoutedPath) -> EdgeKey:
    return route.edge.source, route.edge.target, route.line_id


def _render(path: Path) -> None:
    with warnings.catch_warnings(), curve_abort_softened(is_curve_abort_fixture(path)):
        warnings.simplefilter("ignore")
        render_string(path.read_text(), source_dir=str(path.parent))


@pytest.mark.parametrize(
    ("relative_path", "expected"),
    [
        (
            "examples/topologies/fan_over_top_reach_order.mmd",
            {
                ("__junction_4", "c__entry_right_3", "l1"): 690.0,
                ("b__exit_right_1", "c__entry_right_3", "l2"): 686.0,
            },
        ),
        (
            "tests/fixtures/curve_invariant_repros/fan_over_top_flow_feeder.mmd",
            {
                ("__junction_6", "sec3__entry_right_5", "l1"): 1390.0,
                ("sec2__exit_right_2", "sec3__entry_right_5", "l2"): 1386.0,
            },
        ),
        (
            "tests/fixtures/curve_invariant_repros/inter_row_corridor_overflow.mmd",
            {
                ("__junction_12", "calling__entry_left_8", "la"): 34.0,
                ("__junction_12", "calling__entry_left_8", "lc"): 30.0,
            },
        ),
        (
            "tests/fixtures/regressions/riboseq_row_trunk_subrun.mmd",
            {
                ("__junction_12", "orf_calling__entry_left_8", "riboseq"): 34.0,
                ("__junction_12", "orf_calling__entry_left_8", "tiseq"): 30.0,
            },
        ),
    ],
)
def test_compiled_peeloff_riser_agrees_with_the_drawn_riser(
    monkeypatch: pytest.MonkeyPatch, relative_path: str, expected: dict[EdgeKey, float]
) -> None:
    """The cohort-final compile accepts each peel-off riser where it is drawn."""
    plans: list[cci.CorridorCohortPlan] = []
    drawn: dict[EdgeKey, float] = {}
    compile_plan = member_geometry.compile_corridor_cohort_plan
    drawable = svg._assert_settled_geometry_is_drawable

    def capture_plan(*args: Any, **kwargs: Any) -> cci.CorridorCohortPlan:
        plan = compile_plan(*args, **kwargs)
        plans.append(plan)
        return plan

    def capture_drawn(graph: Any, settled: Any) -> None:
        drawable(graph, settled)
        drawn.update(
            {
                _edge_key(route): tail.peel_x
                for route in settled.routes
                if (tail := port_peeloff_tail(route)) is not None
            }
        )

    monkeypatch.setattr(member_geometry, "compile_corridor_cohort_plan", capture_plan)
    monkeypatch.setattr(svg, "_assert_settled_geometry_is_drawable", capture_drawn)
    _render(ROOT / relative_path)

    assert plans
    risers = {
        allocation.edge_key: allocation.coordinate
        for allocation in plans[-1].allocations
        if allocation.axis == 0
        and allocation.edge_key in expected
        and allocation.segment_rank == 3
    }
    assert risers == expected
    assert {key: drawn[key] for key in expected} == expected


@pytest.mark.parametrize(
    ("relative_path", "expected"),
    [
        (
            "examples/topologies/inter_row_exempt_band_order.mmd",
            {
                ("lvl__exit_right_0", "report__entry_left_2", "riboseq"): 207.0,
                ("src__exit_right_1", "model__entry_left_3", "rnaseq"): 203.0,
            },
        ),
        (
            "examples/topologies/reversed_section_junction_reseat.mmd",
            {
                ("__junction_9", "report_a__entry_bottom_6", "a"): 200.0,
                ("__junction_9", "report_b__entry_top_7", "b"): 196.0,
            },
        ),
    ],
    ids=["inter_row_exempt", "reversed_section"],
)
def test_compiled_trunk_band_agrees_with_the_drawn_band(
    monkeypatch: pytest.MonkeyPatch, relative_path: str, expected: dict[EdgeKey, float]
) -> None:
    """Two trunks share one row-gap band that the trunk-slot pass restacks
    after emission; the cohort-final compile accepts each trunk on the track
    it is drawn on."""
    plans: list[cci.CorridorCohortPlan] = []
    drawn: dict[EdgeKey, float] = {}
    compile_plan = member_geometry.compile_corridor_cohort_plan
    drawable = svg._assert_settled_geometry_is_drawable

    def capture_plan(*args: Any, **kwargs: Any) -> cci.CorridorCohortPlan:
        plan = compile_plan(*args, **kwargs)
        plans.append(plan)
        return plan

    def capture_drawn(graph: Any, settled: Any) -> None:
        drawable(graph, settled)
        drawn.update(
            {
                _edge_key(route): route.points[2][1]
                for route in settled.routes
                if _edge_key(route) in expected
            }
        )

    monkeypatch.setattr(member_geometry, "compile_corridor_cohort_plan", capture_plan)
    monkeypatch.setattr(svg, "_assert_settled_geometry_is_drawable", capture_drawn)
    _render(ROOT / relative_path)

    assert plans
    trunks = {
        allocation.edge_key: allocation.coordinate
        for allocation in plans[-1].allocations
        if allocation.axis == 1
        and allocation.edge_key in expected
        and allocation.segment_rank == 2
    }
    assert trunks == expected
    assert {key: drawn[key] for key in expected} == expected


def _route_state(route: RoutedPath) -> tuple[Any, ...]:
    return (
        tuple(map(tuple, route.points)),
        None if route.curve_radii is None else tuple(route.curve_radii),
        tuple(sorted(route.concentric_corner_offsets_by_segment.items())),
        tuple(sorted(route.concentric_corner_bases_by_segment.items())),
        route.source_turnout,
        route.route_system_owned_segment_ranks,
        tuple(route.gap_slots),
        route.trunk_slot,
    )


@pytest.mark.parametrize(
    ("source", "expect_moved"),
    [
        ("examples/topologies/fan_over_top_reach_order.mmd", False),
        (
            "tests/fixtures/curve_invariant_repros/inter_row_corridor_overflow.mmd",
            False,
        ),
        ("tests/fixtures/regressions/riboseq_row_trunk_subrun.mmd", False),
        ("examples/topologies/entry_cohort_interleaved_fixed_lane.mmd", False),
        (CLEARANCE_HELD_CORRIDOR, True),
        ("examples/topologies/inter_row_exempt_band_order.mmd", True),
        ("examples/riboseq_metro.mmd", True),
    ],
    ids=[
        "fan_over_top",
        "corridor_overflow",
        "trunk_subrun",
        "fixed_lane",
        "held",
        "restacked_band",
        "riboseq",
    ],
)
def test_peeloff_snapshot_leaves_the_live_routes_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str, expect_moved: bool
) -> None:
    """Seating the snapshot writes no live route, and every patch the compile
    returns names a live route.  The interleaved fixed-lane entry cohort
    compiles radius-only patches for re-sized mates, so it holds a mate's patch
    to its live route too.  On the held corridor, the restacked band and
    riboseq the snapshot does move compiled copies off their live routes, so
    the copies are not the routes.  The segment ownership the snapshot lends
    its copies while it replays the passes is gone by the compile, which reads
    each copy's ownership as its live route's."""
    calls: list[tuple[bool, bool]] = []
    moved: list[int] = []
    owned_agrees: list[bool] = []
    compile_cohorts = member_geometry._compile_corridor_cohorts
    compile_plan = member_geometry.compile_corridor_cohort_plan

    def count_moved(
        ledger: Any, targets: Sequence[Any], **kwargs: Any
    ) -> cci.CorridorCohortPlan:
        live_route = kwargs["live_route"]
        moved.append(
            sum(
                target.route.points != live_route(target.route).points
                for target in targets
            )
        )
        owned_agrees.append(
            all(
                target.route.route_system_owned_segment_ranks
                == live_route(target.route).route_system_owned_segment_ranks
                for target in targets
            )
        )
        return compile_plan(ledger, targets, **kwargs)

    def check(
        ledger: Any,
        candidates: Sequence[Any],
        context_candidates: Sequence[Any],
        scaffold: Any,
        ctx: Any,
        **kwargs: Any,
    ) -> Any:
        live = {
            id(route): route
            for route in (
                *(candidate.route for candidate in candidates),
                *(candidate.route for candidate in context_candidates),
                *(target.route for target in kwargs.get("additional_targets", ())),
                *kwargs.get("sizing_population", ()),
            )
        }
        before = {route_id: _route_state(route) for route_id, route in live.items()}
        result = compile_cohorts(
            ledger, candidates, context_candidates, scaffold, ctx, **kwargs
        )
        plan = result[0]
        patches = () if plan is None else plan.route_patches
        calls.append(
            (
                all(_route_state(route) == before[i] for i, route in live.items()),
                all(live.get(id(patch.route)) is patch.route for patch in patches),
            )
        )
        return result

    monkeypatch.setattr(member_geometry, "_compile_corridor_cohorts", check)
    monkeypatch.setattr(member_geometry, "compile_corridor_cohort_plan", count_moved)
    _render(_map_path(source, tmp_path))

    assert calls
    assert all(untouched for untouched, _live in calls)
    assert all(live for _untouched, live in calls)
    assert owned_agrees
    assert all(owned_agrees)
    if expect_moved:
        assert max(moved) > 0


def test_compile_accepts_the_runs_the_clearance_hold_draws(tmp_path: Path) -> None:
    """The shadow render draws the corridor the compile accepted, run for run."""
    path = _map_path(CLEARANCE_HELD_CORRIDOR, tmp_path)

    assert cohort_shadow_oracle.shadow_failure(path) is None


def test_compile_holds_a_trunk_slot_restacked_bundle_where_it_is_drawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A bundle the trunk-slot pass would restack keeps its place on the snapshot.

    The drawn geometry is the trunk-slot restack followed by the hold, which
    cancel, so the plan accepts l0 to l2 where they are drawn.
    """
    bundle = {
        ("sec1__exit_right_1", "sec2__entry_top_3", line) for line in ("l0", "l1", "l2")
    }
    mismatches: list[str] = []
    owned: set[EdgeKey] = set()
    planned_drawn = cohort_shadow_oracle.planned_drawn_mismatches

    def record(routes: Sequence[RoutedPath], route_plan: Any) -> list[str]:
        accepted = route_plan.accepted_corridor_cohort_plan
        owned.update(
            _edge_key(route)
            for route in routes
            if accepted is not None
            and any(frame.segment[1] == _edge_key(route) for frame in accepted.owners)
        )
        found = planned_drawn(routes, route_plan)
        mismatches.extend(found)
        return found

    monkeypatch.setattr(cohort_shadow_oracle, "planned_drawn_mismatches", record)
    cohort_shadow_oracle.shadow_failure(
        _map_path(TRUNK_SLOT_RESTACKED_BUNDLE, tmp_path)
    )

    assert bundle <= owned
    assert not [
        mismatch for mismatch in mismatches for key in bundle if str(key) in mismatch
    ]


@pytest.mark.parametrize(
    "relative_path",
    [
        "examples/topologies/merge_around_below_leftmost.mmd",
        "tests/fixtures/regressions/perp_entry_column_merge_feeder.mmd",
    ],
    ids=["merge_around", "perp_entry_column"],
)
def test_recipe_named_copies_reach_the_compile_where_they_stand(
    monkeypatch: pytest.MonkeyPatch, relative_path: str
) -> None:
    """A scalar request's grant decides where every route its control recipe
    names is drawn, so no pass the snapshot replays moves such a copy and the
    compile reads each one on its live route's points.  On these maps an
    unanchored restack moves a named copy that the traverse passes then move
    back, so each replayed pass is checked, not only the compile's input."""
    named_routes: set[int] = set()
    source_of: dict[int, RoutedPath] = {}
    pass_moves: list[tuple[str, EdgeKey]] = []
    compiled: list[tuple[EdgeKey, bool]] = []
    snapshot = member_geometry._post_emission_seated_snapshot
    sizing_copy = member_geometry._corner_sizing_copy
    compile_plan = member_geometry.compile_corridor_cohort_plan

    def recipe_routes(
        targets: Sequence[Any], scalar_requests: Sequence[Any]
    ) -> list[Any]:
        by_member = {(target.member_id, target.edge_key): target for target in targets}
        return [
            target
            for request in scalar_requests
            if (recipe := request.control_recipe) is not None
            for point in (*recipe.controlled_points, *recipe.fixed_points)
            if (target := by_member.get((point.member_id, point.edge_key))) is not None
        ]

    def copy_route(route: RoutedPath) -> RoutedPath:
        copied = sizing_copy(route)
        source_of[id(copied)] = source_of.get(id(route), route)
        return copied

    def seat(targets: Sequence[Any], *args: Any, **kwargs: Any) -> Any:
        named_routes.update(
            id(target.route)
            for target in recipe_routes(targets, kwargs["scalar_requests"])
        )
        try:
            return snapshot(targets, *args, **kwargs)
        finally:
            named_routes.clear()
            source_of.clear()

    def watched(name: str) -> None:
        replayed = getattr(member_geometry, name)

        def run(routes: list[RoutedPath], ctx: Any, **kwargs: Any) -> None:
            before = [
                (route, list(route.points))
                for route in routes
                if id(source_of.get(id(route), route)) in named_routes
            ]
            replayed(routes, ctx, **kwargs)
            pass_moves.extend(
                (name, _edge_key(route))
                for route, points in before
                if route.points != points
            )

        monkeypatch.setattr(member_geometry, name, run)

    def check(
        ledger: Any, targets: Sequence[Any], **kwargs: Any
    ) -> cci.CorridorCohortPlan:
        live_route = kwargs["live_route"]
        compiled.extend(
            (
                target.edge_key,
                target.route.points == live_route(target.route).points,
            )
            for target in recipe_routes(targets, kwargs.get("scalar_requests", ()))
        )
        return compile_plan(ledger, targets, **kwargs)

    for name in (
        "_materialize_trunk_slots",
        "_coincide_same_line_fanout_traverses",
        "_bundle_divergent_distinct_traverses",
    ):
        watched(name)
    monkeypatch.setattr(member_geometry, "_corner_sizing_copy", copy_route)
    monkeypatch.setattr(member_geometry, "_post_emission_seated_snapshot", seat)
    monkeypatch.setattr(member_geometry, "compile_corridor_cohort_plan", check)
    _render(ROOT / relative_path)

    assert compiled
    assert pass_moves == []
    assert [key for key, unmoved in compiled if not unmoved] == []


def test_free_confluence_descent_reads_the_freeze_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``l2`` reaches ``sec2``'s left entry on a descent at x 472 that the
    freeze will own, so the merge-fed confluence alignment the snapshot
    replays leaves it there, as it does on the live routes."""
    edge_key = ("__junction_5", "sec2__entry_left_3", "l2")
    descents: list[tuple[float, float]] = []
    compile_plan = member_geometry.compile_corridor_cohort_plan

    def record(
        ledger: Any, targets: Sequence[Any], **kwargs: Any
    ) -> cci.CorridorCohortPlan:
        descents.extend(
            (target.route.points[3][0], target.route.points[4][0])
            for target in targets
            if target.edge_key == edge_key
        )
        return compile_plan(ledger, targets, **kwargs)

    monkeypatch.setattr(member_geometry, "compile_corridor_cohort_plan", record)
    _render(ROOT / "tests/fixtures/regressions/cohort_reverse_frame_seating.mmd")

    assert descents
    assert set(descents) == {(472.0, 472.0)}
