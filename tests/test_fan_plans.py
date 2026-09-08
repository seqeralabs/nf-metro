"""Semantic fan plans own complete structural objects or no geometry."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import nf_metro.layout.fan_plans as fan_plans
from nf_metro.api import prepare_graph
from nf_metro.layout.constants import (
    INTER_ROW_EDGE_CLEARANCE,
    SECTION_Y_PADDING,
    X_SPACING,
)
from nf_metro.layout.engine import (
    SettledRouteValidationError,
    compute_layout,
    compute_min_y_spacing,
)
from nf_metro.layout.fan_geometry import fan_lane_offsets, symmetric_lane_offsets
from nf_metro.layout.fan_plans import (
    FanPlanExecution,
    FanPlanQuery,
    FanRouteInvariantError,
    FanTopologyQuery,
    build_fan_plan_execution,
    claimed_station_ids,
    fan_appearance_lane_sign,
    install_fan_plan_execution,
    stated_station_ids,
    validate_fan_route_emissions,
)
from nf_metro.layout.geometry import AxisFrame
from nf_metro.layout.labels import place_labels
from nf_metro.layout.phases.guards import (
    PhaseInvariantError,
    _guard_planned_fan_frame_realised,
)
from nf_metro.layout.phases.planned_fans import (
    _apply_planned_fan_geometry,
    _apply_planned_fan_port_geometry,
    _snapshot_planned_fan_centrelines,
)
from nf_metro.layout.route_plan import (
    CoordinateRegime,
    DemandAxis,
    DemandKind,
    FanAppearancePolicy,
    FanCentrelineAnchor,
    FanOffsetAssignment,
    FanOffsetCarrier,
    FanPlanDisposition,
    KeepOutClass,
    SharedReferenceKind,
    build_route_plan_query,
    fan_lane_seat_keys,
)
from nf_metro.layout.routing import (
    compute_station_offsets,
    observe_route_edges,
    route_edges,
)
from nf_metro.layout.routing.common import (
    Direction,
    RoutedPath,
    gap_lo_for_x,
    gap_lookup_geometry,
    iter_vertical_segments,
)
from nf_metro.layout.routing.invariants import check_gap_channels_materialized
from nf_metro.layout.routing.offsets import _apply_planned_fan_offsets
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge, MetroGraph, Port, PortSide, Section, Station
from nf_metro.parser.route_topology import (
    AuthoredEdgeFact,
    AuthoredEdgeKey,
    BundleId,
    ConnectorId,
    ConvergenceId,
    ResolvedEdge,
    build_route_topology_query,
)

ROOT = Path(__file__).parents[1]


def _fact(
    source: str,
    target: str,
    line_id: str,
    rank: int,
    *,
    section: str = "section",
) -> AuthoredEdgeFact:
    return AuthoredEdgeFact(
        key=AuthoredEdgeKey(source, target, line_id, 0),
        rank=rank,
        source_line=rank + 1,
        source_section=section,
        target_section=section,
    )


@dataclass
class _Topology:
    authored_edges: tuple[AuthoredEdgeFact, ...]
    paths: dict[ConnectorId, tuple[tuple[ResolvedEdge, ...], ...]]
    connector_bundles: dict[ConnectorId, BundleId]
    convergences: tuple[object, ...]

    @classmethod
    def direct(cls, facts: list[AuthoredEdgeFact]) -> _Topology:
        return cls(
            authored_edges=tuple(reversed(facts)),
            paths={
                fact.id: (
                    (
                        ResolvedEdge(
                            fact.key.source,
                            fact.key.target,
                            fact.key.line_id,
                        ),
                    ),
                )
                for fact in facts
            },
            connector_bundles={},
            convergences=(),
        )

    def authored_edge(self, edge_id: ConnectorId) -> AuthoredEdgeFact:
        return next(fact for fact in self.authored_edges if fact.id == edge_id)

    def resolved_paths(
        self, edge_id: ConnectorId
    ) -> tuple[tuple[ResolvedEdge, ...], ...]:
        return self.paths.get(edge_id, ())

    def authored_edge_ids_for_edge(self, edge: ResolvedEdge) -> tuple[ConnectorId, ...]:
        return tuple(
            edge_id
            for edge_id, paths in self.paths.items()
            if any(edge in path for path in paths)
        )

    def connector(self, edge_id: ConnectorId) -> object:
        try:
            bundle_id = self.connector_bundles[edge_id]
        except KeyError as error:
            raise KeyError(edge_id) from error
        return SimpleNamespace(bundle_id=bundle_id)

    def convergence_for_junction(self, junction_id: str) -> object | None:
        return next(
            (
                convergence
                for convergence in self.convergences
                if getattr(convergence, "junction_id", None) == junction_id
            ),
            None,
        )


def _graph(direction: str = "LR") -> MetroGraph:
    graph = MetroGraph()
    graph.add_section(Section(id="section", name="Section", direction=direction))
    return graph


def test_topology_test_double_implements_planner_contract() -> None:
    assert isinstance(_Topology.direct([]), FanTopologyQuery)


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (2, (-5.0, 5.0)),
        (3, (-10.0, 0.0, 10.0)),
        (4, (-15.0, -5.0, 5.0, 15.0)),
        (6, (-25.0, -15.0, -5.0, 5.0, 15.0, 25.0)),
    ],
)
def test_lane_offsets_straddle_one_centreline(
    count: int, expected: tuple[float, ...]
) -> None:
    assert symmetric_lane_offsets(count, 10.0) == expected


def test_branch_rank_comes_from_authored_order() -> None:
    targets = ("zeta", "alpha", "mu", "beta")
    facts = [
        _fact("fork", target, f"line_{rank}", rank)
        for rank, target in enumerate(targets)
    ]

    execution = build_fan_plan_execution(
        _graph(),
        _Topology.direct(facts),
        x_spacing=30.0,
        y_spacing=10.0,
        minimum_runway=24.0,
    )

    assert len(execution.plans) == 1
    plan = execution.plans[0]
    assert plan.disposition is FanPlanDisposition.PLANNED
    assert tuple(branch.root_station_id for branch in plan.branches) == targets
    assert plan.appearance_centreline_branch_id is None
    assert tuple(branch.lane_offset for branch in plan.branches) == (
        -15.0,
        -5.0,
        5.0,
        15.0,
    )
    assert tuple(branch.diagonal_runway for branch in plan.branches) == (
        24.0,
        34.0,
        44.0,
        54.0,
    )
    assert plan.entry_runway == 24.0
    assert plan.exit_runway == 24.0
    assert plan.centreline_reference_id is None
    assert plan.demand_ids == ()
    assert execution.query.planned_for_fork("fork") is plan


def test_centreline_anchor_uses_explicit_grid_before_section_placement() -> None:
    graph = MetroGraph()
    graph.add_section(Section("source", "Source", direction="LR"))
    graph.add_section(Section("layout", "Layout", direction="LR"))
    graph.grid_overrides = {
        "source": (1, 1, 1, 1),
        "layout": (2, 1, 1, 1),
    }
    graph.add_port(Port("source_exit", "source", PortSide.RIGHT, is_entry=False))

    anchor = fan_plans._centreline_anchor(
        graph,
        direction="LR",
        frame=AxisFrame.for_direction("LR", 30.0, 10.0),
        fork_id="fork",
        layout_section_id="layout",
        branches=(),
        entry_port_ids=(),
        exit_port_ids=("source_exit",),
        local_frame_anchor=None,
    )

    assert anchor == FanCentrelineAnchor("source_exit")


def test_unique_exit_branch_keeps_trunk_on_centreline() -> None:
    facts = [
        _fact("fork", "spur", "spur", 0),
        _fact("fork", "trunk", "trunk", 1),
        _fact("trunk", "downstream", "trunk", 2),
    ]
    topology = _Topology.direct(facts)
    topology.paths[facts[-1].id] = (
        (
            ResolvedEdge("trunk", "exit_port", "trunk"),
            ResolvedEdge("exit_port", "downstream", "trunk"),
        ),
    )
    graph = _graph()
    for station_id in ("spur", "trunk"):
        graph.register_station(
            Station(id=station_id, label=station_id.title(), section_id="section")
        )
    graph.ports["fork"] = Port(
        id="fork",
        section_id="section",
        side=PortSide.LEFT,
        is_entry=True,
    )
    graph.ports["exit_port"] = Port(
        id="exit_port",
        section_id="section",
        side=PortSide.RIGHT,
        is_entry=False,
    )

    plan = build_fan_plan_execution(
        graph,
        topology,
        x_spacing=30.0,
        y_spacing=10.0,
        minimum_runway=20.0,
    ).plans[0]

    assert tuple(branch.is_trunk_continuation for branch in plan.branches) == (
        False,
        True,
    )
    assert plan.appearance_centreline_branch_id == plan.branches[1].id
    assert tuple(branch.lane_offset for branch in plan.branches) == (10.0, 0.0)
    assert plan.local_frame_anchor == FanCentrelineAnchor("trunk")


def test_local_full_bundle_continuation_owns_the_fan_centreline() -> None:
    path = ROOT / "examples" / "topologies" / "render_labelwrap_row_gap.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(item for item in graph.fan_plans if item.authored_source_id == "star")

    trunk_branches = tuple(
        branch for branch in plan.branches if branch.is_trunk_continuation
    )
    assert plan.disposition is FanPlanDisposition.PLANNED
    assert tuple(branch.root_station_id for branch in trunk_branches) == ("cram_out",)
    assert trunk_branches[0].lane_offset == 0.0
    assert all(
        not branch.is_trunk_continuation
        for branch in plan.branches
        if branch.landing_port_ids
    )
    assert (
        len(
            {
                graph.stations[station_id].y
                for station_id in ("reads_in", "bbduk", "star", "cram_out")
            }
        )
        == 1
    )


def test_foreign_merge_frame_keeps_the_complete_fan_on_legacy_layout() -> None:
    path = ROOT / "examples" / "topologies" / "tb_passthrough_continuation.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(item for item in graph.fan_plans if item.authored_source_id == "starN")

    assert plan.disposition is FanPlanDisposition.LEGACY
    assert plan.legacy_reason == "local-layout-has-foreign-owner"
    assert graph.stations["starN"].x == graph.stations["leftchild"].x
    assert (
        len(
            {graph.stations[station_id].x for station_id in ("hisatN", "merge", "tail")}
        )
        == 1
    )


def test_straight_diamond_keeps_established_layout_ownership() -> None:
    path = ROOT / "examples" / "topologies" / "shared_cell_fork_trunk_align.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(item for item in graph.fan_plans if item.authored_source_id == "p_hub")

    assert graph.diamond_style == "straight"
    assert plan.authored_join_station_id == "p_merge"
    assert plan.join_station_id == "p_merge"
    assert plan.appearance_policy is FanAppearancePolicy.STRAIGHT
    assert plan.disposition is FanPlanDisposition.LEGACY
    assert plan.legacy_reason == "straight-diamond-layout-owns-geometry"
    assert plan.layout_station_ids == ()


@pytest.mark.parametrize(
    "fixture,source_id",
    [
        ("wide_label_fan.mmd", "hub"),
        ("junction_entry_collision.mmd", "pre2"),
    ],
)
def test_straight_open_fan_keeps_top_branch_on_centreline(
    fixture: str, source_id: str
) -> None:
    """Straight fans keep their first authored branch on the main track."""
    path = ROOT / "examples" / "topologies" / fixture
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(
        item for item in graph.fan_plans if item.authored_source_id == source_id
    )

    assert plan.appearance_policy is FanAppearancePolicy.STRAIGHT
    assert plan.disposition is FanPlanDisposition.PLANNED
    assert plan.frame is not None
    assert plan.appearance_centreline_branch_id == plan.branches[0].id
    assert tuple(branch.lane_offset for branch in plan.branches) == pytest.approx(
        tuple(rank * plan.frame.secondary.step for rank in range(len(plan.branches)))
    )
    fork = graph.stations[plan.fork_station_id]
    first_branch = graph.stations[plan.branches[0].lane_station_ids[0]]
    assert plan.frame.secondary.get(first_branch) == pytest.approx(
        plan.frame.secondary.get(fork)
    )


@pytest.mark.parametrize(
    "fixture,source_id",
    [
        ("wide_label_fan.mmd", "hub"),
        ("bypass_v_tight.mmd", "m1"),
        ("junction_entry_collision.mmd", "pre2"),
    ],
)
def test_symmetric_open_fan_straddles_centreline_only_when_requested(
    fixture: str, source_id: str
) -> None:
    """The symmetric directive gives the same fan an evenly centred frame."""
    path = ROOT / "examples" / "topologies" / fixture
    graph = parse_metro_mermaid(path.read_text())
    graph.diamond_style = "symmetric"
    try:
        compute_layout(graph, validate=True)
    except SettledRouteValidationError as exc:
        if (
            fixture != "junction_entry_collision.mmd"
            or not isinstance(exc.__cause__, FanRouteInvariantError)
            or not str(exc).endswith(
                "has a final route frame discontinuity at 's_b' on 'beta'"
            )
        ):
            raise
    plan = next(
        item for item in graph.fan_plans if item.authored_source_id == source_id
    )

    assert plan.appearance_policy is FanAppearancePolicy.SYMMETRIC
    assert plan.appearance_centreline_branch_id is None
    assert plan.frame is not None
    assert tuple(branch.lane_offset for branch in plan.branches) == pytest.approx(
        symmetric_lane_offsets(len(plan.branches), plan.frame.secondary.step)
    )


@pytest.mark.parametrize(
    ("fixture", "hub_id", "branch_ids", "axis"),
    [
        ("file_icons.mmd", "align", ("ref_in", "reads_in"), "y"),
        (
            "file_icons.mmd",
            "collect",
            ("aln_out", "report_out", "results_out"),
            "y",
        ),
        (
            "tb_file_termini.mmd",
            "report",
            ("multiqc", "bundle", "report_html"),
            "x",
        ),
    ],
)
def test_balanced_file_examples_explicitly_request_symmetric_fans(
    fixture: str,
    hub_id: str,
    branch_ids: tuple[str, ...],
    axis: str,
) -> None:
    path = ROOT / "examples" / fixture
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)

    assert graph.diamond_style == "symmetric"
    centreline = getattr(graph.stations[hub_id], axis)
    offsets = sorted(
        getattr(graph.stations[station_id], axis) - centreline
        for station_id in branch_ids
    )
    assert offsets == pytest.approx(tuple(-offset for offset in reversed(offsets)))


def test_tb_file_termini_symmetric_plan_preserves_complete_branch_bundles() -> None:
    path = ROOT / "examples" / "tb_file_termini.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(item for item in graph.fan_plans if item.authored_source_id == "report")

    assert plan.disposition is FanPlanDisposition.PLANNED
    assert plan.appearance_policy is FanAppearancePolicy.SYMMETRIC
    assert all(branch.line_ids == ("rna", "dna") for branch in plan.branches)
    assert tuple(branch.lane_offset for branch in plan.branches) == pytest.approx(
        symmetric_lane_offsets(len(plan.branches), plan.appearance_lane_pitch)
    )
    assert graph.stations["bundle"].x == pytest.approx(graph.stations["bundle_zip"].x)
    assert graph.stations["multiqc"].x == pytest.approx(
        graph.stations["multiqc_html"].x
    )


def test_runtime_guard_rejects_symmetric_straight_open_fan_plan() -> None:
    """A straight plan cannot silently realise symmetric lane geometry."""
    path = ROOT / "examples" / "topologies" / "wide_label_fan.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=False)
    plan = next(item for item in graph.fan_plans if item.authored_source_id == "hub")
    assert plan.frame is not None
    symmetric_offsets = symmetric_lane_offsets(
        len(plan.branches), plan.frame.secondary.step
    )
    bad_branches = tuple(
        replace(branch, lane_offset=offset)
        for branch, offset in zip(plan.branches, symmetric_offsets, strict=True)
    )
    with pytest.raises(
        ValueError,
        match="straight local fan must have one non-negative centreline lane",
    ):
        replace(plan, branches=bad_branches)
    object.__setattr__(
        plan,
        "branches",
        bad_branches,
    )

    with pytest.raises(
        PhaseInvariantError,
        match="straight planned fan .* does not keep its appearance frame",
    ):
        _guard_planned_fan_frame_realised(
            graph,
            "test",
            offsets=compute_station_offsets(graph),
        )


def test_runtime_guard_accepts_content_expanded_appearance_lane_pitch() -> None:
    """A fan may freeze a content-safe pitch larger than its nominal axis step."""
    path = ROOT / "examples" / "topologies" / "tb_internal_diagonal.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=False)
    plan = next(item for item in graph.fan_plans if item.authored_source_id == "hub")
    assert plan.frame is not None
    expanded_pitch = plan.frame.secondary.step + 20.0
    expanded_branches = tuple(
        replace(
            branch,
            lane_offset=rank * expanded_pitch,
            diagonal_runway=max(
                branch.diagonal_runway or 0.0,
                rank * expanded_pitch,
            ),
        )
        for rank, branch in enumerate(plan.branches)
    )
    with pytest.raises(
        ValueError,
        match="fan lane offsets disagree with appearance pitch",
    ):
        replace(plan, appearance_lane_pitch=expanded_pitch)
    expanded_plan = replace(
        plan,
        branches=expanded_branches,
        appearance_lane_pitch=expanded_pitch,
    )
    install_fan_plan_execution(
        graph,
        FanPlanExecution(
            query=FanPlanQuery.build((expanded_plan,)),
        ),
    )
    centreline = expanded_plan.frame.secondary.get(
        graph.stations[expanded_plan.fork_station_id]
    )
    _apply_planned_fan_geometry(graph, {expanded_plan.id: centreline})

    _guard_planned_fan_frame_realised(
        graph,
        "test",
        offsets=compute_station_offsets(graph),
    )


def test_same_line_open_boundary_fan_keeps_established_layout_ownership() -> None:
    path = ROOT / "examples" / "topologies" / "section_trunk_short_output_branch.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(item for item in graph.fan_plans if item.authored_source_id == "s3")

    assert plan.disposition is FanPlanDisposition.LEGACY
    assert plan.legacy_reason == "same-line-open-fan-layout-owns-geometry"
    assert plan.resolved_member_edges
    assert plan.layout_station_ids == ()
    assert (graph.stations["s3"].x, graph.stations["s3"].y) != (
        graph.stations["sink"].x,
        graph.stations["sink"].y,
    )


def test_branches_keep_line_identity_after_partial_convergence() -> None:
    facts = [
        _fact("fork", "via", "b", 0),
        _fact("fork", "merge", "a", 1),
        _fact("fork", "other", "c", 2),
        _fact("via", "merge", "b", 3),
        _fact("merge", "join", "a", 4),
        _fact("merge", "join", "b", 5),
        _fact("other", "join", "c", 6),
    ]

    graph = _graph()
    graph.diamond_style = "symmetric"
    execution = build_fan_plan_execution(
        graph,
        _Topology.direct(facts),
        x_spacing=30.0,
        y_spacing=10.0,
        minimum_runway=20.0,
    )

    plan = execution.plans[0]
    assert plan.disposition is FanPlanDisposition.PLANNED
    assert tuple(
        tuple(
            edge.line_id for path in branch.continuation_resolved_paths for edge in path
        )
        for branch in plan.branches
    ) == (("b", "b", "b"), ("a", "a"), ("c", "c"))
    assert execution.query.owner_for_authored_edge(facts[4].id) is plan
    assert execution.query.owner_for_authored_edge(facts[5].id) is plan


def test_shared_same_line_suffix_has_plan_but_no_unique_branch_owner() -> None:
    facts = [
        _fact("fork", "a", "rna", 0),
        _fact("fork", "b", "rna", 1),
        _fact("fork", "dead", "qc", 2),
        _fact("a", "merge", "rna", 3),
        _fact("b", "merge", "rna", 4),
        _fact("merge", "tail", "rna", 5),
    ]

    execution = build_fan_plan_execution(
        _graph(),
        _Topology.direct(facts),
        x_spacing=30.0,
        y_spacing=10.0,
        minimum_runway=20.0,
    )

    plan = execution.plans[0]
    shared = ResolvedEdge("merge", "tail", "rna")
    assert plan.disposition is FanPlanDisposition.PLANNED
    assert execution.query.structural_owner_for_resolved_edge(shared) is plan
    assert execution.query.structural_branch_for_resolved_edge(shared) is None
    assert plan.offset_line_order == ("rna", "qc")


def test_duplicated_bundle_fan_explicitly_preserves_incoming_line_order() -> None:
    facts = [
        _fact("fork", "a", "rna", 0),
        _fact("fork", "a", "qc", 1),
        _fact("fork", "b", "rna", 2),
        _fact("fork", "b", "qc", 3),
    ]

    plan = build_fan_plan_execution(
        _graph(),
        _Topology.direct(facts),
        x_spacing=30.0,
        y_spacing=10.0,
        minimum_runway=20.0,
    ).plans[0]

    assert plan.disposition is FanPlanDisposition.PLANNED
    assert plan.offset_line_order == ()
    assert plan.offset_carriers == ()


@pytest.mark.parametrize(
    (
        "direction",
        "primary",
        "secondary",
        "primary_sign",
        "secondary_sign",
        "lane_pitch",
    ),
    [
        ("LR", DemandAxis.X, DemandAxis.Y, 1.0, 1.0, 14.0),
        ("RL", DemandAxis.X, DemandAxis.Y, -1.0, 1.0, 14.0),
        ("TB", DemandAxis.Y, DemandAxis.X, 1.0, -1.0, 30.0),
        ("BT", DemandAxis.Y, DemandAxis.X, -1.0, 1.0, 30.0),
    ],
)
def test_fan_frame_rotates_without_changing_branch_order(
    direction: str,
    primary: DemandAxis,
    secondary: DemandAxis,
    primary_sign: float,
    secondary_sign: float,
    lane_pitch: float,
) -> None:
    facts = [_fact("fork", "a", "one", 0), _fact("fork", "b", "two", 1)]

    plan = build_fan_plan_execution(
        _graph(direction),
        _Topology.direct(facts),
        x_spacing=30.0,
        y_spacing=14.0,
        minimum_runway=20.0,
    ).plans[0]

    assert plan.frame is not None
    assert plan.frame.primary.name == primary.value
    assert plan.frame.secondary.name == secondary.value
    assert plan.frame.primary_sign == primary_sign
    assert plan.frame.secondary_sign == secondary_sign
    assert plan.frame.secondary.step == lane_pitch
    assert plan.appearance_lane_sign == 1.0
    assert tuple(branch.lane_offset for branch in plan.branches) == (
        -lane_pitch / 2,
        lane_pitch / 2,
    )


@pytest.mark.parametrize(
    ("direction", "entry_side", "expected"),
    [
        ("LR", PortSide.TOP, 1.0),
        ("LR", PortSide.BOTTOM, -1.0),
        ("RL", PortSide.TOP, 1.0),
        ("RL", PortSide.BOTTOM, -1.0),
        ("TB", PortSide.LEFT, 1.0),
        ("TB", PortSide.RIGHT, -1.0),
        ("BT", PortSide.LEFT, 1.0),
        ("BT", PortSide.RIGHT, -1.0),
    ],
)
def test_fan_appearance_opens_away_from_secondary_axis_entry(
    direction: str, entry_side: PortSide, expected: float
) -> None:
    graph = _graph(direction)
    graph.add_port(Port("entry", "section", entry_side))
    graph.register_station(Station("fork", "Fork", section_id="section"))
    graph.add_edge(Edge("entry", "fork", "line"))
    frame = AxisFrame.for_direction(direction, 30.0, 14.0)

    assert fan_appearance_lane_sign(graph, frame, "section", "fork") == expected


def test_each_fan_uses_only_its_own_secondary_axis_entry() -> None:
    graph = _graph("TB")
    graph.add_port(Port("left_entry", "section", PortSide.LEFT))
    graph.add_port(Port("right_entry", "section", PortSide.RIGHT))
    graph.register_station(Station("left_fork", "Left", section_id="section"))
    graph.register_station(Station("right_fork", "Right", section_id="section"))
    graph.add_edge(Edge("left_entry", "left_fork", "left"))
    graph.add_edge(Edge("right_entry", "right_fork", "right"))
    frame = AxisFrame.for_direction("TB", 30.0, 14.0)

    assert fan_appearance_lane_sign(graph, frame, "section", "left_fork") == 1.0
    assert fan_appearance_lane_sign(graph, frame, "section", "right_fork") == -1.0


@pytest.mark.parametrize(
    ("direction", "feeder_col", "feeder_row", "expected"),
    [
        ("LR", 1, 0, 1.0),
        ("LR", 1, 2, -1.0),
        ("RL", 1, 0, 1.0),
        ("RL", 1, 2, -1.0),
        ("TB", 0, 1, 1.0),
        ("TB", 2, 1, -1.0),
        ("BT", 0, 1, 1.0),
        ("BT", 2, 1, -1.0),
    ],
)
def test_fan_appearance_opens_away_from_its_secondary_axis_feeder(
    direction: str,
    feeder_col: int,
    feeder_row: int,
    expected: float,
) -> None:
    graph = _graph(direction)
    graph.sections["section"].grid_col = 1
    graph.sections["section"].grid_row = 1
    graph.add_section(
        Section(
            "feeder",
            "Feeder",
            grid_col=feeder_col,
            grid_row=feeder_row,
        )
    )
    graph.register_station(Station("feeder_station", "Feeder", section_id="feeder"))
    graph.register_station(Station("fork", "Fork", section_id="section"))
    graph.add_edge(Edge("feeder_station", "fork", "line"))
    frame = AxisFrame.for_direction(direction, 30.0, 14.0)

    assert fan_appearance_lane_sign(graph, frame, "section", "fork") == expected


def test_vertical_fan_pitch_keeps_same_layer_labels_clear_of_markers() -> None:
    path = ROOT / "examples" / "topologies" / "tb_internal_diagonal.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(item for item in graph.fan_plans if item.authored_source_id == "hub")
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    labels = {
        placement.station_id: placement.text
        for placement in place_labels(graph, station_offsets=offsets, routes=routes)
    }

    assert plan.frame is not None
    assert plan.appearance_lane_pitch == pytest.approx(78.0)
    assert labels["left"] == "Lane A"
    assert labels["right"] == "Lane B"


def test_lr_fed_straight_tb_fan_opens_away_from_its_feeder() -> None:
    path = ROOT / "examples" / "topologies" / "tb_internal_diagonal.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(item for item in graph.fan_plans if item.authored_source_id == "hub")

    assert plan.appearance_policy is FanAppearancePolicy.STRAIGHT
    hub_x = graph.stations["hub"].x
    assert graph.stations["left"].x == pytest.approx(hub_x)
    assert graph.stations["right"].x > hub_x


def test_runtime_guard_rejects_planned_fan_on_the_wrong_appearance_side() -> None:
    path = ROOT / "examples" / "topologies" / "tb_internal_diagonal.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(item for item in graph.fan_plans if item.authored_source_id == "hub")
    assert plan.appearance_lane_sign is not None
    bad_plan = replace(plan, appearance_lane_sign=-plan.appearance_lane_sign)
    install_fan_plan_execution(
        graph,
        FanPlanExecution(query=FanPlanQuery.build((bad_plan,))),
    )

    with pytest.raises(PhaseInvariantError, match="appearance lane sign"):
        _guard_planned_fan_frame_realised(
            graph,
            "test",
            offsets=compute_station_offsets(graph),
        )


def test_vertical_fan_pitch_clears_inward_facing_branch_label() -> None:
    path = ROOT / "examples" / "topologies" / "tb_trunk_through_fan.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(item for item in graph.fan_plans if item.authored_source_id == "hub")

    assert plan.frame is not None
    assert plan.appearance_lane_pitch == pytest.approx(74.0)


@pytest.mark.parametrize(
    ("fixture", "bad_pitch"),
    [
        ("tb_internal_diagonal.mmd", X_SPACING),
        ("tb_trunk_through_fan.mmd", X_SPACING - 2.0),
    ],
)
def test_runtime_guard_rejects_vertical_fan_pitch_under_reservation(
    fixture: str,
    bad_pitch: float,
) -> None:
    path = ROOT / "examples" / "topologies" / fixture
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(item for item in graph.fan_plans if item.authored_source_id == "hub")
    bad_offsets = fan_lane_offsets(
        tuple(branch.id for branch in plan.branches),
        bad_pitch,
        plan.appearance_centreline_branch_id,
    )
    bad_branches = tuple(
        replace(
            branch,
            lane_offset=lane_offset,
            diagonal_runway=max(branch.diagonal_runway or 0.0, abs(lane_offset)),
        )
        for branch, lane_offset in zip(plan.branches, bad_offsets, strict=True)
    )
    bad_plan = replace(
        plan,
        branches=bad_branches,
        appearance_lane_pitch=bad_pitch,
    )
    install_fan_plan_execution(
        graph,
        FanPlanExecution(
            query=FanPlanQuery.build((bad_plan,)),
        ),
    )

    with pytest.raises(PhaseInvariantError, match="under-reserves vertical label"):
        _guard_planned_fan_frame_realised(
            graph,
            "test",
            offsets=compute_station_offsets(graph),
        )


def test_resolved_port_fork_uses_its_section_direction() -> None:
    facts = [_fact("source", "a", "one", 0), _fact("source", "b", "two", 1)]
    topology = _Topology.direct(facts)
    topology.paths[facts[0].id] = (
        (
            ResolvedEdge("source", "entry_port", "one"),
            ResolvedEdge("entry_port", "a", "one"),
        ),
    )
    topology.paths[facts[1].id] = (
        (
            ResolvedEdge("source", "entry_port", "two"),
            ResolvedEdge("entry_port", "b", "two"),
        ),
    )
    graph = _graph("LR")
    graph.add_section(Section(id="vertical", name="Vertical", direction="TB"))
    for station_id in ("a", "b"):
        graph.register_station(
            Station(id=station_id, label=station_id.upper(), section_id="vertical")
        )
    graph.ports["entry_port"] = Port(
        id="entry_port",
        section_id="vertical",
        side=PortSide.TOP,
        is_entry=True,
    )

    plan = build_fan_plan_execution(
        graph,
        topology,
        x_spacing=30.0,
        y_spacing=10.0,
        minimum_runway=20.0,
    ).plans[0]

    assert plan.fork_station_id == "entry_port"
    assert plan.frame is not None
    assert plan.direction == "TB"
    assert plan.frame.primary.name == DemandAxis.Y.value
    assert plan.frame.secondary.step == 30.0
    assert tuple(branch.lane_offset for branch in plan.branches) == (0.0, 30.0)
    assert plan.appearance_centreline_branch_id == plan.branches[0].id
    assert plan.local_frame_anchor == FanCentrelineAnchor("a")


def test_common_resolved_approach_is_owned_as_one_fan_seam() -> None:
    facts = [_fact("source", "a", "one", 0), _fact("source", "b", "two", 1)]
    topology = _Topology.direct(facts)
    for fact in facts:
        topology.paths[fact.id] = (
            (
                ResolvedEdge("source", "exit_port", fact.key.line_id),
                ResolvedEdge("exit_port", "junction", fact.key.line_id),
                ResolvedEdge("junction", fact.key.target, fact.key.line_id),
            ),
        )
    graph = _graph()
    graph.ports["exit_port"] = Port(
        id="exit_port",
        section_id="section",
        side=PortSide.BOTTOM,
        is_entry=False,
    )
    graph.add_junction("junction")

    execution = build_fan_plan_execution(
        graph,
        topology,
        x_spacing=30.0,
        y_spacing=10.0,
        minimum_runway=20.0,
    )

    plan = execution.plans[0]
    assert plan.fork_station_id == "junction"
    assert len(plan.entry_seam_paths) == 2
    assert all(
        tuple((edge.source, edge.target) for edge in seam)
        == (
            ("source", "exit_port"),
            ("exit_port", "junction"),
        )
        for seam in plan.entry_seam_paths
    )
    assert len(plan.resolved_seam_edges) == 4
    assert all(edge in plan.resolved_member_edges for edge in plan.resolved_seam_edges)
    assert all(
        execution.query.structural_owner_for_resolved_edge(seam[-1]) is plan
        for seam in plan.entry_seam_paths
    )


def test_diamond_keeps_ports_handoffs_and_extra_output_in_one_plan() -> None:
    facts = [
        _fact("upstream", "fork", "trunk", 0),
        _fact("fork", "a", "one", 1),
        _fact("fork", "b", "two", 2),
        _fact("a", "join", "one", 3),
        _fact("a", "extra", "report", 4),
        _fact("b", "join", "two", 5),
        _fact("join", "downstream", "trunk", 6),
    ]
    topology = _Topology.direct(facts)
    topology.paths[facts[0].id] = (
        (
            ResolvedEdge("upstream", "entry_port", "trunk"),
            ResolvedEdge("entry_port", "fork", "trunk"),
        ),
    )
    topology.paths[facts[-1].id] = (
        (
            ResolvedEdge("join", "exit_port", "trunk"),
            ResolvedEdge("exit_port", "downstream", "trunk"),
        ),
    )
    topology.connector_bundles[facts[1].id] = BundleId("branch-bundle")
    topology.convergences = (
        SimpleNamespace(
            group=SimpleNamespace(
                id=ConvergenceId("join-handoff"),
                connector_ids=(facts[3].id,),
            )
        ),
    )
    graph = _graph()
    graph.diamond_style = "symmetric"
    graph.ports["entry_port"] = Port(
        id="entry_port",
        section_id="section",
        side=PortSide.LEFT,
        is_entry=True,
    )
    graph.ports["exit_port"] = Port(
        id="exit_port",
        section_id="section",
        side=PortSide.RIGHT,
        is_entry=False,
    )

    execution = build_fan_plan_execution(
        graph,
        topology,
        x_spacing=40.0,
        y_spacing=20.0,
        minimum_runway=30.0,
    )

    assert len(execution.plans) == 1
    plan = execution.plans[0]
    assert plan.disposition is FanPlanDisposition.PLANNED
    assert plan.join_station_id == "join"
    assert plan.entry_handoff_edge_ids == (facts[0].id,)
    assert plan.exit_handoff_edge_ids == (facts[-1].id,)
    assert plan.entry_port_ids == ("entry_port",)
    assert plan.exit_port_ids == ("exit_port",)
    assert plan.trunk_follower_ids == ("upstream", "downstream")
    assert plan.bundle_handoff_ids == (BundleId("branch-bundle"),)
    assert plan.convergence_handoff_ids == (ConvergenceId("join-handoff"),)
    first = plan.branches[0]
    assert first.continuation_edge_ids == (facts[1].id, facts[3].id)
    assert first.extra_output_edge_ids == (facts[4].id,)
    assert facts[4].id in plan.authored_edge_ids
    assert facts[0].id not in plan.authored_edge_ids
    assert execution.query.owner_for_authored_edge(facts[4].id) is plan


def test_missing_resolved_member_falls_back_as_one_complete_group() -> None:
    facts = [_fact("fork", "a", "one", 0), _fact("fork", "b", "two", 1)]
    topology = _Topology.direct(facts)
    del topology.paths[facts[1].id]

    execution = build_fan_plan_execution(
        _graph(),
        topology,
        x_spacing=30.0,
        y_spacing=10.0,
        minimum_runway=20.0,
    )

    plan = execution.plans[0]
    assert plan.disposition is FanPlanDisposition.LEGACY
    assert plan.legacy_reason == "missing-resolved-member-path"
    assert plan.frame is None
    assert plan.centreline_reference_id is None
    assert plan.centreline_anchor is None
    assert plan.demand_ids == ()
    assert all(branch.lane_offset is None for branch in plan.branches)
    assert execution.query.owner_for_authored_edge(facts[0].id) is None


def test_off_track_member_falls_back_as_one_complete_group() -> None:
    facts = [_fact("fork", "a", "one", 0), _fact("fork", "b", "two", 1)]
    graph = _graph()
    for station_id in ("fork", "a", "b"):
        graph.register_station(
            Station(
                id=station_id,
                label=station_id.upper(),
                section_id="section",
                off_track=station_id == "b",
            )
        )

    execution = build_fan_plan_execution(
        graph,
        _Topology.direct(facts),
        x_spacing=30.0,
        y_spacing=10.0,
        minimum_runway=20.0,
    )

    plan = execution.plans[0]
    assert plan.disposition is FanPlanDisposition.LEGACY
    assert plan.legacy_reason == "off-track-layout-owns-fan-geometry"
    assert plan.layout_station_ids == ()
    assert execution.query.planned_for_fork("fork") is None


def test_a_fan_that_states_no_geometry_does_not_veto_the_one_that_does() -> None:
    """Only a fan that claims a coordinate can contend for it.

    Two chained fans pass through each other's stations, but a fan that
    declines states no frame, lanes or carriers, so it holds nothing to contend
    with; and the stations both merely travel through carry no claim either.
    The downstream fan is planned, and the reason the upstream one declined is
    its own rather than the contest.
    """
    facts = [
        _fact("outer", "a", "one", 0),
        _fact("outer", "b", "two", 1),
        _fact("a", "shared", "one", 2),
        _fact("b", "shared", "two", 3),
        _fact("shared", "c", "three", 4),
        _fact("shared", "d", "four", 5),
    ]

    execution = build_fan_plan_execution(
        _graph(),
        _Topology.direct(facts),
        x_spacing=30.0,
        y_spacing=10.0,
        minimum_runway=20.0,
    )

    by_source = {plan.authored_source_id: plan for plan in execution.plans}
    assert set(by_source) == {"outer", "shared"}
    assert by_source["outer"].disposition is FanPlanDisposition.LEGACY
    assert by_source["outer"].legacy_reason == "straight-diamond-layout-owns-geometry"
    assert by_source["shared"].disposition is FanPlanDisposition.PLANNED
    assert set(by_source["outer"].owned_station_ids) & set(
        by_source["shared"].owned_station_ids
    )
    assert not claimed_station_ids(by_source["shared"]) & {"a", "b"}


def test_chained_fans_take_one_seat_each_of_a_shared_port_pair() -> None:
    """Two chained fans reaching one pair of section ports keep one seat each.

    The downstream fan lands a branch on the entry port, so it seats that port
    on the branch's lane and the upstream fan reads it.  Neither fan lands on,
    lanes or carries the exit port, so nothing states it and the fork the trunk
    reaches first keeps it.  Each port ends with one claimant, which lets both
    fans hold their frames, and a station a fan reads stays on its membership.
    """
    path = ROOT / "examples" / "topologies" / "merge_trunk_over_low_section.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    by_source = {plan.authored_source_id: plan for plan in graph.fan_plans}
    upstream = by_source["ta"]
    downstream = by_source["i2"]

    shared = {"ingest__exit_right_0", "tall__entry_left_3"}
    assert upstream.ceded_station_ids == ("tall__entry_left_3",)
    assert downstream.ceded_station_ids == ("ingest__exit_right_0",)
    assert claimed_station_ids(upstream) & shared == {"ingest__exit_right_0"}
    assert claimed_station_ids(downstream) & shared == {"tall__entry_left_3"}
    assert shared.issubset(set(downstream.owned_station_ids))
    assert upstream.disposition is FanPlanDisposition.PLANNED
    assert downstream.disposition is FanPlanDisposition.PLANNED


def test_a_landing_two_forks_reach_is_the_port_allocator_s_seat() -> None:
    """A port two forks land a branch on is the allocator's seat, not a fan's.

    ``_align_entry_ports`` gives every entry port its coordinate from the
    section frame and the runs arriving at it, and each fan would seat the port
    it lands on at the end of its own branch lane.  Letting either state it puts
    its section's content outside the box the allocator sized, so the allocator
    keeps the landing and both fans decline to it.
    """
    path = ROOT / "tests" / "fixtures" / "target_entry_runway_bypass.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    by_fork = {plan.fork_station_id: plan for plan in graph.fan_plans}
    left, right = by_fork["__junction_13"], by_fork["__junction_14"]

    assert left.legacy_reason == "shared-landing-port-allocator-owns-the-seat"
    assert right.legacy_reason == "shared-landing-port-allocator-owns-the-seat"


def test_a_packed_cell_corridor_declares_every_gap_leg_it_builds() -> None:
    """Two of a corridor's columns can share one gap, and the third still holds one.

    The handler names each column it placed by X and intended direction; where
    two of those resolve onto one leg, the leg the third built is left holding a
    gap nothing declared, which the gap allocator cannot then seat around.
    """
    path = (
        ROOT / "examples" / "topologies" / "packed_cell_right_exit_left_entry_wrap.mmd"
    )
    graph = prepare_graph(path.read_text())
    observation = observe_route_edges(
        graph, station_offsets=compute_station_offsets(graph)
    )

    assert check_gap_channels_materialized(graph, list(observation.routes)) == []


def test_line_split_readings_of_one_fork_leave_it_to_the_diamond_layout() -> None:
    """Two line groups forking at one junction are one fan the layout seats.

    Each reading carries its own line to the same landings, so neither is the
    whole fan and folding one away would drop that line's branches.  The
    diamond layout seats the stations either side of the trunk, so it keeps the
    fork and both readings decline to it.
    """
    path = ROOT / "examples" / "topologies" / "paired_input_fan_branch_tree.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    by_source = {plan.authored_source_id: plan for plan in graph.fan_plans}
    left, right = by_source["convert_a"], by_source["convert_b"]

    assert left.fork_station_id == right.fork_station_id
    assert {branch.line_ids for branch in left.branches} != {
        branch.line_ids for branch in right.branches
    }
    assert left.legacy_reason == "line-split-fork-layout-owns-geometry"
    assert right.legacy_reason == "line-split-fork-layout-owns-geometry"


def test_same_line_readings_of_one_fork_fold_into_the_earliest() -> None:
    """Feeders merging before a fork raise one fan read once per feeder.

    The readings land the same branches on the same lines, so they differ only
    in the lead each feeder takes into the fork.  One of them is the fan and
    the rest fold away, leaving their leads to route as ordinary runs.
    """
    path = ROOT / "examples" / "variantprioritization.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    at_fork = [
        plan for plan in graph.fan_plans if plan.fork_station_id == "__junction_6"
    ]

    assert len(at_fork) == 1
    assert at_fork[0].authored_source_id == "get_vep"


def test_chained_fans_leave_the_trunk_between_them_to_local_layout() -> None:
    """Two forks on one trunk both read its stations off the row layout placed.

    Neither fan states a seat the other does not, and a fan claiming the shared
    trunk drags the aligned row with it, so the local layout keeps it and both
    fans decline to that owner rather than to an unstated precedence.
    """
    path = ROOT / "examples" / "topologies" / "rowmate_tb_side_entry_top_align.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    by_fork = {plan.fork_station_id: plan for plan in graph.fan_plans}
    upstream, downstream = by_fork["umi_tools_extract"], by_fork["bbsplit"]

    assert upstream.legacy_reason == "chained-trunk-layout-owns-geometry"
    assert downstream.legacy_reason == "chained-trunk-layout-owns-geometry"
    trunk = graph.stations["fastqc_trimmed"]
    assert graph.stations["bbsplit"].y == pytest.approx(trunk.y)
    assert graph.stations[upstream.fork_station_id].y == pytest.approx(trunk.y)


def test_a_symmetric_diamond_keeps_the_anchor_its_lane_order_disagrees_with() -> None:
    """A centreline stated in two places is the diamond layout's to settle.

    The fan anchors its centreline on the exit port its trunk branch runs to,
    and the lane order seats that branch a pitch off the centreline.  Under the
    symmetric diamond the branches sit in slots balanced about the trunk, so
    taking the branch's lane as the centreline would pull them off those slots:
    the diamond layout keeps the frame.
    """
    path = ROOT / "examples" / "topologies" / "paired_input_fan_branch_tree.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(item for item in graph.fan_plans if item.fork_station_id == "pairs_in")

    assert plan.legacy_reason == "symmetric-diamond-layout-owns-the-anchor"
    trunk_y = graph.stations["stage_a1"].y
    assert graph.stations["convert_a"].y < trunk_y < graph.stations["convert_b"].y


def test_a_branch_stops_where_its_lines_end_and_another_run_carries_on() -> None:
    """A leg fed from elsewhere continues that feed's run, not this branch.

    The fan reaches CPSR on the reference line; the germline run leaving CPSR
    reaches it from the filter upstream instead, so the branch ends at the
    station rather than retagging onto a run another fan brings in.
    """
    path = ROOT / "examples" / "variantprioritization.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(
        item for item in graph.fan_plans if item.fork_station_id == "__junction_6"
    )

    assert {branch.line_ids for branch in plan.branches} == {("reference",)}
    assert not any(
        edge.source == "cpsr" and edge.target == "report_cpsr"
        for edge in plan.resolved_member_edges
    )


_SEAM_HANDOFF_SOURCE = """\
%%metro title: Seam hand-off
%%metro line: l | Line | #0570b0
%%metro line: k | Other | #e31a1c

