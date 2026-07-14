"""Stage-boundary invariant guards run by ``compute_layout(validate=True)``."""

from __future__ import annotations

import math
import warnings
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NamedTuple

from nf_metro.layout.constants import (
    COLLINEAR_AXIS_TOL,
    COMPONENT_BAND_OVERLAP_TOLERANCE,
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    CURVE_RADIUS,
    EDGE_TO_BUNDLE_CLEARANCE,
    FLOW_ALIGNED_PORT_ADVICE,
    GUARD_TOLERANCE,
    ICON_HALF_HEIGHT,
    INTER_ROW_EDGE_CLEARANCE,
    OFFSET_STEP,
    ROW_BAND_SLACK,
    SAME_COORD_TOLERANCE,
    SAME_Y_TOLERANCE,
    SECTION_Y_GAP,
    SECTION_Y_PADDING,
    STATION_RADIUS_APPROX,
    TITLE_BAND_OVERLAP_FLOOR,
    X_SPACING,
)
from nf_metro.layout.geometry import (
    BBoxXIndex,
    iter_section_overlaps,
    lanes_run_along_x,
    lanes_run_along_y,
    segment_intersects_bbox,
)
from nf_metro.layout.phases._common import (
    _bbox_cols_overlap,
    _canvas_width,
    _restoring_layout_geometry,
    _route_crosses_section_boundary,
    _section_bundle_lines,
    _section_lr_port_anchor_y,
    _side_entered_vertical_feeder_pairs,
    _station_marker_bbox,
    first_vertical_leg_sign,
    first_vertical_leg_x,
    flow_exit_carrier_anchor,
    is_loop_side_branch_station,
    iter_corridor_fed_solo_entries,
    iter_fold_lr_exit_straight_runs,
    iter_fold_lr_exits_short_of_target,
    iter_sole_trunk_continuations,
    iter_stacked_rows_in_rowspan_band,
    marker_cross_exempt,
    routes_through_own_section_interior,
    routes_through_unrelated_sections,
    section_axes,
    section_cross_axis,
    wrap_exit_carrier_anchor,
)
from nf_metro.layout.phases.bbox import (
    _min_drawn_section_bbox_top,
    _predict_section_content_bottom,
    _section_fit_top,
)
from nf_metro.layout.phases.off_track import (
    _is_single_trunk_section,
    _off_track_anchor_of,
    _off_track_lift_sign,
    _off_track_lift_step,
    _off_track_output_below,
)
from nf_metro.layout.phases.single_section import _terminus_y_overhang
from nf_metro.layout.phases.spacing import (
    _placed_name_label_station_ids,
    _residual_label_overlaps,
)
from nf_metro.parser.model import (
    LineSpread,
    MetroGraph,
    Port,
    PortSide,
    Section,
    Station,
    is_converge_junction,
)
from nf_metro.parser.resolve import _expected_flow_side

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from typing import Protocol

    from nf_metro.layout.routing.common import RoutedPath

    class _HasMessage(Protocol):
        def message(self) -> str: ...


class PhaseInvariantError(Exception):
    """Raised when a layout phase produces invalid intermediate state."""


class LayoutInvariantError(PhaseInvariantError):
    """Raised on the render path under ``--strict`` when the settled layout
    violates a Tier-A invariant.

    The render-path chokepoint :func:`assert_render_layout_invariants` runs the
    cheap Tier-A layout guards on the final geometry the renderer is about to
    draw.  Without ``--strict`` a violation is a warning, so the user receives
    a best-effort diagram carrying a visible diagnosis; with ``--strict`` the
    chokepoint raises this instead.  Subclassing :class:`PhaseInvariantError`
    lets the CLI's existing layout-error handler surface it as a clean message
    rather than a traceback.
    """


class BackwardFlowError(ValueError):
    """Raised when a layout places a section so an inter-section edge must
    flow backward against its own row's flow direction.

    Unlike :class:`PhaseInvariantError` (an engine self-check), this is an
    authoring error: the section grid the user supplied cannot be rendered
    honestly.  It is a ``ValueError`` so the CLI surfaces it the same way as
    other invalid-input errors (cf. :class:`CyclicGraphError`).
    """


class MixedEntryDirectionError(ValueError):
    """Raised when one section receives incoming lines from more than one
    approach direction (entry ports on more than one cardinal side).

    Routed lines are undirected polylines with no arrowheads, so a reader
    infers flow direction from how a line enters a section.  When one line
    enters heading one way and another enters a different side heading
    another, the approaches conflict and the diagram cannot show which way
    flow runs.  Like :class:`BackwardFlowError`, this is an authoring error
    (a ``ValueError``) the CLI surfaces as invalid input.
    """


class FoldThresholdError(ValueError):
    """Raised when a ``fold_threshold`` the user set is too small for the map.

    A ``--fold-threshold`` / ``%%metro fold_threshold`` below a map's natural
    width folds its sections into a tighter grid.  Past a point the compacted
    geometry leaves the router no room to separate parallel bundles, size
    concentric bundle corners, or seat a section header clear of a route, and
    the render-path self-checks (:class:`CurveInvariantError`,
    :class:`SectionHeaderClashError`) fire.  Those are engine self-checks, not
    something the author can act on directly; when the abort is attributable to
    a user-set threshold compressing the section grid, the render chokepoint
    reframes it as this authoring error (a ``ValueError`` the CLI surfaces as
    invalid input, cf. :class:`BackwardFlowError`) naming the directive.
    """


def _port_anchor_snapshot(graph: MetroGraph) -> dict[str, tuple[float, float]]:
    """``(x, y)`` of every port station -- the inter-section anchors that
    content placement positions content around.

    A port is an anchor on whichever axis its side dictates: LR/RL ports
    fix the trunk Y, TOP/BOTTOM ports fix the trunk X, and either side's
    cross-axis (an LR port's X, a TB port's Y) is pinned by the structural
    port-positioning phases too.  Snapshotting both coordinates of every
    port therefore covers the full anchor set rather than the LR/RL-Y
    subset, so the guard catches any anchor movement during placement
    regardless of port side or axis.  Paired with
    :func:`_guard_anchors_frozen_during_placement`."""
    out: dict[str, tuple[float, float]] = {}
    for pid in graph.ports:
        st = graph.stations.get(pid)
        if st is not None:
            out[pid] = (st.x, st.y)
    return out


def _guard_anchors_frozen_during_placement(
    graph: MetroGraph, before: dict[str, tuple[float, float]], phase: str
) -> None:
    """A content-placement phase positions content around the resolved
    anchors and must not move one.  Compare each port's ``(x, y)`` against
    the snapshot taken before the phase ran (via
    :func:`_port_anchor_snapshot`) and raise if any moved.  Anchors are set
    only by port-positioning, the row trunk alignment, grid snapping, the
    inter-row cascade and uniform translation -- never by fan / off-track /
    band-fill / balance / recenter placement."""
    after = _port_anchor_snapshot(graph)
    for pid, (x0, y0) in before.items():
        coords = after.get(pid)
        if coords is None:
            continue
        x1, y1 = coords
        if abs(x1 - x0) > COORD_TOLERANCE or abs(y1 - y0) > COORD_TOLERANCE:
            port = graph.ports.get(pid)
            side = port.side.value if port is not None else "?"
            raise PhaseInvariantError(
                f"{phase}: content placement moved {side} port anchor {pid!r} "
                f"from (x={x0:.2f}, y={y0:.2f}) to (x={x1:.2f}, y={y1:.2f}) "
                f"(delta x={x1 - x0:+.2f}, y={y1 - y0:+.2f}); "
                f"placement must leave anchors frozen"
            )


def _guard_coordinates_finite(graph: MetroGraph, phase: str) -> None:
    """After Stage 2.1+: all laid-out stations must have finite coordinates."""
    junction_ids = graph.junction_ids
    for sid, st in graph.stations.items():
        if st.section_id and not st.is_port and sid not in junction_ids:
            if math.isnan(st.x) or math.isnan(st.y):
                raise PhaseInvariantError(
                    f"{phase}: station {sid!r} has NaN coordinates (x={st.x}, y={st.y})"
                )
            if math.isinf(st.x) or math.isinf(st.y):
                raise PhaseInvariantError(
                    f"{phase}: station {sid!r} has infinite coordinates "
                    f"(x={st.x}, y={st.y})"
                )


def _bbox_guarded_stations(
    graph: MetroGraph,
) -> Iterator[tuple[str, Station, Section]]:
    """Yield ``(sid, station, section)`` for each rendered station that a
    bbox-containment guard should check: skips ports, junctions, and
    stations whose section has no sized bbox.  Shared by the marker-edge
    and centre-containment guards so they can't drift on what they exempt.
    """
    junction_ids = graph.junction_ids
    for sid, st in graph.stations.items():
        sec = graph.sections.get(st.section_id or "")
        if not sec or st.is_port or sid in junction_ids or sec.bbox_w == 0:
            continue
        yield sid, st, sec


def _guard_stations_in_sections(graph: MetroGraph, phase: str) -> None:
    """After Stage 2.1+: rendered station markers (and terminus icons) must
    be fully within their section bbox.

    Tightened from station-centre containment to marker-edge containment:
    we expand the station's render-time footprint by ``STATION_RADIUS_APPROX``
    (regular markers) or ``ICON_HALF_HEIGHT`` (terminus / off-track icons)
    and require the expanded box to stay inside the section's bbox.  Centre
    containment alone hides regressions where off-track icons (~16 px half
    height) spill above the bbox top while still being technically "in" the
    section.
    """
    tol = GUARD_TOLERANCE
    for sid, st, sec in _bbox_guarded_stations(graph):
        # Off-track inputs and terminus icons render at icon scale; on-track
        # markers render at station-pill scale.  Use the wider reach so the
        # guard catches icon spill-over above the bbox top.
        half_h = (
            ICON_HALF_HEIGHT
            if (st.off_track or st.is_terminus)
            else STATION_RADIUS_APPROX
        )
        top = st.y - half_h
        bottom = st.y + half_h
        if not (
            sec.bbox_x - tol <= st.x <= sec.bbox_x + sec.bbox_w + tol
            and sec.bbox_y - tol <= top
            and bottom <= sec.bbox_y + sec.bbox_h + tol
        ):
            raise PhaseInvariantError(
                f"{phase}: station {sid!r} marker bbox "
                f"(x={st.x:.1f}, y={top:.1f}..{bottom:.1f}, "
                f"half_h={half_h:.1f}) "
                f"outside section {st.section_id!r} bbox "
                f"({sec.bbox_x:.1f}, {sec.bbox_y:.1f}, "
                f"w={sec.bbox_w:.1f}, h={sec.bbox_h:.1f})"
            )


# Sides that lie along a section's internal flow axis: an LR/RL section
# flows horizontally, so its flow-aligned ports are on the left/right; a
# TB/BT section flows vertically, so its flow-aligned ports are on the
# top/bottom.  A section with at least one flow-aligned port has an edge
# to anchor the start (or end) of its run to the bbox boundary.
_FLOW_ALIGNED_SIDES = {
    "LR": {PortSide.LEFT, PortSide.RIGHT},
    "RL": {PortSide.LEFT, PortSide.RIGHT},
    "TB": {PortSide.TOP, PortSide.BOTTOM},
    "BT": {PortSide.TOP, PortSide.BOTTOM},
}


def _section_lacks_flow_aligned_port(graph: MetroGraph, section: Section) -> bool:
    """True when *section* has ports but none on its flow axis.

    An internally-horizontal (LR/RL) section whose only ports are on the
    top/bottom (or a vertical section with only left/right ports) has no
    flow-aligned edge to pin its run to the bbox, so the engine lays the
    run out past the box.  The bbox-containment guard uses this to emit an
    actionable error for that unsupported directive combination.
    """
    flow_sides = _FLOW_ALIGNED_SIDES.get(section.direction)
    if flow_sides is None:
        return False
    sides = [graph.ports[pid].side for pid in section.port_ids if pid in graph.ports]
    return bool(sides) and not any(s in flow_sides for s in sides)


def _guard_stations_within_bbox(graph: MetroGraph, phase: str) -> None:
    """Always-on postcondition: every station centre must lie within its
    section's bbox (plus a small tolerance).

    Unlike :func:`_guard_stations_in_sections` (which runs only under
    ``validate`` mid-layout and checks marker-edge containment on the Y
    axis), this guard runs on every layout -- including the default render
    path -- and checks the *settled* bbox on both axes.  Forcing
    perpendicular ports on a horizontal section lays its stations out past
    the right of its own bbox, and the engine must reject that loudly
    rather than render it silently.
    """
    tol = GUARD_TOLERANCE
    for sid, st, sec in _bbox_guarded_stations(graph):
        inside_x = sec.bbox_x - tol <= st.x <= sec.bbox_x + sec.bbox_w + tol
        inside_y = sec.bbox_y - tol <= st.y <= sec.bbox_y + sec.bbox_h + tol
        if inside_x and inside_y:
            continue
        detail = (
            f"{phase}: station {sid!r} centre ({st.x:.1f}, {st.y:.1f}) "
            f"outside section {st.section_id!r} bbox "
            f"({sec.bbox_x:.1f}, {sec.bbox_y:.1f}, "
            f"w={sec.bbox_w:.1f}, h={sec.bbox_h:.1f})"
        )
        if _section_lacks_flow_aligned_port(graph, sec):
            detail += (
                f"; section {st.section_id!r} is internally {sec.direction} "
                f"but its only ports are perpendicular to that flow, so the "
                f"run has no flow-aligned port to anchor it to the bbox. "
                f"{FLOW_ALIGNED_PORT_ADVICE}"
            )
        raise PhaseInvariantError(detail)


def _guard_centered_line_spread_balanced(graph: MetroGraph, phase: str) -> None:
    """Each ``centered`` section's weave must balance about its trunk.

    Two invariants are enforced for every section resolving to ``centered``
    (the graph-wide ``line_spread`` default or a per-section override):

    1. The line base tracks are placed symmetrically at
       ``(i - (N-1)/2) * line_gap``, whose mean is exactly zero, so the
       shared trunk sits on the vertical midline.
    2. Each line's *exclusive run* (stations carrying only that one line)
       lands on the correct side of its section's shared trunk: a line
       above the centre keeps its exclusive stations at-or-above the trunk
       and a line below the centre keeps them at-or-below it.  This catches
       a regression where the fork-equalize pass collapses an exclusive run
       back onto the trunk midline, leaving the panel top/centre-heavy
       instead of symmetric.

    No-op when no section is centered, fewer than two lines exist, or there
    are no lines at all (nothing to balance).
    """
    centered_anywhere = graph.line_spread is LineSpread.CENTERED or any(
        mode is LineSpread.CENTERED for mode in graph.line_spread_overrides.values()
    )
    if not centered_anywhere:
        return
    n = len(graph.lines)
    if n < 2:
        return
    from nf_metro.layout.constants import LINE_GAP

    bases = [(i - (n - 1) / 2) * LINE_GAP for i in range(n)]
    mean = sum(bases) / n
    if abs(mean) > GUARD_TOLERANCE:
        raise PhaseInvariantError(
            f"{phase}: centered line_spread base tracks not symmetric about zero "
            f"(mean={mean:.3f}, bases={bases})"
        )

    # Sign of each line's symmetric base: <0 above the trunk, >0 below it,
    # 0 on the centre.  Y increases downward, so an above-centre line wants
    # its exclusive stations at smaller Y than the trunk.
    line_index = {lid: i for i, lid in enumerate(graph.lines)}
    line_sign = {lid: (i - (n - 1) / 2) for lid, i in line_index.items()}

    # Real (visible, on-track, non-port) stations grouped by section.
    by_section: dict[str | None, list[str]] = {}
    for sid, st in graph.stations.items():
        if st.is_port or st.is_hidden or st.off_track:
            continue
        by_section.setdefault(st.section_id, []).append(sid)

    # A non-centre line's exclusive station must be displaced to its side
    # of the trunk; a zero/negative displacement means the run has
    # collapsed onto (or crossed) the trunk midline.  Any genuine offset is
    # a whole track unit (one section y-spacing of pixels), far larger than
    # the guard tolerance, so a tolerance floor cleanly separates "offset"
    # from "collapsed" independent of the absolute y-spacing in use.
    min_offset = GUARD_TOLERANCE
    for sec_id, members in by_section.items():
        # Only centered sections must straddle; a bundle or rails section in
        # the same graph legitimately cascades or runs as parallel rails.
        if graph.section_line_spread(sec_id) is not LineSpread.CENTERED:
            continue
        # Skip sections with no vertical spread: an un-laid-out graph (all
        # Y == 0) or a genuinely flat single-track section has no weave to
        # balance, and the side check is only meaningful once coordinates
        # have been assigned.
        member_ys = [graph.stations[s].y for s in members]
        if max(member_ys) - min(member_ys) <= GUARD_TOLERANCE:
            continue
        # The trunk is the set of multi-line stations; use their mean Y.
        trunk_ys = [
            graph.stations[s].y for s in members if len(graph.station_lines(s)) >= 2
        ]
        if not trunk_ys:
            continue
        trunk_y = sum(trunk_ys) / len(trunk_ys)
        for s in members:
            lines = graph.station_lines(s)
            if len(lines) != 1:
                continue
            sign = line_sign.get(lines[0], 0.0)
            if abs(sign) < 1e-9:
                continue  # centre line: exclusive stations belong on trunk
            # signed_offset > 0 means "on this line's side"; Y grows
            # downward, so an above-centre line (sign<0) wants smaller Y.
            dy = graph.stations[s].y - trunk_y
            signed_offset = -dy if sign < 0 else dy
            if signed_offset <= min_offset:
                side = "above" if sign < 0 else "below"
                raise PhaseInvariantError(
                    f"{phase}: centered line_spread exclusive station '{s}' on "
                    f"{side}-centre line '{lines[0]}' is not offset to its "
                    f"side of the trunk (y={graph.stations[s].y:.1f}, "
                    f"trunk_y={trunk_y:.1f}, signed_offset={signed_offset:.1f}, "
                    f"required>={min_offset:.1f})"
                )


def _guard_rail_one_station_per_column(graph: MetroGraph, phase: str) -> None:
    """Rails place one distinct station per column.

    ``line_spread: rails`` is a one-station-per-X layout: each column holds a
    single station.  A genuine interchange is one shared station, so it occupies
    a single column legitimately; two *distinct* on-rail stations sharing a
    column stack their markers and labels and read as a false interchange.
    Flag any rails section where two visible on-rail stations share an X.
    No-op when no section resolves to rails.
    """
    rails_anywhere = graph.line_spread is LineSpread.RAILS or any(
        mode is LineSpread.RAILS for mode in graph.line_spread_overrides.values()
    )
    if not rails_anywhere:
        return
    by_section: dict[str | None, list[str]] = {}
    for sid, st in graph.stations.items():
        if st.is_port or st.is_hidden or st.off_track:
            continue
        by_section.setdefault(st.section_id, []).append(sid)
    for sec_id, members in by_section.items():
        if graph.section_line_spread(sec_id) is not LineSpread.RAILS:
            continue
        ordered = sorted(members, key=lambda m: graph.stations[m].x)
        for a, b in zip(ordered, ordered[1:]):
            xa, xb = graph.stations[a].x, graph.stations[b].x
            if abs(xb - xa) <= GUARD_TOLERANCE:
                raise PhaseInvariantError(
                    f"{phase}: rails section '{sec_id}' places distinct stations "
                    f"'{a}' and '{b}' in the same column (x={xa:.1f}); rails "
                    f"require one station per column"
                )


def _guard_interchange_bar_clears_non_members(graph: MetroGraph, phase: str) -> None:
    """An interchange connector bar must not cross a non-member station.

    The bar runs vertically between the top and bottom member rails at the
    interchange column; any other station sharing that column within the span
    would be cut by the bar (a station-as-elbow violation).  Auto-detection
    abstains or reorders to avoid this, but the seating is geometry-dependent,
    so verify the settled layout directly.
    """
    tol = GUARD_TOLERANCE
    for ic in graph.interchanges:
        member_ids = set(ic.member_ids)
        members = [graph.stations[m] for m in ic.member_ids if m in graph.stations]
        if len(members) < 2:
            continue
        x = members[0].x
        ys = [m.y for m in members]
        lo, hi = min(ys), max(ys)
        for s in graph.stations.values():
            if s.is_port or s.id in member_ids:
                continue
            if abs(s.x - x) < tol and lo - tol < s.y < hi + tol:
                raise PhaseInvariantError(
                    f"{phase}: interchange {ic.node_id!r} bar (x={x:.1f}, "
                    f"y {lo:.1f}..{hi:.1f}) spans non-member station {s.id!r} "
                    f"at ({s.x:.1f}, {s.y:.1f})"
                )


def _guard_interchange_label_clears_connector(graph: MetroGraph, phase: str) -> None:
    """An interchange's own label must clear its connector bridge.

    A cross-track interchange draws a vertical connector spanning its members'
    Y range at the anchor column; the anchor carries the label.  Label placement
    treats the interchange as one long station and pushes that label to the
    outer edge (above the top member or below the bottom one).  This verifies
    the settled label never lands on the bridge it spans.

    Rail-mode interchanges are exempt: their spanning pills keep the rail-panel
    alternation idiom (the label rides beside the pill, not across a bridge).
    """
    from nf_metro.layout.labels import label_glyph_ink_bbox, segment_strikes_label
    from nf_metro.layout.phases.spacing import _probe_label_placements

    spanning = [
        ic
        for ic in graph.interchanges
        if len([m for m in ic.member_ids if m in graph.stations]) >= 2
        and (anchor := graph.stations.get(ic.node_id)) is not None
        and not graph.is_rail_section(anchor.section_id)
    ]
    if not spanning:
        return

    probe = _probe_label_placements(graph, allow_hyphenation=True)
    if probe is None:
        return
    offsets, _routes, placements = probe
    placement_by_sid = {p.station_id: p for p in placements}
    for ic in spanning:
        p = placement_by_sid.get(ic.node_id)
        if p is None or not p.text.strip():
            continue
        members = [graph.stations[m] for m in ic.member_ids if m in graph.stations]
        x = members[0].x
        ys = [
            m.y + offsets.get((m.id, lid), 0.0)
            for m in members
            for lid in graph.station_lines(m.id)
        ]
        top, bot = min(ys), max(ys)
        if segment_strikes_label(x, top, x, bot, p):
            bbox = label_glyph_ink_bbox(p)
            raise PhaseInvariantError(
                f"{phase}: interchange {ic.node_id!r} label {p.text!r} "
                f"(glyph-ink bbox {bbox[0]:.1f},{bbox[1]:.1f}-"
                f"{bbox[2]:.1f},{bbox[3]:.1f}) lands on its connector bridge "
                f"(x={x:.1f}, y {top:.1f}..{bot:.1f})"
            )


def _guard_ports_on_boundaries(graph: MetroGraph, phase: str) -> None:
    """After Stage 3.1+: ports must sit on their section's bounding box edge."""
    tolerance = GUARD_TOLERANCE
    for pid, port in graph.ports.items():
        st = graph.stations.get(pid)
        sec = graph.sections.get(st.section_id or "") if st else None
        if not st or not sec or sec.bbox_w == 0:
            continue
        on_left = abs(st.x - sec.bbox_x) <= tolerance
        on_right = abs(st.x - (sec.bbox_x + sec.bbox_w)) <= tolerance
        on_top = abs(st.y - sec.bbox_y) <= tolerance
        on_bottom = abs(st.y - (sec.bbox_y + sec.bbox_h)) <= tolerance
        if not (on_left or on_right or on_top or on_bottom):
            raise PhaseInvariantError(
                f"{phase}: port {pid!r} at ({st.x:.1f}, {st.y:.1f}) "
                f"not on any edge of section {st.section_id!r} bbox "
                f"({sec.bbox_x:.1f}, {sec.bbox_y:.1f}, "
                f"w={sec.bbox_w:.1f}, h={sec.bbox_h:.1f})"
            )


def _guard_port_station_coords_synced(graph: MetroGraph, phase: str) -> None:
    """A port's Station record and Port record must hold the same coordinates.

    Every port is both a ``Station`` (placed by layout, read by routing and
    rendering) and a ``Port`` (read by later phases as the settled position).
    Phases move ports through :func:`_set_port_y` / :func:`_set_port_x` /
    :func:`shift_section`, which write both; a raw ``station.y = ...`` that
    skips the Port record desyncs the two, so a later phase reads a stale
    position and leaves the inter-section run kinked.
    """
    tolerance = GUARD_TOLERANCE
    for pid, port in graph.ports.items():
        st = graph.stations.get(pid)
        if st is None:
            continue
        if abs(st.x - port.x) > tolerance or abs(st.y - port.y) > tolerance:
            raise PhaseInvariantError(
                f"{phase}: port {pid!r} station record ({st.x:.1f}, {st.y:.1f}) "
                f"desynced from port record ({port.x:.1f}, {port.y:.1f})"
            )


def _tb_top_entry_drop_overshoot(
    graph: MetroGraph,
) -> list[tuple[str, float]]:
    """Return ``(section_id, gap)`` for TB sections whose first station sits
    further below the box top than the standard padding despite the TOP
    entry being a clean vertical drop (see :func:`_adjust_tb_entry_shifts`
    for why such a drop is always vertical).

    Sections with a perpendicular (LEFT/RIGHT) entry are excluded: they
    legitimately shift their stations down to clear the entry port.
    """
    tol = GUARD_TOLERANCE
    offenders: list[tuple[str, float]] = []
    for sid, sec in graph.sections.items():
        if sec.direction != "TB" or sec.bbox_h == 0:
            continue
        entry_ports = [
            graph.ports[pid] for pid in sec.entry_ports if pid in graph.ports
        ]
        top_ports = [p for p in entry_ports if p.side == PortSide.TOP]
        if not top_ports:
            continue
        if any(p.side in (PortSide.LEFT, PortSide.RIGHT) for p in entry_ports):
            continue
        # A hidden trunk-head (e.g. a fan-out hub) is a valid drop target, so
        # do not filter to visible markers further down the trunk.
        body = [
            graph.stations[s]
            for s in sec.station_ids
            if s in graph.stations and not graph.stations[s].is_port
        ]
        if not body:
            continue
        first = min(body, key=lambda st: st.y)
        port_xs = [graph.stations[p.id].x for p in top_ports if p.id in graph.stations]
        if not any(abs(first.x - px) <= tol for px in port_xs):
            continue
        gap = first.y - sec.bbox_y
        if gap > SECTION_Y_PADDING + tol:
            offenders.append((sid, gap))
    return offenders


