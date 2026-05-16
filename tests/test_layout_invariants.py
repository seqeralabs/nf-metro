"""Cross-section layout invariants for inter-section bundle alignment.

These tests assert that the row trunk Y is consistent across sections in
the same grid row, that symmetric-fan column-mates land at mirrored Ys,
and that off-track inputs sit above their consumer's trunk.  They catch
regressions where one section's trunk drifts from the row's anchor (the
"limma kink" bug) or where fan re-centering leaves stations asymmetric.

The fixtures exercise real pipeline graphs with multi-line bundles,
fan-out columns, and off-track inputs (differentialabundance) plus a
two-section grid with simpler topology (variant calling).
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, PortSide

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

# Tolerance for "same Y" assertions.  The grid pitch defaults to 55px;
# 1px slack absorbs sub-pixel rounding from fan-recenter phases.
_Y_TOL = 1.0


def _layout(fixture: str, **kwargs) -> MetroGraph:
    """Parse a fixture file and run the full layout pipeline."""
    text = (FIXTURES / fixture).read_text()
    graph = parse_metro_mermaid(text)
    graph.center_ports = True
    compute_layout(graph, **kwargs)
    return graph


def _layout_example(name: str, **kwargs) -> MetroGraph:
    """Parse an example file and run layout, honouring its own directives."""
    text = (EXAMPLES / name).read_text()
    graph = parse_metro_mermaid(text)
    compute_layout(graph, **kwargs)
    return graph


def _row_lr_sections(graph: MetroGraph) -> dict[int, list]:
    """Group LR/RL sections by grid_row, skipping row-spanners."""
    rows: dict[int, list] = defaultdict(list)
    for sec in graph.sections.values():
        if (
            sec.bbox_h <= 0
            or sec.grid_row < 0
            or sec.direction not in ("LR", "RL")
            or sec.grid_row_span > 1
        ):
            continue
        rows[sec.grid_row].append(sec)
    return rows


def _section_lr_port_ys(graph: MetroGraph, section) -> list[float]:
    """Return Y values of the section's LR (LEFT/RIGHT) ports."""
    ys: list[float] = []
    for pid in list(section.entry_ports) + list(section.exit_ports):
        port = graph.ports.get(pid)
        st = graph.stations.get(pid)
        if (
            port is not None
            and st is not None
            and port.side in (PortSide.LEFT, PortSide.RIGHT)
        ):
            ys.append(st.y)
    return ys


def _section_trunk_marker_cy(
    graph: MetroGraph,
    section,
    offsets: dict[tuple[str, str], float],
) -> float | None:
    """Render-time cy of the trunk station that anchors the row bundle.

    The trunk station is the one whose marker the inter-section bundle
    passes through.  We approximate it as the full-bundle internal
    station whose marker centre (station.y + (min_off + max_off) / 2)
    is closest to the section's LR port Y.

    Returns ``None`` when no full-bundle station exists internally
    (e.g. a section whose bundle exits via a non-trunk station).
    """
    port_ys = _section_lr_port_ys(graph, section)
    if not port_ys:
        return None
    port_y = port_ys[0]
    bundle = _section_full_bundle(graph, section)
    if not bundle:
        return None
    port_set = set(section.entry_ports) | set(section.exit_ports)
    best: tuple[float, float] | None = None  # (distance, cy)
    for sid in section.station_ids:
        if sid in port_set:
            continue
        st = graph.stations.get(sid)
        if st is None or st.is_port or st.is_hidden:
            continue
        lines = graph.station_lines(sid)
        if set(lines) != bundle:
            continue
        line_offs = [offsets.get((sid, lid), 0.0) for lid in lines]
        if not line_offs:
            continue
        cy = st.y + (min(line_offs) + max(line_offs)) / 2
        dist = abs(cy - port_y)
        if best is None or dist < best[0]:
            best = (dist, cy)
    return best[1] if best is not None else None


def _section_full_bundle(graph: MetroGraph, section) -> set[str] | None:
    """The set of line ids that traverse the section's row bundle.

    Defined as the line set carried by the section's LR ports.
    """
    port_lines: set[str] = set()
    has_lr_port = False
    for pid in list(section.entry_ports) + list(section.exit_ports):
        port = graph.ports.get(pid)
        if port is None or port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        has_lr_port = True
        port_lines.update(graph.station_lines(pid))
    return port_lines if (has_lr_port and port_lines) else None


# ---------------------------------------------------------------------------
# Row trunk Y consistency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd", "rnaseq_sections.mmd"])
def test_row_trunk_marker_cy_consistent(fixture):
    """All same-row LR sections must render their trunk marker at the
    same cy.  Inter-section bundles run horizontally between sections
    in the same grid row; a per-section drift in the trunk marker's
    rendered cy produces a visible kink at the section boundary.

    This is the regression test for the "9px limma kink" bug where
    section 2's trunk station sat 9px below sections 1 and 5 because
    of a stray bundle offset shift triggered by a side-branch feeder
    on the section's exit port.
    """
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    rows = _row_lr_sections(graph)
    for row, sections in rows.items():
        cys: list[tuple[str, float]] = []
        for sec in sections:
            cy = _section_trunk_marker_cy(graph, sec, offsets)
            if cy is not None:
                cys.append((sec.id, cy))
        if len(cys) < 2:
            continue
        target = cys[0][1]
        for sid, cy in cys[1:]:
            assert abs(cy - target) < _Y_TOL, (
                f"Row {row}: section {sid} trunk cy={cy} drifts from "
                f"{cys[0][0]} cy={target}"
            )


# ---------------------------------------------------------------------------
# Symmetric fan column-mate Y equality
# ---------------------------------------------------------------------------


def _section_fan_columns(graph: MetroGraph, section) -> dict[float, list[str]]:
    """Group full-bundle internal stations of a section by X column.

    Returns ``{x: [station_id, ...]}`` for columns with >= 2 full-bundle
    stations - the configurations that the symmetric-fan phases target.
    """
    bundle = _section_full_bundle(graph, section)
    if not bundle:
        return {}
    port_set = set(section.entry_ports) | set(section.exit_ports)
    cols: dict[float, list[str]] = defaultdict(list)
    for sid in section.station_ids:
        if sid in port_set:
            continue
        st = graph.stations.get(sid)
        if st is None or st.is_port or st.is_hidden or st.off_track:
            continue
        if set(graph.station_lines(sid)) != bundle:
            continue
        cols[round(st.x, 1)].append(sid)
    return {x: sids for x, sids in cols.items() if len(sids) >= 2}


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_symfan_pairs_share_y(fixture):
    """When a section has exactly two full-bundle stations in the same
    column (a classic symmetric-fan pair such as Reporting's Shiny app
    + Quarto report, or Functional's GSEA + decoupler), the pair must
    be mirrored around the row's trunk Y so the rendered cys are
    equidistant from the trunk.

    Stronger property than "pair has matching Y": catches asymmetric
    placements like (trunk-55, trunk+0) that leave the bottom-fan slot
    empty.
    """
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    for sec in graph.sections.values():
        cols = _section_fan_columns(graph, sec)
        trunk_cy = _section_trunk_marker_cy(graph, sec, offsets)
        if trunk_cy is None:
            continue
        for x, sids in cols.items():
            if len(sids) != 2:
                continue  # Only assert on pairs; 3+ has its own ordering
            cys = []
            for sid in sids:
                st = graph.stations[sid]
                lines = graph.station_lines(sid)
                line_offs = [offsets.get((sid, lid), 0.0) for lid in lines]
                cys.append(st.y + (min(line_offs) + max(line_offs)) / 2)
            cys.sort()
            above_gap = trunk_cy - cys[0]
            below_gap = cys[1] - trunk_cy
            assert abs(above_gap - below_gap) < _Y_TOL, (
                f"Section {sec.id} column x={x}: pair cys={cys} not "
                f"mirrored around trunk cy={trunk_cy} "
                f"(above_gap={above_gap}, below_gap={below_gap})"
            )


# ---------------------------------------------------------------------------
# Off-track inputs sit above their consumer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_off_track_inputs_above_consumer(fixture):
    """Off-track input stations (declared via ``%%metro off_track:``)
    must sit at least one ``y_spacing`` slot above their on-track
    consumer.  Catches the regression where ``_lift_off_track_stations``
    leaves an off-track input on the same Y as its consumer (or below).
    """
    graph = _layout(fixture)
    junction_ids = set(graph.junctions)
    # Build off_track -> consumer map from edges
    consumer_of: dict[str, str] = {}
    for edge in graph.edges:
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if (
            src is None
            or tgt is None
            or not src.off_track
            or src.is_port
            or src.id in junction_ids
            or tgt.is_port
            or tgt.id in junction_ids
            or tgt.off_track
        ):
            continue
        consumer_of.setdefault(src.id, tgt.id)

    assert consumer_of, f"{fixture}: no off-track edges found"

    for off_id, consumer_id in consumer_of.items():
        off_st = graph.stations[off_id]
        cons_st = graph.stations[consumer_id]
        assert off_st.y < cons_st.y - _Y_TOL, (
            f"Off-track {off_id} y={off_st.y} not above consumer "
            f"{consumer_id} y={cons_st.y}"
        )


