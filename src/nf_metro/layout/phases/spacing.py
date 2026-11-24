"""Global spacing widening search that clears residual label crowding.

Probes the renderer's own offset/route/label pipeline -- optionally with
terminus file icons fed in as label obstacles -- to measure residual overlaps
and strikes, widens the auto-resolved spacing axes to clear them, and warns
when a pinned pitch is too narrow to be cleared that way.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING

from nf_metro.layout.constants import DIAGONAL_SLOPE_RATIO, MIN_STATION_FLAT_LENGTH
from nf_metro.layout.geometry import lanes_run_along_x, lanes_run_along_y
from nf_metro.layout.phases._common import _restoring_layout_geometry
from nf_metro.parser.model import LayoutGeometryWarning, MetroGraph, is_bypass_v

if TYPE_CHECKING:
    from nf_metro.layout.labels import LabelOverlap, LabelPlacement
    from nf_metro.layout.routing.common import RoutedPath

# Cap on spread-loop passes.  Each pass strictly widens the binding axis,
# so a handful suffices to clear any realistic crowding before giving up.
_MAX_SPREAD_ITERS = 6

# Extra clearance (px) added on top of the measured intrusion when widening
# spacing, so the re-laid-out labels land with a small gap, not flush.
_SPREAD_SLACK = 4.0

_Probe = tuple[
    dict[tuple[str, str], float],
    "list[RoutedPath]",
    "list[LabelPlacement]",
]


def _icon_label_obstacles(
    graph: MetroGraph, offsets: dict[tuple[str, str], float]
) -> list[tuple[float, float, float, float]]:
    """Terminus file-icon boxes as the renderer feeds them to label placement."""
    from nf_metro.render.svg import _compute_icon_obstacles
    from nf_metro.themes import resolve_theme

    return _compute_icon_obstacles(graph, resolve_theme(None, graph), offsets)


def _probe_label_placements(
    graph: MetroGraph, *, icon_aware: bool = False
) -> _Probe | None:
    """Run the renderer's offset/route/label pipeline at the current layout.

    Returns the computed ``(station_offsets, routes, placements)`` so callers
    can inspect the settled geometry and labelling the renderer would draw, or
    ``None`` if routing/placement raises (a transient failure never blocks
    layout).  Snapshots and restores the in-place mutations route/place make.

    ``icon_aware`` feeds terminus file icons in as label obstacles, matching
    what the renderer does.  A label the renderer sides off an icon is then
    probed where it lands.  A caller measuring *where a label ends up* wants
    that; a caller measuring *how wide a label reaches* wants the unsided view,
    since the reach it must clear is the one the label has before an icon
    narrows its choices.

    The render-time wrapped-label trunk lift is held off here so the spacing
    search and the label-overlap guard reason about the unlifted geometry; the
    lift only moves a label closer to its own pill and never changes which
    labels overlap their neighbours in a way the search should react to.
    """
    from nf_metro.layout.labels import place_labels
    from nf_metro.layout.routing import compute_station_offsets, route_edges_centred

    with _restoring_layout_geometry(graph):
        try:
            offsets = compute_station_offsets(graph)
            routes = route_edges_centred(graph, station_offsets=offsets)
            placements = place_labels(
                graph,
                station_offsets=offsets,
                routes=routes,
                icon_obstacles=(
                    _icon_label_obstacles(graph, offsets) if icon_aware else None
                ),
                label_angle=graph.label_angle or 0.0,
                lift_wrapped_off_trunks=False,
            )
            return offsets, routes, placements
        except Exception:
            return None


def _unsided_placements(graph: MetroGraph) -> dict[str, LabelPlacement]:
    """Label placements keyed by station, placed with no icon in the way.

    The reach a label has before a terminus file icon narrows its choices, which
    is what a clearance lever measured in whole grid columns is sized against.
    Empty if the probe fails.
    """
    probe = _probe_label_placements(graph)
    if probe is None:
        return {}
    _offsets, _routes, placements = probe
    return {p.station_id: p for p in placements if p.station_id}


def _overlaps_from(
    graph: MetroGraph,
    offsets: dict[tuple[str, str], float],
    placements: list[LabelPlacement],
) -> list[LabelOverlap]:
    """Leftover label overlaps in a probed placement set.

    Overlaps involving a rail-section station are dropped: rail sections run a
    dedicated layout with their own column pitch, so widening the normal global
    X spacing cannot fix them and would only needlessly bloat the normal
    sections.
    """
    from nf_metro.layout.labels import find_label_overlaps

    overlaps = find_label_overlaps(graph, placements, offsets)
    if not graph.has_rail_sections:
        return overlaps

    def _in_rail(station_id: str) -> bool:
        st = graph.stations.get(station_id)
        return bool(st and st.section_id and graph.is_rail_section(st.section_id))

    return [o for o in overlaps if not (_in_rail(o.a) or _in_rail(o.b))]


def _struck_label_station_ids(
    graph: MetroGraph,
    offsets: dict[tuple[str, str], float],
    routes: list[RoutedPath],
    placements: list[LabelPlacement],
) -> set[str]:
    """Stations whose horizontal name label a diagonal route crosses.

    The visual goal: a fan-in/fan-out, convergence, or descent diagonal
    transitioning through a station's drawn name reads as a strike-through --
    whether or not the station carries that line.  A wide label sits in the path
    of its own line's sweep just as it sits in a foreign line's, so ownership is
    not an exemption; the flat run must lengthen until the transition clears the
    glyphs either way.

    Segments a longer flat run cannot relocate are excluded: flat (near-
    horizontal) trunk runs, off-track input sweeps, and angled labels (handled
    by their rotated footprint).  An off-track output sweep counts only against
    its own producer's label -- the off-track lead lever seats that divergence
    past the producer's name, but the sweep's crossing of any foreign label
    stays the router's job.  A bypass-V leg likewise counts only against its own
    diverging or merging station's label, since the per-column runway relocates
    that divergence but not the V's fixed-offset crossing of any other label
    (see ``relocatable_for`` below).

    ``placements`` must be the icon-sided view (``icon_aware=True``): a strike is
    judged on the glyphs the renderer draws, and a terminus file icon can side a
    name to the far side of its trunk, where a diagonal clear of the unsided
    placement rakes it.  An off-track output sweep is additionally tested against
    the unsided reach, which is what its clearance lever is sized against.
    """
    from nf_metro.layout.labels import segment_strikes_label
    from nf_metro.layout.routing.common import apply_route_offsets

    def _off_track(node_id: str) -> bool:
        st = graph.stations.get(node_id)
        return bool(st and st.off_track)

    def _bypass_endpoint(r: RoutedPath) -> str | None:
        """The real station a bypass-V leg diverges from or merges at, if any."""
        src_bypass = is_bypass_v(r.edge.source)
        tgt_bypass = is_bypass_v(r.edge.target)
        if src_bypass == tgt_bypass:
            return None
        return r.edge.target if src_bypass else r.edge.source

    # Each route carries which labels its diagonal can be made to clear by a
    # longer flat run: ``None`` -- never (off-track sweeps the off-track
    # machinery owns; nothing for the runway to relocate); ``ANY_STATION`` -- a
    # fan/convergence/descent diagonal that rakes whichever label it crosses; a
    # station id -- a bypass-V leg, which rakes only its own diverging/merging
    # station's label (its crossing of any other label sits at a fixed track
    # offset the per-column runway cannot relocate, left to the router's
    # flat-run seating).
    ANY_STATION = ""

    def _segment_applies(relocatable_for: str | None, station_id: str) -> bool:
        if relocatable_for is None:
            return False
        return relocatable_for in (ANY_STATION, station_id)

    seg_lists = []
    any_off_track_output = False
    for r in routes:
        pts = apply_route_offsets(r, offsets)
        is_off_output = _off_track(r.edge.target) and not _off_track(r.edge.source)
        if is_off_output:
            # Off-track output sweep: relocatable for its own producer's label
            # only (via the off-track lead lever), per the docstring.
            relocatable_for = r.edge.source
            any_off_track_output = True
        elif _off_track(r.edge.source) or _off_track(r.edge.target):
            relocatable_for = None
        else:
            relocatable_for = _bypass_endpoint(r) or ANY_STATION
        seg_lists.append((pts, relocatable_for, is_off_output))

    unsided = _unsided_placements(graph) if any_off_track_output else {}

    def _rakes(pts: list[tuple[float, float]], place: LabelPlacement) -> bool:
        return not place.angle and any(
            segment_strikes_label(x1, y1, x2, y2, place)
            and abs(y2 - y1) >= max(abs(x2 - x1), 1.0) * DIAGONAL_SLOPE_RATIO
            for (x1, y1), (x2, y2) in zip(pts, pts[1:])
        )

    struck: set[str] = set()
    for p in placements:
        station = graph.stations.get(p.station_id)
        if station is None or not station.label.strip() or p.angle:
            continue
        unsided_place = unsided.get(p.station_id)
        for pts, relocatable_for, is_off_output in seg_lists:
            if not _segment_applies(relocatable_for, p.station_id):
                continue
            reach = unsided_place if is_off_output else None
            if _rakes(pts, p) or (reach is not None and _rakes(pts, reach)):
                struck.add(p.station_id)
                break
    return struck


def _collinear_from(
    graph: MetroGraph,
    offsets: dict[tuple[str, str], float],
    routes: list[RoutedPath],
) -> bool:
    """Whether probed routes draw two distinct lines on top of each other.

    Runs the inter- and intra-section collinear-overlay checks the final-phase
    guards enforce, so the strike-clearance loop can step back a widening that
    would overshoot into a collinear defect rather than ship it.
    """
    from nf_metro.layout.routing.invariants import check_collinear_distinct_lines

    return bool(
        check_collinear_distinct_lines(
            graph, routes, offsets, scopes=("inter", "intra")
        )
    )


def _residual_label_overlaps(graph: MetroGraph) -> list[LabelOverlap]:
    """Place labels at the current layout and report leftover overlaps.

    What is reported is what wrapping alone cannot clear, which is exactly what
    the spread loop widens spacing for and what the final guard validates.
    Probes icon-aware, so an overlap that only exists once the renderer has
    sided a label off a terminus icon reaches the search that can widen it away.
    Returns an empty list if routing/placement raises.
    """
    probe = _probe_label_placements(graph, icon_aware=True)
    if probe is None:
        return []
    offsets, _routes, placements = probe
    return _overlaps_from(graph, offsets, placements)


def _reflowed_from(graph: MetroGraph, placements: list[LabelPlacement]) -> set[str]:
    """Stations in a probed placement set whose one-line name wrapping re-flowed.

    An author-written multi-line name is excluded -- those breaks are the
    author's, not the layout's, so no amount of pitch would undo them.
    """
    return {
        p.station_id
        for p in placements
        if (station := graph.stations.get(p.station_id)) is not None
        and "\n" not in station.label
        and "\n" in p.text
    }


def _reflowed_label_station_ids(graph: MetroGraph) -> set[str]:
    """Stations whose one-line name the wrap pass re-flowed onto several lines.

    Probes icon-aware, so the answer is the set the renderer draws: a terminus
    file icon narrows a label's choice of side and can be what forced the
    re-flow.  Returns an empty set if routing/placement raises.
    """
    probe = _probe_label_placements(graph, icon_aware=True)
    if probe is None:
        return set()
    _offsets, _routes, placements = probe
    return _reflowed_from(graph, placements)


def _reflowed_and_residual_overlaps(
    graph: MetroGraph,
) -> tuple[set[str], list[LabelOverlap]]:
    """One probe: the re-flowed label set and the leftover label overlaps.

    Lets a caller weighing a spacing offer judge both halves of it -- the size
    of the re-flowed set and the presence of leftover overlaps -- off a single
    route-and-place pass.  Probes icon-aware, matching
    :func:`_reflowed_label_station_ids` and :func:`_residual_label_overlaps`.

    Returns ``(set(), [])`` if routing/placement raises.
    """
    probe = _probe_label_placements(graph, icon_aware=True)
    if probe is None:
        return set(), []
    offsets, _routes, placements = probe
    return _reflowed_from(graph, placements), _overlaps_from(graph, offsets, placements)


def _struck_stations_and_collinear(graph: MetroGraph) -> tuple[set[str], bool]:
    """One probe: stations whose label a diagonal crosses, and a collinear flag.

    Returns ``(station_ids, has_collinear)`` where ``station_ids`` are stations
    whose horizontal label a diagonal route rakes, and ``has_collinear`` is
    whether the routes draw two distinct lines on top of each other.  Both read
    the same probed routes, so the strike-clearance loop decides growth and
    step-back from a single route+place pass.  Probes icon-aware, so a strike is
    judged on the placement the renderer draws.

    Returns ``(set(), False)`` on probe failure.  See
    :func:`_struck_label_station_ids` and :func:`_collinear_from`.
    """
    probe = _probe_label_placements(graph, icon_aware=True)
    if probe is None:
        return set(), False
    offsets, routes, placements = probe
    struck = _struck_label_station_ids(graph, offsets, routes, placements)
    return struck, _collinear_from(graph, offsets, routes)


def _bypass_v_flat_gaps_from(
    graph: MetroGraph, routes: list[RoutedPath]
) -> set[tuple[str, int]]:
    """Gap layers that would restore bypass-V flats collapsed below the minimum.

    A bypass V should present a visible horizontal run through its X like a
    regular fork/join station.  When the run *into* V (from the station it
    diverges from) is shorter than ``MIN_STATION_FLAT_LENGTH``, a gap before V's
    own layer pushes the bypassed node a grid column out; when the run *out of*
    V (to the merge target) is too short, a gap before the merge target's layer
    pushes that target out.  Returns ``(section_id, gap_layer)`` pairs.
    """
    gaps: set[tuple[str, int]] = set()
    for r in routes:
        into_v = is_bypass_v(r.edge.target)
        out_of_v = is_bypass_v(r.edge.source)
        if into_v == out_of_v:
            continue
        v = graph.stations.get(r.edge.target if into_v else r.edge.source)
        sec = graph.sections.get(v.section_id) if v and v.section_id else None
        if (
            v is None
            or sec is None
            or not lanes_run_along_y(sec.direction)
            or graph.is_rail_section(sec.id)
        ):
            continue
        flat = (
            abs(r.points[-1][0] - r.points[-2][0])
            if into_v
            else abs(r.points[1][0] - r.points[0][0])
        )
        if flat < MIN_STATION_FLAT_LENGTH - 0.5:
            gaps.add((sec.id, v.layer if into_v else v.layer + 1))
    return gaps


def _vertical_bypass_v_short_runouts(
    graph: MetroGraph, routes: list[RoutedPath]
) -> set[tuple[str, int]]:
    """Vertical-flow (TB/BT) bypass V's whose run-out flat is too short.

    A vertical bypass peels around the bypassed station diagonally, so only its
    run-out side carries a full flat; the peel-in side need only clear the corner
    curve.  The run is measured on the section's flow axis (Y).  Each incident
    route is attributed to its bypass-V endpoint -- the segment leaving a V to
    its source endpoint, the segment landing at a V to its target endpoint -- so
    a V chained to a sibling V (the connecting route carries two V's) feeds the
    run on each side.  A V is flagged when its longer (run-out) side is shorter
    than ``MIN_STATION_FLAT_LENGTH``, leaving it on a bare curve apex.  Unlike
    the horizontal :func:`_bypass_v_flat_gaps_from` gaps, these are not restored
    by the grid-column strike-clearance loop (the exit-corridor gap owns the
    run-out flat), so this feeds the runtime guard only.  Returns
    ``(section_id, layer)``.
    """
    run_out: dict[str, float] = {}
    sec_of: dict[str, str] = {}

    def _note(vid: str, flat: float) -> None:
        v = graph.stations.get(vid)
        sec = graph.sections.get(v.section_id) if v and v.section_id else None
        if (
            v is None
            or sec is None
            or not lanes_run_along_x(sec.direction)
            or graph.is_rail_section(sec.id)
        ):
            return
        run_out[vid] = max(run_out.get(vid, 0.0), flat)
        sec_of[vid] = sec.id

    for r in routes:
        if is_bypass_v(r.edge.source):
            _note(r.edge.source, abs(r.points[1][1] - r.points[0][1]))
        if is_bypass_v(r.edge.target):
            _note(r.edge.target, abs(r.points[-1][1] - r.points[-2][1]))
    return {
        (sec_of[vid], graph.stations[vid].layer)
        for vid, run in run_out.items()
        if run < MIN_STATION_FLAT_LENGTH - 0.5
    }


def _bypass_v_collapsed_flat_gaps(graph: MetroGraph) -> set[tuple[str, int]]:
    """Standalone bypass-V flat probe for the runtime guard.

    Free (no route+place) for a graph with no bypass V.  Otherwise routes
    through :func:`_probe_label_placements`, which restores the in-place
    geometry mutations, so this never perturbs the live layout.  Covers both the
    horizontal grid-column gaps (:func:`_bypass_v_flat_gaps_from`) and the
    vertical-flow short run-outs (:func:`_vertical_bypass_v_short_runouts`).
    """
    if not any(is_bypass_v(e.source) or is_bypass_v(e.target) for e in graph.edges):
        return set()
    probe = _probe_label_placements(graph)
    if probe is None:
        return set()
    _offsets, routes, _placements = probe
    return _bypass_v_flat_gaps_from(graph, routes) | _vertical_bypass_v_short_runouts(
        graph, routes
    )


def _label_clearance_issues(
    graph: MetroGraph,
) -> tuple[set[str], bool, set[tuple[str, int]]]:
    """One probe: struck labels, the collinear flag, and collapsed bypass-V gaps.

    The strike-clearance loop reads all three from a single route+place pass, so
    a step's grow/step-back decision never pays for a redundant probe.  Probes
    icon-aware, so a strike is judged on the placement the renderer draws.  See
    :func:`_struck_label_station_ids`, :func:`_collinear_from`, and
    :func:`_bypass_v_flat_gaps_from`.
    """
    probe = _probe_label_placements(graph, icon_aware=True)
    if probe is None:
        return set(), False, set()
    offsets, routes, placements = probe
    struck = _struck_label_station_ids(graph, offsets, routes, placements)
    return (
        struck,
        _collinear_from(graph, offsets, routes),
        _bypass_v_flat_gaps_from(graph, routes),
    )


def _placed_name_label_station_ids(graph: MetroGraph) -> set[str]:
    """Return station ids that ``place_labels`` emits a name label for.

    Returns an empty set if routing/placement raises.
    """
    probe = _probe_label_placements(graph)
    if probe is None:
        return set()
    _offsets, _routes, placements = probe
    return {p.station_id for p in placements if p.station_id}


def _spread_bump(
    graph: MetroGraph,
    residual: list[LabelOverlap],
    x_spacing: float,
    y_spacing: float,
    auto_x: bool,
    auto_y: bool,
) -> tuple[float, float]:
    """Compute widened (x, y) spacing to clear the residual label overlaps.

    Each overlap is attributed to the axis along which its two stations are
    separated (columns -> x, rows -> y).  The required extra pitch is the
    intrusion depth shared across the columns/rows between them, plus slack.
    Only auto-resolved axes are widened; a pinned axis is left untouched.
    """
    extra_x = 0.0
    extra_y = 0.0
    for ov in residual:
        separation = _overlap_separation(graph, ov)
        if separation is None:
            continue
        dx, dy = separation
        if dx >= dy:
            cols = max(round(dx / x_spacing), 1)
            extra_x = max(extra_x, (ov.ox + _SPREAD_SLACK) / cols)
        else:
            rows = max(round(dy / y_spacing), 1)
            extra_y = max(extra_y, (ov.oy + _SPREAD_SLACK) / rows)
    new_x = x_spacing + extra_x if auto_x else x_spacing
    new_y = y_spacing + extra_y if auto_y else y_spacing
    return new_x, new_y


def _overlap_separation(
    graph: MetroGraph, ov: LabelOverlap
) -> tuple[float, float] | None:
    """``(dx, dy)`` between an overlap's two stations, or None if either is gone."""
    a = graph.stations.get(ov.a)
    b = graph.stations.get(ov.b)
    if a is None or b is None:
        return None
    return abs(a.x - b.x), abs(a.y - b.y)


def _warn_if_pinned_x_spacing_crowds_labels(
    graph: MetroGraph, x_spacing: float, y_spacing: float
) -> None:
    """Warn when a pinned column pitch ships overlapping station labels.

    :func:`_spread_bump` widens only the auto-resolved axes, so a pitch the
    caller pinned below what the content needs leaves labels colliding with
    nothing to say so.  This is the X-axis counterpart of ``compute_layout``'s
    explicit-``y_spacing`` warning: the render is honoured at the requested
    pitch and the collision is reported rather than hidden.

    Only overlaps separated along X are reported, by the same attribution rule
    the widening uses -- a pair stacked in rows is not the column pitch's fault.
    """
    residual = [
        ov
        for ov in _residual_label_overlaps(graph)
        if (sep := _overlap_separation(graph, ov)) is not None and sep[0] >= sep[1]
    ]
    if not residual:
        return
    worst = max(residual, key=lambda ov: ov.ox)
    clearing_x, _ = _spread_bump(graph, residual, x_spacing, y_spacing, True, False)
    struck = "label" if worst.kind == "label" else "marker"
    warnings.warn(
        f"explicit x_spacing={x_spacing!r} is too narrow for this graph's "
        f"content: station label {worst.a!r} overlaps {struck} {worst.b!r} by "
        f"({worst.ox:.1f}, {worst.oy:.1f})px in the render "
        f"({len(residual)} pair(s) total); about {clearing_x:.1f} would clear "
        f"them. Omit --x-spacing to let the engine pick a safe value.",
        LayoutGeometryWarning,
        stacklevel=2,
    )


def _bypass_label_obstacles(
    graph: MetroGraph,
) -> dict[str, tuple[float, float, float, float]]:
    """Glyph-ink bbox of each bypassed station's label, keyed by its bypass V.

    Probes the renderer's settled labelling and, for every hidden bypass-V
    helper station, records the drawn glyph-ink box of the station the V routes
    around.  The router consumes this to seat the V's flat-run corners clear of
    that label.  Boxes are in rendered (offset-applied) coordinates and only
    cover Vs whose bypassed station carries a drawn name; the result is empty
    when no bypass V exists or the probe fails.

    The probe routes against no obstacle boxes, so the boxes depend on the
    layout alone.  Seating a V against a box can move a label, so a probe that
    read the current boxes could flip between two answers on recompute.
    """
    from nf_metro.layout.labels import label_glyph_ink_bbox

    vs = [
        st
        for st in graph.stations.values()
        if st.is_hidden and st.bypasses_station_id is not None
    ]
    if not vs:
        return {}
    seated = graph.bypass_label_obstacles
    graph.bypass_label_obstacles = {}
    try:
        probe = _probe_label_placements(graph)
    finally:
        graph.bypass_label_obstacles = seated
    if probe is None:
        return {}
    _offsets, _routes, placements = probe
    ink_by_sid = {
        p.station_id: label_glyph_ink_bbox(p)
        for p in placements
        if p.station_id and not p.angle
    }
    obstacles: dict[str, tuple[float, float, float, float]] = {}
    for st in vs:
        sid = st.bypasses_station_id
        box = ink_by_sid.get(sid) if sid is not None else None
        if box is not None:
            obstacles[st.id] = box
    return obstacles
