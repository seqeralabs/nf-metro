"""Label placement for station names.

Uses horizontal labels (like the reference nf-core metro maps) with
above/below alternation and collision avoidance.
"""

from __future__ import annotations

__all__ = ["LabelPlacement", "label_text_width", "place_labels"]

from dataclasses import dataclass
from typing import TYPE_CHECKING

from nf_metro.layout.constants import (
    CHAR_WIDTH,
    COLLISION_MULTIPLIER,
    DESCENDER_CLEARANCE,
    FONT_HEIGHT,
    LABEL_BBOX_MARGIN,
    LABEL_LINE_HEIGHT,
    LABEL_MARGIN,
    LABEL_NUDGE_MAX,
    LABEL_OFFSET,
    PORT_LABEL_MAX_DX,
    TB_LABEL_H_SPACING,
    TB_LINE_Y_OFFSET,
    TB_PILL_EDGE_OFFSET,
)
from nf_metro.layout.geometry import segment_intersects_bbox
from nf_metro.parser.model import MetroGraph

if TYPE_CHECKING:
    from nf_metro.layout.routing.common import RoutedPath


def label_text_width(label: str) -> float:
    """Pixel width of the widest line in a (possibly multi-line) label."""
    if "\n" not in label:
        return len(label) * CHAR_WIDTH
    return max(len(line) for line in label.split("\n")) * CHAR_WIDTH


def _label_text_height(label: str) -> float:
    """Pixel height of a (possibly multi-line) label."""
    n = label.count("\n") + 1
    if n == 1:
        return FONT_HEIGHT
    return FONT_HEIGHT + (n - 1) * FONT_HEIGHT * LABEL_LINE_HEIGHT


@dataclass
class LabelPlacement:
    """Placement information for a station label."""

    station_id: str
    text: str
    x: float
    y: float
    above: bool
    angle: float = 0.0  # Horizontal by default
    text_anchor: str = "middle"
    dominant_baseline: str = ""  # Empty means use above/below logic
    obstacle_bbox: tuple[float, float, float, float] | None = None


def _label_bbox(
    placement: LabelPlacement,
) -> tuple[float, float, float, float]:
    """Return (x_min, y_min, x_max, y_max) bounding box for a label."""
    if placement.obstacle_bbox is not None:
        return placement.obstacle_bbox
    w = label_text_width(placement.text)
    text_h = _label_text_height(placement.text)

    # Horizontal bounds depend on text_anchor
    if placement.text_anchor == "end":
        x_min = placement.x - w
        x_max = placement.x
    elif placement.text_anchor == "start":
        x_min = placement.x
        x_max = placement.x + w
    else:  # "middle" (default)
        x_min = placement.x - w / 2
        x_max = placement.x + w / 2

    # Vertical bounds: "central" baseline centers text at y
    if placement.dominant_baseline == "central":
        y_min = placement.y - text_h / 2
        y_max = placement.y + text_h / 2
    elif placement.above:
        y_min = placement.y - text_h
        y_max = placement.y
    else:
        y_min = placement.y
        y_max = placement.y + text_h

    return (x_min, y_min, x_max, y_max)


def _boxes_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    margin: float = LABEL_MARGIN,
) -> bool:
    """Check if two bounding boxes overlap."""
    return not (
        a[2] + margin < b[0]
        or b[2] + margin < a[0]
        or a[3] + margin < b[1]
        or b[3] + margin < a[1]
    )