# ---------------------------------------------------------------------------
# Stacked file-input icons leave room for under-icon captions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "example",
    ["differentialabundance.mmd", "differentialabundance_default.mmd"],
)
def test_stacked_file_icons_label_clearance(example):
    """Two vertically-adjacent file-input stations sharing a column must
    sit far enough apart that the upper station's under-icon caption
    doesn't crash into the top edge of the lower icon.

    The default station pitch (``y_spacing`` ~ 40 px) is shorter than
    the captioned icon's vertical extent (~icon_height + caption_gap +
    caption_font_height = 32 + 4 + ~8 = 44 px).  Catches the regression
    where stacked source inputs in DA section 1 (Samples/Contrasts,
    Matrix, GTF, CEL, MaxQuant, GEO ID) have their captions visibly
    overlapping the next icon.
    """
    from nf_metro.layout.constants import (
        ICON_CAPTION_FONT_HEIGHT,
        ICON_CAPTION_GAP,
        ICON_HALF_HEIGHT,
        ICON_STACK_LABEL_CLEARANCE,
    )

    required_pitch = (
        2 * ICON_HALF_HEIGHT
        + ICON_CAPTION_GAP
        + ICON_CAPTION_FONT_HEIGHT
        + ICON_STACK_LABEL_CLEARANCE
    )

    graph = _layout_example(example)
    junction_ids = set(graph.junctions)

    def _has_caption(station) -> bool:
        if not station.is_terminus:
            return False
        return any(bool(n) for n in (station.terminus_names or []))

    # Group captioned terminus stations by section + column.
    by_col: dict[tuple[str, float], list[str]] = defaultdict(list)
    for sid, st in graph.stations.items():
        if (
            st.is_port
            or sid in junction_ids
            or st.section_id is None
            or not _has_caption(st)
        ):
            continue
        by_col[(st.section_id, round(st.x, 1))].append(sid)

    tested = False
    for (sec_id, col_x), sids in by_col.items():
        if len(sids) < 2:
            continue
        tested = True
        sids.sort(key=lambda s: graph.stations[s].y)
        for upper_id, lower_id in zip(sids, sids[1:]):
            upper = graph.stations[upper_id]
            lower = graph.stations[lower_id]
            gap = lower.y - upper.y
            assert gap + _Y_TOL >= required_pitch, (
                f"{example} section {sec_id} col x={col_x}: "
                f"file-icon pair {upper_id} (y={upper.y}) -> "
                f"{lower_id} (y={lower.y}) gap={gap:.2f} px "
                f"< required {required_pitch:.2f} px "
                f"(2*icon_half + caption_gap + caption_font + clearance)"
            )

    assert tested, f"{example}: no captioned file-icon column with two icons"


# ---------------------------------------------------------------------------
# Off-track icons ordered top-to-bottom by their consumer Y
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "example",
    ["differentialabundance.mmd", "differentialabundance_default.mmd"],
)
def test_off_track_icons_ordered_by_consumer_y(example):
    """Within a section, the Y order of off-track input icons must
    match the Y order of their on-track consumers.

    When several off-track inputs feed different consumers in the same
    section, the icon for the upper consumer (smaller consumer Y) must
    sit above the icon for the lower consumer.  Catches the regression
    where placement followed mmd declaration order rather than consumer
    position, leaving the network icon above the gene-sets icon even
    though the network's consumer (decoupler) sits below the gene-sets
    consumer (GSEA).
    """
    graph = _layout_example(example)
    junction_ids = set(graph.junctions)

    # Build off_track -> in-section consumer map from edges.
    consumer_of: dict[str, str] = {}
    for edge in graph.edges:
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if (
            src is None
            or tgt is None
            or not src.off_track
            or src.is_port
            or src.id in junction_ids
            or tgt.is_port
            or tgt.id in junction_ids
            or tgt.off_track
            or src.section_id != tgt.section_id
        ):
            continue
        consumer_of.setdefault(src.id, tgt.id)

    # Group off-track stations by section.
    by_section: dict[str, list[str]] = defaultdict(list)
    for off_id in consumer_of:
        sid = graph.stations[off_id].section_id
        if sid is not None:
            by_section[sid].append(off_id)

    # Need at least one section with two distinct consumers to test
    # the ordering invariant.
    tested = False
    for sec_id, off_ids in by_section.items():
        distinct_consumers = {consumer_of[o] for o in off_ids}
        if len(distinct_consumers) < 2:
            continue
        tested = True
        # Sort off-track stations by their own Y (top to bottom).
        sorted_offs = sorted(off_ids, key=lambda o: graph.stations[o].y)
        # The consumer Ys, in the same order, must be non-decreasing.
        cons_ys = [graph.stations[consumer_of[o]].y for o in sorted_offs]
        for i in range(len(cons_ys) - 1):
            assert cons_ys[i] <= cons_ys[i + 1] + _Y_TOL, (
                f"{example} section {sec_id}: off-track icon order "
                f"does not match consumer Y order.  Icons (top->bottom): "
                f"{[(o, graph.stations[o].y) for o in sorted_offs]}; "
                f"their consumer Ys: {cons_ys}"
            )

    assert tested, f"{example}: no section with multiple off-track consumers"


# ---------------------------------------------------------------------------
# Bundle offsets must not jump at a section boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_no_kink_at_section_boundary(fixture):
    """Adjacent same-row LR sections must agree on the rendered cy
    of the row bundle's pass-through stations.  This catches the
    "limma kink" pattern: matrix_filter (data_prep exit) at cy=110.5
    but limma (differential entry) at cy=119.5, a 9px diagonal line
    visually breaking the horizontal trunk.

    The check pairs each section's exit port with the next section's
    entry port and asserts they share a Y at the rendered (offset-
    adjusted) level.
    """
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    rows = _row_lr_sections(graph)
    for row, sections in rows.items():
        sorted_secs = sorted(sections, key=lambda s: s.grid_col)
        for sec, nxt in zip(sorted_secs, sorted_secs[1:]):
            if nxt.grid_col - sec.grid_col != 1:
                continue
            # Exit port of sec
            for pid in sec.exit_ports:
                port = graph.ports.get(pid)
                if port is None or port.side != PortSide.RIGHT:
                    continue
                exit_lines = graph.station_lines(pid)
                if not exit_lines:
                    continue
                exit_offs = [offsets.get((pid, lid), 0.0) for lid in exit_lines]
                exit_cy = graph.stations[pid].y + (min(exit_offs) + max(exit_offs)) / 2
                # Matching entry port of next section
                for npid in nxt.entry_ports:
                    nport = graph.ports.get(npid)
                    if nport is None or nport.side != PortSide.LEFT:
                        continue
                    entry_lines = graph.station_lines(npid)
                    entry_offs = [offsets.get((npid, lid), 0.0) for lid in entry_lines]
                    entry_cy = (
                        graph.stations[npid].y + (min(entry_offs) + max(entry_offs)) / 2
                    )
                    assert abs(exit_cy - entry_cy) < _Y_TOL, (
                        f"Row {row}: exit port {pid} cy={exit_cy} != "
                        f"entry port {npid} cy={entry_cy}"
                    )


# ---------------------------------------------------------------------------
# Side-branch single-line edges stay off the trunk inside the section
# ---------------------------------------------------------------------------


