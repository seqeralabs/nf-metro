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

from pathlib import Path

import pytest

from nf_metro.layout.constants import CURVE_RADIUS
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.bridges import BRIDGE_NODE_TOLERANCE, compute_bridges
from nf_metro.render.svg import apply_route_offsets, render_svg
from nf_metro.themes import NFCORE_THEME

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"
TOPOLOGIES_DIR = EXAMPLES_DIR / "topologies"

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
    svg = render_svg(graph, NFCORE_THEME)
    broken = [d for d in _edge_path_ds(svg) if d.count("M") > 1]
    assert broken, "expected at least one broken (bridged) under-line path"


def test_theme_toggle_disables_bridges():
    import dataclasses

    graph = parse_metro_mermaid((EXAMPLES_DIR / "genomic_pipeline.mmd").read_text())
    compute_layout(graph)
    theme_off = dataclasses.replace(NFCORE_THEME, bridge_glyph=False)
    svg = render_svg(graph, theme_off)
    assert not any(d.count("M") > 1 for d in _edge_path_ds(svg))


def test_animation_paths_flow_over_gaps():
    """Animated balls travel the continuous route - the bridge gaps are a
    static-render effect only.  Motion paths must be identical with bridges
    on or off."""
    import dataclasses

    graph = parse_metro_mermaid((EXAMPLES_DIR / "genomic_pipeline.mmd").read_text())
    compute_layout(graph)
    on = _motion_paths(render_svg(graph, NFCORE_THEME, animate=True))
    off = _motion_paths(
        render_svg(
            graph, dataclasses.replace(NFCORE_THEME, bridge_glyph=False), animate=True
        )
    )
    assert on and on == off


def _motion_paths(svg: str) -> list[str]:
    import re

    return re.findall(r'<path id="motion-path-[^"]*" d="([^"]*)"', svg)


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
