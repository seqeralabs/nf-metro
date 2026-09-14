"""Invariants for the non-merging-crossing bridge glyph (issue #439).

A bridge is a short gap in the lower-priority ("under") line where two
bundles cross without merging, so the crossing reads as an overpass.  A gap
is drawn only when the two bundles **share a colour** (otherwise colour
already distinguishes them) and the crossing is a true crossover, not an
interchange or a line's approach to a station join.  These tests pin:

* shared-colour crossings produce a gap that breaks the *whole* under
  bundle (every collinear sibling route), not one route;
* distinct-colour crossings, interchanges, and join approaches get no gap;
* every gap sits clear of any node and lies on the under-route's segment;
* detection is deterministic;
* the rendered under-line carries a pen-up at the gap (vs continuous on
  ``main``), while animation motion paths flow over it unchanged.
"""

from collections.abc import Callable
from pathlib import Path

import drawsvg as draw
import pytest

from nf_metro.layout.constants import CURVE_RADIUS
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import RoutedPath, compute_station_offsets, route_edges
from nf_metro.layout.routing.common import Direction, SourceTurnout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge, MetroGraph
from nf_metro.render.bridges import (
    BRIDGE_NODE_TOLERANCE,
    BridgeBreak,
    _line_reachability,
    _same_line_is_fan,
    _segment_containing,
    _segment_intersection,
    compute_bridges,
)
from nf_metro.render.path_geometry import materialize_source_turnout_paths
from nf_metro.render.svg import (
    _render_edges,
    _route_decision_fingerprint,
    apply_route_offsets,
    render_svg,
)
from nf_metro.themes import NFCORE_DARK_THEME

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"
TOPOLOGIES_DIR = EXAMPLES_DIR / "topologies"
SEED_72 = Path(__file__).parent / "fixtures" / "hash_seed_determinism" / "seed_72.mmd"

# Fixtures with at least one bridged crossing: the two crossing bundles share
# a colour, so a gap is needed to show which passes over.
FIXTURES_WITH_CROSSINGS = [
    EXAMPLES_DIR / "genomic_pipeline.mmd",
    EXAMPLES_DIR / "differentialabundance_default.mmd",
]

# Fixtures with no bridge: pure fans/merges/shared sinks; distinct-colour
# crossings (colour already disambiguates, e.g. rnaseq_sections, complex_
# multipath, funcprofiler, genomeassembly); or a crossing on a line's approach
# to a station join (differentialabundance, where the only crossings are a
# distinct-colour crossover and a shared-colour gsea-join approach).
FIXTURES_WITHOUT_CROSSINGS = [
    EXAMPLES_DIR / "rnaseq_auto.mmd",
    EXAMPLES_DIR / "variant_calling.mmd",
    EXAMPLES_DIR / "genomeassembly.mmd",
    EXAMPLES_DIR / "differentialabundance.mmd",
    EXAMPLES_DIR / "rnaseq_sections.mmd",
    TOPOLOGIES_DIR / "complex_multipath.mmd",
    TOPOLOGIES_DIR / "funcprofiler_upstream.mmd",
    TOPOLOGIES_DIR / "trunk_through_fan.mmd",
    TOPOLOGIES_DIR / "shared_sink_parallel.mmd",
]


def _bridges(path: Path):
    """Return (graph, routes, polylines, bridges) for a fixture."""
    graph = parse_metro_mermaid(path.read_text())
    graph.source_dir = str(path.parent)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    line_priority = {lid: i for i, lid in enumerate(graph.lines)}
    routes = sorted(routes, key=lambda r: -line_priority.get(r.line_id, -1))
    polylines = [apply_route_offsets(r, offsets) for r in routes]
    bridges = compute_bridges(graph, routes, polylines, curve_radius=CURVE_RADIUS)
    return graph, routes, polylines, bridges


