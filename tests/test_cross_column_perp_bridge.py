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

import warnings
from pathlib import Path

import pytest

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result, render_string
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

INLINE_MAPS = {"bridged_entry": BRIDGED_ENTRY, "declined_column": DECLINED_COLUMN}

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


def _known_abort(case: Path | str, *issues: str) -> object:
    return pytest.param(
        case,
        marks=pytest.mark.xfail(
            raises=CurveInvariantError, strict=True, reason=", ".join(issues)
        ),
    )


BRIDGED_MAPS = [
    REGRESSIONS / "cross_column_perp_entry_overflow.mmd",
    "bridged_entry",
    _known_abort(
        REGRESSIONS / "perp_entry_column_feeder_conflict.mmd",
        "#2131",
        "#2130",
        "deferred offset on __bypass_s2n4_s2n3_6 untracked",
    ),
    _known_abort(
        REGRESSIONS / "perp_entry_column_opposite_side_fan.mmd",
        "#2124",
        "#2125",
        "#2127",
        "s1n2->s1n3 collinear pair untracked",
    ),
    _known_abort(REGRESSIONS / "perp_entry_column_straight_drop.mmd", "#2121", "#2119"),
    _known_abort(REGRESSIONS / "perp_exit_single_feeder_landing.mmd", "#2124", "#2092"),
    _known_abort("declined_column", "#2119"),
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
def test_bridge_record_describes_the_drawn_layout(case: Path | str) -> None:
    graph = _laid_out(case)
    stale = [
        port_id
        for port_id in sorted(graph._cross_column_perp_bridge_ports)
        if not _feeders_descending_outside(graph, port_id)
    ]
    assert not stale, stale


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
