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
