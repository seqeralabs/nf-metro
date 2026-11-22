"""The cross-column perpendicular-bridge exemption from the render-curve checks.

A TOP/BOTTOM entry port whose feeder descends outside the section's own box
holds the section's column, and routing reaches it with a best-effort L-shaped
lead-in.  ``graph._cross_column_perp_bridge_ports`` records those ports, and
the render-time curve checks downgrade a failure to a warning only when every
route it names is a lead-in into one of them.

Two properties hold the exemption to that one connection:

* the record describes the layout being drawn: every recorded port really is
  fed from outside its section's final box;
* a failure anywhere else in the map aborts the render.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import types
import typing
import warnings
from pathlib import Path

import pytest

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result, render_string
from nf_metro.layout import engine
from nf_metro.layout.phases.ports import _drop_x_within_section, _feeder_descent_x
from nf_metro.layout.routing import compute_station_offsets, invariants
from nf_metro.layout.routing.common import Direction
from nf_metro.layout.routing.invariants import (
    BundleOrderViolation,
    CurveInvariantError,
    Side,
    assert_render_curve_invariants,
)
from nf_metro.layout.routing.perp_columns import deal_perp_entry_column
from nf_metro.parser.model import MetroGraph
from nf_metro.themes import resolve_theme

ROOT = Path(__file__).resolve().parent.parent
REGRESSIONS = ROOT / "tests" / "fixtures" / "regressions"
TOPOLOGIES = ROOT / "examples" / "topologies"

BRIDGED_ENTRY = """\
%%metro line: a | A | #e6194b
%%metro line: b | B | #3cb44b
%%metro grid: z0 | 0,0
%%metro grid: z1 | 0,1
%%metro grid: z2 | 0,2

graph LR
    subgraph z0 [Zone 0]
        p0[P0]
        p1[P1]
        p2[P2]
        p0 -->|a| p1
        p1 -->|a| p2
    end
    subgraph z1 [Zone 1]
        %%metro exit: bottom | b
        r0[R0]
        r1[R1]
        r0 -->|b| r1
    end
    subgraph z2 [Zone 2]
        %%metro entry: top | a, b
        q0[Q0]
        q1[Q1]
        q0 -->|a,b| q1
    end
    p2 -->|a| q0
    r1 -->|b| q0
"""
BRIDGED_PORT = "z2__entry_top_2"

DECLINED_COLUMN = """\
%%metro line: l1 | One | #3cb44b
%%metro line: l2 | Two | #4363d8
%%metro line: l3 | Three | #f58231
%%metro line: l4 | Four | #911eb4
%%metro grid: sec3 | 0,3
%%metro grid: sec4 | 0,4

graph LR
    subgraph sec2 [Top]
        s2n2[polish]
        s2n3[quant_merge]
        s2n2 -->|l4| s2n3
    end
    subgraph sec3 [Middle]
        s3n0[bin_index]
        s3n2[polish]
    end
    subgraph sec4 [Bottom]
        %%metro entry: bottom | l2, l3, l4
        s4n0[pca_annotate]
    end
    s2n3 -->|l1,l2| s3n0
    s3n2 -->|l3| s4n0
    s2n3 -->|l4| s4n0
    s2n3 -->|l2| s4n0
"""

# Call 1's TOP entry is fed from a descent just past its box as Stage 3.2 sees
# it; the perpendicular-entry runway shift then widens the box over that
# descent.
WIDENED_OVER_THE_DESCENT = """\
%%metro line: l0 | Sort | #e6194b
%%metro line: l1 | Merge | #3cb44b
%%metro line: l2 | Export_Filter_Quant | #4363d8
%%metro grid: sec1 | 0,1

graph LR
    subgraph sec0 [Export_Pca 0]
        %%metro direction: TB
        %%metro exit: right | l0, l1
        s0n1 -->|l0| s0n2
        s0n2 -->|l0,l1| s0n3
    end
    subgraph sec1 [Call 1]
        s1n0 -->|l1,l2| s1n1
    end
    s0n1 -->|l0| s1n1
    s0n3 -->|l1| s1n0