graph LR
    subgraph src [Source]
        a[A]
        c[C]
        b[B]
        a -->|l| c
    end
    subgraph top_sec [Top]
        t[T]
        a -->|l,k| t
    end
    subgraph mid_sec [Mid]
        m[M]
        b -->|k| m
    end
    subgraph bot_sec [Bot]
        n[N]
        b -->|k| n
    end
"""
"""``a`` and ``b`` leave one section through one exit port onto one junction.

``a`` forks there, so its branch to ``t`` draws the run from the port through
the junction; ``b`` forks at the junction itself, so the same run is the entry
seam its fan reads.  ``a``'s other arm dead-ends at ``c``, which keeps the two
forks independent: neither arm of ``a`` meets ``b``'s run before the port.
"""


def test_a_fan_hands_a_seam_edge_to_the_neighbour_that_draws_it() -> None:
    """A seam edge one fan draws as a leg is handed off by the fan that reads it.

    The upstream fan carries the exit trunk on a branch, and the junction fan
    downstream reaches that same edge as its entry seam.  The fan that draws the
    edge keeps the route on it; the reader records the hand-off, holds the edge
    on its membership as a bound of its frame, and expects no emission member of
    its own there.  Both fans hold their frames.
    """
    graph = parse_metro_mermaid(_SEAM_HANDOFF_SOURCE)
    compute_layout(graph, validate=True)
    by_fork = {plan.fork_station_id: plan for plan in graph.fan_plans}
    owner, reader = by_fork["a"], by_fork["__junction_4"]

    seam = next(
        edge
        for edge in reader.resolved_member_edges
        if edge.source == "src__exit_right_0" and edge.target == "__junction_4"
    )
    assert reader.ceded_member_edges == (seam,)
    assert seam in fan_plans.drawn_member_edges(owner)
    assert seam not in fan_plans.drawn_member_edges(reader)
    assert seam not in fan_plans.claimed_member_edges(reader)
    assert seam in fan_plans.claimed_member_edges(owner)
    assert seam in reader.resolved_seam_edges
    owned = {
        expectation.edge: expectation.member_id
        for expectation in owner.route_expectations
    }
    read = {
        expectation.edge: expectation.member_id
        for expectation in reader.route_expectations
    }
    assert owned[seam] is not None
    assert read[seam] is None
    assert owned[seam] in owner.member_ids
    assert owned[seam] not in reader.member_ids
    assert owner.disposition is FanPlanDisposition.PLANNED
    assert reader.disposition is FanPlanDisposition.PLANNED


def test_a_fan_may_only_hand_off_a_seam_it_does_not_draw() -> None:
    """The hand-off is declared: a ceded edge is a seam, and it sheds its member.

    Ceding a leg would hand off geometry the fan draws itself, and keeping an
    emission member on a ceded seam would leave two fans routing one edge.  Both
    are rejected by name.
    """
    graph = parse_metro_mermaid(_SEAM_HANDOFF_SOURCE)
    compute_layout(graph, validate=True)
    reader = next(
        plan for plan in graph.fan_plans if plan.fork_station_id == "__junction_4"
    )
    leg = next(
        edge
        for edge in reader.resolved_member_edges
        if edge not in reader.resolved_seam_edges
    )

    with pytest.raises(ValueError, match="cedes an edge it draws rather than a seam"):
        replace(reader, ceded_member_edges=(*reader.ceded_member_edges, leg))

    ceded = reader.ceded_member_edges[0]
    borrowed = next(
        expectation.member_id
        for expectation in reader.route_expectations
        if expectation.member_id is not None
    )
    with pytest.raises(ValueError, match="expects a route it hands off"):
        replace(
            reader,
            route_expectations=tuple(
                replace(expectation, member_id=borrowed)
                if expectation.edge == ceded
                else expectation
                for expectation in reader.route_expectations
            ),
        )


def test_install_publishes_matching_immutable_query() -> None:
    facts = [_fact("fork", "a", "one", 0), _fact("fork", "b", "two", 1)]
    graph = _graph()
    execution = build_fan_plan_execution(
        graph,
        _Topology.direct(facts),
        x_spacing=30.0,
        y_spacing=10.0,
        minimum_runway=20.0,
    )

    install_fan_plan_execution(graph, execution)

    assert graph.fan_plans is execution.plans
    assert graph.fan_plan_query is execution.query


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("dogleg_twoline_fanout.mmd", ("to_new", "to_src")),
        ("fanout_intersection_shared_channel.mmd", ("l1", "l2")),
        ("seed72_cross_family_fan.mmd", ("through", "normal", "exempt")),
    ],
)
def test_cross_family_opening_order_is_planned_before_canvas_placement(
    fixture: str, expected: tuple[str, ...]
) -> None:
    path = ROOT / "examples" / "topologies" / fixture
    graph = parse_metro_mermaid(path.read_text())
    topology = build_route_topology_query(graph)
    assert topology is not None

    execution = build_fan_plan_execution(
        graph,
        topology,
        x_spacing=60.0,
        y_spacing=40.0,
        minimum_runway=20.0,
    )
    plan = next(
        item for item in execution.plans if item.fork_station_id in graph.junction_ids
    )

    assert (
        tuple(
            branch.line_ids[0]
            for branch in sorted(plan.branches, key=lambda item: item.opening_rank)
        )
        == expected
    )


def test_runtime_guard_rejects_planned_branch_coordinate_drift() -> None:
    path = ROOT / "examples" / "topologies" / "port_fed_three_branch_diamond.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    offsets = compute_station_offsets(graph)
    plan = next(item for item in graph.fan_plans if item.layout_station_ids)
    station_id = plan.branches[0].lane_station_ids[0]

    graph.stations[station_id].y += 5.0

    with pytest.raises(PhaseInvariantError, match="expected .* from its frame"):
        _guard_planned_fan_frame_realised(graph, "test", offsets=offsets)


def test_planned_geometry_requires_every_frozen_centreline() -> None:
    path = ROOT / "examples" / "topologies" / "wide_label_fan.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)

    with pytest.raises(
        PhaseInvariantError,
        match="has no frozen placement centreline",
    ):
        _apply_planned_fan_geometry(graph, {})


def test_planned_straight_diamond_is_invalid_at_construction() -> None:
    path = ROOT / "examples" / "topologies" / "port_fed_three_branch_diamond.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(item for item in graph.fan_plans if item.join_station_id is not None)

    with pytest.raises(
        ValueError,
        match="straight-diamond geometry requires established layout",
    ):
        replace(plan, appearance_policy=FanAppearancePolicy.STRAIGHT)


def test_runtime_guard_rejects_corrupted_straight_diamond_policy() -> None:
    path = ROOT / "examples" / "topologies" / "port_fed_three_branch_diamond.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    offsets = compute_station_offsets(graph)
    plan = next(item for item in graph.fan_plans if item.join_station_id is not None)

    object.__setattr__(
        plan,
        "appearance_policy",
        FanAppearancePolicy.STRAIGHT,
    )

    with pytest.raises(
        PhaseInvariantError,
        match="claims geometry for frozen appearance policy 'straight'",
    ):
        _guard_planned_fan_frame_realised(graph, "test", offsets=offsets)


def test_fan_appearance_policy_rejects_string_equivalents() -> None:
    path = ROOT / "examples" / "topologies" / "port_fed_three_branch_diamond.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(item for item in graph.fan_plans if item.join_station_id is not None)

    with pytest.raises(ValueError, match="appearance policy is not canonical"):
        replace(plan, appearance_policy="symmetric")

    object.__setattr__(plan, "appearance_policy", "symmetric")
    with pytest.raises(PhaseInvariantError, match="non-canonical appearance policy"):
        _guard_planned_fan_frame_realised(
            graph,
            "test",
            offsets=compute_station_offsets(graph),
        )


def test_planned_reconvergence_requires_a_resolved_join() -> None:
    path = ROOT / "examples" / "topologies" / "port_fed_three_branch_diamond.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(item for item in graph.fan_plans if item.join_station_id is not None)

    with pytest.raises(ValueError, match="reconvergence has no resolved join"):
        replace(plan, join_station_id=None)

    object.__setattr__(plan, "join_station_id", None)
    with pytest.raises(PhaseInvariantError, match="has no resolved join"):
        _guard_planned_fan_frame_realised(
            graph,
            "test",
            offsets=compute_station_offsets(graph),
        )


def test_runtime_guard_rejects_planned_handoff_offset_drift() -> None:
    path = ROOT / "examples" / "topologies" / "dogleg_twoline_fanout.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    offsets = compute_station_offsets(graph)
    plan = next(
        item for item in graph.fan_plans if item.fork_station_id in graph.junction_ids
    )
    branch = min(plan.branches, key=lambda item: item.opening_rank)
    line_id = branch.line_ids[0]

    offsets[(plan.fork_station_id, line_id)] = 8.0

    with pytest.raises(PhaseInvariantError, match="expected .* from its plan"):
        _guard_planned_fan_frame_realised(graph, "test", offsets=offsets)


def test_runtime_guard_rejects_missing_planned_carrier_offset() -> None:
    path = ROOT / "examples" / "topologies" / "dogleg_twoline_fanout.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    offsets = compute_station_offsets(graph)
    plan = next(item for item in graph.fan_plans if item.offset_carriers)
    carrier = plan.offset_carriers[0]
    del offsets[(carrier.station_id, carrier.line_ids[0])]

    with pytest.raises(PhaseInvariantError, match="has no offset"):
        _guard_planned_fan_frame_realised(graph, "test", offsets=offsets)


def test_offset_carrier_rejects_repeated_exact_slot() -> None:
    with pytest.raises(ValueError, match="repeats a slot"):
        FanOffsetCarrier(
            station_id="hub",
            assignments=(
                FanOffsetAssignment("alpha", 0),
                FanOffsetAssignment("beta", 0),
            ),
        )


def test_planned_fan_rejects_assignment_outside_its_offset_frame() -> None:
    path = ROOT / "examples" / "topologies" / "junction_entry_collision.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(item for item in graph.fan_plans if item.authored_source_id == "pre2")
    carrier = plan.offset_carriers[0]
    bad_carrier = replace(
        carrier,
        assignments=(
            replace(
                carrier.assignments[0],
                slot=len(plan.offset_line_order),
            ),
            *carrier.assignments[1:],
        ),
    )

    with pytest.raises(ValueError, match="slot lies outside its offset frame"):
        replace(plan, offset_carriers=(bad_carrier, *plan.offset_carriers[1:]))


def test_runtime_guard_rejects_legacy_offset_carriers() -> None:
    path = ROOT / "examples" / "topologies" / "tb_passthrough_continuation.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(item for item in graph.fan_plans if item.authored_source_id == "starN")
    line_id = plan.branches[0].line_ids[0]
    object.__setattr__(
        plan,
        "offset_carriers",
        (
            FanOffsetCarrier(
                station_id=plan.fork_station_id,
                assignments=(FanOffsetAssignment(line_id, 0),),
            ),
        ),
    )

    with pytest.raises(PhaseInvariantError, match="legacy fan .* owns offset carriers"):
        _guard_planned_fan_frame_realised(graph, "test", offsets={})


def test_runtime_guard_rejects_a_carrier_line_the_station_does_not_carry() -> None:
    """A carrier states a slot for every line it names, so each must stand there.

    The station may also carry lines some other owner ordered, and the carrier
    stays silent about those; naming one that is absent states a slot for
    nothing, which is a frame the emitter cannot realise.
    """
    path = ROOT / "examples" / "topologies" / "exit_turn_frame_filters.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    offsets = compute_station_offsets(graph)
    plan = next(item for item in graph.fan_plans if item.offset_carriers)
    carrier = plan.offset_carriers[0]
    absent = carrier.line_ids[0]
    graph.replace_edges(
        [
            edge
            for edge in graph.edges
            if edge.line_id != absent
            or carrier.station_id not in (edge.source, edge.target)
        ]
    )

    with pytest.raises(PhaseInvariantError, match="names a line it does not carry"):
        _guard_planned_fan_frame_realised(graph, "test", offsets=offsets)


def test_a_carrier_stays_silent_about_lines_another_owner_ordered() -> None:
    """A carrier orders the lines it names and leaves the station's others alone.

    A trunk line standing at a carrier belongs to whoever laid the trunk out;
    the fan orders the lines it does carry against one another and leaves that
    one where it stands, so a partial frame is realised rather than declined.
    """
    path = ROOT / "examples" / "topologies" / "target_lane_transition.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    offsets = compute_station_offsets(graph)
    carrier = next(
        item
        for plan in graph.fan_plans
        if plan.owns_geometry
        for item in plan.offset_carriers
        if item.station_id == "leave"
    )

    assert "local" in graph.station_lines("leave")
    assert "local" not in carrier.line_ids
    _guard_planned_fan_frame_realised(graph, "test", offsets=offsets)


def test_port_only_fan_freezes_only_structurally_shared_offset_carriers() -> None:
    path = ROOT / "examples" / "topologies" / "disjoint_sameline_trunks.mmd"
    graph = parse_metro_mermaid(path.read_text())
    graph.diamond_style = "symmetric"
    compute_layout(graph, validate=True)
    offsets = compute_station_offsets(graph)
    plan = next(
        item for item in graph.fan_plans if item.fork_station_id in graph.junction_ids
    )

    for carrier in plan.offset_carriers:
        for line_id in carrier.line_ids:
            assert (
                offsets[(carrier.station_id, line_id)]
                == offsets[(plan.fork_station_id, line_id)]
            )

    assert offsets[("secB__entry_left_4", "b")] == 0.0


def test_solo_trunk_branch_offsets_are_frozen_in_the_plan() -> None:
    path = ROOT / "examples" / "topologies" / "junction_entry_align.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(item for item in graph.fan_plans if item.authored_source_id == "pre2")
    assignments = {
        carrier.station_id: {
            assignment.line_id: assignment.slot for assignment in carrier.assignments
        }
        for carrier in plan.offset_carriers
    }

    assert {
        station_id: assignments[station_id]
        for station_id in ("s_a", "da1", "da2", "dst_a__entry_left_6")
    } == {
        "s_a": {"alpha": 0},
        "da1": {"alpha": 0},
        "da2": {"alpha": 0},
        "dst_a__entry_left_6": {"alpha": 0},
    }


def test_runtime_applies_only_frozen_fan_offset_assignments() -> None:
    path = ROOT / "examples" / "topologies" / "junction_entry_align.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    sentinel = 999.0
    initial = {
        (station_id, line_id): sentinel
        for station_id in graph.stations
        for line_id in graph.station_lines(station_id)
    }
    expected = {
        (carrier.station_id, assignment.line_id): assignment.slot * 4.0
        for plan in graph.fan_plans
        if plan.owns_geometry
        for carrier in plan.offset_carriers
        for assignment in carrier.assignments
    }

    before_mutation = SimpleNamespace(
        graph=graph,
        offsets=initial.copy(),
        offset_step=4.0,
    )
    _apply_planned_fan_offsets(before_mutation)
    assert {
        key: value
        for key, value in before_mutation.offsets.items()
        if value != sentinel
    } == expected

    graph.junction_ids.add("da1")
    after_mutation = SimpleNamespace(
        graph=graph,
        offsets=initial.copy(),
        offset_step=4.0,
    )
    _apply_planned_fan_offsets(after_mutation)
    assert after_mutation.offsets == before_mutation.offsets


def test_partial_offset_carrier_retains_absolute_slots() -> None:
    path = ROOT / "examples" / "topologies" / "junction_entry_collision.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(item for item in graph.fan_plans if item.authored_source_id == "pre2")
    carrier = next(item for item in plan.offset_carriers if item.station_id == "dbg1")

    assert {
        assignment.line_id: assignment.slot for assignment in carrier.assignments
    } == {"beta": 1, "gamma": 2}
    offsets = compute_station_offsets(graph)
    assert offsets[("dbg1", "beta")] == 4.0
    assert offsets[("dbg1", "gamma")] == 8.0


def test_planned_fan_preserves_branch_local_reversal() -> None:
    path = ROOT / "examples" / "topologies" / "near_vertical_junction_hook.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    offsets = compute_station_offsets(graph)
    plan = next(
        item for item in graph.fan_plans if item.fork_station_id in graph.junction_ids
    )

    assert "p1" not in {carrier.station_id for carrier in plan.offset_carriers}
    assert offsets[("p1", "a")] == 4.0
    assert offsets[("p1", "b")] == 0.0


def test_planned_fan_preserves_inherited_entry_frame_with_local_blocker() -> None:
    path = ROOT / "examples" / "topologies" / "exit_turn_frame_filters.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    offsets = compute_station_offsets(graph)
    plan = next(
        item for item in graph.fan_plans if item.fork_station_id in graph.junction_ids
    )

    carrier_ids = {carrier.station_id for carrier in plan.offset_carriers}
    assert carrier_ids.isdisjoint({"seam_in", "seam_out"})
    inherited = {
        line_id: offsets[("seam_start", line_id)] for line_id in ("seam_a", "seam_b")
    }
    assert {
        line_id: offsets[("seam_in", line_id)] for line_id in ("seam_a", "seam_b")
    } == inherited
    assert offsets[("seam_in", "seam_blocker")] > max(inherited.values())


def test_stacked_right_landing_route_emission_ownership_is_exact() -> None:
    path = ROOT / "examples" / "topologies" / "bottom_exit_stacked_right_entry_fan.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(item for item in graph.fan_plans if item.route_emissions)
    query = graph.fan_plan_query
    assert query is not None

    for emission in plan.route_emissions:
        binding = query.route_emission_for_resolved_edge(emission.edge)
        assert binding is not None
        owner, branch, indexed = binding
        assert owner is plan
        assert branch.id == emission.branch_id
        assert indexed is emission

    downstream = next(
        edge
        for edge in plan.resolved_member_edges
        if edge not in {item.edge for item in plan.route_emissions}
    )
    assert query.structural_owner_for_resolved_edge(downstream) is plan
    assert query.route_emission_for_resolved_edge(downstream) is None

    assignments = {
        carrier.station_id: {
            assignment.line_id: assignment.slot for assignment in carrier.assignments
        }
        for carrier in plan.offset_carriers
    }
    assert assignments == {
        "split": {"upper": 1, "lower": 0},
        "source__exit_bottom_0": {"upper": 1, "lower": 0},
        "__junction_3": {"upper": 1, "lower": 0},
        "prepare": {"upper": 1, "lower": 0},
        "lower_in": {"lower": 0},
        "lower_done": {"lower": 0},
        "lower_target__entry_right_2": {"lower": 0},
    }

    routes = route_edges(graph, station_offsets=compute_station_offsets(graph))
    tagged = {
        ResolvedEdge(route.edge.source, route.edge.target, route.line_id): route
        for route in routes
        if route.fan_plan_id is not None
    }
    assert set(tagged) == {item.edge for item in plan.route_emissions}
    assert all(route.fan_plan_id == plan.id for route in tagged.values())
    assert all(
        route.fan_route_emitter == "bottom-exit-right-landings"
        for route in tagged.values()
    )

    route = next(iter(tagged.values()))
    route.fan_route_emitter = None
    with pytest.raises(RuntimeError, match="route tag drifted"):
        validate_fan_route_emissions(graph, routes)


def test_ordinary_fan_member_geometry_is_checked_after_emission() -> None:
    path = ROOT / "examples" / "topologies" / "port_fed_three_branch_diamond.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    plan = next(item for item in graph.fan_plans if item.owns_geometry)
    assert not plan.route_emissions

    expected_edge = next(
        item.edge
        for item in plan.route_expectations
        if item.edge.source == plan.fork_station_id
    )
    route = next(
        item
        for item in routes
        if ResolvedEdge(item.edge.source, item.edge.target, item.line_id)
        == expected_edge
    )
    end_x, end_y = route.points[-1]
    route.points[-1] = end_x + 100.0, end_y + 100.0

    with pytest.raises(RuntimeError, match="final route frame discontinuity"):
        validate_fan_route_emissions(graph, routes, offsets)


def test_intra_section_fan_member_must_have_one_final_route() -> None:
    path = ROOT / "examples" / "topologies" / "port_fed_three_branch_diamond.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    plan = next(item for item in graph.fan_plans if item.owns_geometry)
    expectation = next(
        item for item in plan.route_expectations if item.member_id is None
    )
    routes = [
        route
        for route in routes
        if ResolvedEdge(route.edge.source, route.edge.target, route.line_id)
        != expectation.edge
    ]

    with pytest.raises(
        RuntimeError,
        match="in route system .* expected one final route .* found 0",
    ) as error:
        validate_fan_route_emissions(graph, routes, offsets)
    assert str(plan.id) in str(error.value)
    assert str(plan.system_id) in str(error.value)


def test_compatibility_system_does_not_validate_a_planned_child_fan() -> None:
    path = ROOT / "examples" / "topologies" / "port_fed_three_branch_diamond.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    plan = next(item for item in graph.fan_plans if item.owns_geometry)
    expectation = next(
        item for item in plan.route_expectations if item.member_id is None
    )
    routes = [
        route
        for route in routes
        if ResolvedEdge(route.edge.source, route.edge.target, route.line_id)
        != expectation.edge
    ]

    validate_fan_route_emissions(
        graph,
        routes,
        offsets,
        planned_system_ids=frozenset(),
    )


@pytest.mark.parametrize(
    ("edge", "endpoint"),
    [
        (ResolvedEdge("prepare", "split", "normal"), -1),
        (
            ResolvedEdge(
                "__junction_4",
                "normal_target__entry_left_2",
                "normal",
            ),
            -1,
        ),
    ],
    ids=("entry-handoff", "branch-landing"),
)
def test_fan_final_routes_must_meet_at_planned_boundaries(
    edge: ResolvedEdge, endpoint: int
) -> None:
    path = ROOT / "examples" / "topologies" / "seed72_cross_family_fan.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    route = next(
        item
        for item in routes
        if ResolvedEdge(item.edge.source, item.edge.target, item.line_id) == edge
    )
    x, y = route.points[endpoint]
    route.points[endpoint] = x, y + 1.0

    with pytest.raises(RuntimeError, match="planned boundary frame"):
        validate_fan_route_emissions(graph, routes, offsets)


def test_route_only_fan_hub_must_keep_its_planned_slot_frame() -> None:
    path = ROOT / "examples" / "topologies" / "seed72_cross_family_fan.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    plan = next(
        item
        for item in graph.fan_plans
        if item.owns_geometry and item.fork_station_id in graph.junction_ids
    )
    edge = next(
        expectation.edge
        for expectation in plan.route_expectations
        if expectation.edge.source == plan.fork_station_id
        and expectation.edge.line_id == "normal"
    )
    route = next(
        item
        for item in routes
        if ResolvedEdge(item.edge.source, item.edge.target, item.line_id) == edge
    )
    x, y = route.points[0]
    route.points[0] = x, y + 1.0

    with pytest.raises(RuntimeError, match="final route frame discontinuity") as error:
        validate_fan_route_emissions(graph, routes, offsets)
    assert str(plan.id) in str(error.value)
    assert str(plan.system_id) in str(error.value)


def test_route_only_fan_without_slots_stays_on_its_fork_centreline() -> None:
    path = ROOT / "examples" / "topologies" / "divergent_fanout_split.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    plan = next(
        item
        for item in graph.fan_plans
        if item.owns_geometry and item.fork_station_id in graph.junction_ids
    )
    assert not plan.offset_carriers
    edge = next(
        expectation.edge
        for expectation in plan.route_expectations
        if expectation.edge.source == plan.fork_station_id
    )
    route = next(
        item
        for item in routes
        if ResolvedEdge(item.edge.source, item.edge.target, item.line_id) == edge
    )
    x, y = route.points[0]
    route.points[0] = x, y + 1.0

    with pytest.raises(RuntimeError, match="final route frame discontinuity"):
        validate_fan_route_emissions(graph, routes, offsets)


def test_route_only_fan_without_slots_retains_external_line_offset() -> None:
    graph = prepare_graph(
        """