@pytest.mark.parametrize("path", FIXTURES_WITH_CROSSINGS, ids=lambda p: p.stem)
def test_genuine_crossings_produce_bridges(path):
    _, _, _, bridges = _bridges(path)
    assert sum(len(v) for v in bridges.values()) > 0


@pytest.mark.parametrize("path", FIXTURES_WITHOUT_CROSSINGS, ids=lambda p: p.stem)
def test_no_bridges_without_crossings(path):
    _, _, _, bridges = _bridges(path)
    assert bridges == {}


@pytest.mark.parametrize("path", FIXTURES_WITH_CROSSINGS, ids=lambda p: p.stem)
def test_bridges_clear_of_nodes(path):
    """No gap midpoint may land on a station/port/junction/merge - those are
    interchanges, not crossings."""
    graph, _, _, bridges = _bridges(path)
    nodes = [(s.x, s.y) for s in graph.stations.values()]
    for breaks in bridges.values():
        for bk in breaks:
            mx = (bk.cut_a[0] + bk.cut_b[0]) / 2
            my = (bk.cut_a[1] + bk.cut_b[1]) / 2
            for nx, ny in nodes:
                assert not (
                    abs(mx - nx) < BRIDGE_NODE_TOLERANCE
                    and abs(my - ny) < BRIDGE_NODE_TOLERANCE
                ), f"bridge at ({mx:.0f},{my:.0f}) sits on a node in {path.stem}"


@pytest.mark.parametrize("path", FIXTURES_WITH_CROSSINGS, ids=lambda p: p.stem)
def test_bridge_span_lies_on_under_segment(path):
    """Each gap's end points must be collinear with the route segment they
    break, so the rendered pen-up does not distort the line."""
    _, routes, polylines, bridges = _bridges(path)
    by_id = {id(r): poly for r, poly in zip(routes, polylines)}
    for rid, breaks in bridges.items():
        poly = by_id[rid]
        for bk in breaks:
            a, b = poly[bk.seg_index], poly[bk.seg_index + 1]
            for pt in (bk.cut_a, bk.cut_b):
                assert _perp_distance(pt, a, b) <= 1.0


