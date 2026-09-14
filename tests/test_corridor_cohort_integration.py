"""The render path exposes its settlement stages without changing geometry."""

from __future__ import annotations

import importlib.util
from dataclasses import fields, replace
from enum import Enum
from pathlib import Path

import pytest

from nf_metro.api import prepare_graph, resolve_theme
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
from nf_metro.layout.route_reservations import CorridorRegionKind, FinalCanvasGeometry
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
    SettlementStage.FINAL_SOLVE,
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
                SettlementStage.COHORT_FINAL,
                SettlementStage.VALIDATION,
            ),
        ),
        (
            "examples/rnaseq_auto.mmd",
            (
                SettlementStage.DISCOVERY,
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