def _section_for(graph: MetroGraph, sid: str):
    """Return the section a station belongs to (or None)."""
    st = graph.stations.get(sid)
    if st is None or st.section_id is None:
        return None
    return graph.sections.get(st.section_id)


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_side_branch_edge_stays_off_trunk(fixture):
    """A side-branch single-line exit edge must keep its own track
    inside the section instead of joining the main trunk bundle
    immediately after the source station.

    For each non-port internal station S that sits clearly off the
    section's trunk Y (a side branch) and feeds the section's exit
    port or another internal station on the trunk via a single
    outgoing line, walking the routed path from the source forward
    must keep the line on the source Y for at least half of the
    horizontal distance to the target.  The diagonal/climb to the
    trunk must therefore start past the path midpoint between source
    and target, not within the first quarter as the propd regression
    produced.

    Catches the propd regression where the rnaseq line from propd
    climbed to trunk Y immediately after the station, leaving the
    side-branch slot empty for the rest of the section and visually
    merging with the main bundle.
    """
    graph = _layout(fixture)
    routes = route_edges(graph)

    rows = _row_lr_sections(graph)
    section_trunk_y: dict[str, float] = {}
    for sections in rows.values():
        for sec in sections:
            port_ys = _section_lr_port_ys(graph, sec)
            if port_ys:
                section_trunk_y[sec.id] = port_ys[0]

    # Build per-station outbound edges with line set
    outbound: dict[str, list] = defaultdict(list)
    for edge in graph.edges:
        outbound[edge.source].append(edge)

    junction_ids = set(graph.junctions)
    asserted = 0
    for sid, st in graph.stations.items():
        if st.is_port or st.off_track or sid in junction_ids:
            continue
        sec = _section_for(graph, sid)
        if sec is None:
            continue
        trunk_y = section_trunk_y.get(sec.id)
        if trunk_y is None:
            continue
        # Side branch: clearly off the trunk Y (> 2 grid slot offsets).
        if abs(st.y - trunk_y) < 6.0:
            continue
        # Single-line source only.
        src_lines = graph.station_lines(sid)
        if len(src_lines) != 1:
            continue
        for edge in outbound[sid]:
            # Find the matching routed path
            rp = next(
                (
                    r
                    for r in routes
                    if r.edge.source == edge.source
                    and r.edge.target == edge.target
                    and r.edge.line_id == edge.line_id
                ),
                None,
            )
            if rp is None or len(rp.points) < 2:
                continue
            tgt = graph.stations.get(edge.target)
            if tgt is None:
                continue
            tgt_port = graph.ports.get(edge.target)
            same_sec_target = tgt.section_id == sec.id and not tgt.is_port
            is_exit_port = (
                tgt_port is not None
                and not tgt_port.is_entry
                and tgt_port.section_id == sec.id
                and tgt_port.side in (PortSide.LEFT, PortSide.RIGHT)
            )
            if not (same_sec_target or is_exit_port):
                continue
            # Target must sit at or near trunk Y (where the bundle lives).
            if abs(tgt.y - trunk_y) > 6.0:
                continue
            # Walk the path from source: find the X at which the path
            # leaves the source's Y (where the climb starts).
            pts = rp.points
            src_x, src_y = pts[0]
            tgt_x = tgt.x
            leave_x: float | None = None
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                if abs(y0 - src_y) < _Y_TOL and abs(y1 - src_y) >= _Y_TOL:
                    leave_x = x0
                    break
                if abs(y1 - src_y) >= _Y_TOL:
                    leave_x = x1
                    break
            if leave_x is None:
                continue
            # The climb must start past 30% of the source->target run.
            # Pre-fix routes climbed within the first 15% (the diagonal
            # sat near the source under the standard fork bias).
            run = tgt_x - src_x
            if abs(run) < 1.0:
                continue
            climb_frac = (leave_x - src_x) / run
            assert climb_frac >= 0.30 - 1e-6, (
                f"Side-branch edge {edge.source}->{edge.target} "
                f"({edge.line_id}) climbs at x={leave_x:.2f} "
                f"({climb_frac:.0%} of source->target run); expected "
                f">= 30% (src_x={src_x:.2f}, tgt_x={tgt_x:.2f}, "
                f"section={sec.id})"
            )
            asserted += 1
    assert asserted > 0, f"{fixture}: no side-branch single-line exits found to test"


# ---------------------------------------------------------------------------
# Section bbox must contain all stations and off-track inputs
# ---------------------------------------------------------------------------


# Default terminus icon and station marker half-heights from the theme,
# used to verify section bboxes enclose every station's vertical reach.
_ICON_HALF_HEIGHT = 16.0
_MARKER_HALF_HEIGHT = 9.5


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd", "rnaseq_sections.mmd"])
def test_section_bbox_contains_all_content(fixture):
    """Every section's bbox must contain its on-track stations and any
    off-track input icons.  Catches the regression where an off-track
    input is re-anchored above the section's bbox top so the icon
    spills outside the section background.

    Margin: on-track station markers reach ~9.5 px above the centre,
    file-input icons reach ~16 px above the centre.  We assert
    ``station.y - reach >= bbox_y - 0.5`` (sub-pixel tolerance) and
    ``station.y + reach <= bbox_y + bbox_h + 0.5``.
    """
    graph = _layout(fixture)
    junction_ids = set(graph.junctions)

    for sec_id, section in graph.sections.items():
        if section.bbox_h <= 0:
            continue
        for sid in section.station_ids:
            st = graph.stations.get(sid)
            if st is None or st.is_port or sid in junction_ids:
                continue
            half = _ICON_HALF_HEIGHT if st.off_track else _MARKER_HALF_HEIGHT
            top = st.y - half
            bot = st.y + half
            assert top >= section.bbox_y - 0.5, (
                f"Section {sec_id}: station {sid} top={top} "
                f"(y={st.y}, half={half}) overflows bbox top "
                f"y={section.bbox_y}"
            )
            assert bot <= section.bbox_y + section.bbox_h + 0.5, (
                f"Section {sec_id}: station {sid} bottom={bot} "
                f"(y={st.y}, half={half}) overflows bbox bottom "
                f"y={section.bbox_y + section.bbox_h}"
            )


# ---------------------------------------------------------------------------
# Sections with empty above-trunk bands but multiple movable siblings below
# should auto-balance so the top band shrinks to one y_spacing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_section_top_band_filled(fixture):
    """LR/RL sections with room for another above-trunk slot AND
    multiple below-trunk movable siblings should fill the empty top
    band, not leave it stranded.
    """
    y_spacing = 55.0
    label_clearance = y_spacing / 2
    graph = _layout(fixture, y_spacing=y_spacing)

    for section in graph.sections.values():
        if section.bbox_h <= 0 or section.direction not in ("LR", "RL"):
            continue
        bundle = _section_full_bundle(graph, section)
        if not bundle:
            continue
        port_ids = set(section.entry_ports) | set(section.exit_ports)
        cols: dict[float, list[str]] = defaultdict(list)
        for sid in section.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.is_hidden or st.off_track:
                continue
            cols[round(st.x, 1)].append(sid)

        trunk_y: float | None = None
        for pid in section.entry_ports + section.exit_ports:
            port = graph.ports.get(pid)
            st = graph.stations.get(pid)
            if port and st and port.side in (PortSide.LEFT, PortSide.RIGHT):
                trunk_y = st.y
                break
        if trunk_y is None:
            for sids in cols.values():
                for s in sids:
                    if set(graph.station_lines(s)) == bundle:
                        trunk_y = graph.stations[s].y
                        break
                if trunk_y is not None:
                    break
        if trunk_y is None:
            continue

        all_internal = [s for sids in cols.values() for s in sids]
        if not all_internal:
            continue
        top_y = min(graph.stations[s].y for s in all_internal)
        top_band = top_y - section.bbox_y
        if top_band <= y_spacing + _Y_TOL:
            continue

        movable_above = 0
        movable_below_candidates: list[str] = []
        for _x, sids in cols.items():
            trunks_here = [s for s in sids if set(graph.station_lines(s)) == bundle]
            if not trunks_here:
                continue
            for s in sids:
                if s in trunks_here:
                    continue
                lines = set(graph.station_lines(s))
                if not lines or not (lines < bundle):
                    continue
                y = graph.stations[s].y
                if y < trunk_y - _Y_TOL:
                    movable_above += 1
                elif y > trunk_y + _Y_TOL:
                    movable_below_candidates.append(s)

        if len(movable_below_candidates) < 2 or movable_above >= len(
            movable_below_candidates
        ):
            continue

        target_y = top_y - y_spacing
        any_fits = any(
            target_y
            >= section.bbox_y
            + (
                label_clearance
                if graph.stations[s].label and graph.stations[s].label.strip()
                else 0.0
            )
            - _Y_TOL
            for s in movable_below_candidates
        )
        if not any_fits:
            continue

        assert top_band <= y_spacing + _Y_TOL, (
            f"Section {section.id}: top band {top_band:.1f}px > "
            f"{y_spacing:.1f} while {len(movable_below_candidates)} "
            f"movable siblings sit below trunk and only "
            f"{movable_above} above; balance pass should lift one "
            f"into the top slot"
        )


# ---------------------------------------------------------------------------
# Section 1 (data_prep): at least one input must sit above the trunk
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_section1_input_above_trunk(fixture):
    """In ``data_prep`` (the source-stack section) inputs must fill
    the above-trunk band: at least one input sits above the trunk, and
    the topmost input is no more than y_spacing below the bbox top.
    """
    y_spacing = 55.0
    graph = _layout(fixture, y_spacing=y_spacing)
    section = graph.sections.get("data_prep")
    assert section is not None
    port_ids = set(section.entry_ports) | set(section.exit_ports)
    trunk_y: float | None = None
    for pid in section.entry_ports + section.exit_ports:
        port = graph.ports.get(pid)
        st = graph.stations.get(pid)
        if port and st and port.side in (PortSide.LEFT, PortSide.RIGHT):
            trunk_y = st.y
            break
    assert trunk_y is not None, "data_prep has no LR port for trunk Y"

    has_in: set[str] = {e.target for e in graph.edges}
    inputs = [
        sid
        for sid in section.station_ids
        if sid not in port_ids
        and sid not in has_in
        and sid in graph.stations
        and not graph.stations[sid].is_port
    ]
    inputs_above = [sid for sid in inputs if graph.stations[sid].y < trunk_y - _Y_TOL]
    assert inputs_above, (
        f"data_prep: no input sits above trunk_y={trunk_y:.1f} "
        f"(inputs at y={[graph.stations[s].y for s in inputs]})"
    )
    top_input_y = min(graph.stations[s].y for s in inputs_above)
    top_band = top_input_y - section.bbox_y
    assert top_band <= y_spacing + _Y_TOL, (
        f"data_prep: top input at y={top_input_y:.1f} leaves "
        f"top_band={top_band:.1f}px > {y_spacing:.1f} (bbox_y="
        f"{section.bbox_y:.1f}); balance pass should lift another "
        f"input into the top slot"
    )