%%metro line: first | First | #2dd4bf
%%metro line: main | Main | #c792ea
graph LR
    subgraph source [Source]
        start[Start]
        fork[Fork]
        start -->|first,main| fork
    end
    subgraph targets [Targets]
        left[Left]
        right[Right]
    end
    fork -->|main| left
    fork -->|main| right
"""
    )
    offsets = compute_station_offsets(graph)
    plan = next(item for item in graph.fan_plans if item.owns_geometry)

    assert not plan.offset_carriers
    assert offsets[plan.fork_station_id, "main"] != 0.0
    route_edges(graph, station_offsets=offsets)


def test_route_only_fan_hub_cannot_translate_its_complete_slot_frame() -> None:
    path = ROOT / "examples" / "topologies" / "seed72_cross_family_fan.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    plan = next(
        item
        for item in graph.fan_plans
        if item.owns_geometry and item.fork_station_id in graph.junction_ids
    )
    fork_edges = {
        expectation.edge
        for expectation in plan.route_expectations
        if plan.fork_station_id in (expectation.edge.source, expectation.edge.target)
    }
    for route in routes:
        edge = ResolvedEdge(route.edge.source, route.edge.target, route.line_id)
        if edge not in fork_edges:
            continue
        endpoint = 0 if edge.source == plan.fork_station_id else -1
        x, y = route.points[endpoint]
        route.points[endpoint] = x, y + 1.0

    with pytest.raises(RuntimeError, match="drifted from its planned fork frame"):
        validate_fan_route_emissions(graph, routes, offsets)


@pytest.mark.parametrize(
    "station_id",
    ("split", "normal_target__entry_left_2"),
    ids=("entry-handoff", "branch-landing"),
)
def test_fan_boundary_frame_rejects_collective_translation(station_id: str) -> None:
    path = ROOT / "examples" / "topologies" / "seed72_cross_family_fan.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    plan = next(item for item in graph.fan_plans if item.owns_geometry)
    plan_edges = {expectation.edge for expectation in plan.route_expectations} | {
        edge
        for handoff_path in (*plan.entry_handoff_paths, *plan.exit_handoff_paths)
        for edge in handoff_path
    }
    for route in routes:
        edge = ResolvedEdge(route.edge.source, route.edge.target, route.line_id)
        if edge not in plan_edges or station_id not in (edge.source, edge.target):
            continue
        endpoint = 0 if edge.source == station_id else -1
        x, y = route.points[endpoint]
        route.points[endpoint] = x, y + 1.0

    with pytest.raises(RuntimeError, match="planned boundary frame"):
        validate_fan_route_emissions(graph, routes, offsets)


def test_stacked_right_multiline_landing_freezes_reflected_screen_order() -> None:
    path = (
        ROOT
        / "examples"
        / "topologies"
        / "bottom_exit_stacked_right_entry_multiline_branch.mmd"
    )
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(item for item in graph.fan_plans if item.route_emissions)

    assert (
        {item.edge.line_id for item in plan.route_emissions}
        == set(plan.offset_line_order)
        == {"upper_a", "upper_b", "lower"}
    )
    assert {
        branch.id: {
            item.edge.line_id
            for item in plan.route_emissions
            if item.branch_id == branch.id
        }
        for branch in plan.branches
    } == {branch.id: set(branch.line_ids) for branch in plan.branches}

    assignments = {
        carrier.station_id: {
            assignment.line_id: assignment.slot for assignment in carrier.assignments
        }
        for carrier in plan.offset_carriers
    }
    expected = {"upper_a": 1, "upper_b": 2, "lower": 0}
    assert assignments == {
        "split": expected,
        "source__exit_bottom_0": expected,
        "__junction_3": expected,
        "prepare": expected,
        "lower_in": {"lower": 0},
        "lower_done": {"lower": 0},
        "lower_target__entry_right_2": {"lower": 0},
    }


def test_centreline_port_membership_is_frozen_before_materialisation() -> None:
    path = (
        ROOT / "examples" / "topologies" / "ported_symmetric_fan_centreline_trunk.mmd"
    )
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    offsets = compute_station_offsets(graph)
    plan = next(item for item in graph.fan_plans if item.authored_source_id == "hub")

    assert plan.centreline_port_ids == (
        "fetch__entry_left_2",
        "report__entry_left_3",
        "input__exit_right_0",
        "fetch__exit_right_1",
    )
    port_id = plan.centreline_port_ids[0]
    graph.ports[port_id].side = PortSide.TOP
    graph.stations[port_id].y += 5.0

    with pytest.raises(PhaseInvariantError, match="expected .* from its frame"):
        _guard_planned_fan_frame_realised(graph, "test", offsets=offsets)


def test_absolute_centreline_anchor_is_frozen_before_materialisation() -> None:
    path = ROOT / "examples" / "topologies" / "junction_entry_collision.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(item for item in graph.fan_plans if item.authored_source_id == "pre2")

    assert plan.centreline_anchor == FanCentrelineAnchor("pre__exit_right_0")
    assert plan.local_frame_anchor == FanCentrelineAnchor("s_a")
    anchor_y = 137.0
    graph.stations[plan.centreline_anchor.station_id].y = anchor_y
    graph.stations["src__entry_left_2"].y = 263.0

    graph.ports.pop(plan.centreline_anchor.station_id)
    graph.edges.clear()
    graph.sections["pre"].grid_row += 3
    graph.sections["src"].grid_col += 2

    _apply_planned_fan_port_geometry(graph)
    centrelines = _snapshot_planned_fan_centrelines(graph)

    assert centrelines[plan.id] == anchor_y
    assert {
        graph.stations[port_id].y
        for port_id in plan.centreline_port_ids
        if port_id in graph.ports
    } == {anchor_y}


def test_diamond_branch_target_is_its_own_join() -> None:
    """A two-branch diamond whose short branch lands on the convergence node."""
    path = ROOT / "examples" / "topologies" / "bypass_v_tight.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(item for item in graph.fan_plans if item.authored_source_id == "m1")

    assert plan.authored_join_station_id == "d1"
    assert plan.join_station_id == "mid__exit_right_1"
    assert all(
        path for branch in plan.branches for path in branch.continuation_resolved_paths
    )
    assert plan.legacy_reason == "straight-diamond-layout-owns-geometry"


def test_centreline_anchor_is_complete_and_inside_fan_membership() -> None:
    path = ROOT / "examples" / "topologies" / "junction_entry_collision.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    plan = next(item for item in graph.fan_plans if item.authored_source_id == "pre2")

    with pytest.raises(ValueError, match="centreline anchor is incomplete"):
        replace(plan, centreline_anchor=None)
    with pytest.raises(ValueError, match="outside complete membership"):
        replace(plan, centreline_anchor=FanCentrelineAnchor("unrelated"))

    assert plan.centreline_anchor is not None
    graph.stations.pop(plan.centreline_anchor.station_id)
    with pytest.raises(PhaseInvariantError, match="centreline anchor .* is missing"):
        _apply_planned_fan_port_geometry(graph)


def test_symmetric_style_keeps_planned_two_way_fan_on_shared_centreline() -> None:
    path = ROOT / "examples" / "topologies" / "symmetric_deadend_fanout_exit.mmd"
    graph = parse_metro_mermaid(path.read_text())
    topology = build_route_topology_query(graph)
    assert topology is not None
    execution = build_fan_plan_execution(
        graph,
        topology,
        x_spacing=X_SPACING,
        y_spacing=compute_min_y_spacing(graph),
        minimum_runway=INTER_ROW_EDGE_CLEARANCE,
    )
    plan = next(item for item in execution.plans if item.authored_source_id == "entry")

    lane_offsets = tuple(branch.lane_offset for branch in plan.branches)
    assert lane_offsets == pytest.approx(
        (-plan.frame.secondary.step / 2, plan.frame.secondary.step / 2)
    )

    laid_out = parse_metro_mermaid(path.read_text())
    compute_layout(laid_out)
    fork = laid_out.stations["s1__entry_left_2"]
    branch_ys = [laid_out.stations[station_id].y for station_id in ("split", "salmon")]
    assert fork.y == pytest.approx(sum(branch_ys) / 2)


@pytest.mark.parametrize(
    "fixture,fork_id,helper_id,stepped_id",
    [
        ("bypass_label_rake.mmd", "align", "__bypass_quant_align_1", "quant"),
        ("bypass_label_rake_left.mmd", "align", "__bypass_quant_align_1", "quant"),
        ("bypass_label_rake_wide.mmd", "align", "__bypass_quant_align_1", "quant"),
        pytest.param(
            "bypass_v_tight.mmd",
            "m1",
            "__bypass_m2_m1_1",
            "m2",
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "issue #1863: the column seats the stopping station on the "
                    "fork's row and sends the passing line around it"
                ),
            ),
        ),
    ],
)
def test_a_bypass_helper_keeps_the_row_the_line_it_carries_runs_on(
    fixture: str,
    fork_id: str,
    helper_id: str,
    stepped_id: str,
) -> None:
    """The line with no station here runs straight; the one that stops steps off.

    A hidden bypass helper carries its line around a station that line skips, so
    the helper stands where that line already runs - the fork's row - and the
    skipped station takes a rung beside it.  Seating them the other way round
    sends the line with no business at the station around the outside of it.

    Held against the settled coordinates rather than any one owner's decision:
    which of the fan frame and the section's column ladder states the rung
    depends on whether the branches reconverge at an authored join.
    """
    path = ROOT / "examples" / "topologies" / fixture
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    fork_y = graph.stations[fork_id].y

    assert graph.stations[helper_id].y == pytest.approx(fork_y)
    assert graph.stations[stepped_id].y > fork_y


def test_runtime_guard_rejects_asymmetric_symmetric_fan_plan() -> None:
    path = ROOT / "examples" / "topologies" / "symmetric_deadend_fanout.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    plan = next(item for item in graph.fan_plans if item.authored_source_id == "entry")
    bad_branches = (
        replace(plan.branches[0], lane_offset=0.0),
        plan.branches[1],
    )
    with pytest.raises(
        ValueError,
        match="fan lane offsets disagree with appearance pitch",
    ):
        replace(plan, branches=bad_branches)
    object.__setattr__(plan, "branches", bad_branches)

    with pytest.raises(PhaseInvariantError, match="uses asymmetric lane offsets"):
        _guard_planned_fan_frame_realised(
            graph,
            "test",
            offsets=compute_station_offsets(graph),
        )


def test_planned_fan_does_not_level_unrelated_row_bbox_tops() -> None:
    path = (
        ROOT / "examples" / "topologies" / "ported_symmetric_fan_centreline_trunk.mmd"
    )
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)

    assert graph.sections["input"].bbox_y == pytest.approx(
        graph.stations["identify"].y - SECTION_Y_PADDING
    )
    assert graph.sections["report"].bbox_y == pytest.approx(
        graph.stations["generate"].y - SECTION_Y_PADDING
    )
    assert graph.sections["fetch"].bbox_y < graph.sections["input"].bbox_y


def test_planned_handoff_does_not_reslot_unrelated_same_line_stations() -> None:
    path = ROOT / "examples" / "topologies" / "compact_gap_peer_conflict.mmd"
    text = path.read_text().replace(
        "        s2[Prepare]\n",
        "        s2[Prepare]\n"
        "        peer1[Peer input]\n"
        "        peer2[Peer output]\n"
        "        peer1 -->|beta| peer2\n",
    )
    graph = parse_metro_mermaid(text)
    compute_layout(graph, validate=True)
    offsets = compute_station_offsets(graph)
    plan = next(item for item in graph.fan_plans if item.fork_station_id == "p1")

    assert {carrier.station_id for carrier in plan.offset_carriers}.isdisjoint(
        {"peer1", "peer2"}
    )
    assert offsets[("peer1", "beta")] == offsets[("peer2", "beta")] == 4.0


def test_planned_fan_resources_resolve_through_final_route_plan() -> None:
    path = ROOT / "examples" / "topologies" / "port_fed_three_branch_diamond.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    observation = observe_route_edges(
        graph, station_offsets=compute_station_offsets(graph)
    )
    query = build_route_plan_query(observation.plan)
    planned = tuple(
        item
        for item in graph.fan_plans
        if item.disposition is FanPlanDisposition.PLANNED
    )

    assert planned
    assert observation.plan.fan_plans == graph.fan_plans
    assert tuple(
        item.id
        for item in observation.plan.shared_references
        if item.kind is SharedReferenceKind.CENTRELINE
        and item.coordinate_regime is CoordinateRegime.RELATIVE_FRAME
    ) == tuple(item.centreline_reference_id for item in planned)
    assert tuple(
        item.id
        for item in observation.plan.demands
        if item.minimum_size_regime is CoordinateRegime.RELATIVE_FRAME
    ) == tuple(demand_id for item in planned for demand_id in item.demand_ids)
    for fan_plan in planned:
        assert fan_plan.system_id is not None
        assert query.fan_plan(fan_plan.id) is fan_plan
        assert fan_plan.centreline_reference_id is not None
        reference = query.shared_reference(fan_plan.centreline_reference_id)
        demands = tuple(query.demand(item) for item in fan_plan.demand_ids)
        system = next(
            item for item in observation.plan.systems if item.id == reference.system_id
        )

        assert reference.kind is SharedReferenceKind.CENTRELINE
        assert reference.coordinate_regime is CoordinateRegime.RELATIVE_FRAME
        assert reference.id in system.shared_reference_ids
        assert fan_plan.id in system.fan_plan_ids
        assert query.fan_plans_for_system(system.id) == (fan_plan,)
        assert (
            tuple(
                item.member_id
                for item in fan_plan.route_expectations
                if item.member_id is not None
            )
            == fan_plan.member_ids
        )
        assert all(
            query.fan_plans_for_member(member_id) == (fan_plan,)
            for member_id in fan_plan.member_ids
        )
        assert all(
            branch.continuation_edge_ids[0] in system.connector_ids
            for branch in fan_plan.branches
        )
        assert tuple(item.id for item in demands) == fan_plan.demand_ids
        assert all(item.system_id == system.id for item in demands)
        assert all(item.id in system.demand_ids for item in demands)
        assert all(item.kind is DemandKind.RUNWAY for item in demands)
        assert all(
            item.minimum_size_regime is CoordinateRegime.RELATIVE_FRAME
            for item in demands
        )
        assert all(item.ordered_reference_ids == (reference.id,) for item in demands)
        assert all(
            item.keep_out_classes == (KeepOutClass.SECTION, KeepOutClass.MARKER)
            for item in demands
        )


def test_legacy_fans_publish_no_relative_route_plan_resources() -> None:
    path = ROOT / "examples" / "topologies" / "tb_passthrough_continuation.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    observation = observe_route_edges(
        graph, station_offsets=compute_station_offsets(graph)
    )

    assert graph.fan_plans
    assert all(
        item.disposition is FanPlanDisposition.LEGACY for item in graph.fan_plans
    )
    assert not any(
        item.kind is SharedReferenceKind.CENTRELINE
        and item.coordinate_regime is CoordinateRegime.RELATIVE_FRAME
        for item in observation.plan.shared_references
    )
    assert not any(
        item.minimum_size_regime is CoordinateRegime.RELATIVE_FRAME
        for item in observation.plan.demands
    )


def test_legacy_fan_disposition_is_visible_in_route_plan_diagnostics() -> None:
    path = ROOT / "examples" / "topologies" / "tb_passthrough_continuation.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    observation = observe_route_edges(
        graph, station_offsets=compute_station_offsets(graph)
    )

    diagnostics = tuple(
        item for item in observation.plan.diagnostics if item.code == "fan-plan-legacy"
    )
    assert observation.plan.fan_plans == graph.fan_plans
    assert all(item.system_id is None for item in graph.fan_plans)
    assert all(not item.fan_plan_ids for item in observation.plan.systems)
    assert len(diagnostics) == 1
    assert diagnostics[0].blocking is False
    assert "local-layout-has-foreign-owner" in diagnostics[0].message


_UPSTREAM_BUNDLE = (
    ("feed", "enter", "alpha"),
    ("feed", "enter", "beta"),
    ("feed", "enter", "gamma"),
    ("enter", "leave", "local"),
)
_RETAGGED_FORK = (
    ("leave", "straight", "gamma"),
    ("leave", "lower", "alpha"),
    ("leave", "lower", "beta"),
)
_RETAGGED_FORK_REDECLARED = (
    ("leave", "lower", "alpha"),
    ("leave", "lower", "beta"),
    ("leave", "straight", "gamma"),
)
# Both adversary bundles below name the fork's lines in an order that differs
# from the answer their test asserts, so inheriting one would be visible.
_DISCONNECTED_BUNDLE = (
    ("island_in", "island_out", "gamma"),
    ("island_in", "island_out", "beta"),
    ("island_in", "island_out", "alpha"),
)
_DOWNSTREAM_BUNDLE = (
    ("straight", "sink", "gamma"),
    ("lower", "sink", "alpha"),
    ("lower", "sink", "beta"),
    ("sink", "tail", "beta"),
    ("sink", "tail", "alpha"),
    ("sink", "tail", "gamma"),
)


def _fork_offset_line_order(
    declaration: tuple[tuple[str, str, str], ...],
) -> tuple[str, ...]:
    facts = [
        _fact(source, target, line_id, rank)
        for rank, (source, target, line_id) in enumerate(declaration)
    ]
    execution = build_fan_plan_execution(
        _graph(),
        _Topology.direct(facts),
        x_spacing=30.0,
        y_spacing=10.0,
        minimum_runway=24.0,
    )
    plan = next(item for item in execution.plans if item.authored_source_id == "leave")
    return plan.offset_line_order


def test_branch_order_is_inherited_from_the_upstream_bundle() -> None:
    assert _fork_offset_line_order(_UPSTREAM_BUNDLE + _RETAGGED_FORK) == (
        "alpha",
        "beta",
        "gamma",
    )


@pytest.mark.parametrize(
    "declaration",
    (
        _UPSTREAM_BUNDLE + _RETAGGED_FORK + _DISCONNECTED_BUNDLE,
        _DISCONNECTED_BUNDLE + _UPSTREAM_BUNDLE + _RETAGGED_FORK,
        _UPSTREAM_BUNDLE + _RETAGGED_FORK_REDECLARED + _DISCONNECTED_BUNDLE,
        _DISCONNECTED_BUNDLE + _UPSTREAM_BUNDLE + _RETAGGED_FORK_REDECLARED,
    ),
)
def test_inherited_branch_order_survives_declaration_permutation(
    declaration: tuple[tuple[str, str, str], ...],
) -> None:
    assert _fork_offset_line_order(declaration) == ("alpha", "beta", "gamma")


@pytest.mark.parametrize(
    "declaration",
    (
        _RETAGGED_FORK + _DOWNSTREAM_BUNDLE,
        _DOWNSTREAM_BUNDLE + _RETAGGED_FORK,
    ),
)
def test_a_bundle_the_fork_does_not_descend_from_states_no_branch_order(
    declaration: tuple[tuple[str, str, str], ...],
) -> None:
    assert _fork_offset_line_order(declaration) == ("gamma", "alpha", "beta")


def _foreign_corridor_graph(*, foreign_feed_first: bool) -> MetroGraph:
    graph = MetroGraph()
    for section_id, members in (("clean", ("c1", "c2")), ("mixed", ("m1", "m2"))):
        graph.add_section(Section(id=section_id, name=section_id, direction="LR"))
        for member in members:
            graph.register_station(
                Station(id=member, label=member, section_id=section_id)
            )
    graph.add_junction("__junction_1")
    feeds = [Edge("m2", "__junction_1", "beta"), Edge("c2", "__junction_1", "alpha")]
    if not foreign_feed_first:
        feeds.reverse()
    for edge in (
        Edge("c1", "c2", "alpha"),
        Edge("m1", "m2", "beta"),
        Edge("m1", "m2", "outsider"),
        *feeds,
    ):
        graph.add_edge(edge)
    return graph


@pytest.mark.parametrize("foreign_feed_first", (True, False))
def test_junction_rides_a_foreign_corridor_when_any_feeder_carries_one(
    foreign_feed_first: bool,
) -> None:
    graph = _foreign_corridor_graph(foreign_feed_first=foreign_feed_first)

    assert fan_plans._rides_foreign_line_corridor(
        graph, "__junction_1", frozenset({"alpha", "beta"})
    )


def test_unchained_boundary_peers_read_the_seat_from_the_earlier_fork(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two fans that only bound one station settle it without a chain between them.

    Neither fan lanes, centres, carries or lands on ``extra__exit_right_3``, and
    neither fork lies on the other's membership, so no topological order says
    which of them should hold the seat.  Both are reading a coordinate the
    section allocator settled, so one holder is as good as the other and the
    choice only has to be total: the earlier fork keeps it and the other reads
    it, independent of the order the plans were built in.
    """
    seen: list[tuple[tuple[fan_plans.FanPlan, ...], dict[str, int], dict]] = []
    cede = fan_plans._cede_read_claims

    def record(plans, ranks):  # type: ignore[no-untyped-def]
        result = cede(plans, ranks)
        seen.append((plans, ranks, result))
        return result

    monkeypatch.setattr(fan_plans, "_cede_read_claims", record)
    path = ROOT / "examples" / "topologies" / "merge_trunk_out_of_range_section.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)

    candidates, ranks, read_from = seen[-1]
    by_fork = {plan.fork_station_id: plan for plan in candidates}
    far, near = by_fork["__junction_8"], by_fork["__junction_9"]
    seat = "extra__exit_right_3"

    assert seat in claimed_station_ids(far) & claimed_station_ids(near)
    assert seat not in stated_station_ids(far) | stated_station_ids(near)
    assert far.fork_station_id not in near.owned_station_ids
    assert near.fork_station_id not in far.owned_station_ids
    assert (ranks.get(far.fork_station_id), str(far.id)) < (
        ranks.get(near.fork_station_id),
        str(near.id),
    )
    assert read_from[near.id][seat] == far.id
    assert seat not in read_from.get(far.id, {})