def _guard_tb_top_entry_drop_hugs_top(graph: MetroGraph, phase: str) -> None:
    """Final: a clean TB TOP-entry drop must seat its first station at the
    standard top padding, with no unused in-section reservation."""
    offenders = _tb_top_entry_drop_overshoot(graph)
    if offenders:
        sid, gap = offenders[0]
        raise PhaseInvariantError(
            f"{phase}: section {sid!r} first station sits {gap:.1f}px below "
            f"its box top despite a clean vertical TOP-entry drop "
            f"(expected <= {SECTION_Y_PADDING:.1f})"
        )


def _guard_side_entered_vertical_top_not_below_feeder(
    graph: MetroGraph, phase: str
) -> None:
    """Final: a TB/BT section entered from a perpendicular side keeps its bbox
    top no lower than the contiguous row-mate immediately to its left.

    The side entry's approach runs across the band above the section's first
    internal station, so the content-hug shrink must not lower the top below
    the feeder row-mate that flows into it (which would drop the section badge
    beneath the rest of its grid row).
    """
    tol = SAME_COORD_TOLERANCE
    for section, neighbour in _side_entered_vertical_feeder_pairs(graph):
        if section.bbox_y - neighbour.bbox_y > tol:
            raise PhaseInvariantError(
                f"{phase}: side-entered vertical section {section.id!r} "
                f"bbox top y={section.bbox_y:.1f} drops "
                f"{section.bbox_y - neighbour.bbox_y:.1f}px below its feeder "
                f"row-mate {neighbour.id!r} (top y={neighbour.bbox_y:.1f})"
            )


def _guard_symmetric_diamond_branches_straddle_trunk(
    graph: MetroGraph, phase: str
) -> None:
    """Final: in ``diamond_style='symmetric'`` a clean horizontal 2-way diamond
    keeps both branches off the trunk row.

    A fork F whose only two successors B1, B2 rejoin at a common successor J,
    with F and J on the same row (the trunk runs straight through), must place
    both branches clear of that row.  A branch landing on the trunk row means a
    grid-snap collapsed the diamond.

    Skipped when F or J is a port: an entry-port fork keeps its first branch on
    the through-track and fans the rest to one side, a deliberately asymmetric
    placement rather than a collapse.
    """
    if graph.diamond_style != "symmetric":
        return
    from nf_metro.layout.phases.fan_bundles import _iter_fork_join_diamonds

    tol = SAME_COORD_TOLERANCE
    for fork, b1, b2, join in _iter_fork_join_diamonds(graph):
        trunk_y = fork.y
        for b in (b1, b2):
            if abs(b.y - trunk_y) <= tol:
                raise PhaseInvariantError(
                    f"{phase}: symmetric diamond branch {b.id!r} sits on the trunk "
                    f"row y={trunk_y:.1f} (fork {fork.id!r} -> {join.id!r}); the "
                    f"diamond collapsed instead of straddling"
                )


def _guard_symmetric_diamond_branches_half_pitch(graph: MetroGraph, phase: str) -> None:
    """Final: in ``diamond_style='symmetric'`` a clean 2-way diamond straddles
    its trunk symmetrically at half pitch, so its bubble is one grid unit tall.

    Stage 6.17 compacts each symmetric fork-join diamond onto
    ``trunk_y +/- 0.5 * pitch``.  Two failure modes this catches:

    - asymmetry: the two branches sit at unequal distances from the trunk
      (a later pass pulled one branch but not the other), and
    - re-inflation: a branch back at full pitch (``trunk_y +/- 1`` slot),
      meaning the compaction was dropped, so the diamond reads as tall as a
      3-way fan with an empty trunk row between its branches.

    Full pitch is read from the nearest off-trunk on-grid sibling in the
    section, so the magnitude check needs no ``y_spacing`` and is skipped
    for a solo diamond with no sibling fan (only symmetry is checked there).
    """
    if graph.diamond_style != "symmetric":
        return
    from nf_metro.layout.phases.fan_bundles import _iter_symmetric_diamonds

    tol = SAME_COORD_TOLERANCE
    half_grid = graph.half_grid_station_ids
    for fork, lo, hi, _join in _iter_symmetric_diamonds(graph):
        trunk_y = fork.y
        d_lo = trunk_y - lo.y
        d_hi = hi.y - trunk_y
        if abs(d_lo - d_hi) > 1.0:
            raise PhaseInvariantError(
                f"{phase}: symmetric diamond branches {lo.id!r}/{hi.id!r} "
                f"straddle trunk y={trunk_y:.1f} asymmetrically "
                f"(above={d_lo:.1f}, below={d_hi:.1f})"
            )
        section = graph.sections.get(fork.section_id) if fork.section_id else None
        if section is None:
            continue
        sibling_dists = [
            abs(st.y - trunk_y)
            for sid in section.station_ids
            if sid not in half_grid
            and (st := graph.stations.get(sid)) is not None
            and not (st.is_port or st.is_hidden or st.off_track)
            and abs(st.y - trunk_y) > tol
        ]
        if not sibling_dists:
            continue
        full_pitch = min(sibling_dists)
        # Compacted branches sit at 0.5 * full_pitch; the 0.75 midpoint
        # between half and full pitch separates a compacted branch from an
        # uncompacted one at full pitch.
        if d_hi > 0.75 * full_pitch:
            raise PhaseInvariantError(
                f"{phase}: symmetric diamond branches {lo.id!r}/{hi.id!r} sit "
                f"{d_hi:.1f}px from trunk y={trunk_y:.1f}, not the half pitch "
                f"{0.5 * full_pitch:.1f} expected beside a full-pitch "
                f"{full_pitch:.1f} fan; the diamond was not compacted"
            )


# A same-layer sibling group may sit at most this many columns upstream of the
# junction that merges it before the parallel run reads as a distant-terminus
# bow: one column is the immediate merge, a second is a modest bulge left alone.
_MAX_SIBLING_MERGE_SLACK = 2


class _LateSiblingMerge(NamedTuple):
    junction: str
    junction_layer: int
    sibling_layer: int
    sibling_count: int


def _converge_sibling_merge_violations(
    graph: MetroGraph,
) -> Iterator[_LateSiblingMerge]:
    """Yield convergence junctions whose same-layer siblings merge far downstream.

    A convergence junction inserted before a multi-source terminus should merge
    each group of same-layer sibling sources close to them, so the siblings
    never bow out and run parallel across the gap to a distant terminus.  For
    every source layer shared by 2+ of a junction's in-section direct sources
    that sits more than ``_MAX_SIBLING_MERGE_SLACK`` columns upstream of the
    junction, yields the offending group.
    """
    for sid, st in graph.stations.items():
        if not is_converge_junction(sid):
            continue
        srcs_by_layer: dict[int, set[str]] = defaultdict(set)
        for edge in graph.edges_to(sid):
            src = graph.station_for_edge_source(edge)
            if src.section_id != st.section_id:
                continue
            srcs_by_layer[src.layer].add(edge.source)
        for layer, srcs in srcs_by_layer.items():
            if len(srcs) >= 2 and st.layer - layer > _MAX_SIBLING_MERGE_SLACK:
                yield _LateSiblingMerge(sid, st.layer, layer, len(srcs))


def _guard_converge_siblings_merge_locally(graph: MetroGraph, phase: str) -> None:
    """Final: same-layer fan-in siblings merge near their column, not far downstream.

    When 2+ sibling sources share a layer and feed a common terminus, the
    convergence junction must sit within a column or two of them so they meet
    promptly.  If a longer parallel path pushes the terminus (and its lone
    junction) far to the right, the short-path siblings would otherwise run
    parallel all the way there, bowing the fan out to fill the gap (issue
    #1296).
    """
    for jid, jlayer, shared, count in _converge_sibling_merge_violations(graph):
        raise PhaseInvariantError(
            f"{phase}: convergence junction {jid!r} (layer {jlayer}) merges "
            f"{count} same-layer sibling sources at layer {shared}; they run "
            f"parallel to a distant terminus instead of merging locally at "
            f"layer {shared + 1}"
        )


def _guard_section_bboxes_positive(graph: MetroGraph, phase: str) -> None:
    """After Stage 1.1+: non-empty sections must have positive-size bboxes."""
    for sid, sec in graph.sections.items():
        if not sec.station_ids:
            continue
        if sec.bbox_w < 0 or sec.bbox_h < 0:
            raise PhaseInvariantError(
                f"{phase}: section {sid!r} has negative bbox "
                f"(w={sec.bbox_w:.1f}, h={sec.bbox_h:.1f})"
            )


def _guard_no_negative_grid_columns(graph: MetroGraph, phase: str) -> None:
    """After Stage 1.1+: no section may sit at a negative grid column.

    The auto-layout serpentine packer steps a return row leftward from
    its fold bridge; without normalization that walk can run off the left
    edge into negative columns, which renders the section's badge left of
    everything and snakes the inter-section trunk down the left margin.
    ``infer_section_layout`` normalizes the grid so the leftmost column is 0;
    this guard fails loudly if that ever regresses.
    """
    # Read from grid_overrides (populated with an explicit column for every
    # placed section) rather than Section.grid_col, whose -1 sentinel for
    # "auto" is indistinguishable from a genuine column -1.
    offenders = {
        sid: override[0]
        for sid, override in graph.grid_overrides.items()
        if override[0] < 0
    }
    if offenders:
        raise PhaseInvariantError(
            f"{phase}: sections at negative grid columns "
            f"(serpentine packer ran off the left edge): {offenders}"
        )


def _guard_independent_components_disjoint(graph: MetroGraph, phase: str) -> None:
    """After Stage 1.3: independently-stacked components must not overlap.

    When the section meta-graph splits into 2+ weakly-connected components
    and the author pinned no explicit ``%%metro grid:`` positions,
    :func:`place_sections` lays each component out in its own local column
    grid and stacks the components vertically.  Stacking is only correct if
    the components occupy disjoint vertical bands; an off-by-one in the
    stacking cursor would let one component's bbox overlap another's,
    producing tangled, ambiguous output.  This guard fails loudly if the
    stacked bands ever overlap.

    No-op for single-component graphs and for explicit-grid graphs (which
    deliberately keep the shared grid and may interleave components).
    """
    from nf_metro.layout.section_placement import (
        _component_extent,
        _weakly_connected_components,
    )

    if not graph.sections or graph.section_dag is None or graph._explicit_grid:
        return

    components = _weakly_connected_components(graph, graph.section_dag.section_edges)
    if len(components) <= 1:
        return

    def band(comp: set[str]) -> tuple[float, float]:
        _, top, bottom = _component_extent([graph.sections[s] for s in comp])
        return top, bottom

    bands = sorted((band(c), c) for c in components)
    for (lo_band, lo_comp), (hi_band, hi_comp) in zip(bands, bands[1:]):
        # Bands are sorted by top edge; overlap if the lower band's bottom
        # protrudes past the next band's top.
        if lo_band[1] > hi_band[0] + COMPONENT_BAND_OVERLAP_TOLERANCE:
            raise PhaseInvariantError(
                f"{phase}: independently-stacked components overlap vertically: "
                f"{sorted(lo_comp)} (band {lo_band[0]:.1f}..{lo_band[1]:.1f}) "
                f"vs {sorted(hi_comp)} (band {hi_band[0]:.1f}..{hi_band[1]:.1f})"
            )


def _guard_multi_section_cell_packed(graph: MetroGraph, phase: str) -> None:
    """After Stage 1.3: a multi-section cell's members pack without overlap.

    A ``%%metro grid:`` directive naming several sections shares one grid cell
    among them (:attr:`MetroGraph.cell_packs`); :func:`_pack_cells` lays them
    side-by-side along the flow axis.  This guard fails loudly if any pair's
    bounding boxes overlap horizontally, or if a member did not land in its
    declared cell -- either would mean the packer left two sections sitting on
    top of one another.
    """
    if not graph.cell_packs:
        return

    def footprint(section: Section) -> tuple[float, float]:
        left = section.offset_x + section.bbox_x
        return left, left + section.bbox_w

    for (col, row), member_ids in graph.cell_packs.items():
        members = [graph.sections[m] for m in member_ids if m in graph.sections]
        for section in members:
            if (section.grid_col, section.grid_row) != (col, row):
                raise PhaseInvariantError(
                    f"{phase}: section {section.id!r} was declared in packed cell "
                    f"({col},{row}) but placed at "
                    f"({section.grid_col},{section.grid_row})"
                )
        ordered = sorted(members, key=lambda s: footprint(s)[0])
        for upstream, downstream in zip(ordered, ordered[1:]):
            if footprint(upstream)[1] > footprint(downstream)[0] + SAME_COORD_TOLERANCE:
                raise PhaseInvariantError(
                    f"{phase}: packed sections in cell ({col},{row}) overlap: "
                    f"{upstream.id!r} right={footprint(upstream)[1]:.1f} > "
                    f"{downstream.id!r} left={footprint(downstream)[0]:.1f}"
                )


def _guard_no_section_overlap(graph: MetroGraph, phase: str) -> None:
    """Section bounding boxes must not overlap.

    An all-pairs AABB test over the settled section bboxes -- the runtime
    counterpart of ``check_section_overlap`` in the offline validator.  Two
    overlapping boxes tangle their interiors and let a route cut through a
    section it does not belong to, so the layout must never emit them.  A small
    negative tolerance lets flush (touching) boxes pass but flags any genuine
    overlap.
    """
    for (
        sid_a,
        sid_b,
        (ax1, ay1, ax2, ay2),
        (bx1, by1, bx2, by2),
    ) in iter_section_overlaps(graph):
        raise PhaseInvariantError(
            f"{phase}: sections {sid_a!r} and {sid_b!r} overlap: "
            f"A=({ax1:.0f},{ay1:.0f},{ax2:.0f},{ay2:.0f}) "
            f"B=({bx1:.0f},{by1:.0f},{bx2:.0f},{by2:.0f})"
        )


def _guard_explicit_grid_directions(graph: MetroGraph, phase: str) -> None:
    """Explicit-grid sections keep the LR default unless they carry an
    explicit %%metro direction.

    A section's grid position is the author's manual layout intent, not a
    flow-direction signal. Direction inference therefore skips explicit-grid
    sections; this guard fails loudly if a future change ever lets inference
    reorient one (e.g. by reading override-aware positions), which would
    silently elongate serpentine-stacked maps vertically.
    """
    offenders = {
        sid: graph.sections[sid].direction
        for sid in graph._explicit_grid - graph._explicit_directions
        if sid in graph.sections and graph.sections[sid].direction != "LR"
    }
    if offenders:
        raise PhaseInvariantError(
            f"{phase}: explicit-grid sections with no %%metro direction were "
            f"inferred to a non-LR direction: {offenders}"
        )


def _section_has_exit_on_side(
    graph: MetroGraph, section: Section, side: PortSide
) -> bool:
    """Whether *section* has an exit port on *side*.

    An exit on the side facing the target marks an author who deliberately
    redirected the flow there (e.g. an explicit ``%%metro exit: left``).
    """
    return any(
        (port := graph.ports.get(pid)) is not None and port.side == side
        for pid in section.exit_ports
    )


def _guard_fold_relocated_flow_ports_face_connections(
    graph: MetroGraph, phase: str
) -> None:
    """A fold-relocated section's flow-axis port faces its connecting sections.

    A lowered fold threshold relocates sections onto a return row, where a
    left/right entry/exit authored for the unfolded grid can land on the edge
    opposite the column its connecting sections occupy; the connecting leg then
    wraps back across the section's own box.  For a section the fold compressed,
    a left/right entry must sit on the side its producer sections do and an exit
    on the side its consumers do, whenever those all lie strictly to one side.
    """
    dag = graph.section_dag
    if dag is None or not graph._fold_compressed_sections:
        return
    producer_cols: dict[str, set[int]] = defaultdict(set)
    consumer_cols: dict[str, set[int]] = defaultdict(set)
    for src_id, tgt_id in dag.section_edges:
        if src_id in graph.sections and tgt_id in graph.sections:
            consumer_cols[src_id].add(graph.sections[tgt_id].grid_col)
            producer_cols[tgt_id].add(graph.sections[src_id].grid_col)

    for sec_id in graph._fold_compressed_sections:
        section = graph.sections.get(sec_id)
        if section is None or section.direction not in ("LR", "RL"):
            continue
        col = section.grid_col
        for ports, cols, is_entry in (
            (section.entry_ports, producer_cols[sec_id], True),
            (section.exit_ports, consumer_cols[sec_id], False),
        ):
            expected = _expected_flow_side(cols, col)
            if expected is None:
                continue
            for pid in ports:
                port = graph.ports.get(pid)
                if port is None or port.side not in (PortSide.LEFT, PortSide.RIGHT):
                    continue  # cross-axis port, not on the flow axis
                if port.side != expected:
                    raise PhaseInvariantError(
                        f"{phase}: fold-relocated section {sec_id!r} has a "
                        f"flow-axis {'entry' if is_entry else 'exit'} port "
                        f"{pid!r} on the {port.side.value} edge, but its "
                        f"connecting sections all sit to the {expected.value} "
                        f"(cols {sorted(cols)} vs {col}); the connecting leg "
                        f"wraps back across the section"
                    )


def _guard_no_same_row_backward_feed(graph: MetroGraph) -> None:
    """Reject a same-row inter-section edge that runs against the source
    section's flow direction with no exit facing the target.

    Within a single row, flow direction is read purely from horizontal
    position -- routed lines are undirected polylines with no arrowheads.  A
    producer placed at a column past a consumer it feeds therefore reads as
    flowing the wrong way, and the only route to that consumer ploughs back
    across the producer's own box.  Cross-row backward feeds are exempt: they
    descend into a separate row, which the inter-section router carries around
    cleanly and which the reader sees as a distinct branch.  Sections whose
    author redirected the exit toward the target (an explicit ``exit`` on the
    facing side) are exempt too.
    """
    dag = graph.section_dag
    if dag is None:
        return
    for src_id, tgt_id in sorted(dag.section_edges):
        src = graph.sections.get(src_id)
        if src is None or tgt_id not in graph.sections:
            continue
        src_pos = graph.grid_overrides.get(src_id)
        tgt_pos = graph.grid_overrides.get(tgt_id)
        if src_pos is None or tgt_pos is None or src_pos[1] != tgt_pos[1]:
            continue
        src_col, tgt_col = src_pos[0], tgt_pos[0]
        if src.direction == "LR" and tgt_col < src_col:
            facing = PortSide.LEFT
        elif src.direction == "RL" and tgt_col > src_col:
            facing = PortSide.RIGHT
        else:
            continue
        if _section_has_exit_on_side(graph, src, facing):
            continue
        raise BackwardFlowError(
            f"section '{src_id}' (grid column {src_col}) feeds '{tgt_id}' "
            f"(column {tgt_col}) backward against its {src.direction} flow in "
            f"the same row: a routed line cannot show this reversal and would "
            f"cross '{src_id}'s own box.  Place '{src_id}' ahead of '{tgt_id}' "
            f"in the row's flow direction, move it to a separate row, or add "
            f"'%%metro exit: {facing.value}' so the exit faces the target."
        )


def _guard_no_mixed_entry_directions(graph: MetroGraph) -> None:
    """Reject a section whose incoming lines approach from more than one
    cardinal direction (entry ports on more than one side).

    Routed metro lines are undirected polylines with no arrowheads, so the
    reader infers flow from how a line enters a section.  When one line enters
    a section heading one way (say, rightward into a LEFT port) and another
    enters a different side heading another (downward into a TOP port, or
    leftward into a RIGHT port), the approaches conflict and the rendered flow
    direction is unreadable.  Reject the topology rather than emit an ambiguous
    diagram.

    A section fed from a single side reads cleanly: parser-inferred multi-side
    entry hints that collapse to one natural side therefore pass, and an entry
    port carrying no line is not an approach.
    """
    for sec_id, section in sorted(graph.sections.items()):
        sides: dict[PortSide, set[str]] = defaultdict(set)
        for pid in section.entry_ports:
            port = graph.ports.get(pid)
            if port is None:
                continue
            lines = {edge.line_id for edge in graph.edges_to(pid)}
            if lines:
                sides[port.side].update(lines)
        if len(sides) <= 1:
            continue
        detail = ", ".join(
            f"{side.value} ({'+'.join(sorted(lines))})"
            for side, lines in sorted(sides.items(), key=lambda kv: kv[0].value)
        )
        raise MixedEntryDirectionError(
            f"section '{sec_id}' receives lines from more than one approach "
            f"direction: {detail}.  Routed lines have no arrowheads, so entries "
            f"from different sides leave the flow direction ambiguous.  Feed "
            f"'{sec_id}' from a single side (align the producers' grid columns "
            f"so every line enters the same edge), or split it into separate "
            f"sections."
        )


def _assert_exit_on_carrier_row(
    graph: MetroGraph,
    phase: str,
    anchor: Callable[
        [MetroGraph, str, Section, set[str]], tuple[float, list[str]] | None
    ],
    label: str,
    consequence: str,
) -> None:
    """Raise when an exit *anchor* selects a carrier row the exit is off.

    Shared body for the flow and wrap exit-anchor guards: iterate LEFT/RIGHT
    exit ports, and for those the *anchor* function claims, require the exit
    sit on the returned carrier row within ``GUARD_TOLERANCE``.
    """
    junction_ids = graph.junction_ids
    for pid, port in graph.ports.items():
        if port.is_entry or port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        sec = graph.sections.get(port.section_id)
        if sec is None:
            continue
        found = anchor(graph, pid, sec, junction_ids)
        if found is None:
            continue
        carrier_y, carrier_ids = found
        port_y = graph.stations[pid].y
        if abs(port_y - carrier_y) > GUARD_TOLERANCE:
            raise PhaseInvariantError(
                f"{phase}: {label} {pid!r} at y={port_y:.1f} is off its carrier "
                f"row y={carrier_y:.1f} (carriers {sorted(carrier_ids)}); {consequence}"
            )


def _guard_flow_exit_anchored_to_carrier(graph: MetroGraph, phase: str) -> None:
    """A flow-aligned exit anchoring to a shared carrier row must sit on it.

    Otherwise the link from the carriers to the exit climbs to the port and
    renders as a diagonal inside the section instead of a clean horizontal run
    with one riser in the inter-section gap.  Scope (single carrier or parallel
    bundle, direct entry or fan-out junction, clear corridor, non-fold section)
    is exactly :func:`flow_exit_carrier_anchor`; everything else anchors
    elsewhere by design.
    """
    _assert_exit_on_carrier_row(
        graph,
        phase,
        flow_exit_carrier_anchor,
        "exit port",
        "the boundary run will render as a diagonal instead of a riser",
    )


def _guard_wrap_exit_anchored_to_carrier(graph: MetroGraph, phase: str) -> None:
    """A wrapping flow-aligned exit anchoring to a shared carrier row sits on it.

    When the exit and its target entry sit on the same horizontal side, the line
    wraps vertically around the target rather than hopping straight across the
    gap; pulling the exit onto the downstream row then leaves the in-section link
    a diagonal from the carrying station into the box corner instead of a clean
    horizontal with one riser in the corridor.  Scope is exactly
    :func:`wrap_exit_carrier_anchor`.
    """
    _assert_exit_on_carrier_row(
        graph,
        phase,
        wrap_exit_carrier_anchor,
        "wrapping exit port",
        "the in-section link will render as a diagonal into the box corner",
    )


def _guard_fold_lr_exit_follows_target(graph: MetroGraph, phase: str) -> None:
    """A fold's LEFT/RIGHT exit must reach a target settled along its flow.

    A target seated against the flow keeps its own descent (an intentional
    staircase) and is exempt; see :func:`iter_fold_lr_exits_short_of_target`.
    """
    for pid, tgt in iter_fold_lr_exits_short_of_target(graph, GUARD_TOLERANCE):
        raise PhaseInvariantError(
            f"{phase}: fold exit port {pid!r} at y={graph.stations[pid].y:.1f} "
            f"sits a sub-row short of its target entry {tgt.id!r} at "
            f"y={tgt.y:.1f}; the inter-section run will render with a jog"
        )


def _guard_fold_lr_exit_sections_share_bbox_bottom(
    graph: MetroGraph, phase: str
) -> None:
    """A straight folded LR/RL run's two sections must end at the same bbox bottom.

    The run is horizontal, so an unequal bbox bottom on the exit vs the target
    section leaves the line a different distance above each section's bottom
    edge -- a lopsided clearance even though the run itself is straight.  Scope
    is :func:`iter_fold_lr_exit_straight_runs`.
    """
    for pid, tgt in iter_fold_lr_exit_straight_runs(graph, GUARD_TOLERANCE):
        assert tgt.section_id is not None  # generator only yields placed targets
        exit_section = graph.sections[graph.ports[pid].section_id]
        tgt_section = graph.sections[tgt.section_id]
        exit_bot = exit_section.bbox_y + exit_section.bbox_h
        tgt_bot = tgt_section.bbox_y + tgt_section.bbox_h
        if abs(exit_bot - tgt_bot) > GUARD_TOLERANCE:
            raise PhaseInvariantError(
                f"{phase}: straight folded run from exit {pid!r} joins "
                f"{exit_section.id!r} (bbox bottom {exit_bot:.1f}) to "
                f"{tgt_section.id!r} (bbox bottom {tgt_bot:.1f}); the "
                f"{abs(exit_bot - tgt_bot):.1f}px mismatch makes the run clear "
                f"the two sections by different distances"
            )


def _guard_stacked_rows_fill_rowspan_band(graph: MetroGraph, phase: str) -> None:
    """Single-row sections stacked beside a rowspan neighbour must fill its band.

    A column holding single-row sections stacked one per grid row beside a
    ``grid_row_span > 1`` section spanning those rows must be distributed across
    that section's band: the topmost's bbox top meets the band top and the
    bottommost's bbox bottom meets the band bottom.  A topmost section above the
    band top has spread out of the layout into the title band; a bottommost
    above the band bottom floats high with empty slack beneath it.  Scope is
    :func:`iter_stacked_rows_in_rowspan_band`.
    """
    for stack, band_top, band_bot in iter_stacked_rows_in_rowspan_band(
        graph, 2 * GUARD_TOLERANCE
    ):
        top, bot = stack[0], stack[-1]
        if band_top - top.bbox_y > GUARD_TOLERANCE:
            raise PhaseInvariantError(
                f"{phase}: section {top.id!r} (col {top.grid_col}, top of a stack "
                f"beside a rowspan band) has bbox top {top.bbox_y:.1f} above the "
                f"band top {band_top:.1f}; it rises {band_top - top.bbox_y:.1f}px "
                f"out of the band into the title space"
            )
        bot_edge = bot.bbox_y + bot.bbox_h
        if band_bot - bot_edge > GUARD_TOLERANCE:
            raise PhaseInvariantError(
                f"{phase}: section {bot.id!r} (col {bot.grid_col}, bottom of a "
                f"stack beside a rowspan band) has bbox bottom {bot_edge:.1f} "
                f"above the band bottom {band_bot:.1f}; {band_bot - bot_edge:.1f}px "
                f"of empty slack sits below it"
            )


