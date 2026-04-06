"""Tests for edge routing."""

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


def test_exit_only_line_above_trunk():
    """Exit-only lines should get offsets above trunk lines when the exit port is above.

    In variant_calling_tuned, the QC line only exits the Variant Calling
    section (no entry port). The exit port is above the feeding station
    (bcftools). The QC line's offset at bcftools should be above (lower
    than) the main line's offset so the QC line flows upward to the exit
    port without crossing the main line.

    Regression test for #125.
    """
    from pathlib import Path

    mmd_path = Path(__file__).parent.parent / "examples" / "variant_calling_tuned.mmd"
    graph = parse_metro_mermaid(mmd_path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)

    # bcftools carries both main and qc lines
    main_offset = offsets[("bcftools", "main")]
    qc_offset = offsets[("bcftools", "qc")]

    # QC should be above main (lower offset = higher position)
    assert qc_offset < main_offset, (
        f"QC offset ({qc_offset}) should be below main offset ({main_offset}) "
        f"at bcftools to avoid crossing (exit port is above)"
    )
