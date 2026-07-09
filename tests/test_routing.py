"""Tests for edge routing."""

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import (
    OffsetRegime,
    compute_station_offsets,
    route_edges,
)
from nf_metro.parser.mermaid import parse_metro_mermaid


def test_straight_route():
    """Edges on the same track should be straight horizontal lines."""
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\ngraph LR\n    a -->|main| b\n"
    )
    compute_layout(graph)
    routes = route_edges(graph)
    assert len(routes) == 1
    # Same track -> 2 points (straight line)
    assert len(routes[0].points) == 2


def test_diagonal_route():
    """Edges between different tracks should have 4 waypoints."""
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro line: alt | Alt | #0000ff\n"
        "graph LR\n"
        "    a -->|main| b\n"
        "    a -->|alt| c\n"
        "    b -->|main| d\n"
        "    c -->|alt| d\n"
    )
    compute_layout(graph)
    routes = route_edges(graph)

    # Find a route that changes tracks
    _ = [r for r in routes if len(r.points) == 4]
    # At least some routes should be diagonal (track changes)
    # The exact count depends on layout, but we should have some
    assert len(routes) == 4


def test_station_offsets_single_line():
    """Single line on a station should have zero offset."""
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\ngraph LR\n    a -->|main| b\n"
    )
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    assert offsets[("a", "main")] == 0.0
    assert offsets[("b", "main")] == 0.0


def test_station_offsets_multiple_lines():
    """Multiple lines on the same station should get different offsets."""
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro line: alt | Alt | #0000ff\n"
        "graph LR\n"
        "    a -->|main| b\n"
        "    a -->|alt| b\n"
    )
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    assert offsets[("a", "main")] != offsets[("a", "alt")]


def test_exit_only_line_reordered_above():
    """A line originating at a shared station and exiting to a port above
    should get a lower (top) offset than the through-running line.

    Regression test for #125: at bcftools in variant_calling_tuned, the
    QC line should not cross over the Main line.
    """
    mmd = (
        Path(__file__).resolve().parent.parent
        / "examples"
        / "variant_calling_tuned.mmd"
    )
    graph = parse_metro_mermaid(mmd.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)

    # QC exits upward to reporting, so should be above (lower offset) Main
    qc_off = offsets[("bcftools", "qc")]
    main_off = offsets[("bcftools", "main")]
    assert qc_off < main_off, (
        f"QC offset ({qc_off}) should be less than Main ({main_off}) "
        f"at bcftools to avoid crossing"
    )


# --- Inter-section routing tests ---


def test_inter_section_routing():
    """Inter-section edges should be routed through ports."""
    from routing_inter_section import route_inter_section_edges

    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    subgraph sec1 [S1]\n"
        "        a[A]\n"
        "        b[B]\n"
        "        a -->|main| b\n"
        "    end\n"
        "    subgraph sec2 [S2]\n"
        "        c[C]\n"
        "    end\n"
        "    b -->|main| c\n"
    )
    compute_layout(graph)
    inter_routes = route_inter_section_edges(graph)
    # Should have at least one inter-section route (the port-to-port edges)
    assert len(inter_routes) > 0


def test_section_routes_have_valid_points():
    """All routed paths should have at least 2 points."""
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro line: alt | Alt | #0000ff\n"
        "graph LR\n"
        "    subgraph sec1 [S1]\n"
        "        a[A]\n"
        "    end\n"
        "    subgraph sec2 [S2]\n"
        "        b[B]\n"
        "    end\n"
        "    subgraph sec3 [S3]\n"
        "        c[C]\n"
        "    end\n"
        "    a -->|main| b\n"
        "    a -->|alt| c\n"
    )
    compute_layout(graph)
    routes = route_edges(graph)
    for route in routes:
        assert len(route.points) >= 2, (
            f"Route {route.edge.source}->{route.edge.target}"
            f" has {len(route.points)} points"
        )