# ---------------------------------------------------------------------------
# Terminus stations must not be hit by a diagonal route segment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_terminus_not_directly_after_diagonal(fixture):
    """Routes terminating at an output terminus must arrive on an
    orthogonal (horizontal or vertical) final segment.
    """
    MIN_LEN = 30.0
    AXIS_TOL = 1.0
    graph = _layout(fixture)
    routes = route_edges(graph)
    by_target: dict[str, list] = defaultdict(list)
    for r in routes:
        tgt = graph.stations.get(r.edge.target)
        if tgt is None or not tgt.is_terminus:
            continue
        by_target[r.edge.target].append(r)

    for tid, paths in by_target.items():
        sources = {r.edge.source for r in paths}
        if len(sources) < 2:
            continue
        for r in paths:
            pts = r.points
            if len(pts) < 2:
                continue
            for i in range(len(pts) - 1, 0, -1):
                x1, y1 = pts[i - 1]
                x2, y2 = pts[i]
                length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
                if length < MIN_LEN:
                    continue
                dx = abs(x2 - x1)
                dy = abs(y2 - y1)
                axis_aligned = dx <= AXIS_TOL or dy <= AXIS_TOL
                assert axis_aligned, (
                    f"Terminus {tid}: edge {r.edge.source}->{tid} "
                    f"last segment ({x1:.1f},{y1:.1f}) -> "
                    f"({x2:.1f},{y2:.1f}) is diagonal "
                    f"(dx={dx:.1f}, dy={dy:.1f})"
                )
                break


# ---------------------------------------------------------------------------
# Station markers and off-track file icons must never overlap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd", "rnaseq_sections.mmd"])
def test_no_station_or_icon_overlap(fixture):
    """No two station marker bboxes (including off-track file icons)
    may overlap; otherwise one station hides another in the rendered
    SVG."""
    from nf_metro.layout.engine import _station_marker_bbox

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    boxes: list[tuple[str, tuple[float, float, float, float]]] = []
    for sid in graph.stations:
        b = _station_marker_bbox(graph, sid, offsets=offsets)
        if b is not None:
            boxes.append((sid, b))

    tol = 0.5
    for i, (s1, (x1, y1, X1, Y1)) in enumerate(boxes):
        for s2, (x2, y2, X2, Y2) in boxes[i + 1 :]:
            overlap = (
                x1 < X2 - tol and x2 < X1 - tol and y1 < Y2 - tol and y2 < Y1 - tol
            )
            assert not overlap, (
                f"{fixture}: marker overlap between {s1!r} "
                f"bbox=({x1:.1f},{y1:.1f},{X1:.1f},{Y1:.1f}) "
                f"and {s2!r} "
                f"bbox=({x2:.1f},{y2:.1f},{X2:.1f},{Y2:.1f})"
            )


# ---------------------------------------------------------------------------
# Non-consumed lines bypass intermediate stations via a virtual station
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_non_consumed_lines_route_via_virtual_station(fixture):
    """A line not consumed by station S must not enter S's marker bbox
    and, when it would otherwise cross S's column, must be routed
    through an invisible (``is_hidden``) virtual station in the same
    section.

    Mirrors the v104 terminus-convergence pattern applied to bypassing:
    inserting a hidden station in S's column at a separate trunk-Y row
    forces the layout to allocate the bypass a parallel-branch track,
    so the path uses the existing fan-out / fan-in primitives.
    """
    from nf_metro.layout.routing import compute_station_offsets, route_edges
    from nf_metro.render.svg import apply_route_offsets

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    # Identify the bypass case in this fixture: ``annotate`` in the
    # ``differential`` section consumes only rnaseq+affy but maxquant
    # and geo travel from limma to differential's exit port, so they
    # would otherwise route past annotate.  After v110, those lines
    # must enter a hidden station in the same section.
    bypass_station_ids = {
        sid
        for sid, st in graph.stations.items()
        if st.is_hidden and sid.startswith("__bypass_")
    }
    assert bypass_station_ids, (
        f"{fixture}: expected at least one __bypass_ hidden station "
        "from _insert_bypass_stations"
    )

    # For each bypass station, the section_id should be a real
    # (visible) section and the virtual station should not have a
    # rendered marker.  Test by inspecting station attributes.
    for vsid in bypass_station_ids:
        vstation = graph.stations[vsid]
        assert vstation.is_hidden, f"{vsid} should be is_hidden"
        assert not vstation.label, f"{vsid} should have no label"
        assert vstation.section_id is not None, f"{vsid} needs section_id"

    # For the specific differential-section case, verify the maxquant
    # and geo lines are routed via a hidden bypass station and the
    # paths' rendered Y at annotate's X does NOT enter annotate's bbox.
    annotate = graph.stations.get("annotate")
    assert annotate is not None, "fixture must contain ``annotate`` station"

    diff_bypass = [
        sid
        for sid in bypass_station_ids
        if graph.stations[sid].section_id == annotate.section_id
    ]
    assert diff_bypass, (
        f"{fixture}: expected a bypass virtual station in section {annotate.section_id}"
    )

    # The two bypassing lines (maxquant, geo) should each have edges
    # ending at and starting from the same hidden bypass station.
    bypass_predecessors_for = {
        v: {e.source for e in graph.edges if e.target == v} for v in diff_bypass
    }
    bypass_successors_for = {
        v: {e.target for e in graph.edges if e.source == v} for v in diff_bypass
    }
    bypass_lines_for = {
        v: {e.line_id for e in graph.edges if e.source == v} for v in diff_bypass
    }
    # At least one bypass virtual station should carry the
    # non-consumed lines and chain limma -> V -> exit_port.
    found_bypass_for_lines = False
    for v in diff_bypass:
        if {"maxquant", "geo"}.issubset(bypass_lines_for[v]):
            assert "limma" in bypass_predecessors_for[v]
            assert any("exit" in succ for succ in bypass_successors_for[v]), (
                f"{v}: expected an exit-port successor, got {bypass_successors_for[v]}"
            )
            found_bypass_for_lines = True
            break
    assert found_bypass_for_lines, (
        f"{fixture}: expected a bypass V carrying maxquant and geo from "
        f"limma to the differential exit port"
    )

    # Rendered routes for the bypassing lines must not cross annotate's
    # bbox.  Use a half-bbox approximation centered at annotate (x, y).
    HALF_H = 14.0  # pill half-height plus slack
    HALF_W = 14.0  # marker half-width plus slack
    ann_cx = annotate.x
    ann_cy = annotate.y
    rendered = [apply_route_offsets(r, offsets) for r in routes]
    for ri, r in enumerate(routes):
        # Only interested in lines NOT consumed by annotate.
        if r.line_id not in {"maxquant", "geo"}:
            continue
        # Skip routes whose endpoints don't span past annotate.
        if r.edge.source == "annotate" or r.edge.target == "annotate":
            continue
        pts = rendered[ri]
        for i in range(len(pts) - 1):
            x1, y1 = pts[i]
            x2, y2 = pts[i + 1]
            xlo, xhi = (x1, x2) if x1 <= x2 else (x2, x1)
            if xhi < ann_cx - HALF_W or xlo > ann_cx + HALF_W:
                continue
            # Linearly interpolate Y at ann_cx along this segment.
            if abs(x2 - x1) < 1e-6:
                seg_y = (y1 + y2) / 2
            else:
                t = (ann_cx - x1) / (x2 - x1)
                t = max(0.0, min(1.0, t))
                seg_y = y1 + t * (y2 - y1)
            assert abs(seg_y - ann_cy) > HALF_H, (
                f"{fixture}: line {r.line_id} enters annotate marker "
                f"bbox at x={ann_cx:.1f}, y={seg_y:.1f} (annotate "
                f"cy={ann_cy:.1f})"
            )


# ---------------------------------------------------------------------------
# Bypass virtual stations must clear off-track input rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_bypass_avoids_off_track_inputs(fixture):
    """Each ``__bypass_`` virtual station must sit at a Y that clears
    every off-track input icon in its section.

    v110 inserted bypass virtual stations to push non-consumed lines off
    the trunk, but the chosen bypass row could coincide with an off-
    track input's Y, producing a marker collision (e.g. ``grea`` lifted
    to ``gmt_in``'s y=100 in the v106 regression).  Asserting a minimum
    Y separation between each bypass V and every off-track icon in the
    same section locks the clearance.
    """
    graph = _layout(fixture)
    # Marker clearance: off-track icons render at ~10 px tall, bypass
    # virtual stations contribute to line-bundle routing whose track
    # half-width is one ``offset_step`` (~3 px) plus the marker radius
    # (~5 px).  ``y_spacing`` (55 px) is the natural row pitch; we
    # require strictly less than one full row, ie ~12 px or more.
    MIN_CLEARANCE = 12.0
    bypass_ids = [
        sid
        for sid, st in graph.stations.items()
        if st.is_hidden and sid.startswith("__bypass_")
    ]
    if not bypass_ids:
        pytest.skip(f"{fixture}: no bypass virtual stations")
    for vsid in bypass_ids:
        v = graph.stations[vsid]
        for sid, st in graph.stations.items():
            if sid == vsid or not st.off_track:
                continue
            if st.section_id != v.section_id:
                continue
            # Different column: clearance not required.
            if abs(st.x - v.x) > 0.5:
                continue
            dy = abs(st.y - v.y)
            assert dy >= MIN_CLEARANCE, (
                f"{fixture}: bypass V {vsid!r} at "
                f"({v.x:.1f},{v.y:.1f}) too close to off-track input "
                f"{sid!r} at ({st.x:.1f},{st.y:.1f}); dy={dy:.1f} "
                f"< MIN_CLEARANCE={MIN_CLEARANCE}"
            )


