"""A trunk band's slot order carries the lane order of the entry port its lines
descend into, and the trunk-slot pass reaches that order in every call."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nf_metro.api import render_string
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases.guards import _guard_trunk_bands_crossing_optimal
from nf_metro.layout.routing import compute_station_offsets, normalize
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.normalize import (
    _band_order_deficits,
    _band_port_constraints,
    _coincident_trunk_slots,
    _HTrunk,
    _plan_trunk_band,
    _slot_depth_in_band,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge, MetroGraph, Port, PortSide, Section

ROOT = Path(__file__).resolve().parents[1]
RIBOSEQ_MAPS = (
    "examples/riboseq_metro.mmd",
    "tests/fixtures/curve_invariant_repros/riboseq_inter_row_corridor.mmd",
)
RL_RETURN = "tests/fixtures/curve_invariant_repros/rl_return_row_convergence.mmd"
PORT = "dst__entry_left_0"


def _graph(
    *edges: tuple[str, str, str],
    side: PortSide = PortSide.LEFT,
    direction: str = "LR",
) -> MetroGraph:
    """One section whose entry ``PORT`` the given edges may feed."""
    graph = MetroGraph()
    graph.add_section(Section(id="dst", name="Dst", direction=direction))
    graph.add_port(Port(id=PORT, section_id="dst", side=side))
    for source, target, line in edges:
        graph.add_edge(Edge(source, target, line))
    return graph


def _ctx(graph: MetroGraph, offsets: dict[str, float]) -> SimpleNamespace:
    return SimpleNamespace(
        graph=graph,
        station_offsets={(PORT, line): off for line, off in offsets.items()},
    )


def _trunk(
    line: str,
    source: str,
    target: str,
    *,
    y: float = 100.0,
    x_lo: float = 0.0,
    x_hi: float = 300.0,
) -> _HTrunk:
    """A leftward trunk dipping below two risers that climb back to its row."""
    return _HTrunk(
        route=RoutedPath(
            edge=Edge(source, target, line),
            line_id=line,
            points=[(x_hi, y - 80), (x_hi, y), (x_lo, y), (x_lo, y - 80)],
        ),
        idx=1,
        y=y,
        x_lo=x_lo,
        x_hi=x_hi,
        dips_down=True,
        sign_x=-1,
    )


def _top_to_bottom(plan: tuple[list[list[_HTrunk]], dict[int, int], int]) -> list:
    order, track_of, count = plan
    depth = {
        sg[0].route.line_id: _slot_depth_in_band(
            track_of[id(sg)], count, sg[0].dips_down
        )
        for sg in order
    }
    return sorted(depth, key=depth.__getitem__)


@pytest.mark.parametrize(
    ("offsets", "expected"),
    [({"a": 4.0, "b": -4.0}, ["a", "b"]), ({"a": -4.0, "b": 4.0}, ["b", "a"])],
)
def test_band_takes_the_lane_order_of_the_port_its_lines_enter(offsets, expected):
    """A leftward trunk turns down onto a descent and right into a LEFT entry:
    the outer lane stays outer through both turns, so the slot on top of the
    band lands on the larger port offset."""
    graph = _graph(("sa", PORT, "a"), ("sb", PORT, "b"))
    band = [_trunk("a", "sa", PORT), _trunk("b", "sb", PORT)]

    assert _top_to_bottom(_plan_trunk_band(band, _ctx(graph, offsets))) == expected


def test_a_sibling_edge_through_one_merge_hop_reaches_the_port():
    """A trunk ending elsewhere reaches the port its source sends the same line
    to as well, through a merge station with a single onward edge."""
    graph = _graph(
        ("sa", PORT, "a"),
        ("sb", "elsewhere", "b"),
        ("sb", "merge", "b"),
        ("merge", PORT, "b"),
    )
    band = [_trunk("a", "sa", PORT), _trunk("b", "sb", "elsewhere")]

    for offsets, expected in (
        ({"a": 4.0, "b": -4.0}, ["a", "b"]),
        ({"a": -4.0, "b": 4.0}, ["b", "a"]),
    ):
        plan = _plan_trunk_band(band, _ctx(graph, offsets))
        assert _top_to_bottom(plan) == expected


@pytest.mark.parametrize(
    "offsets", [{"a": 4.0, "b": -4.0}, {"a": -4.0, "b": 4.0}], ids=["a>b", "b>a"]
)
def test_a_port_one_source_feeds_two_lines_decides_no_order(offsets):
    """``sa`` sends ``a`` and ``c`` into the port as one bundle whose order
    ``sa`` pins, so the port's lane order is not the band's to follow."""
    graph = _graph(("sa", PORT, "a"), ("sa", PORT, "c"), ("sb", PORT, "b"))
    band = [_trunk("a", "sa", PORT), _trunk("b", "sb", PORT)]
    ctx = _ctx(graph, offsets)

    assert _band_port_constraints(_coincident_trunk_slots(band), ctx) == []
    assert _top_to_bottom(_plan_trunk_band(band, ctx)) == _top_to_bottom(
        _plan_trunk_band(band)
    )


def test_a_refused_port_passes_the_pair_to_the_next_port_both_lines_enter():
    """``sa`` feeds ``a`` and ``c`` into ``PORT`` together, so ``PORT`` decides
    nothing; ``a`` and ``b`` also both enter ``zz``'s left entry, which puts
    ``a`` on the larger offset and so its slot on top, against the free order."""
    graph = _graph(
        ("sa", PORT, "a"),
        ("sa", PORT, "c"),
        ("sb", PORT, "b"),
        ("sa", "zz__entry_left_0", "a"),
        ("sb", "zz__entry_left_0", "b"),
    )
    graph.add_section(Section(id="zz", name="Zz", direction="LR"))
    graph.add_port(Port(id="zz__entry_left_0", section_id="zz", side=PortSide.LEFT))
    band = [_trunk("a", "sa", PORT), _trunk("b", "sb", PORT)]
    ctx = _ctx(graph, {"a": -4.0, "b": 4.0})
    ctx.station_offsets.update(
        {("zz__entry_left_0", "a"): 4.0, ("zz__entry_left_0", "b"): -4.0}
    )

    assert _band_port_constraints(_coincident_trunk_slots(band), ctx) == [(0, 1)]
    assert _top_to_bottom(_plan_trunk_band(band)) == ["b", "a"]
    assert _top_to_bottom(_plan_trunk_band(band, ctx)) == ["a", "b"]


@pytest.mark.parametrize(
    ("side", "direction"),
    [(PortSide.TOP, "LR"), (PortSide.LEFT, "TB")],
    ids=["top-entry", "tb-side-entry"],
)
def test_a_port_whose_lanes_do_not_stack_on_y_decides_no_order(side, direction):
    """Only a side entry of a section whose lanes stack on Y turns the band's
    vertical order into its lane order."""
    graph = _graph(("sa", PORT, "a"), ("sb", PORT, "b"), side=side, direction=direction)
    band = [_trunk("a", "sa", PORT), _trunk("b", "sb", PORT)]
    ctx = _ctx(graph, {"a": 4.0, "b": -4.0})

    assert _band_port_constraints(_coincident_trunk_slots(band), ctx) == []


def test_a_port_order_that_packs_more_tracks_keeps_the_free_order():
    """``a`` spans the corridor, ``b`` and ``c`` share one track at either end.
    Lifting ``a`` above ``b`` for the port wedges it between them and costs the
    band a track, so the band keeps its two-track free order."""
    graph = _graph(("sa", PORT, "a"), ("sb", PORT, "b"), ("sc", "other", "c"))
    band = [
        _trunk("a", "sa", PORT, x_lo=0.0, x_hi=300.0),
        _trunk("b", "sb", PORT, x_lo=0.0, x_hi=100.0),
        _trunk("c", "sc", "other", x_lo=200.0, x_hi=300.0),
    ]
    ctx = _ctx(graph, {"a": 4.0, "b": -4.0})
    slots = _coincident_trunk_slots(band)

    assert _band_port_constraints(slots, ctx) == [(0, 1)]
    plan = _plan_trunk_band(band, ctx)
    assert plan[2] == _plan_trunk_band(band)[2] == 2
    order = _top_to_bottom(plan)
    assert order.index("b") < order.index("a")


def test_port_ordered_slots_at_one_y_are_a_restack_deficit_only_when_strict():
    """Two slots the port orders, fused on one Y, express both orders at once:
    the restack gate reads that as a deficit, the band guard does not."""
    graph = _graph(("sa", PORT, "a"), ("sb", PORT, "b"))
    band = [_trunk("a", "sa", PORT), _trunk("b", "sb", PORT)]
    ctx = _ctx(graph, {"a": 4.0, "b": -4.0})

    assert _band_order_deficits(band) == []
    assert _band_order_deficits(band, ctx) == []
    assert _band_order_deficits(band, ctx, strict=True) == [(100.0, 0, 0)]


def test_a_band_stacked_against_its_port_order_is_a_deficit():
    """Crossings cannot tell the two stackings apart; the port order can."""
    graph = _graph(("sa", PORT, "a"), ("sb", PORT, "b"))
    band = [_trunk("a", "sa", PORT, y=104.0), _trunk("b", "sb", PORT, y=100.0)]
    ctx = _ctx(graph, {"a": 4.0, "b": -4.0})

    assert _band_order_deficits(band) == []
    assert _band_order_deficits(band, ctx) == [(100.0, 0, 0)]
    assert (
        _band_order_deficits(
            [_trunk("a", "sa", PORT, y=100.0), _trunk("b", "sb", PORT, y=104.0)], ctx
        )
        == []
    )


def _layout(relative_path: str) -> MetroGraph:
    path = ROOT / relative_path
    graph = parse_metro_mermaid(path.read_text())
    graph.source_dir = str(path.parent)
    compute_layout(graph)
    return graph


@pytest.mark.parametrize("relative_path", RIBOSEQ_MAPS)
def test_riboseq_corridor_band_carries_the_orf_calling_entry_order(
    relative_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``annotation`` and ``riboseq`` run left along the top corridor, drop
    down the west side and turn right into ``orf_calling``'s left entry, where
    ``annotation`` holds the lower lane.  Every trunk-slot plan of that band in
    the render, whichever pass makes it, stacks ``annotation`` above
    ``riboseq``."""
    annotation = ("novel_transcripts__exit_right_5", "orf_calling__entry_left_7")
    riboseq = ("__junction_13", "psite_id__entry_left_9")
    port = annotation[1]
    real_plan = normalize._plan_trunk_band
    real_stack = normalize._stack_trunk_bands
    seen: list[tuple[list[tuple[str, str]], dict]] = []
    stack_ctx: list = []

    def stack(bands, ctx, *args, **kwargs):
        stack_ctx.append(ctx)
        try:
            return real_stack(bands, ctx, *args, **kwargs)
        finally:
            stack_ctx.pop()

    def plan(band, *args, **kwargs):
        result = real_plan(band, *args, **kwargs)
        edges = {(t.route.edge.source, t.route.edge.target) for t in band}
        if annotation in edges and riboseq in edges:
            seen.append((_top_to_bottom_edges(result), stack_ctx[-1].station_offsets))
        return result

    monkeypatch.setattr(normalize, "_stack_trunk_bands", stack)
    monkeypatch.setattr(normalize, "_plan_trunk_band", plan)
    path = ROOT / relative_path
    render_string(path.read_text(), source_dir=str(path.parent))

    assert seen
    for order, offsets in seen:
        assert offsets[(port, "annotation")] > offsets[(port, "riboseq")]
        assert order.index(annotation) < order.index(riboseq), order