def _nudge_to_clear(
    candidate: LabelPlacement,
    existing: list[LabelPlacement],
    max_nudge: float = LABEL_NUDGE_MAX,
) -> LabelPlacement | None:
    """Try a small horizontal shift to clear collisions on the preferred side.

    When a label collides with already-placed labels, a tiny X nudge can
    often resolve it without flipping above/below (which breaks
    alternation).  Returns a nudged copy if successful, None otherwise.
    """
    cbox = _label_bbox(candidate)

    # Accumulate the minimum shift needed in each direction.
    need_right = 0.0  # shift right to clear left-side collisions
    need_left = 0.0  # shift left to clear right-side collisions

    for placed in existing:
        pbox = _label_bbox(placed)
        if not _boxes_overlap(cbox, pbox):
            continue
        if placed.x <= candidate.x:
            # Collider is to the left: need to shift our left edge rightward.
            gap = pbox[2] + LABEL_MARGIN - cbox[0]
            if gap > 0:
                need_right = max(need_right, gap)
        else:
            # Collider is to the right: need to shift our right edge leftward.
            gap = cbox[2] + LABEL_MARGIN - pbox[0]
            if gap > 0:
                need_left = max(need_left, gap)

    # If squeezed from both sides, nudging can't help.
    if need_right > 0 and need_left > 0:
        return None

    # Add a tiny epsilon so the shifted edge clears the strict-less-than
    # overlap check rather than landing exactly on the boundary.
    _EPS = 0.1
    shift = need_right - need_left
    if shift > 0:
        shift += _EPS
    elif shift < 0:
        shift -= _EPS
    if abs(shift) > max_nudge:
        return None

    nudged = LabelPlacement(
        station_id=candidate.station_id,
        text=candidate.text,
        x=candidate.x + shift,
        y=candidate.y,
        above=candidate.above,
    )
    if _has_collision(nudged, existing):
        return None
    return nudged


def _edge_solo(
    stations: list,
    section_y_range: dict[str, tuple[float, float]],
) -> dict[str, tuple[bool, bool]]:
    """Determine whether each section's Y extremes have a sole station.

    Returns a dict mapping section_id -> (lo_solo, hi_solo).  The edge
    station override (prefer outward labels) should only apply when the
    extreme Y has a single station; otherwise it kills alternation on
    crowded tracks.
    """
    from collections import Counter

    result: dict[str, tuple[bool, bool]] = {}
    sec_ys: dict[str, list[float]] = {}
    for s in stations:
        if s.section_id and s.section_id in section_y_range:
            sec_ys.setdefault(s.section_id, []).append(s.y)

    for sec_id, ys in sec_ys.items():
        y_lo, y_hi = section_y_range[sec_id]
        counts = Counter(ys)
        result[sec_id] = (counts.get(y_lo, 0) == 1, counts.get(y_hi, 0) == 1)

    return result


def _compute_port_label_preference(
    graph: MetroGraph,
    max_dx: float = 0.0,
) -> dict[str, bool]:
    """Determine preferred label side for stations connected to nearby off-Y ports.

    When a station connects to a port at a different Y **and** the port is
    horizontally close, the diagonal route between them occupies the space
    on that side.  The label should go on the opposite side to avoid
    overlapping the route.

    *max_dx* is the maximum horizontal distance between station and port
    for the override to apply.  Stations far from their port have enough
    horizontal room for the diagonal to clear the label.

    Returns a dict mapping station_id -> preferred_above (True = above).
    Only stations with a clear preference are included.
    """
    result: dict[str, bool] = {}
    for edge in graph.edges:
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if not src or not tgt:
            continue

        # Only consider station -> exit port edges.  Exit routes depart
        # from the station toward the section boundary and can pass
        # through the label area.  Entry routes arrive via L-shaped
        # inter-section routing and rarely conflict with labels.
        if not (tgt.is_port and not src.is_port):
            continue
        port_info = graph.ports.get(tgt.id)
        if not port_info or port_info.is_entry:
            continue

        station, port = src, tgt
        dy = port.y - station.y
        if abs(dy) < 1:
            continue

        # Only override when the port is close enough that the
        # diagonal route would visually clash with the label.
        if max_dx > 0 and abs(port.x - station.x) > max_dx:
            continue

        # Port is below station -> route goes down -> prefer label above
        # Port is above station -> route goes up -> prefer label below
        preferred_above = dy > 0
        if station.id in result and result[station.id] != preferred_above:
            # Conflicting ports on both sides; no preference
            del result[station.id]
        else:
            result[station.id] = preferred_above

    return result


def _apply_edge_override(
    station,
    start_above: bool,
    section_y_range: dict[str, tuple[float, float]],
    sections_with_multiline: set[str],
    edge_solo: dict[str, tuple[bool, bool]],
) -> bool:
    """Apply the edge-station outward-label override when appropriate."""
    if (
        not station.section_id
        or station.section_id in sections_with_multiline
        or station.section_id not in section_y_range
    ):
        return start_above

    y_lo, y_hi = section_y_range[station.section_id]
    lo_solo, hi_solo = edge_solo.get(station.section_id, (True, True))
    if y_lo < y_hi:
        if station.y == y_lo and lo_solo:
            return True
        if station.y == y_hi and hi_solo:
            return False
    return start_above


