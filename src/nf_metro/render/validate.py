"""Check the geometry drawn in a rendered SVG.

Layout guards run before rendering applies line offsets and final label
adjustments. These checks parse the finished SVG so they can find problems
introduced during rendering.

The label-strike, marker-cross and directional-chevron checks need only the
SVG. The offset-collapse check also needs the
:class:`~nf_metro.render.plan.RenderPlan` used to create the SVG. The plan
records whether two lines should share a track or remain separate.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any, NamedTuple

from nf_metro.layout.constants import LABEL_FONT_SIZE, OFFSET_STEP
from nf_metro.layout.geometry import segment_intersects_bbox
from nf_metro.layout.labels import (
    LabelPlacement,
    segment_strikes_label,
)
from nf_metro.layout.pass_metrics import font_scale_context
from nf_metro.manifest import read_manifest

if TYPE_CHECKING:
    from nf_metro.render.plan import RenderPlan
from nf_metro.text_metrics import DEFAULT_TEXT_METRICS, TextRole, text_style

# The defect family of a finding; one per render-geometry check.
LABEL_STRIKE = "label-strike"
MARKER_CROSS = "marker-cross"
OFFSET_COLLAPSE = "offset-collapse"
CHEVRON_OFF_STROKE = "chevron-off-stroke"

# A drawn run shorter than this reads as a corner nub, not a parallel stretch;
# two lines closer than ``_FLUSH_TOL`` perpendicular read as a single stroke.
_RUN_MIN = 8.0
_FLUSH_TOL = 1.5
# A pair the offset regime spread by a full step (allowing diagonal foreshorten
# and rounding) is meant to read as two lines; drawn flush, it has collapsed.
_PITCH_MIN = OFFSET_STEP - 0.5

# A chevron is placed on its line's drawn centreline exactly, so these bound
# only this parser's flattening of the quadratic corners (a chord of
# ``_Q_CHORDS`` strays well under a degree and a hundredth of a pixel).
_CHEVRON_CENTRE_TOL = 0.5
_CHEVRON_HEADING_COS = math.cos(math.radians(3.0))
_Q_CHORDS = 64
# Pen-lift overlap below this is float noise at a chevron that ends flush with
# a bridge gap, not a chevron drawn into it.
_GAP_OVERLAP_EPS = 1e-3

_Point = tuple[float, float]
_Segment = tuple[_Point, _Point]
_Subpaths = list[list[_Point]]


class RenderFinding(NamedTuple):
    """One render-geometry defect read back out of the drawn SVG.

    Captured from the rendered ink (after the offset and label-lift regimes),
    so a finding is a real visual defect in the user's output.  ``segment`` is
    the drawn line segment ``((x1, y1), (x2, y2))`` that triggered it.
    """

    kind: str
    line_id: str
    station_id: str
    message: str
    segment: tuple[tuple[float, float], tuple[float, float]]


class _Label(NamedTuple):
    placement: LabelPlacement
    font_size: float


# A single route renders as one element per metro line; ``metro-direction-*``
# chevrons (the optional directional markers) also carry ``data-line-id`` but
# are not the route, so the route class substring gates them out.
_ROUTE_CLASS = "metro-line-"
_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_ELEMENT = re.compile(r"<(path|line)\b([^>]*?)/?>", re.DOTALL)
_TEXT = re.compile(r"<text\b([^>]*?)>(.*?)</text>", re.DOTALL)
_TSPAN = re.compile(r"<tspan\b[^>]*>(.*?)</tspan>", re.DOTALL)
_TAG = re.compile(r"<[^>]+>")


def _attr(attrs: str, name: str, default: str | None = None) -> str | None:
    match = re.search(rf'\b{name}="([^"]*)"', attrs)
    return match.group(1) if match else default


def parse_route_polylines(
    svg: str,
) -> list[tuple[str, list[list[tuple[float, float]]]]]:
    """Reconstruct each drawn route's polylines from the SVG ink.

    Returns ``(line_id, subpaths)`` per drawn route element, where each subpath
    is a list of ``(x, y)`` vertices.  A route splits into several subpaths at
    every ``M`` after the first, so a bridged edge's hop gap is a real break
    rather than a phantom segment spanning it.  Each smoothing ``Q`` is
    collapsed back to its corner (the control point), which is the exact
    pre-smoothing vertex, so the polyline equals the logical routed path.
    """
    routes: list[tuple[str, list[list[tuple[float, float]]]]] = []
    for tag, attrs in _ELEMENT.findall(svg):
        cls = _attr(attrs, "class") or ""
        if _ROUTE_CLASS not in cls:
            continue
        line_id = _attr(attrs, "data-line-id") or ""
        if tag == "line":
            x1, y1, x2, y2 = (
                float(_attr(attrs, k) or 0.0) for k in ("x1", "y1", "x2", "y2")
            )
            routes.append((line_id, [[(x1, y1), (x2, y2)]]))
            continue
        d = _attr(attrs, "d") or ""
        subpaths: list[list[tuple[float, float]]] = []
        current: list[tuple[float, float]] = []
        for cmd, body in re.findall(r"([MLQ])([^MLQ]*)", d):
            nums = [float(n) for n in _NUM.findall(body)]
            if cmd == "M":
                if current:
                    subpaths.append(current)
                current = [(nums[0], nums[1])]
            elif cmd == "L":
                current.append((nums[0], nums[1]))
            elif cmd == "Q":
                if current:
                    current.pop()
                current.append((nums[0], nums[1]))
        if current:
            subpaths.append(current)
        routes.append((line_id, subpaths))
    return routes


def drawn_segments(
    routes: list[tuple[str, _Subpaths]],
) -> list[tuple[str, _Point, _Point]]:
    """Flatten parsed route subpaths into ``(line_id, p1, p2)`` drawn segments."""
    return [
        (line_id, subpath[i], subpath[i + 1])
        for line_id, subpaths in routes
        for subpath in subpaths
        for i in range(len(subpath) - 1)
    ]


def parse_station_labels(svg: str) -> list[_Label]:
    """Reconstruct station-label ink placements from the drawn ``<text>`` ink.

    Inverts :func:`~nf_metro.render.svg._render_labels`' emission: the
    ``dominant-baseline`` attribute names the side (``auto`` above, ``hanging``
    below, ``central`` centred), the multi-line block's anchor Y is recovered
    from the per-line ``dy`` stacking the renderer applied, and a ``rotate``
    transform restores the diagonal angle.  The resulting
    :class:`~nf_metro.layout.labels.LabelPlacement` feeds the authoritative
    glyph-ink predicates so the box matches what was drawn.
    """
    labels: list[_Label] = []
    for attrs, body in _TEXT.findall(svg):
        station_id = _attr(attrs, "data-station-id")
        cls = _attr(attrs, "class") or ""
        if station_id is None or "station-label" not in cls:
            continue
        spans = _TSPAN.findall(body)
        text = (
            "\n".join(_TAG.sub("", s) for s in spans) if spans else _TAG.sub("", body)
        )
        text = text.strip("\n")
        x = float(_attr(attrs, "x") or 0.0)
        y = float(_attr(attrs, "y") or 0.0)
        font_size = float(_attr(attrs, "font-size") or LABEL_FONT_SIZE)
        anchor = _attr(attrs, "text-anchor") or "start"
        baseline = _attr(attrs, "dominant-baseline") or ""
        rotate = re.search(r"rotate\(([-\d.]+)", attrs)
        angle = float(rotate.group(1)) if rotate else 0.0

        # A rotated label is emitted at its anchor already; only the upright
        # baselines carry the multi-line Y stacking that has to be undone.
        n_lines = text.count("\n") + 1
        line_spacing = DEFAULT_TEXT_METRICS.line_height(
            text_style(font_size, "bold"), TextRole.STATION_LABEL
        )
        above = False
        dominant = ""
        if not angle:
            if baseline == "auto":
                above = True
                y += (n_lines - 1) * line_spacing
            elif baseline == "central":
                dominant = "central"
                y += (n_lines - 1) * line_spacing / 2
            elif baseline and baseline != "hanging":
                dominant = baseline

        labels.append(
            _Label(
                LabelPlacement(
                    station_id=station_id,
                    text=text,
                    x=x,
                    y=y,
                    above=above,
                    angle=angle,
                    text_anchor=anchor,
                    dominant_baseline=dominant,
                ),
                font_size,
            )
        )
    return labels


def _nodes_by_id(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {node["id"]: node for node in manifest.get("nodes", [])}


# Rail-mode markers (knob, knob outline, connector bar) all carry both an
# ``...-rail-...`` class and the station id; a line threading an interchange
# knob is the intended rail idiom, so the station id signals a marker-cross
# exemption straight from the artifact (the manifest carries no rail flag).
_RAIL_MARKER = re.compile(r"<\w+\b[^>]*?\brail\b[^>]*?/?>", re.DOTALL)


def parse_rail_station_ids(svg: str) -> set[str]:
    """Recover the set of rail-station ids from the drawn rail markers.

    A line passing through a rail interchange's knob is the deliberate rail
    idiom (the same reason the hanging-route guard skips rail endpoints), so
    :func:`check_marker_crossings` reads these ids back from the SVG to exempt
    rail stations without a manifest schema addition.
    """
    ids: set[str] = set()
    for match in _RAIL_MARKER.finditer(svg):
        element = match.group(0)
        cls = _attr(element, "class") or ""
        if "rail" not in cls:
            continue
        sid = _attr(element, "data-station-id")
        if sid is not None:
            ids.add(sid)
    return ids


def check_label_strikes(
    svg: str,
    manifest: dict[str, Any],
    routes: list[tuple[str, list[list[tuple[float, float]]]]],
) -> list[RenderFinding]:
    """A drawn route segment striking through a station name's glyph ink.

    A line painting over a label reads as a strike-through, and a pixel oracle
    false-negatives on it because the line covers the text.  A segment is exempt
    when its line is one the labelled station carries (the
    manifest ``groups``, which subsumes the case of the station being that
    edge's own endpoint).  Uses the authoritative glyph-ink footprint at the
    label's drawn font scale, so it fires only on the inked glyphs, not the
    reserved margin around them.
    """
    nodes = _nodes_by_id(manifest)
    findings: list[RenderFinding] = []
    for placement, font_size in parse_station_labels(svg):
        node = nodes.get(placement.station_id)
        if node is None:
            continue
        carried = set(node.get("groups", ()))
        scale = font_size / LABEL_FONT_SIZE
        with font_scale_context(scale):
            for line_id, subpaths in routes:
                if line_id in carried:
                    continue
                for subpath in subpaths:
                    hit = _first_striking_segment(subpath, placement)
                    if hit is not None:
                        findings.append(
                            RenderFinding(
                                LABEL_STRIKE,
                                line_id,
                                placement.station_id,
                                f"line {line_id!r} strikes through the label of "
                                f"{placement.station_id!r} ({placement.text!r})",
                                hit,
                            )
                        )
                        break
    return findings


def _first_striking_segment(
    subpath: list[tuple[float, float]], placement: LabelPlacement
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    for i in range(len(subpath) - 1):
        (x1, y1), (x2, y2) = subpath[i], subpath[i + 1]
        if segment_strikes_label(x1, y1, x2, y2, placement):
            return ((x1, y1), (x2, y2))
    return None


def check_marker_crossings(
    svg: str,
    manifest: dict[str, Any],
    routes: list[tuple[str, _Subpaths]],
) -> list[RenderFinding]:
    """A drawn route segment passing through a non-consumer station's marker.

    A line raking across a station marker reads as the route running through
    that station, so it must touch only the markers of stations that carry it.
    A segment is exempt when its line is one the marker's station carries (the
    manifest ``groups``, which subsumes the station being that edge's own
    endpoint).  Rail-interchange stations are exempt too: a line threading an
    interchange knob is the intended rail idiom, and their ids are read back
    from the drawn rail markers.  The render-side companion to the layout
    guard ``_guard_no_line_crosses_non_consumer``, read from the artifact so
    it also catches any marker crossing introduced after the offset regime.
    """
    nodes = _nodes_by_id(manifest)
    rail = parse_rail_station_ids(svg)
    findings: list[RenderFinding] = []
    for line_id, subpaths in routes:
        for sid, node in nodes.items():
            if sid in rail or line_id in node.get("groups", ()):
                continue
            radius = node.get("r", 0.0)
            bbox = (
                node["x"] - radius,
                node["y"] - radius,
                node["x"] + radius,
                node["y"] + radius,
            )
            hit = _first_crossing_segment(subpaths, bbox)
            if hit is not None:
                findings.append(
                    RenderFinding(
                        MARKER_CROSS,
                        line_id,
                        sid,
                        f"line {line_id!r} crosses the marker of non-consumer "
                        f"station {sid!r}",
                        hit,
                    )
                )
    return findings


def _first_crossing_segment(
    subpaths: _Subpaths, bbox: tuple[float, float, float, float]
) -> _Segment | None:
    for subpath in subpaths:
        for i in range(len(subpath) - 1):
            (x1, y1), (x2, y2) = subpath[i], subpath[i + 1]
            if segment_intersects_bbox(x1, y1, x2, y2, bbox):
                return ((x1, y1), (x2, y2))
    return None


def check_offset_collapse(
    plan: RenderPlan,
    routes: list[tuple[str, _Subpaths]],
) -> list[RenderFinding]:
    """Find lines drawn together when the plan keeps them separate.

    Lines assigned to the same offset may share a track. Lines separated by at
    least one offset step must remain visually distinct.
    """
    expected = _expected_line_segments(plan)
    if not expected:
        return []
    drawn = drawn_segments(routes)
    findings: list[RenderFinding] = []
    seen: set[tuple[str, str, int, int]] = set()
    for i, (line_a, a1, a2) in enumerate(drawn):
        for line_b, b1, b2 in drawn[i + 1 :]:
            if line_b == line_a:
                continue
            midpoint = _flush_run((a1, a2), (b1, b2))
            if midpoint is None:
                continue
            anchor = _nearest_vertex(expected.get(line_a, ()), midpoint)
            if anchor is None:
                continue
            assigned = _perp_gap(anchor, expected.get(line_b, ()))
            if assigned is None or assigned < _PITCH_MIN:
                continue
            pair = tuple(sorted((line_a, line_b)))
            key = (pair[0], pair[1], round(midpoint[0]), round(midpoint[1]))
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                RenderFinding(
                    OFFSET_COLLAPSE,
                    line_a,
                    "",
                    f"lines {pair[0]!r} and {pair[1]!r} draw flush near "
                    f"({midpoint[0]:.1f},{midpoint[1]:.1f}) but the offset regime "
                    f"spread them by {assigned:.1f}px",
                    (a1, a2),
                )
            )
    return findings


def _expected_line_segments(plan: RenderPlan) -> dict[str, list[_Segment]]:
    """Return the plan's offset route segments, grouped by metro line."""
    segments: dict[str, list[_Segment]] = {}
    for line_id, pts in plan.offset_polylines():
        segments.setdefault(line_id, []).extend(zip(pts, pts[1:]))
    return segments