def test_bypass_routing_around_intervening_sections():
    """Bypass edges spanning 2+ columns should route around intervening sections.

    When a line goes from section A directly to section D (skipping B and C),
    the routed path must be a U-shape that dips below the intervening sections,
    not a straight horizontal line through them. This must work regardless of
    edge declaration order in the .mmd file.
    """
    # 4-section linear pipeline: A -> B -> C -> D
    # "main" goes through all sections, "bypass" skips B and C
    mmd = (
        "%%metro line: main | Main | #ff0000\n"
        "%%metro line: bypass | Bypass | #0000ff\n"
        "graph LR\n"
        "    subgraph sec_a [A]\n"
        "        a1[A1]\n"
        "        a2[A2]\n"
        "        a1 -->|main,bypass| a2\n"
        "    end\n"
        "    subgraph sec_b [B]\n"
        "        b1[B1]\n"
        "        b2[B2]\n"
        "        b1 -->|main| b2\n"
        "    end\n"
        "    subgraph sec_c [C]\n"
        "        c1[C1]\n"
        "        c2[C2]\n"
        "        c1 -->|main| c2\n"
        "    end\n"
        "    subgraph sec_d [D]\n"
        "        d1[D1]\n"
        "        d2[D2]\n"
        "        d1 -->|main,bypass| d2\n"
        "    end\n"
        "    a2 -->|main| b1\n"
        "    b2 -->|main| c1\n"
        "    c2 -->|main| d1\n"
        "    a2 -->|bypass| d1\n"
    )
    graph = parse_metro_mermaid(mmd)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    # Find inter-section bypass routes
    bypass_routes = [r for r in routes if r.line_id == "bypass" and r.is_inter_section]
    assert bypass_routes, "Expected at least one inter-section bypass route"

    # The bypass route spanning the most X distance is the actual bypass
    bypass_route = max(
        bypass_routes, key=lambda r: abs(r.points[-1][0] - r.points[0][0])
    )
    assert len(bypass_route.points) > 2, (
        f"Bypass route should have >2 waypoints (U-shape), "
        f"got {len(bypass_route.points)}: {bypass_route.points}"
    )

    # At least one waypoint should be below the bottom of intervening sections
    intervening_secs = [graph.sections[sid] for sid in ("sec_b", "sec_c")]
    max_section_bottom = max(s.bbox_y + s.bbox_h for s in intervening_secs)
    waypoint_ys = [p[1] for p in bypass_route.points]
    assert any(y > max_section_bottom for y in waypoint_ys), (
        f"No waypoint below intervening sections (bottom={max_section_bottom}). "
        f"Waypoint Ys: {waypoint_ys}"
    )


def _route_endpoint_attached(point, station, sibling_polylines, tol=2.0):
    """True if *point* coincides with *station* or any sibling polyline."""
    import math

    from nf_metro.layout.routing.common import point_on_polyline

    if (
        station is not None
        and math.hypot(point[0] - station.x, point[1] - station.y) < 5.0
    ):
        return True
    return any(
        point_on_polyline(point, pts, tol) is not None for pts in sibling_polylines
    )