def _make_obstacle_placements(
    obstacles: list[tuple[float, float, float, float]] | None,
) -> list[LabelPlacement]:
    """Create phantom LabelPlacement entries for obstacle bounding boxes.

    These participate in collision detection but are filtered out before
    the final placement list is returned.
    """
    if not obstacles:
        return []
    return [
        LabelPlacement(
            station_id=f"__obstacle_{i}",
            text="",
            x=(bbox[0] + bbox[2]) / 2,
            y=bbox[1],
            above=False,
            obstacle_bbox=bbox,
        )
        for i, bbox in enumerate(obstacles)
    ]


def _trial_cost(
    stations: list,
    graph: MetroGraph,
    label_offset: float,
    station_offsets: dict[tuple[str, str], float] | None,
    section_y_range: dict[str, tuple[float, float]],
    sections_with_multiline: set[str],
    flip: bool,
    icon_obstacles: list[tuple[float, float, float, float]] | None = None,
    port_pref: dict[str, bool] | None = None,
) -> float:
    """Count label collision cost for a section using the given alternation.

    Returns a score where lower is better: each collision-resolution flip
    costs 1, each push (still colliding after flip) costs 2.  A small
    fractional penalty is added for labels that face inward (toward the
    section's Y center, i.e. into the route-line bundle), weighted by
    label width so the longest labels dominate the tiebreaker.
    """
    solo = _edge_solo(stations, section_y_range)

    # Compute section Y midpoint for inward-facing penalty
    sec_id = stations[0].section_id if stations else None
    if sec_id and sec_id in section_y_range:
        y_lo, y_hi = section_y_range[sec_id]
        y_mid = (y_lo + y_hi) / 2
    else:
        y_mid = None

    placements: list[LabelPlacement] = _make_obstacle_placements(icon_obstacles)
    cost: float = 0
    for station in stations:
        if station_offsets:
            line_offs = [
                station_offsets.get((station.id, lid), 0.0)
                for lid in graph.station_lines(station.id)
            ]
            min_off = min(line_offs) if line_offs else 0.0
            max_off = max(line_offs) if line_offs else 0.0
        else:
            min_off = max_off = 0.0

        start_above = station.layer % 2 == 1
        if flip:
            start_above = not start_above

        start_above = _apply_edge_override(
            station,
            start_above,
            section_y_range,
            sections_with_multiline,
            solo,
        )

        if port_pref and station.id in port_pref:
            start_above = port_pref[station.id]

        candidate = _try_place(
            station, label_offset, start_above, placements, min_off, max_off
        )

        if _has_collision(candidate, placements):
            # Try a small horizontal nudge before flipping sides.
            nudged = _nudge_to_clear(candidate, placements)
            if nudged is not None:
                cost += 0.1  # small penalty for nudge (better than flip)
                candidate = nudged
            else:
                cost += 1
                candidate = _try_place(
                    station,
                    label_offset,
                    not start_above,
                    placements,
                    min_off,
                    max_off,
                )
                if _has_collision(candidate, placements):
                    cost += 2
                    direction = -1 if not start_above else 1
                    if direction < 0:
                        y = station.y + min_off - label_offset * COLLISION_MULTIPLIER
                    else:
                        y = station.y + max_off + label_offset * COLLISION_MULTIPLIER
                    candidate = LabelPlacement(
                        station_id=station.id,
                        text=station.label,
                        x=station.x,
                        y=y,
                        above=(direction < 0),
                    )

        # Small tiebreaker: penalize labels that face inward (toward
        # the section Y center, i.e. into the route-line bundle),
        # weighted by label width so longer labels drive the decision.
        if y_mid is not None and y_lo < y_hi:  # type: ignore[possibly-undefined]
            if not candidate.above and station.y < y_mid:
                cost += label_text_width(station.label) * 0.001
            elif candidate.above and station.y > y_mid:
                cost += label_text_width(station.label) * 0.001

        placements.append(candidate)

    return cost


