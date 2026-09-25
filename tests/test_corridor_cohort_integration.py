"""The render path exposes its settlement stages without changing geometry."""

from __future__ import annotations

import importlib.util
from dataclasses import fields, replace
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest

from nf_metro.api import prepare_graph, resolve_theme
from nf_metro.layout.constants import COORD_TOLERANCE
from nf_metro.layout.route_plan import (
    SETTLEMENT_STAGE_ORDER,
    ConvergenceDisposition,
    DemandKind,
    ExitTurnDisposition,
    FanPlanDisposition,
    KeepOutClass,
    RoutePlan,
    RouteSystemDisposition,
    SettlementStage,
    SettlementStageTrace,
    SharedReferenceKind,
    register_settlement_stage,
)
from nf_metro.layout.route_reservations import (
    ColumnGapRegion,
    CorridorOrientation,
    CorridorRegion,
    CorridorRegionKind,
    FinalCanvasGeometry,
    RowGapRegion,
)
from nf_metro.layout.routing import corridor_cohort_integration as cci
from nf_metro.layout.routing import member_geometry as member_geometry_routing
from nf_metro.layout.routing.common import Direction, graph_offset_step
from nf_metro.layout.routing.corridor_cohort_integration import (
    CorridorCohortCompilationError,
    CorridorCohortLedger,
    CorridorCohortLedgerClaim,
    CorridorCohortTarget,
    CorridorScalarOwnerKind,
    CorridorScalarVariable,
    _atomic_components,
    _BoundClaim,
    _FootprintContact,
    _FootprintOrder,
    _FootprintTerm,
    _MemberFootprintModel,
    _physical_components,
    build_corridor_footprint_witnesses,
    claims_share_fixed_lane_identity,
)
from nf_metro.layout.routing.families import RouteFamilyId
from nf_metro.parser.model import Edge, MetroGraph
from nf_metro.render import svg
from nf_metro.render.plan import _RENDER_GRAPH_EXCLUDED_FIELDS, RenderPlan

ROOT = Path(__file__).parents[1]
_BUILD_GALLERY_SCRIPT = ROOT / "scripts" / "build_gallery.py"