def test_merge_branch_lands_on_trunk_y():
    """Merge-junction branch routes must terminate on the trunk bundle Y
    (or on a sibling polyline) -- not hanging in mid-air between rows."""
    from nf_metro.render.svg import apply_route_offsets

    fp = Path(__file__).parent / "fixtures" / "genomeassembly_organellar.mmd"
    graph = parse_metro_mermaid(fp.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    by_line: dict[str, list[list[tuple[float, float]]]] = {}
    for r in routes:
        by_line.setdefault(r.line_id, []).append(apply_route_offsets(r, offsets))

    offences = []
    for r in routes:
        pts = apply_route_offsets(r, offsets)
        if len(pts) < 2:
            continue
        siblings = [p for p in by_line[r.line_id] if p is not pts]
        src = graph.stations.get(r.edge.source)
        tgt = graph.stations.get(r.edge.target)
        if not _route_endpoint_attached(pts[0], src, siblings):
            offences.append(
                f"{r.edge.source}->{r.edge.target} on {r.line_id!r}: "
                f"src {pts[0]} disconnected"
            )
        if not _route_endpoint_attached(pts[-1], tgt, siblings):
            offences.append(
                f"{r.edge.source}->{r.edge.target} on {r.line_id!r}: "
                f"tgt {pts[-1]} disconnected"
            )

    assert not offences, (
        f"{fp.name}: route endpoints hanging in mid-air (not on a "
        f"station marker or sibling route segment):\n  " + "\n  ".join(offences[:5])
    )


def test_l_shape_route_quadrant_symmetry():
    """An L-shape route and its 180-degree mirror must produce mirrored
    geometry: same 4-point shape, same curve radii, with all (x,y) offsets
    from the source negated. This pins down direction handling inside
    ``_route_l_shape`` so future direction-enum refactors cannot silently
    swap a sign on one branch only.
    """

    def _route_one(src_col: int, src_row: int, tgt_col: int, tgt_row: int):
        # Two single-station sections with forced exit/entry sides on the
        # axis between them, so the inter-section edge takes the standard
        # 4-point L-shape (horizontal -> vertical -> horizontal).
        if tgt_col > src_col:
            exit_side, entry_side = "right", "left"
        else:
            exit_side, entry_side = "left", "right"
        graph = parse_metro_mermaid(
            "%%metro line: main | Main | #ff0000\n"
            f"%%metro grid: s1 | {src_col},{src_row}\n"
            f"%%metro grid: s2 | {tgt_col},{tgt_row}\n"
            "graph LR\n"
            "    subgraph s1 [S1]\n"
            f"        %%metro exit: {exit_side} | main\n"
            "        a[A]\n"
            "    end\n"
            "    subgraph s2 [S2]\n"
            f"        %%metro entry: {entry_side} | main\n"
            "        b[B]\n"
            "    end\n"
            "    a -->|main| b\n"
        )
        compute_layout(graph)
        routes = route_edges(graph)
        inter = [r for r in routes if r.is_inter_section and len(r.points) == 4]
        assert len(inter) == 1, f"expected 1 L-shape inter-section route, got {inter}"
        return inter[0]

    # Mirror pair: target down-right vs target up-left, same Manhattan distance.
    rd = _route_one(0, 0, 1, 1)  # dx>0, dy>0  -> R/D
    lu = _route_one(1, 1, 0, 0)  # dx<0, dy<0  -> L/U

    # Mirror in source-relative coordinates.
    rd_rel = [(p[0] - rd.points[0][0], p[1] - rd.points[0][1]) for p in rd.points]
    lu_rel = [(p[0] - lu.points[0][0], p[1] - lu.points[0][1]) for p in lu.points]
    for a, b in zip(rd_rel, lu_rel):
        assert abs(a[0] + b[0]) < 1e-6, f"x mirror broken: {a} vs {b}"
        assert abs(a[1] + b[1]) < 1e-6, f"y mirror broken: {a} vs {b}"

    # Curve radii are direction-agnostic and must match exactly.
    assert rd.curve_radii == lu.curve_radii


def test_around_section_below_dispatched_for_cross_row_left_entry():
    """The around-section-below handler must fire for a LEFT entry port
    reached from an opposite-row source when the short band-above-target
    approach is unusable because its descent would cut through a section.

    A source stacked above a *wider* neighbour cannot drop into the band
    abutting the target (the lead-out channel lands in the neighbour's
    bbox), so ``_left_entry_gap_above_is_clear`` defers and the feed loops
    around below the whole stack instead.
    """
    import nf_metro.layout.routing.inter_section_handlers as ish

    fixture = Path(__file__).parent.parent / "examples" / "topologies"
    fixture = fixture / "corridor_narrow_gap_fallback.mmd"
    graph = parse_metro_mermaid(fixture.read_text())
    compute_layout(graph)

    real = ish._route_around_section_below
    captured: list = []

    def hook(edge, src, tgt, entry_port, i, n, ctx):
        result = real(edge, src, tgt, entry_port, i, n, ctx)
        captured.append(result)
        return result

    ish._route_around_section_below = hook
    try:
        route_edges(graph)
    finally:
        ish._route_around_section_below = real

    assert captured, "_route_around_section_below was not dispatched"
    for r in captured:
        # 6-point R-D-L-U-R shape.
        assert len(r.points) == 6, f"expected 6 points, got {r.points}"
        # All four corners concentric with the same radius (CW loop).
        assert r.curve_radii is not None and len(r.curve_radii) == 4
        assert len(set(r.curve_radii)) == 1, (
            f"around-section corners must share one radius (CW loop); "
            f"got {r.curve_radii}"
        )


@pytest.mark.parametrize(
    ("fixture", "handler"),
    [
        ("around_section_below.mmd", "_route_left_entry_via_gap_above"),
        ("around_below_ep_col_gt0.mmd", "_route_left_entry_via_gap_above"),
        ("self_crossing_bridge.mmd", "_route_left_entry_via_gap_above"),
        ("corridor_narrow_gap_fallback.mmd", "_route_around_section_below"),
        ("genomic_pipeline.mmd", "_route_inter_row_gap_corridor"),
    ],
)
def test_around_and_corridor_routes_built_from_centreline(fixture, handler):
    """The gap-above, around-below and inter-row-corridor handlers route via
    the centreline builder, so each is a 6-point loop with concentric, derived
    corner radii (never hand-rolled) and route_edges' always-on curve
    invariants stay green.
    """
    import nf_metro.layout.routing.inter_section_handlers as ish

    root = Path(__file__).parent.parent / "examples"
    candidate = root / "topologies" / fixture
    path = candidate if candidate.exists() else root / fixture
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)

    real = getattr(ish, handler)
    captured: list = []

    def hook(*args):
        result = real(*args)
        captured.append(result)
        return result

    setattr(ish, handler, hook)
    try:
        # route_edges runs assert_render_curve_invariants on its output; a flip
        # or non-concentric corner from these handlers would raise here.
        route_edges(graph)
    finally:
        setattr(ish, handler, real)

    assert captured, f"{handler} was not dispatched for {fixture}"
    for r in captured:
        assert len(r.points) == 6, f"expected a 6-point loop, got {r.points}"
        assert r.curve_radii is not None and len(r.curve_radii) == 4, (
            f"expected 4 derived corner radii, got {r.curve_radii}"
        )
        assert all(c > 0 for c in r.curve_radii)
        assert r.offset_regime is OffsetRegime.BAKED


