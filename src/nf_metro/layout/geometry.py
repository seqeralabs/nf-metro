"""Low-level geometric primitives shared by layout passes and validation guards."""

from __future__ import annotations

import bisect
import math
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from nf_metro.layout.routing.common import RoutedPath
    from nf_metro.parser.model import MetroGraph, Section, Station

_Box = tuple[float, float, float, float]


def quantize_coord(value: float, ndigits: int) -> float:
    """Round *value* to *ndigits* decimal places for use as a grouping key.

    Snaps a float coordinate to a stable dict/set key so layout passes can
    group stations sharing a column, row, or trunk coordinate despite float
    drift from arithmetic (averaging, offset accumulation).
    """
    return round(value, ndigits)


def shift_section(
    graph: MetroGraph, section: Section, *, dx: float = 0.0, dy: float = 0.0
) -> None:
    """Rigidly translate a section's stations, ports and bbox by ``(dx, dy)``.

    Internal geometry (port-to-station gaps, runways) is preserved; only the
    bbox origin moves, not its size.
    """
    for sid in section.station_ids:
        station = graph.stations.get(sid)
        if station is not None:
            station.x += dx
            station.y += dy
        port = graph.ports.get(sid)
        if port is not None:
            port.x += dx
            port.y += dy
    section.bbox_x += dx
    section.bbox_y += dy


def iter_section_overlaps(
    graph: MetroGraph, tolerance: float = -1.0
) -> Iterator[tuple[str, str, _Box, _Box]]:
    """Yield ``(sid_a, sid_b, box_a, box_b)`` for every overlapping section pair.

    Each box is ``(x1, y1, x2, y2)``.  A small negative ``tolerance`` lets flush
    (touching) boxes pass but flags any genuine overlap; a positive tolerance
    would require a gap.  Zero-area sections are skipped.  Shared by the runtime
    guard and the offline validator so the two cannot drift.
    """
    boxed = [
        (sid, s) for sid, s in graph.sections.items() if s.bbox_w > 0 and s.bbox_h > 0
    ]
    for i in range(len(boxed)):
        sid_a, a = boxed[i]
        box_a = (a.bbox_x, a.bbox_y, a.bbox_x + a.bbox_w, a.bbox_y + a.bbox_h)
        for j in range(i + 1, len(boxed)):
            sid_b, b = boxed[j]
            box_b = (b.bbox_x, b.bbox_y, b.bbox_x + b.bbox_w, b.bbox_y + b.bbox_h)
            overlap_x = (
                box_a[2] - tolerance > box_b[0] and box_b[2] - tolerance > box_a[0]
            )
            overlap_y = (
                box_a[3] - tolerance > box_b[1] and box_b[3] - tolerance > box_a[1]
            )
            if overlap_x and overlap_y:
                yield sid_a, sid_b, box_a, box_b


def iter_coincident_stations(
    graph: MetroGraph, tolerance: float = 1.0
) -> Iterator[tuple[str, str, float, float]]:
    """Yield ``(sid_a, sid_b, x, y)`` for every pair of distinct visible
    stations placed within *tolerance* of the same centre.

    Two real (non-port, non-hidden) stations sharing an ``(x, y)`` render
    their pill markers on top of each other.  Rail-mode stations are exempt:
    their markers render as per-rail knobs distributed across the rail bundle,
    so a shared centre is not a visual collision there.  Sorting by ``x`` lets
    the inner scan stop once a candidate's ``x`` clears the tolerance window.
    Shared by the runtime guard and the offline validator so the two cannot
    drift.
    """
    exempt_rail = graph.has_rail_sections
    placed: list[tuple[str, float, float]] = []
    for sid, station in graph.stations.items():
        if station.is_port or station.is_hidden:
            continue
        if exempt_rail and graph.station_is_rail(sid):
            continue
        placed.append((sid, station.x, station.y))
    placed.sort(key=lambda p: p[1])
    for i, (sid, x, y) in enumerate(placed):
        for oid, ox, oy in placed[i + 1 :]:
            if ox - x > tolerance:
                break
            if abs(y - oy) <= tolerance:
                yield sid, oid, ox, oy