# ---------------------------------------------------------------------------
# v113: Bypass virtual stations use standard routing primitives
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_bypass_virtual_station_uses_standard_routing(fixture):
    """Edges touching a bypass virtual station are routed identically
    to any other fan-out branch.

    v110 introduced ``__bypass_*`` virtual stations.  v111 added a
    bypass-specific routing override in ``_route_diagonal`` that biased
    the diagonal toward the virtual station; v113 removes that override
    so the virtual station participates in the standard fork/join
    placement primitives.  This test asserts both edges of every
    bypass loop (``P -> V`` and ``V -> T``) produce a four-point
    diagonal path with curve-radius corners - the same shape any
    regular fan-out branch produces.
    """
    from nf_metro.layout.constants import CURVE_RADIUS

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    bypass_ids = {
        sid
        for sid, st in graph.stations.items()
        if st.is_hidden and sid.startswith("__bypass_")
    }
    if not bypass_ids:
        pytest.skip(f"{fixture}: no bypass virtual stations")

    diagonal_routes = 0
    for rp in routes:
        if not (rp.edge.source in bypass_ids or rp.edge.target in bypass_ids):
            continue
        # Both bypass edges should be standard 4-point diagonals:
        # (sx,sy) -> (diag_start_x,sy) -> (diag_end_x,ty) -> (tx,ty).
        assert len(rp.points) == 4, (
            f"bypass edge {rp.edge.source}->{rp.edge.target} should be a "
            f"4-point diagonal, got {len(rp.points)} points"
        )
        sx, sy = rp.points[0]
        x1, y1 = rp.points[1]
        x2, y2 = rp.points[2]
        tx, ty = rp.points[3]
        # First/last segments must be horizontal at endpoints' Y.
        assert abs(y1 - sy) < 0.5 and abs(y2 - ty) < 0.5, (
            f"bypass edge {rp.edge.source}->{rp.edge.target} should have "
            "horizontal entry/exit; got non-horizontal end segment"
        )
        # The diagonal X span must equal abs(dy) for a 45-degree run
        # (within rounding).  CURVE_RADIUS corners trim each end so
        # the actual diagonal_run = dy when measured between mids.
        dx = abs(x2 - x1)
        dy = abs(y2 - y1)
        assert dx >= CURVE_RADIUS - 0.5 and dy >= CURVE_RADIUS - 0.5, (
            f"bypass edge {rp.edge.source}->{rp.edge.target} should have "
            f"a non-degenerate diagonal transition; dx={dx:.1f} dy={dy:.1f}"
        )
        diagonal_routes += 1

    assert diagonal_routes >= 2, (
        f"{fixture}: expected at least 2 bypass-touching routes "
        f"(P->V and V->T); got {diagonal_routes}"
    )


# ---------------------------------------------------------------------------
# v113: Section 1 below-trunk content has no empty row directly below trunk
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_section1_below_trunk_compact(fixture):
    """The first below-trunk content row should sit directly below the
    trunk (no empty row gap).

    Section 1 (Data import and preparation) has below-trunk inputs
    (affy_load, proteus, GEOquery) that previously sat one ``y_spacing``
    slot below the trunk row, leaving an empty row between Samples/
    Contrasts and affy_load.  v113 compacts the below-trunk stack so
    the first row is at ``trunk_y + y_spacing``.
    """
    graph = _layout(fixture)
    sec = graph.sections.get("data_prep")
    assert sec is not None, "fixture must contain data_prep section"
    y_spacing = 55.0

    # Trunk Y: take the LR entry port station's Y (the section's
    # inter-section bundle anchor).
    trunk_y: float | None = None
    for pid in list(sec.entry_ports) + list(sec.exit_ports):
        port = graph.ports.get(pid)
        ps = graph.stations.get(pid)
        if port and ps and port.side in (PortSide.LEFT, PortSide.RIGHT):
            trunk_y = ps.y
            break
    assert trunk_y is not None, "data_prep must have an LR port"

    below_ys = [
        graph.stations[sid].y
        for sid in sec.station_ids
        if sid in graph.stations
        and not graph.stations[sid].is_port
        and not graph.stations[sid].is_hidden
        and graph.stations[sid].y > trunk_y + 0.5
    ]
    assert below_ys, "data_prep should have below-trunk content"
    top_below = min(below_ys)
    gap = top_below - trunk_y
    assert gap < y_spacing + 5.0, (
        f"first below-trunk content row should sit at trunk_y+y_spacing="
        f"{trunk_y + y_spacing:.1f}; got top below at {top_below:.1f} "
        f"(gap {gap:.1f} > {y_spacing + 5.0:.1f})"
    )


# ---------------------------------------------------------------------------
# v113: Fan-out side stations are centred on their loop midpoint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_fan_station_centered_on_loop(fixture):
    """Each fan-out side station should sit at the midpoint of its
    loop's horizontal run.

    A fan-out side station is fed by one on-trunk predecessor and
    feeds one on-trunk successor, both at the same trunk Y.  v113
    repositions such stations to the midpoint of the two diagonal
    corner Xs so they're not biased toward the fork side.
    """
    from nf_metro.layout.constants import (
        CURVE_RADIUS,
        DIAGONAL_RUN,
        MIN_STRAIGHT_EDGE,
        MIN_STRAIGHT_PORT,
    )
    from nf_metro.layout.labels import label_text_width

    graph = _layout(fixture)

    # Index edges for loop detection.
    out_by_src: dict[str, list] = defaultdict(list)
    in_by_tgt: dict[str, list] = defaultdict(list)
    for e in graph.edges:
        out_by_src[e.source].append(e)
        in_by_tgt[e.target].append(e)

    fork_t: dict[str, set] = defaultdict(set)
    join_s: dict[str, set] = defaultdict(set)
    for e in graph.edges:
        fork_t[e.source].add(e.target)
        join_s[e.target].add(e.source)
    fork_stations = {sid for sid, t in fork_t.items() if len(t) > 1}
    join_stations = {sid for sid, s in join_s.items() if len(s) > 1}

    def _corner(a, b, role: str) -> float:
        sx, tx = a.x, b.x
        sign = 1.0 if tx > sx else -1.0
        src_min = CURVE_RADIUS + MIN_STRAIGHT_PORT if a.is_port else MIN_STRAIGHT_EDGE
        tgt_min = CURVE_RADIUS + MIN_STRAIGHT_PORT if b.is_port else MIN_STRAIGHT_EDGE
        if a.id in fork_stations and a.label.strip():
            src_min = max(src_min, label_text_width(a.label) / 2)
        if b.id in join_stations and b.label.strip():
            tgt_min = max(tgt_min, label_text_width(b.label) / 2)
        half_diag = DIAGONAL_RUN / 2
        if a.id in fork_stations:
            mid = sx + sign * (src_min + half_diag)
        elif b.id in join_stations:
            mid = tx - sign * (tgt_min + half_diag)
        else:
            mid = (sx + tx) / 2.0
        diag_start = mid - sign * half_diag
        diag_end = mid + sign * half_diag
        return diag_end if role == "src" else diag_start

    checked = 0
    for sid, st in graph.stations.items():
        if st.is_port or st.is_hidden:
            continue
        ins = in_by_tgt.get(sid, [])
        outs = out_by_src.get(sid, [])
        if len(ins) != 1 or len(outs) != 1:
            continue
        src = graph.stations.get(ins[0].source)
        tgt = graph.stations.get(outs[0].target)
        if src is None or tgt is None:
            continue
        if abs(src.y - tgt.y) > 0.5 or abs(st.y - src.y) < 0.5:
            continue
        if not ((src.x < st.x < tgt.x) or (tgt.x < st.x < src.x)):
            continue
        cl = _corner(src, st, role="src")
        cr = _corner(st, tgt, role="tgt")
        midpoint = (cl + cr) / 2.0
        # Allow a small tolerance for grid-snap interactions and
        # subsequent shrink/tighten passes.
        assert abs(st.x - midpoint) <= 2.0, (
            f"loop side station {sid!r} should sit at midpoint "
            f"{midpoint:.1f} of corners ({cl:.1f}, {cr:.1f}); "
            f"got x={st.x:.1f} (delta={st.x - midpoint:+.1f})"
        )
        checked += 1
    assert checked >= 1, f"{fixture}: expected at least one loop side station to test"