def place_labels(
    graph: MetroGraph,
    label_offset: float = LABEL_OFFSET,
    station_offsets: dict[tuple[str, str], float] | None = None,
    icon_obstacles: list[tuple[float, float, float, float]] | None = None,
    routes: list["RoutedPath"] | None = None,
) -> list[LabelPlacement]:
    """Place horizontal labels alternating above/below stations.

    Strategy:
    1. Default: alternate above/below based on layer index.
    2. If it collides with an existing label, try the other side.
    3. If still colliding, push further away.

    Per-section trial: for each LR/RL section with multiple stations,
    both alternation patterns are tested and the one with fewer
    collisions is used.
    """
    sorted_stations = sorted(
        (
            s
            for s in graph.stations.values()
            if not s.is_port and not s.is_hidden and s.label.strip()
        ),
        key=lambda s: (s.layer, s.track),
    )

    # Pre-compute per-section Y extremes for LR/RL sections so edge
    # stations prefer outward-facing labels, centering visual content.
    # Include port station Y positions so routes entering/exiting at a
    # different Y than the station track inform the outward preference
    # (prevents labels facing into an entry/exit route).
    # Skip sections that contain multi-line labels: consistent layer
    # alternation avoids cascading collisions between the taller labels.
    section_y_range: dict[str, tuple[float, float]] = {}
    sections_with_multiline: set[str] = set()
    for s in sorted_stations:
        if not s.section_id:
            continue
        if "\n" in s.label:
            sections_with_multiline.add(s.section_id)
        sec = graph.sections.get(s.section_id)
        if not sec or sec.direction not in ("LR", "RL"):
            continue
        if s.section_id not in section_y_range:
            section_y_range[s.section_id] = (s.y, s.y)
        else:
            lo, hi = section_y_range[s.section_id]
            section_y_range[s.section_id] = (min(lo, s.y), max(hi, s.y))

    # Extend section Y ranges with port station positions so single-track
    # sections with off-track ports get outward-facing label preference.
    for s in graph.stations.values():
        if not s.is_port or not s.section_id:
            continue
        if s.section_id not in section_y_range:
            continue
        lo, hi = section_y_range[s.section_id]
        section_y_range[s.section_id] = (min(lo, s.y), max(hi, s.y))

    # Pre-compute which section edges have a sole station, so the
    # outward-label override only fires when it won't kill alternation.
    solo = _edge_solo(sorted_stations, section_y_range)

    # Pre-compute label side preference for stations connected to
    # off-Y ports, so labels avoid overlapping diagonal port routes.
    port_pref = _compute_port_label_preference(graph, max_dx=PORT_LABEL_MAX_DX)

    # Trial both alternation patterns per section, pick the better one.
    section_flip: dict[str, bool] = {}
    sec_groups: dict[str, list] = {}
    for s in sorted_stations:
        if s.section_id and s.section_id in section_y_range:
            sec_groups.setdefault(s.section_id, []).append(s)
    for sec_id, sec_stations in sec_groups.items():
        if len(sec_stations) < 2:
            continue
        args = (
            sec_stations,
            graph,
            label_offset,
            station_offsets,
            section_y_range,
            sections_with_multiline,
        )
        cost_default = _trial_cost(
            *args, flip=False, icon_obstacles=icon_obstacles, port_pref=port_pref
        )
        cost_flipped = _trial_cost(
            *args, flip=True, icon_obstacles=icon_obstacles, port_pref=port_pref
        )
        if cost_flipped < cost_default:
            section_flip[sec_id] = True

    # Pre-compute per-station safe label offsets so labels between
    # vertically stacked stations stay closer to their own pill.
    safe_offsets = _compute_safe_offsets(
        sorted_stations, label_offset, station_offsets, graph
    )

    placements: list[LabelPlacement] = _make_obstacle_placements(icon_obstacles)

    for i, station in enumerate(sorted_stations):
        # Compute the vertical extent of the station pill so labels
        # are offset from the pill edge, not from station.y.
        if station_offsets:
            line_offs = [
                station_offsets.get((station.id, lid), 0.0)
                for lid in graph.station_lines(station.id)
            ]
            min_off = min(line_offs) if line_offs else 0.0
            max_off = max(line_offs) if line_offs else 0.0
        else:
            min_off = max_off = 0.0

        # Check if this is a TB section station (horizontal pill)
        is_tb_vert = False
        if station.section_id:
            sec = graph.sections.get(station.section_id)
            if sec and sec.direction == "TB":
                is_tb_vert = True

        if is_tb_vert:
            # Place label to the left of the horizontal pill
            n_lines = len(graph.station_lines(station.id))
            offset_span = (n_lines - 1) * TB_LINE_Y_OFFSET
            pill_left = station.x - offset_span / 2 - TB_PILL_EDGE_OFFSET
            pill_right = station.x + offset_span / 2 + TB_PILL_EDGE_OFFSET
            candidate = LabelPlacement(
                station_id=station.id,
                text=station.label,
                x=pill_left - TB_LABEL_H_SPACING,
                y=station.y,
                above=True,
                text_anchor="end",
                dominant_baseline="central",
            )
            if _has_collision(candidate, placements):
                # Try right side of the pill
                candidate = LabelPlacement(
                    station_id=station.id,
                    text=station.label,
                    x=pill_right + TB_LABEL_H_SPACING,
                    y=station.y,
                    above=True,
                    text_anchor="start",
                    dominant_baseline="central",
                )
            # Expand section bbox to contain the label
            if station.section_id:
                tb_sec = graph.sections.get(station.section_id)
                if tb_sec and tb_sec.bbox_w > 0:
                    margin = LABEL_BBOX_MARGIN
                    lx_min, _, lx_max, _ = _label_bbox(candidate)
                    lx_min -= margin
                    lx_max += margin
                    if lx_min < tb_sec.bbox_x:
                        old_right = tb_sec.bbox_x + tb_sec.bbox_w
                        tb_sec.bbox_x = lx_min
                        tb_sec.bbox_w = old_right - lx_min
                    if lx_max > tb_sec.bbox_x + tb_sec.bbox_w:
                        tb_sec.bbox_w = lx_max - tb_sec.bbox_x
            placements.append(candidate)
            continue

        # Alternate by layer (column): even layers below, odd layers above
        start_above = station.layer % 2 == 1
        if station.section_id and section_flip.get(station.section_id, False):
            start_above = not start_above

        # For isolated edge stations in LR/RL sections, prefer labels
        # extending outward (away from center).  Only applies when the
        # station is the sole occupant at the section's Y extreme;
        # crowded edges keep alternation to avoid horizontal collisions.
        start_above = _apply_edge_override(
            station,
            start_above,
            section_y_range,
            sections_with_multiline,
            solo,
        )

        # Override when a port route would clash with the label.
        if station.id in port_pref:
            start_above = port_pref[station.id]

        safe_above, safe_below = safe_offsets.get(
            station.id, (label_offset, label_offset)
        )
        eff_offset = safe_above if start_above else safe_below

        candidate = _try_place(
            station, eff_offset, start_above, placements, min_off, max_off
        )

        if _has_collision(candidate, placements):
            # Try a small horizontal nudge before flipping sides.
            nudged = _nudge_to_clear(candidate, placements)
            if nudged is not None:
                candidate = nudged
            else:
                # Try the other side
                alt_offset = safe_below if start_above else safe_above
                candidate = _try_place(
                    station,
                    alt_offset,
                    not start_above,
                    placements,
                    min_off,
                    max_off,
                )

                if _has_collision(candidate, placements):
                    # Push further in the non-default direction
                    direction = -1 if not start_above else 1
                    if direction < 0:
                        y = station.y + min_off - safe_above * COLLISION_MULTIPLIER
                    else:
                        y = station.y + max_off + safe_below * COLLISION_MULTIPLIER
                    candidate = LabelPlacement(
                        station_id=station.id,
                        text=station.label,
                        x=station.x,
                        y=y,
                        above=(direction < 0),
                    )

        # Final obstacle clearance: if the label still overlaps an
        # obstacle (e.g. a terminus file icon), try flipping to the
        # opposite side of the station first (keeps label close).
        # Only push past the obstacle as a last resort.
        if _has_collision(candidate, placements):
            cbox = _label_bbox(candidate)
            for p in placements:
                if p.obstacle_bbox is None:
                    continue
                obox = _label_bbox(p)
                if not _boxes_overlap(cbox, obox):
                    continue

                # Try flipping to the opposite side of the station.
                flip_above = not candidate.above
                flip_off = safe_above if flip_above else safe_below
                flipped = _try_place(
                    station,
                    flip_off,
                    flip_above,
                    placements,
                    min_off,
                    max_off,
                )
                if not _has_collision(flipped, placements):
                    candidate = flipped
                    break

                # Flipping also collides.  Pick whichever side keeps the
                # label closer to the station.  When both sides have
                # obstacles (e.g. tight grid between two terminus
                # icons), prefer closeness over clearance - a slight
                # overlap is better than pushing the label into an
                # adjacent row.
                flip_dist = abs(flipped.y - station.y)
                orig_dist = abs(candidate.y - station.y)
                if flip_dist < orig_dist:
                    candidate = flipped
                break

        # Clamp labels so they stay within section bbox
        if station.section_id:
            sec = graph.sections.get(station.section_id)
            if sec and sec.bbox_w > 0:
                text_half_w = label_text_width(candidate.text) / 2
                margin = LABEL_BBOX_MARGIN
                # Horizontal: expand section bbox if needed, keeping
                # the label centered on its station.
                label_left = candidate.x - text_half_w - margin
                label_right = candidate.x + text_half_w + margin
                if label_left < sec.bbox_x:
                    old_right = sec.bbox_x + sec.bbox_w
                    sec.bbox_x = label_left
                    sec.bbox_w = old_right - label_left
                if label_right > sec.bbox_x + sec.bbox_w:
                    sec.bbox_w = label_right - sec.bbox_x
                # Vertical clamping (with flip/expand on overlap)
                candidate = _clamp_label_vertical(
                    candidate,
                    sec,
                    station,
                    label_offset,
                    min_off,
                    max_off,
                    margin,
                    placements,
                )

        placements.append(candidate)

    if routes:
        _avoid_diagonal_routes(placements, graph, routes, station_offsets)

    return [p for p in placements if p.obstacle_bbox is None]