def _exit_perp_to_flow(src_port: Port, src_section: Section) -> bool:
    """Whether an exit port sits on a side perpendicular to its section's flow.

    The complement of :data:`_FLOW_ALIGNED_SIDES`: a perpendicular exit lies on
    a boundary edge rather than on a trunk (a TB section's exit dips below its
    last station), so its Y is a structural boundary, not a row that aligns
    with the downstream consumer.
    """
    flow_sides = _FLOW_ALIGNED_SIDES.get(src_section.direction)
    return flow_sides is not None and src_port.side not in flow_sides


def _exit_off_consumer_trunk(src_port: Port, src_section: Section) -> bool:
    """Whether a side (LEFT/RIGHT) entry fed by this exit must anchor to its
    consumer rather than the source.

    A side entry aligns to its source only for a same-row horizontal trunk feed:
    a flow-aligned LEFT/RIGHT exit on a horizontal-flow (LR/RL) section, whose
    exit Y is a station row that already matches the consumer.  Every other exit
    sits on a structural boundary edge whose Y is unrelated to the consumer's
    row -- a perpendicular exit on any section, or any exit on a vertical-flow
    (TB/BT) section, where the flow-aligned TOP/BOTTOM exit dips onto the
    section's bottom/top edge below or above its stations.  Anchoring the entry
    to such a Y forces a diagonal into the first station instead of a riser in
    the column gap with a horizontal turn-in.
    """
    return _exit_perp_to_flow(src_port, src_section) or lanes_run_along_x(
        src_section.direction
    )


def _perp_fed_entry_consumer_y(
    graph: MetroGraph, port_id: str, port: Port
) -> float | None:
    """Consumer Y a LEFT/RIGHT entry fed by a perpendicular exit must anchor to.

    Returns the Y of the single internal consumer station when *port* is a
    LEFT/RIGHT entry whose sole feed is a same-row exit port sitting
    perpendicular to its section's flow, and whose consumers share one Y.
    Anchoring the entry there keeps the inter-section climb a riser in the
    column gap with a horizontal turn-in, rather than a diagonal into the
    first station (#908).  Returns ``None`` when the port is out of scope.

    A vertical-flow (TB/BT) entry section is exempt: a LEFT/RIGHT entry is
    perpendicular to its trunk, so it must sit a station gap above the trunk
    head, not on the consumer's own row -- pinning it there leaves no drop room
    and slants a multi-line bundle into the trunk.
    """
    if not port.is_entry or port.side not in (PortSide.LEFT, PortSide.RIGHT):
        return None
    entry_section = graph.sections.get(port.section_id)
    if entry_section is None or lanes_run_along_x(entry_section.direction):
        return None
    fed_by_perp_exit = False
    for edge in graph.edges_to(port_id):
        src_port = graph.ports.get(edge.source)
        if src_port is None or src_port.is_entry:
            continue
        src_section = graph.sections.get(src_port.section_id)
        if src_section is None or src_section.grid_row != entry_section.grid_row:
            continue
        if _exit_perp_to_flow(src_port, src_section):
            fed_by_perp_exit = True
            break
    if not fed_by_perp_exit:
        return None
    consumer_ys = {
        round(st.y, 1)
        for edge in graph.edges_from(port_id)
        if not (st := graph.station_for_edge_target(edge)).is_port
        and st.section_id == entry_section.id
    }
    if len(consumer_ys) != 1:
        return None
    return consumer_ys.pop()


def _guard_perp_fed_entry_anchored_to_consumer(graph: MetroGraph, phase: str) -> None:
    """A LEFT/RIGHT entry fed by a perpendicular exit sits on its consumer row.

    When such an entry anchors to the source exit's boundary Y instead, the
    inter-section bundle climbs into the single consumer station via a diagonal
    rather than rising in the column gap and entering horizontally (#908).
    Scope is exactly :func:`_perp_fed_entry_consumer_y`.
    """
    for pid, port in graph.ports.items():
        consumer_y = _perp_fed_entry_consumer_y(graph, pid, port)
        if consumer_y is None:
            continue
        port_y = graph.stations[pid].y
        if abs(port_y - consumer_y) > GUARD_TOLERANCE:
            raise PhaseInvariantError(
                f"{phase}: entry port {pid!r} at y={port_y:.1f} is off its "
                f"consumer row y={consumer_y:.1f}; the bundle will climb into "
                f"the consumer via a diagonal instead of a horizontal turn-in"
            )


def _guard_corridor_fed_solo_rides_trunk(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
) -> None:
    """A corridor-fed single-line section anchors its entry + consumer on the trunk.

    With one present line there is no bundle to keep ordered, so the lane the
    line held in the upstream multi-line section only drags the lone consumer
    off the section trunk -- the box then reserves empty space for lines that
    never enter it.  The vertical corridor absorbs the lane step, so both the
    LEFT/RIGHT entry port and the consumer it feeds must ride offset 0.  Scope
    is exactly :func:`iter_corridor_fed_solo_entries`.
    """
    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)
    for sec_id, pid, line_id in iter_corridor_fed_solo_entries(graph, SAME_Y_TOLERANCE):
        port_off = offsets.get((pid, line_id), 0.0)
        if abs(port_off) > COORD_TOLERANCE:
            raise PhaseInvariantError(
                f"{phase}: section {sec_id!r} entry port {pid!r} sits at offset "
                f"{port_off:.1f} for sole line {line_id!r}; a corridor-fed "
                "single-line section must anchor its entry on the trunk"
            )
        for sid in graph.sections[sec_id].station_ids:
            st = graph.stations.get(sid)
            if st is None or st.is_port or line_id not in graph.station_lines(sid):
                continue
            st_off = offsets.get((sid, line_id), 0.0)
            if abs(st_off) > COORD_TOLERANCE:
                raise PhaseInvariantError(
                    f"{phase}: section {sec_id!r} station {sid!r} sits at offset "
                    f"{st_off:.1f} for sole line {line_id!r}; the lone consumer "
                    "must ride the section trunk, not the upstream bundle lane"
                )


def _guard_perp_entry_clears_vertical_trunk_head(graph: MetroGraph, phase: str) -> None:
    """A LEFT/RIGHT entry into a vertical-flow section clears its trunk head.

    The entry is perpendicular to a TB/BT trunk, so it must sit a station gap
    off the trunk head: the bundle enters level and drops onto each lane.  When
    the port instead shares an internal station's Y the line routes through the
    marker and a multi-line bundle slants in for want of drop room (#1054).
    """
    for pid, port in graph.ports.items():
        if not port.is_entry or port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        section = graph.sections.get(port.section_id)
        if section is None or not lanes_run_along_x(section.direction):
            continue
        port_y = graph.stations[pid].y
        section_ports = set(section.entry_ports) | set(section.exit_ports)
        for sid in section.station_ids:
            if sid in section_ports:
                continue
            st = graph.stations.get(sid)
            if st is not None and abs(port_y - st.y) <= GUARD_TOLERANCE:
                raise PhaseInvariantError(
                    f"{phase}: entry port {pid!r} at y={port_y:.1f} shares Y "
                    f"with internal station {sid!r} (y={st.y:.1f}) in "
                    f"vertical-flow section {section.id!r}; the line routes "
                    f"through the marker with no room for a level turn-in"
                )


def _guard_post_convergence_trunk_continues(graph: MetroGraph, phase: str) -> None:
    """The sole continuation of a line-shedding station continues on its row.

    When a horizontal-section station carries strictly more lines than its only
    in-section successor -- because some of its lines ended there, whether a
    merged branch that stopped (#946) or a bundled line that terminated (#977)
    -- and that successor is its only forward path, the chain is linear with no
    sibling branch to fan toward. The successor must share the predecessor's Y;
    otherwise the trunk dives onto a line base row, painting a needless diagonal
    (or an in-section V-kink) right after the junction.

    A predecessor whose flow also leaves elsewhere -- a section-exit edge or a
    bypass V routing a line *around* the successor -- genuinely forks, so its
    successor would legitimately drop off the trunk; the predecessor's *only*
    forward path in the whole graph must be this successor. Vertical (TB/BT)
    sections, ports, hidden, and off-track stations are out of scope.
    """
    for _section_id, pred, node in iter_sole_trunk_continuations(graph):
        pred_y = graph.stations[pred].y
        node_y = graph.stations[node].y
        if abs(node_y - pred_y) > GUARD_TOLERANCE:
            raise PhaseInvariantError(
                f"{phase}: continuation station {node!r} at y={node_y:.1f} "
                f"is off its predecessor {pred!r} y={pred_y:.1f}; the trunk "
                f"dives onto a branch row right after the junction"
            )


def _guard_tall_anchor_stack_well_formed(graph: MetroGraph, phase: str) -> None:
    """A tall-anchor vertical stack keeps its anchor's downstream chain
    stacked in one column within the anchor's row span, all horizontal.

    The vertical-stack packer relies on every stacked section flowing
    horizontally so the inter-section router carriage-returns each drop; a
    section that slipped out of the anchor's column, off its row span, or into
    a vertical (TB/BT) direction would dangle beside empty space or force a
    perpendicular port on an LR sink. No-op when the packer did not fire.
    """
    from nf_metro.layout.auto_layout import (
        _detect_tall_anchor_chain,
        _transitive_successors,
    )

    anchor = _detect_tall_anchor_chain(graph)
    if anchor is None or graph.section_dag is None:
        return

    tail = _transitive_successors(anchor, graph.section_dag.successors)

    horizontal = {"LR", "RL"}
    non_horizontal = {
        sid: graph.sections[sid].direction
        for sid in (anchor, *tail)
        if graph.sections[sid].direction not in horizontal
    }
    if non_horizontal:
        raise PhaseInvariantError(
            f"{phase}: tall-anchor stack sections are not horizontal-flow: "
            f"{non_horizontal}"
        )

    tail_cols = {graph.sections[sid].grid_col for sid in tail}
    if len(tail_cols) != 1:
        raise PhaseInvariantError(
            f"{phase}: tall-anchor tail spans columns {sorted(tail_cols)}; "
            f"expected a single stacked column"
        )

    anchor_sec = graph.sections[anchor]
    span_top = anchor_sec.grid_row
    span_bottom = anchor_sec.grid_row + anchor_sec.grid_row_span - 1
    escaped = {
        sid: graph.sections[sid].grid_row
        for sid in tail
        if not span_top <= graph.sections[sid].grid_row <= span_bottom
    }
    if escaped:
        raise PhaseInvariantError(
            f"{phase}: tall-anchor tail rows {escaped} fall outside the anchor "
            f"{anchor!r} row span [{span_top}, {span_bottom}]"
        )


def _guard_row_gaps(graph: MetroGraph, phase: str, *, section_y_gap: float) -> None:
    """Final phase: column-overlapping adjacent-row section pairs must
    keep at least ``section_y_gap`` between the upper section's bbox
    bottom and the lower section's bbox top.

    Sections that don't share horizontal extent are unconstrained --
    their vertical proximity has no visual impact.
    """
    tol = SAME_COORD_TOLERANCE
    sections_by_row_start: dict[int, list[tuple[str, Section]]] = defaultdict(list)
    for sid, sec in graph.sections.items():
        if sec.bbox_w <= 0 or sec.bbox_h <= 0:
            continue
        sections_by_row_start[sec.grid_row].append((sid, sec))
    if not sections_by_row_start:
        return

    deepest: tuple[float, float, str, str] | None = None
    for usid, us in graph.sections.items():
        if us.bbox_w <= 0 or us.bbox_h <= 0:
            continue
        next_row = us.grid_row + us.grid_row_span
        for lsid, ls in sections_by_row_start.get(next_row, []):
            if not _bbox_cols_overlap(us, ls):
                continue
            gap = ls.bbox_y - (us.bbox_y + us.bbox_h)
            deficit = section_y_gap - gap
            if deficit > tol and (deepest is None or deficit > deepest[0]):
                deepest = (deficit, gap, usid, lsid)
    if deepest is None:
        return
    deficit, gap, usid, lsid = deepest
    raise PhaseInvariantError(
        f"{phase}: row gap below required: sections {usid!r} (bottom) "
        f"and {lsid!r} (top) overlap horizontally and are {gap:.1f}px "
        f"apart, expected >= {section_y_gap:.1f}px "
        f"(deficit {deficit:.1f}px)"
    )


def _guard_section_top_padding(
    graph: MetroGraph,
    phase: str,
    *,
    section_y_padding: float,
    section_y_gap: float,
    offsets: dict[tuple[str, str], float],
) -> None:
    """Final phase: each section's bbox top must clear its highest marker.

    The mirror of the bottom-padding contract (:func:`_guard_section_bottom_padding`).
    After :func:`_fit_bboxes_to_content_top` runs, every section's bbox top
    should sit at its content-anchored target (a full ``section_y_padding``
    above the highest marker, unless gap-bounded by the row above).  A
    bbox top below that target means a later pass crowded the topmost
    marker against the box edge (issue #406).
    """
    tol = 1.0
    for section in graph.sections.values():
        if section.bbox_h <= 0:
            continue
        target = _section_fit_top(
            graph, section, section_y_padding, section_y_gap, offsets
        )
        if target is None:
            continue
        if section.bbox_y > target + tol:
            raise PhaseInvariantError(
                f"{phase}: section {section.id!r} bbox top {section.bbox_y:.1f} "
                f"sits below its content-anchored target {target:.1f} "
                f"(highest marker crowds the bbox top edge)"
            )


def _guard_section_bottom_padding(
    graph: MetroGraph,
    phase: str,
    *,
    section_y_padding: float,
    offsets: dict[tuple[str, str], float],
) -> None:
    """Final phase: each section's bbox bottom must clear its lowest marker.

    The mirror of the top-padding contract (:func:`_guard_section_top_padding`).
    After :func:`_shrink_bboxes_to_content_bottom` runs, every section's bbox
    bottom sits at or below its content-anchored target: a full
    ``section_y_padding`` below the lowest marker's drawn bundle pill, or
    further down still when a row-mate's bottom pins it there.  A bbox
    bottom above that target means a later pass crowded the lowest marker
    against the box edge.
    """
    tol = 1.0
    for section in graph.sections.values():
        if section.bbox_h <= 0:
            continue
        target = _predict_section_content_bottom(
            graph, section, section_y_padding, offsets
        )
        if target is None:
            continue
        bbox_bot = section.bbox_y + section.bbox_h
        if bbox_bot < target - tol:
            raise PhaseInvariantError(
                f"{phase}: section {section.id!r} bbox bottom {bbox_bot:.1f} "
                f"sits above its content-anchored target {target:.1f} "
                f"(lowest marker crowds the bbox bottom edge)"
            )


def _guard_rail_above_label_band(graph: MetroGraph, phase: str) -> None:
    """A rail section must reserve room above its top rail for above-hanging labels.

    Single-rail stations on a panel's top rail are labelled above the top rail
    (``labels._rail_label_side``); the engine reserves a band for them by pushing
    the rails down (``rail_mode._layout_section_rails``).  If that band is smaller
    than the labels' footprint, ``place_labels`` grows the panel box upward at
    render time and can climb into the section stacked above it.  Verify the
    reservation independently of the code that makes it.
    """
    if not graph.has_rail_sections:
        return
    # Function-local: a module-level import would close a layout import cycle.
    from nf_metro.layout.rail_mode import _rail_label_band, rail_above_label_ids

    tol = 1.0
    for section in graph.sections.values():
        if section.bbox_h <= 0 or not graph.is_rail_section(section.id):
            continue
        above_ids = rail_above_label_ids(graph, section)
        if not above_ids:
            continue
        per_line = graph._rail_y.get(section.id) or {}
        needed = _rail_label_band(graph, above_ids)
        reserved = min(per_line.values()) - section.bbox_y
        if reserved + tol < needed:
            raise PhaseInvariantError(
                f"{phase}: rail section {section.id!r} reserves {reserved:.1f}px "
                f"above its top rail but its above-labels need {needed:.1f}px; "
                f"render-time label growth would climb out of the box"
            )


def _guard_rail_stations_seat_on_rails(graph: MetroGraph, phase: str) -> None:
    """Every rail station's used-rail Ys must land on its lines' fixed rails.

    A rail station records ``rail_used_ys`` parallel to its line order
    (``rail_mode._layout_section_rails``); the renderer draws a knob at each so
    the glyph seats on every line it carries.  If a used-rail Y drifts off its
    line's rail (``graph._rail_y``), the knob, and a coloured-marker
    interchange's fill, would float off the rail.  Verify the seating directly.
    """
    if not graph.has_rail_sections:
        return
    tol = 1.0
    for section in graph.sections.values():
        if not graph.is_rail_section(section.id):
            continue
        per_line = graph._rail_y.get(section.id) or {}
        if not per_line:
            continue
        for sid in section.station_ids:
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.off_track or st.is_blank_terminus:
                continue
            lines = graph.station_lines_ordered(sid)
            if len(st.rail_used_ys) != len(lines):
                continue
            for lid, y in zip(lines, st.rail_used_ys):
                rail_y = per_line.get(lid)
                if rail_y is None:
                    continue
                if abs(y - rail_y) > tol:
                    raise PhaseInvariantError(
                        f"{phase}: rail station {sid!r} seats line {lid!r} at "
                        f"y={y:.1f}, off its rail {rail_y:.1f}"
                    )


def _guard_terminus_icons_within_bbox(graph: MetroGraph, phase: str) -> None:
    """Final phase: TB/BT terminus file icons must fit inside the section bbox.

    Vertical-flow termini stack their file icon (and caption) below or
    above the station marker; the section bbox must reserve that extent so
    the icon doesn't spill past the box edge (issue #254).
    """
    tol = 1.0
    for section in graph.sections.values():
        if section.bbox_h <= 0 or lanes_run_along_y(section.direction):
            continue
        top = section.bbox_y
        bottom = section.bbox_y + section.bbox_h
        for sid in section.station_ids:
            st = graph.stations.get(sid)
            if st is None or not st.is_terminus:
                continue
            above, below = _terminus_y_overhang(st, section.direction, graph)
            if st.y + below > bottom + tol:
                raise PhaseInvariantError(
                    f"{phase}: terminus {sid!r} icons extend to "
                    f"{st.y + below:.1f}, past section {section.id!r} bbox "
                    f"bottom {bottom:.1f}"
                )
            if st.y - above < top - tol:
                raise PhaseInvariantError(
                    f"{phase}: terminus {sid!r} icons extend to "
                    f"{st.y - above:.1f}, above section {section.id!r} bbox "
                    f"top {top:.1f}"
                )


def _guard_single_trunk_off_track_step(graph: MetroGraph, phase: str) -> None:
    """Single-trunk sections lift off-track stations by the base pitch.

    A section that is a single horizontal trunk has no parallel tracks, so
    its off-track lift step stays at the base content pitch
    (``graph._base_y_spacing``) rather than the spread-widened ``y_spacing``
    (issue #580).  When the base pitch is recorded (auto ``y_spacing``), each
    off-track station in such a section must sit an integer number of base
    steps above its anchor, never at a wider pitch that would strand the icon
    far above the trunk.

    The base-pitch widening is a Y-axis (diagonal-label) concern, so this
    applies only to horizontal-flow (LR/RL) sections, whose off-track band is
    offset along Y; a vertical-flow section offsets along X by the column pitch,
    which is never widened.
    """
    base = graph._base_y_spacing
    if base is None or base <= 0:
        return
    anchor_of = _off_track_anchor_of(graph)
    junction_ids = graph.junction_ids
    tol = 1.0
    for off_id, anchor_id in anchor_of.items():
        off_st = graph.stations.get(off_id)
        anchor = graph.stations.get(anchor_id)
        if off_st is None or anchor is None:
            continue
        section = graph.sections.get(off_st.section_id or "")
        if (
            section is None
            or not lanes_run_along_y(section.direction or "LR")
            or not _is_single_trunk_section(graph, section, junction_ids)
        ):
            continue
        gap = anchor.y - off_st.y
        if gap <= tol:
            continue
        nearest_multiple = round(gap / base) * base
        if abs(gap - nearest_multiple) > tol:
            raise PhaseInvariantError(
                f"{phase}: off-track {off_id!r} sits {gap:.1f}px above anchor "
                f"{anchor_id!r} on single-trunk section {section.id!r}, not an "
                f"integer multiple of the base step {base:.1f} -- the widened "
                f"diagonal-label pitch leaked into the lift"
            )


def _guard_off_track_input_column_stack(graph: MetroGraph, phase: str) -> None:
    """On a single-trunk section, an off-track input hugs its consumer by its
    same-column stack depth, not the whole anchor group's size.

    When a consumer is fed by off-track stations in different columns (an input
    above it, a producer-fed output beside it), the lift step is counted per
    column.  Counting the whole anchor group instead strands a lone-in-its-column
    input an extra slot up over an empty row above an earlier trunk station
    (issue #651).  Restricted to single-trunk sections, whose lift pitch carries
    no stacked line bands that could legitimately bump an input past its slot.

    A "column" is a shared flow-axis coordinate and the gap is measured on the
    cross axis (:func:`section_cross_axis`), so the check holds for an LR/RL
    trunk (columns along X, band stacked on Y) and a TB/BT one alike.
    """
    from nf_metro.layout.engine import compute_min_y_spacing

    junction_ids = graph.junction_ids
    y_spacing = compute_min_y_spacing(graph)
    anchor_of = _off_track_anchor_of(graph)
    tol = 1.0

    def _flow_coord(st: Station) -> float:
        flow, _cross = section_axes(graph.sections.get(st.section_id or ""))
        return round(getattr(st, flow), 1)

    col_group: dict[tuple[str | None, float, str], int] = defaultdict(int)
    for off_id, anchor_id in anchor_of.items():
        st = graph.stations.get(off_id)
        if st is not None:
            col_group[(st.section_id, _flow_coord(st), anchor_id)] += 1

    for off_id, anchor_id in anchor_of.items():
        off_st = graph.stations.get(off_id)
        anchor = graph.stations.get(anchor_id)
        if off_st is None or anchor is None:
            continue
        if not any(e.target == anchor_id for e in graph.edges_from(off_id)):
            continue  # producer-fed sink, not an input
        section = graph.sections.get(off_st.section_id or "")
        if section is None or not _is_single_trunk_section(
            graph, section, junction_ids
        ):
            continue
        step = _off_track_lift_step(graph, section, junction_ids, y_spacing)
        n = col_group[(off_st.section_id, _flow_coord(off_st), anchor_id)]
        cross = section_cross_axis(section)
        gap = abs(getattr(anchor, cross) - getattr(off_st, cross))
        if gap > n * step + tol:
            raise PhaseInvariantError(
                f"{phase}: off-track input {off_id!r} sits {gap:.1f}px "
                f"({gap / step:.1f} slots) from consumer {anchor_id!r} on "
                f"single-trunk section {section.id!r}, but only {n} off-track "
                f"station(s) share its column and anchor -- it is stranded past "
                f"an empty slot (expected at most {n * step:.1f}px)"
            )


# Same-column on-track stations sit at least one row pitch apart, and the
# half-grid symfan idiom places members at half a pitch.  A sparse loop station
# crowded below this fraction of a pitch from a column neighbour can only have
# been shifted toward it, so the threshold sits between the half-grid offset and
# a full row.
_LOOP_STATION_COLUMN_CLEARANCE_FRACTION = 0.6


def _guard_sparse_loop_station_clears_column_neighbour(
    graph: MetroGraph, phase: str
) -> None:
    """A sparse single-line loop station clears its same-column neighbours by
    at least a row pitch.

    ``_shift_sparse_loop_stations_to_clear_bundle`` (Stage 6.14) shifts a
    sparse loop station (one edge in, one edge out, both endpoints on the
    section trunk) one row off the trunk only to clear a busier same-row
    sibling whose inbound bundle crosses its column.  Fired without that
    crossing, the shift pushes the station a partial pitch toward a
    same-column neighbour, crowding the two markers and stranding the row it
    skipped (issue #1071).  The complementary
    :func:`_guard_no_line_crosses_non_consumer` catches the opposite error
    of skipping a needed shift.
    """
    from nf_metro.layout.engine import compute_min_y_spacing

    pitch = compute_min_y_spacing(graph)
    if pitch <= 0:
        return
    floor = _LOOP_STATION_COLUMN_CLEARANCE_FRACTION * pitch
    half_grid = graph.half_grid_station_ids
    tol = 1.0
    for section in graph.sections.values():
        if section.bbox_h <= 0 or section.direction not in ("LR", "RL"):
            continue
        trunk_y = _section_lr_port_anchor_y(graph, section)
        if trunk_y is None:
            continue
        port_ids = set(section.entry_ports) | set(section.exit_ports)
        members = [
            st
            for sid in section.station_ids
            if sid not in port_ids
            and (st := graph.stations.get(sid)) is not None
            and not (st.is_port or st.is_hidden or st.off_track)
            and sid not in half_grid
        ]
        for st in members:
            ins = graph.edges_to(st.id)
            outs = graph.edges_from(st.id)
            if len(ins) != 1 or len(outs) != 1:
                continue
            src = graph.stations.get(ins[0].source)
            tgt = graph.stations.get(outs[0].target)
            if src is None or tgt is None:
                continue
            if (
                abs(src.y - trunk_y) > SAME_COORD_TOLERANCE
                or abs(tgt.y - trunk_y) > SAME_COORD_TOLERANCE
                or abs(st.y - trunk_y) <= SAME_COORD_TOLERANCE
            ):
                continue
            for other in members:
                if other.id == st.id or abs(other.x - st.x) > SAME_COORD_TOLERANCE:
                    continue
                gap = abs(other.y - st.y)
                if tol < gap < floor:
                    raise PhaseInvariantError(
                        f"{phase}: sparse loop station {st.id!r} (y={st.y:.1f}, "
                        f"x={st.x:.1f}) sits only {gap:.1f}px from same-column "
                        f"neighbour {other.id!r} (y={other.y:.1f}), under one row "
                        f"pitch ({pitch:.1f}) -- shifted toward it without a "
                        f"crossing bundle to clear"
                    )