"""
WIDENED_PORT = "sec1__entry_top_1"

INLINE_MAPS = {
    "bridged_entry": BRIDGED_ENTRY,
    "declined_column": DECLINED_COLUMN,
    "widened_over_the_descent": WIDENED_OVER_THE_DESCENT,
}

RECORDING_FIXTURES = [
    TOPOLOGIES / "bottom_entry_two_feeders_own_lanes.mmd",
    TOPOLOGIES / "cross_column_perp_drop.mmd",
    TOPOLOGIES / "left_exit_fan_perp_entry_landing.mmd",
    TOPOLOGIES / "lr_perp_top_entry_bottom_exit.mmd",
    TOPOLOGIES / "orbit_perp_exit_turning_entry.mmd",
    REGRESSIONS / "cross_column_perp_entry_overflow.mmd",
    REGRESSIONS / "perp_entry_column_feeder_conflict.mmd",
    REGRESSIONS / "perp_entry_column_opposite_side_fan.mmd",
    REGRESSIONS / "perp_entry_column_straight_drop.mmd",
    REGRESSIONS / "perp_exit_single_feeder_landing.mmd",
]


BRIDGED_MAPS = [
    REGRESSIONS / "cross_column_perp_entry_overflow.mmd",
    "bridged_entry",
]


def _source(case: Path | str) -> tuple[str, str]:
    if isinstance(case, Path):
        return case.read_text(), str(case.parent)
    return INLINE_MAPS[case], str(ROOT)


def _case_id(case: Path | str) -> str:
    return case.stem if isinstance(case, Path) else case


def _laid_out(case: Path | str) -> MetroGraph:
    text, source_dir = _source(case)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return prepare_graph(text, source_dir=source_dir)


def _feeders_descending_outside(graph: MetroGraph, port_id: str) -> list[str]:
    section = graph.sections[graph.ports[port_id].section_id]
    edges = graph.edges_to(port_id)
    outside = []
    for edge in edges:
        source = graph.station_for_edge_source(edge)
        if not (source.is_port or edge.source in graph.junction_ids):
            continue
        bundle = len({e.line_id for e in edges if e.source == edge.source})
        descent_x = _feeder_descent_x(graph, edge, source.x, bundle)
        if not _drop_x_within_section(section, descent_x):
            outside.append(edge.source)
    return outside


@pytest.mark.parametrize("case", [*RECORDING_FIXTURES, *INLINE_MAPS], ids=_case_id)
def test_bridge_record_describes_the_settled_layout(case: Path | str) -> None:
    graph = _laid_out(case)
    stale = [
        port_id
        for port_id in sorted(graph._cross_column_perp_bridge_ports)
        if not _feeders_descending_outside(graph, port_id)
    ]
    assert not stale, stale


def test_a_box_widened_over_the_descent_drops_its_bridge(monkeypatch) -> None:
    """The port is recorded while its feeder descends past the box, then dropped.

    Without the settled-layout prune the record keeps the port Stage 3.2 saw
    as bridged, so this map holds the prune to a port it really recorded.
    """
    with monkeypatch.context() as unpatched_prune:
        unpatched_prune.setattr(
            engine, "prune_settled_cross_column_perp_bridges", lambda graph: None
        )
        unpruned = _laid_out("widened_over_the_descent")
    assert WIDENED_PORT in unpruned._cross_column_perp_bridge_ports

    graph = _laid_out("widened_over_the_descent")
    assert not _feeders_descending_outside(graph, WIDENED_PORT)
    assert WIDENED_PORT not in graph._cross_column_perp_bridge_ports


def test_every_layout_pass_ends_with_a_record_of_its_own_boxes(monkeypatch) -> None:
    """The record matches each pass's boxes, not only the settled layout's.

    The bypass pass scores candidate layouts with the render-curve check, which
    reads the record before layout settles.
    """
    stale_after_pass: list[list[str]] = []
    run_pass = engine._layout_once

    def checked_pass(graph: MetroGraph, *args, **kwargs) -> None:
        run_pass(graph, *args, **kwargs)
        stale_after_pass.append(
            [
                port_id
                for port_id in sorted(graph._cross_column_perp_bridge_ports)
                if not _feeders_descending_outside(graph, port_id)
            ]
        )

    monkeypatch.setattr(engine, "_layout_once", checked_pass)
    _laid_out("widened_over_the_descent")
    assert stale_after_pass
    assert not any(stale_after_pass), stale_after_pass


def _inject_bundle_flip(monkeypatch, source: str, target: str) -> None:
    flip = BundleOrderViolation(
        source, target, "a", "b", (0.0, 0.0), Direction.D, Direction.R,
        Side.LEFT, Side.RIGHT,
    )  # fmt: skip
    monkeypatch.setattr(
        invariants, "check_bundle_order_preserved", lambda routes: [flip]
    )


def test_failure_on_the_bridged_lead_in_warns(monkeypatch) -> None:
    graph = _laid_out("bridged_entry")
    feeder = graph.edges_to(BRIDGED_PORT)[0].source
    _inject_bundle_flip(monkeypatch, feeder, BRIDGED_PORT)
    with pytest.warns(UserWarning, match="bridged across grid columns"):
        assert_render_curve_invariants(graph, [], {})


@pytest.mark.parametrize(
    ("source", "target"),
    [("p0", "p1"), (BRIDGED_PORT, "q0")],
    ids=["elsewhere_in_the_map", "past_the_bridged_port"],
)
def test_failure_off_the_bridged_lead_in_aborts(
    monkeypatch, source: str, target: str
) -> None:
    graph = _laid_out("bridged_entry")
    assert BRIDGED_PORT in graph._cross_column_perp_bridge_ports
    _inject_bundle_flip(monkeypatch, source, target)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(CurveInvariantError):
            assert_render_curve_invariants(graph, [], {})


@pytest.mark.parametrize(
    "case",
    BRIDGED_MAPS,
    ids=lambda case: _case_id(case) if isinstance(case, Path | str) else None,
)
def test_bridged_maps_render_without_a_curve_abort(case: Path | str) -> None:
    text, source_dir = _source(case)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        render_string(text, source_dir=source_dir)


@pytest.mark.parametrize(
    ("case", "port_id", "high_x_line"),
    [
        (
            REGRESSIONS / "cross_column_perp_entry_overflow.mmd",
            "reporting__entry_top_7",
            "germline",
        ),
        ("bridged_entry", BRIDGED_PORT, "a"),
    ],
    ids=["merge_fed", "collapsed_approach_fan"],
)
def test_bridged_port_lands_each_line_on_its_dealt_channel(
    case: Path | str, port_id: str, high_x_line: str
) -> None:
    """The bridged port's column is dealt from its trunk, and every feeder lands on it.

    The trunk's topmost lane takes the high-X channel, so the drop nests through
    its turn toward larger X, and no two lines share a descent into the port.
    """
    graph = _laid_out(case)
    offsets = compute_station_offsets(graph)
    column = deal_perp_entry_column(graph, port_id, offsets)
    assert column is not None and column.offsets is not None
    assert max(column.offsets, key=lambda item: item[1])[0] == high_x_line

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = render_graph_result(
            graph, resolve_theme(None, graph, mode=None), RenderConfig()
        )
    routes = list(zip(result.plan.routes, result.plan.route_polylines))
    departures = {
        route.line_id: round(points[0][0], 1)
        for route, points in routes
        if route.edge.source == port_id
    }
    landings = {
        (route.edge.source, route.line_id): round(points[-1][0], 1)
        for route, points in routes
        if route.edge.target == port_id
    }
    assert len(set(departures.values())) == len(departures)
    assert max(departures, key=departures.__getitem__) == high_x_line
    assert landings
    assert all(
        x == departures[line_id] for (_source, line_id), x in landings.items()
    ), (landings, departures)


EDGELESS_VIOLATIONS = {
    "FanOpeningGeometryViolation",
    "FusedCotravellingLanes",
    "LooseDestinationTail",
    "LooseSameDestinationFinalVertical",
    "MergeBranchHang",
    "OpposingEntryConfluenceOrderViolation",
    "PackedCellHandoffDivergence",
    "PeeloffBundleCrossing",
}


def _render_curve_violation_classes() -> set[type]:
    tree = ast.parse(inspect.getsource(invariants.render_curve_violations))
    checks = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id.startswith("check_")
    }
    classes: set[type] = set()
    for name in checks:
        (item,) = typing.get_args(
            typing.get_type_hints(getattr(invariants, name))["return"]
        )
        is_union = isinstance(item, types.UnionType)
        classes.update(typing.get_args(item) if is_union else (item,))
    return classes


def _sample(cls: type) -> object:
    sample = object.__new__(cls)
    for field in dataclasses.fields(cls):
        value = ("a", "b") if field.name in invariants._EDGE_FIELDS else "a"
        object.__setattr__(sample, field.name, value)
    return sample


def test_every_render_curve_violation_names_an_edge_or_is_listed_edgeless() -> None:
    """A violation naming no edge can never be exempted on a bridged lead-in.

    Every class a render-time curve check returns either names the edges the
    exemption matches on, or is listed here as edge-less, so a new check whose
    violations carry their edge under another name fails here.
    """
    classes = _render_curve_violation_classes()
    assert classes
    unnamed = sorted(
        cls.__name__
        for cls in classes
        if not invariants._violation_edges(_sample(cls))
        and cls.__name__ not in EDGELESS_VIOLATIONS
    )
    assert not unnamed, unnamed
    assert EDGELESS_VIOLATIONS <= {cls.__name__ for cls in classes}