# ---------------------------------------------------------------------------
# v113: Section bbox height matches actual content extent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_section_bbox_matches_content_extent(fixture):
    """Each LR/RL section's bbox should hug its content top/bottom.

    The Plots section (a 2-branch symfan placed on half-pitch offsets
    by v110) had a bbox top one full ``y_spacing`` above its content,
    leaving empty space.  v113 shrinks the bbox top for half-grid
    sections so the gap from bbox top to first station equals exactly
    ``section_y_padding``.
    """
    from nf_metro.layout.constants import SECTION_Y_PADDING

    graph = _layout(fixture)
    # Section 4 in da_pipeline is the plots section, alone in row 1.
    sec = graph.sections.get("plots")
    assert sec is not None, "fixture must contain plots section"
    assert sec.bbox_h > 0
    content_ys = [
        graph.stations[sid].y
        for sid in sec.station_ids
        if sid in graph.stations
        and not graph.stations[sid].is_port
        and not graph.stations[sid].is_hidden
    ]
    assert content_ys, "plots section should have content stations"
    top_gap = min(content_ys) - sec.bbox_y
    # Allow padding +/- 1 px slack for float rounding.
    assert abs(top_gap - SECTION_Y_PADDING) <= 1.0, (
        f"plots section top gap should equal SECTION_Y_PADDING="
        f"{SECTION_Y_PADDING}; got {top_gap:.1f}"
    )


# ---------------------------------------------------------------------------
# v113 follow-up: recenter only applies to true loop side-branches.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture",
    [
        "topologies/rnaseq_lite.mmd",
        "topologies/mismatched_tracks.mmd",
        "topologies/variant_calling.mmd",
    ],
)
def test_loop_recenter_only_for_pure_side_branches(fixture):
    """Loop side stations that share their X with an on-trunk co-looper
    must keep that column.

    ``_recenter_loop_side_stations`` moves a side station to the
    midpoint of its loop's diagonal corners.  That's a win for true
    fan-out side stations with their own off-trunk siblings (DA's
    deseq2/dream around limma), but breaks visible column alignment
    when the on-trunk member of the same loop sits at the same X
    (e.g. rnaseq_lite ``star_align`` ↔ ``hisat_align``, mismatched
    tracks ``t_a`` ↔ ``t_b``).  The narrowed pass leaves those
    side stations alone so the on-trunk and off-trunk siblings stay
    column-aligned.
    """
    graph = _layout(fixture)

    out_by_src: dict[str, list] = defaultdict(list)
    in_by_tgt: dict[str, list] = defaultdict(list)
    for e in graph.edges:
        out_by_src[e.source].append(e)
        in_by_tgt[e.target].append(e)

    checked = 0
    for section in graph.sections.values():
        if section.bbox_h <= 0 or section.direction not in ("LR", "RL"):
            continue
        port_ids = set(section.entry_ports) | set(section.exit_ports)
        for sid in section.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.is_hidden:
                continue
            ins = in_by_tgt.get(sid, [])
            outs = out_by_src.get(sid, [])
            if len(ins) != 1 or len(outs) != 1:
                continue
            src = graph.stations.get(ins[0].source)
            tgt = graph.stations.get(outs[0].target)
            if src is None or tgt is None:
                continue
            if abs(src.y - tgt.y) > 0.5:
                continue
            trunk_y = src.y
            if abs(st.y - trunk_y) < 0.5:
                continue
            if not ((src.x < st.x < tgt.x) or (tgt.x < st.x < src.x)):
                continue
            # Find any same-src/tgt sibling that sits on the trunk row.
            # These on-trunk co-loopers anchor a column the off-trunk
            # side station should share.
            on_trunk_sibling_x: float | None = None
            for other_sid in section.station_ids:
                if other_sid == sid:
                    continue
                other = graph.stations.get(other_sid)
                if other is None or other.is_port or other.is_hidden:
                    continue
                if abs(other.y - trunk_y) >= 0.5:
                    continue  # off-trunk, ignore here
                other_ins = in_by_tgt.get(other_sid, [])
                other_outs = out_by_src.get(other_sid, [])
                other_srcs = {e.source for e in other_ins}
                other_tgts = {e.target for e in other_outs}
                if other_srcs == {ins[0].source} and other_tgts == {outs[0].target}:
                    on_trunk_sibling_x = other.x
                    break
            if on_trunk_sibling_x is None:
                continue  # nothing to anchor against
            assert abs(st.x - on_trunk_sibling_x) < 0.5, (
                f"loop side station {sid!r} was recentered off the column "
                f"of its on-trunk co-looper: x={st.x:.1f} vs co-looper "
                f"x={on_trunk_sibling_x:.1f}"
            )
            checked += 1
    assert checked >= 1, (
        f"{fixture}: expected at least one off-trunk loop side station "
        "paired with an on-trunk co-looper"
    )


# ---------------------------------------------------------------------------
# v114: Lines never cross a non-consumer station's marker bbox
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd", "rnaseq_sections.mmd"])
def test_lines_dont_cross_non_consumer_markers(fixture):
    """No rendered line segment may pass through the marker bbox of
    any station that neither consumes nor produces that line.

    Complements ``test_no_station_or_icon_overlap`` (which catches
    marker/marker collisions) with the symmetric line/marker check
    that catches the "breeze-past" pattern: a sparse-consumer
    station S sharing a Y row with a busier sibling whose inbound
    bundle traverses S's column.  Pre-v114 ``grea`` (rnaseq-only)
    sat at the same Y as ``decoupler`` (full bundle), so the lines
    flowing from the section entry to decoupler crossed grea's
    marker on the way in.

    Iterates every (station, route) pair and asserts no segment of
    the route's rendered polyline intersects the station's marker
    bbox when the line is not part of the station's consumed or
    produced set.
    """
    from nf_metro.layout.engine import _station_marker_bbox
    from nf_metro.render.svg import apply_route_offsets

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    consumed_by: dict[str, set[str]] = defaultdict(set)
    produced_by: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges:
        consumed_by[e.target].add(e.line_id)
        produced_by[e.source].add(e.line_id)

    def _seg_crosses_bbox(
        p1: tuple[float, float],
        p2: tuple[float, float],
        bbox: tuple[float, float, float, float],
    ) -> bool:
        x1, y1 = p1
        x2, y2 = p2
        bx1, by1, bx2, by2 = bbox
        if max(x1, x2) < bx1 or min(x1, x2) > bx2:
            return False
        if max(y1, y2) < by1 or min(y1, y2) > by2:
            return False
        for k in range(21):
            f = k / 20.0
            x = x1 + f * (x2 - x1)
            y = y1 + f * (y2 - y1)
            if bx1 <= x <= bx2 and by1 <= y <= by2:
                return True
        return False

    for sid, st in graph.stations.items():
        bbox = _station_marker_bbox(graph, sid, offsets=offsets)
        if bbox is None:
            continue
        station_lines = consumed_by.get(sid, set()) | produced_by.get(sid, set())
        for r in routes:
            if r.line_id in station_lines:
                continue
            if r.edge.source == sid or r.edge.target == sid:
                continue
            pts = apply_route_offsets(r, offsets)
            for k in range(len(pts) - 1):
                if _seg_crosses_bbox(pts[k], pts[k + 1], bbox):
                    raise AssertionError(
                        f"{fixture}: line {r.line_id!r} on edge "
                        f"{r.edge.source!r} -> {r.edge.target!r} "
                        f"crosses non-consumer station {sid!r} "
                        f"marker bbox "
                        f"({bbox[0]:.1f},{bbox[1]:.1f})-"
                        f"({bbox[2]:.1f},{bbox[3]:.1f}); segment "
                        f"({pts[k][0]:.1f},{pts[k][1]:.1f})->"
                        f"({pts[k + 1][0]:.1f},{pts[k + 1][1]:.1f})"
                    )