def _guard_off_track_consumer_on_trunk(graph: MetroGraph, phase: str) -> None:
    """An off-track input's consumer that continues straight into the
    section trunk sits level with that successor.

    When a multi-line bundle enters a section and converges on one deep
    first station (the off-track-input consumer), that station heads the
    section trunk.  Dragging it off the trunk (issue #650) leaves the
    onward edge a near-vertical climb whose lines merge into one stroke.
    Restricted to consumers with exactly one on-track in-section successor
    so a genuine on-track fork's off-row branches don't trip it.

    "On the trunk" means sharing the successor's cross coordinate
    (:func:`section_cross_axis`): a shared Y for an LR/RL trunk, a shared X
    for a TB/BT one.
    """
    junction_ids = graph.junction_ids
    tol = 1.0
    consumers = {
        anchor_id
        for off_id, anchor_id in _off_track_anchor_of(graph).items()
        if any(e.target == anchor_id for e in graph.edges_from(off_id))
    }
    for cons_id in consumers:
        cons = graph.stations.get(cons_id)
        if cons is None or cons.is_port or cons_id in junction_ids:
            continue
        succs = [
            tgt
            for e in graph.edges_from(cons_id)
            if not (tgt := graph.station_for_edge_target(e)).is_port
            and tgt.id not in junction_ids
            and not tgt.off_track
            and tgt.section_id == cons.section_id
        ]
        distinct = {s.id for s in succs}
        if len(distinct) != 1:
            continue
        succ = succs[0]
        section = graph.sections.get(cons.section_id or "")
        cross = section_cross_axis(section) if section is not None else "y"
        cons_c, succ_c = getattr(cons, cross), getattr(succ, cross)
        if abs(cons_c - succ_c) > tol:
            raise PhaseInvariantError(
                f"{phase}: off-track consumer {cons_id!r} {cross}={cons_c:.1f} "
                f"dragged off the section trunk; its continuation "
                f"{succ.id!r} sits at {cross}={succ_c:.1f} "
                f"({abs(cons_c - succ_c):.0f}px climb)"
            )


def _guard_symfan_entry_port_on_feeder_trunk(graph: MetroGraph, phase: str) -> None:
    """A symmetric entry fork's port stays on its in-row feeder's trunk.

    Under ``diamond_style: symmetric`` a two-way entry fork's branches straddle
    the section's LR entry port.  When exactly one same-row section's exit port
    feeds that entry port, the two must share a Y so the inter-section run is
    straight; centering the fork on the fork midline instead pulls the port off
    that trunk and drags the whole row's baseline with it (issue #1299).
    Restricted to a single same-row feeder so a cross-row feed (which wraps
    between rows and needn't align) does not trip it.
    """
    from nf_metro.layout.phases.fan_bundles import _symfan_entry_port_feeder_y

    if graph.diamond_style != "symmetric":
        return
    tol = 1.0
    for section in graph.sections.values():
        feeder = _symfan_entry_port_feeder_y(graph, section)
        if feeder is None:
            continue
        entry_port, feeder_y = feeder
        port_y = graph.stations[entry_port].y
        if abs(port_y - feeder_y) > tol:
            raise PhaseInvariantError(
                f"{phase}: symmetric entry fork {section.id!r} port sits at "
                f"y={port_y:.1f} but its in-row feeder arrives at y={feeder_y:.1f} "
                f"({abs(port_y - feeder_y):.0f}px off the trunk); the fork must "
                f"straddle a port fixed on that trunk, not recentre it"
            )


def _guard_off_track_not_hub(graph: MetroGraph, phase: str) -> None:
    """No off-track station has both a predecessor and a successor.

    ``off_track`` lifts a station clear of the trunk so something else can
    continue past it undisturbed (an input feeding a downstream consumer, or
    a producer-fed sink with nothing after it). A station with edges on both
    sides is a pass-through hub: it has no trunk slot to protect, since
    nothing needs to route around it there. Marking one off-track anyway
    lifts it for no reason and forces its outgoing edge to detour back down
    to rejoin the trunk (issue #1295). The parser already refuses to set the
    flag on a hub; this catches any other path that sets it directly.
    """
    junction_ids = graph.junction_ids
    for sid, st in graph.stations.items():
        if not st.off_track or st.is_port or sid in junction_ids:
            continue
        if graph.is_hub(sid):
            raise PhaseInvariantError(
                f"{phase}: off-track station {sid!r} has both a predecessor "
                f"and a successor; it is a pass-through hub with nothing to "
                f"protect, so off_track should not have been set"
            )


def _guard_no_stacked_elbow_graze(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """Two stacked, non-parallel inter-section risers must not graze.

    When two different lines descend the same inter-section gap as risers
    that merely meet at one elbow band (their Y spans overlap by less than
    ``MIN_CORRIDOR_Y_OVERLAP``, rather than running parallel), they are two
    separate corridors and must be distributed across the gap width.  Packed
    within ``BUNDLE_TO_BUNDLE_CLEARANCE`` of each other their opposing elbows
    overlap and the lines graze instead of reading as distinct streams.
    """
    from nf_metro.layout.routing.invariants import check_stacked_elbow_clearance

    _raise_on_first_violation(
        graph, phase, check_stacked_elbow_clearance, offsets, routes
    )


def _guard_no_station_overlap(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
) -> None:
    """Final-phase: no two station marker bboxes may overlap at render
    time, else one station hides another in the SVG.

    Sweep-line: bboxes are sorted by left edge, and the inner loop breaks
    once a candidate's left edge passes the current bbox's right edge.
    """
    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)
    boxes: list[tuple[str, tuple[float, float, float, float]]] = []
    for sid in graph.stations:
        b = _station_marker_bbox(graph, sid, offsets=offsets)
        if b is not None:
            boxes.append((sid, b))
    boxes.sort(key=lambda item: item[1][0])
    tol = SAME_COORD_TOLERANCE
    n = len(boxes)
    for i in range(n):
        s1, (x1, y1, X1, Y1) = boxes[i]
        for j in range(i + 1, n):
            s2, (x2, y2, X2, Y2) = boxes[j]
            if x2 >= X1 - tol:
                break  # Sorted by left edge; no further X-overlap possible.
            if y1 < Y2 - tol and y2 < Y1 - tol:
                raise PhaseInvariantError(
                    f"{phase}: position clash: {s1!r} at "
                    f"({(x1 + X1) / 2:.1f},{(y1 + Y1) / 2:.1f}) overlaps "
                    f"{s2!r} at ({(x2 + X2) / 2:.1f},{(y2 + Y2) / 2:.1f})"
                )


def _guard_no_coincident_station_coords(
    graph: MetroGraph, phase: str, *, tolerance: float = 1.0
) -> None:
    """Final-phase: no two distinct visible stations may share a centre.

    Offset-independent companion to ``_guard_no_station_overlap``: it reads
    ``Station.x``/``.y`` directly, so it catches branches collapsed onto the
    same track even where per-line routing offsets nudge the marker bboxes
    just clear of each other.  Rail-mode stations are exempt -- their markers
    render as per-rail knobs across the rail bundle, so a shared centre is
    not a visual collision there.
    """
    exempt_rail = graph.has_rail_sections
    placed: list[tuple[str, float, float]] = []
    for sid, st in graph.stations.items():
        if st.is_port or st.is_hidden or (exempt_rail and graph.station_is_rail(sid)):
            continue
        placed.append((sid, st.x, st.y))
    placed.sort(key=lambda p: p[1])
    for i, (sid, x, y) in enumerate(placed):
        for oid, ox, oy in placed[i + 1 :]:
            if ox - x > tolerance:
                break  # Sorted by x; no further coincidence possible.
            if abs(y - oy) <= tolerance:
                raise PhaseInvariantError(
                    f"{phase}: {sid!r} and {oid!r} share coordinate ({x:.1f}, {y:.1f})"
                )


def _guard_no_line_crosses_non_consumer(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """Final-phase: no rendered line segment may pass through a
    station marker whose station neither consumes nor produces that
    line.

    Complements ``_guard_no_station_overlap``: station/station marker
    overlap catches one class of clash; this catches the other --
    a line bundle routed at a Y that crosses an off-trunk station's
    marker bbox while bypassing it (the "breeze-past" pattern).
    A common trigger is a sparse single-line consumer (e.g. ``grea``
    in the differential-functional section, consuming only rnaseq)
    sharing its trunk-Y row with a busier sibling whose inbound
    bundle traverses the sparse consumer's column.
    """
    from nf_metro.layout.routing.common import apply_route_offsets

    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)
    if routes is None:
        from nf_metro.layout.routing import route_edges

        try:
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:  # noqa: BLE001 - routing failure surfaces elsewhere
            return

    boxes: list[tuple[str, tuple[float, float, float, float]]] = []
    station_lines_cache: dict[str, set[str]] = {}
    for sid in graph.stations:
        if marker_cross_exempt(graph, sid):
            continue
        bbox = _station_marker_bbox(graph, sid, offsets=offsets)
        if bbox is None:
            continue
        boxes.append((sid, bbox))
        station_lines_cache[sid] = set(graph.station_lines(sid))
    if not boxes:
        return
    index = BBoxXIndex(boxes)

    for r in routes:
        pts = apply_route_offsets(r, offsets)
        src, tgt, line_id = r.edge.source, r.edge.target, r.line_id
        for k in range(len(pts) - 1):
            p1, p2 = pts[k], pts[k + 1]
            for sid, bbox in index.query_x_range(min(p1[0], p2[0]), max(p1[0], p2[0])):
                if line_id in station_lines_cache[sid]:
                    continue
                if src == sid or tgt == sid:
                    continue
                if segment_intersects_bbox(p1[0], p1[1], p2[0], p2[1], bbox):
                    # The pre-bypass passes defer this guard so the geometric
                    # bypass pass can fix the crossing; the settled geometry is
                    # validated once the bypass cycle completes.
                    if graph._defer_final_guards:
                        return
                    raise PhaseInvariantError(
                        f"{phase}: line {line_id!r} on edge "
                        f"{src!r} -> {tgt!r} "
                        f"crosses non-consumer station {sid!r} "
                        f"marker bbox ({bbox[0]:.1f},{bbox[1]:.1f})-"
                        f"({bbox[2]:.1f},{bbox[3]:.1f}); segment "
                        f"({p1[0]:.1f},{p1[1]:.1f})->"
                        f"({p2[0]:.1f},{p2[1]:.1f})"
                    )


def _guard_no_line_crosses_file_icon(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """Final-phase: no rendered line segment may pass through a file /
    terminus icon's drawn bbox, except the one segment that legitimately
    terminates at (or originates from) that icon's station.

    A metro line raking across a file icon reads as the route running
    through the artefact.  Only the segment that arrives at (or leaves
    from) the icon's own station is exempt -- that line is meant to touch
    it.  Every other segment crossing the icon box is a violation, even
    one belonging to a line the icon's station also carries, since a
    different edge of that line is still raking the artefact.
    """
    from nf_metro.layout.routing.common import apply_route_offsets
    from nf_metro.render.svg import _icon_obstacles_by_station
    from nf_metro.themes import THEMES

    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)
    if routes is None:
        from nf_metro.layout.routing import route_edges

        try:
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:  # noqa: BLE001 - routing failure surfaces elsewhere
            return

    icon_boxes = _icon_obstacles_by_station(graph, THEMES["nfcore"], offsets)
    if not icon_boxes:
        return
    index = BBoxXIndex(list(icon_boxes.items()))

    for r in routes:
        pts = apply_route_offsets(r, offsets)
        src, tgt, line_id = r.edge.source, r.edge.target, r.line_id
        for k in range(len(pts) - 1):
            p1, p2 = pts[k], pts[k + 1]
            for sid, bbox in index.query_x_range(min(p1[0], p2[0]), max(p1[0], p2[0])):
                if src == sid or tgt == sid:
                    continue
                if segment_intersects_bbox(p1[0], p1[1], p2[0], p2[1], bbox):
                    # The pre-bypass passes defer this guard so the geometric
                    # bypass pass can bow the crossing line clear; the settled
                    # geometry is validated once the bypass cycle completes.
                    if graph._defer_final_guards:
                        return
                    raise PhaseInvariantError(
                        f"{phase}: line {line_id!r} on edge {src!r} -> {tgt!r} "
                        f"crosses file icon of {sid!r} "
                        f"bbox ({bbox[0]:.1f},{bbox[1]:.1f})-"
                        f"({bbox[2]:.1f},{bbox[3]:.1f}); segment "
                        f"({p1[0]:.1f},{p1[1]:.1f})->({p2[0]:.1f},{p2[1]:.1f})"
                    )


class OpposingOverlap(NamedTuple):
    """Two segments of one line covering a stretch in opposing directions."""

    line_id: str
    axis: str  # "V" (shared X) or "H" (shared Y)
    coord: float  # the shared constant-axis value
    lo: float  # overlap start along the variable axis
    hi: float  # overlap end along the variable axis
    src_a: str
    tgt_a: str
    src_b: str
    tgt_b: str


class _AxisLeg(NamedTuple):
    """One axis-aligned leg of a rendered path, with its travel direction."""

    axis: str  # "V" (shared X) or "H" (shared Y)
    coord: float  # the constant-axis value
    lo: float  # span start along the variable axis
    hi: float  # span end along the variable axis
    direction: int  # +1 advancing in the increasing-coordinate sense, else -1
    src: str
    tgt: str


def _line_axis_segments(
    pts: list[tuple[float, float]],
    src: str,
    tgt: str,
) -> Iterator[_AxisLeg]:
    """Yield the axis-aligned legs of a rendered path.

    Diagonal legs carry no single constant axis and are skipped.
    """
    for k in range(len(pts) - 1):
        (x1, y1), (x2, y2) = pts[k], pts[k + 1]
        if abs(x1 - x2) <= COLLINEAR_AXIS_TOL and abs(y1 - y2) > GUARD_TOLERANCE:
            yield _AxisLeg(
                "V",
                (x1 + x2) / 2,
                min(y1, y2),
                max(y1, y2),
                1 if y2 > y1 else -1,
                src,
                tgt,
            )
        elif abs(y1 - y2) <= COLLINEAR_AXIS_TOL and abs(x1 - x2) > GUARD_TOLERANCE:
            yield _AxisLeg(
                "H",
                (y1 + y2) / 2,
                min(x1, x2),
                max(x1, x2),
                1 if x2 > x1 else -1,
                src,
                tgt,
            )


def iter_opposing_line_overlaps(
    graph: MetroGraph,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> Iterator[OpposingOverlap]:
    """Yield every stretch a single line covers twice in opposing directions.

    Two axis-aligned legs of the *same* line that share a constant axis (same
    X for vertical legs, same Y for horizontal) and overlap along the other
    axis while pointing in opposite senses draw the line back over itself --
    a fold-back.  Spatially the line reads as running one way then doubling
    straight back, so a station caught in the overlap is visited out of flow
    order (#885).  Legs are compared only within one ``line_id``: distinct
    lines sharing a channel are carried on their own offset slots, not on the
    same track.
    """
    from nf_metro.layout.routing.common import apply_route_offsets

    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)
    if routes is None:
        from nf_metro.layout.routing import route_edges

        try:
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:  # noqa: BLE001 - routing failure surfaces elsewhere
            return

    by_line: dict[str, list[_AxisLeg]] = defaultdict(list)
    for r in routes:
        pts = apply_route_offsets(r, offsets)
        for leg in _line_axis_segments(pts, r.edge.source, r.edge.target):
            by_line[r.line_id].append(leg)

    for line_id, legs in by_line.items():
        for i, a in enumerate(legs):
            for b in legs[i + 1 :]:
                if a.axis != b.axis or abs(a.coord - b.coord) > COLLINEAR_AXIS_TOL:
                    continue
                if a.direction * b.direction >= 0:
                    continue
                if min(a.hi, b.hi) - max(a.lo, b.lo) > GUARD_TOLERANCE:
                    yield OpposingOverlap(
                        line_id,
                        a.axis,
                        (a.coord + b.coord) / 2,
                        max(a.lo, b.lo),
                        min(a.hi, b.hi),
                        a.src,
                        a.tgt,
                        b.src,
                        b.tgt,
                    )