def _avoid_diagonal_routes(
    placements: list[LabelPlacement],
    graph: MetroGraph,
    routes: list["RoutedPath"],
    station_offsets: dict[tuple[str, str], float] | None,
) -> None:
    """Flip labels overlapping a non-horizontal route segment.

    Trunk segments are horizontal by design so labels offset above or
    below them clear naturally.  Diagonal segments (trunk-to-fan
    transitions, off-track entries) can land on top of a label and need
    explicit avoidance: flip the label to the opposite side of its
    station when the flipped position is free of label and route
    collisions; otherwise leave it.
    """
    diag: list[tuple[float, float, float, float]] = []
    for route in routes:
        pts = list(route.points)
        if station_offsets and not route.offsets_applied and pts:
            so = station_offsets.get((route.edge.source, route.line_id), 0.0)
            to = station_offsets.get((route.edge.target, route.line_id), 0.0)
            pts[0] = (pts[0][0], pts[0][1] + so)
            pts[-1] = (pts[-1][0], pts[-1][1] + to)
        for i in range(len(pts) - 1):
            (x1, y1), (x2, y2) = pts[i], pts[i + 1]
            dx, dy = x2 - x1, y2 - y1
            if abs(dy) < max(abs(dx), 1.0) * 0.05:
                continue  # ~horizontal; not a label obstacle.
            diag.append((x1, y1, x2, y2))
    if not diag:
        return

    m = LABEL_MARGIN

    def hits(box: tuple[float, float, float, float]) -> bool:
        padded = (box[0] - m, box[1] - m, box[2] + m, box[3] + m)
        return any(segment_intersects_bbox(*s, padded) for s in diag)

    for placement in [p for p in placements if p.obstacle_bbox is None]:
        if placement.dominant_baseline or placement.angle:
            continue
        if not hits(_label_bbox(placement)):
            continue
        station = graph.stations.get(placement.station_id)
        if station is None:
            continue
        if station_offsets:
            offs = [
                station_offsets.get((station.id, lid), 0.0)
                for lid in graph.station_lines(station.id)
            ]
            min_off, max_off = (min(offs), max(offs)) if offs else (0.0, 0.0)
        else:
            min_off = max_off = 0.0
        if placement.above:
            gap = max((station.y + min_off) - placement.y, LABEL_OFFSET)
            trial = LabelPlacement(
                station_id=placement.station_id,
                text=placement.text,
                x=placement.x,
                y=station.y + max_off + gap,
                above=False,
            )
        else:
            gap = max(placement.y - (station.y + max_off), LABEL_OFFSET)
            trial = LabelPlacement(
                station_id=placement.station_id,
                text=placement.text,
                x=placement.x,
                y=station.y + min_off - gap,
                above=True,
            )
        siblings = [p for p in placements if p is not placement]
        if _has_collision(trial, siblings) or hits(_label_bbox(trial)):
            continue
        placement.x, placement.y, placement.above = trial.x, trial.y, trial.above


