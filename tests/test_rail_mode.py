"""Invariant tests for opt-in rail mode (parallel rails + spanning pills)."""

from __future__ import annotations

from pathlib import Path

from nf_metro.layout import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

RAIL_MMD = EXAMPLES / "rail_mode.mmd"


def _rail_graph():
    graph = parse_metro_mermaid(RAIL_MMD.read_text())
    assert graph.rail_mode is True
    compute_layout(graph, validate=True)
    return graph


def _section_line_rails(graph):
    """Map each section id -> {line_id: rail_y} derived from station Ys.

    A line's rail Y is the common Y at which every station carrying ONLY that
    line sits.  We reconstruct it from single-line stations plus the spanning
    pills' top/bottom rails.
    """
    rails: dict[str, dict[str, float]] = {}
    for st in graph.stations.values():
        if st.is_port or st.is_hidden:
            continue
        if st.off_track or (st.is_terminus and not st.label.strip()):
            # Converge to a point; contribute no rail evidence.
            continue
        lines = graph.station_lines(st.id)
        if len(lines) == 1:
            rails.setdefault(st.section_id, {})[lines[0]] = st.y
        elif st.rail_used_ys and len(st.rail_used_ys) == len(lines):
            # A spanning pill records each used line's rail Y directly, which
            # lets us reconstruct rails even in sections with no single-line
            # stations.
            for lid, y in zip(lines, st.rail_used_ys):
                rails.setdefault(st.section_id, {})[lid] = y
    return rails


def test_rail_mode_parses_directive():
    graph = parse_metro_mermaid(RAIL_MMD.read_text())
    assert graph.rail_mode is True


def test_each_line_runs_on_a_single_fixed_rail():
    """Every station carrying a given line meets it at that line's fixed
    rail Y, so the line is a straight horizontal run across the section."""
    graph = _rail_graph()

    # Per-section, per-line, collect the Y at which each station meets the
    # line: single-rail stations contribute their y; spanning pills
    # contribute the interpolated rail Y for the line within their span.
    from nf_metro.layout.routing.rail import _line_rail_y

    by_section_line: dict[tuple[str, str], set[float]] = {}
    for st in graph.stations.values():
        if st.is_port or st.is_hidden:
            continue
        # Off-track inputs sit above the rails and blank termini converge the
        # rails to their icon; both legitimately meet lines off the rail Y.
        if st.off_track or (st.is_terminus and not st.label.strip()):
            continue
        for lid in graph.station_lines(st.id):
            y = _line_rail_y(graph, st.id, lid)
            by_section_line.setdefault((st.section_id, lid), set()).add(round(y, 2))

    offenders = [
        f"{sec}/{lid}: rails at {sorted(ys)}"
        for (sec, lid), ys in by_section_line.items()
        if len(ys) > 1
    ]
    assert not offenders, (
        "each line must meet every station on a single fixed rail Y: "
        + "; ".join(offenders)
    )


def test_rails_are_evenly_spaced_and_distinct():
    """A section's lines occupy distinct, evenly-spaced rails."""
    graph = _rail_graph()
    rails = _section_line_rails(graph)
    assert rails, "expected at least one section with single-line stations"
    for sec_id, line_rails in rails.items():
        ys = sorted(line_rails.values())
        assert len(set(ys)) == len(ys), f"{sec_id}: rails not distinct: {ys}"
        if len(ys) >= 3:
            gaps = [round(b - a, 2) for a, b in zip(ys, ys[1:])]
            assert len(set(gaps)) == 1, f"{sec_id}: rails not evenly spaced: {gaps}"


def test_multi_line_station_span_covers_exactly_its_lines_rails():
    """A spanning pill's drawn span (rail_top_y..rail_bottom_y) equals the
    Y range of the rails its lines occupy -- no more, no less."""
    graph = _rail_graph()
    rails = _section_line_rails(graph)

    for st in graph.stations.values():
        if st.is_port or st.is_hidden:
            continue
        # Off-track inputs and blank termini converge to a point (an icon /
        # an above-rail feeder), so they are deliberately not spanning pills.
        if st.off_track or (st.is_terminus and not st.label.strip()):
            assert st.rail_top_y is None and st.rail_bottom_y is None, (
                f"{st.id}: off-track/blank-terminus must not be a spanning pill"
            )
            continue
        lines = graph.station_lines(st.id)
        line_rails = rails.get(st.section_id, {})
        used_ys = [line_rails[lid] for lid in lines if lid in line_rails]
        if len(used_ys) < 2:
            # Single-rail station: must not be a spanning pill.
            assert st.rail_top_y is None and st.rail_bottom_y is None, (
                f"{st.id}: single-rail station marked as spanning"
            )
            continue
        assert st.rail_top_y is not None and st.rail_bottom_y is not None, (
            f"{st.id}: multi-line station not marked spanning"
        )
        assert abs(st.rail_top_y - min(used_ys)) < 1.0, (
            f"{st.id}: top rail {st.rail_top_y} != min used rail {min(used_ys)}"
        )
        assert abs(st.rail_bottom_y - max(used_ys)) < 1.0, (
            f"{st.id}: bottom rail {st.rail_bottom_y} != max used rail {max(used_ys)}"
        )


