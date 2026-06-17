"""Stage-boundary invariant guards run by ``compute_layout(validate=True)``."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, NamedTuple

from nf_metro.layout.constants import (
    COMPONENT_BAND_OVERLAP_TOLERANCE,
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    CURVE_RADIUS,
    EDGE_TO_BUNDLE_CLEARANCE,
    GUARD_TOLERANCE,
    ICON_HALF_HEIGHT,
    INTER_ROW_EDGE_CLEARANCE,
    OFFSET_STEP,
    ROW_BAND_SLACK,
    SAME_COORD_TOLERANCE,
    SECTION_Y_GAP,
    STATION_RADIUS_APPROX,
    X_SPACING,
)
from nf_metro.layout.geometry import BBoxXIndex, segment_intersects_bbox
from nf_metro.layout.phases._common import (
    _bbox_cols_overlap,
    _canvas_width,
    _restoring_layout_geometry,
    _route_crosses_section_boundary,
    _section_bundle_lines,
    _station_marker_bbox,
    first_vertical_leg_sign,
    first_vertical_leg_x,
    is_loop_side_branch_station,
    routes_through_unrelated_sections,
)
from nf_metro.layout.phases.bbox import _section_fit_top
from nf_metro.layout.phases.off_track import (
    _is_single_trunk_lr_section,
    _off_track_anchor_of,
    _off_track_lift_step,
    _off_track_output_below,
)
from nf_metro.layout.phases.single_section import _terminus_y_overhang
from nf_metro.layout.phases.spacing import (
    _placed_name_label_station_ids,
    _residual_label_overlaps,
)
from nf_metro.parser.model import LineSpread, MetroGraph, PortSide, Section, Station

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from typing import Protocol

    from nf_metro.layout.routing.common import RoutedPath

    class _HasMessage(Protocol):
        def message(self) -> str: ...


class PhaseInvariantError(Exception):
    """Raised when a layout phase produces invalid intermediate state."""


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
                f"Give the section a flow-aligned entry/exit port "
                f"(left/right for LR/RL, top/bottom for TB/BT) or change "
                f"its '%%metro direction:'."
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
    edge into negative columns (issue #256), which renders the section's
    badge left of everything and snakes the inter-section trunk down the
    left margin. ``infer_section_layout`` normalizes the grid so the
    leftmost column is 0; this guard fails loudly if that ever regresses.
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


def _guard_explicit_grid_directions(graph: MetroGraph, phase: str) -> None:
    """Explicit-grid sections keep the LR default unless they carry an
    explicit %%metro direction.

    A section's grid position is the author's manual layout intent, not a
    flow-direction signal (issue #446). Direction inference therefore skips
    explicit-grid sections; this guard fails loudly if a future change ever
    lets inference reorient one (e.g. by reading override-aware positions),
    which would silently elongate serpentine-stacked maps vertically.
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
) -> None:
    """Final phase: each section's bbox top must clear its highest marker.

    The mirror of the bottom-padding contract.  After
    :func:`_fit_bboxes_to_content_top` runs, every section's bbox top
    should sit at its content-anchored target (a full ``section_y_padding``
    above the highest marker, unless gap-bounded by the row above).  A
    bbox top below that target means a later pass crowded the topmost
    marker against the box edge (issue #406).
    """
    tol = 1.0
    for section in graph.sections.values():
        if section.bbox_h <= 0:
            continue
        target = _section_fit_top(graph, section, section_y_padding, section_y_gap)
        if target is None:
            continue
        if section.bbox_y > target + tol:
            raise PhaseInvariantError(
                f"{phase}: section {section.id!r} bbox top {section.bbox_y:.1f} "
                f"sits below its content-anchored target {target:.1f} "
                f"(highest marker crowds the bbox top edge)"
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
    from nf_metro.layout.rail_mode import (
        _rail_above_label_stations,
        _rail_label_band,
    )

    tol = 1.0
    for section in graph.sections.values():
        if section.bbox_h <= 0 or not graph.is_rail_section(section.id):
            continue
        per_line = graph._rail_y.get(section.id) or {}
        if not per_line:
            continue
        real_ids = [
            sid
            for sid in section.station_ids
            if (st := graph.stations.get(sid)) is not None and not st.is_port
        ]
        above_ids = _rail_above_label_stations(graph, real_ids, per_line)
        if not above_ids:
            continue
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
        if section.bbox_h <= 0 or section.direction not in ("TB", "BT"):
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
        if section is None or not _is_single_trunk_lr_section(
            graph, section, junction_ids
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
    no stacked horizontal line bands that could legitimately bump an input past
    its slot.
    """
    from nf_metro.layout.engine import compute_min_y_spacing

    junction_ids = graph.junction_ids
    y_spacing = compute_min_y_spacing(graph)
    anchor_of = _off_track_anchor_of(graph)
    tol = 1.0

    col_group: dict[tuple[str | None, float, str], int] = defaultdict(int)
    for off_id, anchor_id in anchor_of.items():
        st = graph.stations.get(off_id)
        if st is not None:
            col_group[(st.section_id, round(st.x, 1), anchor_id)] += 1

    for off_id, anchor_id in anchor_of.items():
        off_st = graph.stations.get(off_id)
        anchor = graph.stations.get(anchor_id)
        if off_st is None or anchor is None:
            continue
        if not any(e.target == anchor_id for e in graph.edges_from(off_id)):
            continue  # producer-fed sink, not an input
        section = graph.sections.get(off_st.section_id or "")
        if section is None or not _is_single_trunk_lr_section(
            graph, section, junction_ids
        ):
            continue
        step = _off_track_lift_step(graph, section, junction_ids, y_spacing)
        n = col_group[(off_st.section_id, round(off_st.x, 1), anchor_id)]
        gap = anchor.y - off_st.y
        if gap > n * step + tol:
            raise PhaseInvariantError(
                f"{phase}: off-track input {off_id!r} sits {gap:.1f}px "
                f"({gap / step:.1f} slots) above consumer {anchor_id!r} on "
                f"single-trunk section {section.id!r}, but only {n} off-track "
                f"station(s) share its column and anchor -- it is stranded above "
                f"an empty row (expected at most {n * step:.1f}px)"
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
            if (tgt := graph.stations.get(e.target)) is not None
            and not tgt.is_port
            and tgt.id not in junction_ids
            and not tgt.off_track
            and tgt.section_id == cons.section_id
        ]
        distinct = {s.id for s in succs}
        if len(distinct) != 1:
            continue
        succ = succs[0]
        if abs(cons.y - succ.y) > tol:
            raise PhaseInvariantError(
                f"{phase}: off-track consumer {cons_id!r} y={cons.y:.1f} "
                f"dragged off the section trunk; its continuation "
                f"{succ.id!r} sits at y={succ.y:.1f} "
                f"({abs(cons.y - succ.y):.0f}px climb)"
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
    from nf_metro.render.svg import apply_route_offsets

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
    from nf_metro.render.svg import _icon_obstacles_by_station, apply_route_offsets
    from nf_metro.themes import THEMES

    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)
    if routes is None:
        from nf_metro.layout.routing import route_edges

        # route_edges' diagonal-centring pass mutates Station.x in place;
        # a guard must stay observational, so snapshot and restore X
        # around the call (mirrors _run_pass_c_guards).
        saved_x = {sid: s.x for sid, s in graph.stations.items()}
        try:
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:  # noqa: BLE001 - routing failure surfaces elsewhere
            return
        finally:
            for sid, x in saved_x.items():
                graph.stations[sid].x = x

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
                    raise PhaseInvariantError(
                        f"{phase}: line {line_id!r} on edge {src!r} -> {tgt!r} "
                        f"crosses file icon of {sid!r} "
                        f"bbox ({bbox[0]:.1f},{bbox[1]:.1f})-"
                        f"({bbox[2]:.1f},{bbox[3]:.1f}); segment "
                        f"({p1[0]:.1f},{p1[1]:.1f})->({p2[0]:.1f},{p2[1]:.1f})"
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
    from nf_metro.render.svg import _compute_icon_obstacles, apply_route_offsets
    from nf_metro.themes import THEMES

    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)

    with _restoring_layout_geometry(graph):
        if routes is None:
            from nf_metro.layout.routing import route_edges

            try:
                routes = route_edges(graph, station_offsets=offsets)
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
    """Final-phase: every bypass V keeps a visible horizontal run through its X.

    A bypass V whose diverging run pins to the station marker rakes that
    station's label; one whose run collapses sits at the curve apex instead of
    on a flat like a regular station.  The strike-clearance loop pushes the
    bypassed node (or the merge target) a grid column out until both runs reach
    ``MIN_STATION_FLAT_LENGTH``; this is the backstop for a residual the loop
    could not relocate.
    """
    from nf_metro.layout.phases.spacing import _bypass_v_collapsed_flat_gaps

    collapsed = _bypass_v_collapsed_flat_gaps(graph)
    if collapsed:
        detail = ", ".join(
            f"section {sid!r} layer {layer}" for sid, layer in sorted(collapsed)
        )
        raise PhaseInvariantError(
            f"{phase}: bypass-V flat run collapsed below the minimum visible "
            f"length; a grid-column gap is unplaced at {detail}"
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
            from nf_metro.layout.routing import route_edges

            try:
                routes = route_edges(graph, station_offsets=offsets)
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
    from nf_metro.render.svg import apply_route_offsets

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
            or sec.direction not in ("LR", "RL")
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


def _ensure_routes(
    graph: MetroGraph, routes: list[RoutedPath] | None
) -> list[RoutedPath]:
    """Return *routes*, routing all edges first if the caller didn't supply them."""
    if routes is not None:
        return routes
    from nf_metro.layout.routing import route_edges

    return route_edges(graph)


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
        port = graph.ports.get(rp.edge.target)
        if port is None or not port.is_entry:
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
                    (rp, rp.edge.target, "approaches RIGHT entry from inside box")
                )
            elif port.side == PortSide.LEFT and prev[0] > bx0 + tol:
                offenders.append(
                    (rp, rp.edge.target, "approaches LEFT entry from inside box")
                )
        elif port.side in (PortSide.TOP, PortSide.BOTTOM):
            if abs(prev[0] - end[0]) > tol:
                continue
            if port.side == PortSide.BOTTOM and prev[1] < by1 - tol:
                offenders.append(
                    (rp, rp.edge.target, "approaches BOTTOM entry from inside box")
                )
            elif port.side == PortSide.TOP and prev[1] > by0 + tol:
                offenders.append(
                    (rp, rp.edge.target, "approaches TOP entry from inside box")
                )
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
        gy = _center_inter_row_channel(gap_top, gap_bottom)
        exclude = {sid for sid in (src_sec.id, tgt_sec.id) if sid is not None}
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            if abs(y2 - y1) > tol or abs(x2 - x1) <= tol:
                continue  # horizontal runs only
            if y1 < tgt_top - tol:
                continue  # run sits above the target row's top -> with-flow band
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
            if abs(x1 - x0) > tol:
                continue  # vertical segments only
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


def _guard_bundle_order_preserved(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """Final-phase: at every shared-xy corner where 2 or more bundled
    lines meet, the lines' relative left/right ordering must be
    preserved between incoming and outgoing tangents.

    See ``src/nf_metro/layout/routing/invariants.py`` for the
    semantic definition.  The guard is a thin wrapper: it routes the
    edges (if not provided), invokes
    :func:`check_bundle_order_preserved`, and raises
    :class:`PhaseInvariantError` with the first violation's
    self-describing message (the full violation list is summarised in
    the count).

    The check operates on the final ``route_edges`` output, so it can
    only run at the final guard block where the routing is stable.
    """
    from nf_metro.layout.routing.invariants import check_bundle_order_preserved

    if routes is None:
        from nf_metro.layout.routing import compute_station_offsets, route_edges

        if offsets is None:
            offsets = compute_station_offsets(graph)
        try:
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:  # noqa: BLE001 - routing failure surfaces elsewhere
            return

    violations = check_bundle_order_preserved(routes)
    if not violations:
        return
    first = violations[0]
    extra = f" (+{len(violations) - 1} more)" if len(violations) > 1 else ""
    raise PhaseInvariantError(f"{phase}: {first.message()}{extra}")


def _guard_no_collinear_distinct_lines(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """Final-phase: no two distinct lines may render on top of each other.

    Wraps :func:`check_no_collinear_distinct_lines`: a bundling/offset
    defect that collapses two co-travelling lines onto one channel makes
    one stroke obscure the other.  Operates on the final, offset-applied
    inter-section geometry.
    """
    from nf_metro.layout.routing.invariants import (
        check_no_collinear_distinct_lines,
    )

    _raise_on_first_violation(
        graph, phase, check_no_collinear_distinct_lines, offsets, routes
    )


def _guard_no_intra_section_collinear_distinct_lines(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """Final-phase: no two distinct lines may render on top within a section.

    Wraps :func:`check_intra_section_collinear_distinct_lines`, the
    intra-section counterpart to :func:`_guard_no_collinear_distinct_lines`.
    A bundling/offset defect that collapses two co-travelling lines onto one
    channel *inside* a section hides one line behind the other.
    """
    from nf_metro.layout.routing.invariants import (
        check_intra_section_collinear_distinct_lines,
    )

    _raise_on_first_violation(
        graph, phase, check_intra_section_collinear_distinct_lines, offsets, routes
    )


def _guard_no_same_line_parallel_descents(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """Final-phase: one line never descends as two parallel adjacent tracks.

    Wraps :func:`check_no_same_line_parallel_descents`: where a line fans out
    from one source (or converges on one port), the branches must share a
    single trunk over the span they travel together rather than occupying
    adjacent offset slots that render as two same-colour tracks.
    """
    from nf_metro.layout.routing.invariants import (
        check_no_same_line_parallel_descents,
    )

    _raise_on_first_violation(
        graph, phase, check_no_same_line_parallel_descents, offsets, routes
    )


def _guard_concentric_bundle_corners(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> None:
    """Final-phase: wholesale-translated bundle corners share an arc centre.

    Wraps :func:`check_concentric_bundle_corners`, the correctness check the
    corner-radius source ratchet (``test_corner_radius_ratchet``) cannot do:
    a radius can trace to an approved helper yet nest non-concentrically when
    the caller hand-picks the wrong sign.  A bundle turning a 90-degree corner
    as a unit must keep its arcs concentric or it pinches through the bend.
    """
    from nf_metro.layout.routing.invariants import (
        check_concentric_bundle_corners,
    )

    _raise_on_first_violation(
        graph, phase, check_concentric_bundle_corners, offsets, routes
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


def _guard_off_track_clear_of_anchor(graph: MetroGraph, phase: str) -> None:
    """At final: every off-track station must sit at least ``GUARD_TOLERANCE``
    clear of its anchor on the expected side.

    An input's anchor is its consumer, a producer-fed sink's anchor is its
    producer; :func:`_off_track_anchor_of` resolves which, so the same Y
    relationship is enforced for both roles.  Outputs are normally lifted
    above (smaller Y) their producer, but an output whose producer sits on a
    downward branch is dropped below it (:func:`_off_track_output_below`); such
    outputs are required to sit below (larger Y) so a downward-branch output
    does not cross back over the trunk.
    """
    below = _off_track_output_below(graph)
    for off_id, anchor_id in _off_track_anchor_of(graph).items():
        off_st = graph.stations.get(off_id)
        anchor_st = graph.stations.get(anchor_id)
        if off_st is None or anchor_st is None:
            continue
        if off_id in below:
            if not (off_st.y > anchor_st.y + GUARD_TOLERANCE):
                raise PhaseInvariantError(
                    f"{phase}: downward off-track output {off_id!r} "
                    f"y={off_st.y:.1f} not below anchor {anchor_id!r} "
                    f"y={anchor_st.y:.1f}"
                )
        elif not (off_st.y < anchor_st.y - GUARD_TOLERANCE):
            raise PhaseInvariantError(
                f"{phase}: off-track {off_id!r} y={off_st.y:.1f} "
                f"not above anchor {anchor_id!r} y={anchor_st.y:.1f}"
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
            if (src := graph.stations.get(e.source)) and src.is_port
        }
        entry_succs = {
            e.target
            for e in graph.edges_from(jid)
            if (tgt := graph.stations.get(e.target)) and tgt.is_port
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
                src = graph.stations.get(edge.source)
                if src is not None and not src.is_port:
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
                feeder = graph.stations.get(edge.source)
                if feeder is None:
                    continue
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

# Each entry: bisection-runnable guard -> first checkpoint at which its
# invariant must hold.  Before that checkpoint, the guard is skipped in
# bisection mode; the final guard block (phase ``"after Stage 5.1
# (final)"``, which is not in ``_PASS_C_BISECTION_ORDER``) always runs it.
#
# - stations_in_sections: Stage 5.2 lifts off-track stations above their
#   section's pre-grow bbox top; Stage 5.3's row top-align grows the
#   bbox upward to enclose them.
# - no_station_overlap: pre-snap fan placement can sit a fraction of a
#   pitch off the row grid; Stage 6.4's snap pulls every station onto the
#   grid and keeps same-column stations on distinct slots, after which
#   markers must be collision-free.
# - no_line_crosses_non_consumer: a sparse loop-side station (single
#   line in, single line out, full-bundle row-mates) sits on the trunk
#   Y until Stage 6.14 shifts it to a half-grid offset; before that,
#   the sibling line bundle's route passes through its marker bbox.
_BISECTION_FIRST_VALID: dict[str, str] = {
    "_guard_stations_in_sections": "after Stage 5.3",
    "_guard_no_station_overlap": "after Stage 6.4",
    "_guard_no_coincident_station_coords": "after Stage 6.4",
    "_guard_no_line_crosses_non_consumer": "after Stage 6.14",
}


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


def _guard_routing_preserves_non_x(
    graph: MetroGraph,
    phase: str,
    saved_xy: dict[str, tuple[float, float]],
    saved_bbox: dict[str, tuple[float, float, float, float]],
) -> None:
    """Raise if a guard's ``route_edges`` call mutated anything but ``Station.x``.

    ``_run_pass_c_guards`` snapshots and restores only ``Station.x`` because
    that is the sole graph state ``route_edges`` mutates.  If a future routing
    change starts moving ``Station.y`` or a section bbox, the X-only restore
    would leak that mutation into later Pass C stages, silently making
    ``validate=True`` non-idempotent again (#518).  This fails loudly instead.
    """
    for sid, station in graph.stations.items():
        if abs(station.y - saved_xy[sid][1]) > 1e-6:
            raise PhaseInvariantError(
                f"{phase}: guard's route_edges call moved station {sid!r} in Y "
                f"({saved_xy[sid][1]:.3f} -> {station.y:.3f}); the guard snapshot "
                "restores X only. Extend the snapshot/restore in "
                "_run_pass_c_guards to keep the validate flag observational."
            )
    for sid, sec in graph.sections.items():
        now = (sec.bbox_x, sec.bbox_y, sec.bbox_w, sec.bbox_h)
        if now != saved_bbox[sid]:
            raise PhaseInvariantError(
                f"{phase}: guard's route_edges call mutated section {sid!r} bbox "
                f"({saved_bbox[sid]} -> {now}); the guard snapshot restores "
                "station X only. Extend the snapshot/restore in "
                "_run_pass_c_guards to keep the validate flag observational."
            )


def _run_pass_c_guards(
    graph: MetroGraph,
    phase: str,
    *,
    offsets: dict[tuple[str, str], float] | None = None,
    routes: list[RoutedPath] | None = None,
) -> tuple[dict[tuple[str, str], float], list[RoutedPath] | None]:
    """Bisection guards run after every Pass C sub-phase boundary in
    ``validate=True`` mode.

    The Pass C tidy-up pipeline is a sequence of ~20 mutating passes
    over a shared graph; before this helper, ``validate=True`` only
    sampled the final state, so a regression introduced at e.g.
    Stage 6.7 surfaced as ``after final: ...`` with no way to
    bisect.  Running the same overlap / breeze-past / column-drift
    checks at each boundary localises the culprit to a single phase.

    Guards transient through specific Pass C sub-phases are gated
    by ``_BISECTION_FIRST_VALID`` and skipped before they're valid.
    See that table for the per-guard transient windows.

    Always excluded from the bisection set (only meaningful at the
    final boundary):

    * ``_guard_off_track_clear_of_anchor`` -- Stage 6.4's snap-to-grid
      shifts the on-track anchor (consumer or producer) Y by up to half
      a pitch before Stage 6.6 re-anchors the off-track station.
    * ``_guard_row_trunk_cy_consistent`` -- the row trunk Y is only
      finalised once Stage 6.7 has re-centred ``center_ports`` graphs.
    * ``_guard_inter_section_routes_in_row_band`` -- row-band height
      tolerance assumes final bboxes, which Stages 6.13 / 6.14 may still be
      shrinking.

    The final guard block (``after final``) composes this
    helper with the three excluded guards above, sharing
    ``offsets``/``routes`` for a single computation per checkpoint.
    Returns the computed ``(offsets, routes)`` so callers (e.g. the
    final block) can pass them on to the remaining guards.
    """
    from nf_metro.layout.routing import compute_station_offsets, route_edges

    if offsets is None:
        offsets = compute_station_offsets(graph)
    if routes is None:
        # route_edges' diagonal-centring pass mutates Station.x in place.
        # That is intended on the final render path (validate=False never
        # calls this), but a guard must be observational: running it
        # mid-pipeline would change the input to later Pass C stages.
        # Snapshot and restore station X around the call.  The snapshot
        # covers only X because route_edges touches no other graph state;
        # _guard_routing_preserves_non_x verifies that contract still holds.
        saved_xy = {sid: (s.x, s.y) for sid, s in graph.stations.items()}
        saved_bbox = {
            sid: (sec.bbox_x, sec.bbox_y, sec.bbox_w, sec.bbox_h)
            for sid, sec in graph.sections.items()
        }
        try:
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:  # noqa: BLE001 - routing failure surfaces elsewhere
            routes = None
        finally:
            _guard_routing_preserves_non_x(graph, phase, saved_xy, saved_bbox)
            for sid, (x, _) in saved_xy.items():
                graph.stations[sid].x = x

    _guard_coordinates_finite(graph, phase)
    _guard_section_bboxes_positive(graph, phase)
    if _bisection_should_run("_guard_stations_in_sections", phase):
        _guard_stations_in_sections(graph, phase)
    _guard_ports_on_boundaries(graph, phase)
    if _bisection_should_run("_guard_no_station_overlap", phase):
        _guard_no_station_overlap(graph, phase, offsets=offsets)
    if _bisection_should_run("_guard_no_coincident_station_coords", phase):
        _guard_no_coincident_station_coords(graph, phase)
    if routes is not None and _bisection_should_run(
        "_guard_no_line_crosses_non_consumer", phase
    ):
        _guard_no_line_crosses_non_consumer(
            graph, phase, offsets=offsets, routes=routes
        )
    _guard_station_x_column_drift(graph, phase)
    return offsets, routes


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