def iter_bbox_checkable_stations(
    graph: MetroGraph,
) -> Iterator[tuple[str, Station, Section]]:
    """Yield ``(sid, station, section)`` for each rendered station a
    bbox-containment check should inspect: skips ports, junctions, and
    stations whose section has no sized bbox.

    Ports live on the boundary by construction and junctions are invisible
    synthetic nodes, so neither is a visible-station containment concern.
    Shared by the marker-edge guard, the centre-containment guard, and the
    offline validator so they cannot drift on what they exempt.
    """
    junction_ids = graph.junction_ids
    for sid, station in graph.stations.items():
        section = graph.sections.get(station.section_id or "")
        if (
            section is None
            or station.is_port
            or sid in junction_ids
            or section.bbox_w == 0
        ):
            continue
        yield sid, station, section


def iter_stations_outside_bbox(
    graph: MetroGraph, margin: float
) -> Iterator[tuple[str, Station, Section]]:
    """Yield ``(sid, station, section)`` for each rendered station whose
    centre lies outside its section's bbox by more than *margin*.

    Shared by the always-on centre-containment guard and the offline
    validator so the two report the same defect from one implementation.
    """
    for sid, station, section in iter_bbox_checkable_stations(graph):
        inside_x = (
            section.bbox_x - margin
            <= station.x
            <= section.bbox_x + section.bbox_w + margin
        )
        inside_y = (
            section.bbox_y - margin
            <= station.y
            <= section.bbox_y + section.bbox_h + margin
        )
        if not (inside_x and inside_y):
            yield sid, station, section


class _HasXY(Protocol):
    x: float
    y: float


@dataclass(frozen=True)
class Axis:
    """A coordinate axis (``"x"`` or ``"y"``) and its spacing unit."""

    name: str
    step: float

    def get(self, station: _HasXY) -> float:
        return getattr(station, self.name)

    def set(self, station: _HasXY, value: float) -> None:
        setattr(station, self.name, value)


@dataclass(frozen=True)
class AxisFrame:
    """A section's layer (``primary``) and track (``secondary``) axes.

    LR/RL place layers along X and stack lines along Y; TB transposes the two.
    ``primary_sign`` is ``-1`` for RL, which runs the primary axis in reverse
    (mirrored by ``single_section._mirror_primary``), else ``+1``.

    ``secondary_sign`` is the lane fan direction.  A 90-degree-CW rotation maps
    LR's screen-down lane (+Y) to screen-left (-X), so TB fans lanes to -X
    (``-1``).  LR/RL keep ``+1`` (RL reverses only the primary); BT is TB
    reflected on its flow axis, so it fans lanes to +X (``+1``) -- the rotation
    image of TB's lane.  The sign is applied at the draw accessor
    (:func:`station_lane_coord`, :func:`lane_delta`), never to a stored offset,
    which stays positive.
    """

    primary: Axis
    secondary: Axis
    primary_sign: float
    secondary_sign: float

    @staticmethod
    def axes_for_direction(direction: str) -> tuple[str, str]:
        """``(primary, secondary)`` axis names for *direction*, spacing-free.

        A vertical flow (TB/BT) runs its layers down Y and stacks lines along
        X; a horizontal flow (LR/RL) does the reverse.  Exposed separately from
        :meth:`for_direction` so passes can ask which axis is the flow axis (or
        the lane axis) without having spacings to hand.
        """
        return ("y", "x") if direction in ("TB", "BT") else ("x", "y")

    @staticmethod
    def flow_sign(direction: str) -> float:
        """The flow-axis sign (``primary_sign``) for *direction*, spacing-free.

        ``-1`` for the reversed flows (RL, BT), ``+1`` otherwise.  Exposed so a
        pass can read the sign without building a frame with dummy spacings.
        """
        return -1.0 if direction in ("RL", "BT") else 1.0

    @staticmethod
    def secondary_sign_for(direction: str) -> float:
        """The lane-fan sign (:attr:`secondary_sign`) for *direction*, spacing-free.

        ``-1`` for TB (a 90-degree-CW rotation fans a downward flow's lanes to
        -X), ``+1`` otherwise.  Exposed so a pass can read the sign without
        building a frame with dummy spacings.
        """
        secondary = AxisFrame.axes_for_direction(direction)[1]
        return -1.0 if secondary == "x" and direction != "BT" else 1.0

    @classmethod
    def for_direction(
        cls, direction: str, x_spacing: float, y_spacing: float
    ) -> AxisFrame:
        primary, secondary = cls.axes_for_direction(direction)
        step = {"x": x_spacing, "y": y_spacing}
        return cls(
            Axis(primary, step[primary]),
            Axis(secondary, step[secondary]),
            cls.flow_sign(direction),
            cls.secondary_sign_for(direction),
        )