_GAP_CROSSING_FAN = """\
%%metro title: Bottom-exit Fan Climbing an Inter-column Gap
%%metro line: upper | Upper branch | #e63946
%%metro line: lower | Lower branch | #0570b0
%%metro line: side | Side branch | #2a9d8f
%%metro grid: source | 0,1
%%metro grid: upper_target | 0,0
%%metro grid: lower_target | 0,2
%%metro grid: feeder | 0,3
%%metro grid: aux | 1,3

graph LR
    subgraph source [Vertical source]
        %%metro direction: TB
        %%metro exit: bottom | upper, lower
        prepare[Prepare]
        split[Split]
        prepare -->|upper,lower| split
    end

    subgraph upper_target [Upper target]
        %%metro direction: RL
        %%metro entry: right | upper
        upper_in[Upper in]
        upper_done[Upper done]
        upper_in -->|upper| upper_done
    end

    subgraph lower_target [Lower target]
        %%metro direction: RL
        %%metro entry: right | lower
        lower_in[Lower in]
        lower_done[Lower done]
        lower_in -->|lower| lower_done
    end

    subgraph feeder [Feeder]
        %%metro direction: LR
        %%metro exit: right | side
        feed_a[Feed a]
        feed_b[Feed b]
        feed_a -->|side| feed_b
    end

    subgraph aux [Aux]
        %%metro direction: LR
        %%metro entry: left | side
        aux_in[Aux in]
        aux_done[Aux done]
        aux_in -->|side| aux_done
    end

    split -->|upper| upper_in
    split -->|lower| lower_in
    feed_b -->|side| aux_in
"""
"""A planned fan whose branches climb and drop through one inter-column gap.

The ``feeder`` / ``aux`` pair exists only to open a gap between grid columns 0
and 1; the fan drops out of ``source`` and reaches its two RIGHT landings by
running back up that gap for ``upper`` and on down it for ``lower``.
"""