def _clamp_label_vertical(
    candidate: LabelPlacement,
    sec,
    station,
    label_offset: float,
    min_off: float,
    max_off: float,
    margin: float,
    existing: list[LabelPlacement] | None = None,
) -> LabelPlacement:
    """Clamp label vertically within section bbox.

    If clamping would push the label into the station pill, flip it to the
    opposite side (provided the flipped position doesn't collide with an
    existing label).  If both sides would overlap, expand the section bbox
    so the label fits at its ideal position.
    """
    pill_top = station.y + min_off
    pill_bottom = station.y + max_off
    sec_top = sec.bbox_y
    sec_bottom = sec.bbox_y + sec.bbox_h

    text_h = _label_text_height(candidate.text)

    if candidate.above:
        # Label text occupies [candidate.y - text_h, candidate.y].
        min_y = sec_top + text_h + margin
        if candidate.y >= min_y:
            return candidate  # fits without clamping

        overshoot = min_y - candidate.y

        # Clamping needed - would the clamped position overlap the pill?
        if min_y <= pill_top - label_offset:
            # Still enough gap after clamping
            candidate.y = min_y
            return candidate

        # Small overshoot: prefer expanding the bbox over flipping, so
        # the label keeps its intended above/below side (preserving
        # alternation).
        if overshoot <= margin:
            sec.bbox_y -= overshoot + margin
            sec.bbox_h += overshoot + margin
            return candidate

        # Clamped position too close to pill - try flipping to below
        below_y = pill_bottom + label_offset
        max_y = sec_bottom - text_h - margin
        if below_y <= max_y:
            flipped = LabelPlacement(
                station_id=candidate.station_id,
                text=candidate.text,
                x=candidate.x,
                y=below_y,
                above=False,
            )
            if existing is None or not _has_collision(flipped, existing):
                candidate.y = below_y
                candidate.above = False
                return candidate

        # Neither side fits (or flip collides) - expand bbox upward
        expand = overshoot + margin
        sec.bbox_y -= expand
        sec.bbox_h += expand
        return candidate

    else:
        # Label text occupies [candidate.y, candidate.y + text_h].
        max_y = sec_bottom - text_h - margin
        if candidate.y <= max_y:
            return candidate  # fits without clamping

        overshoot = candidate.y - max_y

        # Clamping needed - would the clamped position overlap the pill?
        if max_y >= pill_bottom + label_offset:
            # Still enough gap after clamping
            candidate.y = max_y
            return candidate

        # Small overshoot: prefer expanding the bbox over flipping, so
        # the label keeps its intended above/below side (preserving
        # alternation).
        if overshoot <= margin:
            sec.bbox_h += overshoot + margin
            return candidate

        # Clamped position too close to pill - try flipping to above
        above_y = pill_top - label_offset
        min_y = sec_top + text_h + margin
        if above_y >= min_y:
            flipped = LabelPlacement(
                station_id=candidate.station_id,
                text=candidate.text,
                x=candidate.x,
                y=above_y,
                above=True,
            )
            if existing is None or not _has_collision(flipped, existing):
                candidate.y = above_y
                candidate.above = True
                return candidate

        # Neither side fits (or flip collides) - expand bbox downward
        expand = overshoot + margin
        sec.bbox_h += expand
        return candidate