def lanes_run_along_y(direction: str) -> bool:
    """``True`` when a section stacks its lines (the secondary/lane axis) on Y.

    Row-level inter-section passes align the Y axis: row trunk-Y alignment, the
    shared row Y-grid, top-aligning row-mates.  A horizontal (LR/RL) section's
    lanes are Y-separated, so it is a first-class member of that machinery.  A
    vertical (TB/BT) section runs its flow down Y and separates lines along X,
    so it has no row-Y lane grid to share and the row passes leave its Y alone.
    """
    return AxisFrame.axes_for_direction(direction)[1] == "y"


def lanes_run_along_x(direction: str) -> bool:
    """``True`` when a section stacks its lines (the secondary/lane axis) on X.

    The complement of :func:`lanes_run_along_y`: a vertical flow (TB/BT) runs
    its trunk down Y and separates lines along X, so its labels sit beside the
    pill and its file icons march along Y.  The positive way to ask "is this a
    vertical flow" without a bare ``direction == "TB"`` branch.
    """
    return AxisFrame.axes_for_direction(direction)[1] == "x"


Point = tuple[float, float]


def axis_point(primary_axis: str, primary: float, secondary: float) -> Point:
    """Assemble an ``(x, y)`` point from a ``(primary, secondary)`` coordinate pair.

    *primary_axis* is the flow-axis name (``"x"`` for LR/RL, ``"y"`` for TB/BT)
    from :meth:`AxisFrame.axes_for_direction`; *secondary* lands on the other
    axis.  The inverse of :func:`axis_split`.
    """
    return (primary, secondary) if primary_axis == "x" else (secondary, primary)


def axis_split(primary_axis: str, point: Point) -> Point:
    """Decompose an ``(x, y)`` point into its ``(primary, secondary)`` coordinates.

    The inverse of :func:`axis_point`.
    """
    px, py = point
    return (px, py) if primary_axis == "x" else (py, px)


def station_lane_coord(frame: AxisFrame, station: _HasXY, offset: float) -> float:
    """Screen coordinate of a positive lane *offset* from *station* on its lane axis.

    ``station.y + offset`` for LR/RL; ``station.x - offset`` for TB.  The lane
    sign (:attr:`AxisFrame.secondary_sign`) lives here, at the draw accessor, so
    stored offsets stay positive and a section plots as a true rotation of LR.
    """
    return frame.secondary.get(station) + frame.secondary_sign * offset


def lane_delta(frame: AxisFrame, offset: float) -> float:
    """Signed secondary-axis displacement for a positive lane *offset*.

    ``+offset`` for LR/RL, ``-offset`` for TB -- the lane-sign image of *offset*
    on the screen axis the lines stack along, without reference to a station.
    """
    return frame.secondary_sign * offset


def lane_delta_to_normal_offset(delta: float, travel: Point) -> float:
    """Map a lane-axis delta to the bundle builder's right-normal offset.

    ``routing.bundle.build_concentric_bundle`` fans members along the right-hand
    normal of travel (``(-ty, tx)`` in screen coords, Y growing downward) and
    expects positive offsets.  A *delta* from :func:`lane_delta` lives on the
    secondary (lane) screen axis -- Y for a horizontal flow, X for a vertical
    one -- so projecting it onto the unit right-normal of *travel* restates it in
    the builder's convention; for axis-aligned travel it is a +/-1 sign lookup.
    This is the sole point where the lane-sign and builder-normal conventions
    meet (used by the perpendicular turn-in corner hybrid site).
    """
    tx, ty = travel
    length = math.hypot(tx, ty)
    if length == 0.0:
        return delta
    nx, ny = -ty / length, tx / length
    # The call site's travel is axis-aligned, so the lane delta sits wholly on
    # the screen axis perpendicular to travel: Y for horizontal flow, X for
    # vertical.  Project that displacement onto the unit right-normal.
    travels_horizontally = abs(tx) >= abs(ty)
    dx, dy = (0.0, delta) if travels_horizontally else (delta, 0.0)
    return dx * nx + dy * ny


