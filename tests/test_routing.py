"""Tests for edge routing."""

from pathlib import Path

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
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
    reached from the opposite-row source when the natural inter-row
    channel would cut through an intervening section's bbox.

    Mirrors a row-0 source to lower-row LEFT-entry geometry: 3-row
    layout with target in the bottom row, source in the top row to the
    right of target, and an intervening section in the middle row whose
    bbox falls in the inter-row channel's Y range.
    """
    import nf_metro.layout.routing.core as core

    fixture = Path(__file__).parent.parent / "examples" / "topologies"
    fixture = fixture / "around_section_below.mmd"
    graph = parse_metro_mermaid(fixture.read_text())
    compute_layout(graph)

    real = core._route_around_section_below
    captured: list = []

    def hook(edge, src, tgt, entry_port, i, n, ctx):
        result = real(edge, src, tgt, entry_port, i, n, ctx)
        captured.append(result)
        return result

    core._route_around_section_below = hook
    try:
        route_edges(graph)
    finally:
        core._route_around_section_below = real

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


# ---------------------------------------------------------------------------
# Stepped-descent concentric bundling (#508)
# ---------------------------------------------------------------------------


def _stepped_descent_ctx(line_ids):
    """Minimal _RoutingCtx + stations triggering a nested stepped descent.

    An oversized source section (col 0) whose right edge sits well right of a
    narrow target column (col 1) entry port, junction-sourced, so a multi-line
    bundle fans down through the stepped staircase.
    """
    from nf_metro.layout.routing import core
    from nf_metro.parser.model import (
        MetroGraph,
        Port,
        PortSide,
        Section,
        Station,
    )

    g = MetroGraph(title="t", style="nfcore")
    g.sections["A"] = Section(
        id="A",
        name="A",
        grid_col=0,
        grid_row=0,
        bbox_x=0.0,
        bbox_y=0.0,
        bbox_w=400.0,
        bbox_h=100.0,
    )
    g.sections["B"] = Section(
        id="B",
        name="B",
        grid_col=1,
        grid_row=0,
        bbox_x=120.0,
        bbox_y=200.0,
        bbox_w=60.0,
        bbox_h=80.0,
    )
    src = Station(id="J", label="", section_id="A", x=50.0, y=50.0)
    g.stations["J"] = src
    ep = Station(id="P", label="", section_id="B", is_port=True, x=150.0, y=230.0)
    g.stations["P"] = ep
    g.ports["P"] = Port(
        id="P", section_id="B", side=PortSide.RIGHT, is_entry=True, x=150.0, y=230.0
    )
    offs = {("J", lid): (k - 1) * 3.0 for k, lid in enumerate(line_ids)}
    offs.update({("P", lid): (k - 1) * 3.0 for k, lid in enumerate(line_ids)})
    ctx = core._RoutingCtx(
        graph=g,
        fold_x=0.0,
        junction_ids={"J"},
        bottom_exit_junctions=set(),
        bottom_exit_junction_ports={},
        offset_step=3.0,
        fork_stations=set(),
        join_stations=set(),
        tb_sections=set(),
        tb_right_entry=set(),
        bundle_info={},
        bypass_gap_idx={},
        station_offsets=offs,
        diagonal_run=20.0,
        curve_radius=10.0,
    )
    return core, ctx, src, ep


def _arc_centre_from_points(pts, corner_idx, r):
    """Arc centre at points[corner_idx+1] for a 90-degree rounded corner."""
    a, c, b = pts[corner_idx], pts[corner_idx + 1], pts[corner_idx + 2]

    def unit(p, q):
        dx, dy = q[0] - p[0], q[1] - p[1]
        m = (dx * dx + dy * dy) ** 0.5 or 1.0
        return dx / m, dy / m

    ti = unit(a, c)
    to = unit(c, b)
    return (c[0] + r * (to[0] - ti[0]), c[1] + r * (to[1] - ti[1]))


def test_stepped_descent_single_line_uses_base_radius():
    from nf_metro.parser.model import Edge

    core, ctx, src, ep = _stepped_descent_ctx(["solo"])
    assert core._should_step_descent(ctx.graph, src, ep, ctx.graph.ports["P"])
    rp = core._route_stepped_descent(Edge("J", "P", "solo"), src, ep, 0, 1, ctx)
    assert rp.curve_radii == [ctx.curve_radius] * 4


def test_stepped_descent_bundle_is_concentric():
    from nf_metro.parser.model import Edge

    lines = ["l0", "l1", "l2"]
    core, ctx, src, ep = _stepped_descent_ctx(lines)
    n = len(lines)
    routes = [
        core._route_stepped_descent(Edge("J", "P", lid), src, ep, i, n, ctx)
        for i, lid in enumerate(lines)
    ]
    # The two middle rungs (corners 1 and 2: down->left, left->down) are fanned
    # by ``spread`` on BOTH legs, so they are genuinely concentric - every
    # line's arc centre must coincide.  (Corners 0 and 3 join a spread-fanned
    # vertical to a port-offset horizontal, so they are transition corners,
    # sized to keep the vertical legs aligned rather than to share a centre.)
    for corner_idx in (1, 2):
        centres = [
            _arc_centre_from_points(rp.points, corner_idx, rp.curve_radii[corner_idx])
            for rp in routes
        ]
        for cx, cy in centres[1:]:
            assert abs(cx - centres[0][0]) < 1e-6
            assert abs(cy - centres[0][1]) < 1e-6
    # All four corners stay nested (radii monotonic across the bundle), so the
    # arcs never cross regardless of the transition-corner sizing.
    for corner_idx in range(4):
        radii = [rp.curve_radii[corner_idx] for rp in routes]
        diffs = [b - a for a, b in zip(radii, radii[1:])]
        assert all(d > 0 for d in diffs) or all(d < 0 for d in diffs)
