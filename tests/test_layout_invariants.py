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
from nf_metro.layout.routing import compute_station_offsets
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