def test_lines_do_not_converge_to_a_point():
    """In rail mode no two distinct rails collapse to a shared Y at any
    station -- the hallmark of the parallel-rails (non-converging) look."""
    graph = _rail_graph()
    rails = _section_line_rails(graph)
    for sec_id, line_rails in rails.items():
        ys = list(line_rails.values())
        assert len(set(round(y, 2) for y in ys)) == len(ys), (
            f"{sec_id}: rails converged to shared Ys: {sorted(ys)}"
        )


def test_rail_routes_are_straight_horizontal_at_rail_y():
    """Every routed edge in rail mode is a straight horizontal run at its
    line's rail Y (all waypoints share one Y)."""
    graph = _rail_graph()
    from nf_metro.layout.routing import route_edges
    from nf_metro.layout.routing.rail import _line_rail_y

    routes = route_edges(graph)
    assert routes
    for route in routes:
        src = graph.stations.get(route.edge.source)
        tgt = graph.stations.get(route.edge.target)
        # Off-track feeders deliberately leave the rail with an S-curve;
        # exempt them from the straight-horizontal invariant.
        if (src and src.off_track) or (tgt and tgt.off_track):
            assert len(route.points) >= 3, (
                f"off_track feeder {route.edge.source}->{route.edge.target} "
                "should be a multi-point S-curve"
            )
            continue
        ys = {round(y, 2) for _, y in route.points}
        src_y = _line_rail_y(graph, route.edge.source, route.line_id)
        tgt_y = _line_rail_y(graph, route.edge.target, route.line_id)
        if abs(src_y - tgt_y) < 0.5:
            assert len(ys) == 1, (
                f"{route.edge.source}->{route.edge.target} ({route.line_id}) "
                f"not horizontal: Ys {ys}"
            )
        else:
            # endpoints on different rails: jog uses exactly the two rail Ys
            assert ys <= {round(src_y, 2), round(tgt_y, 2)}, (
                f"jog uses unexpected Ys {ys}"
            )


def test_rail_mode_stations_within_bbox():
    """All stations (incl. spanning pill extents) stay within their section
    bbox -- the always-on containment guard passes under validate=True (run
    in _rail_graph) and pills don't overflow."""
    graph = _rail_graph()
    for st in graph.stations.values():
        if st.is_port or st.is_hidden or not st.section_id:
            continue
        sec = graph.sections.get(st.section_id)
        if sec is None or sec.bbox_w <= 0:
            continue
        top = st.rail_top_y if st.rail_top_y is not None else st.y
        bot = st.rail_bottom_y if st.rail_bottom_y is not None else st.y
        assert sec.bbox_y - 1.0 <= top, f"{st.id} top {top} above bbox {sec.bbox_y}"
        assert bot <= sec.bbox_y + sec.bbox_h + 1.0, (
            f"{st.id} bottom {bot} below bbox {sec.bbox_y + sec.bbox_h}"
        )


def test_spanning_pill_has_a_knob_per_used_rail():
    """A multi-line spanning station records one used-rail Y per line it
    carries, and the renderer draws a knob (a circle) at each."""
    from nf_metro.render import render_svg
    from nf_metro.themes import THEMES

    graph = _rail_graph()
    svg = render_svg(graph, THEMES["nfcore"])

    spanning = [
        st
        for st in graph.stations.values()
        if st.rail_top_y is not None and st.rail_bottom_y is not None
    ]
    assert spanning, "demo must contain at least one spanning pill"
    for st in spanning:
        used = graph.station_lines(st.id)
        assert len(st.rail_used_ys) == len(used), (
            f"{st.id}: rail_used_ys {st.rail_used_ys} not parallel to lines {used}"
        )
        # One knob marker per used line for this station.
        knobs = svg.count(f'data-station-id="{st.id}"')
        # The pill rect + one knob per used line all carry the station id.
        assert knobs >= len(used) + 1, (
            f"{st.id}: expected pill + {len(used)} knobs, found {knobs} markers"
        )


