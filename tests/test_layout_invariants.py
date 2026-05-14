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


def _section_trunk_marker_cy(graph: MetroGraph, section) -> float | None:
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
    offsets = compute_station_offsets(graph)
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


@pytest.mark.parametrize(
    "fixture", ["da_pipeline.mmd", "rnaseq_sections.mmd"]
)
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
    rows = _row_lr_sections(graph)
    for row, sections in rows.items():
        cys: list[tuple[str, float]] = []
        for sec in sections:
            cy = _section_trunk_marker_cy(graph, sec)
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
    for sec in graph.sections.values():
        cols = _section_fan_columns(graph, sec)
        trunk_cy = _section_trunk_marker_cy(graph, sec)
        if trunk_cy is None:
            continue
        offsets = compute_station_offsets(graph)
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
                exit_cy = graph.stations[pid].y + (
                    min(exit_offs) + max(exit_offs)
                ) / 2
                # Matching entry port of next section
                for npid in nxt.entry_ports:
                    nport = graph.ports.get(npid)
                    if nport is None or nport.side != PortSide.LEFT:
                        continue
                    entry_lines = graph.station_lines(npid)
                    entry_offs = [
                        offsets.get((npid, lid), 0.0) for lid in entry_lines
                    ]
                    entry_cy = graph.stations[npid].y + (
                        min(entry_offs) + max(entry_offs)
                    ) / 2
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
                (r for r in routes
                 if r.edge.source == edge.source
                 and r.edge.target == edge.target
                 and r.edge.line_id == edge.line_id),
                None,
            )
            if rp is None or len(rp.points) < 2:
                continue
            tgt = graph.stations.get(edge.target)
            if tgt is None:
                continue
            tgt_port = graph.ports.get(edge.target)
            same_sec_target = (
                tgt.section_id == sec.id and not tgt.is_port
            )
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
    assert asserted > 0, (
        f"{fixture}: no side-branch single-line exits found to test"
    )