# ---------------------------------------------------------------------------
# On-track stations must snap to the section trunk Y grid
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd", "rnaseq_sections.mmd"])
def test_all_stations_snap_to_grid(fixture):
    """Every on-track station's Y must be at ``trunk_y + k * y_spacing``
    for some integer ``k``.

    Half-grid placement (``trunk_y +/- 0.5 * y_spacing``) is reserved
    for the auto-half-grid 2-branch symmetric fan feature: stations
    registered in ``graph._half_grid_station_ids`` whose section
    satisfies ``_section_symfan_uses_half_grid`` and has exactly two
    on-track branches.  Any other half-grid station is a regression.
    """
    from nf_metro.layout.engine import _section_symfan_uses_half_grid

    y_spacing = 55.0
    tol = 1.0
    graph = _layout(fixture, y_spacing=y_spacing)

    half_grid_ids = getattr(graph, "_half_grid_station_ids", set()) or set()
    port_ids: set[str] = set()
    for sec in graph.sections.values():
        port_ids.update(sec.entry_ports)
        port_ids.update(sec.exit_ports)
    junction_ids = set(graph.junctions)

    # Compute each LR/RL section's trunk Y from its LR ports.
    section_trunk_y: dict[str, float] = {}
    for sec in graph.sections.values():
        if sec.direction not in ("LR", "RL") or sec.bbox_h <= 0:
            continue
        port_ys = _section_lr_port_ys(graph, sec)
        if port_ys:
            section_trunk_y[sec.id] = port_ys[0]

    # Sections eligible for the half-grid 2-branch fan exception.
    half_grid_sections = {
        sec.id
        for sec in graph.sections.values()
        if sec.direction in ("LR", "RL")
        and sec.bbox_h > 0
        and _section_symfan_uses_half_grid(graph, sec)
    }

    offenders: list[str] = []
    for sid, st in graph.stations.items():
        if (
            st.is_port
            or st.is_hidden
            or st.off_track
            or sid in port_ids
            or sid in junction_ids
        ):
            continue
        if st.section_id is None:
            continue
        trunk_y = section_trunk_y.get(st.section_id)
        if trunk_y is None:
            continue
        offset = (st.y - trunk_y) / y_spacing
        nearest_int = round(offset)
        on_grid = abs(offset - nearest_int) * y_spacing <= tol
        if on_grid:
            continue
        # Half-grid exception is allowed only for 2-branch fan members
        # whose section legitimately uses the half-grid layout.
        is_half = (
            abs(offset - (nearest_int - 0.5)) * y_spacing <= tol
            or abs(offset - (nearest_int + 0.5)) * y_spacing <= tol
        )
        if is_half and sid in half_grid_ids and st.section_id in half_grid_sections:
            continue
        offenders.append(
            f"{sid!r} cy={st.y:.2f} trunk_y={trunk_y:.2f} "
            f"offset/y_spacing={offset:.3f} "
            f"section={st.section_id!r} "
            f"in_half_grid_ids={sid in half_grid_ids} "
            f"section_uses_half_grid="
            f"{st.section_id in half_grid_sections}"
        )
    assert not offenders, (
        f"{fixture}: on-track stations off the y_spacing grid "
        f"without a legitimate half-grid 2-branch fan exception: "
        + "; ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Bypass V routed polyline middle segment must be flat at V's Y
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_bypass_v_horizontal_segment_is_flat(fixture):
    """For each hidden bypass V station, the routed polyline carrying a
    bypassed line through V must form a clean U: the horizontal middle
    segment at V's Y (the union of the P->V approach past the diagonal
    end and the V->T departure before the diagonal start) must share Y
    within 0.5px tolerance.
    """
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    bypass_v_ids = {
        sid
        for sid, st in graph.stations.items()
        if st.is_hidden and sid.startswith("__bypass_")
    }
    assert bypass_v_ids, f"{fixture}: expected at least one __bypass_ virtual station"

    # Group routes by (V, line) so we can pair the P->V and V->T halves.
    by_v_line: dict[tuple[str, str], list] = defaultdict(list)
    for r in routes:
        if r.edge.source in bypass_v_ids:
            by_v_line[(r.edge.source, r.line_id)].append(("out", r))
        if r.edge.target in bypass_v_ids:
            by_v_line[(r.edge.target, r.line_id)].append(("in", r))

    tol = 0.5
    checked = 0
    for (vid, lid), pair in by_v_line.items():
        if len(pair) != 2:
            continue
        in_route = next((r for kind, r in pair if kind == "in"), None)
        out_route = next((r for kind, r in pair if kind == "out"), None)
        if in_route is None or out_route is None:
            continue
        # The P -> V leg should end with a flat segment at V's Y.
        # The V -> T leg should start with a flat segment at V's Y.
        # Both must agree on V's Y within tolerance.
        in_y_at_v = in_route.points[-1][1]
        # Y at the start of the V->T flat segment.
        out_y_at_v = out_route.points[0][1]
        assert abs(in_y_at_v - out_y_at_v) <= tol, (
            f"{fixture}: bypass {vid!r} line {lid!r}: P->V ends at "
            f"y={in_y_at_v:.3f} but V->T starts at y={out_y_at_v:.3f} "
            f"(delta={abs(in_y_at_v - out_y_at_v):.3f}px > {tol})"
        )
        # The flat horizontal segment at V's Y on the P->V leg is from
        # point[-2] to point[-1] - they should share Y.
        prev_y = in_route.points[-2][1]
        if len(in_route.points) >= 3 and abs(prev_y - in_y_at_v) > tol:
            # Diagonal directly into V; the V's flat segment is on the
            # other half only.
            pass
        else:
            assert abs(prev_y - in_y_at_v) <= tol, (
                f"{fixture}: bypass {vid!r} line {lid!r}: P->V last "
                f"flat segment Y mismatch ({prev_y:.3f} vs {in_y_at_v:.3f})"
            )
        next_y = out_route.points[1][1]
        if len(out_route.points) >= 3 and abs(next_y - out_y_at_v) > tol:
            pass
        else:
            assert abs(next_y - out_y_at_v) <= tol, (
                f"{fixture}: bypass {vid!r} line {lid!r}: V->T first "
                f"flat segment Y mismatch ({next_y:.3f} vs {out_y_at_v:.3f})"
            )

        # When the bypass V has multiple lines passing through, all of
        # them must reach V at the same per-line Y (no X spread bundle
        # asymmetry that distorts one line's flat segment relative to
        # the other).  Compare diagonal end X (P->V points[-2]) with
        # the diagonal start X (V->T points[1]) relative to V's nominal
        # X: the flat segment on each side should be the SAME length
        # for a clean U.
        v_x = graph.stations[vid].x
        left_flat = abs(in_route.points[-1][0] - in_route.points[-2][0])
        right_flat = abs(out_route.points[1][0] - out_route.points[0][0])
        # Allow modest asymmetry but reject the case where one side is
        # nearly collapsed while the other has a visible flat.
        if max(left_flat, right_flat) > 5.0:
            asym = abs(left_flat - right_flat)
            assert asym <= 2.0, (
                f"{fixture}: bypass {vid!r} line {lid!r}: asymmetric "
                f"flat segments at V (left={left_flat:.2f}px, "
                f"right={right_flat:.2f}px, V.x={v_x:.2f}); the V "
                f"bottom should form a symmetric U"
            )
        checked += 1
    assert checked > 0, (
        f"{fixture}: expected at least one paired bypass V edge to verify"
    )


# ---------------------------------------------------------------------------
# Bypass V must sit on a visible horizontal flat segment, not at a corner
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_bypass_v_has_horizontal_segment(fixture):
    """Each hidden bypass V station must sit in the middle of a clearly
    visible horizontal flat segment, matching how regular fork/join
    stations present a horizontal run through their X.

    Stronger than ``test_bypass_v_horizontal_segment_is_flat``: that
    test only checks the polyline flat at V's Y is flat in Y, which is
    trivially true even when the flat is zero pixels long because the
    two halves of the U meet at V's X.  Here we assert the polyline
    flat reaches V from at least ``MIN_STATION_FLAT_LENGTH`` pixels
    away (in run-axis X) on each side, so that after the curve corner
    consumes ``CURVE_RADIUS`` pixels, a visible flat of
    ``MIN_STATION_FLAT_LENGTH - CURVE_RADIUS`` pixels remains on each
    side of V (matching e.g. propd / dream / DESeq2).
    """
    from nf_metro.layout.constants import MIN_STATION_FLAT_LENGTH

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    bypass_v_ids = {
        sid
        for sid, st in graph.stations.items()
        if st.is_hidden and sid.startswith("__bypass_")
    }
    assert bypass_v_ids, f"{fixture}: expected at least one __bypass_ virtual station"

    by_v_line: dict[tuple[str, str], list] = defaultdict(list)
    for r in routes:
        if r.edge.source in bypass_v_ids:
            by_v_line[(r.edge.source, r.line_id)].append(("out", r))
        if r.edge.target in bypass_v_ids:
            by_v_line[(r.edge.target, r.line_id)].append(("in", r))

    tol = 0.5
    checked = 0
    for (vid, lid), pair in by_v_line.items():
        if len(pair) != 2:
            continue
        in_route = next((r for kind, r in pair if kind == "in"), None)
        out_route = next((r for kind, r in pair if kind == "out"), None)
        if in_route is None or out_route is None:
            continue

        # P -> V: last two polyline points (-2, -1) form the flat
        # segment landing at V.  Its length is what reaches V in X
        # before the curve corner consumes CURVE_RADIUS pixels.
        left_flat = abs(in_route.points[-1][0] - in_route.points[-2][0])
        # V -> T: first two polyline points form the flat leaving V.
        right_flat = abs(out_route.points[1][0] - out_route.points[0][0])

        assert left_flat >= MIN_STATION_FLAT_LENGTH - tol, (
            f"{fixture}: bypass {vid!r} line {lid!r}: P->V flat segment "
            f"too short to render a visible horizontal run through V "
            f"(left_flat={left_flat:.2f}px, "
            f"MIN_STATION_FLAT_LENGTH={MIN_STATION_FLAT_LENGTH}px); "
            f"V would sit at the curve apex instead of on a visible "
            f"horizontal flat like regular stations"
        )
        assert right_flat >= MIN_STATION_FLAT_LENGTH - tol, (
            f"{fixture}: bypass {vid!r} line {lid!r}: V->T flat segment "
            f"too short to render a visible horizontal run through V "
            f"(right_flat={right_flat:.2f}px, "
            f"MIN_STATION_FLAT_LENGTH={MIN_STATION_FLAT_LENGTH}px)"
        )
        checked += 1

    assert checked > 0, (
        f"{fixture}: expected at least one paired bypass V edge to verify"
    )


# ---------------------------------------------------------------------------
# Loop-column stations share X (trunk + off-trunk siblings co-aligned)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_loop_column_stations_share_x(fixture):
    """Every station in a loop column (defined by its trunk-Y
    horizontal extent) must share an X with the column's other clean
    members.

    A "loop column" groups stations within an LR/RL section by the
    pair ``(rightmost trunk-Y predecessor X, leftmost trunk-Y
    successor X)``.  A station counts as a column member when:

    - all its visible predecessors and successors sit on the
      section's trunk Y (no off-track inputs that would pull its X
      away from the column), and
    - either it has a single inbound edge and a single outbound
      edge (a "clean" off-trunk side station, mirrored at the loop
      midpoint by ``_recenter_loop_side_stations`` pass 1), or it
      sits ON the trunk row (the column's trunk station, which pass
      2 snaps onto the clean-sibling midpoint).

    Catches the v115 regression where ``limma`` sat at the raw
    layer X (e.g. 629.4) while its off-trunk siblings ``propd``,
    ``dream`` and ``DESeq2`` had been recentered to the column
    midpoint (~648.6).
    """
    from nf_metro.parser.model import PortSide

    graph = _layout(fixture)

    in_by_tgt: dict[str, list] = defaultdict(list)
    out_by_src: dict[str, list] = defaultdict(list)
    for e in graph.edges:
        in_by_tgt[e.target].append(e)
        out_by_src[e.source].append(e)

    checked_columns = 0
    for sec in graph.sections.values():
        if sec.bbox_h <= 0 or sec.direction not in ("LR", "RL"):
            continue
        trunk_y: float | None = None
        for pid in sec.entry_ports + sec.exit_ports:
            ps = graph.stations.get(pid)
            port = graph.ports.get(pid)
            if (
                ps is not None
                and port is not None
                and port.side in (PortSide.LEFT, PortSide.RIGHT)
            ):
                trunk_y = ps.y
                break
        if trunk_y is None:
            continue
        port_ids = set(sec.entry_ports) | set(sec.exit_ports)

        # Group eligible stations by (pred_x, succ_x).
        columns: dict[tuple[float, float], list[str]] = defaultdict(list)
        for sid in sec.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.is_hidden:
                continue
            visible_ins = [
                e
                for e in in_by_tgt.get(sid, [])
                if (
                    (gs := graph.stations.get(e.source)) is not None
                    and not gs.is_hidden
                )
            ]
            visible_outs = [
                e
                for e in out_by_src.get(sid, [])
                if (
                    (gs := graph.stations.get(e.target)) is not None
                    and not gs.is_hidden
                )
            ]
            # All visible neighbours must be on trunk Y; otherwise an
            # off-track input anchors this station elsewhere.
            ok = True
            for e in visible_ins:
                if abs(graph.stations[e.source].y - trunk_y) > 0.5:
                    ok = False
                    break
            if not ok:
                continue
            for e in visible_outs:
                if abs(graph.stations[e.target].y - trunk_y) > 0.5:
                    ok = False
                    break
            if not ok:
                continue
            if not visible_ins or not visible_outs:
                continue
            # Eligibility: clean side station (1 edge in, 1 edge out
            # AND off-trunk) OR trunk-Y station.
            on_trunk = abs(st.y - trunk_y) <= 0.5
            clean_side = (
                not on_trunk and len(visible_ins) == 1 and len(visible_outs) == 1
            )
            if not (on_trunk or clean_side):
                continue
            # Column key: rightmost trunk-Y predecessor X (LR), or
            # leftmost (RL); leftmost trunk-Y successor X (LR), or
            # rightmost (RL).
            if sec.direction == "LR":
                pred_x = max(graph.stations[e.source].x for e in visible_ins)
                succ_x = min(graph.stations[e.target].x for e in visible_outs)
            else:
                pred_x = min(graph.stations[e.source].x for e in visible_ins)
                succ_x = max(graph.stations[e.target].x for e in visible_outs)
            # Station must sit strictly between its trunk-Y
            # neighbours.
            lo, hi = min(pred_x, succ_x), max(pred_x, succ_x)
            if not (lo < st.x < hi):
                continue
            columns[(round(pred_x, 3), round(succ_x, 3))].append(sid)

        for key, members in columns.items():
            if len(members) < 2:
                continue
            xs = [graph.stations[sid].x for sid in members]
            spread = max(xs) - min(xs)
            member_xs = [(sid, round(graph.stations[sid].x, 2)) for sid in members]
            assert spread <= 1.0, (
                f"{fixture}: section {sec.id!r} loop column {key}: "
                f"members {member_xs} span {spread:.2f}px (>1px); "
                f"trunk + clean siblings should share X"
            )
            checked_columns += 1

    assert checked_columns >= 1, (
        f"{fixture}: expected at least one loop column with multiple members to verify"
    )


# ---------------------------------------------------------------------------
# Section bbox bottom padding (Phase 13k post-shift padding)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd", "rnaseq_sections.mmd"])
def test_section_bbox_has_bottom_padding(fixture):
    """Each section's bbox bottom must sit at least ``section_y_padding``
    below the centre Y of its lowest internal station.

    The codebase convention (see ``_shrink_bboxes_to_content_bottom``)
    measures bottom padding from the station's centre Y, not its marker
    edge, so the invariant is ``bbox_bot >= max(station.y) +
    section_y_padding``.

    ``_shift_sparse_loop_stations_to_clear_bundle`` (Phase 13k) can
    move a sparse loop station like ``grea`` further down without
    restoring this padding.  Catches the v116 regression where
    section 3's bbox sat ~5px below ``grea``'s centre instead of
    ``section_y_padding`` (50px).
    """
    from nf_metro.layout.constants import SECTION_Y_PADDING

    graph = _layout(fixture)
    tol = 1.0

    offenders: list[str] = []
    for sec in graph.sections.values():
        if sec.bbox_h <= 0:
            continue
        port_ids = set(sec.entry_ports) | set(sec.exit_ports)
        internal_ys = [
            graph.stations[sid].y
            for sid in sec.station_ids
            if sid in graph.stations
            and sid not in port_ids
            and not graph.stations[sid].is_hidden
        ]
        if not internal_ys:
            continue
        lowest_marker_cy = max(internal_ys)
        bbox_bot = sec.bbox_y + sec.bbox_h
        gap = bbox_bot - lowest_marker_cy
        if gap + tol < SECTION_Y_PADDING:
            offenders.append(
                f"section {sec.id!r}: bbox bot={bbox_bot:.1f}, "
                f"lowest marker cy={lowest_marker_cy:.1f}, "
                f"gap={gap:.1f} < section_y_padding={SECTION_Y_PADDING}"
            )

    assert not offenders, (
        f"{fixture}: section bbox bottoms must sit at least "
        f"section_y_padding below the lowest station centre: " + "; ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Inter-row gap accommodates grown bboxes from Phase 13k shifts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_row_gap_accommodates_bypass(fixture):
    """The vertical gap between row ``r`` sections' bbox bottoms and
    row ``r + 1`` sections' bbox tops must be at least
    ``section_y_gap`` for every column-overlapping pair.

    When ``_shift_sparse_loop_stations_to_clear_bundle`` grows an
    upper-row section's bbox downward (e.g. section 3 in the
    differentialabundance pipeline, around ``grea``), the row offset
    computed by ``_compute_section_offsets`` from the pre-shift bbox
    height is no longer enough; the lower row must be pushed down so
    routing has room between the new bbox bottom and the next row's
    header.  Catches the v116 regression where section 4 (plots) sat
    only ~40px below section 3's grown bbox bottom.

    Tested at ``y_spacing=55`` because the production render uses that
    pitch; the default ``y_spacing=40`` happens to leave the bbox
    growth absorbed by row-0's taller rowspan section, hiding the
    regression.
    """
    from nf_metro.layout.constants import SECTION_Y_GAP

    graph = _layout(fixture, y_spacing=55)
    tol = 1.0

    by_row: dict[int, list] = defaultdict(list)
    for sec in graph.sections.values():
        if sec.bbox_h <= 0:
            continue
        by_row[sec.grid_row + sec.grid_row_span - 1].append(sec)
    starting_at: dict[int, list] = defaultdict(list)
    for sec in graph.sections.values():
        if sec.bbox_h <= 0:
            continue
        starting_at[sec.grid_row].append(sec)

    def _cols_overlap(a, b) -> bool:
        a_start = a.grid_col
        a_end = a_start + a.grid_col_span - 1
        b_start = b.grid_col
        b_end = b_start + b.grid_col_span - 1
        return not (a_end < b_start or b_end < a_start)

    offenders: list[str] = []
    if not by_row:
        return
    max_row = max(by_row)
    for r in range(max_row):
        upper = by_row.get(r, [])
        lower = starting_at.get(r + 1, [])
        for us in upper:
            for ls in lower:
                if not _cols_overlap(us, ls):
                    continue
                upper_bot = us.bbox_y + us.bbox_h
                lower_top = ls.bbox_y
                gap = lower_top - upper_bot
                if gap + tol < SECTION_Y_GAP:
                    offenders.append(
                        f"rows {r}->{r + 1} col-overlap "
                        f"{us.id!r} (bot={upper_bot:.1f}) -> "
                        f"{ls.id!r} (top={lower_top:.1f}): "
                        f"gap={gap:.1f} < section_y_gap={SECTION_Y_GAP}"
                    )

    assert not offenders, (
        f"{fixture}: row gap must be >= section_y_gap for every "
        f"column-overlapping pair: " + "; ".join(offenders)
    )