def test_planned_fan_declares_a_gap_slot_for_each_leg_it_runs_in_a_gap() -> None:
    """A planned fan states the gap columns its frozen branches occupy.

    The fan settles both branch legs at once, so which of them lands in a gap,
    and which way each runs, are facts of the geometry it just built rather than
    of any single leg the emitter could name up front.  Every leg that lands in
    one carries a slot for it, which is what lets the gap seat its movable
    bundles clear of a column no later pass can move.
    """
    graph = prepare_graph(_GAP_CROSSING_FAN)
    observation = observe_route_edges(
        graph, station_offsets=compute_station_offsets(graph)
    )
    lookup = gap_lookup_geometry(graph)
    routes = [route for route in observation.routes if route.fan_route_emitter]

    assert {route.line_id for route in routes} == {"upper", "lower"}
    occupied = {
        (route.line_id, match[0], Direction.D if down else Direction.U)
        for route in routes
        for _rank, x, y_lo, y_hi, down in iter_vertical_segments(route)
        if (match := gap_lo_for_x(graph, x, y_lo, y_hi, lookup=lookup)) is not None
    }
    declared = {
        (route.line_id, slot.gap_lo_col, slot.direction)
        for route in routes
        for slot in route.gap_slots
    }

    assert ("upper", 0, Direction.U) in occupied
    assert ("lower", 0, Direction.D) in occupied
    assert declared == occupied