def _flush_run(a: _Segment, b: _Segment) -> _Point | None:
    """Midpoint of the stretch where *a* and *b* run collinear and flush.

    ``None`` unless the two segments are parallel, perpendicular-closer than
    ``_FLUSH_TOL``, and overlap for at least ``_RUN_MIN`` along their shared
    direction (so a mere corner touch never counts).
    """
    (ax1, ay1), (ax2, ay2) = a
    ux, uy = ax2 - ax1, ay2 - ay1
    length = (ux * ux + uy * uy) ** 0.5
    if length < _RUN_MIN:
        return None
    ux, uy = ux / length, uy / length
    (bx1, by1), (bx2, by2) = b
    if abs((bx2 - bx1) * uy - (by2 - by1) * ux) > 1e-3 * length:
        return None
    if abs((bx1 - ax1) * (-uy) + (by1 - ay1) * ux) >= _FLUSH_TOL:
        return None
    t0 = (bx1 - ax1) * ux + (by1 - ay1) * uy
    t1 = (bx2 - ax1) * ux + (by2 - ay1) * uy
    lo = max(0.0, min(t0, t1))
    hi = min(length, max(t0, t1))
    if hi - lo < _RUN_MIN:
        return None
    mid = (lo + hi) / 2
    return (ax1 + ux * mid, ay1 + uy * mid)