def _top_to_bottom_edges(plan) -> list[tuple[str, str]]:
    order, track_of, count = plan
    depth = {
        (t.route.edge.source, t.route.edge.target): _slot_depth_in_band(
            track_of[id(sg)], count, sg[0].dips_down
        )
        for sg in order
        for t in sg
    }
    return sorted(depth, key=depth.__getitem__)


@pytest.mark.parametrize("relative_path", (*RIBOSEQ_MAPS, RL_RETURN))
def test_port_ordered_bands_pass_the_trunk_band_guard(relative_path: str) -> None:
    """The trunk-band guard regroups bands from a narrower route population
    than the restack sees, and reaches the same port-order verdicts there.
    ``rl_return_row_convergence`` holds a band whose port order would pack an
    extra track, which the plan refuses in every pass."""
    graph = _layout(relative_path)
    _guard_trunk_bands_crossing_optimal(
        graph, "test", offsets=compute_station_offsets(graph)
    )
    path = ROOT / relative_path
    assert render_string(path.read_text(), source_dir=str(path.parent))


def test_port_ordered_maps_settle_identically_across_hash_seeds() -> None:
    """The port order is read from the graph's edge lists and the slots, so
    every hash seed settles and renders these maps alike."""
    from hash_seed_oracle import (
        observation_differences,
        observe_paths_across_hash_seeds,
    )

    paths = tuple(ROOT / path for path in (*RIBOSEQ_MAPS, RL_RETURN))
    differences = observation_differences(observe_paths_across_hash_seeds(paths))
    assert not differences, differences