def test_an_undeclared_fan_gap_leg_is_named_by_the_gap_channel_check() -> None:
    """The fan's climb through the gap is clean because it declared it.

    Replaying the same waypoints without the declaration is the whole
    difference: the check that every in-gap channel is materialized names the
    leg, so the slot is what answers it rather than any exemption.
    """
    graph = prepare_graph(_GAP_CROSSING_FAN)
    observation = observe_route_edges(
        graph, station_offsets=compute_station_offsets(graph)
    )
    climb = next(
        route
        for route in observation.routes
        if route.fan_route_emitter and route.line_id == "upper"
    )
    replay = RoutedPath(
        climb.edge, climb.line_id, list(climb.points), is_inter_section=True
    )

    undeclared = check_gap_channels_materialized(graph, [replay])
    assert [violation.message() for violation in undeclared] == [
        "undeclared gap channel: line 'upper' "
        "(__junction_5->upper_target__entry_right_2) runs up at x=214.0 "
        "in gap (cols 0,1) with no declared GapSlot"
    ]

    replay.gap_slots = list(climb.gap_slots)
    assert check_gap_channels_materialized(graph, [replay]) == []


def test_symmetric_fan_whose_trunk_reaches_a_terminus_seats_it_on_the_centreline() -> (
    None
):
    """A leg carrying the whole bundle to its last stop keeps the trunk's row."""
    path = ROOT / "examples" / "topologies" / "fanout_hub_two_line_trunk.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    plan = next(item for item in graph.fan_plans if item.fork_station_id == "hub")
    terminus = next(
        branch for branch in plan.branches if branch.lane_station_ids == ("out_main",)
    )

    assert graph.diamond_style == "symmetric"
    assert plan.disposition is FanPlanDisposition.PLANNED
    assert plan.appearance_policy is FanAppearancePolicy.STRAIGHT
    assert plan.appearance_centreline_branch_id == terminus.id
    assert terminus.lane_offset == 0.0
    assert graph.stations["out_main"].y == pytest.approx(graph.stations["hub"].y)