def _nearest_vertex(
    segments: tuple[_Segment, ...] | list[_Segment], point: _Point
) -> _Point | None:
    best: _Point | None = None
    best_d = 0.0
    for (x1, y1), (x2, y2) in segments:
        for vx, vy in ((x1, y1), (x2, y2)):
            d = (vx - point[0]) ** 2 + (vy - point[1]) ** 2
            if best is None or d < best_d:
                best, best_d = (vx, vy), d
    return best


def _perp_gap(
    point: _Point, segments: tuple[_Segment, ...] | list[_Segment]
) -> float | None:
    """Shortest distance from *point* to any segment it projects onto."""
    best: float | None = None
    px, py = point
    for (x1, y1), (x2, y2) in segments:
        dx, dy = x2 - x1, y2 - y1
        length_sq = dx * dx + dy * dy
        if length_sq < 1e-9:
            continue
        t = ((px - x1) * dx + (py - y1) * dy) / length_sq
        if t < -0.05 or t > 1.05:
            continue
        cx, cy = x1 + t * dx, y1 + t * dy
        d = ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
        if best is None or d < best:
            best = d
    return best


_DIRECTION_CLASS = "metro-direction-"


class StrokeTrace(NamedTuple):
    """One drawn route's painted centreline as straight chords.

    ``chords`` are the inked pieces in drawing order, each quadratic corner
    flattened into ``_Q_CHORDS`` of them; ``gaps`` are the pen lifts between
    subpaths (a bridged edge's hop gaps), which carry no ink.
    """

    line_id: str
    chords: list[_Segment]
    gaps: list[_Segment]