def _compute_safe_offsets(
    sorted_stations: list,
    label_offset: float,
    station_offsets: dict[tuple[str, str], float] | None,
    graph: MetroGraph,
) -> dict[str, tuple[float, float]]:
    """Compute per-station safe label offsets (above, below).

    For vertically stacked stations at the same X, the label offset is
    reduced so a label stays closer to its own pill than to the
    neighboring pill.  We use 40% of the available gap (instead of 50%)
    so the label is visibly biased toward its own station.

    Returns a dict mapping station_id -> (safe_above, safe_below).
    """
    # Group stations by (section_id, rounded X) to find vertical neighbors.
    col_groups: dict[tuple[str | None, float], list] = {}
    for s in sorted_stations:
        key = (s.section_id, round(s.x, 1))
        col_groups.setdefault(key, []).append(s)

    result: dict[str, tuple[float, float]] = {}

    for _key, group in col_groups.items():
        if len(group) < 2:
            for s in group:
                result[s.id] = (label_offset, label_offset)
            continue

        group.sort(key=lambda s: s.y)

        for idx, s in enumerate(group):
            if station_offsets:
                offs = [
                    station_offsets.get((s.id, lid), 0.0)
                    for lid in graph.station_lines(s.id)
                ]
                s_min = min(offs) if offs else 0.0
                s_max = max(offs) if offs else 0.0
            else:
                s_min = s_max = 0.0

            text_h = _label_text_height(s.label)
            safe_above = label_offset
            safe_below = label_offset

            # Check neighbor above
            if idx > 0:
                nb = group[idx - 1]
                if station_offsets:
                    nb_offs = [
                        station_offsets.get((nb.id, lid), 0.0)
                        for lid in graph.station_lines(nb.id)
                    ]
                    nb_max = max(nb_offs) if nb_offs else 0.0
                else:
                    nb_max = 0.0
                pill_gap = (s.y + s_min) - (nb.y + nb_max)
                if pill_gap > 0:
                    safe_above = min(label_offset, (pill_gap - text_h) * 0.4)
                    safe_above = max(safe_above, 2.0)  # floor

            # Check neighbor below
            if idx < len(group) - 1:
                nb = group[idx + 1]
                if station_offsets:
                    nb_offs = [
                        station_offsets.get((nb.id, lid), 0.0)
                        for lid in graph.station_lines(nb.id)
                    ]
                    nb_min = min(nb_offs) if nb_offs else 0.0
                else:
                    nb_min = 0.0
                pill_gap = (nb.y + nb_min) - (s.y + s_max)
                if pill_gap > 0:
                    safe_below = min(label_offset, (pill_gap - text_h) * 0.4)
                    safe_below = max(safe_below, 2.0)  # floor

            result[s.id] = (safe_above, safe_below)

    return result