def single_corner_centreline(
    direction: str, src: Point, tgt: Point, *, flow_first: bool
) -> list[Point]:
    """Three-point centreline turning one right-angle corner from *src* to *tgt*.

    The two legs run along a section's flow (primary) and lane (secondary) axes.
    With *flow_first* the first leg runs along the flow axis to the target's flow
    coordinate, then turns onto the lane axis into the port -- the exit-port
    shape (an LR/RL trunk run then perpendicular rise, a TB drop then run out to
    a side port).  Without it the legs swap order, lane axis first then flow --
    the lane-axis entry shape (a TB side-port run in then drop onto the trunk).
    """
    primary_axis, _secondary_axis = AxisFrame.axes_for_direction(direction)
    src_primary, src_secondary = axis_split(primary_axis, src)
    tgt_primary, tgt_secondary = axis_split(primary_axis, tgt)
    if flow_first:
        corner = axis_point(primary_axis, tgt_primary, src_secondary)
    else:
        corner = axis_point(primary_axis, src_primary, tgt_secondary)
    return [src, corner, tgt]


def diagonal_centreline(
    direction: str, src: Point, tgt: Point, primary_start: float, primary_end: float
) -> list[Point]:
    """Four-point centreline: a flow-axis run, a 45-degree diagonal, a flow-axis run.

    *primary_start* and *primary_end* are the flow-axis coordinates where the
    diagonal begins and ends (from ``_compute_diagonal_placement``).  The first
    straight run carries the source's lane coordinate, the second the target's,
    so the line steps from one lane to the other across the diagonal.
    """
    primary_axis, _secondary_axis = AxisFrame.axes_for_direction(direction)
    _src_primary, src_secondary = axis_split(primary_axis, src)
    _tgt_primary, tgt_secondary = axis_split(primary_axis, tgt)
    return [
        src,
        axis_point(primary_axis, primary_start, src_secondary),
        axis_point(primary_axis, primary_end, tgt_secondary),
        tgt,
    ]