def _guard_no_opposing_line_overlap(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """After routing: no location may be covered by one line travelling in
    opposing directions.

    A line that runs out along a track and straight back over the same track
    folds on itself; any station in the overlap is read out of flow order
    (#885).  This is the general safety net complementing the placement fix
    that keeps a flow-axis entry/exit port on its consumer's side.
    """
    for ov in iter_opposing_line_overlaps(graph, offsets=offsets, routes=routes):
        axis = "x" if ov.axis == "V" else "y"
        raise PhaseInvariantError(
            f"{phase}: line {ov.line_id!r} covers {axis}={ov.coord:.1f} "
            f"over [{ov.lo:.1f},{ov.hi:.1f}] in opposing directions "
            f"(legs {ov.src_a!r}->{ov.tgt_a!r} and {ov.src_b!r}->{ov.tgt_b!r}); "
            f"the line folds back over its own track"
        )


class LabelStrike(NamedTuple):
    """One rendered line segment striking through a station's label glyph ink."""

    line_id: str
    src: str
    tgt: str
    station_id: str
    bbox: tuple[float, float, float, float]
    p1: tuple[float, float]
    p2: tuple[float, float]


def iter_line_label_strikes(
    graph: MetroGraph,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> Iterator[LabelStrike]:
    """Yield every rendered line segment that strikes through a station label.

    A line dipping, fanning, or running across a station's name label reads as
    a strike-through.  The label glyph-ink box (the reserved label width
    tightened to where the text is actually inked, see
    ``label_glyph_ink_bbox``) is used rather than the full reserved box, so a
    line clipping only the empty reserved margin does not trip this -- only one
    crossing the glyphs does.  A segment is exempt when it belongs to a line
    the label's station carries, or when the label's station is an endpoint of
    the segment's edge (that line legitimately touches the station).

    This is the single strike definition shared by
    ``_guard_no_line_strikes_label`` (which raises on the first) and the passive
    label-strike CI metric (which counts them all).
    """
    from nf_metro.layout.labels import (
        LabelPlacement,
        label_glyph_ink_bbox,
        place_labels,
        segment_strikes_label,
    )
    from nf_metro.layout.routing.common import apply_route_offsets
    from nf_metro.render.svg import _compute_icon_obstacles
    from nf_metro.themes import THEMES

    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)

    with _restoring_layout_geometry(graph):
        if routes is None:
            from nf_metro.layout.routing import route_edges_centred

            try:
                routes = route_edges_centred(graph, station_offsets=offsets)
            except Exception:  # noqa: BLE001 - routing failure surfaces elsewhere
                return
        placements = place_labels(
            graph,
            station_offsets=offsets,
            icon_obstacles=_compute_icon_obstacles(graph, THEMES["nfcore"], offsets),
            routes=routes,
            label_angle=graph.label_angle or 0.0,
        )
        boxes: list[tuple[str, tuple[float, float, float, float]]] = []
        placement_by_sid: dict[str, LabelPlacement] = {}
        for p in placements:
            station = graph.stations.get(p.station_id)
            if station is None or not station.label.strip():
                continue
            boxes.append((p.station_id, label_glyph_ink_bbox(p)))
            placement_by_sid[p.station_id] = p
        if not boxes:
            return
        index = BBoxXIndex(boxes)
        station_lines_cache: dict[str, set[str]] = {}

        for r in routes:
            pts = apply_route_offsets(r, offsets)
            src, tgt, line_id = r.edge.source, r.edge.target, r.line_id
            for k in range(len(pts) - 1):
                p1, p2 = pts[k], pts[k + 1]
                lo, hi = min(p1[0], p2[0]), max(p1[0], p2[0])
                for sid, bbox in index.query_x_range(lo, hi):
                    if src == sid or tgt == sid:
                        continue
                    lines = station_lines_cache.get(sid)
                    if lines is None:
                        lines = set(graph.station_lines(sid))
                        station_lines_cache[sid] = lines
                    if line_id in lines:
                        continue
                    if segment_strikes_label(
                        p1[0], p1[1], p2[0], p2[1], placement_by_sid[sid]
                    ):
                        yield LabelStrike(line_id, src, tgt, sid, bbox, p1, p2)


def _guard_no_line_strikes_label(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """Final-phase: no rendered line segment may strike through a station label."""
    for s in iter_line_label_strikes(graph, offsets=offsets, routes=routes):
        raise PhaseInvariantError(
            f"{phase}: line {s.line_id!r} on edge {s.src!r} -> {s.tgt!r} "
            f"strikes through label of {s.station_id!r} "
            f"glyph-ink bbox ({s.bbox[0]:.1f},{s.bbox[1]:.1f})-"
            f"({s.bbox[2]:.1f},{s.bbox[3]:.1f}); segment "
            f"({s.p1[0]:.1f},{s.p1[1]:.1f})->({s.p2[0]:.1f},{s.p2[1]:.1f})"
        )


def _guard_bypass_v_flat_visible(graph: MetroGraph, phase: str) -> None:
    """Final-phase: every bypass V keeps a visible run through its marker.

    A bypass V whose diverging run pins to the station marker rakes that
    station's label; one whose run collapses sits at the curve apex instead of
    on a flat like a regular station.  For a horizontal (LR/RL) bypass the
    strike-clearance loop pushes the bypassed node (or the merge target) a grid
    column out until both runs reach ``MIN_STATION_FLAT_LENGTH``; for a
    vertical-flow (TB/BT) bypass the section's exit-corridor gap owns the run-out
    flat.  This is the backstop for a residual neither resolved.
    """
    from nf_metro.layout.phases.spacing import _bypass_v_collapsed_flat_gaps

    collapsed = _bypass_v_collapsed_flat_gaps(graph)
    if collapsed:
        detail = ", ".join(
            f"section {sid!r} layer {layer}" for sid, layer in sorted(collapsed)
        )
        raise PhaseInvariantError(
            f"{phase}: bypass-V flat run collapsed below the minimum visible "
            f"length at {detail}"
        )


def _guard_no_diagonal_strikes_horizontal_label(
    graph: MetroGraph,
    phase: str,
) -> None:
    """Final-phase: no foreign fan diagonal rakes a stacked station's name.

    Protects the strike-clearance loop's result: a fan-in/fan-out or
    convergence diagonal that transitions through a horizontal label reads as a
    strike-through, and the loop grows the offending section's runway by whole
    grid columns until the transition seats clear.  This guard fails loudly if a
    layout ships such a strike anyway.  It probes through the same helper the
    loop uses, so it validates exactly the geometry the loop reasoned about.

    Narrower than :func:`_guard_no_line_strikes_label`: it excludes bypass-V
    crossings (cleared by the router seating the V's flat-run corners off the
    label, not by runway growth) and angled labels (handled by their rotated
    footprint).  It attributes a fan or convergence strike to the runway loop
    that owns it.
    """
    from nf_metro.layout.phases.spacing import (
        _probe_label_placements,
        _struck_label_station_ids,
    )

    probe = _probe_label_placements(graph, allow_hyphenation=True)
    if probe is None:
        return
    offsets, routes, placements = probe
    struck = _struck_label_station_ids(graph, offsets, routes, placements)
    if struck:
        names = ", ".join(
            f"{sid!r} ({graph.stations[sid].label!r})" for sid in sorted(struck)
        )
        raise PhaseInvariantError(
            f"{phase}: foreign fan diagonal strikes horizontal label(s): {names}"
        )


def _guard_no_wrapped_label_trunk_strike(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """Final-phase: no wrapped label's ink overruns a foreign horizontal trunk.

    A label that wraps stacks its extra lines toward a neighbouring track, so a
    label a collision push-out drove toward that track can grow across a metro
    line its station does not carry, drawing the name through the line.  The
    render-time lift in ``place_labels`` pulls such a label back to its
    un-pushed anchor; this asserts the settled render leaves none striking.

    Narrower than :func:`_guard_no_line_strikes_label`: it covers only the
    horizontal-trunk overrun the lift resolves, reported against the unlifted
    geometry the render-time lift then clears.
    """
    from nf_metro.layout.labels import (
        find_wrapped_label_trunk_strikes,
        place_labels,
    )
    from nf_metro.render.svg import _compute_icon_obstacles
    from nf_metro.themes import THEMES

    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)

    with _restoring_layout_geometry(graph):
        if routes is None:
            from nf_metro.layout.routing import route_edges_centred

            try:
                routes = route_edges_centred(graph, station_offsets=offsets)
            except Exception:  # noqa: BLE001 - routing failure surfaces elsewhere
                return
        placements = place_labels(
            graph,
            station_offsets=offsets,
            icon_obstacles=_compute_icon_obstacles(graph, THEMES["nfcore"], offsets),
            routes=routes,
            label_angle=graph.label_angle or 0.0,
        )
        strikes = find_wrapped_label_trunk_strikes(graph, placements, routes, offsets)
        if strikes:
            sid, y, line_id = strikes[0]
            raise PhaseInvariantError(
                f"{phase}: wrapped label of {sid!r} overruns foreign trunk "
                f"{line_id!r} at y={y:.1f}; {len(strikes)} strike(s) total"
            )


def _guard_off_track_output_clears_non_producer(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """Final-phase: an off-track output's route crosses only its producer.

    An off-track output icon hangs in the trunk gap just past its producer.
    When the output's horizontal extent overruns that gap, its up-right
    diagonal rakes across the next on-track station's marker.  Because every
    trunk station carries the same line, :func:`_guard_no_line_crosses_non_consumer`
    exempts that crossing (the marker's station consumes the line); this guard
    closes the gap by checking the output route against same-section trunk
    markers regardless of line membership, exempting only the producer.
    """
    from nf_metro.layout.routing.common import apply_route_offsets

    producer_of = {
        off_id: anchor_id
        for off_id, anchor_id in _off_track_anchor_of(graph).items()
        if not any(e.target == anchor_id for e in graph.edges_from(off_id))
    }
    if not producer_of:
        return

    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)
    if routes is None:
        from nf_metro.layout.routing import route_edges

        try:
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:  # noqa: BLE001 - routing failure surfaces elsewhere
            return

    junction_ids = graph.junction_ids
    route_by_endpoints = {(r.edge.source, r.edge.target): r for r in routes}
    for off_id, prod_id in producer_of.items():
        route = route_by_endpoints.get((prod_id, off_id))
        if route is None:
            continue
        pts = apply_route_offsets(route, offsets)
        sec_id = graph.stations[off_id].section_id
        section = graph.sections.get(sec_id) if sec_id else None
        if section is None:
            continue
        for sid in section.station_ids:
            if sid in (off_id, prod_id) or sid in junction_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.off_track or st.is_port or st.is_hidden:
                continue
            bbox = _station_marker_bbox(graph, sid, offsets=offsets)
            if bbox is None:
                continue
            for k in range(len(pts) - 1):
                p1, p2 = pts[k], pts[k + 1]
                if segment_intersects_bbox(p1[0], p1[1], p2[0], p2[1], bbox):
                    raise PhaseInvariantError(
                        f"{phase}: off-track output {off_id!r} route from "
                        f"producer {prod_id!r} crosses non-producer marker "
                        f"{sid!r} bbox ({bbox[0]:.1f},{bbox[1]:.1f})-"
                        f"({bbox[2]:.1f},{bbox[3]:.1f}); segment "
                        f"({p1[0]:.1f},{p1[1]:.1f})->({p2[0]:.1f},{p2[1]:.1f})"
                    )


def _guard_row_trunk_cy_consistent(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
) -> None:
    """Final-phase: same-row LR sections that share the same line bundle
    AND whose trunk Y-ranges overlap must render their trunk marker at
    the same cy within ``GUARD_TOLERANCE``.

    The bundle-overlap filter means same-row sections carrying disjoint
    line sets (e.g. parallel sub-rows on a row-spanner) don't trigger.
    """
    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)

    rows: dict[int, list[Section]] = {}
    for sec in graph.sections.values():
        if (
            sec.bbox_h <= 0
            or sec.grid_row < 0
            or not lanes_run_along_y(sec.direction)
            or sec.grid_row_span > 1
        ):
            continue
        rows.setdefault(sec.grid_row, []).append(sec)

    for row, sections in rows.items():
        _check_row_trunk_cy(graph, phase, row, sections, offsets)


def _section_trunk_cy_band(
    graph: MetroGraph,
    sec: Section,
    offsets: dict[tuple[str, str], float],
) -> tuple[float, float, float, set[str]] | None:
    """Trunk ``(cy, y_min, y_max, bundle)`` of the bundle-carrying station
    nearest the LR/RL port Y, or None when the section has no such trunk."""
    bundle = _section_bundle_lines(graph, sec)
    if not bundle:
        return None
    port_ys: list[float] = []
    for pid in list(sec.entry_ports) + list(sec.exit_ports):
        pst = graph.stations.get(pid)
        pport = graph.ports.get(pid)
        if (
            pst is not None
            and pport is not None
            and pport.side in (PortSide.LEFT, PortSide.RIGHT)
        ):
            port_ys.append(pst.y)
    if not port_ys:
        return None
    port_y = port_ys[0]
    port_set = sec.port_ids
    best: tuple[float, float, float, float] | None = None
    for sid in sec.station_ids:
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
        y_min = st.y + min(line_offs)
        y_max = st.y + max(line_offs)
        cy = st.y + (min(line_offs) + max(line_offs)) / 2
        dist = abs(cy - port_y)
        if best is None or dist < best[0]:
            best = (dist, cy, y_min, y_max)
    if best is None:
        return None
    return (best[1], best[2], best[3], bundle)


def _check_row_trunk_cy(
    graph: MetroGraph,
    phase: str,
    row: int,
    sections: list[Section],
    offsets: dict[tuple[str, str], float],
) -> None:
    """Raise if same-bundle, band-overlapping sections in one row drift in cy."""
    info: dict[str, tuple[float, float, float, set[str]]] = {}
    for sec in sections:
        t = _section_trunk_cy_band(graph, sec, offsets)
        if t is not None:
            info[sec.id] = t
    if len(info) < 2:
        return
    parent = {sid: sid for sid in info}

    def _find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    ids = list(info)
    for i, a in enumerate(ids):
        _cy_a, lo_a, hi_a, bun_a = info[a]
        for b in ids[i + 1 :]:
            _cy_b, lo_b, hi_b, bun_b = info[b]
            bands_overlap = min(hi_a, hi_b) - max(lo_a, lo_b) >= -GUARD_TOLERANCE
            if bands_overlap and bun_a == bun_b:
                ra, rb = _find(a), _find(b)
                if ra != rb:
                    parent[ra] = rb

    groups: dict[str, list[str]] = {}
    for sid in ids:
        groups.setdefault(_find(sid), []).append(sid)

    for members in groups.values():
        if len(members) < 2:
            continue
        anchor = members[0]
        anchor_cy = info[anchor][0]
        for sid in members[1:]:
            cy = info[sid][0]
            if abs(cy - anchor_cy) > GUARD_TOLERANCE:
                raise PhaseInvariantError(
                    f"{phase}: row {row} trunk cy drift: "
                    f"section {sid!r} cy={cy:.1f} vs "
                    f"section {anchor!r} cy={anchor_cy:.1f}"
                )


def _guard_inter_section_routes_in_row_band(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """After routing: inter-section routes whose endpoints both sit in
    grid row R must keep all waypoint Ys within a one-row band centered
    on R, plus ``ROW_BAND_SLACK`` for a clean below-row wrap channel
    (bypass clearance + bundle nest + diagonal corner approach).
    """
    if offsets is None or routes is None:
        from nf_metro.layout.routing import compute_station_offsets, route_edges

        if offsets is None:
            offsets = compute_station_offsets(graph)
        if routes is None:
            routes = route_edges(graph, station_offsets=offsets)

    row_band: dict[int, tuple[float, float]] = {}
    for sec in graph.sections.values():
        if sec.bbox_h <= 0 or sec.grid_row_span != 1:
            continue
        cur = row_band.get(sec.grid_row)
        top = sec.bbox_y
        bot = sec.bbox_y + sec.bbox_h
        if cur is None:
            row_band[sec.grid_row] = (top, bot)
        else:
            row_band[sec.grid_row] = (min(cur[0], top), max(cur[1], bot))

    slack = ROW_BAND_SLACK
    for r in routes:
        src = graph.stations.get(r.edge.source)
        tgt = graph.stations.get(r.edge.target)
        if src is None or tgt is None:
            continue
        if src.section_id is None or tgt.section_id is None:
            continue
        if src.section_id == tgt.section_id:
            continue
        sec_a = graph.sections.get(src.section_id)
        sec_b = graph.sections.get(tgt.section_id)
        if sec_a is None or sec_b is None:
            continue
        if sec_a.grid_row != sec_b.grid_row:
            continue
        if sec_a.grid_row_span != 1 or sec_b.grid_row_span != 1:
            continue
        band = row_band.get(sec_a.grid_row)
        if band is None:
            continue
        lo, hi = band[0] - slack, band[1] + slack
        for _x, y in r.points:
            if y < lo or y > hi:
                raise PhaseInvariantError(
                    f"{phase}: route {r.edge.source!r}->{r.edge.target!r} "
                    f"line {r.line_id!r} waypoint y={y:.1f} outside "
                    f"row-{sec_a.grid_row} band [{lo:.1f}..{hi:.1f}]"
                )


def _guard_topmost_row_top_entry_hugs_section(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """After routing: a same-row inter-section route into a section in the
    topmost grid row must hug that section's top edge, not climb into the
    canvas-top band.

    A TOP port fed by a same-row producer routes up-and-over into the port.
    The topmost row has no inter-row gap above it -- only the title, drawn
    in the canvas-top padding -- so the over-the-top channel must sit a
    route's-width above the section edge. A deeper climb drives the line
    through the title text.
    """
    from nf_metro.layout.routing.common import (
        row_top_edge,
        section_exists_above_row,
    )

    routes = _ensure_routes(graph, routes)

    for r in routes:
        src = graph.stations.get(r.edge.source)
        tgt = graph.stations.get(r.edge.target)
        if src is None or tgt is None:
            continue
        if src.section_id is None or tgt.section_id is None:
            continue
        if src.section_id == tgt.section_id:
            continue
        sec_a = graph.sections.get(src.section_id)
        sec_b = graph.sections.get(tgt.section_id)
        if sec_a is None or sec_b is None:
            continue
        if sec_a.grid_row != sec_b.grid_row:
            continue
        if sec_a.grid_row_span != 1 or sec_b.grid_row_span != 1:
            continue
        if section_exists_above_row(graph, sec_b.grid_row):
            continue
        tgt_port = graph.ports.get(r.edge.target)
        if tgt_port is not None and tgt_port.side == PortSide.RIGHT:
            # An over-the-top loop into a RIGHT entry port must climb above the
            # section's header badge to approach from the right; the row was
            # pushed down (_reserve_over_top_headroom) to keep that climb below
            # the title, so the hug limit does not apply.
            continue
        band_top = row_top_edge(graph, sec_b.grid_row, default=sec_b.bbox_y)
        limit = band_top - (INTER_ROW_EDGE_CLEARANCE + CURVE_RADIUS) - GUARD_TOLERANCE
        min_y = min(y for _x, y in r.points)
        if min_y < limit:
            raise PhaseInvariantError(
                f"{phase}: topmost-row route {r.edge.source!r}->{r.edge.target!r} "
                f"line {r.line_id!r} climbs to y={min_y:.1f}, above the "
                f"section-edge clearance limit {limit:.1f} (band top "
                f"{band_top:.1f}); the over-the-top channel would cross the "
                f"canvas-top title band"
            )


def _guard_title_band_clearance(
    graph: MetroGraph, phase: str, *, section_y_padding: float
) -> None:
    """A titled map's topmost drawn section must not overlap the title band.

    The title is drawn in the canvas-top padding at a fixed baseline; the
    section header badge protrudes ``SECTION_HEADER_PROTRUSION`` above its box
    top.  A drawn box top above ``TITLE_BAND_OVERLAP_FLOOR`` sits its badge
    level with the title.  Untitled maps, and implicit holders (which draw no
    badge), are exempt.
    """
    if not graph.title:
        return
    min_top = _min_drawn_section_bbox_top(graph)
    if min_top is None:
        return
    limit = max(section_y_padding, TITLE_BAND_OVERLAP_FLOOR) - GUARD_TOLERANCE
    if min_top < limit:
        raise PhaseInvariantError(
            f"{phase}: titled map's topmost section box top y={min_top:.1f} "
            f"sits above the title-band floor {limit:.1f}; the header badge "
            f"would rise level with the map title"
        )


def _ensure_routes(
    graph: MetroGraph, routes: list[RoutedPath] | None
) -> list[RoutedPath]:
    """Return *routes*, routing all edges first if the caller didn't supply them.

    Routes *with* ``station_offsets`` so the guards dispatch the same handler
    the render does: several dispatch predicates gate on
    ``bool(ctx.station_offsets)`` (e.g. ``_InterFacts.is_tb_bottom_exit``), so
    routing bare would fire a different handler than the render and make a
    ``validate=True`` verdict disagree with the drawn picture (#1319).  The
    offset *magnitude* need not match the render's themed ``offset_step``; only
    its presence gates dispatch.
    """
    if routes is not None:
        return routes
    from nf_metro.layout.routing import compute_station_offsets, route_edges

    return route_edges(graph, station_offsets=compute_station_offsets(graph))


def _route_exit_side(graph: MetroGraph, rp: RoutedPath) -> PortSide | None:
    """Side of the port a route exits through (directly or via its feeder)."""
    port = graph.ports.get(rp.edge.source)
    if port is not None:
        return port.side
    for e in graph.edges_to(rp.edge.source):
        port = graph.ports.get(e.source)
        if port is not None:
            return port.side
    return None


def _inter_section_backtrack_legs(
    graph: MetroGraph,
    routes: list[RoutedPath],
    *,
    reference: str = "grid",
    tolerance: float = 0.0,
    include_exempt: bool = False,
) -> Iterator[tuple[RoutedPath, float, float]]:
    """Yield ``(rp, x1, x2)`` for each horizontal leg of a forward LR
    inter-section route that reverses against its flow.

    *reference* selects how "forward" is defined:

    * ``"grid"`` - flow points toward the target grid column (strict;
      used by the monotonic guard, which assumes grid order matches X).
    * ``"endpoint"`` - flow points toward the route's own endpoint X
      (tolerant of a nested-column approach where the target column sits
      left of its source; used by the full-width dog-leg guard).

    *tolerance* widens the reversal threshold; *include_exempt* keeps
    ``normalize_exempt`` wrap routes (needed to measure around-section
    dog-legs).  Routes exiting a port that faces away from their target
    column legitimately wrap and are skipped, as are TB folds and
    same-column routes.
    """
    from nf_metro.layout.routing.common import resolve_section

    for rp in routes:
        if not rp.is_inter_section:
            continue
        if rp.normalize_exempt and not include_exempt:
            continue
        src_sec = resolve_section(graph, graph.stations[rp.edge.source])
        tgt_sec = resolve_section(graph, graph.stations[rp.edge.target])
        if src_sec is None or tgt_sec is None:
            continue
        if src_sec.direction != "LR" or tgt_sec.direction != "LR":
            continue
        if src_sec.grid_col == tgt_sec.grid_col:
            continue
        xs = [p[0] for p in rp.points]
        if len(xs) < 2:
            continue
        rightward_cols = tgt_sec.grid_col > src_sec.grid_col
        side = _route_exit_side(graph, rp)
        if rightward_cols and side != PortSide.RIGHT:
            continue
        if not rightward_cols and side != PortSide.LEFT:
            continue
        forward_is_right = rightward_cols if reference == "grid" else xs[-1] > xs[0]
        for x1, x2 in zip(xs, xs[1:]):
            backtracks = (
                (x2 < x1 - tolerance) if forward_is_right else (x2 > x1 + tolerance)
            )
            if backtracks:
                yield rp, x1, x2


def _guard_inter_section_route_no_backtrack(
    graph: MetroGraph,
    phase: str,
    *,
    routes: list[RoutedPath] | None = None,
) -> None:
    """After routing: a forward-flowing inter-section route between two LR
    columns must be X-monotonic.

    A route that exits a port toward its target column (rightward exit, target
    to the right) must not contain a horizontal segment that reverses; such a
    backtrack renders as a turn-back toward the section just behind the exit
    (#386).  Routes that exit AWAY from their target legitimately wrap and are
    skipped, as are ``normalize_exempt`` wrap legs, TB folds, and same-column
    routes.
    """
    from nf_metro.layout.routing.common import resolve_section

    routes = _ensure_routes(graph, routes)

    for rp, x1, x2 in _inter_section_backtrack_legs(
        graph, routes, reference="grid", tolerance=GUARD_TOLERANCE
    ):
        src_sec = resolve_section(graph, graph.stations[rp.edge.source])
        tgt_sec = resolve_section(graph, graph.stations[rp.edge.target])
        rightward = (
            src_sec is not None
            and tgt_sec is not None
            and tgt_sec.grid_col > src_sec.grid_col
        )
        raise PhaseInvariantError(
            f"{phase}: route {rp.edge.source!r}->{rp.edge.target!r} "
            f"line {rp.line_id!r} backtracks x={x1:.1f}->{x2:.1f} "
            f"against its {'rightward' if rightward else 'leftward'} flow"
        )


def _guard_fan_bundles_coincide_or_separate(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """After routing: two routes carrying the SAME line out of a unified-fan
    junction must coincide on their source-side vertical channel or separate
    clearly - never smear a few px apart.

    A unified-fan junction (one the router assigns shared
    ``junction_fan_info`` positions) fans the same line to multiple targets
    that are MEANT to pivot through one channel.  When two such routes' first
    vertical legs sit between ``OFFSET_STEP`` (the legitimate per-bundle
    stagger) and ``SECTION_Y_GAP`` (a clean column split) apart, they render
    as a smeared partial overlap rather than one bundle or two separated
    bundles.
    """
    from nf_metro.layout.routing import compute_station_offsets, route_edges
    from nf_metro.layout.routing.core import compute_junction_fan_info

    if offsets is None:
        offsets = compute_station_offsets(graph)
    if routes is None:
        routes = route_edges(graph, station_offsets=offsets)
    fan_sources = {key[0] for key in compute_junction_fan_info(graph)}
    if not fan_sources:
        return

    # Track each route's V1 X together with its vertical direction: two
    # routes whose source-side channels head OPPOSITE ways (one up, one
    # down) diverge at the junction and cannot smear, however close their
    # X channels sit, so they are not paired.
    by_src_line: dict[tuple[str, str], list[tuple[float, int]]] = {}
    for rp in routes:
        if not rp.is_inter_section or rp.edge.source not in fan_sources:
            continue
        vx = first_vertical_leg_x(rp.points)
        sign = first_vertical_leg_sign(rp.points)
        if vx is None or sign is None:
            continue
        by_src_line.setdefault((rp.edge.source, rp.line_id), []).append((vx, sign))

    # Coincide within the per-bundle stagger plus a 1px rounding epsilon;
    # GUARD_TOLERANCE (5px) would swallow the 6px smear this guards against.
    coincide_tol = OFFSET_STEP + 1.0
    for (src, line), entries in by_src_line.items():
        if len(entries) < 2:
            continue
        ordered = sorted(entries)
        for (lo, lo_sign), (hi, hi_sign) in zip(ordered, ordered[1:]):
            if lo_sign != hi_sign:
                continue
            gap = hi - lo
            if coincide_tol < gap < SECTION_Y_GAP:
                raise PhaseInvariantError(
                    f"{phase}: junction {src!r} line {line!r} fans two routes "
                    f"whose first vertical channels are {gap:.1f}px apart "
                    f"(x={lo:.1f} vs {hi:.1f}) - neither coincident "
                    f"(<= {coincide_tol:.1f}) nor clearly separated "
                    f"(>= {SECTION_Y_GAP:.1f}); a smeared partial overlap"
                )


def inter_section_route_backtrack_legs(
    graph: MetroGraph, routes: list[RoutedPath]
) -> Iterator[tuple[RoutedPath, float, float]]:
    """Yield ``(rp, x1, x2)`` for each horizontal leg that moves *away* from
    the route's own endpoint X - a genuine out-and-back dog-leg.

    A backtrack is reverse-direction travel: the line heads away from where
    it is going, then has to come back.  This is measured against the
    route's actual endpoint X (its last waypoint), not the grid-column
    order.  Grid columns can disagree with X order when a narrow target
    column nests inside a wide row-span sibling: there the target column is
    "higher" yet sits to the *left*, so a single long leftward traverse is a
    monotonic approach toward the target - not a dog-leg - and is not
    yielded.  A true dog-leg (right past the target, then back left)
    still moves away from the endpoint on its outward leg and is yielded.

    Routes that exit a port facing away from their endpoint legitimately
    wrap and are skipped, as are TB folds and same-column routes.  Unlike
    the strict :func:`_guard_inter_section_route_no_backtrack`, exempt
    (``normalize_exempt``) wrap routes are *included* so a multi-corner
    around-section dog-leg is still measured.
    """
    yield from _inter_section_backtrack_legs(
        graph, routes, reference="endpoint", tolerance=0.0, include_exempt=True
    )


def _guard_inter_section_route_no_full_width_backtrack(
    graph: MetroGraph,
    phase: str,
    *,
    routes: list[RoutedPath] | None = None,
    fraction: float = 0.4,
) -> None:
    """After routing: a forward inter-section route may reverse in X (when a
    narrow target column nests inside an oversized sibling) but no single
    backtrack leg may exceed *fraction* of the canvas width.

    The strict :func:`_guard_inter_section_route_no_backtrack` forbids *any*
    reversal on a forward LR route, assuming grid-column order matches X
    order.  When a column is geometrically nested inside an oversized
    sibling, reaching it requires a legitimate X
    reversal, so such routes are made ``normalize_exempt`` and the strict
    guard skips them.  This guard still bounds those reversals: a
    right-then-left dog-leg sweeping the whole diagram is forbidden
    even when exempt.
    """
    routes = _ensure_routes(graph, routes)

    canvas_width = _canvas_width(graph)
    if canvas_width <= 0:
        return
    limit = fraction * canvas_width

    for rp, x1, x2 in inter_section_route_backtrack_legs(graph, routes):
        span = abs(x2 - x1)
        if span > limit + GUARD_TOLERANCE:
            raise PhaseInvariantError(
                f"{phase}: route {rp.edge.source!r}->{rp.edge.target!r} "
                f"line {rp.line_id!r} backtracks {span:.1f}px in one leg "
                f"(x={x1:.1f}->{x2:.1f}), exceeding {fraction:.0%} of canvas "
                f"width {canvas_width:.1f} - a full-width dog-leg"
            )


def _guard_rail_connector_ports_no_stub(
    graph: MetroGraph,
    phase: str,
    *,
    routes: list[RoutedPath] | None = None,
) -> None:
    """After routing: a whole-graph rail-mode inter-section connector must
    leave its exit port outward and reach its entry port from outside.

    Rail mode stacks sections vertically; the dedicated rail router connects a
    RIGHT exit port to a LEFT entry port via a corridor that wraps around the
    outside of both boxes.  A connector that instead heads back *into* the
    section it just left (or reaches the entry port from inside the box) leaves
    a dangling stub and slices the section's own rails (#743).  The outward
    direction is set by the port side: a RIGHT port is left/reached rightward,
    a LEFT port leftward.
    """
    if graph.line_spread is not LineSpread.RAILS:
        return
    routes = _ensure_routes(graph, routes)

    def outward_ok(side: PortSide, anchor_x: float, neighbour_x: float) -> bool:
        if side is PortSide.RIGHT:
            return neighbour_x >= anchor_x - GUARD_TOLERANCE
        if side is PortSide.LEFT:
            return neighbour_x <= anchor_x + GUARD_TOLERANCE
        return True

    for rp in routes:
        src = graph.ports.get(rp.edge.source)
        tgt = graph.ports.get(rp.edge.target)
        if src is None or tgt is None or src.section_id == tgt.section_id:
            continue
        if len(rp.points) < 2:
            continue
        if not outward_ok(src.side, rp.points[0][0], rp.points[1][0]):
            raise PhaseInvariantError(
                f"{phase}: rail connector {rp.edge.source!r}->{rp.edge.target!r} "
                f"line {rp.line_id!r} leaves its {src.side.name} exit port "
                f"x={rp.points[0][0]:.1f} inward toward x={rp.points[1][0]:.1f}"
            )
        if not outward_ok(tgt.side, rp.points[-1][0], rp.points[-2][0]):
            raise PhaseInvariantError(
                f"{phase}: rail connector {rp.edge.source!r}->{rp.edge.target!r} "
                f"line {rp.line_id!r} reaches its {tgt.side.name} entry port "
                f"x={rp.points[-1][0]:.1f} from inside at x={rp.points[-2][0]:.1f}"
            )


def _guard_routes_enter_sections_at_ports(
    graph: MetroGraph,
    phase: str,
    *,
    routes: list[RoutedPath] | None = None,
) -> None:
    """After routing: no routed segment may cross a section bbox boundary
    except within tolerance of a declared port on that section.

    A line that cuts through a section box anywhere other than a port is
    visually entering/leaving the section where nothing invites it (e.g. a
    fan-in merge bundle ploughing into a section through its right edge, or
    an entry inferred on the wrong side so the connector slices the box).
    """
    routes = _ensure_routes(graph, routes)

    hit = _route_crosses_section_boundary(graph, routes)
    if hit is not None:
        rp, sid, bx, by = hit
        raise PhaseInvariantError(
            f"{phase}: route {rp.edge.source!r}->{rp.edge.target!r} "
            f"line {rp.line_id!r} crosses section {sid!r} boundary at "
            f"({bx:.1f}, {by:.1f}) away from any declared port"
        )


def _guard_no_route_through_section(
    graph: MetroGraph,
    phase: str,
    *,
    routes: list[RoutedPath] | None = None,
    offsets: dict[tuple[str, str], float] | None = None,
) -> None:
    """After routing: no routed line may pass through the interior of a
    section box it does not connect to.

    A line may only occupy a section's bbox where it interacts with a
    station there -- i.e. the section holds the route's source (the line
    starts there) or target (it enters via that section's port).  A
    segment crossing any other section's box is plotting the line over a
    section it never touches (issue #484).  Unlike
    ``_guard_routes_enter_sections_at_ports`` this inspects the final
    rendered geometry and every route, including fan-in/-out bundle routes
    through junction/merge nodes.
    """
    offenders = routes_through_unrelated_sections(graph, routes=routes, offsets=offsets)
    if offenders:
        rp, sid = offenders[0]
        raise PhaseInvariantError(
            f"{phase}: line {rp.line_id!r} on route "
            f"{rp.edge.source!r}->{rp.edge.target!r} passes through section "
            f"{sid!r} without interacting with any station there "
            f"({len(offenders)} pass-through(s) total)"
        )


def _guard_inter_section_route_clears_own_section_interior(
    graph: MetroGraph,
    phase: str,
    *,
    routes: list[RoutedPath] | None = None,
    offsets: dict[tuple[str, str], float] | None = None,
) -> None:
    """After routing: an inter-section route must not run back through the
    interior of its own source or target section box.

    A route connects a source section's exit port to a target section's entry
    port, both on the box boundary; reaching between them means travelling the
    inter-section gaps and routing *around* the boxes.  A segment whose
    interior lies inside its own source or target bbox has clawed back through
    the box -- the symptom of an exit side that faces away from the consumer,
    which renders as a silent wrapped/backtracking bundle (#1078, surfaced by
    #1074).  ``_guard_no_route_through_section`` exempts a route's own
    sections; this guard covers exactly that gap.

    This guard makes the wrap *visible* on the render path; choosing an exit
    side that faces the consumer (#1081) and routing around the boxes (#1083)
    is what removes it.
    """
    offenders = routes_through_own_section_interior(
        graph, routes=routes, offsets=offsets
    )
    if offenders:
        rp, sid = offenders[0]
        raise PhaseInvariantError(
            f"{phase}: inter-section route {rp.edge.source!r}->"
            f"{rp.edge.target!r} line {rp.line_id!r} runs back through the "
            f"interior of its own section {sid!r} instead of routing around it "
            f"({len(offenders)} interior crossing(s) total); the exit side "
            f"faces away from the target and the bundle wraps"
        )


def _guard_feeder_exits_section_through_side(
    graph: MetroGraph,
    phase: str,
    *,
    routes: list[RoutedPath] | None = None,
    offsets: dict[tuple[str, str], float] | None = None,
) -> None:
    """After routing: an inter-section feeder must leave its source section
    through a vertical side, never across the box's top or bottom edge.

    A feeder that turns down out of its source section to reach a row below
    must do so *outside* the section's drawn right (or left) edge, then loop
    around -- otherwise its vertical run crosses the box's bottom edge while
    inside the box's x-range and visibly passes through the section frame.

    The source section bbox grows to contain the rightmost station's angled
    label (#527), so the feeder's turn-down X must clear that grown edge.
    Detected by checking whether any route segment crosses the source
    section's horizontal top/bottom edge line strictly inside the box's
    x-range (issue #527, feeder-through-bottom).
    """
    from nf_metro.layout.constants import LABEL_BBOX_MARGIN
    from nf_metro.layout.phases.single_section import (
        angled_label_reach,
        angled_label_right_reach,
    )

    label_angle = graph.label_angle or 0.0

    def _drawn_box(sec: Section) -> tuple[float, float, float, float]:
        """Box edges as the renderer will draw them, including the right/
        bottom growth for the section's angled labels (#527).  Measuring
        against the *grown* box gives the guard teeth even if a future change
        drops the layout-time growth: the feeder must clear the rendered box.
        """
        bx0, by0 = sec.bbox_x, sec.bbox_y
        bx1 = bx0 + sec.bbox_w
        by1 = by0 + sec.bbox_h
        if label_angle and sec.direction in ("LR", "RL"):
            for sid in sec.station_ids:
                st = graph.stations.get(sid)
                if st is None:
                    continue
                right = st.x + angled_label_right_reach(st, label_angle)
                if right + LABEL_BBOX_MARGIN > bx1:
                    bx1 = right + LABEL_BBOX_MARGIN
                bot = st.y + angled_label_reach(st, label_angle)
                if bot + LABEL_BBOX_MARGIN > by1:
                    by1 = bot + LABEL_BBOX_MARGIN
        return bx0, by0, bx1, by1

    routes = _ensure_routes(graph, routes)
    tol = GUARD_TOLERANCE
    for rp in routes:
        src = graph.stations.get(rp.edge.source)
        tgt = graph.stations.get(rp.edge.target)
        if src is None or tgt is None or src.section_id is None:
            continue
        if src.section_id == tgt.section_id:
            continue
        sec = graph.sections.get(src.section_id)
        if sec is None or sec.bbox_w <= 0 or sec.bbox_h <= 0:
            continue
        bx0, by0, bx1, by1 = _drawn_box(sec)
        pts = rp.points
        for i in range(len(pts) - 1):
            (x0, y0), (x1, y1) = pts[i], pts[i + 1]
            for edge_y, name in ((by0, "top"), (by1, "bottom")):
                lo, hi = min(y0, y1), max(y0, y1)
                if not (lo < edge_y - tol and hi > edge_y + tol):
                    continue  # segment does not straddle this edge line
                if abs(y1 - y0) < tol:
                    continue
                t = (edge_y - y0) / (y1 - y0)
                cross_x = x0 + t * (x1 - x0)
                if bx0 + tol < cross_x < bx1 - tol:
                    raise PhaseInvariantError(
                        f"{phase}: feeder {rp.edge.source!r}->{rp.edge.target!r} "
                        f"line {rp.line_id!r} crosses section {src.section_id!r} "
                        f"{name} edge (y={edge_y:.1f}) at x={cross_x:.1f} inside "
                        f"the box x-range [{bx0:.1f}, {bx1:.1f}]; it must exit "
                        f"through a vertical side, clear of the drawn box"
                    )


def _route_landing_entry_port(
    graph: MetroGraph, rp: RoutedPath, tol: float
) -> Port | None:
    """The entry port a route physically lands on, or ``None``.

    A port-targeted edge resolves directly from ``edge.target``.  A merge or
    bypass trunk targets a virtual ``__merge_*`` / ``__junction_*`` node but is
    extended to terminate on the section's entry-port station; that landing is
    found by matching the route's final point to an entry port's coordinates,
    so a trunk crossing its own target box to reach a far-side port is judged
    on the same outward-approach rule as a direct port edge.
    """
    port = graph.ports.get(rp.edge.target)
    if port is not None:
        return port if port.is_entry else None
    # Only an inter-section route (a merge/bypass trunk targeting a virtual
    # node) lands on a far section's entry-port station; intra-section and
    # off-track routes end at internal stations, so they skip the scan.
    pts = rp.points
    if not rp.is_inter_section or len(pts) < 2:
        return None
    ex, ey = pts[-1]
    for p in graph.ports.values():
        if p.is_entry and abs(p.x - ex) <= tol and abs(p.y - ey) <= tol:
            return p
    return None


def _entry_approach_offenders(
    graph: MetroGraph, routes: list[RoutedPath]
) -> list[tuple[RoutedPath, str, str]]:
    """Routes whose final approach to an entry port crosses the target's
    interior from the port's far side.

    The final leg of a route landing on a perpendicular-axis entry port
    (LEFT/RIGHT for the X axis, TOP/BOTTOM for the Y axis) must arrive
    from the port's own OUTWARD side.  A RIGHT entry port must be reached
    from ``x >= port.x`` (outside the box on the right); a route whose
    approach leg starts INSIDE the box and runs outward to the port has
    sliced through the section interior to get there.  Returns
    ``(route, port_id, reason)`` for each offender.
    """
    offenders: list[tuple[RoutedPath, str, str]] = []
    tol = GUARD_TOLERANCE
    for rp in routes:
        port = _route_landing_entry_port(graph, rp, tol)
        if port is None:
            continue
        section = graph.sections.get(port.section_id) if port.section_id else None
        if section is None or section.bbox_w <= 0 or section.bbox_h <= 0:
            continue
        pts = rp.points
        if len(pts) < 2:
            continue
        end = pts[-1]
        prev = pts[-2]
        bx0, by0 = section.bbox_x, section.bbox_y
        bx1, by1 = bx0 + section.bbox_w, by0 + section.bbox_h
        if port.side in (PortSide.LEFT, PortSide.RIGHT):
            # Approach must be horizontal-ish and arrive from outside.
            if abs(prev[1] - end[1]) > tol:
                continue
            if port.side == PortSide.RIGHT and prev[0] < bx1 - tol:
                offenders.append(
                    (rp, port.id, "approaches RIGHT entry from inside box")
                )
            elif port.side == PortSide.LEFT and prev[0] > bx0 + tol:
                offenders.append((rp, port.id, "approaches LEFT entry from inside box"))
        elif port.side in (PortSide.TOP, PortSide.BOTTOM):
            if abs(prev[0] - end[0]) > tol:
                continue
            if port.side == PortSide.BOTTOM and prev[1] < by1 - tol:
                offenders.append(
                    (rp, port.id, "approaches BOTTOM entry from inside box")
                )
            elif port.side == PortSide.TOP and prev[1] > by0 + tol:
                offenders.append((rp, port.id, "approaches TOP entry from inside box"))
    return offenders


def _guard_entry_approach_from_port_side(
    graph: MetroGraph,
    phase: str,
    *,
    routes: list[RoutedPath] | None = None,
) -> None:
    """After routing: a route's final approach to an entry port must arrive
    from the port's own outward side, not by crossing the target section's
    interior to reach a far-side port.

    Distinct from ``_guard_no_route_through_section`` (which exempts the
    route's own target section): this catches a route reaching its OWN
    target's far-edge entry port by slicing through the box interior and
    doubling back (issue #484).
    """
    routes = _ensure_routes(graph, routes)
    offenders = _entry_approach_offenders(graph, routes)
    if offenders:
        rp, pid, reason = offenders[0]
        raise PhaseInvariantError(
            f"{phase}: route {rp.edge.source!r}->{rp.edge.target!r} "
            f"line {rp.line_id!r} {reason} (entry port {pid!r}); the final "
            f"approach leg crosses the target section interior "
            f"({len(offenders)} offender(s) total)"
        )


def _row_flow_directions(graph: MetroGraph) -> dict[int, str]:
    """Dominant horizontal flow direction (``"LR"``/``"RL"``) per grid row.

    Serpentine layout alternates row flow LR/RL.  The direction of a row is
    the majority of its ``LR``/``RL`` sections; ``TB``/``BT`` transition
    sections are ignored (they carry no horizontal flow).  Rows with no
    horizontal section are absent from the result.
    """
    counts: dict[int, dict[str, int]] = {}
    for s in graph.sections.values():
        if s.bbox_w <= 0 or s.direction not in ("LR", "RL"):
            continue
        counts.setdefault(s.grid_row, {"LR": 0, "RL": 0})[s.direction] += 1
    return {row: ("LR" if c["LR"] >= c["RL"] else "RL") for row, c in counts.items()}


def _guard_no_artefactual_counter_flow(
    graph: MetroGraph,
    phase: str,
    *,
    routes: list[RoutedPath] | None = None,
) -> None:
    """After routing: a feed from a row ABOVE its target must not run its long
    horizontal BELOW the target row counter to that row's flow when a clear
    inter-row gap above the target was available.

    Serpentine rows alternate flow (even rows LR, odd rows RL).  A route into
    a RIGHT entry port whose source sits in a higher row can either run its
    rightward traverse in the clear inter-row band just above the target row
    (then drop straight down the target's RIGHT side into the port), or dive
    UNDER the whole target row and run that traverse counter to the target
    row's flow.  The latter is *artefactual* counter-flow: the routing picked
    the wrong channel when the with-flow gap above was free (issue #484).

    Some counter-flow is *topological* and allowed: a feed into a LEFT/TOP/
    BOTTOM entry from a far source must wrap to reach the port's outward side,
    so the counter-flow is intrinsic; and a RIGHT-entry dive whose gap-above
    band is blocked by an intervening section had no clear alternative.  This
    guard fires only when the with-flow gap channel above the target row was
    genuinely free for the run's X-span yet went unused.
    """
    from nf_metro.layout.routing.common import (
        _center_inter_row_channel,
        _inter_row_band_fits,
        resolve_section,
        row_bottom_edge,
        row_top_edge,
    )
    from nf_metro.layout.routing.core import _h_segment_crosses_other_section

    routes = _ensure_routes(graph, routes)

    tol = GUARD_TOLERANCE
    row_flow = _row_flow_directions(graph)
    for rp in routes:
        if not rp.is_inter_section:
            continue
        src_sec = resolve_section(graph, graph.stations.get(rp.edge.source))
        tgt_sec = resolve_section(graph, graph.stations.get(rp.edge.target))
        if src_sec is None or tgt_sec is None:
            continue
        src_row, tgt_row = src_sec.grid_row, tgt_sec.grid_row
        if src_row < 0 or tgt_row < 0 or src_row >= tgt_row:
            continue
        flow = row_flow.get(tgt_row)
        if flow is None:
            continue
        # Only RIGHT entry ports: the with-flow gap-above approach (drop into
        # the port from its own outward/right side after a clear gap run) is a
        # genuine alternative only here.  A LEFT/TOP/BOTTOM entry fed from a far
        # source must wrap to reach the port's outward side, so any counter-flow
        # is intrinsic (topological), not an avoidable channel choice.
        port = graph.ports.get(rp.edge.target)
        if port is None or not port.is_entry or port.side != PortSide.RIGHT:
            continue
        tgt_top = row_top_edge(graph, tgt_row, default=tgt_sec.bbox_y)
        tgt_bottom = row_bottom_edge(
            graph, tgt_row, default=tgt_sec.bbox_y + tgt_sec.bbox_h
        )
        pts = rp.points
        if len(pts) < 2:
            continue
        # Candidate with-flow channel: the inter-row band immediately ABOVE the
        # target row (the row above the target's bottom up to the target row's
        # top).  Its centre Y is where the routing fix runs the rightward
        # traverse before dropping into the RIGHT port.
        gap_top = row_bottom_edge(graph, tgt_row - 1, default=tgt_top)
        gap_bottom = tgt_top
        if gap_bottom <= gap_top:
            continue  # no inter-row band above target -> dive was forced
        # The with-flow band is a genuine alternative only when wide enough to
        # clear both the upper row's bottom edge and the target row's header
        # badge; a band too narrow for that forces the dive below.
        if not _inter_row_band_fits(gap_top, gap_bottom):
            continue
        gy = _center_inter_row_channel(gap_top, gap_bottom)
        exclude = {sid for sid in (src_sec.id, tgt_sec.id) if sid is not None}
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            if abs(y2 - y1) > tol or abs(x2 - x1) <= tol:
                continue  # horizontal runs only
            # Only a long horizontal diving BELOW the whole row is artefactual.
            # A run above the row bottom is either the with-flow band above the
            # row or the intrinsic in-row approach into a far-side port (the
            # outward-approach guard covers that), neither of which this catches.
            if y1 < tgt_bottom - tol:
                continue
            # (a) the run goes counter to the target row's flow.
            counter = (x2 < x1 - tol) if flow == "LR" else (x2 > x1 + tol)
            if not counter:
                continue
            # (b) the with-flow gap above the target was clear for this X-span.
            if _h_segment_crosses_other_section(graph, x1, x2, gy, exclude):
                continue  # gap blocked -> dive was topologically necessary
            raise PhaseInvariantError(
                f"{phase}: route {rp.edge.source!r}->{rp.edge.target!r} line "
                f"{rp.line_id!r} runs its horizontal at y={y1:.1f} below target "
                f"row {tgt_row} (top={tgt_top:.1f}) counter to that row's {flow} "
                f"flow, but the inter-row gap above the target (y={gy:.1f}) was "
                f"clear for x=[{min(x1, x2):.1f},{max(x1, x2):.1f}]; this is "
                f"artefactual counter-flow (issue #484)"
            )


def _is_side_entry_turn_in(graph: MetroGraph, rp: RoutedPath) -> bool:
    """Whether *rp* is the turn-in leg from an entry port perpendicular to flow.

    An entry port on the axis perpendicular to its section's flow (LEFT/RIGHT on
    a vertical-flow TB/BT section, TOP/BOTTOM on a horizontal-flow LR/RL one)
    reaches the trunk via one traverse perpendicular to that flow.  That leg is
    the entry, bounded by the section width, not a serpentine fold-back, so
    backtrack accounting excludes it.
    """
    port = graph.ports.get(rp.edge.source)
    if not port or not port.is_entry:
        return False
    section = graph.sections.get(port.section_id)
    if section is None:
        return False
    if lanes_run_along_y(section.direction):
        return port.side in (PortSide.TOP, PortSide.BOTTOM)
    return port.side in (PortSide.LEFT, PortSide.RIGHT)


def _guard_serpentine_no_backtrack(
    graph: MetroGraph,
    phase: str,
    *,
    routes: list[RoutedPath] | None = None,
) -> None:
    """After routing: stacked same-direction sections must not backtrack.

    Same-direction sections stacked in one grid column and chained
    serpentine their effective flow row by row so consecutive sections meet
    on a shared side joined by a short vertical drop.  A section that fails
    to alternate enters on the wrong side and folds its internal route back
    across the section width.  For every section in a detected serpentine
    run, the wrong-way horizontal travel of its internal segments must stay
    below half the section width.
    """
    from nf_metro.layout.auto_layout import detect_serpentine_runs

    routes = _ensure_routes(graph, routes)

    dag = graph.section_dag
    if dag is None:
        return
    runs = detect_serpentine_runs(graph, dag.successors, dag.predecessors)
    serpentine_sections = {sid for run in runs for sid in run}
    if not serpentine_sections:
        return

    wrong_way: dict[str, float] = {sid: 0.0 for sid in serpentine_sections}
    for rp in routes:
        src_sec = graph.section_for_station(rp.edge.source)
        if src_sec != graph.section_for_station(rp.edge.target):
            continue
        if src_sec not in serpentine_sections:
            continue
        if _is_side_entry_turn_in(graph, rp):
            # A LEFT/RIGHT entry port feeds the trunk via one turn-in leg
            # perpendicular to the section's flow -- the entry itself, bounded
            # by the section width, not a serpentine fold-back.
            continue
        forward = 1.0 if graph.sections[src_sec].direction != "RL" else -1.0
        xs = [p[0] for p in rp.points]
        for x1, x2 in zip(xs, xs[1:]):
            dx = x2 - x1
            if dx * forward < 0:
                wrong_way[src_sec] += abs(dx)

    for sid, against in wrong_way.items():
        section = graph.sections[sid]
        limit = 0.5 * max(section.bbox_w, 1.0)
        if against > limit + GUARD_TOLERANCE:
            raise PhaseInvariantError(
                f"{phase}: stacked section {sid!r} (dir={section.direction}) "
                f"backtracks {against:.1f}px against its flow (>{limit:.1f}px); "
                f"the serpentine chain is kinking instead of dropping vertically"
            )


def _guard_inter_row_run_clearance(
    graph: MetroGraph,
    phase: str,
    *,
    routes: list[RoutedPath] | None = None,
) -> None:
    """After routing: a horizontal leg of an inter-*row* route must keep
    ``INTER_ROW_EDGE_CLEARANCE`` from its source section's near bbox edge.

    An inter-section bundle that crosses grid rows (e.g. a right-exit
    wrapping down to a left-entry below) lands its horizontal run in the
    inter-row gap.  A run grazing the source bbox reads as "running along
    under the box".  The placement-side widening
    (``_wrap_bundle_row_minimums``) reserves the space; this guard fails
    loudly if a layout change ever lets the run creep back against the box.
    """
    from nf_metro.layout.routing.common import resolve_section

    routes = _ensure_routes(graph, routes)

    tol = GUARD_TOLERANCE
    for rp in routes:
        if not rp.is_inter_section:
            continue
        src_sec = resolve_section(graph, graph.stations.get(rp.edge.source))
        tgt_sec = resolve_section(graph, graph.stations.get(rp.edge.target))
        if src_sec is None or tgt_sec is None:
            continue
        if src_sec.grid_row == tgt_sec.grid_row:
            continue
        left = src_sec.bbox_x
        right = left + src_sec.bbox_w
        top = src_sec.bbox_y
        bottom = top + src_sec.bbox_h
        pts = rp.points
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if abs(y1 - y0) > tol or abs(x1 - x0) < tol:
                continue  # horizontal runs only
            xlo, xhi = sorted((x0, x1))
            if xhi <= left + tol or xlo >= right - tol:
                continue  # run doesn't overlap the source section in X
            y = y0
            if bottom + tol < y < bottom + INTER_ROW_EDGE_CLEARANCE - tol:
                raise PhaseInvariantError(
                    f"{phase}: inter-row run of {rp.edge.source!r}->"
                    f"{rp.edge.target!r} line {rp.line_id!r} at y={y:.1f} sits "
                    f"{y - bottom:.1f}px below source section {src_sec.id!r} "
                    f"bottom={bottom:.1f} (< {INTER_ROW_EDGE_CLEARANCE})"
                )
            if top - INTER_ROW_EDGE_CLEARANCE + tol < y < top - tol:
                raise PhaseInvariantError(
                    f"{phase}: inter-row run of {rp.edge.source!r}->"
                    f"{rp.edge.target!r} line {rp.line_id!r} at y={y:.1f} sits "
                    f"{top - y:.1f}px above source section {src_sec.id!r} "
                    f"top={top:.1f} (< {INTER_ROW_EDGE_CLEARANCE})"
                )


def _guard_trunk_bands_crossing_optimal(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """After routing: every same-direction inter-row trunk band carries the
    line order that minimises crossings between its slots.

    Sibling corridors sharing one inter-row gap stack in a Y order chosen by
    :func:`_plan_trunk_band`.  When two slots are ordered so that one's
    peel-off risers needlessly cross the other's trunk leg, swapping them would
    strictly reduce crossings.  This guard fails loudly if a future change ever
    leaves a band in such an avoidable-crossing order.
    """
    from nf_metro.layout.constants import CURVE_RADIUS, DIAGONAL_RUN
    from nf_metro.layout.routing.context import _build_routing_context
    from nf_metro.layout.routing.normalize import _suboptimal_trunk_bands

    routes = _ensure_routes(graph, routes)
    ctx = _build_routing_context(graph, DIAGONAL_RUN, CURVE_RADIUS, offsets)
    bad = _suboptimal_trunk_bands(routes, ctx)
    if bad:
        y, cur, best = bad[0]
        raise PhaseInvariantError(
            f"{phase}: inter-row trunk band near y={y:.1f} has {cur} crossings; "
            f"reordering its slots reaches {best}. A sibling-corridor band is "
            f"stacked in an avoidable-crossing order."
        )


def _guard_inter_section_descent_edge_clearance(
    graph: MetroGraph,
    phase: str,
    *,
    routes: list[RoutedPath] | None = None,
) -> None:
    """After routing: a vertical descent channel of an inter-section route
    must not *incidentally* graze a section bbox edge.

    A descent legitimately sits on a section edge when its X coincides
    with a port at one of the route's endpoints (a port-to-port drop).
    When the channel instead lands within ``EDGE_TO_BUNDLE_CLEARANCE`` of
    a section edge, on the interior side, with no endpoint port at that
    X, the lines visibly cross the border.  The channel-x selection
    in :func:`_route_l_shape` pushes such channels outward; this guard
    fails loudly if a future change lets one creep back against an edge.
    """
    from nf_metro.layout.routing.common import endpoint_port_xs

    routes = _ensure_routes(graph, routes)

    tol = GUARD_TOLERANCE
    for rp in routes:
        if not rp.is_inter_section:
            continue
        port_xs = endpoint_port_xs(graph, rp.edge)
        pts = rp.points
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            # A short horizontal hop also has small dx, so dx alone would
            # misclassify it as a descent; require a meaningful dy too.
            if abs(x1 - x0) > tol or abs(y1 - y0) < tol:
                continue
            vx = (x0 + x1) / 2
            if any(abs(vx - px) <= COORD_TOLERANCE for px in port_xs):
                continue  # legitimate port-to-port drop
            ylo, yhi = sorted((y0, y1))
            for sec in graph.sections.values():
                if sec.bbox_w <= 0:
                    continue
                if yhi < sec.bbox_y or ylo > sec.bbox_y + sec.bbox_h:
                    continue
                left = sec.bbox_x
                right = left + sec.bbox_w
                from_left = vx - left
                from_right = right - vx
                grazes = (-tol <= from_left < EDGE_TO_BUNDLE_CLEARANCE - tol) or (
                    -tol <= from_right < EDGE_TO_BUNDLE_CLEARANCE - tol
                )
                if grazes:
                    edge_x = left if from_left < from_right else right
                    raise PhaseInvariantError(
                        f"{phase}: descent of {rp.edge.source!r}->"
                        f"{rp.edge.target!r} line {rp.line_id!r} at x={vx:.1f} "
                        f"grazes section {sec.id!r} edge x={edge_x:.1f} "
                        f"(< {EDGE_TO_BUNDLE_CLEARANCE})"
                    )


def _guard_tb_exit_corner_column_order(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """Final-phase: a TB section's LEFT/RIGHT exit corner must continue its
    in-section column order, so the bundle does not cross at the feeder station.

    Wraps :func:`check_tb_exit_corner_preserves_column_order`.  A TB exit corner
    that derives its vertical-drop X from a different reversal convention than
    the column swaps two lines' X and renders a crossing through the feeder
    station marker.
    """
    from nf_metro.layout.routing.invariants import (
        check_tb_exit_corner_preserves_column_order,
    )

    _raise_on_first_violation(
        graph, phase, check_tb_exit_corner_preserves_column_order, offsets, routes
    )


def _guard_no_split_same_line_fanout_descents(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """Final-phase: same-line fan-out descents fuse before either branch turns.

    Wraps :func:`check_no_split_same_line_fanout_descents`: where one line fans
    out from a single source, two descents that overlap in their Y span must
    ride one fused trunk rather than open at distinct Xs, which would peel the
    farther-reaching branch onto the inside of the nearer one and cross it.
    """
    from nf_metro.layout.routing.invariants import (
        check_no_split_same_line_fanout_descents,
    )

    _raise_on_first_violation(
        graph, phase, check_no_split_same_line_fanout_descents, offsets, routes
    )


def _guard_no_distinct_line_fanout_crossing(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """Final-phase: distinct lines diverging from one fan-out stay bundled.

    Wraps :func:`check_no_distinct_line_fanout_crossing`: at a clean-divergence
    junction (distinct lines peeling to disjoint targets), the bundle must
    descend as one unit and split only where each line turns into its target,
    never crossing a mate's run on the way down.
    """
    from nf_metro.layout.routing.invariants import (
        check_no_distinct_line_fanout_crossing,
    )

    _raise_on_first_violation(
        graph, phase, check_no_distinct_line_fanout_crossing, offsets, routes
    )


def _guard_fan_merge_no_partition_crossing(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """Final-phase: a vertical-flow fork/merge keeps its partitioning bundle clear.

    Wraps :func:`check_fan_merge_no_partition_crossing`: at a fork or merge in a
    vertical-flow section, two distinct lines that each reach exactly one
    in-section neighbour must leave/dock in those neighbours' lane order.  A
    transposed order crosses them between the station and where they peel apart,
    a defect the bundle-mate guard misses because the lines ride different edges.
    """
    from nf_metro.layout.routing.invariants import (
        check_fan_merge_no_partition_crossing,
    )

    _raise_on_first_violation(
        graph, phase, check_fan_merge_no_partition_crossing, offsets, routes
    )


def _guard_trunk_continuation_drops_straight(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """Final-phase: a TB in-lane continuation drops straight.

    Wraps :func:`check_trunk_continuation_drops_straight`: where one line shares
    a TB section's trunk lane at both ends of an edge -- a fan-out continuation
    peeling past a sibling, or a collinear feeder into a terminal merge -- it
    must run straight rather than jog by one step off the trunk.
    """
    from nf_metro.layout.routing.invariants import (
        check_trunk_continuation_drops_straight,
    )

    _raise_on_first_violation(
        graph, phase, check_trunk_continuation_drops_straight, offsets, routes
    )


def _guard_no_dogleg_crosses_exempt_trunk(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """Final-phase: a doglegged trunk runs parallel to its exempt mate.

    Wraps :func:`check_no_dogleg_crosses_exempt_trunk`: a non-exempt bypass
    trunk cleared off an ``normalize_exempt`` run of a different line must land
    on the side that keeps the two parallel, never the side whose riser pierces
    the exempt run and crosses it twice.
    """
    from nf_metro.layout.routing.invariants import (
        check_no_dogleg_crosses_exempt_trunk,
    )

    _raise_on_first_violation(
        graph, phase, check_no_dogleg_crosses_exempt_trunk, offsets, routes
    )


def _raise_on_first_violation(
    graph: MetroGraph,
    phase: str,
    check: Callable[
        [MetroGraph, list[RoutedPath], dict[tuple[str, str], float]],
        Sequence[_HasMessage],
    ],
    offsets: dict[tuple[str, str], float] | None,
    routes: list[RoutedPath] | None,
) -> None:
    """Run a route *check* and raise ``PhaseInvariantError`` on its first hit."""
    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)
    if routes is None:
        from nf_metro.layout.routing import route_edges

        try:
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:  # noqa: BLE001 - routing failure surfaces elsewhere
            return

    violations = check(graph, routes, offsets)
    if not violations:
        return
    first = violations[0]
    extra = f" (+{len(violations) - 1} more)" if len(violations) > 1 else ""
    raise PhaseInvariantError(f"{phase}: {first.message()}{extra}")


def _guard_merge_port_approach_side(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
) -> None:
    """Final-phase: at every multi-feeder reconvergence merge port, a
    line that re-joins the bundle perpendicular (rising from a section
    below, descending from one above) must take the bundle slot nearest
    its approach side, so its riser does not cross over the lines that
    arrive horizontally.

    See
    :func:`nf_metro.layout.routing.invariants.check_merge_port_approach_side`
    for the semantic definition.
    """
    from nf_metro.layout.routing.invariants import check_merge_port_approach_side

    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)

    violations = check_merge_port_approach_side(graph, offsets)
    if not violations:
        return
    first = violations[0]
    extra = f" (+{len(violations) - 1} more)" if len(violations) > 1 else ""
    raise PhaseInvariantError(f"{phase}: {first.message()}{extra}")


def _guard_convergence_shallow_feeder_concentric(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
) -> None:
    """Final-phase: at a LEFT entry where bypass feeders climb in from off the
    port row and a flat shallow feeder joins them, the shallow feeder must take
    a port-near slot above the climbing risers rather than weave across the
    turning bundle.

    See
    :func:`nf_metro.layout.routing.invariants.check_convergence_shallow_feeder_concentric`
    for the semantic definition.
    """
    from nf_metro.layout.routing.invariants import (
        check_convergence_shallow_feeder_concentric,
    )

    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)

    messages = check_convergence_shallow_feeder_concentric(graph, offsets)
    if not messages:
        return
    extra = f" (+{len(messages) - 1} more)" if len(messages) > 1 else ""
    raise PhaseInvariantError(f"{phase}: {messages[0]}{extra}")


def _guard_merge_port_outgoing_side_preserved(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
) -> None:
    """Final-phase: a line re-slotted at a merge port must keep that slot
    along the merge row down to its consumer, so it does not cross the trunk
    on the outgoing run.

    See
    :func:`nf_metro.layout.routing.invariants.check_merge_port_outgoing_side_preserved`
    for the semantic definition.
    """
    from nf_metro.layout.routing.invariants import (
        check_merge_port_outgoing_side_preserved,
    )

    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)

    violations = check_merge_port_outgoing_side_preserved(graph, offsets)
    if not violations:
        return
    first = violations[0]
    extra = f" (+{len(violations) - 1} more)" if len(violations) > 1 else ""
    raise PhaseInvariantError(f"{phase}: {first.message()}{extra}")


def _guard_exit_inherits_entry_bundle_order(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
) -> None:
    """Final-phase: an LR/RL section fed by a single incoming bundle must
    keep that bundle's order at its exit port, so a line running straight
    through the section is not re-sorted off its incoming slot.

    See
    :func:`nf_metro.layout.routing.invariants.check_exit_inherits_entry_bundle_order`
    for the semantic definition.
    """
    from nf_metro.layout.routing.invariants import (
        check_exit_inherits_entry_bundle_order,
    )

    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)

    violations = check_exit_inherits_entry_bundle_order(graph, offsets)
    if not violations:
        return
    first = violations[0]
    extra = f" (+{len(violations) - 1} more)" if len(violations) > 1 else ""
    raise PhaseInvariantError(f"{phase}: {first.message()}{extra}")


def _guard_bypass_port_no_slot_gaps(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
) -> None:
    """Final-phase: at a multi-feeder merge port containing a bypass horizontal
    line, all bundle slots must be consecutive with no empty interior gaps.

    A bypass line that is incorrectly classified as a horizontal co-traveller
    inflates ``max_horiz`` and pushes perpendicular feeders into outer slots,
    leaving empty slots between the horizontal band and the feeders.
    """
    from nf_metro.layout.routing.invariants import (
        bypass_horizontal_targets,
        classify_merge_port_feeders,
        distinct_offset_levels,
    )

    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)

    for port_id in graph.ports:
        if classify_merge_port_feeders(graph, port_id) is None:
            continue
        bypass = bypass_horizontal_targets(graph, port_id)
        if not bypass:
            continue
        lines = list(graph.station_lines(port_id))
        port_offsets = sorted(offsets.get((port_id, lid), 0.0) for lid in lines)
        levels = distinct_offset_levels(port_offsets)
        has_gap = any(
            levels[i + 1] - levels[i] > OFFSET_STEP + COORD_TOLERANCE_FINE
            for i in range(len(levels) - 1)
        )
        if has_gap:
            raise PhaseInvariantError(
                f"{phase}: merge port {port_id!r} has empty bundle slot gaps: "
                f"max offset {port_offsets[-1]:.1f} > expected "
                f"{(len(port_offsets) - 1) * OFFSET_STEP:.1f} "
                f"for {len(port_offsets)} lines "
                f"(offsets: {[f'{o:.0f}' for o in port_offsets]})"
            )


def _guard_partial_branch_offset_gaps(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
) -> None:
    """Final-phase: under ``compact_offsets``, an independent fan branch
    that carries only a subset of a bundle's lines must place them on
    consecutive offset slots, not reserve an empty interior slot for the
    lines it omits (which parks its marker off-centre with a gap).

    See
    :func:`nf_metro.layout.routing.invariants.check_partial_branch_offset_gaps`
    for the semantic definition.
    """
    from nf_metro.layout.routing.invariants import check_partial_branch_offset_gaps

    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)

    violations = check_partial_branch_offset_gaps(graph, offsets)
    if not violations:
        return
    first = violations[0]
    extra = f" (+{len(violations) - 1} more)" if len(violations) > 1 else ""
    raise PhaseInvariantError(f"{phase}: {first.message()}{extra}")


def _guard_fanout_tail_join(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """Final-phase: at every single-source fan-out junction, each
    upstream ``port -> junction`` route must hand off to its same-line
    downstream ``junction -> target`` route with no gap along the line's
    travel direction (no visible apex notch).

    See :func:`nf_metro.layout.routing.invariants.check_fanout_tail_join`
    for the semantic definition.
    """
    from nf_metro.layout.routing.invariants import check_fanout_tail_join

    if routes is None:
        from nf_metro.layout.routing import compute_station_offsets, route_edges

        if offsets is None:
            offsets = compute_station_offsets(graph)
        try:
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:  # noqa: BLE001 - routing failure surfaces elsewhere
            return

    gaps = check_fanout_tail_join(routes, graph)
    if not gaps:
        return
    first = gaps[0]
    extra = f" (+{len(gaps) - 1} more)" if len(gaps) > 1 else ""
    raise PhaseInvariantError(f"{phase}: {first.message()}{extra}")


def _guard_perp_entry_boundary_consistent(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """Final-phase: a line crossing a shared TOP/BOTTOM entry port must do so at
    one consistent per-line X - its inter-section approach and its
    intra-section drop must not reverse lateral direction at the boundary.

    See
    :func:`nf_metro.layout.routing.invariants.check_perp_entry_boundary_consistent`
    for the semantic definition.
    """
    from nf_metro.layout.routing.invariants import (
        check_perp_entry_boundary_consistent,
    )

    if routes is None:
        from nf_metro.layout.routing import compute_station_offsets, route_edges

        if offsets is None:
            offsets = compute_station_offsets(graph)
        try:
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:  # noqa: BLE001 - routing failure surfaces elsewhere
            return

    violations = check_perp_entry_boundary_consistent(graph, routes)
    if not violations:
        return
    first = violations[0]
    extra = f" (+{len(violations) - 1} more)" if len(violations) > 1 else ""
    raise PhaseInvariantError(f"{phase}: {first.message()}{extra}")


def _guard_perp_exit_over_leadin_no_overdip(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """Final-phase: a cross-column perp-exit lead-in must clear only the
    sections its exit-side down-leg actually passes under, not the row's
    deepest section in a far column.

    See
    :func:`nf_metro.layout.routing.invariants.check_perp_exit_over_leadin_clears_only_spanned_sections`
    for the semantic definition.
    """
    from nf_metro.layout.routing.invariants import (
        check_perp_exit_over_leadin_clears_only_spanned_sections,
    )

    if routes is None:
        from nf_metro.layout.routing import compute_station_offsets, route_edges

        if offsets is None:
            offsets = compute_station_offsets(graph)
        try:
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:  # noqa: BLE001 - routing failure surfaces elsewhere
            return

    violations = check_perp_exit_over_leadin_clears_only_spanned_sections(graph, routes)
    if not violations:
        return
    first = violations[0]
    extra = f" (+{len(violations) - 1} more)" if len(violations) > 1 else ""
    raise PhaseInvariantError(f"{phase}: {first.message()}{extra}")


def _guard_right_entry_drop_in_when_clear(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """Final-phase: a RIGHT entry fed from above must take the direct drop-in
    rather than loop below the box when the outward-side descent is clear.

    See
    :func:`nf_metro.layout.routing.invariants.check_right_entry_drop_in_when_clear`
    for the semantic definition.
    """
    from nf_metro.layout.routing.invariants import (
        check_right_entry_drop_in_when_clear,
    )

    if routes is None:
        from nf_metro.layout.routing import compute_station_offsets, route_edges

        if offsets is None:
            offsets = compute_station_offsets(graph)
        try:
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:  # noqa: BLE001 - routing failure surfaces elsewhere
            return

    violations = check_right_entry_drop_in_when_clear(graph, routes)
    if not violations:
        return
    first = violations[0]
    extra = f" (+{len(violations) - 1} more)" if len(violations) > 1 else ""
    raise PhaseInvariantError(f"{phase}: {first.message()}{extra}")


def _guard_right_entry_corridor_descent_no_jog(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """Final-phase: a RIGHT entry fed from the left must reach a clear descent
    corridor at the top corner, not step onto it partway down the descent.

    See
    :func:`nf_metro.layout.routing.invariants.check_right_entry_corridor_descent_no_jog`
    for the semantic definition.
    """
    from nf_metro.layout.routing.invariants import (
        check_right_entry_corridor_descent_no_jog,
    )

    if routes is None:
        from nf_metro.layout.routing import compute_station_offsets, route_edges

        if offsets is None:
            offsets = compute_station_offsets(graph)
        try:
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:  # noqa: BLE001 - routing failure surfaces elsewhere
            return

    violations = check_right_entry_corridor_descent_no_jog(graph, routes)
    if not violations:
        return
    first = violations[0]
    extra = f" (+{len(violations) - 1} more)" if len(violations) > 1 else ""
    raise PhaseInvariantError(f"{phase}: {first.message()}{extra}")


def _guard_off_track_clear_of_anchor(graph: MetroGraph, phase: str) -> None:
    """At final: every off-track station must sit at least ``GUARD_TOLERANCE``
    clear of its anchor on the expected side of the section's cross axis.

    An input's anchor is its consumer, a producer-fed sink's anchor is its
    producer; :func:`_off_track_anchor_of` resolves which, so the same
    relationship is enforced for both roles.  The offset is on the cross axis
    (:func:`section_cross_axis`): Y for an LR/RL trunk, X for a TB/BT one, so an
    off-track sits beside where its data is read whatever the flow direction.
    It is normally on the lift side of its anchor (:func:`_off_track_lift_sign`
    -- above an LR trunk, beside a TB one); an output whose producer sits on a
    branch column (:func:`_off_track_output_below`) is offset the other way so
    it does not cross back over the trunk.
    """
    below = _off_track_output_below(graph)
    for off_id, anchor_id in _off_track_anchor_of(graph).items():
        off_st = graph.stations.get(off_id)
        anchor_st = graph.stations.get(anchor_id)
        if off_st is None or anchor_st is None:
            continue
        section = graph.sections.get(off_st.section_id or "")
        flow, cross = section_axes(section)
        # The expected offset direction: the lift side, flipped for a branch
        # output that runs out the far side of the trunk.
        want_sign = _off_track_lift_sign(section)
        if off_id in below:
            want_sign = -want_sign
        signed = want_sign * (getattr(off_st, cross) - getattr(anchor_st, cross))
        if not (signed > GUARD_TOLERANCE):
            side = "beside" if cross == "x" else ("below" if want_sign > 0 else "above")
            raise PhaseInvariantError(
                f"{phase}: off-track {off_id!r} {cross}={getattr(off_st, cross):.1f} "
                f"not clear {side} anchor {anchor_id!r} "
                f"{cross}={getattr(anchor_st, cross):.1f}"
            )
        # The connector must hang via an S, not leave the anchor perpendicular to
        # flow: the off-track keeps a flow-axis lead off its anchor.
        if abs(getattr(off_st, flow) - getattr(anchor_st, flow)) <= GUARD_TOLERANCE:
            raise PhaseInvariantError(
                f"{phase}: off-track {off_id!r} shares anchor {anchor_id!r} "
                f"{flow}={getattr(anchor_st, flow):.1f} - its connector leaves the "
                f"station perpendicular to flow instead of hanging via an S"
            )


def _guard_fanout_junction_shares_exit_port_y(graph: MetroGraph, phase: str) -> None:
    """A fan-out junction fed by an LR/RL exit port must share that port's Y.

    ``_position_junctions`` anchors such a junction at the exit port's Y so the
    bundle runs straight from exit to junction.  When a late settling pass
    moves the exit port without re-running junction positioning, the junction
    is stranded above/below the port and the fanned routes dip to the stale
    junction Y and back (#386).  BOTTOM/TOP exit ports are intentionally offset
    from their junction, so only LEFT/RIGHT exits are checked.
    """
    for jid in graph.junction_ids:
        junction = graph.stations.get(jid)
        if junction is None:
            continue
        port_preds = {
            e.source
            for e in graph.edges_to(jid)
            if graph.station_for_edge_source(e).is_port
        }
        entry_succs = {
            e.target
            for e in graph.edges_from(jid)
            if graph.station_for_edge_target(e).is_port
        }
        if len(port_preds) != 1 or len(entry_succs) <= 1:
            continue
        exit_port = graph.stations[next(iter(port_preds))]
        port_obj = graph.ports.get(exit_port.id)
        if port_obj is None or port_obj.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        if abs(junction.y - exit_port.y) > GUARD_TOLERANCE:
            raise PhaseInvariantError(
                f"{phase}: fan-out junction {jid!r} y={junction.y:.1f} "
                f"stranded from exit port {exit_port.id!r} y={exit_port.y:.1f}"
            )


def _guard_fanout_junction_resolves_upstream(graph: MetroGraph, phase: str) -> None:
    """Every ``section_id``-less junction resolves to a section upstream.

    ``resolve_section`` traces such a junction to a connected port's section
    through its incoming edges; a fan-out junction is emitted with an
    ``exit_port -> junction`` edge whose source carries the source section's
    id, so the upstream scan always resolves.  A junction that resolves to no
    section would leave routing without a grid column/row for it and silently
    misplace the fanned bundle.
    """
    from nf_metro.layout.routing.common import resolve_section

    for jid in graph.junction_ids:
        junction = graph.stations.get(jid)
        if junction is None or junction.section_id:
            continue
        if resolve_section(graph, junction) is None:
            raise PhaseInvariantError(
                f"{phase}: fan-out junction {jid!r} resolves to no section; "
                f"its upstream neighbours carry no section_id"
            )


def _guard_entry_port_fed_only_by_ports(graph: MetroGraph, phase: str) -> None:
    """Every edge into a section entry port originates at a port station.

    ``_resolve_sections`` rewrites each inter-section edge into a chain
    ``source -> exit_port -> entry_port -> target``, so an entry port's
    incoming edges come from exit ports or fan-out junctions, all carrying
    ``is_port=True``.  ``_section_line_feeders`` relies on this to read the
    feeder section straight off the source's ``section_id``; a non-port
    source would mean an internal station feeds an entry port directly,
    which would mis-attribute the reconvergence feeder ordering.
    """
    for section in graph.sections.values():
        for pid in section.entry_ports:
            for edge in graph.edges_to(pid):
                src = graph.station_for_edge_source(edge)
                if not src.is_port:
                    raise PhaseInvariantError(
                        f"{phase}: entry port {pid!r} is fed by non-port station "
                        f"{edge.source!r}; entry ports must be fed only by ports"
                    )


def _guard_perp_entry_feed_not_collinear(graph: MetroGraph, phase: str) -> None:
    """A TOP/BOTTOM entry port never sits at a flow-aligned feeder's Y.

    A perpendicular (TOP/BOTTOM) entry port is held off the consumer's
    internal station rows by the station-as-elbow constraint, and snaps to
    the section's top/bottom boundary edge.  A LEFT/RIGHT exit port seats at
    a producer station row in a vertically-distinct section, so a feed from
    one can never be collinear with the entry port; a collinear feed there
    means a producer has dragged the port off its boundary edge.

    A perpendicular (TOP/BOTTOM) exit port is exempt: it rises into the
    inter-row corridor band that also hosts the entry port (the up-and-over
    and drop-pair shapes), so feeder and entry legitimately share that Y.
    ``_perp_corridor_feeder`` keys the matching entry-drop route off the same
    collinear perp-exit feed this guard exempts.
    """
    for section in graph.sections.values():
        for pid in section.entry_ports:
            port = graph.ports.get(pid)
            if port is None or port.side not in (PortSide.TOP, PortSide.BOTTOM):
                continue
            entry_st = graph.stations.get(pid)
            if entry_st is None:
                continue
            for edge in graph.edges_to(pid):
                feeder = graph.station_for_edge_source(edge)
                feeder_port = graph.ports.get(edge.source)
                if (
                    feeder_port is not None
                    and not feeder_port.is_entry
                    and feeder_port.side in (PortSide.TOP, PortSide.BOTTOM)
                ):
                    continue
                if abs(feeder.y - entry_st.y) <= COORD_TOLERANCE:
                    raise PhaseInvariantError(
                        f"{phase}: {port.side.name} entry port {pid!r} "
                        f"(y={entry_st.y:.1f}) is collinear with its feeder "
                        f"{edge.source!r} (y={feeder.y:.1f}); a perpendicular "
                        f"entry port must stay off its feeder's Y"
                    )


def _guard_station_x_column_drift(graph: MetroGraph, phase: str) -> None:
    """Final-phase: within each LR/RL section, stations sharing a layer
    must agree on X within one ``X_SPACING`` of the layer's median X.

    Excludes loop-side-branch stations: ``_recenter_loop_side_stations``
    deliberately moves single in/out stations whose endpoints share Y to
    the midpoint of their loop's diagonal corners, legitimately decoupling
    their X from the column grid.
    """
    import statistics

    for sec in graph.sections.values():
        if sec.bbox_h <= 0 or sec.direction not in ("LR", "RL"):
            continue
        port_ids = sec.port_ids
        layer_xs: dict[int, list[tuple[str, float]]] = {}
        for sid in sec.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.is_hidden:
                continue
            if st.off_track:
                continue
            if is_loop_side_branch_station(graph, sid):
                continue
            layer_xs.setdefault(st.layer, []).append((sid, st.x))
        for layer, members in layer_xs.items():
            if len(members) < 2:
                continue
            xs = [x for _, x in members]
            median_x = statistics.median(xs)
            for sid, x in members:
                if abs(x - median_x) > X_SPACING:
                    raise PhaseInvariantError(
                        f"{phase}: section {sec.id!r} layer {layer} "
                        f"{sid!r} x={x:.1f} drifts "
                        f"{abs(x - median_x):.1f} > X_SPACING={X_SPACING:.1f} "
                        f"from median={median_x:.1f}"
                    )


_PASS_C_BISECTION_ORDER: tuple[str, ...] = (
    "after Stage 5.2",
    "after Stage 5.3",
    "after Stage 5.4",
    "after Stage 5.5",
    "after Stage 6.1",
    "after Stage 6.2",
    "after Stage 6.3",
    "after Stage 6.4",
    "after Stage 6.5",
    "after Stage 6.6",
    "after Stage 6.9",
    "after Stage 6.10",
    "after Stage 6.11",
    "after Stage 6.12",
    "after Stage 6.13",
    "after Stage 6.14",
    "after Stage 6.15",
)
"""Ordered Pass C bisection checkpoints, used by
``_run_pass_c_guards`` to gate guards whose invariants only become
valid mid-pipeline.  Update when adding or removing a Pass C
checkpoint in ``_compute_section_layout``.
"""


def _bisection_should_run(guard_name: str, phase: str) -> bool:
    """True if ``guard_name`` should run at bisection checkpoint ``phase``.

    Returns True for the final guard block (``phase`` outside
    ``_PASS_C_BISECTION_ORDER``) so the final invariant set stays complete.
    """
    threshold = _BISECTION_FIRST_VALID.get(guard_name)
    if threshold is None:
        return True
    try:
        phase_idx = _PASS_C_BISECTION_ORDER.index(phase)
        threshold_idx = _PASS_C_BISECTION_ORDER.index(threshold)
        return phase_idx >= threshold_idx
    except ValueError:
        return True


def _run_pass_c_guards(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> tuple[dict[tuple[str, str], float], list[RoutedPath] | None]:
    """Run the per-checkpoint bisection guard set at a Pass C sub-phase boundary.

    The Pass C tidy-up pipeline is a sequence of ~20 mutating passes
    over a shared graph; sampling guards only at the final state surfaces
    a regression introduced at e.g. Stage 6.7 as ``after final: ...`` with
    no way to bisect.  Running the bisection-safe overlap / breeze-past /
    column-drift checks at each boundary localises the culprit to a single
    phase.

    Dispatches the ``bisection_safe`` entries of :data:`GUARD_REGISTRY`
    (gated by each spec's ``first_valid_stage``); the final-only entries are
    skipped here and run by :func:`run_validate_guards` at the closing
    ``after final`` boundary.  Returns the computed ``(offsets, routes)`` so
    the caller can pass them on.
    """
    return run_validate_guards(
        graph, phase, include_final=False, offsets=offsets, routes=routes
    )


def _guard_no_label_overlap(graph: MetroGraph, phase: str) -> None:
    """Raise if any station label overlaps another label or a marker.

    Runs after the spread loop has settled, so it asserts the final, fully
    wrapped-and-spread state.  Label/label overlap is never tolerated;
    label/marker grazes within ``LABEL_OVERLAP_TOL`` are allowed (see
    :func:`nf_metro.layout.labels.find_label_overlaps`).
    """
    residual = _residual_label_overlaps(graph, allow_hyphenation=True)
    if not residual:
        return
    ov = residual[0]
    kind = "label" if ov.kind == "label" else "marker"
    raise PhaseInvariantError(
        f"{phase}: station label {ov.a!r} overlaps "
        f"{kind} {ov.b!r} by ({ov.ox:.1f}, {ov.oy:.1f})px after wrapping and "
        f"spreading; {len(residual)} overlap(s) total"
    )


def _guard_file_icon_no_name_label(graph: MetroGraph, phase: str) -> None:
    """Raise if a file-icon station also receives a separate name label.

    A ``%%metro file:`` station renders its caption(s) beneath the icon
    (``terminus_names``); per #93 the file directive owns the station's
    labelling entirely.  A second node-name label would overprint that
    caption and the converging tracks, so no ``is_terminus`` station may
    appear among the placed name labels regardless of its node label text.
    """
    terminus_ids = {s.id for s in graph.stations.values() if s.is_terminus}
    if not terminus_ids:
        return
    offenders = sorted(terminus_ids & _placed_name_label_station_ids(graph))
    if offenders:
        raise PhaseInvariantError(
            f"{phase}: file-icon station(s) {offenders} also got a separate "
            f"name label, overprinting the icon caption and tracks; a "
            f"%%metro file: station must not carry a node-name label"
        )


@dataclass(frozen=True)
class GuardSpec:
    """One ``validate=True`` guard, with the dispatch + classification data
    that used to be scattered across hand-written call sites and the
    ``_BISECTION_FIRST_VALID`` table.

    ``fn`` is the guard function; every guard takes ``(graph, phase)`` and the
    optional keyword inputs named in ``needs`` (a subset of ``offsets``,
    ``routes``, ``section_y_gap``, ``section_y_padding``).  The dispatcher
    passes exactly those keywords, so heterogeneous signatures need no
    wrapping.

    ``bisection_safe`` guards run at every Pass C checkpoint (gated by
    ``first_valid_stage``, the earliest checkpoint at which their invariant
    holds) as well as at the closing ``after final`` boundary; the rest run
    only at ``after final``.  ``tier`` is the cost-tier classification
    (``docs/dev/guard_tiers.md``).

    ``issue_pin`` is the tuple of ``#NNN`` issues a guard was born from; it
    keeps the regression trail as data so consolidating or renaming a guard
    cannot let the original bug be silently re-filed.  ``narrow_reason`` states
    why a guard pinned to an issue stays scoped to its case rather than being
    folded into a broader geometric property -- a required field for any
    issue-pinned guard (``test_issue_pinned_guards_document_why_they_are_narrow``).
    """

    fn: Callable[..., Any]
    tier: str
    needs: frozenset[str] = field(default_factory=frozenset)
    bisection_safe: bool = False
    first_valid_stage: str | None = None
    issue_pin: tuple[str, ...] = ()
    narrow_reason: str | None = None

    @property
    def name(self) -> str:
        return self.fn.__name__


# The single ordered source of truth for the ``validate=True`` guard
# sequence.  The runner iterates this list in order, so its order *is* the
# guard call order; the bisection-safe prefix is what ``_run_pass_c_guards``
# runs at each Pass C checkpoint, and the whole list is what
# ``run_validate_guards`` runs at ``after final``.
GUARD_REGISTRY: tuple[GuardSpec, ...] = (
    # --- bisection-safe set (run at every Pass C checkpoint + at final) ---
    GuardSpec(_guard_coordinates_finite, "A", bisection_safe=True),
    GuardSpec(_guard_section_bboxes_positive, "A", bisection_safe=True),
    # Stage 5.2 lifts off-track stations above their section's pre-grow bbox
    # top; Stage 5.3's row top-align grows the bbox upward to enclose them.
    GuardSpec(
        _guard_stations_in_sections,
        "A",
        bisection_safe=True,
        first_valid_stage="after Stage 5.3",
    ),
    GuardSpec(_guard_ports_on_boundaries, "A", bisection_safe=True),
    # Pre-snap fan placement can sit a fraction of a pitch off the row grid;
    # Stage 6.4's snap pulls every station onto the grid and onto distinct
    # same-column slots, after which markers must be collision-free.
    GuardSpec(
        _guard_no_station_overlap,
        "A",
        needs=frozenset({"offsets"}),
        bisection_safe=True,
        first_valid_stage="after Stage 6.4",
    ),
    GuardSpec(
        _guard_no_coincident_station_coords,
        "A",
        bisection_safe=True,
        first_valid_stage="after Stage 6.4",
    ),
    # A sparse loop-side station (single line in/out, full-bundle row-mates)
    # sits on the trunk Y until Stage 6.14 shifts it to a half-grid offset;
    # before that the sibling bundle's route passes through its marker bbox.
    GuardSpec(
        _guard_no_line_crosses_non_consumer,
        "A",
        needs=frozenset({"offsets", "routes"}),
        bisection_safe=True,
        first_valid_stage="after Stage 6.14",
    ),
    # The sparse loop-station shift runs at Stage 6.14, so its crowding
    # outcome is only observable from that checkpoint onward.
    GuardSpec(
        _guard_sparse_loop_station_clears_column_neighbour,
        "A",
        bisection_safe=True,
        first_valid_stage="after Stage 6.14",
        issue_pin=("#1071",),
        narrow_reason=(
            "Scoped to sparse single-line loop stations (one edge in, one out, "
            "both endpoints on the section trunk) on LR/RL sections -- the only "
            "stations Stage 6.14 shifts -- and exempts half-grid symfan members "
            "that legitimately sit a half pitch from a column neighbour."
        ),
    ),
    GuardSpec(_guard_station_x_column_drift, "A", bisection_safe=True),
    # --- final-only set (run only at the closing ``after final`` boundary) ---
    # A desync feeds a stale port position to later phases, not the renderer
    # (routing reads the Station record), so this is a pipeline-consistency
    # check for validate runs rather than a render-output guard.
    GuardSpec(_guard_port_station_coords_synced, "B"),
    GuardSpec(_guard_no_section_overlap, "B"),
    # The row trunk Y is only finalised once Stage 6.7 has re-centred
    # ``center_ports`` graphs, so this cannot bisect.
    GuardSpec(_guard_row_trunk_cy_consistent, "B", needs=frozenset({"offsets"})),
    # Stage 6.4's snap-to-grid shifts the on-track anchor Y by up to half a
    # pitch before Stage 6.6 re-anchors the off-track station, so this cannot
    # bisect.
    GuardSpec(_guard_off_track_clear_of_anchor, "B"),
    GuardSpec(
        _guard_fanout_junction_shares_exit_port_y,
        "B",
        issue_pin=("#386",),
        narrow_reason=(
            "Only LEFT/RIGHT exit ports are checked: BOTTOM/TOP exit ports are "
            "intentionally offset from their fan-out junction."
        ),
    ),
    GuardSpec(_guard_fanout_junction_resolves_upstream, "B"),
    GuardSpec(_guard_entry_port_fed_only_by_ports, "B"),
    GuardSpec(_guard_flow_exit_anchored_to_carrier, "B"),
    GuardSpec(_guard_wrap_exit_anchored_to_carrier, "B"),
    GuardSpec(_guard_fold_lr_exit_follows_target, "B"),
    GuardSpec(
        _guard_fold_lr_exit_sections_share_bbox_bottom,
        "B",
        issue_pin=("#1162",),
        narrow_reason=(
            "Only straight folded LR/RL runs are checked (a cross-row, "
            "bbox-contained entry target sitting at the exit Y).  A staircase "
            "run into a target seated off the exit Y, or a same-row seam, has no "
            "single shared bottom edge to balance against."
        ),
    ),
    GuardSpec(
        _guard_stacked_rows_fill_rowspan_band,
        "B",
        issue_pin=("#1207", "#1209"),
        narrow_reason=(
            "Scoped to a column whose single-row sections cover, one per row, "
            "the full row range a neighbouring rowspan section spans, with slack "
            "to distribute.  A partial stack or a band already filled has no "
            "outer edges to pin to the neighbour."
        ),
    ),
    GuardSpec(
        _guard_perp_fed_entry_anchored_to_consumer,
        "B",
        issue_pin=("#908",),
        narrow_reason=(
            "Scoped to a LEFT/RIGHT entry whose sole feed is a same-row "
            "perpendicular-to-flow exit and whose internal consumers share one "
            "Y: a multi-consumer entry fans to several rows, so no single "
            "consumer Y can anchor it, and a trunk-aligned (non-perpendicular) "
            "feed already arrives on the consumer row."
        ),
    ),
    GuardSpec(
        _guard_corridor_fed_solo_rides_trunk,
        "B",
        needs=frozenset({"offsets"}),
        issue_pin=("#1173",),
        narrow_reason=(
            "Scoped to LEFT/RIGHT entries of an LR/RL section carrying a single "
            "present line, fed only over a vertical corridor (every feeder on a "
            "different base Y): a multi-line section keeps its bundle lanes, and "
            "a flat same-Y seam must hold the upstream lane or the "
            "straight-through run would slope into an almost-horizontal segment."
        ),
    ),
    GuardSpec(
        _guard_perp_entry_clears_vertical_trunk_head,
        "B",
        issue_pin=("#1054",),
        narrow_reason=(
            "Scoped to LEFT/RIGHT entry ports on vertical-flow (TB/BT) sections, "
            "where the entry is perpendicular to the trunk: a horizontal-flow "
            "section's LEFT/RIGHT entry is flow-aligned and lands on a station "
            "row by design, and TOP/BOTTOM entries continue the trunk rather "
            "than turning into it."
        ),
    ),
    GuardSpec(
        _guard_post_convergence_trunk_continues,
        "B",
        issue_pin=("#946", "#977"),
        narrow_reason=(
            "Scoped to the sole in-section forward successor of a line-shedding "
            "predecessor: a predecessor with multiple successors (including a "
            "bypass V) fans out by design, a successor carrying as many lines is "
            "trunk-aligned already, and a cross-section convergence anchors its "
            "successor via port alignment."
        ),
    ),
    GuardSpec(_guard_perp_entry_feed_not_collinear, "B"),
    GuardSpec(_guard_merge_port_approach_side, "B", needs=frozenset({"offsets"})),
    GuardSpec(
        _guard_convergence_shallow_feeder_concentric,
        "B",
        needs=frozenset({"offsets"}),
    ),
    GuardSpec(
        _guard_merge_port_outgoing_side_preserved,
        "B",
        needs=frozenset({"offsets"}),
    ),
    GuardSpec(
        _guard_exit_inherits_entry_bundle_order,
        "B",
        needs=frozenset({"offsets"}),
    ),
    GuardSpec(_guard_bypass_port_no_slot_gaps, "B", needs=frozenset({"offsets"})),
    GuardSpec(_guard_partial_branch_offset_gaps, "B", needs=frozenset({"offsets"})),
    GuardSpec(_guard_row_gaps, "B", needs=frozenset({"section_y_gap"})),
    GuardSpec(
        _guard_section_top_padding,
        "B",
        needs=frozenset({"section_y_gap", "section_y_padding", "offsets"}),
        issue_pin=("#406",),
        narrow_reason=(
            "Mirror of the bottom-padding contract; a gap-bounded top is "
            "allowed where the row above legitimately constrains it."
        ),
    ),
    GuardSpec(
        _guard_section_bottom_padding,
        "B",
        needs=frozenset({"section_y_padding", "offsets"}),
        issue_pin=("#1274",),
        narrow_reason=(
            "Mirror of the top-padding contract; a row-mate-pinned bottom "
            "is allowed to sit further down than its own content requires."
        ),
    ),
    GuardSpec(
        _guard_terminus_icons_within_bbox,
        "B",
        issue_pin=("#254",),
        narrow_reason=(
            "Scoped to TB/BT termini, whose file icon stacks vertically past "
            "the marker and so needs explicit bbox room."
        ),
    ),
    # Row-band height tolerance assumes final bboxes, which Stages 6.13 / 6.14
    # may still be shrinking, so this cannot bisect.
    GuardSpec(
        _guard_inter_section_routes_in_row_band,
        "B",
        needs=frozenset({"offsets", "routes"}),
    ),
    GuardSpec(
        _guard_topmost_row_top_entry_hugs_section,
        "B",
        needs=frozenset({"offsets", "routes"}),
    ),
    GuardSpec(
        _guard_title_band_clearance,
        "B",
        needs=frozenset({"section_y_padding"}),
        issue_pin=("#1273",),
        narrow_reason=(
            "Scoped to titled maps, whose canvas-top title band the header "
            "must clear; untitled maps keep the tighter section_y_padding top."
        ),
    ),
    GuardSpec(
        _guard_off_track_output_clears_non_producer,
        "B",
        needs=frozenset({"offsets", "routes"}),
    ),
    GuardSpec(
        _guard_tb_exit_corner_column_order,
        "B",
        needs=frozenset({"offsets", "routes"}),
    ),
    GuardSpec(
        _guard_no_split_same_line_fanout_descents,
        "B",
        needs=frozenset({"offsets", "routes"}),
    ),
    GuardSpec(
        _guard_no_distinct_line_fanout_crossing,
        "B",
        needs=frozenset({"offsets", "routes"}),
    ),
    GuardSpec(
        _guard_fan_merge_no_partition_crossing,
        "B",
        needs=frozenset({"offsets", "routes"}),
    ),
    GuardSpec(
        _guard_trunk_continuation_drops_straight,
        "B",
        needs=frozenset({"offsets", "routes"}),
        issue_pin=("#929", "#1007"),
        narrow_reason=(
            "Scoped to TB sections, the only axis that draws its lane reversed "
            "against a per-station bundle max; LR/RL draw the lane un-reversed "
            "and keep a fan-out or fan-in continuation on the trunk via base "
            "priority."
        ),
    ),
    GuardSpec(
        _guard_no_dogleg_crosses_exempt_trunk,
        "B",
        needs=frozenset({"offsets", "routes"}),
    ),
    GuardSpec(
        _guard_no_stacked_elbow_graze,
        "B",
        needs=frozenset({"offsets", "routes"}),
    ),
    GuardSpec(_guard_fanout_tail_join, "B", needs=frozenset({"offsets", "routes"})),
    GuardSpec(
        _guard_perp_entry_boundary_consistent,
        "B",
        needs=frozenset({"offsets", "routes"}),
    ),
    GuardSpec(
        _guard_perp_exit_over_leadin_no_overdip,
        "B",
        needs=frozenset({"offsets", "routes"}),
    ),
    GuardSpec(
        _guard_right_entry_drop_in_when_clear,
        "B",
        needs=frozenset({"offsets", "routes"}),
    ),
    GuardSpec(
        _guard_right_entry_corridor_descent_no_jog,
        "B",
        needs=frozenset({"offsets", "routes"}),
    ),
    GuardSpec(
        _guard_inter_section_route_no_backtrack,
        "A",
        needs=frozenset({"routes"}),
        issue_pin=("#386",),
        narrow_reason=(
            "Only forward-flowing routes between two LR columns must stay "
            "X-monotonic; away-exit wraps, normalize-exempt legs, TB folds and "
            "same-column routes reverse legitimately and are skipped."
        ),
    ),
    GuardSpec(
        _guard_inter_section_route_no_full_width_backtrack,
        "A",
        needs=frozenset({"routes"}),
    ),
    GuardSpec(
        _guard_routes_enter_sections_at_ports,
        "B",
        needs=frozenset({"routes"}),
    ),
    GuardSpec(
        _guard_rail_connector_ports_no_stub,
        "B",
        needs=frozenset({"routes"}),
        issue_pin=("#743",),
        narrow_reason=(
            "Acts only on whole-graph rail-mode routes whose endpoints are both "
            "boundary ports of different sections; the wrap corridor is exempt "
            "from the X-monotonic backtrack guards by design."
        ),
    ),
    GuardSpec(
        _guard_no_route_through_section,
        "A",
        needs=frozenset({"routes", "offsets"}),
        issue_pin=("#484",),
        narrow_reason=(
            "A route may occupy a section's bbox only where it has a station "
            "there (its source or port-entered target); exempting those is "
            "what stops it flagging legitimate occupancy."
        ),
    ),
    GuardSpec(
        _guard_inter_section_route_clears_own_section_interior,
        "A",
        needs=frozenset({"routes", "offsets"}),
        issue_pin=("#1074", "#1078", "#1081", "#1083"),
        narrow_reason=(
            "Fires only on a route through its OWN source/target box interior "
            "(the gap _guard_no_route_through_section leaves by exempting those "
            "sections); a clean route grazes only their boundary ports and an "
            "own-line trunk overlay is exempt, so it stays silent on legitimate "
            "wraps and is the always-on detector for the #1078 backtrack."
        ),
    ),
    GuardSpec(
        _guard_feeder_exits_section_through_side,
        "B",
        needs=frozenset({"routes", "offsets"}),
        issue_pin=("#527",),
        narrow_reason=(
            "Checks only the source section's top/bottom edge crossing inside "
            "its label-grown x-range; a feeder must turn down outside the drawn "
            "side edge."
        ),
    ),
    GuardSpec(
        _guard_entry_approach_from_port_side,
        "B",
        needs=frozenset({"routes"}),
        issue_pin=("#484",),
        narrow_reason=(
            "Catches a route reaching its OWN target's far-edge entry port by "
            "slicing through the box interior; complements "
            "_guard_no_route_through_section, which exempts the target section."
        ),
    ),
    GuardSpec(
        _guard_no_opposing_line_overlap,
        "B",
        needs=frozenset({"offsets", "routes"}),
        issue_pin=("#885",),
        narrow_reason=(
            "General safety net for one line folding back over its own track; "
            "compared only within a single line_id so distinct lines sharing a "
            "channel are not flagged."
        ),
    ),
    GuardSpec(_guard_serpentine_no_backtrack, "A", needs=frozenset({"routes"})),
    GuardSpec(
        _guard_no_artefactual_counter_flow,
        "B",
        needs=frozenset({"routes"}),
        issue_pin=("#484",),
        narrow_reason=(
            "Fires only when the with-flow inter-row gap above the target was "
            "genuinely free for the run's X-span; topological counter-flow into "
            "LEFT/TOP/BOTTOM entries and gap-blocked dives are legitimate."
        ),
    ),
    GuardSpec(_guard_inter_row_run_clearance, "B", needs=frozenset({"routes"})),
    GuardSpec(
        _guard_trunk_bands_crossing_optimal,
        "B",
        needs=frozenset({"offsets", "routes"}),
    ),
    GuardSpec(
        _guard_inter_section_descent_edge_clearance,
        "B",
        needs=frozenset({"routes"}),
    ),
    GuardSpec(
        _guard_fan_bundles_coincide_or_separate,
        "B",
        needs=frozenset({"offsets", "routes"}),
    ),
)


# Classification-only registry (mirrors ``CHECK_REGISTRY``): these guards are
# invoked directly by ``engine.py`` at a specific pipeline stage rather than
# dispatched through the Pass C / final runner, so they carry no ``needs`` or
# ``bisection_safe`` dispatch data -- only their tier and any issue pin.  Every
# defined ``_guard_*`` lives in exactly one of the two registries
# (``test_every_guard_is_classified_in_exactly_one_registry``).
INLINE_GUARD_REGISTRY: tuple[GuardSpec, ...] = (
    GuardSpec(_guard_stations_within_bbox, "A"),
    GuardSpec(_guard_no_negative_grid_columns, "A"),
    GuardSpec(_guard_explicit_grid_directions, "A"),
    GuardSpec(_guard_no_mixed_entry_directions, "A"),
    GuardSpec(_guard_independent_components_disjoint, "A"),
    GuardSpec(_guard_multi_section_cell_packed, "A"),
    GuardSpec(_guard_no_same_row_backward_feed, "A"),
    GuardSpec(_guard_anchors_frozen_during_placement, "B"),
    GuardSpec(_guard_bypass_v_flat_visible, "B"),
    GuardSpec(_guard_centered_line_spread_balanced, "B"),
    GuardSpec(_guard_file_icon_no_name_label, "B"),
    GuardSpec(_guard_fold_relocated_flow_ports_face_connections, "B"),
    GuardSpec(_guard_interchange_bar_clears_non_members, "B"),
    GuardSpec(_guard_interchange_label_clears_connector, "B"),
    GuardSpec(_guard_no_diagonal_strikes_horizontal_label, "B"),
    GuardSpec(_guard_no_label_overlap, "B"),
    GuardSpec(_guard_no_line_crosses_file_icon, "B"),
    GuardSpec(_guard_no_line_strikes_label, "B"),
    GuardSpec(_guard_no_wrapped_label_trunk_strike, "B"),
    GuardSpec(
        _guard_off_track_consumer_on_trunk,
        "B",
        issue_pin=("#650",),
        narrow_reason=(
            "Restricted to consumers with exactly one on-track in-section "
            "successor, so a genuine on-track fork's off-row branches are not "
            "dragged onto the trunk."
        ),
    ),
    GuardSpec(
        _guard_symfan_entry_port_on_feeder_trunk,
        "B",
        issue_pin=("#1299",),
        narrow_reason=(
            "Restricted to a symmetric entry fork fed by exactly one same-row "
            "section's exit port, so a cross-row feed that legitimately wraps "
            "between rows is not required to align."
        ),
    ),
    GuardSpec(
        _guard_off_track_input_column_stack,
        "B",
        issue_pin=("#651",),
        narrow_reason=(
            "Restricted to single-trunk sections, whose lift pitch carries no "
            "stacked horizontal line bands that could legitimately bump an "
            "input past its same-column slot."
        ),
    ),
    GuardSpec(
        _guard_off_track_not_hub,
        "B",
        issue_pin=("#1295",),
        narrow_reason=(
            "Restricted to stations with edges on both sides: a genuine "
            "off-track input or producer-fed sink has edges on only one side, "
            "so it is never a false positive."
        ),
    ),
    GuardSpec(_guard_rail_above_label_band, "B"),
    GuardSpec(_guard_rail_one_station_per_column, "B"),
    GuardSpec(_guard_rail_stations_seat_on_rails, "B"),
    GuardSpec(
        _guard_single_trunk_off_track_step,
        "B",
        issue_pin=("#580",),
        narrow_reason=(
            "Restricted to single-trunk sections, which have no parallel tracks "
            "and so lift off-track stations by the base content pitch rather "
            "than the spread-widened y_spacing."
        ),
    ),
    GuardSpec(_guard_side_entered_vertical_top_not_below_feeder, "B"),
    GuardSpec(
        _guard_converge_siblings_merge_locally,
        "B",
        issue_pin=("#1296",),
        narrow_reason=(
            "Restricted to same-layer sibling groups of 2+ that merge more than "
            "_MAX_SIBLING_MERGE_SLACK columns downstream: a lone source or a "
            "one-column-late merge is a legitimate staircase or modest bulge, "
            "not the distant-terminus bow."
        ),
    ),
    GuardSpec(_guard_symmetric_diamond_branches_straddle_trunk, "B"),
    GuardSpec(_guard_symmetric_diamond_branches_half_pitch, "B"),
    GuardSpec(_guard_tall_anchor_stack_well_formed, "B"),
    GuardSpec(_guard_tb_top_entry_drop_hugs_top, "B"),
)


# Derived from the registry so the bisection thresholds stay single-sourced.
# Kept as a module attribute for the engine re-export and the threshold tests.
_BISECTION_FIRST_VALID: dict[str, str] = {
    spec.name: spec.first_valid_stage
    for spec in GUARD_REGISTRY
    if spec.bisection_safe and spec.first_valid_stage is not None
}


def _ensure_pass_c_inputs(
    graph: MetroGraph,
    offsets: dict[tuple[str, str], float] | None,
    routes: list[RoutedPath] | None,
) -> tuple[dict[tuple[str, str], float], list[RoutedPath] | None]:
    """Compute the shared ``offsets`` / ``routes`` a guard run inspects, once.

    ``route_edges`` is placement-pure: routing here to inspect the routes
    leaves ``graph.stations`` untouched, so the routes-consuming guards stay
    observational even running mid-pipeline between Pass C stages.  A routing
    failure leaves ``routes`` as ``None`` (it surfaces elsewhere); guards that
    need routes are then skipped.
    """
    from nf_metro.layout.routing import compute_station_offsets, route_edges

    if offsets is None:
        offsets = compute_station_offsets(graph)
    if routes is None:
        try:
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:  # noqa: BLE001 - routing failure surfaces elsewhere
            routes = None
    return offsets, routes


def run_validate_guards(
    graph: MetroGraph,
    phase: str,
    *,
    include_final: bool,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
    section_y_gap: float | None = None,
    section_y_padding: float | None = None,
) -> tuple[dict[tuple[str, str], float], list[RoutedPath] | None]:
    """Dispatch :data:`GUARD_REGISTRY` for one ``validate=True`` checkpoint.

    At a Pass C checkpoint (``include_final=False``) only the bisection-safe
    specs run, each gated by its ``first_valid_stage``.  At the closing
    ``after final`` boundary (``include_final=True``) the bisection-safe specs
    run first (none gated, since ``after final`` is past every threshold)
    followed by the final-only specs, reproducing the historical call order.
    A spec needing ``routes`` is skipped when routing failed.  Returns the
    shared ``(offsets, routes)``.
    """
    offsets, routes = _ensure_pass_c_inputs(graph, offsets, routes)
    available: dict[str, object] = {
        "offsets": offsets,
        "routes": routes,
        "section_y_gap": section_y_gap,
        "section_y_padding": section_y_padding,
    }
    for spec in GUARD_REGISTRY:
        if not spec.bisection_safe and not include_final:
            continue
        if spec.bisection_safe and not _bisection_should_run(spec.name, phase):
            continue
        if "routes" in spec.needs and routes is None:
            continue
        spec.fn(graph, phase, **{name: available[name] for name in spec.needs})
    return offsets, routes


# Tier-A guards that the render chokepoint does NOT run.  These two raise an
# *authoring* error (a ``ValueError`` the CLI surfaces as invalid input) on an
# un-renderable topology, not an observational ``PhaseInvariantError`` on the
# settled geometry, and the engine already runs them always-on at Stage 1.1.
# Warning-then-rendering an un-renderable map would be wrong, so they stay hard
# fails outside this warn-by-default chokepoint.
_RENDER_CHOKEPOINT_AUTHORING_GUARDS = frozenset(
    {"_guard_no_same_row_backward_feed", "_guard_no_mixed_entry_directions"}
)

# Every Tier-A spec from both registries except the authoring-error guards.
# Declaration order across ``GUARD_REGISTRY`` then ``INLINE_GUARD_REGISTRY``
# fixes the order violations are reported in.  Computed once over the immutable
# registries so the render chokepoint allocates nothing per render.
_RENDER_LAYOUT_INVARIANT_SPECS: tuple[GuardSpec, ...] = tuple(
    spec
    for spec in (*GUARD_REGISTRY, *INLINE_GUARD_REGISTRY)
    if spec.tier == "A" and spec.name not in _RENDER_CHOKEPOINT_AUTHORING_GUARDS
)


def render_layout_invariant_specs() -> tuple[GuardSpec, ...]:
    """The Tier-A guards :func:`assert_render_layout_invariants` runs."""
    return _RENDER_LAYOUT_INVARIANT_SPECS


def assert_render_layout_invariants(
    graph: MetroGraph,
    routes: list[RoutedPath],
    offsets: dict[tuple[str, str], float],
    *,
    strict: bool = False,
) -> None:
    """Run the cheap Tier-A layout guards on the final settled geometry.

    Sibling of :func:`assert_render_curve_invariants`: both run on the exact
    geometry the renderer is about to draw, so a layout defect is visible to
    end users instead of shipping a silently-broken SVG.  The render path
    already pays for ``offsets`` and ``routes``, and the guards are
    observational, so this is near-zero cost and cannot move a pixel.

    Each Tier-A guard raises ``PhaseInvariantError`` on its first violation
    rather than returning a list, so each runs in isolation and its message is
    captured; the aggregate is one message.  Without *strict* the aggregate is
    a :class:`UserWarning`; with *strict* it raises :class:`LayoutInvariantError`
    (modelled on the ``NF_METRO_ALLOW_BAD_CURVES`` chokepoint).
    """
    available: dict[str, Any] = {"offsets": offsets, "routes": routes}
    messages: list[str] = []
    for spec in render_layout_invariant_specs():
        try:
            spec.fn(graph, "render", **{name: available[name] for name in spec.needs})
        except PhaseInvariantError as exc:
            messages.append(f"[{spec.name}] {exc}")
    if not messages:
        return

    detail = "\n  ".join(messages)
    msg = (
        "the settled layout violates Tier-A invariants the renderer is about "
        "to draw. The map will render but is visibly broken; fix the layout "
        "(or the directive combination) that produced this geometry.\n  "
        f"{detail}"
    )
    if strict:
        raise LayoutInvariantError(msg)
    warnings.warn(msg, stacklevel=2)


def assert_render_row_gaps(
    graph: MetroGraph, section_y_gap: float, *, strict: bool = False
) -> None:
    """Run :func:`_guard_row_gaps` on the final render geometry.

    Runs after render-time label wrapping has grown section bboxes and the
    row reflow that follows -- the point where a shrunk row gap becomes
    visible.  The Tier-A ``assert_render_layout_invariants`` set cannot host
    this check: those guards run on the pre-wrap routed geometry (label growth
    legitimately moves a bbox edge past an invisible port, which the Tier-A
    port guards would flag), whereas ``_guard_row_gaps`` is a Tier-B guard
    whose invariant only holds at the final boundary.

    Warns by default; raises :class:`LayoutInvariantError` under *strict*,
    matching the sibling chokepoints.
    """
    try:
        _guard_row_gaps(graph, "render", section_y_gap=section_y_gap)
    except PhaseInvariantError as exc:
        msg = (
            "the settled layout violates a row-gap invariant the renderer is "
            "about to draw. The map will render but sections crowd; fix the "
            f"layout that produced this geometry.\n  [_guard_row_gaps] {exc}"
        )
        if strict:
            raise LayoutInvariantError(msg) from exc
        warnings.warn(msg, stacklevel=2)