def test_branches_leaving_the_fork_together_share_one_lane_seat() -> None:
    """One station on two branches' runs takes one row, stated once."""
    path = ROOT / "examples" / "topologies" / "multirow_source_stacked_fan.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    plan = next(item for item in graph.fan_plans if item.fork_station_id == "star")
    peeled = tuple(branch for branch in plan.branches if branch.lane_offset != 0.0)

    assert plan.disposition is FanPlanDisposition.PLANNED
    assert len(peeled) == 5
    assert {branch.root_station_id for branch in peeled} == {"__bypass_cram_star_1"}
    assert {branch.lane_offset for branch in peeled} == {plan.appearance_lane_pitch}
    assert [branch.lane_station_ids for branch in peeled].count(
        ("__bypass_cram_star_1",)
    ) == 1
    assert (
        sum(
            "__bypass_cram_star_1" in branch.lane_station_ids
            for branch in plan.branches
        )
        == 1
    )


def test_peel_only_boundary_port_is_left_to_the_lane_it_carries() -> None:
    """The exit port a fan only peels through does not join its centreline."""
    path = ROOT / "examples" / "topologies" / "multirow_source_stacked_fan.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    plan = next(item for item in graph.fan_plans if item.fork_station_id == "star")

    assert "align_sec__exit_right_0" not in plan.centreline_port_ids
    assert graph.stations["align_sec__exit_right_0"].y == pytest.approx(
        graph.stations["__bypass_cram_star_1"].y
    )
    assert graph.stations["align_sec__exit_right_0"].y != pytest.approx(
        graph.stations["star"].y
    )


def test_lane_seat_keys_number_seats_in_landing_order() -> None:
    """Shared seats collapse and the nearer landing keeps the nearer lane."""
    branches = (
        SimpleNamespace(root_station_id="trunk", landing_rank=2),
        SimpleNamespace(root_station_id="peel", landing_rank=1),
        SimpleNamespace(root_station_id="peel", landing_rank=0),
    )

    assert fan_lane_seat_keys(branches) == (2, 0, 0)
    assert fan_lane_offsets(("a", "b", "c"), 10.0, "a", (2, 0, 0)) == (0.0, 10.0, 10.0)
    assert fan_lane_offsets(("a", "b", "c"), 10.0, "a") == (0.0, 10.0, 20.0)
    with pytest.raises(ValueError, match="shares a lane seat"):
        fan_lane_offsets(("a", "b"), 10.0, "a", (0, 0))
    with pytest.raises(ValueError, match="do not cover every branch"):
        fan_lane_offsets(("a", "b"), 10.0, "a", (0,))