def segment_intersects_quad(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    quad: list[tuple[float, float]],
) -> bool:
    """``True`` iff the segment touches or crosses the convex *quad*.

    *quad* is four corners in order (winding either way).  Exact for a convex
    polygon: the segment hits it when an endpoint lies inside or the segment
    crosses any edge.  Used for rotated (angled) label footprints, where an
    axis-aligned bbox would overstate the diagonal strip's extent.
    """
    n = len(quad)

    def _inside(px: float, py: float) -> bool:
        sign = 0
        for i in range(n):
            ax, ay = quad[i]
            bx, by = quad[(i + 1) % n]
            cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
            if cross > 1e-9:
                if sign < 0:
                    return False
                sign = 1
            elif cross < -1e-9:
                if sign > 0:
                    return False
                sign = -1
        return True

    if _inside(x1, y1) or _inside(x2, y2):
        return True

    def _ccw(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> bool:
        return (cy - ay) * (bx - ax) > (by - ay) * (cx - ax)

    for i in range(n):
        cx, cy = quad[i]
        dx, dy = quad[(i + 1) % n]
        if _ccw(x1, y1, cx, cy, dx, dy) != _ccw(x2, y2, cx, cy, dx, dy) and _ccw(
            x1, y1, x2, y2, cx, cy
        ) != _ccw(x1, y1, x2, y2, dx, dy):
            return True
    return False


def segment_intersects_bbox(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    bbox: tuple[float, float, float, float],
) -> bool:
    """Liang-Barsky test: ``True`` iff the segment touches or crosses *bbox*.

    Exact for any segment against an axis-aligned
    ``(x_min, y_min, x_max, y_max)`` bbox.
    """
    bx_min, by_min, bx_max, by_max = bbox
    if max(x1, x2) < bx_min or min(x1, x2) > bx_max:
        return False
    if max(y1, y2) < by_min or min(y1, y2) > by_max:
        return False
    dx, dy = x2 - x1, y2 - y1
    t_min, t_max = 0.0, 1.0
    for p, q in (
        (-dx, x1 - bx_min),
        (dx, bx_max - x1),
        (-dy, y1 - by_min),
        (dy, by_max - y1),
    ):
        if abs(p) < 1e-9:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0 and t > t_min:
            t_min = t
        elif p > 0 and t < t_max:
            t_max = t
        if t_min > t_max:
            return False
    return True


class BBoxXIndex:
    """X-sorted index over labelled bboxes for O(log N + k) range queries."""

    __slots__ = ("_items", "_x_mins")

    def __init__(
        self,
        boxes: list[tuple[str, tuple[float, float, float, float]]],
    ) -> None:
        self._items = sorted(boxes, key=lambda item: item[1][0])
        self._x_mins = [item[1][0] for item in self._items]

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[tuple[str, tuple[float, float, float, float]]]:
        return iter(self._items)

    def query_x_range(
        self, qx_min: float, qx_max: float
    ) -> Iterator[tuple[str, tuple[float, float, float, float]]]:
        """Yield ``(key, bbox)`` for every item whose bbox X-extent
        overlaps ``[qx_min, qx_max]``.
        """
        upper = bisect.bisect_right(self._x_mins, qx_max)
        for i in range(upper):
            key, bbox = self._items[i]
            if bbox[2] >= qx_min:
                yield key, bbox


def _route_is_side_entry_turn_in(graph: MetroGraph, rp: RoutedPath) -> bool:
    """Whether *rp* is the turn-in leg from an entry port perpendicular to flow.

    An entry port on the axis perpendicular to its section's flow (LEFT/RIGHT
    on a vertical-flow TB/BT section, TOP/BOTTOM on a horizontal-flow LR/RL
    one) reaches the trunk via one traverse perpendicular to that flow.  That
    leg is the entry, bounded by the section width, not a serpentine fold-back,
    so backtrack accounting excludes it.
    """
    from nf_metro.parser.model import PortSide

    port = graph.ports.get(rp.edge.source)
    if not port or not port.is_entry:
        return False
    section = graph.sections.get(port.section_id)
    if section is None:
        return False
    if lanes_run_along_y(section.direction):
        return port.side in (PortSide.TOP, PortSide.BOTTOM)
    return port.side in (PortSide.LEFT, PortSide.RIGHT)


def iter_serpentine_backtracks(
    graph: MetroGraph,
    routes: list[RoutedPath],
    offsets: dict[tuple[str, str], float],
    *,
    backtrack_frac: float = 0.5,
    tolerance: float = 0.0,
) -> Iterator[tuple[str, float, float, Section]]:
    """Yield ``(sid, against, limit, section)`` for each stacked section that
    backtracks against its flow beyond ``backtrack_frac`` of its width.

    Same-direction sections stacked in one grid column and chained serpentine
    their effective flow row by row so consecutive sections meet on a shared
    side joined by a short vertical drop.  A section that fails to
    alternate enters on the wrong side and folds its internal route back across
    the section width.  For every section in a detected serpentine run this
    sums the wrong-way horizontal travel of its internal segments, measured on
    the rendered geometry (route offsets applied), and reports the section when
    that travel exceeds ``backtrack_frac * width + tolerance``.  Shared by the
    runtime guard and the offline validator so the two cannot drift.
    """
    from nf_metro.layout.auto_layout import detect_serpentine_runs
    from nf_metro.layout.routing.common import apply_route_offsets

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
        if _route_is_side_entry_turn_in(graph, rp):
            continue
        forward = 1.0 if graph.sections[src_sec].direction != "RL" else -1.0
        pts = apply_route_offsets(rp, offsets)
        for k in range(len(pts) - 1):
            dx = pts[k + 1][0] - pts[k][0]
            if dx * forward < 0:
                wrong_way[src_sec] += abs(dx)

    for sid, against in wrong_way.items():
        section = graph.sections[sid]
        limit = backtrack_frac * max(section.bbox_w, 1.0)
        if against > limit + tolerance:
            yield sid, against, limit, section
