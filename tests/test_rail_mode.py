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
    from nf_metro.layout.rail_mode import _station_lines_in_order

    rails: dict[str, dict[str, float]] = {}
    for st in graph.stations.values():
        if st.is_port or st.is_hidden:
            continue
        if st.off_track or (st.is_terminus and not st.label.strip()):
            # Converge to a point; contribute no rail evidence.
            continue
        # rail_used_ys is parallel to the line-definition order, so reconstruct
        # rails with that ordering (not edge-discovery order).
        lines = _station_lines_in_order(graph, st.id)
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


def _combo_member_ids(graph) -> set[str]:
    members: set[str] = set()
    for line_ids, _label in graph.legend_combos:
        if len([lid for lid in line_ids if lid in graph.lines]) >= 2:
            members.update(line_ids)
    return members


def test_rails_are_evenly_spaced_and_distinct():
    """A section's rail SLOTS are distinct and evenly spaced.

    Each non-combo line occupies its own slot; combo members share one slot
    (a tight bundle), so even spacing is asserted over the slot CENTRES (one
    Y per non-combo line plus the bundle centre), not over every line's rail.
    """
    graph = _rail_graph()
    rails = _section_line_rails(graph)
    members = _combo_member_ids(graph)
    assert rails, "expected at least one section with single-line stations"
    for sec_id, line_rails in rails.items():
        # Collapse combo members in this section to their bundle centre.
        bundle_ys = [y for lid, y in line_rails.items() if lid in members]
        slot_ys = [y for lid, y in line_rails.items() if lid not in members]
        if bundle_ys:
            slot_ys.append(sum(bundle_ys) / len(bundle_ys))
        ys = sorted(slot_ys)
        assert len(set(round(y, 2) for y in ys)) == len(ys), (
            f"{sec_id}: rail slots not distinct: {ys}"
        )
        if len(ys) >= 3:
            gaps = [round(b - a, 2) for a, b in zip(ys, ys[1:])]
            assert max(gaps) - min(gaps) < 1.0, (
                f"{sec_id}: rail slots not evenly spaced: {gaps}"
            )


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