class DrawnChevron(NamedTuple):
    """A directional chevron read back from its ``M wing L apex L wing`` ink.

    ``reach`` is how far its ink extends along ``heading`` either side of
    ``centre``, round stroke caps included.
    """

    line_id: str
    centre: _Point
    heading: _Point
    reach: float


def _quad_point(a: _Point, c: _Point, b: _Point, t: float) -> _Point:
    s = 1.0 - t
    return (
        s * s * a[0] + 2 * s * t * c[0] + t * t * b[0],
        s * s * a[1] + 2 * s * t * c[1] + t * t * b[1],
    )


def parse_route_strokes(svg: str) -> list[StrokeTrace]:
    """Each drawn route's painted centreline, rounded corners included.

    :func:`parse_route_polylines` folds every ``Q`` back onto its corner
    vertex; this follows the curve the stroke paints instead, so a mark can be
    measured against the ink rather than the pre-smoothing polyline.
    """
    traces: list[StrokeTrace] = []
    for tag, attrs in _ELEMENT.findall(svg):
        cls = _attr(attrs, "class") or ""
        if _ROUTE_CLASS not in cls:
            continue
        line_id = _attr(attrs, "data-line-id") or ""
        if tag == "line":
            x1, y1, x2, y2 = (
                float(_attr(attrs, k) or 0.0) for k in ("x1", "y1", "x2", "y2")
            )
            traces.append(StrokeTrace(line_id, [((x1, y1), (x2, y2))], []))
            continue
        chords: list[_Segment] = []
        gaps: list[_Segment] = []
        pen: _Point | None = None
        for cmd, body in re.findall(r"([MLQ])([^MLQ]*)", _attr(attrs, "d") or ""):
            nums = [float(n) for n in _NUM.findall(body)]
            if cmd == "M":
                target = (nums[0], nums[1])
                if pen is not None:
                    gaps.append((pen, target))
                pen = target
            elif pen is None:
                continue
            elif cmd == "L":
                target = (nums[0], nums[1])
                chords.append((pen, target))
                pen = target
            else:
                control, end = (nums[0], nums[1]), (nums[2], nums[3])
                prev = pen
                for k in range(1, _Q_CHORDS + 1):
                    point = _quad_point(pen, control, end, k / _Q_CHORDS)
                    chords.append((prev, point))
                    prev = point
                pen = end
        traces.append(StrokeTrace(line_id, chords, gaps))
    return traces