def _load_build_gallery():
    spec = importlib.util.spec_from_file_location(
        "build_gallery", _BUILD_GALLERY_SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def build_gallery():
    pytest.importorskip("yaml")
    return _load_build_gallery()


RESERVED_STAGES = {
    SettlementStage.COHORT_INTENT,
    SettlementStage.APERTURE_SETTLEMENT,
    SettlementStage.TYPED_MATERIALIZATION,
}


def _render(relative_path: str):
    path = ROOT / relative_path
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    return graph, svg.build_observed_render_plan(graph, resolve_theme(None, graph))


@pytest.mark.parametrize(
    ("relative_path", "expected_stages"),
    [
        (
            "examples/simple_pipeline.mmd",
            (
                SettlementStage.DISCOVERY,
                SettlementStage.GENERAL_SETTLEMENT,
                SettlementStage.COHORT_FINAL,
                SettlementStage.VALIDATION,
            ),
        ),
        (
            "examples/rnaseq_auto.mmd",
            (
                SettlementStage.DISCOVERY,
                SettlementStage.GENERAL_SETTLEMENT,
                SettlementStage.GENERAL_SETTLEMENT,
                SettlementStage.COHORT_FINAL,
                SettlementStage.VALIDATION,
            ),
        ),
    ],
)
def test_render_records_each_route_observation_in_settlement_order(
    relative_path: str,
    expected_stages: tuple[SettlementStage, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route_observations = 0
    real_observe = svg.observe_route_edges_centred

    def count_observation(*args, **kwargs):
        nonlocal route_observations
        route_observations += 1
        return real_observe(*args, **kwargs)

    monkeypatch.setattr(svg, "observe_route_edges_centred", count_observation)
    _graph, observed = _render(relative_path)
    records = observed.route_plan.settlement_trace.records

    assert tuple(record.stage for record in records) == expected_stages
    assert tuple(
        record.route_observation_rank
        for record in records
        if record.route_observation_rank is not None
    ) == tuple(range(route_observations))
    assert route_observations == sum(
        record.route_observation_rank is not None for record in records
    )
    assert RESERVED_STAGES.isdisjoint(record.stage for record in records)


def test_settlement_stage_vocabulary_is_frozen() -> None:
    assert SETTLEMENT_STAGE_ORDER == (
        SettlementStage.DISCOVERY,
        SettlementStage.GENERAL_SETTLEMENT,
        SettlementStage.COHORT_INTENT,
        SettlementStage.APERTURE_SETTLEMENT,
        SettlementStage.FINAL_SOLVE,
        SettlementStage.TYPED_MATERIALIZATION,
        SettlementStage.COHORT_FINAL,
        SettlementStage.VALIDATION,
    )


def test_expected_aborts_name_the_guard_each_fixture_trips(
    build_gallery, tmp_path
) -> None:
    """Each registered render-diff fixture aborts exactly as annotated, or renders.

    A fixture whose render aborts produces no render-diff entry at all, so the
    ``expected_aborts`` map in ``scripts/gallery.yaml`` is the only statement of
    why it is registered. Holding the map to what the fixtures do keeps it
    accurate both ways: an unannotated abort would print as a fresh failure
    every build, and an annotation left behind would hide a fixture that has
    started rendering again.
    """
    observed: dict[str, type[BaseException]] = {}
    for stem in build_gallery._render_only_stems():
        source = build_gallery.TEST_FIXTURES_DIR / f"{stem}.mmd"
        assert source.exists(), (
            f"{stem} is registered for render-diff but its fixture is absent"
        )
        try:
            build_gallery.render_mmd(source, tmp_path / f"{Path(stem).name}.svg")
        except Exception as exc:  # noqa: BLE001 - the guard's identity is the datum
            observed[stem] = type(exc)

    assert observed == build_gallery.EXPECTED_ABORTS


def test_seed_corpus_is_registered_only_for_render_diff(build_gallery) -> None:
    seed_stems = ("seed_15", "seed_41", "seed_72", "seed_77")
    assert set(seed_stems).isdisjoint(
        entry["id"] for entry in build_gallery._config["gallery"]
    )
    seed_ids = tuple(f"hash_seed_determinism/{stem}" for stem in seed_stems)
    fixtures = build_gallery._config["render_only"]["test_fixtures"]
    assert tuple(item for item in fixtures if item in seed_ids) == seed_ids
    for fixture_id in seed_ids:
        assert (build_gallery.TEST_FIXTURES_DIR / f"{fixture_id}.mmd").is_file()


def test_all_seed_render_attempts_are_present_without_yaml_runtime() -> None:
    """The registration survives an environment with no YAML parser installed.

    The parsed checks above skip when PyYAML is missing, so this reads the
    manifest as text.  It compares whole list items with the comments stripped,
    since a substring search would also match a stem named in prose.
    """
    manifest = (ROOT / "scripts" / "gallery.yaml").read_text()
    items = [
        line.strip()
        for line in manifest.splitlines()
        if not line.lstrip().startswith("#")
    ]

    for stem in ("seed_15", "seed_41", "seed_72", "seed_77"):
        assert items.count(f"- hash_seed_determinism/{stem}") == 1


def test_nested_render_only_fixture_flattens_its_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_gallery
) -> None:
    fixture_id = "hash_seed_determinism/seed_72"
    fixture_root = tmp_path / "fixtures"
    source = fixture_root / f"{fixture_id}.mmd"
    source.parent.mkdir(parents=True)
    source.write_text("graph LR\n")
    render_root = tmp_path / "renders"
    observed: list[tuple[Path, Path]] = []

    monkeypatch.setattr(build_gallery, "TEST_FIXTURES_DIR", fixture_root)
    monkeypatch.setattr(build_gallery, "RENDERS_DIR", render_root)
    monkeypatch.setattr(build_gallery, "ONLY_CHANGED", None)
    monkeypatch.setattr(build_gallery, "_manifest", {})
    monkeypatch.setitem(
        build_gallery._config["render_only"], "test_fixtures", [fixture_id]
    )
    monkeypatch.setattr(
        build_gallery,
        "render_mmd",
        lambda source_path, output_path: observed.append((source_path, output_path)),
    )

    build_gallery.render_test_fixtures()

    assert observed == [(source, render_root / "seed_72.svg")]


def test_settlement_trace_registration_is_monotonic() -> None:
    trace = register_settlement_stage(
        SettlementStageTrace(),
        SettlementStage.DISCOVERY,
        geometry_fingerprint="discovery",
    )
    trace = register_settlement_stage(
        trace,
        SettlementStage.COHORT_FINAL,
        geometry_fingerprint="final",
    )

    with pytest.raises(ValueError, match="settlement stage order"):
        register_settlement_stage(
            trace,
            SettlementStage.GENERAL_SETTLEMENT,
            geometry_fingerprint="late settlement",
        )


def test_settlement_digest_ignores_value_sharing() -> None:
    """Two equal observations digest alike however their values share storage.

    Whether the second occurrence of an equal value is the same object as the
    first is settled by upstream allocation, and that varies with
    ``PYTHONHASHSEED``. The digest names the geometry, so it must not see the
    difference.
    """
    name = "corridor"
    twin = "".join(("corri", "dor"))

    assert twin == name
    assert twin is not name

    shared = ((name, 1.0), (name, 2.0))
    unshared = ((name, 1.0), (twin, 2.0))

    assert shared == unshared
    assert svg._final_settlement_geometry_digest(
        shared
    ) == svg._final_settlement_geometry_digest(unshared)


def test_final_state_is_read_twice_and_named_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The published state is observed twice and digested once when it holds.

    The second observation is a full re-reading of the graph, every route and
    the plan -- that is what proves the read-only guards changed nothing -- but
    naming an observation costs more than taking it, so a state that matches the
    cohort-final reading carries its digest forward and only a changed one is
    digested again.
    """
    observations = 0
    digests = 0
    real_observation = svg._final_settlement_geometry_observation
    real_digest = svg._final_settlement_geometry_digest

    def counted_observation(*args, **kwargs):
        nonlocal observations
        observations += 1
        return real_observation(*args, **kwargs)

    def counted_digest(*args, **kwargs):
        nonlocal digests
        digests += 1
        return real_digest(*args, **kwargs)

    monkeypatch.setattr(
        svg, "_final_settlement_geometry_observation", counted_observation
    )
    monkeypatch.setattr(svg, "_final_settlement_geometry_digest", counted_digest)

    _graph, observed = _render("examples/simple_pipeline.mmd")
    records = observed.route_plan.settlement_trace.records

    assert observations == 2
    assert digests == 1
    assert records[-2].stage is SettlementStage.COHORT_FINAL
    assert records[-1].stage is SettlementStage.VALIDATION
    assert records[-1].geometry_fingerprint == records[-2].geometry_fingerprint


def test_geometry_mutation_after_cohort_final_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_guard = svg._assert_final_canvas_read_only_guards

    def mutate_after_guard(graph, station_offsets, routes, route_plan, final_canvas):
        real_guard(graph, station_offsets, routes, route_plan, final_canvas)
        next(iter(graph.stations.values())).x += 1.0

    monkeypatch.setattr(
        svg, "_assert_final_canvas_read_only_guards", mutate_after_guard
    )

    with pytest.raises(ValueError, match="geometry changed after cohort-final"):
        _render("examples/simple_pipeline.mmd")


@pytest.mark.parametrize(
    ("field", "mutated_value"),
    [
        ("exit_turn_member_id", "mutated-member"),
        ("exit_turn_family_id", "mutated-family"),
        ("exit_turn_axis_id", "mutated-axis"),
        ("exit_turn_segment_rank", 99),
        ("exit_lane_transition_plan_id", "mutated-transition"),
        ("fan_route_emitter", "mutated-emitter"),
    ],
)
def test_ownership_mutation_after_cohort_final_is_rejected(
    field: str,
    mutated_value: str | int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_guard = svg._assert_final_canvas_read_only_guards

    def mutate_after_guard(graph, station_offsets, routes, route_plan, final_canvas):
        real_guard(graph, station_offsets, routes, route_plan, final_canvas)
        setattr(routes[0], field, mutated_value)

    monkeypatch.setattr(
        svg, "_assert_final_canvas_read_only_guards", mutate_after_guard
    )

    with pytest.raises(ValueError, match="geometry changed after cohort-final"):
        _render("examples/simple_pipeline.mmd")


def test_final_fingerprint_dataclass_field_coverage_is_explicit() -> None:
    canonical_graph = svg._canonical_final_value(MetroGraph())
    assert canonical_graph[0] == "nf_metro.parser.model.MetroGraph"
    canonical_graph_fields = {name for name, _value in canonical_graph[1]}
    assert svg._FINAL_GRAPH_FINGERPRINT_EXCLUDED_FIELDS == frozenset(
        _RENDER_GRAPH_EXCLUDED_FIELDS
    )
    assert {field.name for field in fields(MetroGraph)} - canonical_graph_fields == (
        _RENDER_GRAPH_EXCLUDED_FIELDS
    )
    assert svg._FINAL_ROUTE_FINGERPRINT_EXCLUDED_FIELDS == frozenset()
    assert svg._FINAL_EDGE_FINGERPRINT_EXCLUDED_FIELDS == frozenset({"source_line"})
    assert {field.name for field in fields(Edge)} - {
        field.name
        for field in fields(Edge)
        if field.name not in svg._FINAL_EDGE_FINGERPRINT_EXCLUDED_FIELDS
    } == {"source_line"}
    assert svg._FINAL_PLAN_FINGERPRINT_EXCLUDED_FIELDS == frozenset(
        {"settlement_trace"}
    )
    assert {field.name for field in fields(RoutePlan)} - {
        field.name
        for field in fields(RoutePlan)
        if field.name not in svg._FINAL_PLAN_FINGERPRINT_EXCLUDED_FIELDS
    } == {"settlement_trace"}
    assert {field.name for field in fields(FinalCanvasGeometry)} == {
        "width",
        "height",
        "header_keepouts",
        "route_polylines",
        "route_curve_radii",
        "route_segment_shifts",
    }
    assert set(svg._FINAL_RENDER_PLAN_FINGERPRINT_SOURCES) == {
        field.name for field in fields(RenderPlan)
    }


def test_materialized_turnout_radius_mutation_after_cohort_final_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_guard = svg._assert_final_canvas_read_only_guards

    def mutate_after_guard(graph, station_offsets, routes, route_plan, final_canvas):
        real_guard(graph, station_offsets, routes, route_plan, final_canvas)
        route_index = next(
            index
            for index, route in enumerate(routes)
            if route.source_turnout is not None
            and final_canvas.route_curve_radii[index]
        )
        radii = final_canvas.route_curve_radii[route_index]
        assert isinstance(radii, list)
        radii[0] += 1.0

    monkeypatch.setattr(
        svg, "_assert_final_canvas_read_only_guards", mutate_after_guard
    )

    with pytest.raises(ValueError, match="geometry changed after cohort-final"):
        _render("examples/topologies/same_destination_short_overlap.mmd")


def test_graph_title_mutation_after_cohort_final_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_guard = svg._assert_final_canvas_read_only_guards

    def mutate_after_guard(graph, station_offsets, routes, route_plan, published):
        real_guard(graph, station_offsets, routes, route_plan, published)
        graph.title = "mutated title"

    monkeypatch.setattr(
        svg, "_assert_final_canvas_read_only_guards", mutate_after_guard
    )

    with pytest.raises(ValueError, match="geometry changed after cohort-final"):
        _render("examples/simple_pipeline.mmd")


def test_graph_line_order_mutation_after_cohort_final_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_guard = svg._assert_final_canvas_read_only_guards

    def mutate_after_guard(graph, station_offsets, routes, route_plan, published):
        real_guard(graph, station_offsets, routes, route_plan, published)
        graph.lines = dict(reversed(graph.lines.items()))

    monkeypatch.setattr(
        svg, "_assert_final_canvas_read_only_guards", mutate_after_guard
    )

    with pytest.raises(ValueError, match="geometry changed after cohort-final"):
        _render("examples/simple_pipeline.mmd")


@pytest.mark.parametrize(
    ("relative_path", "mutation"),
    (
        ("examples/rnaseq_auto.mmd", "header-label"),
        ("examples/genomic_pipeline.mmd", "bridge"),
        ("examples/simple_pipeline.mmd", "label"),
        ("examples/group_labels.mmd", "group-band"),
        ("examples/simple_pipeline.mmd", "chrome"),
    ),
)
def test_published_render_geometry_mutation_after_cohort_final_is_rejected(
    relative_path: str,
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_guard = svg._assert_final_canvas_read_only_guards

    def mutate_after_guard(graph, station_offsets, routes, route_plan, published):
        real_guard(graph, station_offsets, routes, route_plan, published)
        if mutation == "header-label":
            placement = next(iter(published.header_placements.values()))
            object.__setattr__(placement, "label_x", placement.label_x + 1.0)
        elif mutation == "bridge":
            bridge = next(
                bridge
                for route_breaks in published.bridge_breaks
                for bridge in route_breaks
            )
            object.__setattr__(
                bridge, "cut_a", (bridge.cut_a[0] + 1.0, bridge.cut_a[1])
            )
        elif mutation == "label":
            label = published.labels[0]
            label.x += 1.0
        elif mutation == "group-band":
            assert isinstance(published.group_bands, list)
            band = published.group_bands[0]
            published.group_bands[0] = band._replace(rule_y=band.rule_y + 1.0)
        else:
            assert mutation == "chrome"
            object.__setattr__(published, "legend_x", published.legend_x + 1.0)

    monkeypatch.setattr(
        svg, "_assert_final_canvas_read_only_guards", mutate_after_guard
    )

    with pytest.raises(ValueError, match="geometry changed after cohort-final"):
        _render(relative_path)


_VALUE_SHARING_ENUM_FAMILIES = (
    (
        ExitTurnDisposition.PLANNED,
        FanPlanDisposition.PLANNED,
        ConvergenceDisposition.PLANNED,
        RouteSystemDisposition.PLANNED,
    ),
    (
        ExitTurnDisposition.LEGACY,
        FanPlanDisposition.LEGACY,
        ConvergenceDisposition.LEGACY,
    ),
    (KeepOutClass.CANVAS, CorridorRegionKind.CANVAS),
    (SharedReferenceKind.RUNWAY, DemandKind.RUNWAY),
)


@pytest.mark.parametrize(
    "family", _VALUE_SHARING_ENUM_FAMILIES, ids=lambda f: f[0].value
)
def test_final_fingerprint_separates_enum_types_sharing_one_value(
    family: tuple[Enum, ...],
) -> None:
    """A shared ``.value`` must not merge two enum types' projections.

    A ``(str, Enum)`` member compares equal to, and hashes alike as, a member
    of another such class carrying the same value and name, so a projection
    cache the two can share hands the second member the first member's type
    id. The cohort-final fingerprint reads these disposition fields, so that
    merge would silently stop the guard distinguishing one planning decision
    from another.
    """
    assert len({member.value for member in family}) == 1
    assert len({type(member) for member in family}) == len(family)
    first, second = family[0], family[1]
    assert first == second
    assert hash(first) == hash(second)

    projections = [svg._canonical_final_value(member) for member in family]
    for member, projection in zip(family, projections):
        member_type = type(member)
        assert projection == (
            f"{member_type.__module__}.{member_type.__qualname__}",
            member.value,
        )
    assert len(set(projections)) == len(family)
    assert len(
        {
            svg._final_settlement_geometry_digest((projection,))
            for projection in projections
        }
    ) == len(family)


def test_final_fingerprint_uses_qualified_type_ids_and_rejects_objects() -> None:
    edge = svg._canonical_final_value(Edge("source", "target", "line"))
    stage = svg._canonical_final_value(SettlementStage.DISCOVERY)

    assert edge[0] == "nf_metro.parser.model.Edge"
    assert stage[0] == "nf_metro.layout.route_plan.SettlementStage"
    assert svg._canonical_final_value({"second": 2, "first": 1}) == (
        ("second", 2),
        ("first", 1),
    )
    with pytest.raises(
        TypeError, match=r"unsupported final fingerprint value type: builtins\.object"
    ):
        svg._canonical_final_value(object())


@pytest.mark.parametrize(
    "mutation",
    (
        "route-map",
        "trunk-slot",
        "edge-line",
        "reservations",
        "realised-reservations",
        "dispositions",
        "boundary-requirements",
        "diagnostics",
    ),
)
def test_post_final_route_and_ledger_mutations_are_rejected(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_guard = svg._assert_final_canvas_read_only_guards

    def mutate_after_guard(graph, station_offsets, routes, route_plan, final_canvas):
        real_guard(graph, station_offsets, routes, route_plan, final_canvas)
        route = routes[0]
        if mutation == "route-map":
            route.concentric_corner_offsets_by_segment[999] = (1.0, 2.0)
        elif mutation == "trunk-slot":
            route.trunk_slot = None if route.trunk_slot is not None else "mutated"
        elif mutation == "edge-line":
            route.edge = replace(route.edge, line_id="mutated-line")
        else:
            field = {
                "reservations": "reservations",
                "realised-reservations": "realised_reservations",
                "dispositions": "exit_turn_dispositions",
                "boundary-requirements": "boundary_clearance_requirements",
                "diagnostics": "diagnostics",
            }[mutation]
            current = getattr(route_plan, field)
            object.__setattr__(route_plan, field, current + (None,))

    monkeypatch.setattr(
        svg, "_assert_final_canvas_read_only_guards", mutate_after_guard
    )

    with pytest.raises(ValueError, match="geometry changed after cohort-final"):
        _render("examples/simple_pipeline.mmd")


def test_edge_source_line_is_excluded_from_final_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_guard = svg._assert_final_canvas_read_only_guards

    def mutate_after_guard(graph, station_offsets, routes, route_plan, final_canvas):
        real_guard(graph, station_offsets, routes, route_plan, final_canvas)
        routes[0].edge = replace(routes[0].edge, source_line=999)

    monkeypatch.setattr(
        svg, "_assert_final_canvas_read_only_guards", mutate_after_guard
    )

    _render("examples/simple_pipeline.mmd")


def test_final_trace_registration_follows_reservation_realisation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    real_realise = svg.realise_route_reservations
    real_register = svg.register_settlement_stage

    def observe_realisation(*args, **kwargs):
        events.append("realise")
        return real_realise(*args, **kwargs)

    def observe_registration(trace, stage, **kwargs):
        if stage in {SettlementStage.COHORT_FINAL, SettlementStage.VALIDATION}:
            events.append(stage.value)
        return real_register(trace, stage, **kwargs)

    monkeypatch.setattr(svg, "realise_route_reservations", observe_realisation)
    monkeypatch.setattr(svg, "register_settlement_stage", observe_registration)

    _render("examples/simple_pipeline.mmd")

    assert events[-3:] == ["realise", "cohort-final", "validation"]


# --- Owner-typed corridor records and the footprint relation graph ---


def _footprint_target(
    member_id: str, line_id: str, points: list[tuple[float, float]]
) -> CorridorCohortTarget:
    edge_key = (f"{member_id}:source", f"{member_id}:target", line_id)
    return CorridorCohortTarget(
        member_id,
        f"plan:{member_id}",
        edge_key,
        RouteFamilyId.MERGE_TRUNK,
        (f"connector:{member_id}",),
        SimpleNamespace(
            edge=SimpleNamespace(source=edge_key[0], target=edge_key[1]),
            line_id=line_id,
            points=points,
        ),
        False,
    )


def test_footprint_witnesses_publish_typed_scalar_ownership_deterministically() -> None:
    target = _footprint_target(
        "scalar",
        "trunk",
        [(0.0, 4.0), (10.0, 4.0), (10.0, 20.0)],
    )
    variable = CorridorScalarVariable(
        "variable:scalar",
        CorridorScalarOwnerKind.CONVERGENCE_TRUNK,
        "convergence:scalar",
        target.member_id,
        target.edge_key,
        target.connector_ids,
        1,
        0,
        10.0,
    )

    witnesses = build_corridor_footprint_witnesses((target,), (variable,))

    assert tuple(item.segment_rank for item in witnesses) == (0, 1)
    assert witnesses[0].end_variable_id == variable.variable_id
    assert witnesses[1].coordinate_variable_id == variable.variable_id
    assert witnesses[1].owner_id == target.member_geometry_plan_id


def test_semantic_ledger_claims_store_no_observed_geometry() -> None:
    field_names = {field.name for field in fields(CorridorCohortLedgerClaim)}

    assert "coordinate" not in field_names
    assert "longitudinal_start" not in field_names
    assert "longitudinal_end" not in field_names


def _identity_claim(
    *,
    claim_id: str,
    reservation_id: str,
    network_id: str | None,
    endpoint_cohort_id: str | None,
    direction: Direction = Direction.R,
    region: CorridorRegion = RowGapRegion(0, 1),
    orientation: CorridorOrientation = CorridorOrientation.HORIZONTAL,
    lane_rank: int | None = 0,
    segment_rank: int = 0,
    endpoint_network_rank: int | None = None,
) -> CorridorCohortLedgerClaim:
    return CorridorCohortLedgerClaim(
        claim_id=claim_id,
        reservation_id=reservation_id,
        reservation_rank=0,
        claim_rank=0,
        region=region,
        orientation=orientation,
        direction=direction,
        lane_rank=lane_rank,
        member_id=claim_id,
        member_geometry_plan_id=f"plan:{claim_id}",
        edge_key=(f"{claim_id}:source", f"{claim_id}:target", "line"),
        family_id=RouteFamilyId.SAME_Y_STRAIGHT,
        connector_ids=(f"connector:{claim_id}",),
        segment_rank=segment_rank,
        path_rank=0,
        endpoint_cohort_id=endpoint_cohort_id,
        endpoint_network_rank=endpoint_network_rank,
        destination_boundary_carrier=endpoint_cohort_id is not None,
        destination_boundary_axis_sign=None,
        network_id=network_id,
        reservation_complete=True,
    )


def test_opposite_running_peer_is_not_a_fixed_equality() -> None:
    """Two claims that run in opposite directions never share a fixed lane.

    ``claims_share_fixed_lane_identity`` takes no coordinates, so it has
    nothing to fall back on: the direction mismatch alone must decide it.
    """
    movable = _identity_claim(
        claim_id="movable",
        reservation_id="reservation:shared",
        network_id="network",
        endpoint_cohort_id="endpoint",
    )
    fixed = _identity_claim(
        claim_id="fixed",
        reservation_id="reservation:shared",
        network_id="network",
        endpoint_cohort_id=None,
        direction=Direction.L,
    )

    assert not claims_share_fixed_lane_identity(movable, fixed)


def test_coordinate_proximity_without_a_shared_network_is_not_a_fixed_equality() -> (
    None
):
    """Two claims from different networks never share a fixed lane.

    ``claims_share_fixed_lane_identity`` takes no coordinates, so a route
    that later happens to land both claims at the same point cannot make
    this true: only shared witness identity can.
    """
    movable = _identity_claim(
        claim_id="movable",
        reservation_id="reservation:movable",
        network_id="network:movable",
        endpoint_cohort_id="endpoint",
    )
    fixed = _identity_claim(
        claim_id="fixed",
        reservation_id="reservation:fixed",
        network_id="network:fixed",
        endpoint_cohort_id=None,
    )

    assert not claims_share_fixed_lane_identity(movable, fixed)


# --- Atomic component closure over typed footprint relations ---


def _bound_claim(
    *,
    claim_id: str,
    region: CorridorRegion,
    orientation: CorridorOrientation,
    longitudinal_start: float,
    longitudinal_end: float,
    coordinate: float = 0.0,
) -> _BoundClaim:
    ledger = replace(
        _identity_claim(
            claim_id=claim_id,
            reservation_id=f"reservation:{claim_id}",
            network_id=None,
            endpoint_cohort_id=None,
        ),
        region=region,
        orientation=orientation,
    )
    target = _footprint_target(claim_id, "line", [(0.0, 0.0), (1.0, 0.0)])
    return _BoundClaim(
        ledger, target, longitudinal_start, longitudinal_end, coordinate, None
    )


def _scalar_variable(
    variable_id: str, *, axis: int, coordinate: float = 0.0
) -> CorridorScalarVariable:
    return CorridorScalarVariable(
        variable_id,
        CorridorScalarOwnerKind.CONVERGENCE_TRUNK,
        f"convergence:{variable_id}",
        f"member:{variable_id}",
        (f"{variable_id}:source", f"{variable_id}:target", "line"),
        (f"connector:{variable_id}",),
        0,
        axis,
        coordinate,
    )


def _order(owner_id: str, participant_variable_ids: tuple[str, ...]) -> _FootprintOrder:
    return _FootprintOrder(
        owner_id,
        _FootprintTerm(participant_variable_ids[0], None, f"witness:{owner_id}:lower"),
        _FootprintTerm(None, 0.0, f"witness:{owner_id}:upper"),
        1.0,
        participant_variable_ids,
        (f"witness:{owner_id}",),
        (),
    )


def _contact(
    owner_id: str, participant_variable_ids: tuple[str, ...]
) -> _FootprintContact:
    return _FootprintContact(
        owner_id,
        participant_variable_ids,
        (f"witness:{owner_id}",),
        "network",
        (),
        (),
    )


def test_seed77_closes_one_snapshot_into_complete_atomic_components() -> None:
    """One closure snapshot groups overlap and relation-linked nodes together
    and leaves an untouched physical group standing alone.

    Asserts directly on the closure output
    (``_AtomicComponentSpec.physical_ranks`` / ``.scalar_variable_ids``).
    """
    overlap_a = _bound_claim(
        claim_id="overlap-a",
        region=RowGapRegion(0, 1),
        orientation=CorridorOrientation.HORIZONTAL,
        longitudinal_start=0.0,
        longitudinal_end=10.0,
    )
    overlap_b = _bound_claim(
        claim_id="overlap-b",
        region=RowGapRegion(0, 1),
        orientation=CorridorOrientation.HORIZONTAL,
        longitudinal_start=5.0,
        longitudinal_end=15.0,
    )
    isolated_same_axis = _bound_claim(
        claim_id="isolated-same-axis",
        region=RowGapRegion(0, 1),
        orientation=CorridorOrientation.HORIZONTAL,
        longitudinal_start=20.0,
        longitudinal_end=30.0,
    )
    isolated_other_axis = _bound_claim(
        claim_id="isolated-other-axis",
        region=ColumnGapRegion(0, 1),
        orientation=CorridorOrientation.VERTICAL,
        longitudinal_start=0.0,
        longitudinal_end=10.0,
    )
    claims = (overlap_a, overlap_b, isolated_same_axis, isolated_other_axis)

    carrier_variable = _scalar_variable("member-carrier|overlap-a", axis=1)
    scalar_variable = _scalar_variable("convergence-trunk|scalar", axis=1)
    footprint_model = _MemberFootprintModel(
        (carrier_variable, scalar_variable),
        (),
        {
            carrier_variable.variable_id: (overlap_a.claim_id,),
            scalar_variable.variable_id: (),
        },
        (
            _order(
                "relation:carrier-scalar",
                (carrier_variable.variable_id, scalar_variable.variable_id),
            ),
        ),
        (),
    )

    physical = _physical_components(claims)
    components = _atomic_components(claims, physical, footprint_model)

    rank_by_claim_id = {claim.claim_id: rank for rank, claim in enumerate(claims)}
    overlap_group = next(
        group for group in physical if rank_by_claim_id["overlap-a"] in group
    )
    same_axis_group = next(
        group for group in physical if rank_by_claim_id["isolated-same-axis"] in group
    )
    other_axis_group = next(
        group for group in physical if rank_by_claim_id["isolated-other-axis"] in group
    )
    assert set(overlap_group) == {
        rank_by_claim_id["overlap-a"],
        rank_by_claim_id["overlap-b"],
    }
    assert same_axis_group == (rank_by_claim_id["isolated-same-axis"],)
    assert other_axis_group == (rank_by_claim_id["isolated-other-axis"],)

    components_by_physical_rank = {
        rank: component for component in components for rank in component.physical_ranks
    }
    overlap_group_rank = physical.index(overlap_group)
    same_axis_group_rank = physical.index(same_axis_group)
    other_axis_group_rank = physical.index(other_axis_group)

    joined = components_by_physical_rank[overlap_group_rank]
    assert joined.physical_ranks == (overlap_group_rank,)
    assert joined.scalar_variable_ids == (scalar_variable.variable_id,)

    standalone_same_axis = components_by_physical_rank[same_axis_group_rank]
    assert standalone_same_axis.physical_ranks == (same_axis_group_rank,)
    assert standalone_same_axis.scalar_variable_ids == ()

    standalone_other_axis = components_by_physical_rank[other_axis_group_rank]
    assert standalone_other_axis.physical_ranks == (other_axis_group_rank,)
    assert standalone_other_axis.scalar_variable_ids == ()

    assert len(components) == 3


def test_contact_only_scalar_nodes_close_into_one_component_without_claims() -> None:
    """A contact relation must join two scalar-only nodes even though
    neither has claim ranks.
    """
    scalar_a = _scalar_variable("scalar-a", axis=0)
    scalar_b = _scalar_variable("scalar-b", axis=0)
    footprint_model = _MemberFootprintModel(
        (scalar_a, scalar_b),
        (),
        {scalar_a.variable_id: (), scalar_b.variable_id: ()},
        (),
        (_contact("relation:contact", (scalar_a.variable_id, scalar_b.variable_id)),),
    )

    components = _atomic_components((), (), footprint_model)

    assert len(components) == 1
    assert components[0].physical_ranks == ()
    assert set(components[0].scalar_variable_ids) == {
        scalar_a.variable_id,
        scalar_b.variable_id,
    }


def test_same_axis_scalar_variables_without_a_relation_stay_separate() -> None:
    """Sharing an axis must never union two scalar nodes by itself."""
    scalar_a = _scalar_variable("scalar-a", axis=0, coordinate=0.0)
    scalar_b = _scalar_variable("scalar-b", axis=0, coordinate=10.0)
    footprint_model = _MemberFootprintModel(
        (scalar_a, scalar_b),
        (),
        {scalar_a.variable_id: (), scalar_b.variable_id: ()},
        (),
        (),
    )

    components = _atomic_components((), (), footprint_model)

    assert len(components) == 2
    assert {component.scalar_variable_ids for component in components} == {
        (scalar_a.variable_id,),
        (scalar_b.variable_id,),
    }


def test_relation_spanning_two_orientation_buckets_fails_closed() -> None:
    """A relation naming claims from two ``(region, orientation)`` buckets
    must raise rather than silently merge them into one component.

    Physical-overlap bucketing alone cannot stop this: a relation can name
    participants that live in different buckets, so closure needs its own
    guard.
    """
    horizontal = _bound_claim(
        claim_id="horizontal",
        region=RowGapRegion(0, 1),
        orientation=CorridorOrientation.HORIZONTAL,
        longitudinal_start=0.0,
        longitudinal_end=10.0,
    )
    vertical = _bound_claim(
        claim_id="vertical",
        region=ColumnGapRegion(0, 1),
        orientation=CorridorOrientation.VERTICAL,
        longitudinal_start=0.0,
        longitudinal_end=10.0,
    )
    claims = (horizontal, vertical)
    horizontal_variable = _scalar_variable("member-carrier|horizontal", axis=1)
    vertical_variable = _scalar_variable("member-carrier|vertical", axis=0)
    footprint_model = _MemberFootprintModel(
        (horizontal_variable, vertical_variable),
        (),
        {
            horizontal_variable.variable_id: (horizontal.claim_id,),
            vertical_variable.variable_id: (vertical.claim_id,),
        },
        (
            _order(
                "relation:cross-orientation",
                (horizontal_variable.variable_id, vertical_variable.variable_id),
            ),
        ),
        (),
    )
    physical = _physical_components(claims)

    with pytest.raises(CorridorCohortCompilationError, match="orientation"):
        _atomic_components(claims, physical, footprint_model)


# --- Member footprint relation lowering through the scalar corridor solver ---


def _mutable_route(
    *,
    source: str,
    target: str,
    line_id: str,
    points: list[tuple[float, float]],
) -> SimpleNamespace:
    return SimpleNamespace(
        edge=SimpleNamespace(source=source, target=target),
        line_id=line_id,
        points=points,
        curve_radii=None,
        route_system_owned_segment_ranks=(),
        convergence_owned_segment_ranks=(),
        fan_route_emitter=None,
        exit_turn_axis_id=None,
        exit_turn_segment_rank=None,
    )


def test_fixed_endpoint_landing_is_planned_clear_of_an_unclaimed_fixed_lead() -> None:
    """A landing carrier plans at its own port slot, never snapped onto a
    collinear fixed lead that belongs to a different network.

    The carrier drops vertically and lands rightward at ``y=40``; an immutable
    lead in a different network runs horizontally along the same ``y=40`` but
    starts well to the right. Because the two claims share no network identity
    they cannot form a ``CorridorFixedEquality``, so nothing may pull the
    carrier onto the lead: the carrier keeps its own coordinates, the lead is
    an obstacle rather than a member, and its geometry is left untouched.
    """
    carrier_claim = _identity_claim(
        claim_id="carrier",
        reservation_id="reservation:carrier",
        network_id="network:carrier",
        endpoint_cohort_id="cohort:landing",
        direction=Direction.D,
        region=ColumnGapRegion(0, 1),
        orientation=CorridorOrientation.VERTICAL,
        endpoint_network_rank=0,
    )
    fixed_claim = _identity_claim(
        claim_id="fixed",
        reservation_id="reservation:fixed",
        network_id="network:fixed",
        endpoint_cohort_id=None,
    )
    carrier_edge = carrier_claim.edge_key
    fixed_edge = fixed_claim.edge_key
    assert carrier_edge is not None and fixed_edge is not None
    carrier_route = _mutable_route(
        source=carrier_edge[0],
        target=carrier_edge[1],
        line_id=carrier_edge[2],
        points=[(0.0, 0.0), (0.0, 40.0), (30.0, 40.0)],
    )
    fixed_route = _mutable_route(
        source=fixed_edge[0],
        target=fixed_edge[1],
        line_id=fixed_edge[2],
        points=[(60.0, 40.0), (90.0, 40.0)],
    )
    # The different-network precondition is what makes the assertions
    # non-vacuous: a shared identity would let a fixed equality snap the two
    # collinear claims together regardless of the lowering under test.
    assert not claims_share_fixed_lane_identity(carrier_claim, fixed_claim)

    ledger = CorridorCohortLedger(
        claims=(carrier_claim, fixed_claim),
        endpoint_members=(("cohort:landing", frozenset({"carrier"})),),
        eligible_member_ids=frozenset({"carrier"}),
        ambiguous_endpoint_cohort_ids=frozenset(),
        offset_step=10.0,
    )
    carrier_target = CorridorCohortTarget(
        "carrier",
        "plan:carrier",
        carrier_edge,
        RouteFamilyId.SAME_Y_STRAIGHT,
        ("connector:carrier",),
        carrier_route,
        True,
        endpoint_lane_axis=1,
        endpoint_lane_coordinate=40.0,
        network_id="network:carrier",
    )
    fixed_target = CorridorCohortTarget(
        "fixed",
        "plan:fixed",
        fixed_edge,
        RouteFamilyId.SAME_Y_STRAIGHT,
        ("connector:fixed",),
        fixed_route,
        False,
        network_id="network:fixed",
    )
    targets = (carrier_target, fixed_target)

    fixed_points_before = tuple(fixed_route.points)

    plan = cci.compile_corridor_cohort_plan(ledger, targets)
    cci.publish_corridor_cohort_plan(plan)

    (landing,) = plan.landings
    assert landing.member_id == "carrier"
    assert landing.axis == 1
    assert landing.coordinate == pytest.approx(40.0)

    (carrier_allocation,) = [
        allocation
        for allocation in plan.allocations
        if allocation.member_id == "carrier"
    ]
    assert carrier_allocation.axis == 0
    assert carrier_allocation.coordinate == pytest.approx(0.0)

    landing_segment = carrier_route.points[-2:]
    assert landing_segment[0][1] == pytest.approx(landing_segment[1][1])
    assert fixed_route.points[0][1] == pytest.approx(fixed_route.points[1][1])
    assert landing_segment[0][1] == pytest.approx(fixed_route.points[0][1])

    landing_x_max = max(point[0] for point in landing_segment)
    fixed_x_min = min(point[0] for point in fixed_route.points)
    assert landing_x_max + COORD_TOLERANCE <= fixed_x_min

    assert all(allocation.member_id != "fixed" for allocation in plan.allocations)
    assert tuple(fixed_route.points) == fixed_points_before


def test_problem_rejects_an_infeasible_fixed_fixed_footprint_order() -> None:
    """A footprint order whose two ends are both fixed and overlap fails closed.

    Both ``_FootprintOrder`` constructors in ``_member_footprint_model`` bind at
    least one variable term today, so the fixed-fixed branch of ``_problem`` is
    unreachable through the live builders. This drives it directly to prove the
    guard raises rather than silently rotting into dead code.
    """
    order = _FootprintOrder(
        "relation:fixed-fixed",
        _FootprintTerm(None, 10.0, "witness:lower"),
        _FootprintTerm(None, 0.0, "witness:upper"),
        5.0,
        (),
        ("witness:fixed",),
        (),
    )
    footprint_model = _MemberFootprintModel((), (), {}, (order,), (), ())

    with pytest.raises(CorridorCohortCompilationError, match="infeasible"):
        cci._problem(
            (),
            {},
            True,
            10.0,
            8.0,
            footprint_model,
            {},
        )


def _footprint_witness(
    *,
    footprint_id: str,
    member_id: str,
    edge_key: tuple[str, str, str],
    axis: int,
    coordinate: float,
    longitudinal_start: float,
    longitudinal_end: float,
    coordinate_variable_id: str,
) -> cci.CorridorFootprintWitness:
    return cci.CorridorFootprintWitness(
        footprint_id=footprint_id,
        owner_id=f"plan:{member_id}",
        member_id=member_id,
        edge_key=edge_key,
        connector_ids=(f"connector:{member_id}",),
        segment_rank=0,
        axis=axis,
        coordinate=coordinate,
        longitudinal_start=longitudinal_start,
        longitudinal_end=longitudinal_end,
        direction=Direction.R,
        line_id=edge_key[2],
        network_id=None,
        regions=(),
        semantic_rank=(0, 0),
        crossing_disposition=cci.CorridorCrossingDisposition.FIXED_DOGLEG,
        coordinate_variable_id=coordinate_variable_id,
    )


def _member_scalar_relation_scenario():
    """One movable member carrier plus one convergence scalar request that a
    single ``_FootprintOrder`` joins into one atomic component.

    Returns the compile inputs and the synthetic ``_MemberFootprintModel`` a
    monkeypatched ``_member_footprint_model`` publishes, so the relation the live
    witness builder does not yet mint is present at the compiler boundary.
    """
    carrier_claim = _identity_claim(
        claim_id="carrier",
        reservation_id="reservation:carrier",
        network_id="network:carrier",
        endpoint_cohort_id=None,
        direction=Direction.R,
        region=RowGapRegion(0, 1),
        orientation=CorridorOrientation.HORIZONTAL,
    )
    carrier_edge = carrier_claim.edge_key
    assert carrier_edge is not None
    carrier_route = _mutable_route(
        source=carrier_edge[0],
        target=carrier_edge[1],
        line_id=carrier_edge[2],
        points=[(0.0, 10.0), (30.0, 10.0)],
    )
    carrier_target = CorridorCohortTarget(
        "carrier",
        "plan:carrier",
        carrier_edge,
        RouteFamilyId.SAME_Y_STRAIGHT,
        ("connector:carrier",),
        carrier_route,
        True,
        network_id="network:carrier",
    )
    ledger = CorridorCohortLedger(
        claims=(carrier_claim,),
        endpoint_members=(),
        eligible_member_ids=frozenset({"carrier"}),
        ambiguous_endpoint_cohort_ids=frozenset(),
        offset_step=10.0,
    )

    scalar_edge = ("scalar:source", "scalar:target", "line")
    scalar_variable = CorridorScalarVariable(
        "convergence-trunk|scalar",
        CorridorScalarOwnerKind.CONVERGENCE_TRUNK,
        "convergence:scalar",
        "member:scalar",
        scalar_edge,
        ("connector:member:scalar",),
        0,
        1,
        25.0,
    )
    scalar_request = cci.CorridorScalarRequest(
        scalar_variable,
        preferred_coordinate=25.0,
        domain=cci.CorridorCoordinateDomain(scalar_variable.variable_id),
    )

    carrier_variable_id = f"member-carrier|carrier|{carrier_edge}|segment:0"
    carrier_variable = CorridorScalarVariable(
        carrier_variable_id,
        CorridorScalarOwnerKind.MEMBER_CARRIER,
        "plan:carrier",
        "carrier",
        carrier_edge,
        ("connector:carrier",),
        0,
        1,
        10.0,
    )
    carrier_witness = _footprint_witness(
        footprint_id="witness:carrier",
        member_id="carrier",
        edge_key=carrier_edge,
        axis=1,
        coordinate=10.0,
        longitudinal_start=0.0,
        longitudinal_end=30.0,
        coordinate_variable_id=carrier_variable_id,
    )
    scalar_witness = _footprint_witness(
        footprint_id="witness:scalar",
        member_id="member:scalar",
        edge_key=scalar_edge,
        axis=1,
        coordinate=25.0,
        longitudinal_start=5.0,
        longitudinal_end=20.0,
        coordinate_variable_id=scalar_variable.variable_id,
    )
    order = _FootprintOrder(
        "relation:carrier-scalar",
        _FootprintTerm(carrier_variable_id, None, "witness:carrier"),
        _FootprintTerm(scalar_variable.variable_id, None, "witness:scalar"),
        5.0,
        (carrier_variable_id, scalar_variable.variable_id),
        ("witness:carrier", "witness:scalar"),
        (),
    )
    footprint_model = _MemberFootprintModel(
        (carrier_variable, scalar_variable),
        (carrier_witness, scalar_witness),
        {
            carrier_variable_id: (carrier_claim.claim_id,),
            scalar_variable.variable_id: (),
        },
        (order,),
        (),
        (),
    )
    return ledger, (carrier_target,), (scalar_request,), footprint_model


def test_member_and_scalar_relation_occupy_one_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member carrier and a convergence scalar joined by one ``_FootprintOrder``
    lower into one ``CorridorAllocationProblem`` solved once for their axis.

    The live witness builder does not yet mint a member<->scalar order, so the
    synthetic footprint model stands in for that relation at the compiler
    boundary. The defect this locks is the split solve: the member reaches its
    own component problem while the scalar is swept into a separate per-axis
    problem, so no one problem names both and the solver runs twice.
    """
    ledger, targets, scalar_requests, footprint_model = (
        _member_scalar_relation_scenario()
    )
    monkeypatch.setattr(
        cci, "_member_footprint_model", lambda *args, **kwargs: footprint_model
    )
    solved_problems: list[cci.CorridorAllocationProblem] = []
    real_solve = cci.solve_corridor_cohorts

    def spy_solve(problem):
        solved_problems.append(problem)
        return real_solve(problem)

    monkeypatch.setattr(cci, "solve_corridor_cohorts", spy_solve)

    plan = cci.compile_corridor_cohort_plan(
        ledger, targets, scalar_requests=scalar_requests
    )

    scalar_variable_id = scalar_requests[0].variable.variable_id
    problems_with_carrier = [
        problem
        for problem in solved_problems
        if any(lane.member_id == "carrier" for lane in problem.lanes)
    ]
    problems_with_scalar = [
        problem
        for problem in solved_problems
        if any(lane.member_id == scalar_variable_id for lane in problem.lanes)
    ]
    joint_problems = [
        problem
        for problem in solved_problems
        if {"carrier", scalar_variable_id}.issubset(
            {lane.member_id for lane in problem.lanes}
        )
    ]

    assert len(problems_with_carrier) == 1
    assert len(problems_with_scalar) == 1
    assert len(joint_problems) == 1
    assert len(solved_problems) == 1

    joint = joint_problems[0]
    assert any(
        separation.lower_member_id == "carrier"
        and separation.upper_member_id == scalar_variable_id
        for separation in joint.directed_separations
    )

    grant = next(
        item for item in plan.scalar_grants if item.variable_id == scalar_variable_id
    )
    assert grant.owner_kind is CorridorScalarOwnerKind.CONVERGENCE_TRUNK


def test_no_separate_scalar_solve_path_exists() -> None:
    """The convergence sweep is deleted, not merely bypassed.

    A second solve path is the anti-pattern this issue removes; a test that reds
    if it reappears keeps the compiler down to its one integration call site.
    """
    assert not hasattr(cci, "_scalar_component_plan")


def test_compiler_counts_every_solve_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """One relation-joined component solves once, and the count is published."""
    ledger, targets, scalar_requests, footprint_model = (
        _member_scalar_relation_scenario()
    )
    monkeypatch.setattr(
        cci, "_member_footprint_model", lambda *args, **kwargs: footprint_model
    )
    real_solve = cci.solve_corridor_cohorts
    calls = 0

    def counting_solve(problem):
        nonlocal calls
        calls += 1
        return real_solve(problem)

    monkeypatch.setattr(cci, "solve_corridor_cohorts", counting_solve)

    plan = cci.compile_corridor_cohort_plan(
        ledger, targets, scalar_requests=scalar_requests
    )

    assert plan.solve_call_count == 1
    assert calls == plan.solve_call_count


def test_final_solve_trace_is_emitted_from_the_integration_call_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one integration call site records ``FINAL_SOLVE`` on a passed trace."""
    ledger, targets, scalar_requests, footprint_model = (
        _member_scalar_relation_scenario()
    )
    monkeypatch.setattr(
        cci, "_member_footprint_model", lambda *args, **kwargs: footprint_model
    )
    trace = SettlementStageTrace()
    trace = register_settlement_stage(
        trace, SettlementStage.DISCOVERY, geometry_fingerprint="discovery"
    )
    trace = register_settlement_stage(
        trace, SettlementStage.APERTURE_SETTLEMENT, geometry_fingerprint="aperture"
    )

    plan = cci.compile_corridor_cohort_plan(
        ledger, targets, scalar_requests=scalar_requests, settlement_trace=trace
    )

    assert plan.settlement_trace is not None
    assert plan.settlement_trace.records[-1].stage is SettlementStage.FINAL_SOLVE
    assert plan.settlement_trace.records[-1].route_observation_rank is None


def _assert_no_silent_compatibility(plan: cci.CorridorCohortPlan) -> None:
    """Every compatibility disposition names its whole group and migrates nothing."""
    for component in plan.components:
        if component.status is not cci.CorridorAllocationStatus.COMPATIBILITY:
            continue
        assert component.compatibility is not None
        assert component.allocations == ()


def _overlapping_member_claim(
    *,
    claim_id: str,
    reservation_complete: bool,
    longitudinal: tuple[float, float],
) -> tuple[CorridorCohortLedgerClaim, CorridorCohortTarget]:
    claim = replace(
        _identity_claim(
            claim_id=claim_id,
            reservation_id=f"reservation:{claim_id}",
            network_id=f"network:{claim_id}",
            endpoint_cohort_id=None,
            direction=Direction.R,
            region=RowGapRegion(0, 1),
            orientation=CorridorOrientation.HORIZONTAL,
        ),
        reservation_complete=reservation_complete,
    )
    edge = claim.edge_key
    assert edge is not None
    route = _mutable_route(
        source=edge[0],
        target=edge[1],
        line_id=edge[2],
        points=[(longitudinal[0], 10.0), (longitudinal[1], 10.0)],
    )
    target = CorridorCohortTarget(
        claim_id,
        f"plan:{claim_id}",
        edge,
        RouteFamilyId.SAME_Y_STRAIGHT,
        (f"connector:{claim_id}",),
        route,
        True,
        network_id=f"network:{claim_id}",
    )
    return claim, target


def test_incomplete_component_stands_as_typed_compatibility() -> None:
    """A component with an incomplete claim keeps its whole group unmigrated.

    The complete peer never migrates alone under a legacy owner: the disposition
    is compatibility for the entire component, named by typed provenance, with no
    allocation left behind.
    """
    complete_claim, complete_target = _overlapping_member_claim(
        claim_id="complete", reservation_complete=True, longitudinal=(0.0, 30.0)
    )
    incomplete_claim, incomplete_target = _overlapping_member_claim(
        claim_id="incomplete", reservation_complete=False, longitudinal=(10.0, 40.0)
    )
    ledger = CorridorCohortLedger(
        claims=(complete_claim, incomplete_claim),
        endpoint_members=(),
        eligible_member_ids=frozenset({"complete", "incomplete"}),
        ambiguous_endpoint_cohort_ids=frozenset(),
        offset_step=10.0,
    )

    plan = cci.compile_corridor_cohort_plan(
        ledger, (complete_target, incomplete_target)
    )

    (compatibility_component,) = [
        component
        for component in plan.components
        if component.status is cci.CorridorAllocationStatus.COMPATIBILITY
    ]
    provenance = compatibility_component.compatibility
    assert provenance is not None
    assert provenance.reason is cci.CorridorCompatibilityReason.INCOMPLETE_WITNESSES
    assert set(provenance.member_ids) == {"complete", "incomplete"}
    assert plan.allocations == ()
    _assert_no_silent_compatibility(plan)


def test_unrepresented_endpoint_cohort_stands_as_typed_compatibility() -> None:
    """An endpoint cohort with no current claim keeps its legacy geometry, typed."""
    carrier_claim, carrier_target = _overlapping_member_claim(
        claim_id="carrier", reservation_complete=True, longitudinal=(0.0, 30.0)
    )
    ledger = CorridorCohortLedger(
        claims=(carrier_claim,),
        endpoint_members=(("cohort:absent", frozenset({"ghost"})),),
        eligible_member_ids=frozenset({"carrier"}),
        ambiguous_endpoint_cohort_ids=frozenset(),
        offset_step=10.0,
    )

    plan = cci.compile_corridor_cohort_plan(ledger, (carrier_target,))

    (compatibility_component,) = [
        component
        for component in plan.components
        if component.status is cci.CorridorAllocationStatus.COMPATIBILITY
    ]
    provenance = compatibility_component.compatibility
    assert provenance is not None
    assert (
        provenance.reason
        is cci.CorridorCompatibilityReason.UNREPRESENTED_ENDPOINT_COHORT
    )
    assert provenance.endpoint_cohort_ids == ("cohort:absent",)
    _assert_no_silent_compatibility(plan)


def test_member_geometry_corridor_handoff_returns_heterogeneous_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The member-geometry handoff compiles complete requests and receives grants
    without applying the convergence grants."""
    from nf_metro.layout.routing import member_geometry

    ledger, targets, scalar_requests, footprint_model = (
        _member_scalar_relation_scenario()
    )
    monkeypatch.setattr(
        cci, "_member_footprint_model", lambda *args, **kwargs: footprint_model
    )

    plan = member_geometry.plan_corridor_cohorts(
        ledger, targets, scalar_requests=scalar_requests
    )

    scalar_variable_id = scalar_requests[0].variable.variable_id
    assert any(grant.variable_id == scalar_variable_id for grant in plan.scalar_grants)
    assert plan.route_patches


def test_unwitnessed_blocking_obstacle_raises_instead_of_losing_its_shortfall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clearance shortfall naming an obstacle with no footprint provenance
    fails closed rather than silently collapsing to no shortfall at all.

    A component whose solve fails always makes ``compile_corridor_cohort_plan``
    raise; what matters here is which message it raises with. Treating
    "unwitnessed" the same as "no shortfall" would report a generic failure
    with no obstacle identity, so the raised message must name the missing
    obstacle explicitly to prove the shortfall was not silently discarded.
    """
    claim = _identity_claim(
        claim_id="carrier",
        reservation_id="reservation:carrier",
        network_id="network",
        endpoint_cohort_id=None,
    )
    route = _mutable_route(
        source="carrier:source",
        target="carrier:target",
        line_id="line",
        points=[(0.0, 10.0), (30.0, 10.0)],
    )
    target = CorridorCohortTarget(
        "carrier",
        "plan:carrier",
        ("carrier:source", "carrier:target", "line"),
        RouteFamilyId.SAME_Y_STRAIGHT,
        ("connector:carrier",),
        route,
        True,
    )
    ledger = CorridorCohortLedger(
        claims=(claim,),
        endpoint_members=(),
        eligible_member_ids=frozenset({"carrier"}),
        ambiguous_endpoint_cohort_ids=frozenset(),
        offset_step=10.0,
    )

    def fake_solve(
        problem: cci.CorridorAllocationProblem,
    ) -> cci.CorridorAllocationResult:
        return cci.CorridorAllocationResult(
            status=cci.CorridorAllocationStatus.FAILURE,
            reason=cci.CorridorAllocationFailureReason.INFEASIBLE,
            blocking_member_ids=("carrier",),
            clearance_shortfall=cci.CorridorClearanceShortfall(
                claim_ids=("carrier",),
                blocking_obstacle_ids=("obstacle-missing",),
                deficit=5.0,
                axis=0,
                required_shift_sign=1,
            ),
        )

    monkeypatch.setattr(cci, "solve_corridor_cohorts", fake_solve)

    with pytest.raises(CorridorCohortCompilationError, match="unwitnessed obstacles"):
        cci.compile_corridor_cohort_plan(ledger, (target,))


def test_shortfall_without_a_directed_shift_sign_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clearance shortfall with no directed boundary side fails closed the
    same way an unwitnessed obstacle does, rather than reaching the aperture
    producer with a shift sign it cannot act on.
    """
    claim = _identity_claim(
        claim_id="carrier",
        reservation_id="reservation:carrier",
        network_id="network",
        endpoint_cohort_id=None,
    )
    route = _mutable_route(
        source="carrier:source",
        target="carrier:target",
        line_id="line",
        points=[(0.0, 10.0), (30.0, 10.0)],
    )
    target = CorridorCohortTarget(
        "carrier",
        "plan:carrier",
        ("carrier:source", "carrier:target", "line"),
        RouteFamilyId.SAME_Y_STRAIGHT,
        ("connector:carrier",),
        route,
        True,
    )
    ledger = CorridorCohortLedger(
        claims=(claim,),
        endpoint_members=(),
        eligible_member_ids=frozenset({"carrier"}),
        ambiguous_endpoint_cohort_ids=frozenset(),
        offset_step=10.0,
    )

    def fake_solve(
        problem: cci.CorridorAllocationProblem,
    ) -> cci.CorridorAllocationResult:
        return cci.CorridorAllocationResult(
            status=cci.CorridorAllocationStatus.FAILURE,
            reason=cci.CorridorAllocationFailureReason.INFEASIBLE,
            blocking_member_ids=("carrier",),
            clearance_shortfall=cci.CorridorClearanceShortfall(
                claim_ids=("carrier",),
                blocking_obstacle_ids=(),
                deficit=5.0,
                axis=0,
                required_shift_sign=0,
            ),
        )

    monkeypatch.setattr(cci, "solve_corridor_cohorts", fake_solve)

    with pytest.raises(CorridorCohortCompilationError, match="directed boundary side"):
        cci.compile_corridor_cohort_plan(ledger, (target,))


def _top_entry_drops(routes, port_id: str) -> dict[str, tuple[float, int]]:
    """Each line's drop X into *port_id* and the travel sign of its run there."""
    drops = {}
    for route in routes:
        if route.edge.target != port_id:
            continue
        (run_start, run_end), (drop_start, drop_end) = zip(
            route.points[-3:-1], route.points[-2:], strict=True
        )
        assert run_start[1] == run_end[1]
        assert drop_start[0] == drop_end[0]
        drops[route.line_id] = (
            drop_start[0],
            1 if run_end[0] > run_start[0] else -1,
        )
    return drops


def test_opposing_bypass_lines_hold_separated_direction_qualified_lanes() -> None:
    """Opposite-running bypasses into one top entry keep their own lanes.

    ``ribo`` arrives running right and ``rnaseq`` running left.  Counter-running
    lines never share a bundle, so each drops into the port on its own lane, one
    pitch apart, in the order its travel direction earns.
    """
    graph, observed = _render("examples/topologies/opposing_bypass_corridor.mmd")
    drops = _top_entry_drops(observed.plan.routes, "orf_calling__entry_top_9")

    assert drops == {"ribo": (692.0, 1), "rnaseq": (696.0, -1)}
    assert drops["rnaseq"][0] - drops["ribo"][0] == graph_offset_step(graph)


def test_no_solver_owner_contains_opposite_running_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No solver cohort or lane equality binds claims travelling opposite ways.

    Seed 77 compiles one cohort over both directions of several shared gaps; a
    cohort or equality owner holding both would bundle counter-running lines.
    Scalar trunk lanes are their own cohort and carry no ledger claim.
    """
    compiles = []
    original = member_geometry_routing.compile_corridor_cohort_plan

    def capture(ledger, targets, **kwargs):
        plan = original(ledger, targets, **kwargs)
        compiles.append((ledger, plan))
        return plan

    monkeypatch.setattr(
        member_geometry_routing, "compile_corridor_cohort_plan", capture
    )
    _render("tests/fixtures/hash_seed_determinism/seed_77.mmd")

    assert compiles
    for ledger, plan in compiles:
        direction_by_claim = {
            claim.claim_id: claim.direction for claim in ledger.claims
        }
        assert {claim.direction for claim in ledger.claims} >= {
            Direction.U,
            Direction.D,
        }
        for component in plan.components:
            for problem in component.problems:
                directions_by_owner: dict[str, set[Direction]] = {}
                for lane in problem.lanes:
                    if lane.member_id in direction_by_claim:
                        directions_by_owner.setdefault(
                            f"cohort|{lane.cohort_id}", set()
                        ).add(direction_by_claim[lane.member_id])
                for equality in problem.equalities:
                    directions_by_owner.setdefault(
                        f"equality|{equality.owner_id}", set()
                    ).update(
                        (
                            direction_by_claim[equality.left_member_id],
                            direction_by_claim[equality.right_member_id],
                        )
                    )
                assert all(
                    len(directions) == 1 for directions in directions_by_owner.values()
                ), directions_by_owner