def _try_place(
    station,
    label_offset: float,
    above: bool,
    existing: list[LabelPlacement],
    min_off: float = 0.0,
    max_off: float = 0.0,
) -> LabelPlacement:
    """Create a label placement above or below a station.

    Offsets are measured from the pill edge: above labels use min_off
    (top of the pill) and below labels use max_off (bottom of the pill).

    Above labels include DESCENDER_CLEARANCE so that letter descenders
    (g, p, y ...) don't visually touch the pill.  The SVG renderer
    uses ``dominant-baseline: auto`` which places the alphabetic
    baseline at label.y -- descenders extend below that point.
    """
    if above:
        return LabelPlacement(
            station_id=station.id,
            text=station.label,
            x=station.x,
            y=station.y + min_off - label_offset - DESCENDER_CLEARANCE,
            above=True,
        )
    else:
        return LabelPlacement(
            station_id=station.id,
            text=station.label,
            x=station.x,
            y=station.y + max_off + label_offset,
            above=False,
        )


def _has_collision(
    candidate: LabelPlacement,
    existing: list[LabelPlacement],
) -> bool:
    """Check if a candidate label collides with any existing placement."""
    cbox = _label_bbox(candidate)
    for placed in existing:
        if _boxes_overlap(cbox, _label_bbox(placed)):
            return True
    return False