def test_knob_absent_on_a_rail_the_span_crosses_but_does_not_use():
    """A pill that spans a rail belonging to a line it does NOT use draws no
    knob on that rail -- the rail reads as passing behind the pill."""
    graph = _rail_graph()

    rails = _section_line_rails(graph)
    found = False
    for st in graph.stations.values():
        if st.rail_top_y is None or st.rail_bottom_y is None:
            continue
        used = set(graph.station_lines(st.id))
        line_rails = rails.get(st.section_id, {})
        # Lines whose rail falls strictly inside this pill's span.
        crossing = {
            lid
            for lid, y in line_rails.items()
            if st.rail_top_y - 0.5 < y < st.rail_bottom_y + 0.5
        }
        passing_behind = crossing - used
        if not passing_behind:
            continue
        found = True
        # None of the passing-behind lines may have a used-rail Y on this
        # station (no knob), while every used line must.
        for lid in passing_behind:
            assert line_rails[lid] not in [
                round(y, 2) for y in (round(v, 2) for v in st.rail_used_ys)
            ], f"{st.id}: knob drawn on non-used rail for {lid}"
    assert found, "demo must contain a pill that spans a rail of a line it does not use"


def test_blank_terminus_renders_icon_not_pill():
    """A blank file-terminus serving several lines renders as its file icon
    (a path with the terminus fold), not as a bare spanning pill."""
    from nf_metro.render import render_svg
    from nf_metro.themes import THEMES

    graph = _rail_graph()
    svg = render_svg(graph, THEMES["nfcore"])

    blank_termini = [
        st
        for st in graph.stations.values()
        if st.is_terminus and not st.label.strip() and not st.is_port
    ]
    assert blank_termini, "demo must contain a blank file-terminus"
    for st in blank_termini:
        # A blank terminus must not be marked as a spanning pill.
        assert st.rail_top_y is None and st.rail_bottom_y is None, (
            f"{st.id}: blank terminus marked as spanning pill"
        )
    # The CRAM/CSV/VCF icon chip labels must appear as rendered text.
    for chip in ("CRAM", "CSV", "VCF"):
        assert chip in svg, f"expected file-icon chip {chip!r} in rail-mode SVG"


def test_stacked_sections_do_not_overlap():
    """Two or more stacked rail-mode sections occupy disjoint vertical bands
    (bbox-wise), so neither section's content reaches into the other."""
    graph = _rail_graph()
    boxes = sorted(
        (
            (s.bbox_y, s.bbox_y + s.bbox_h, s.id)
            for s in graph.sections.values()
            if s.bbox_h > 0 and (not s.is_implicit or s.station_ids)
        ),
        key=lambda b: b[0],
    )
    assert len(boxes) >= 2, "demo must contain at least two stacked sections"
    for (top_a, bot_a, id_a), (top_b, bot_b, id_b) in zip(boxes, boxes[1:]):
        assert bot_a <= top_b + 0.5, (
            f"sections {id_a} and {id_b} overlap: {bot_a} > {top_b}"
        )


def test_off_track_input_sits_above_the_rails():
    """An off_track input station is parked above every rail in its section
    (it feeds in with an S-curve rather than sitting on a rail)."""
    graph = _rail_graph()
    rails = _section_line_rails(graph)
    off_tracks = [st for st in graph.stations.values() if st.off_track]
    assert off_tracks, "demo must contain an off_track input"
    for st in off_tracks:
        section_rails = rails.get(st.section_id, {})
        if not section_rails:
            continue
        assert st.y < min(section_rails.values()) - 0.5, (
            f"{st.id}: off_track station at {st.y} not above rails "
            f"{sorted(section_rails.values())}"
        )


def test_rail_mode_off_by_default_leaves_graph_unchanged():
    """A representative graph laid out with rail mode OFF is byte-for-byte
    the same as today: no rail spans are set, and the same SVG is produced
    whether or not the (unset) rail_mode flag is touched."""
    from nf_metro.render import render_svg
    from nf_metro.themes import THEMES

    src = (EXAMPLES / "rnaseq_auto.mmd").read_text()

    g1 = parse_metro_mermaid(src)
    assert g1.rail_mode is False
    compute_layout(g1)
    svg1 = render_svg(g1, THEMES["nfcore"])

    # No station should carry a rail span when rail mode is off.
    assert all(
        s.rail_top_y is None and s.rail_bottom_y is None for s in g1.stations.values()
    )

    g2 = parse_metro_mermaid(src)
    g2.rail_mode = False  # explicit no-op
    compute_layout(g2)
    svg2 = render_svg(g2, THEMES["nfcore"])

    assert svg1 == svg2