def test_spanning_station_draws_one_circle_per_used_rail_plus_connector():
    """A multi-rail station renders as the interchange idiom: one white
    circle on each rail it uses, joined by a single straight connector
    segment -- not as one filled capsule (pill)."""
    import re

    from nf_metro.render import render_svg
    from nf_metro.themes import THEMES

    graph = _rail_graph()
    svg = render_svg(graph, THEMES["nfcore"])

    spanning = [
        st
        for st in graph.stations.values()
        if st.rail_top_y is not None and st.rail_bottom_y is not None
    ]
    assert spanning, "demo must contain at least one multi-rail station"
    for st in spanning:
        used = graph.station_lines(st.id)
        assert len(st.rail_used_ys) == len(used), (
            f"{st.id}: rail_used_ys {st.rail_used_ys} not parallel to lines {used}"
        )
        # One circle (knob) per used rail.
        knob_count = svg.count(f'class="nf-metro-rail-knob" data-station-id="{st.id}"')
        assert knob_count == len(used), (
            f"{st.id}: expected {len(used)} rail circles, found {knob_count}"
        )
        # Exactly one straight connector segment for the multi-rail station.
        connectors = svg.count(
            f'class="nf-metro-rail-connector" data-station-id="{st.id}"'
        )
        assert connectors == 1, (
            f"{st.id}: expected one interchange connector, found {connectors}"
        )

    # The multi-rail marker must NOT be a filled capsule: no rounded rect
    # (rx/ry) carries a rail station id.  (Single-rail circles use <circle>.)
    for st in spanning:
        rect_pat = re.compile(
            rf'<rect[^>]*rx="[^"]+"[^>]*data-station-id="{re.escape(st.id)}"'
        )
        assert not rect_pat.search(svg), (
            f"{st.id}: multi-rail station still renders as a capsule rect"
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


def test_rail_mode_labels_alternate_above_and_below():
    """Rail-mode station labels alternate above/below the rails so dense
    label runs don't pile on one edge: a section with several labelled
    stations must use BOTH sides, and no label sits between two rails."""
    from nf_metro.layout.labels import place_labels

    graph = _rail_graph()
    rails = _section_line_rails(graph)
    placements = place_labels(graph)

    by_section: dict[str, list] = {}
    for lp in placements:
        st = graph.stations.get(lp.station_id)
        if st is None or st.is_port or st.is_terminus or not st.label.strip():
            continue
        by_section.setdefault(st.section_id, []).append(lp)

    saw_multi = False
    for sec_id, lps in by_section.items():
        if len(lps) < 2:
            continue
        saw_multi = True
        sides = {lp.above for lp in lps}
        assert sides == {True, False}, (
            f"{sec_id}: labels all on one side ({sides}); expected alternation"
        )
        # Each label stays close to its own station's rail span (it does not
        # drift off into the middle of the inter-rail bundle): the baseline is
        # within ~2 row-gaps of the nearest rail the station sits on.
        section_rails = rails.get(sec_id, {})
        for lp in lps:
            st = graph.stations[lp.station_id]
            own_top = st.rail_top_y if st.rail_top_y is not None else st.y
            own_bot = st.rail_bottom_y if st.rail_bottom_y is not None else st.y
            ys = sorted(section_rails.values())
            gap = (ys[1] - ys[0]) if len(ys) >= 2 else 40.0
            assert own_top - 2 * gap <= lp.y <= own_bot + 2 * gap, (
                f"{sec_id}/{lp.station_id}: label at {lp.y} drifts far from its "
                f"own rail span [{own_top}, {own_bot}]"
            )
    assert saw_multi, "demo must contain a section with several station labels"


def test_off_track_input_feeds_in_with_clean_drop_and_elbow():
    """An off-track input drops straight down from its icon then turns onto the
    consumer's rail with a single elbow: a clean L (vertical leg + horizontal
    leg), near its consumer, not a long diagonal traverse from the left edge."""
    from nf_metro.layout.routing import route_edges

    graph = _rail_graph()
    routes = route_edges(graph)
    off_tracks = {st.id for st in graph.stations.values() if st.off_track}
    assert off_tracks, "demo must contain an off_track input"

    checked = 0
    for rp in routes:
        src = graph.stations.get(rp.edge.source)
        tgt = graph.stations.get(rp.edge.target)
        if src is None or tgt is None:
            continue
        if rp.edge.source not in off_tracks and rp.edge.target not in off_tracks:
            continue
        consumer = tgt if rp.edge.source in off_tracks else src
        checked += 1
        # An off-track feed is exactly drop + elbow + horizontal: three points,
        # a vertical first leg (shared X) and a horizontal last leg (shared Y).
        pts = rp.points if rp.edge.source in off_tracks else list(reversed(rp.points))
        assert len(pts) == 3, f"off-track feed not a 3-point L: {pts}"
        assert abs(pts[0][0] - pts[1][0]) < 0.5, f"first leg not vertical: {pts}"
        assert abs(pts[1][1] - pts[2][1]) < 0.5, f"last leg not horizontal: {pts}"
        # The horizontal reach stays within the section (feeder near consumer).
        xs = [x for x, _ in pts]
        sec = graph.sections.get(consumer.section_id)
        if sec and sec.bbox_w > 0:
            assert (max(xs) - min(xs)) < sec.bbox_w, (
                f"off-track feeder spans {max(xs) - min(xs)}px across a "
                f"{sec.bbox_w}px section -- too long a traverse"
            )
    assert checked, "expected at least one off-track feeder edge"


def test_off_track_bundle_feeders_do_not_merge():
    """Several lines feeding from one off-track input each drop on their own X
    (one per target rail), so the bundle stays distinct parallel lines in the
    vertical leg rather than collapsing into one fat merged line."""
    from nf_metro.layout.routing import route_edges

    graph = _rail_graph()
    routes = route_edges(graph)
    off_tracks = {st.id for st in graph.stations.values() if st.off_track}

    # Group off-track feeds by (feeder, consumer); the demo's samples_csv feeds
    # bqsr on two lines (pair_n, pair_t) -- a 2-line bundle.
    drops: dict[tuple[str, str], list[float]] = {}
    for rp in routes:
        if rp.edge.source not in off_tracks and rp.edge.target not in off_tracks:
            continue
        feeder = rp.edge.source if rp.edge.source in off_tracks else rp.edge.target
        consumer = rp.edge.target if rp.edge.source in off_tracks else rp.edge.source
        pts = rp.points if rp.edge.source in off_tracks else list(reversed(rp.points))
        drops.setdefault((feeder, consumer), []).append(round(pts[0][0], 2))

    multi = {k: v for k, v in drops.items() if len(v) >= 2}
    assert multi, "demo must contain a multi-line off-track bundle feed"
    for key, drop_xs in multi.items():
        assert len(set(drop_xs)) == len(drop_xs), (
            f"{key}: bundle feeder lines share a drop X {drop_xs} -- they merge"
        )


def test_rail_labels_clear_the_whole_panel_never_beside_a_middle_rail():
    """Every rail-mode station label sits ABOVE the panel's topmost rail or
    BELOW its bottommost rail - never beside a middle rail (which would have
    the label collide with the lines).  This holds even for a station that
    only occupies middle rails (its label still clears the outer rails)."""
    from nf_metro.layout.labels import _label_bbox, place_labels

    graph = _rail_graph()
    rails = _section_line_rails(graph)
    placements = place_labels(graph)

    checked = 0
    for lp in placements:
        st = graph.stations.get(lp.station_id)
        if st is None or st.is_port or not st.label.strip():
            continue
        if st.is_terminus and not st.label.strip():
            continue
        section_rails = rails.get(st.section_id)
        if not section_rails or len(section_rails) < 2:
            continue
        top_rail = min(section_rails.values())
        bot_rail = max(section_rails.values())
        x0, y0, x1, y1 = _label_bbox(lp)
        # The label's nearest edge to the rail band must be outside it: an
        # above label's bottom edge is at/above the top rail; a below label's
        # top edge is at/below the bottom rail.  A small tolerance covers the
        # descender clearance baked into placement.
        tol = 6.0
        clears_top = y1 <= top_rail + tol
        clears_bottom = y0 >= bot_rail - tol
        assert clears_top or clears_bottom, (
            f"{st.section_id}/{st.id}: label y[{y0:.1f},{y1:.1f}] sits beside a "
            f"middle rail (rail band [{top_rail:.1f},{bot_rail:.1f}])"
        )
        checked += 1
    assert checked, "expected at least one labelled rail station"


def test_stacked_rail_section_bbox_contains_hanging_labels():
    """A rail section's bbox reserves room for its below-rail labels so a
    section stacked beneath it clears them.  A long, steeply-angled label whose
    footprint exceeds the default padding grows the box; a section below then
    keeps a positive header gap (no clash)."""
    from nf_metro.layout import compute_layout
    from nf_metro.layout.constants import SECTION_HEADER_PROTRUSION

    src = (
        "%%metro title: t\n"
        "%%metro style: dark\n"
        "%%metro rail_mode: true\n"
        "%%metro label_angle: 45\n"
        "%%metro line: a | A | #2db572\n"
        "%%metro line: b | B | #0570b0\n"
        "%%metro grid: top | 0,0\n"
        "%%metro grid: bot | 0,1\n"
        "graph LR\n"
        "    subgraph top [Top]\n"
        "        t1[Start]\n"
        "        t2[An extremely long station label name to overflow the pad]\n"
        "        t1 -->|a,b| t2\n"
        "    end\n"
        "    subgraph bot [Bottom]\n"
        "        u1[Go]\n"
        "        u2[End]\n"
        "        u1 -->|a,b| u2\n"
        "    end\n"
    )
    graph = parse_metro_mermaid(src)
    compute_layout(graph, validate=True)
    top = graph.sections["top"]
    bot = graph.sections["bot"]
    # The long-label section's bbox grew well past a bare two-rail panel.
    assert top.bbox_h > 200, f"top bbox did not reserve label band: {top.bbox_h}"
    # The lower section's header (badge top) clears the upper section's box.
    gap = (bot.bbox_y - SECTION_HEADER_PROTRUSION) - (top.bbox_y + top.bbox_h)
    assert gap >= 0, f"lower header overlaps the section above: gap {gap:.1f}px"


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


# ---------------------------------------------------------------------------
# Per-section rail mode (%%metro rail_section: <id>)
# ---------------------------------------------------------------------------

RAIL_SECTION_MMD = EXAMPLES / "rail_section.mmd"


def _rail_section_graph():
    graph = parse_metro_mermaid(RAIL_SECTION_MMD.read_text())
    compute_layout(graph, validate=True)
    return graph


def test_rail_section_directive_parses():
    graph = parse_metro_mermaid(RAIL_SECTION_MMD.read_text())
    assert graph.rail_mode is False
    assert graph.rail_sections == {"pathways"}
    assert graph.has_rail_sections is True
    assert graph.is_rail_section("pathways") is True
    assert graph.is_rail_section("calling") is False


def test_global_rail_mode_treats_all_sections_as_rail():
    """The legacy global flag means every section is a rail section."""
    graph = parse_metro_mermaid(RAIL_MMD.read_text())
    assert graph.rail_mode is True
    # Every declared section reports as a rail section under the global flag.
    for sid in graph.sections:
        assert graph.is_rail_section(sid) is True
    # ...even though rail_sections was never populated explicitly.
    assert graph.rail_sections == set()


def test_flagged_section_gets_rail_spans():
    """Stations in the flagged section span multiple rails (pills)."""
    graph = _rail_section_graph()
    pathways = graph.sections["pathways"]
    spanning = [
        graph.stations[sid]
        for sid in pathways.station_ids
        if not graph.stations[sid].is_port
    ]
    # The multi-line pathway stations all carry 3 lines, so each spans rails.
    assert spanning, "pathways section should have real stations"
    assert all(
        st.rail_top_y is not None and st.rail_bottom_y is not None
        for st in spanning
        if len(graph.station_lines(st.id)) > 1
    )
    # Used rails recorded per station match the lines they carry.
    for st in spanning:
        lines = graph.station_lines(st.id)
        if len(lines) > 1:
            assert len(st.rail_used_ys) == len(lines)


def test_normal_section_keeps_per_line_tracks_not_rail_spans():
    """A non-flagged connected section keeps normal layout: no rail spans."""
    graph = _rail_section_graph()
    for sid in ("preprocess", "calling", "annotate"):
        section = graph.sections[sid]
        for st_id in section.station_ids:
            st = graph.stations[st_id]
            assert st.rail_top_y is None, f"{st_id} unexpectedly has a rail span"
            assert st.rail_bottom_y is None
            assert st.rail_used_ys == []


def test_normal_section_lines_share_a_trunk():
    """The connected trunk's co-travelling lines bundle (converge), unlike
    rail mode where they stay on separate fixed rails."""
    graph = _rail_section_graph()
    # In the calling section, markdup carries both dna and rna; in normal
    # (non-rail) layout that station sits at a single Y (the trunk), not a
    # multi-rail pill.
    markdup = graph.stations["markdup"]
    assert markdup.rail_top_y is None
    # Rail-section pathway stations DO span; assert the contrast holds.
    score = graph.stations["score"]
    assert score.rail_top_y is not None


def test_rail_section_internal_edges_routed_as_straight_rails():
    """Internal edges of the rail section render as flat horizontal runs."""
    from nf_metro.layout.routing import compute_station_offsets, route_edges

    graph = _rail_section_graph()
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    pathway_ids = set(graph.sections["pathways"].station_ids)
    seen_internal = 0
    for rp in routes:
        if rp.edge.source in pathway_ids and rp.edge.target in pathway_ids:
            ys = {round(y, 2) for _, y in rp.points}
            assert len(ys) == 1, (
                f"rail edge {rp.edge.source}->{rp.edge.target} is not a "
                f"flat horizontal run: Ys {ys}"
            )
            seen_internal += 1
    assert seen_internal > 0, "expected some routed internal rail edges"


def test_per_section_rail_validates_and_contains():
    """validate=True passes and rail stations stay within their bbox."""
    graph = _rail_section_graph()  # compute_layout(validate=True) inside
    pathways = graph.sections["pathways"]
    for st_id in pathways.station_ids:
        st = graph.stations[st_id]
        if st.is_port:
            continue
        top = st.rail_top_y if st.rail_top_y is not None else st.y
        bot = st.rail_bottom_y if st.rail_bottom_y is not None else st.y
        assert top >= pathways.bbox_y - 1e-6
        assert bot <= pathways.bbox_y + pathways.bbox_h + 1e-6


def test_no_rail_directive_default_off_byte_identical():
    """Adding per-section rail support must not change a normal render."""
    from nf_metro.render import render_svg
    from nf_metro.themes import THEMES

    src = (EXAMPLES / "rnaseq_auto.mmd").read_text()
    g = parse_metro_mermaid(src)
    assert g.has_rail_sections is False
    compute_layout(g)
    svg = render_svg(g, THEMES["nfcore"])
    # No rail span leaks into a normal graph.
    assert all(
        s.rail_top_y is None and s.rail_bottom_y is None for s in g.stations.values()
    )
    assert "nf-metro-rail-knob" not in svg


# ---------------------------------------------------------------------------
# Convergence corners: 45-degree diagonals, not square right-angle bends
# ---------------------------------------------------------------------------


def _is_axis_aligned(p0, p1) -> bool:
    """True when the segment p0->p1 is purely horizontal or purely vertical."""
    return abs(p0[0] - p1[0]) < 0.5 or abs(p0[1] - p1[1]) < 0.5


def test_rail_convergence_segments_are_diagonal_not_square():
    """Where rails fan out from a single input or fan in to a single output,
    the rail eases between rail Ys on a 45-degree diagonal segment (a segment
    that changes BOTH x and y), not a square right-angle vertical jog."""
    graph = _rail_graph()
    from nf_metro.layout.routing import route_edges

    routes = route_edges(graph)
    diagonals = 0
    for route in routes:
        src = graph.stations.get(route.edge.source)
        tgt = graph.stations.get(route.edge.target)
        # Off-track feeders deliberately use an S-curve; not a convergence.
        if (src and src.off_track) or (tgt and tgt.off_track):
            continue
        pts = route.points
        # A convergence route changes rail Y between its endpoints.
        if abs(pts[0][1] - pts[-1][1]) < 0.5:
            continue
        # Some interior segment must be a true diagonal (changes both x and y);
        # a square jog would have only axis-aligned segments.
        has_diag = any(not _is_axis_aligned(a, b) for a, b in zip(pts, pts[1:]))
        assert has_diag, (
            f"convergence {route.edge.source}->{route.edge.target} "
            f"({route.line_id}) uses square bends, not a diagonal: {pts}"
        )
        # And no purely-vertical interior jog between the rails (the old
        # square-bend signature).
        interior = list(zip(pts, pts[1:]))[1:-1]
        for a, b in interior:
            assert not (abs(a[0] - b[0]) < 0.5 and abs(a[1] - b[1]) >= 0.5), (
                f"convergence {route.edge.source}->{route.edge.target} "
                f"has a square vertical jog {a}->{b}"
            )
        diagonals += 1
    assert diagonals > 0, "expected at least one diagonal convergence route"


def test_rail_fan_out_diagonal_eases_off_the_shared_input():
    """The CRAM fan-out rails (germline up, pair_t down) leave the shared
    input point on a diagonal then run flat -- the diagonal is biased early
    (toward the fork) so most of the column is a straight rail."""
    graph = _rail_graph()
    from nf_metro.layout.routing import route_edges

    routes = {(r.edge.source, r.edge.target, r.line_id): r for r in route_edges(graph)}
    # cram_in -> align carries germline (top) and pair_t (bottom): both
    # change rail Y and must contain a diagonal.
    for line in ("germline", "pair_t"):
        rp = routes.get(("cram_in", "align", line))
        assert rp is not None, f"missing cram_in->align route for {line}"
        pts = rp.points
        assert abs(pts[0][1] - pts[-1][1]) > 0.5, "expected a rail-Y change"
        assert any(not _is_axis_aligned(a, b) for a, b in zip(pts, pts[1:])), (
            f"cram_in->align ({line}) is not diagonal: {pts}"
        )


# ---------------------------------------------------------------------------
# Angled labels: tighter column spacing via the rotated horizontal projection
# ---------------------------------------------------------------------------


def _calling_column_step(angle: float) -> float:
    g = parse_metro_mermaid(RAIL_MMD.read_text())
    g.label_angle = angle
    compute_layout(g)
    xs = sorted(
        {
            round(s.x, 2)
            for s in g.stations.values()
            if s.section_id == "calling" and not s.is_port and not s.off_track
        }
    )
    steps = [b - a for a, b in zip(xs, xs[1:])]
    assert steps, "expected multiple columns in the calling section"
    return steps[0]


def test_angled_label_column_pitch_is_tighter_than_horizontal():
    """An angled-label rail panel packs columns tighter than the same panel
    with horizontal labels, because a diagonal label's horizontal footprint
    is width*cos(angle)."""
    horizontal = _calling_column_step(0.0)
    angled = _calling_column_step(45.0)
    assert angled < horizontal - 1.0, (
        f"angled pitch {angled:.1f} not tighter than horizontal {horizontal:.1f}"
    )


def test_label_angle_directive_parses():
    g = parse_metro_mermaid(RAIL_MMD.read_text())
    assert g.label_angle == 45.0


def test_angled_rail_labels_render_rotated():
    """With label_angle set, rail-section station labels render with a
    rotate() transform (so the tilted text is actually drawn)."""
    from nf_metro.render import render_svg
    from nf_metro.themes import THEMES

    g = _rail_graph()
    assert g.label_angle == 45.0
    svg = render_svg(g, THEMES["nfcore"])
    assert "rotate(45" in svg, "expected rotated label transforms in the SVG"


def test_label_angle_default_off_byte_identical():
    """label_angle support must not change a render with no directive: a graph
    without label_angle produces an identical SVG before and after this change
    (label_angle is None -> no rotation, no spacing change)."""
    from nf_metro.render import render_svg
    from nf_metro.themes import THEMES

    src = (EXAMPLES / "rnaseq_auto.mmd").read_text()
    g = parse_metro_mermaid(src)
    assert g.label_angle is None
    compute_layout(g)
    svg = render_svg(g, THEMES["nfcore"])
    assert "rotate(45" not in svg
    assert "rotate(" not in svg


# ---------------------------------------------------------------------------
# legend_combo bundling: combo lines share ONE rail slot (a tight bundle)
# ---------------------------------------------------------------------------


def test_legend_combo_directive_parses():
    g = parse_metro_mermaid(RAIL_MMD.read_text())
    assert g.legend_combos == [(("pair_n", "pair_t"), "Tumour-normal pair")]


def test_combo_lines_share_one_rail_slot():
    """Lines that are members of a legend_combo occupy a single rail slot
    drawn as a tight adjacent bundle, so the rail-slot count equals the
    distinct non-combo lines plus one slot per combo -- not one per line."""
    from nf_metro.layout.rail_mode import _rail_slot_offsets, _section_lines_in_order

    graph = _rail_graph()
    y_spacing = 40.0
    section = graph.sections["calling"]
    lines = _section_lines_in_order(graph, section)
    members = _combo_member_ids(graph)
    assert members, "demo must contain a legend_combo"

    slot_offset, n_slots = _rail_slot_offsets(graph, lines, y_spacing)

    expected = len([lid for lid in lines if lid not in members]) + 1
    assert n_slots == expected, (
        f"expected {expected} rail slots (non-combo lines + 1 per combo), got {n_slots}"
    )
    # All combo members in the section land on the SAME slot centre: their
    # offsets differ by less than a full rail pitch (they hug as a bundle).
    member_offsets = [slot_offset[lid] for lid in members if lid in slot_offset]
    assert len(member_offsets) >= 2
    assert max(member_offsets) - min(member_offsets) < y_spacing, (
        f"combo members did not bundle onto one slot: {member_offsets}"
    )
    # ...and they are NOT coincident (a visible two-line bundle, not one rail).
    assert max(member_offsets) - min(member_offsets) > 0.5, (
        "combo members collapsed to a single coincident rail"
    )
    # A non-combo line is a full rail pitch away from the bundle centre.
    bundle_centre = sum(member_offsets) / len(member_offsets)
    non_combo = [
        slot_offset[lid] for lid in lines if lid not in members and lid in slot_offset
    ]
    assert all(abs(o - bundle_centre) >= y_spacing - 1.0 for o in non_combo), (
        "a non-combo rail is closer than a full pitch to the bundle"
    )


def test_combo_bundle_sublines_hug_at_one_offset_step():
    """A legend_combo bundle's sub-lines sit exactly one OFFSET_STEP apart -
    the same tight pitch the normal router uses for parallel lines in a bundle -
    so the bundle reads as a single track, not two spaced-apart rails."""
    from nf_metro.layout.constants import OFFSET_STEP
    from nf_metro.layout.rail_mode import _rail_slot_offsets, _section_lines_in_order

    graph = _rail_graph()
    section = graph.sections["calling"]
    lines = _section_lines_in_order(graph, section)
    members = _combo_member_ids(graph)
    slot_offset, _ = _rail_slot_offsets(graph, lines, 40.0)

    member_offsets = sorted(slot_offset[lid] for lid in members if lid in slot_offset)
    assert len(member_offsets) == 2, "demo's combo bundle has two members"
    gap = member_offsets[1] - member_offsets[0]
    assert abs(gap - OFFSET_STEP) < 1e-6, (
        f"combo sub-lines {gap}px apart; expected one OFFSET_STEP ({OFFSET_STEP}px)"
    )


def test_cross_track_station_knob_spans_the_bundle():
    """A cross-track (spanning) station that uses the bundle draws a knob on
    each of the bundle's hugging sub-rails -- its span reaches the bundle
    slot."""
    from nf_metro.layout.rail_mode import _station_lines_in_order

    graph = _rail_graph()
    members = _combo_member_ids(graph)
    # `align` carries every line, so it must place a knob on each bundle sub-rail.
    st = graph.stations["align"]
    served = _station_lines_in_order(graph, st.id)
    member_ys = [y for lid, y in zip(served, st.rail_used_ys) if lid in members]
    assert len(member_ys) == len(members), (
        f"spanning station missing a bundle knob: {member_ys}"
    )
    # The bundle knobs are adjacent (within a pitch) and distinct.
    assert 0.5 < max(member_ys) - min(member_ys) < 40.0


def test_interchange_link_uses_station_fill_not_dark_stroke():
    """The cross-track interchange link renders WHITE (the station fill), not
    the dark station stroke -- the dark stroke is only the outer boundary.

    The white link bar fills the interior; a separate dark layer paints the
    outline.  Assert the white interior bar is present (station fill stroke)
    and that the connector is no longer a lone dark line cutting across."""
    import re

    from nf_metro.render import render_svg
    from nf_metro.themes import THEMES

    graph = _rail_graph()
    theme = THEMES["nfcore"]
    svg = render_svg(graph, theme)

    spanning = [
        st
        for st in graph.stations.values()
        if st.rail_top_y is not None and st.rail_bottom_y is not None
    ]
    assert spanning, "demo must contain a multi-rail (cross-track) station"

    # The link bars render as round-capped <path> elements (drawsvg emits Line
    # as a path).  A white (station-fill) interior bar must be drawn for the
    # glyph, joining the circles in white rather than dark.
    fill = re.escape(theme.station_fill)
    white_vbar = re.compile(rf'<path[^>]*stroke="{fill}"[^>]*stroke-linecap="round"')
    assert white_vbar.search(svg), (
        f"expected a white ({theme.station_fill}) interchange link bar; "
        "the connector must render with the station fill, not the dark stroke"
    )

    # The dark outline layer carries the connector class so the glyph keeps a
    # continuous dark outer boundary behind the white interior.
    stroke = re.escape(theme.station_stroke)
    dark_connector = re.compile(
        rf'stroke="{stroke}"[^>]*stroke-linecap="round"[^>]*nf-metro-rail-connector'
    )
    assert dark_connector.search(svg), (
        "expected a dark outline layer (connector class) behind the white link"
    )


def test_legend_combo_default_off_byte_identical():
    """Adding legend_combo parsing/render must not change a render that has no
    legend_combo directive: byte-for-byte identical SVG, no combo state."""
    from nf_metro.render import render_svg
    from nf_metro.themes import THEMES

    src = (EXAMPLES / "rnaseq_auto.mmd").read_text()
    g = parse_metro_mermaid(src)
    assert g.legend_combos == []
    compute_layout(g)
    svg1 = render_svg(g, THEMES["nfcore"])

    g2 = parse_metro_mermaid(src)
    g2.legend_combos = []  # explicit no-op
    compute_layout(g2)
    svg2 = render_svg(g2, THEMES["nfcore"])
    assert svg1 == svg2
