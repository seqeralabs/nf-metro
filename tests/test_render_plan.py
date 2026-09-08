"""Tests for building and reusing immutable render plans."""

from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from nf_metro.api import prepare_graph, resolve_theme
from nf_metro.parser.model import MetroGraph
from nf_metro.render.html import emit_render_plan_html
from nf_metro.render.manifest import read_manifest
from nf_metro.render.plan import (
    RenderPlan,
    contains_mutable_model_reference,
    thaw_render_value,
)
from nf_metro.render.svg import (
    _copy_graph_for_render,
    build_render_plan,
    emit_render_plan,
    render_svg,
)

ROOT = Path(__file__).parents[1]


def _plan(path: str) -> tuple[MetroGraph, RenderPlan]:
    source = ROOT / path
    graph = prepare_graph(source.read_text(), source_dir=str(source.parent))
    theme = resolve_theme(None, graph)
    return graph, build_render_plan(graph, theme)


@pytest.mark.parametrize(
    ("path", "station_count", "route_count", "dimensions"),
    [
        ("examples/guide/01_minimal.mmd", 6, 7, (538, 336)),
        ("examples/topologies/divergent_fanout_split.mmd", 9, 8, (640, 422)),
        ("examples/guide/03b_fan_in_merge.mmd", 18, 24, (880, 326)),
        ("examples/topologies/fold_double.mmd", 55, 108, (1537, 716)),
        (
            "examples/topologies/lr_perp_top_entry_bottom_exit.mmd",
            9,
            8,
            (373, 628),
        ),
        ("examples/rail_mode.mmd", 11, 32, (531, 965)),
        ("examples/file_icons.mmd", 9, 8, (540, 419)),
    ],
)
def test_representative_plan_geometry_snapshot(
    path: str,
    station_count: int,
    route_count: int,
    dimensions: tuple[int, int],
) -> None:
    _, plan = _plan(path)

    assert len(plan.graph.stations) == station_count
    assert len(plan.routes) == route_count
    assert (plan.svg_width, plan.svg_height) == dimensions
    assert all(isinstance(route.points, tuple) for route in plan.routes)


def test_render_plan_is_deeply_immutable_and_has_no_mutable_models() -> None:
    _, plan = _plan("examples/rail_mode.mmd")

    assert not contains_mutable_model_reference(plan)
    with pytest.raises(FrozenInstanceError):
        plan.svg_width = 1  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        plan.theme.line_width = 1  # type: ignore[misc]
    with pytest.raises(TypeError):
        plan.graph.stations["new"] = object()  # type: ignore[index]


def test_render_copy_shares_only_frozen_route_metadata() -> None:
    source = ROOT / "examples" / "guide" / "03b_fan_in_merge.mmd"
    graph = prepare_graph(source.read_text(), source_dir=str(source.parent))

    copied = _copy_graph_for_render(graph)

    assert copied is not graph
    assert copied.stations is not graph.stations
    assert copied.route_topology is graph.route_topology
    assert copied.route_resolution is graph.route_resolution


def test_repeated_plan_emission_is_byte_identical() -> None:
    _graph, plan = _plan("examples/topologies/fold_double.mmd")
    before = repr(plan)

    first = emit_render_plan(plan)
    second = emit_render_plan(plan)

    assert first == second
    assert repr(plan) == before


def test_bridge_gaps_are_settled_in_plan() -> None:
    _, plan = _plan("examples/topologies/self_crossing_bridge.mmd")

    assert any(plan.bridge_breaks)
    assert len(plan.bridge_breaks) == len(plan.edge_routes)


def test_repeated_html_emission_is_byte_identical() -> None:
    _graph, plan = _plan("examples/guide/01_minimal.mmd")

    assert emit_render_plan_html(plan) == emit_render_plan_html(plan)


@pytest.mark.parametrize(
    "path",
    [
        "examples/guide/05c_files_icon.mmd",
        "examples/cross_track_interchange.mmd",
        "examples/guide/03b_fan_in_merge.mmd",
    ],
)
def test_route_metadata_is_excluded_from_render_artifacts(path: str) -> None:
    _graph, plan = _plan(path)

    assert not hasattr(plan.graph, "layout_provenance")
    assert not hasattr(plan.graph, "route_topology")
    assert not hasattr(plan.graph, "route_resolution")
    assert not hasattr(plan.graph, "_route_topology_query")
    assert not hasattr(plan.graph, "fan_plan_execution")
    assert hasattr(plan.graph, "_linear_entry_pill_lines_cache")
    excluded_route_fields = {
        "exit_turn_plan_id",
        "exit_turn_member_id",
        "exit_turn_family_id",
        "exit_turn_axis_id",
        "exit_turn_segment_rank",
        "exit_lane_transition_plan_id",
        "fan_plan_id",
        "fan_route_emitter",
    }
    assert all(
        not hasattr(route, field)
        for route in plan.routes
        for field in excluded_route_fields
    )


def test_rendering_does_not_mutate_prepared_graph() -> None:
    source = ROOT / "examples" / "sarek_metro.mmd"
    graph = prepare_graph(source.read_text(), source_dir=str(source.parent))
    theme = resolve_theme(None, graph)
    before = copy.deepcopy(graph)

    render_svg(graph, theme)

    assert graph == before


def test_manifest_geometry_is_frozen_in_plan() -> None:
    graph, plan = _plan("examples/guide/01_minimal.mmd")
    expected = thaw_render_value(plan.manifest)
    assert expected is not None

    graph.stations["fastqc"].x += 1000
    emitted = read_manifest(emit_render_plan(plan))

    assert emitted == expected
    nodes = {node["id"]: node for node in emitted["nodes"]}
    assert nodes["fastqc"]["x"] == plan.graph.stations["fastqc"].x