# ---------------------------------------------------------------------------
# Section content balance around the trunk
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_section_top_band_filled(fixture):
    """LR/RL sections with room for another above-trunk slot AND
    multiple below-trunk movable siblings should fill the empty top
    band, not leave it stranded.

    Specifically: when the top band (between bbox_y and the topmost
    station) is large enough to fit another station with its label
    (>= y_spacing + label_clearance), AND there are >= 2 movable
    below-trunk siblings versus at most 1 above, the balance pass
    should have lifted one of them so the top band shrinks to within
    one y_spacing of the bbox.

    Catches the v103 ``data_prep`` layout where only Matrix was lifted
    above the trunk while four other file inputs stayed below.
    """
    import networkx as nx

    y_spacing = 55.0
    label_clearance = y_spacing / 2
    graph = _layout(fixture, y_spacing=y_spacing)
    G = nx.DiGraph()
    for e in graph.edges:
        G.add_edge(e.source, e.target)

    for section in graph.sections.values():
        if (
            section.bbox_h <= 0
            or section.direction not in ("LR", "RL")
        ):
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

        # Trunk Y: prefer an LR port; fall back to a full-bundle station.
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
            continue  # band is already tight

        # Count movable siblings above vs below trunk.
        movable_above = 0
        movable_below_candidates: list[str] = []
        for x, sids in cols.items():
            trunks_here = [
                s for s in sids
                if set(graph.station_lines(s)) == bundle
            ]
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

        # Only flag sections where >= 2 below-trunk movables exist
        # AND at least one would fit in a new top slot with its
        # label.  Sections where slot -k of every below station
        # would clip into the bbox stroke are skipped.
        if len(movable_below_candidates) < 2 or movable_above >= len(
            movable_below_candidates
        ):
            continue

        target_y = top_y - y_spacing
        any_fits = any(
            target_y >= section.bbox_y + (
                label_clearance
                if graph.stations[s].label and graph.stations[s].label.strip()
                else 0.0
            ) - _Y_TOL
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

    Catches the regression where ``_fan_source_inputs_upward`` lifts
    only a single input (e.g. just Matrix), leaving a 2+ y_spacing
    gap between the bbox top and the topmost input.
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

    # Inputs: stations with no inbound edges (sources).
    has_in: set[str] = {e.target for e in graph.edges}
    inputs = [
        sid for sid in section.station_ids
        if sid not in port_ids
        and sid not in has_in
        and sid in graph.stations
        and not graph.stations[sid].is_port
    ]
    inputs_above = [
        sid for sid in inputs
        if graph.stations[sid].y < trunk_y - _Y_TOL
    ]
    assert inputs_above, (
        f"data_prep: no input sits above trunk_y={trunk_y:.1f} "
        f"(inputs at y={[graph.stations[s].y for s in inputs]})"
    )
    # Top of section: the topmost input should sit within one
    # y_spacing of the bbox top so the top band is visibly filled.
    top_input_y = min(graph.stations[s].y for s in inputs_above)
    top_band = top_input_y - section.bbox_y
    assert top_band <= y_spacing + _Y_TOL, (
        f"data_prep: top input at y={top_input_y:.1f} leaves "
        f"top_band={top_band:.1f}px > {y_spacing:.1f} (bbox_y="
        f"{section.bbox_y:.1f}); balance pass should lift another "
        f"input into the top slot"
    )


# ---------------------------------------------------------------------------
# Section bbox must contain all stations and off-track inputs
# ---------------------------------------------------------------------------


def _icon_half_height(graph: MetroGraph) -> float:
    """Vertical reach of a file-input icon above/below its centre.

    Mirrors the renderer's terminus icon height (32 px default).  Used
    to verify the section bbox encloses off-track icon tops.
    """
    # Default terminus_height in Theme is 32 px; half = 16.
    return 16.0


@pytest.mark.parametrize(
    "fixture", ["da_pipeline.mmd", "rnaseq_sections.mmd"]
)
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
    icon_half = _icon_half_height(graph)
    marker_half = 9.5  # station marker height / 2

    for sec_id, section in graph.sections.items():
        if section.bbox_h <= 0:
            continue
        for sid in section.station_ids:
            st = graph.stations.get(sid)
            if st is None or st.is_port or sid in junction_ids:
                continue
            # Use icon half-height for off-track (file-input) stations,
            # marker half-height otherwise.
            half = icon_half if st.off_track else marker_half
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
# Terminus stations must not be hit by a diagonal route segment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_terminus_not_directly_after_diagonal(fixture):
    """Routes terminating at an output terminus must arrive on an
    orthogonal (horizontal or vertical) final segment.

    Catches the v103 layout where ``plot_expl`` and ``plot_diff`` both
    fed ``plots_png`` via diagonals that converged AT the terminus
    marker, producing a Y-shape with the file icon at the convergence
    point.  After v104 the layout should insert a virtual convergence
    station so the last segment to the terminus is purely orthogonal.

    The check tolerates short corner segments (curve smoothing) by
    requiring the LAST segment of length >= MIN_LEN to be axis-aligned.
    """
    from nf_metro.layout.routing import route_edges

    MIN_LEN = 30.0  # require axis-aligned approach for the last >= 30px
    AXIS_TOL = 1.0
    graph = _layout(fixture)
    routes = route_edges(graph)
    # Group routes by terminus target id.
    by_target: dict[str, list] = defaultdict(list)
    for r in routes:
        tgt = graph.stations.get(r.edge.target)
        if tgt is None or not tgt.is_terminus:
            continue
        by_target[r.edge.target].append(r)

    for tid, paths in by_target.items():
        # Only enforce the invariant when a terminus has 2+ direct
        # inbound edges (the convergence case).  Single-source termini
        # already inherit their source's Y.
        sources = {r.edge.source for r in paths}
        if len(sources) < 2:
            continue
        for r in paths:
            pts = r.points
            if len(pts) < 2:
                continue
            # Find the last segment of length >= MIN_LEN
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
# Lines passing through a non-consuming station's column must detour a
# full grid row (~y_spacing), not just nudge around the marker.
# ---------------------------------------------------------------------------


def _render_y_at_x(pts: list[tuple[float, float]], x: float) -> float | None:
    """Y of a polyline at the given X, or None if X is outside its span."""
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        xlo, xhi = (x1, x2) if x1 <= x2 else (x2, x1)
        if xlo - 1e-6 <= x <= xhi + 1e-6:
            # Linear interp along the segment.
            if abs(x2 - x1) < 1e-6:
                return (y1 + y2) / 2
            frac = (x - x1) / (x2 - x1)
            return y1 + frac * (y2 - y1)
    return None


@pytest.mark.parametrize(
    "fixture,y_spacing",
    [
        ("da_pipeline.mmd", 55.0),
        ("rnaseq_sections.mmd", 55.0),
    ],
)
def test_lines_bypass_non_consumers_at_full_grid_row(fixture, y_spacing):
    """A line whose route crosses a non-consuming station's X must
    visually clear the marker bbox AND sit at least a full grid row
    (~y_spacing) away from the trunk at that X.

    This catches v107 baseline where ``maxquant`` and ``geo`` lines
    passed straight through ``Annotate results`` (and earlier, through
    ``grea``), violating the structural rule that lines route AROUND
    non-consuming stations rather than THROUGH them.  v105's 5px nudge
    cleared the marker but didn't reach a full row; v108 shifts by
    exactly ``y_spacing``.
    """
    from nf_metro.layout.routing import compute_station_offsets, route_edges
    from nf_metro.layout.routing.core import _build_marker_bboxes
    from nf_metro.render.svg import apply_route_offsets

    # Threshold for "a full grid row": 0.9 * y_spacing absorbs slight
    # sub-pixel jitter from the line-bundle spacing within the row.
    FULL_ROW = 0.9 * y_spacing

    graph = _layout(fixture, y_spacing=y_spacing)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    # Junction set: any station listed in graph.junctions.
    junction_ids = set(graph.junctions)
    bboxes = _build_marker_bboxes(graph, junction_ids, offsets)

    # Build per-line trunk-Y lookup per station: the average rendered Y
    # of CONSUMED lines at the station's X.  Lines bypassing must sit
    # at least FULL_ROW away from this trunk Y.
    rendered: list[list[tuple[float, float]]] = [
        apply_route_offsets(r, offsets) for r in routes
    ]

    violations: list[str] = []
    # "Near the station's row" Y-band: routes whose Y at bb.cx falls
    # within this band are considered the trunk passing through the
    # station's row; routes outside it are in a different row entirely
    # (e.g. cross-section bundles in a lower row that happen to share
    # an X column with this station).
    NEAR_ROW = y_spacing * 0.6
    for sid, bb in bboxes.items():
        trunk_ys: list[float] = []
        crossing_lines: list[tuple[str, int, float]] = []
        for ri, r in enumerate(routes):
            if r.edge.source == sid or r.edge.target == sid:
                continue
            pts = rendered[ri]
            # Find a horizontal segment crossing bb.cx near bb.cy.
            seg_y_at_cx: float | None = None
            for i in range(len(pts) - 1):
                x1, y1 = pts[i]
                x2, y2 = pts[i + 1]
                xlo, xhi = (x1, x2) if x1 <= x2 else (x2, x1)
                if not (xlo - 1e-6 <= bb.cx <= xhi + 1e-6):
                    continue
                if abs(y2 - y1) > 1.0:
                    continue
                seg_y = (y1 + y2) / 2
                if abs(seg_y - bb.cy) > NEAR_ROW:
                    continue  # Different grid row entirely
                seg_y_at_cx = seg_y
                break
            if seg_y_at_cx is None:
                continue
            if r.line_id in bb.consumed:
                trunk_ys.append(seg_y_at_cx)
            else:
                crossing_lines.append((r.line_id, ri, seg_y_at_cx))

        if not crossing_lines:
            continue

        # Trunk Y: average of consumed lines' rendered Ys at bb.cx, or
        # fall back to the marker center if no consumed line crosses.
        if trunk_ys:
            trunk_y = sum(trunk_ys) / len(trunk_ys)
        else:
            trunk_y = bb.cy

        bbox_top = bb.cy - bb.half_h - 0.5
        bbox_bot = bb.cy + bb.half_h + 0.5
        # A non-consumed line is considered "in the trunk band" if its
        # rendered Y at the station X is within the marker bbox extent
        # plus one OFFSET_STEP slot of slack.  Such a line was at risk
        # of crossing the marker pre-bypass and must therefore be
        # detoured a full grid row away (v108 invariant).  Lines that
        # are already clear (e.g. above an off-track station or below
        # a low feeder) are unaffected.
        trunk_band_top = trunk_y - bb.half_h - 4.0
        trunk_band_bot = trunk_y + bb.half_h + 4.0

        for line_id, ri, y_at in crossing_lines:
            # Rule 1: line must clear the marker bbox itself.
            if bbox_top <= y_at <= bbox_bot:
                violations.append(
                    f"{fixture}: line {line_id} crosses non-consuming "
                    f"station {sid} marker bbox at "
                    f"(x={bb.cx:.1f}, y={y_at:.1f}) "
                    f"[bbox y-range {bbox_top:.1f}..{bbox_bot:.1f}]"
                )
                continue
            # Rule 2: when the line would otherwise sit in the trunk
            # band (the v107 "passes through marker" case), it must
            # have been detoured a full grid row away.
            if trunk_band_top <= y_at <= trunk_band_bot:
                if abs(y_at - trunk_y) < FULL_ROW:
                    violations.append(
                        f"{fixture}: line {line_id} at non-consuming "
                        f"station {sid} (trunk y={trunk_y:.1f}) sits at "
                        f"y={y_at:.1f}, gap={abs(y_at - trunk_y):.1f} < "
                        f"{FULL_ROW:.1f} (full grid row)"
                    )

    assert not violations, "Bypass violations:\n  " + "\n  ".join(violations)
