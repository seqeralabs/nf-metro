"""Invariants for the non-merging-crossing bridge glyph (issue #439).

A bridge is a short gap in the lower-priority ("under") line where two
distinct lines cross without sharing a node, so the crossing reads as an
overpass rather than an interchange.  These tests pin:

* genuine crossings produce a gap, and the gap breaks the *whole* under
  bundle (every collinear sibling route of the line), not one route;
* interchanges (shared station/port/junction/merge) never produce a gap;
* every gap sits clear of any node and lies on the under-route's segment;
* detection is deterministic;
* the rendered under-line SVG path actually carries a pen-up at the gap
  (this is what differs from ``main``, which draws the line continuous).
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

# Fixtures with at least one genuine non-merging crossing.
FIXTURES_WITH_CROSSINGS = [
    EXAMPLES_DIR / "genomic_pipeline.mmd",
    EXAMPLES_DIR / "rnaseq_sections.mmd",
    EXAMPLES_DIR / "differentialabundance.mmd",
    TOPOLOGIES_DIR / "complex_multipath.mmd",
    TOPOLOGIES_DIR / "funcprofiler_upstream.mmd",
]

# Fixtures with no bridge: pure fans/merges/shared sinks, or a lone under-line
# travelling in the over-line's own bundle (genomeassembly - a branch
# divergence, deliberately not bridged).
FIXTURES_WITHOUT_CROSSINGS = [
    EXAMPLES_DIR / "rnaseq_auto.mmd",
    EXAMPLES_DIR / "variant_calling.mmd",
    EXAMPLES_DIR / "genomeassembly.mmd",
    TOPOLOGIES_DIR / "trunk_through_fan.mmd",
    TOPOLOGIES_DIR / "terminal_symmetric_fan.mmd",
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
            (by_id[rid].line_id, bk.seg_index, round(bk.cut_a[0]), round(bk.cut_a[1]),
             round(bk.cut_b[0]), round(bk.cut_b[1]))
            for rid, breaks in bridges.items()
            for bk in breaks
        )

    assert fingerprint() == fingerprint()


def test_whole_under_bundle_breaks_differentialabundance():
    """Regression for the sibling-route gap fill: where the rnaseq diagonal
    crosses the assay bundle, all three under lines must break, not just the
    routes that happen to cross (overlapping siblings would fill the gap)."""
    graph, routes, _, bridges = _bridges(EXAMPLES_DIR / "differentialabundance.mmd")
    lines_broken = set()
    by_id = {id(r): r for r in routes}
    for rid, breaks in bridges.items():
        for bk in breaks:
            mx = (bk.cut_a[0] + bk.cut_b[0]) / 2
            my = (bk.cut_a[1] + bk.cut_b[1]) / 2
            if abs(mx - 703) < 20 and abs(my - 240) < 20:
                lines_broken.add(by_id[rid].line_id)
    assert {"affy", "maxquant", "geo"} <= lines_broken


def test_suppressed_when_lone_underline_in_over_bundle():
    """genomeassembly's hic_reads crosses the assemblies bus while travelling
    in the assemblies bundle - a branch divergence, so no bridge fires (would
    leave hic_reads broken beside its continuous bundle-mate)."""
    _, _, _, bridges = _bridges(EXAMPLES_DIR / "genomeassembly.mmd")
    assert bridges == {}


def test_rendered_under_line_has_pen_up():
    """An under-line that crosses under another draws a path with an interior
    move (pen-up) - on ``main`` it is continuous."""
    graph = parse_metro_mermaid(
        (TOPOLOGIES_DIR / "complex_multipath.mmd").read_text()
    )
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_THEME)
    broken = [d for d in _edge_path_ds(svg) if d.count("M") > 1]
    assert broken, "expected at least one broken (bridged) under-line path"


def test_theme_toggle_disables_bridges():
    import dataclasses

    graph = parse_metro_mermaid(
        (TOPOLOGIES_DIR / "complex_multipath.mmd").read_text()
    )
    compute_layout(graph)
    theme_off = dataclasses.replace(NFCORE_THEME, bridge_glyph=False)
    svg = render_svg(graph, theme_off)
    assert not any(d.count("M") > 1 for d in _edge_path_ds(svg))


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