@pytest.mark.parametrize(
    "fixture",
    [
        "left_entry_from_above_far.mmd",
        "around_section_below.mmd",
        "around_below_ep_col_gt0.mmd",
        "self_crossing_bridge.mmd",
        "straddling_fanout_junction.mmd",
    ],
)
def test_left_entry_from_above_avoids_canvas_bottom_dive(fixture):
    """A LEFT/far-side entry reached from a row above with a clear band above
    the target takes the short inter-row approach, not a canvas-bottom wrap.

    Its inter-section feed must never descend below the target section's own
    bottom edge -- the mirror of the RIGHT ``_route_right_entry_via_gap_above``
    path -- rather than looping around below the whole stack and running the
    full width back into the port.
    """
    from nf_metro.parser.model import PortSide

    path = Path(__file__).parent.parent / "examples" / "topologies" / fixture
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    routes = route_edges(graph)

    checked = 0
    for r in routes:
        port = graph.ports.get(r.edge.target)
        if port is None or not port.is_entry or port.side is not PortSide.LEFT:
            continue
        src = graph.stations.get(r.edge.source)
        tgt_sec = graph.sections.get(port.section_id)
        if src is None or tgt_sec is None or src.y >= tgt_sec.bbox_y:
            continue
        bottom = tgt_sec.bbox_y + tgt_sec.bbox_h
        max_y = max(p[1] for p in r.points)
        assert max_y <= bottom + 1.0, (
            f"{fixture}: feed {r.edge.source}->{r.edge.target} descends to "
            f"y={max_y:.0f}, below target '{tgt_sec.id}' bottom {bottom:.0f} "
            f"(canvas-bottom dive instead of the band-above approach)"
        )
        checked += 1
    assert checked, f"{fixture}: no LEFT-entry-from-above feed exercised"


def _segment_crosses_bbox_interior(p0, p1, sec, margin=1.0):
    """Whether the axis-aligned segment ``p0``-``p1`` enters *sec*'s interior.

    The interior is the bbox shrunk by *margin* on every edge so a port sitting
    ON the boundary (and its outward approach) does not count as a crossing.
    """
    left = sec.bbox_x + margin
    right = sec.bbox_x + sec.bbox_w - margin
    top = sec.bbox_y + margin
    bottom = sec.bbox_y + sec.bbox_h - margin
    (x0, y0), (x1, y1) = p0, p1
    if abs(y0 - y1) < 1e-6:
        return top < y0 < bottom and min(x0, x1) < right and max(x0, x1) > left
    if abs(x0 - x1) < 1e-6:
        return left < x0 < right and min(y0, y1) < bottom and max(y0, y1) > top
    return False


def test_samerow_far_left_entry_avoids_canvas_bottom_dive():
    """A same-row feed into a far-side LEFT entry wraps the target's shorter side.

    ``psite_id`` (grid row 1) exits LEFT toward ``te`` (same row, immediately to
    its left) whose entry sits on ``te``'s far (left) edge.  The feed must reach
    that port without ploughing ``te``'s interior (a straight run to the far-edge
    port) and without diving below the target's own bottom edge and running the
    full width back -- the same-row sibling of
    :func:`test_left_entry_from_above_avoids_canvas_bottom_dive`.
    """
    from nf_metro.parser.model import PortSide

    path = (
        Path(__file__).parent.parent
        / "examples"
        / "topologies"
        / "samerow_left_exit_far_left_entry.mmd"
    )
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    routes = route_edges(graph)

    checked = 0
    for r in routes:
        port = graph.ports.get(r.edge.target)
        if port is None or not port.is_entry or port.side is not PortSide.LEFT:
            continue
        src = graph.stations.get(r.edge.source)
        tgt_sec = graph.sections.get(port.section_id)
        if src is None or tgt_sec is None:
            continue
        bottom = tgt_sec.bbox_y + tgt_sec.bbox_h
        max_y = max(p[1] for p in r.points)
        assert max_y <= bottom + 1.0, (
            f"feed {r.edge.source}->{r.edge.target} descends to y={max_y:.0f}, "
            f"below target '{tgt_sec.id}' bottom {bottom:.0f} (canvas-bottom "
            f"dive instead of the band-above approach)"
        )
        for p0, p1 in zip(r.points, r.points[1:]):
            assert not _segment_crosses_bbox_interior(p0, p1, tgt_sec), (
                f"feed {r.edge.source}->{r.edge.target} segment {p0}-{p1} "
                f"ploughs target '{tgt_sec.id}' interior to reach its far-edge "
                f"LEFT port"
            )
        checked += 1
    assert checked, "no same-row far-LEFT-entry feed exercised"