def parse_chevrons(svg: str) -> list[DrawnChevron]:
    """Every directional chevron in the drawn SVG."""
    chevrons: list[DrawnChevron] = []
    for tag, attrs in _ELEMENT.findall(svg):
        cls = _attr(attrs, "class") or ""
        if tag != "path" or _DIRECTION_CLASS not in cls:
            continue
        nums = [float(n) for n in _NUM.findall(_attr(attrs, "d") or "")]
        if len(nums) != 6:
            continue
        w1x, w1y, ax, ay, w2x, w2y = nums
        bx, by = (w1x + w2x) / 2, (w1y + w2y) / 2
        length = math.hypot(ax - bx, ay - by)
        if length <= 0.0:
            continue
        stroke = float(_attr(attrs, "stroke-width") or 0.0)
        chevrons.append(
            DrawnChevron(
                _attr(attrs, "data-line-id") or "",
                ((ax + bx) / 2, (ay + by) / 2),
                ((ax - bx) / length, (ay - by) / length),
                length / 2 + stroke / 2,
            )
        )
    return chevrons


def _point_segment_distance(point: _Point, a: _Point, b: _Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length_sq = dx * dx + dy * dy
    t = 0.0
    if length_sq > 0.0:
        t = ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length_sq
        t = min(1.0, max(0.0, t))
    return math.hypot(point[0] - a[0] - t * dx, point[1] - a[1] - t * dy)


def _runs_along(heading: _Point, a: _Point, b: _Point) -> bool:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length <= 0.0:
        return False
    return (dx * heading[0] + dy * heading[1]) / length >= _CHEVRON_HEADING_COS


def _gap_overlap(chevron: DrawnChevron, gap: _Segment) -> float:
    """Length of *gap* lying along the chevron's ink; negative when clear."""
    (cx, cy), (ux, uy) = chevron.centre, chevron.heading
    along: list[float] = []
    for gx, gy in gap:
        rx, ry = gx - cx, gy - cy
        if abs(rx * uy - ry * ux) > _CHEVRON_CENTRE_TOL:
            return -math.inf
        along.append(rx * ux + ry * uy)
    return min(max(along), chevron.reach) - max(min(along), -chevron.reach)


def _near_trace(point: _Point, trace: StrokeTrace) -> bool:
    xs = [x for seg in (*trace.chords, *trace.gaps) for x, _ in seg]
    ys = [y for seg in (*trace.chords, *trace.gaps) for _, y in seg]
    pad = _CHEVRON_CENTRE_TOL
    return bool(xs) and (
        min(xs) - pad <= point[0] <= max(xs) + pad
        and min(ys) - pad <= point[1] <= max(ys) + pad
    )


def check_chevrons_on_stroke(svg: str) -> list[RenderFinding]:
    """A directional chevron off its own line's drawn stroke, or in a bridge gap.

    A chevron reads as the direction of travel of the line under it, so its
    centre must sit on its own line's painted centreline (rounded corners
    included) and point along that stroke's downstream tangent there. A
    chevron reaching into a bridge gap paints its line across the over-line
    the gap exists to reveal, so that is flagged too.
    """
    traces_by_line: dict[str, list[StrokeTrace]] = {}
    for trace in parse_route_strokes(svg):
        traces_by_line.setdefault(trace.line_id, []).append(trace)
    findings: list[RenderFinding] = []
    for chevron in parse_chevrons(svg):
        nearest = math.inf
        in_gap = False
        for trace in traces_by_line.get(chevron.line_id, ()):
            if not _near_trace(chevron.centre, trace):
                continue
            on_stroke = False
            for a, b in trace.chords:
                distance = _point_segment_distance(chevron.centre, a, b)
                nearest = min(nearest, distance)
                if distance <= _CHEVRON_CENTRE_TOL and _runs_along(
                    chevron.heading, a, b
                ):
                    on_stroke = True
            gapped = any(
                _gap_overlap(chevron, gap) > _GAP_OVERLAP_EPS for gap in trace.gaps
            )
            if on_stroke and not gapped:
                break
            in_gap = in_gap or gapped
        else:
            (cx, cy), (ux, uy) = chevron.centre, chevron.heading
            if in_gap:
                problem = "reaches into a bridge gap of its line"
            elif nearest <= _CHEVRON_CENTRE_TOL:
                problem = "points off its line's drawn tangent"
            elif math.isinf(nearest):
                problem = "lies on no drawn stroke of its line"
            else:
                problem = f"sits {nearest:.2f}px off its line's drawn stroke"
            reach = chevron.reach
            findings.append(
                RenderFinding(
                    CHEVRON_OFF_STROKE,
                    chevron.line_id,
                    "",
                    f"directional chevron of line {chevron.line_id!r} at "
                    f"({cx:.1f},{cy:.1f}) {problem}",
                    (
                        (cx - ux * reach, cy - uy * reach),
                        (cx + ux * reach, cy + uy * reach),
                    ),
                )
            )
    return findings


def validate_render(
    svg: str,
    *,
    plan: RenderPlan | None = None,
) -> list[RenderFinding]:
    """Check a rendered SVG and return its geometry problems.

    The SVG alone supports the label-strike, marker-cross and directional
    chevron checks. Pass the matching *plan* to also check whether separate
    lines collapsed onto one track.
    """
    manifest = read_manifest(svg)
    if manifest is None:
        return []
    routes = parse_route_polylines(svg)
    findings = check_label_strikes(svg, manifest, routes)
    findings += check_marker_crossings(svg, manifest, routes)
    findings += check_chevrons_on_stroke(svg)
    if plan is not None:
        findings += check_offset_collapse(plan, routes)
    return findings