def test_seed72_straight_endpoint_bridges_keep_the_full_gap() -> None:
    graph = parse_metro_mermaid(SEED_72.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    line_priority = {line_id: index for index, line_id in enumerate(graph.lines)}
    routes = sorted(
        route_edges(graph, station_offsets=offsets),
        key=lambda route: -line_priority.get(route.line_id, -1),
    )
    polylines = [apply_route_offsets(route, offsets) for route in routes]
    geometry_and_ownership = tuple(
        (
            tuple(route.points),
            route.curve_radii,
            route.route_system_id,
            route.emission_member_id,
            route.route_plan_ids,
            route.route_system_owned_segment_ranks,
        )
        for route in routes
    )
    decision_fingerprint = _route_decision_fingerprint(routes)

    bridges = compute_bridges(graph, routes, polylines, curve_radius=CURVE_RADIUS)

    assert _route_decision_fingerprint(routes) == decision_fingerprint
    assert (
        tuple(
            (
                tuple(route.points),
                route.curve_radii,
                route.route_system_id,
                route.emission_member_id,
                route.route_plan_ids,
                route.route_system_owned_segment_ranks,
            )
            for route in routes
        )
        == geometry_and_ownership
    )
    # The l0/l3 pair leaves __junction_15 on a bare two-point run (540 -> 602):
    # neither end is a smoothed corner, so no corner clearance narrows the gap.
    # One l0 descent crosses the run, at x=568, so the over bundle has zero
    # span and the gap is BRIDGE_GAP_HALF either side of that descent.
    for line_id, y in (("l0", 324.0), ("l3", 328.0)):
        route = next(
            route
            for route in routes
            if route.edge.source == "__junction_15"
            and route.edge.target == "s5__entry_left_11"
            and route.line_id == line_id
        )
        assert bridges[id(route)] == [
            BridgeBreak(
                seg_index=0,
                cut_a=(562.0, y),
                cut_b=(574.0, y),
            )
        ]

    # That descent's own approach run (530 -> 568 at y=506) is crossed by two
    # descents, at x=546 and x=554.  Its right end is a smoothed corner, whose
    # clearance stops the gap at 552; its left end is the s3 exit port, a
    # straight run start that imposes no clearance, so the gap keeps the full
    # BRIDGE_GAP_HALF below the leftmost descent rather than being pulled in.
    descent = next(
        route
        for route in routes
        if route.edge.source == "s3__exit_right_2"
        and route.edge.target == "s4__entry_left_9"
    )
    assert bridges[id(descent)] == [
        BridgeBreak(
            seg_index=0,
            cut_a=(540.0, 506.0),
            cut_b=(552.0, 506.0),
        )
    ]


def test_bridge_gap_cannot_invade_horizontal_to_diagonal_curve() -> None:
    points = [(0.0, 0.0), (100.0, 0.0), (120.0, 20.0)]

    assert (
        _segment_containing(
            points,
            ((80.0, 0.0), (90.0, 0.0)),
            CURVE_RADIUS,
            [CURVE_RADIUS],
        )
        is None
    )
    assert (
        _segment_containing(
            points,
            ((60.0, 0.0), (70.0, 0.0)),
            CURVE_RADIUS,
            [CURVE_RADIUS],
        )
        == 0
    )


def test_bridge_gap_cannot_cut_a_materialized_source_turnout() -> None:
    route = RoutedPath(
        Edge("junction", "target", "line"),
        "line",
        [(100.0, 0.0), (100.0, 100.0)],
        source_turnout=SourceTurnout(
            "upstream",
            "continuing",
            Direction.R,
            Direction.D,
            14.0,
        ),
    )
    points, radii, _shifts = materialize_source_turnout_paths(
        [route],
        [route.points],
        default_radius=CURVE_RADIUS,
    )

    assert (
        _segment_containing(
            points[0],
            ((100.0, 14.0), (100.0, 16.0)),
            CURVE_RADIUS,
            radii[0],
        )
        is None
    )
    assert (
        _segment_containing(
            points[0],
            ((100.0, 30.0), (100.0, 40.0)),
            CURVE_RADIUS,
            radii[0],
        )
        == 1
    )


def test_terminal_source_turnout_clips_incoming_member_at_tangent() -> None:
    incoming = RoutedPath(
        Edge("upstream", "junction", "line"),
        "line",
        [(80.0, 0.0), (100.0, 0.0)],
    )
    outgoing = RoutedPath(
        Edge("junction", "target", "line"),
        "line",
        [(100.0, 0.0), (100.0, 100.0)],
        source_turnout=SourceTurnout(
            "upstream",
            None,
            Direction.R,
            Direction.D,
            CURVE_RADIUS,
        ),
    )

    points, radii, shifts = materialize_source_turnout_paths(
        [incoming, outgoing],
        [incoming.points, outgoing.points],
        default_radius=CURVE_RADIUS,
    )

    assert points == [
        [(80.0, 0.0), (90.0, 0.0)],
        [(90.0, 0.0), (100.0, 0.0), (100.0, 100.0)],
    ]
    assert radii == [None, [CURVE_RADIUS]]
    assert shifts == [0, 1]


def test_terminal_source_turnouts_sharing_an_incoming_member_clip_once() -> None:
    incoming = RoutedPath(
        Edge("upstream", "junction", "line"),
        "line",
        [(80.0, 0.0), (100.0, 0.0)],
    )
    outgoing = [
        RoutedPath(
            Edge("junction", target, "line"),
            "line",
            [(100.0, 0.0), (100.0, 100.0)],
            source_turnout=SourceTurnout(
                "upstream",
                None,
                Direction.R,
                Direction.D,
                CURVE_RADIUS,
            ),
        )
        for target in ("first", "second")
    ]

    points, radii, shifts = materialize_source_turnout_paths(
        [incoming, *outgoing],
        [incoming.points, *(route.points for route in outgoing)],
        default_radius=CURVE_RADIUS,
    )

    assert points[0] == [(80.0, 0.0), (90.0, 0.0)]
    assert points[1:] == [
        [(90.0, 0.0), (100.0, 0.0), (100.0, 100.0)],
        [(90.0, 0.0), (100.0, 0.0), (100.0, 100.0)],
    ]
    assert radii == [None, [CURVE_RADIUS], [CURVE_RADIUS]]
    assert shifts == [0, 1, 1]


@pytest.mark.parametrize(
    ("factory", "message"),
    (
        (
            lambda: SourceTurnout(
                "", "continuing", Direction.R, Direction.D, CURVE_RADIUS
            ),
            "incoming member",
        ),
        (
            lambda: SourceTurnout(
                "upstream", "", Direction.R, Direction.D, CURVE_RADIUS
            ),
            "continuing source turnout",
        ),
        (
            lambda: SourceTurnout(
                "upstream", "continuing", Direction.D, Direction.D, CURVE_RADIUS
            ),
            "enters horizontally",
        ),
        (
            lambda: SourceTurnout(
                "upstream", "continuing", Direction.R, Direction.R, CURVE_RADIUS
            ),
            "leaves vertically",
        ),
        (
            lambda: SourceTurnout(
                "upstream", "continuing", Direction.R, Direction.D, 0.0
            ),
            "positive finite radius",
        ),
        (
            lambda: SourceTurnout(
                "upstream", "continuing", Direction.R, Direction.D, float("inf")
            ),
            "positive finite radius",
        ),
    ),
)
def test_source_turnout_rejects_invalid_frozen_metadata(
    factory: Callable[[], SourceTurnout],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        factory()


def test_rendered_bridge_break_uses_materialized_turnout_segment_index() -> None:
    graph = parse_metro_mermaid(
        """%%metro line: line | Line | #24B064
graph LR
    upstream -->|line| junction
    junction -->|line| target
"""
    )
    route = RoutedPath(
        Edge("junction", "target", "line"),
        "line",
        [(100.0, 0.0), (100.0, 100.0)],
        source_turnout=SourceTurnout(
            "upstream",
            "continuing",
            Direction.R,
            Direction.D,
            CURVE_RADIUS,
        ),
    )
    drawing = draw.Drawing(200, 200)
    polylines, radii, _shifts = materialize_source_turnout_paths(
        [route], [route.points], default_radius=CURVE_RADIUS
    )

    _render_edges(
        drawing,
        graph,
        [route],
        polylines,
        radii,
        ((BridgeBreak(1, (100.0, 30.0), (100.0, 40.0)),),),
        NFCORE_DARK_THEME,
        CURVE_RADIUS,
    )

    svg = drawing.as_svg()
    assert "L100.0,30.0 M100.0,40.0" in svg


@pytest.mark.parametrize("path", FIXTURES_WITH_CROSSINGS, ids=lambda p: p.stem)
def test_detection_is_deterministic(path):
    def fingerprint():
        routes, polys, bridges = _bridges(path)[1:]
        by_id = {id(r): r for r in routes}
        return sorted(
            (
                by_id[rid].line_id,
                bk.seg_index,
                round(bk.cut_a[0]),
                round(bk.cut_a[1]),
                round(bk.cut_b[0]),
                round(bk.cut_b[1]),
            )
            for rid, breaks in bridges.items()
            for bk in breaks
        )

    assert fingerprint() == fingerprint()


def test_shared_colour_bundle_breaks_whole():
    """At differentialabundance_default's shared-colour crossing (all four
    lines on both sides), the whole under bundle breaks - several distinct
    lines, not one - and overlapping sibling routes don't fill the gap."""
    graph, routes, _, bridges = _bridges(
        EXAMPLES_DIR / "differentialabundance_default.mmd"
    )
    by_id = {id(r): r for r in routes}
    lines_broken = {
        by_id[rid].line_id for rid, breaks in bridges.items() for _ in breaks
    }
    assert len(lines_broken) >= 3


def test_distinct_colour_crossings_not_bridged():
    """rnaseq_sections (hisat2 over bowtie2) and complex_multipath (fast over
    legacy/standard) are crossings of wholly distinct colours - colour already
    disambiguates them, so no bridge is drawn."""
    for path in (
        EXAMPLES_DIR / "rnaseq_sections.mmd",
        TOPOLOGIES_DIR / "complex_multipath.mmd",
    ):
        _, _, _, bridges = _bridges(path)
        assert bridges == {}, path.stem


def test_join_approach_not_bridged():
    """In differentialabundance the gprofiler2 lines cross the gmt_in->gsea
    lines on their approach to the gsea station (~22px), sharing colours - but
    it is a join, not a crossover, so no bridge fires near gsea."""
    graph, _, _, bridges = _bridges(EXAMPLES_DIR / "differentialabundance.mmd")
    gsea = graph.stations["gsea"]
    for breaks in bridges.values():
        for bk in breaks:
            mx = (bk.cut_a[0] + bk.cut_b[0]) / 2
            my = (bk.cut_a[1] + bk.cut_b[1]) / 2
            assert not (abs(mx - gsea.x) < 40 and abs(my - gsea.y) < 40), (
                f"bridge at ({mx:.0f},{my:.0f}) sits on the gsea join approach"
            )


def test_rendered_under_line_has_pen_up():
    """An under-line that crosses under another draws a path with an interior
    move (pen-up) - on ``main`` it is continuous."""
    graph = parse_metro_mermaid((EXAMPLES_DIR / "genomic_pipeline.mmd").read_text())
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_DARK_THEME)
    broken = [d for d in _edge_path_ds(svg) if d.count("M") > 1]
    assert broken, "expected at least one broken (bridged) under-line path"


def test_theme_toggle_disables_bridges():
    import dataclasses

    graph = parse_metro_mermaid((EXAMPLES_DIR / "genomic_pipeline.mmd").read_text())
    compute_layout(graph)
    theme_off = dataclasses.replace(NFCORE_DARK_THEME, bridge_glyph=False)
    svg = render_svg(graph, theme_off)
    assert not any(d.count("M") > 1 for d in _edge_path_ds(svg))


def test_animation_paths_flow_over_gaps():
    """Animated balls travel the continuous route - the bridge gaps are a
    static-render effect only.  Motion paths must be identical with bridges
    on or off."""
    import dataclasses

    graph = parse_metro_mermaid((EXAMPLES_DIR / "genomic_pipeline.mmd").read_text())
    compute_layout(graph)
    on = _motion_paths(render_svg(graph, NFCORE_DARK_THEME, animate=True))
    off = _motion_paths(
        render_svg(
            graph,
            dataclasses.replace(NFCORE_DARK_THEME, bridge_glyph=False),
            animate=True,
        )
    )
    assert on and on == off


def test_same_line_fan_legs_are_not_a_crossover():
    """Two same-line edges whose legs fork and rejoin (a fan-out / fan-in /
    loop) are a fan, not a crossover - even when their geometry crosses."""
    g = MetroGraph(
        edges=[
            Edge("fork", "left", "x"),
            Edge("fork", "right", "x"),
            Edge("left", "join", "x"),
            Edge("right", "join", "x"),
        ]
    )
    reachability = _line_reachability(g)
    e_left = Edge("fork", "left", "x")
    e_right = Edge("fork", "right", "x")
    # Distinct legs that rejoin downstream at "join" -> a fan, not a crossover.
    assert _same_line_is_fan(
        Edge("left", "join", "x"), Edge("right", "join", "x"), reachability
    )
    # Edges sharing the fork node are also a fan.
    assert _same_line_is_fan(e_left, e_right, reachability)


def test_same_line_independent_legs_are_a_crossover():
    """Two same-line edges that head to destinations which never reconverge are
    a genuine crossover and must be eligible for a bridge."""
    g = MetroGraph(
        edges=[
            Edge("hub", "a1", "x"),
            Edge("a1", "a2", "x"),
            Edge("hub", "b1", "x"),
            Edge("b1", "b2", "x"),
        ]
    )
    reachability = _line_reachability(g)
    # a1->a2 and b1->b2 share no endpoint and never reconverge downstream.
    assert not _same_line_is_fan(
        Edge("a1", "a2", "x"), Edge("b1", "b2", "x"), reachability
    )


def _segment_crossing(a, b, c, d):
    """Where segments ``a``-``b`` and ``c``-``d`` cross, or ``None``.

    Parallel segments are excluded however far they overlap: two runs sharing a
    channel have no single point where one passes over the other, so a gap on
    such an overlap shows the reader nothing.  The crossing must also be
    interior to both segments, since a shared endpoint is a join, not a
    crossover.
    """
    run = (b[0] - a[0], b[1] - a[1])
    other = (d[0] - c[0], d[1] - c[1])
    denominator = run[0] * other[1] - run[1] * other[0]
    if abs(denominator) < 1e-9:
        return None
    along = ((c[0] - a[0]) * other[1] - (c[1] - a[1]) * other[0]) / denominator
    across = ((c[0] - a[0]) * run[1] - (c[1] - a[1]) * run[0]) / denominator
    if not (0 < along < 1 and 0 < across < 1):
        return None
    return (a[0] + along * run[0], a[1] + along * run[1])


def _same_line_crossings(routes, polylines, line_id):
    """Every point at which two *line_id* runs cross, keyed by rounded point.

    Valued by the sections every leg crossing there heads into, which name a
    crossover independently of where global compaction has since put it.  One
    crossing can involve more than two legs, because a drop shared by several
    downstream sections is drawn as one channel carrying each of them.
    """
    drawn = [(r, p) for r, p in zip(routes, polylines) if r.line_id == line_id]
    crossings: dict[tuple[float, float], set[str]] = {}
    for index, (route_a, poly_a) in enumerate(drawn):
        for route_b, poly_b in drawn[index + 1 :]:
            for a, b in zip(poly_a, poly_a[1:]):
                for c, d in zip(poly_b, poly_b[1:]):
                    point = _segment_crossing(a, b, c, d)
                    if point is None:
                        continue
                    key = (round(point[0], 1), round(point[1], 1))
                    crossings.setdefault(key, set()).update(
                        {
                            route_a.edge.target.split("__")[0],
                            route_b.edge.target.split("__")[0],
                        }
                    )
    return crossings


def test_issue484_same_colour_crossover_is_bridged():
    """issue #484: a horizontal bam run crosses a vertical bam drop below the
    Small-variant/Phasing sections - a genuine same-colour crossover whose legs
    head to separate, never-reconverging destinations.  A bridge must fire.

    The map holds exactly one such crossover, so requiring the sole bam gap to
    sit on the sole bam crossing pins the gap to that one place without naming
    a coordinate that moves whenever the map compacts.
    """
    path = EXAMPLES_DIR / "longread_variant_calling.mmd"
    _, routes, polylines, bridges = _bridges(path)

    crossings = _same_line_crossings(routes, polylines, "bam")
    assert len(crossings) == 1, (
        f"expected the one documented bam crossover in {path.stem}, found "
        f"{len(crossings)}: {crossings}"
    )
    [(crossing_point, crossed_sections)] = crossings.items()
    assert {"tr_calling", "structural_variants"} <= crossed_sections, (
        f"the bam crossover involves legs into {crossed_sections}, which does "
        f"not include the documented tr_calling / structural_variants pair"
    )

    by_id = {id(r): (r, poly) for r, poly in zip(routes, polylines)}
    bam_breaks = [
        (bk, by_id[rid][0])
        for rid, breaks in bridges.items()
        for bk in breaks
        if by_id[rid][0].line_id == "bam"
    ]
    assert len(bam_breaks) == 1, (
        f"expected exactly one bam gap in {path.stem}, got "
        f"{[(r.edge.source, r.edge.target) for _, r in bam_breaks]}"
    )
    bk, route = bam_breaks[0]
    midpoint = (
        (bk.cut_a[0] + bk.cut_b[0]) / 2,
        (bk.cut_a[1] + bk.cut_b[1]) / 2,
    )
    assert midpoint == pytest.approx(crossing_point, abs=1.0), (
        f"bam gap on {route.edge.source}->{route.edge.target} is centred at "
        f"{midpoint}, not on the crossing at {crossing_point}"
    )
    assert route.edge.target.split("__")[0] in crossed_sections, (
        f"the bam gap breaks the leg into "
        f"{route.edge.target.split('__')[0]}, which is not one of the two "
        f"crossing legs {set(crossed_sections)}"
    )


def test_issue1322_forked_arms_recross_far_from_fork_is_bridged():
    """issue #1322: two l1 arms fork at a junction and re-cross perpendicular
    ~400px away, between Branch B and Feeder L1.  A third line (l2) corners
    through the same point, making the crossing cluster non-bipartite.  A
    bridge must still fire on the l1 crossover: the shared fork explains a
    meeting only at the junction, not a re-cross in open space, and the
    distinct-colour l2 corner reads apart by colour so must not veto it."""
    path = Path(__file__).parent / "fixtures" / "target_entry_runway_bypass.mmd"
    graph, routes, polylines, bridges = _bridges(path)
    by_id = {id(r): r for r in routes}
    l1_breaks = [
        bk
        for rid, breaks in bridges.items()
        for bk in breaks
        if by_id[rid].line_id == "l1"
    ]
    destination_ports = {
        next(
            pid
            for pid, port in graph.ports.items()
            if port.section_id == section_id and port.is_entry
        )
        for section_id in ("branch_b", "feeder_l1")
    }
    arm_candidates = [
        (route.edge.source, route.edge.target, route, polyline)
        for route, polyline in zip(routes, polylines)
        if route.line_id == "l1" and route.edge.target in destination_ports
    ]
    fork_sources = {
        source
        for source, _, _, _ in arm_candidates
        if {
            target
            for candidate_source, target, _, _ in arm_candidates
            if candidate_source == source
        }
        == destination_ports
    }
    assert len(fork_sources) == 1
    fork_source = next(iter(fork_sources))
    fork_arms = [
        (route, polyline)
        for source, _, route, polyline in arm_candidates
        if source == fork_source
    ]
    assert len(fork_arms) == 2
    crossings = [
        crossing
        for a, b in zip(fork_arms[0][1], fork_arms[0][1][1:])
        for c, d in zip(fork_arms[1][1], fork_arms[1][1][1:])
        if (crossing := _segment_intersection(a, b, c, d)) is not None
    ]
    assert len(crossings) == 1
    cross_x, cross_y = crossings[0]
    assert cross_x == pytest.approx(1432, abs=1)
    assert any(
        (bk.cut_a[0] + bk.cut_b[0]) / 2 == pytest.approx(cross_x, abs=1)
        and (bk.cut_a[1] + bk.cut_b[1]) / 2 == pytest.approx(cross_y, abs=1)
        for bk in l1_breaks
    ), "expected an l1 crossover bridge at the Branch B / Feeder L1 crossroads"


def _motion_paths(svg: str) -> list[str]:
    import re

    return re.findall(r"offset-path: path\('([^']*)'\)", svg)


def _perp_distance(pt, a, b):
    import math

    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length == 0:
        return math.hypot(pt[0] - a[0], pt[1] - a[1])
    return abs((pt[0] - a[0]) * (-dy / length) + (pt[1] - a[1]) * (dx / length))


def _edge_path_ds(svg: str) -> list[str]:
    import re

    return re.findall(r'<path[^>]*\bd="([^"]*)"', svg)
