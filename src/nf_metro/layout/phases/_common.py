"""Shared leaf helpers used across layout phases (bbox math, section queries)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from nf_metro.layout.constants import (
    COORD_TOLERANCE,
    SAME_COORD_TOLERANCE,
    SECTION_Y_PADDING,
    STATION_RADIUS_APPROX,
)
from nf_metro.layout.geometry import AxisFrame, lanes_run_along_x, lanes_run_along_y
from nf_metro.layout.phase_state import require_phase_field
from nf_metro.parser.model import (
    Edge,
    MetroGraph,
    Port,
    PortSide,
    Section,
    Station,
    is_bypass_v,
)

if TYPE_CHECKING:
    from nf_metro.layout.routing.common import RoutedPath


def perp_entry_lands_left(section: Section, graph: MetroGraph) -> bool:
    """Which side of the internal trunk a perpendicular entry drop lands on.

    A TOP/BOTTOM entry into an LR/RL section drops in beside the trunk, then
    runs horizontally to the trunk and out the flow-axis exit.  If the drop
    lands on the *same* side as the exit, that run and the exit leg cover the
    same track in opposing directions -- the line folds back over itself.  So
    the drop must land on the side opposite the flow-axis exit: LEFT-exit ->
    drop on the right, RIGHT-exit -> drop on the left.

    With no single LEFT/RIGHT exit to key off, falls back to the flow-natural
    side (LR enters left, RL enters right); an exit on that natural side then
    also resolves to the natural side.
    """
    exit_sides = {graph.ports[pid].side for pid in flow_axis_exit_ports(section, graph)}
    if exit_sides == {PortSide.LEFT}:
        return False
    if exit_sides == {PortSide.RIGHT}:
        return True
    return section.direction == "LR"


def iter_sole_trunk_continuations(
    graph: MetroGraph,
) -> Iterator[tuple[str, str, str]]:
    """Yield ``(section_id, pred, node)`` for in-section linear continuations.

    A *node* is a sole trunk continuation when, inside a horizontal (LR/RL)
    section, it has exactly one real (non-port, non-hidden) in-section
    predecessor whose *only* forward path in the whole graph is this node, and
    that predecessor carries a strict superset of the node's lines (some of the
    predecessor's lines ended there).  The chain is then linear with no sibling
    branch to fan toward, so the node must hold the predecessor's track rather
    than drop to its own line base.

    The full-graph successor test is the discriminator: a predecessor that also
    feeds a section-exit edge (or a bypass V) routes that line *around* the
    node, so the node legitimately drops off the trunk.  Off-track stations are
    excluded at both ends; their Y comes from later phases.
    """
    for section in graph.sections.values():
        if not lanes_run_along_y(section.direction):
            continue
        sec_ids = set(section.station_ids)
        for sid in section.station_ids:
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.is_hidden or st.off_track:
                continue
            preds = {
                e.source
                for e in graph.edges_to(sid)
                if e.source in sec_ids
                and not graph.stations[e.source].is_port
                and not graph.stations[e.source].is_hidden
            }
            if len(preds) != 1:
                continue
            pred = next(iter(preds))
            if graph.stations[pred].off_track:
                continue
            if {e.target for e in graph.edges_from(pred)} != {sid}:
                continue
            if set(graph.station_lines(pred)) > set(graph.station_lines(sid)):
                yield (section.id, pred, sid)


def iter_corridor_fed_solo_entries(
    graph: MetroGraph, tol: float
) -> Iterator[tuple[str, str, str]]:
    """Yield ``(section_id, entry_port_id, line_id)`` for corridor-fed solos.

    A LEFT/RIGHT entry port of an LR/RL section (lanes spread along Y) that
    carries a single present line, where every feeder reaches the port on a
    base Y more than ``tol`` away -- a vertical corridor.  Such a section has no
    bundle to keep ordered, so its lone consumer must ride offset 0 rather than
    the lane the line held in the upstream multi-line section: the corridor's
    vertical leg absorbs the lane step with no sloped segment.  A flat (same-Y)
    seam is excluded, since re-basing there would slope the straight-through run
    into an almost-horizontal segment.
    """
    present: dict[str, set[str]] = defaultdict(set)
    for sid, st in graph.stations.items():
        if not st.is_port and st.section_id is not None:
            present[st.section_id].update(graph.station_lines(sid))
    for sec_id, sec in graph.sections.items():
        if not lanes_run_along_y(sec.direction):
            continue
        lines = present.get(sec_id, set())
        if len(lines) != 1:
            continue
        line_id = next(iter(lines))
        for pid in sec.entry_ports:
            port = graph.ports.get(pid)
            if port is None or port.side not in (PortSide.LEFT, PortSide.RIGHT):
                continue
            port_y = graph.stations[pid].y
            feeders = [
                graph.stations[e.source]
                for e in graph.edges_to(pid)
                if e.source in graph.stations
            ]
            if feeders and all(abs(f.y - port_y) > tol for f in feeders):
                yield sec_id, pid, line_id


def flow_axis_exit_ports(section: Section, graph: MetroGraph) -> set[str]:
    """Ids of *section*'s exit ports on its flow axis (LEFT/RIGHT).

    For a vertical-flow (TB/BT) section these are the ports a route turns
    sideways to leave through -- the exit corridor -- as opposed to a
    perpendicular TOP/BOTTOM drop.
    """
    return {
        pid
        for pid in section.exit_ports
        if (p := graph.ports.get(pid)) is not None
        and p.side in (PortSide.LEFT, PortSide.RIGHT)
    }


def section_entry_sides(graph: MetroGraph, section: Section) -> set[PortSide]:
    """The distinct sides through which lines actually enter *section*.

    Only entry ports carrying at least one edge count -- an entry port with no
    line is not an approach.  A section with more than one side here is entered
    from multiple directions (a perpendicular multi-side entry), which drives
    the entry-side placement and routing that a single-side section skips.
    """
    return {
        port.side
        for pid in section.entry_ports
        if (port := graph.ports.get(pid)) is not None
        and any(True for _ in graph.edges_to(pid))
    }


def leftward_blocker_right_edge(graph: MetroGraph, entry_port: Station) -> float | None:
    """Right edge of the nearest section box straddling *entry_port*'s Y to its left.

    A LEFT entry is reached by a horizontal run at the port's Y from its own
    side; any other section whose box spans that Y and lies left of the port
    sits across that run.  Returns the rightmost such box's right edge (the
    immediate blocker), or ``None`` when nothing straddles the Y.
    """
    ey, ex = entry_port.y, entry_port.x
    own = entry_port.section_id
    best: float | None = None
    for sid, s in graph.sections.items():
        if sid == own or s.bbox_w <= 0:
            continue
        right = s.bbox_x + s.bbox_w
        if (
            s.bbox_y - COORD_TOLERANCE <= ey <= s.bbox_y + s.bbox_h + COORD_TOLERANCE
            and right < ex - COORD_TOLERANCE
            and (best is None or right > best)
        ):
            best = right
    return best


def _is_fold_section(section: Section) -> bool:
    """``True`` for a section the row-fold logic produced.

    A fold either spans more than one grid row or runs its flow vertically
    (TB/BT).  Its exit ports are placed by the fold exit-port path
    (``_align_exit_ports``) rather than the row-level exit passes, which expect
    a single-row horizontal-flow section.
    """
    return section.grid_row_span > 1 or not lanes_run_along_y(section.direction)


def _lr_exit_aligned_target(
    graph: MetroGraph,
    port_id: str,
    exit_section: Section,
    junction_ids: set[str],
) -> Station | None:
    """Return the entry port a LEFT/RIGHT exit aligns its Y to, or ``None``.

    The exit aligns to a directly-connected LEFT/RIGHT entry port lying within
    the exit section's bbox.  A fan-out junction, a perpendicular (cross-axis)
    target port, or a target outside the bbox is not an alignment target.
    """
    bbox_top = exit_section.bbox_y
    bbox_bot = exit_section.bbox_y + exit_section.bbox_h
    for edge in graph.edges_from(port_id):
        tgt = graph.stations.get(edge.target)
        if not tgt:
            continue
        if edge.target in junction_ids:
            return None
        if not tgt.is_port:
            continue
        tgt_port_obj = graph.ports.get(tgt.id)
        if tgt_port_obj and tgt_port_obj.side in (PortSide.TOP, PortSide.BOTTOM):
            return None
        if not (bbox_top <= tgt.y <= bbox_bot):
            return None
        return tgt
    return None


def _iter_cross_row_aligned_fold_lr_exits(
    graph: MetroGraph,
) -> Iterator[tuple[str, Section, Station]]:
    """Yield ``(exit_port_id, exit_section, target_entry)`` for a fold's
    cross-row aligned LEFT/RIGHT exits.

    The shared scope of :func:`iter_fold_lr_exits_short_of_target` and
    :func:`iter_fold_lr_exit_straight_runs`: a vertical-flow (TB/BT) fold's
    LEFT/RIGHT exit aligned to a bbox-contained entry target in a different grid
    row (the fold relocated it, so its multi-sub-row entry can settle away from
    the exit).  Each consumer applies the final predicate that distinguishes a
    straight run from a staircase, so the two cannot drift on scope.
    """
    junction_ids = graph.junction_ids
    for port_id, port in graph.ports.items():
        if port.is_entry or port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        section = graph.sections.get(port.section_id)
        if (
            section is None
            or not _is_fold_section(section)
            or not lanes_run_along_x(section.direction)
        ):
            continue
        tgt = _lr_exit_aligned_target(graph, port_id, section, junction_ids)
        if tgt is None:
            continue
        tgt_section = graph.sections.get(tgt.section_id) if tgt.section_id else None
        if tgt_section is None or tgt_section.grid_row == section.grid_row:
            continue
        yield port_id, section, tgt


def iter_fold_lr_exits_short_of_target(
    graph: MetroGraph, tolerance: float
) -> Iterator[tuple[str, Station]]:
    """Yield ``(exit_port_id, target_entry)`` for fold exits short of their target.

    A cross-row aligned LEFT/RIGHT fold exit is yielded when its target is
    seated *along the flow* from the exit by more than ``tolerance`` -- meaning
    the exit must follow it to that Y for a straight inter-section run.  A target
    seated against the flow (keeping its own descent, an intentional staircase)
    is not yielded.

    The single source of "which fold exit is short of its target" shared by the
    re-alignment that fixes it (:func:`_realign_fold_lr_exit_ports`), the guard
    that flags it (``_guard_fold_lr_exit_follows_target``), and the layout
    invariant test -- so the three cannot drift on scope or predicate.
    """
    for port_id, section, tgt in _iter_cross_row_aligned_fold_lr_exits(graph):
        flow = AxisFrame.flow_sign(section.direction)
        if flow * (tgt.y - graph.stations[port_id].y) > tolerance:
            yield port_id, tgt


def iter_fold_lr_exit_straight_runs(
    graph: MetroGraph, tolerance: float
) -> Iterator[tuple[str, Station]]:
    """Yield ``(exit_port_id, target_entry)`` for straight folded LR/RL runs.

    The companion of :func:`iter_fold_lr_exits_short_of_target`: the same
    cross-row aligned fold exits, but yielding the runs whose exit sits *at* its
    target entry Y -- the inter-section run is straight.  A target seated off the
    exit Y (the staircase case the sibling generator covers) is excluded.

    The single source of "which folded LR/RL run is straight" shared by the
    bbox-bottom alignment (:func:`_align_tb_section_bbox_bottoms`) and the guard
    that checks the two sections clear it evenly
    (``_guard_fold_lr_exit_sections_share_bbox_bottom``).
    """
    for port_id, _section, tgt in _iter_cross_row_aligned_fold_lr_exits(graph):
        if abs(tgt.y - graph.stations[port_id].y) <= tolerance:
            yield port_id, tgt


def iter_stacked_rows_in_rowspan_band(
    graph: MetroGraph, tolerance: float
) -> Iterator[tuple[list[Section], float, float]]:
    """Yield ``(stack, band_top, band_bot)`` for single-row stacks beside a rowspan.

    A ``stack`` is the single-row sections of one column, ordered by grid row,
    that cover one-per-row the full row range an *adjacent* ``grid_row_span > 1``
    section spans.  ``band_top``/``band_bot`` are that neighbour's bbox extent.
    Only stacks whose band has slack beyond their combined height (by more than
    ``tolerance``) are yielded, so a stack already filling its band is skipped.

    The single source of "which stack must fill which rowspan band" shared by the
    pass that distributes it (:func:`_distribute_stacked_rows_in_rowspan_band`),
    the guard that flags a stack that does not
    (``_guard_stacked_rows_fill_rowspan_band``), and the layout invariant test --
    so the three cannot drift on scope or predicate.
    """
    rowspans = [
        s
        for s in graph.sections.values()
        if s.grid_row_span > 1 and s.bbox_h > 0 and s.grid_row >= 0
    ]
    if not rowspans:
        return

    by_col: dict[int, list[Section]] = defaultdict(list)
    for section in graph.sections.values():
        if section.grid_row_span == 1 and section.bbox_h > 0 and section.grid_row >= 0:
            by_col[section.grid_col].append(section)

    for col, stack in by_col.items():
        stack.sort(key=lambda s: s.grid_row)
        band = [
            r
            for r in rowspans
            if abs(r.grid_col - col) == 1
            and r.grid_row <= stack[0].grid_row
            and r.grid_row + r.grid_row_span - 1 >= stack[-1].grid_row
        ]
        if not band:
            continue
        band_top_row = min(r.grid_row for r in band)
        band_bot_row = max(r.grid_row + r.grid_row_span - 1 for r in band)
        if sorted(s.grid_row for s in stack) != list(
            range(band_top_row, band_bot_row + 1)
        ):
            continue
        band_top = min(r.bbox_y for r in band)
        band_bot = max(r.bbox_y + r.bbox_h for r in band)
        if (band_bot - band_top) - sum(s.bbox_h for s in stack) > tolerance:
            yield stack, band_top, band_bot


@contextmanager
def _scoped_sections(graph: MetroGraph, section_ids: list[str]) -> Iterator[None]:
    """Temporarily restrict ``graph.sections`` to ``section_ids``.

    Row-local content phases iterate ``graph.sections`` and read only
    coordinates within each section, so restricting the view to one grid
    row's sections lets a whole-graph phase run row-by-row.  Station and
    edge data on ``graph`` are untouched, so the per-station caches stay
    valid.  Restores the original mapping on exit, including on error.
    """
    original = graph.sections
    graph.sections = {sid: original[sid] for sid in section_ids if sid in original}
    try:
        yield
    finally:
        graph.sections = original


@contextmanager
def _restoring_layout_geometry(graph: MetroGraph) -> Iterator[None]:
    """Restore station coords and section bboxes on exit.

    route_edges' diagonal-centring nudges Station.x and place_labels expands
    section bboxes to fit labels, so a probe or guard that re-routes and
    re-places to inspect the drawn geometry must undo those mutations:
    inspecting the settled layout must not perturb it.
    """
    pos = {sid: (s.x, s.y) for sid, s in graph.stations.items()}
    bbox = {
        sid: (s.bbox_x, s.bbox_y, s.bbox_w, s.bbox_h)
        for sid, s in graph.sections.items()
    }
    try:
        yield
    finally:
        for sid, (x, y) in pos.items():
            st = graph.stations.get(sid)
            if st is not None:
                st.x, st.y = x, y
        for sid, (bx, by, bw, bh) in bbox.items():
            s = graph.sections.get(sid)
            if s is not None:
                s.bbox_x, s.bbox_y, s.bbox_w, s.bbox_h = bx, by, bw, bh


def _grid_rows_top_to_bottom(graph: MetroGraph) -> list[list[str]]:
    """Section ids grouped by grid row, rows ordered top-to-bottom.

    Sections with no bbox or an unassigned row are dropped, matching the
    precondition the row-local content phases already apply when they skip
    such sections.  Ids within a row keep ascending-column order.
    """
    by_row: dict[int, list[Section]] = defaultdict(list)
    for section in graph.sections.values():
        if section.bbox_h > 0 and section.grid_row >= 0:
            by_row[section.grid_row].append(section)
    return [
        [s.id for s in sorted(by_row[row], key=lambda s: s.grid_col)]
        for row in sorted(by_row)
    ]


def _bbox_cols_overlap(a: Section, b: Section) -> bool:
    """True when two sections' bboxes overlap in X (share horizontal extent)."""
    return a.bbox_x < b.bbox_x + b.bbox_w and b.bbox_x < a.bbox_x + a.bbox_w


def _content_station_ids(graph: MetroGraph, section: Section) -> list[str]:
    """IDs of every content marker in ``section``.

    Content = non-port stations excluding the ``__bypass_`` helpers; hidden
    phantoms are kept.  The single definition of the content set the
    top-fit helpers (:func:`...bbox._section_content_hug_top`,
    :func:`...bbox._section_fit_top`,
    :func:`...off_track._off_track_fit_top`) anchor on, so the set cannot
    drift between them -- e.g. a switch to ``is_hidden``, a superset that
    would drop the phantoms.
    """
    return [
        sid
        for sid in section.station_ids
        if (
            sid in graph.stations
            and not graph.stations[sid].is_port
            and not is_bypass_v(sid)
        )
    ]


def _content_station_ys(graph: MetroGraph, section: Section) -> list[float]:
    """Y of every content marker in ``section``; see :func:`_content_station_ids`."""
    return [graph.stations[sid].y for sid in _content_station_ids(graph, section)]


def _trunk_symmetric_fan_ids(graph: MetroGraph, section: Section) -> set[str]:
    """Content station ids in ``section`` sitting in a Y-mirrored off-trunk pair.

    A station qualifies when another station shares its X (same layer) and
    their Ys are equidistant on opposite sides of the section's trunk Y (the
    Y shared by the most content stations).  This is the "diamond straddles
    the trunk at equal offsets" shape -- a 2-way fork/join or a fork with a
    straight-through middle branch both produce it.

    Scopes the section-bbox padding's bundle-span correction
    (:func:`...bbox._predict_section_content_bottom`,
    :func:`...bbox._section_content_hug_top`) to symmetric fans: an
    unmirrored off-trunk placement (a plain flat multi-line run, a fold, an
    asymmetric fan) keeps the existing anchor-only padding rather than
    growing every multi-line section's bbox.

    Y and X are rounded to 1dp before grouping (matching the convention
    elsewhere for float-keyed layout-coordinate grouping, e.g.
    ``_station_marker_bbox``'s callers), so settled-but-not-bit-identical
    coordinates land in the same bucket.  When no single Y is a clear
    majority, the tie-break (highest count, then topmost Y) can pick either
    row as the trunk; the effect is only fewer/no pairs found, never a wrong
    padding target, so it is left unresolved rather than special-cased.
    """
    content_ids = _content_station_ids(graph, section)
    if len(content_ids) < 3:
        return set()
    counts: dict[float, int] = defaultdict(int)
    for sid in content_ids:
        counts[round(graph.stations[sid].y, 1)] += 1
    trunk_y = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]
    by_x: dict[float, list[str]] = defaultdict(list)
    for sid in content_ids:
        by_x[round(graph.stations[sid].x, 1)].append(sid)
    result: set[str] = set()
    for sids in by_x.values():
        if len(sids) < 2:
            continue
        seen_by_offset: dict[float, list[str]] = defaultdict(list)
        for sid in sids:
            off = round(graph.stations[sid].y - trunk_y, 1)
            if off == 0.0:
                continue
            mirrors = seen_by_offset.get(-off)
            if mirrors:
                result.add(sid)
                result.update(mirrors)
            seen_by_offset[off].append(sid)
    return result


def _station_bundle_offset_span(
    graph: MetroGraph, sid: str, offsets: dict[tuple[str, str], float]
) -> tuple[float, float]:
    """Min/max per-line Y offset ``sid``'s drawn bundle pill spans around
    its anchor lane, ``(0.0, 0.0)`` for a station with no lines.

    A multi-line bundle's per-line offsets need not be centred on the
    anchor -- the default non-compact assignment gives each line a
    priority-ordered offset (0, step, 2*step, ...), so a station carrying
    several lines can have its whole pill sit to one side of ``station.y``.
    Shared by :func:`_station_marker_bbox` and the section-bbox padding
    targets in :mod:`...bbox`, so the room a padding constant reserves
    matches what the marker pill actually spans.
    """
    line_offs = [offsets.get((sid, lid), 0.0) for lid in graph.station_lines(sid)]
    if not line_offs:
        return 0.0, 0.0
    return min(line_offs), max(line_offs)


def _station_marker_bbox(
    graph: MetroGraph,
    sid: str,
    offsets: dict[tuple[str, str], float] | None = None,
    radius: float = STATION_RADIUS_APPROX,
) -> tuple[float, float, float, float] | None:
    """Rendered marker / icon bbox for ``sid``, or ``None`` for ports,
    hidden stations, and junctions.

    Mirrors the pill geometry used by ``nf_metro.render.svg``: width
    ``2 * radius``, height ``(max_off - min_off) + 2 * radius``, centred
    at ``(station.x, station.y + (min_off + max_off) / 2)``.
    """
    from nf_metro.layout.routing import compute_station_offsets

    st = graph.stations.get(sid)
    if st is None or st.is_port or st.is_hidden or sid in graph.junctions:
        return None
    if offsets is None:
        offsets = compute_station_offsets(graph)
    min_off, max_off = _station_bundle_offset_span(graph, sid, offsets)
    cy = st.y + (min_off + max_off) / 2
    half_h = (max_off - min_off) / 2 + radius
    return (st.x - radius, cy - half_h, st.x + radius, cy + half_h)


def marker_cross_exempt(graph: MetroGraph, sid: str) -> bool:
    """True when a non-consumer line crossing ``sid``'s marker is no defect.

    A rail-mode section lays its lines on fixed parallel rails; a line whose
    route skips an interchange runs along its rail through the interchange's
    column and threads its knob.  That is the deliberate rail idiom, not a
    breeze-past, so the marker-cross checks exempt it - matching the render-side
    ``check_marker_crossings`` exemption (#942), which reads the same fact back
    from the drawn rail markers.
    """
    return graph.station_is_rail(sid)


def first_vertical_leg_x(points: list[tuple[float, float]]) -> float | None:
    """X of the first (near-)vertical leg of *points*.

    The source-side vertical channel ("V1") of an inter-section route;
    ``None`` when no vertical leg exists.
    """
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if abs(x1 - x0) < COORD_TOLERANCE and abs(y1 - y0) > COORD_TOLERANCE:
            return x1
    return None


def first_vertical_leg_sign(points: list[tuple[float, float]]) -> int | None:
    """Sign of the first (near-)vertical leg of *points*.

    ``-1`` when the source-side vertical channel ("V1") heads up
    (toward smaller Y), ``+1`` when it heads down, ``None`` when no
    vertical leg exists.
    """
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if abs(x1 - x0) < COORD_TOLERANCE and abs(y1 - y0) > COORD_TOLERANCE:
            return -1 if y1 < y0 else 1
    return None


def _canvas_width(graph: MetroGraph) -> float:
    """Horizontal extent of all positioned sections (rightmost - leftmost)."""
    rights = [s.bbox_x + s.bbox_w for s in graph.sections.values() if s.bbox_w > 0]
    lefts = [s.bbox_x for s in graph.sections.values() if s.bbox_w > 0]
    if not rights or not lefts:
        return 0.0
    return max(rights) - min(lefts)


def _route_crosses_section_boundary(
    graph: MetroGraph,
    routes: list[RoutedPath],
    *,
    port_tol: float = 24.0,
    inset: float = 4.0,
    axis_tol: float = 1.0,
) -> tuple[RoutedPath, str, float, float] | None:
    """Return the first ``(route, section_id, x, y)`` where an axis-aligned
    routed segment crosses a section bbox edge away from any declared port,
    else None.

    A segment p1->p2 "crosses" a section edge when it passes strictly
    through one of the four bbox sides within the perpendicular extent of
    that side.  Crossings within *port_tol* of one of the section's ports
    are permitted (that is how a line legitimately enters/leaves).  Any
    other crossing means a horizontal or vertical run is cutting through a
    section box where no port invites it -- the symptom this guard forbids
    (e.g. an entry inferred on the wrong side so the connector slices the
    box,).

    Two classes are intentionally out of scope:

    * **Diagonal transition segments** (45-degree corner curves) clip box
      corners while legitimately approaching a side port; only axis-aligned
      runs (within *axis_tol*) are inspected.
    * **Fan-in/-out bundle routes** through ``__junction_*`` / ``__merge_*``
      / ``__bypass_*`` virtual nodes route around or through neighbouring
      sections by a separate mechanism with its own guards; long-range
      multi-row merge bundles are a known, tracked
      limitation and are excluded here.
    """
    ports_by_sec: dict[str | None, list[Port]] = {}
    for port in graph.ports.values():
        ports_by_sec.setdefault(port.section_id, []).append(port)

    def near_port(sec_id: str, x: float, y: float) -> bool:
        return any(
            abs(p.x - x) <= port_tol and abs(p.y - y) <= port_tol
            for p in ports_by_sec.get(sec_id, [])
        )

    def edge_crossings(
        p1: tuple[float, float],
        p2: tuple[float, float],
        x0: float,
        y0: float,
        x1: float,
        y1: float,
    ) -> list[tuple[float, float]]:
        ax, ay = p1
        bx, by = p2
        hits = []
        for ex in (x0, x1):
            if (ax - ex) * (bx - ex) < 0:
                t = (ex - ax) / (bx - ax)
                yy = ay + t * (by - ay)
                if y0 - inset <= yy <= y1 + inset:
                    hits.append((ex, yy))
        for ey in (y0, y1):
            if (ay - ey) * (by - ey) < 0:
                t = (ey - ay) / (by - ay)
                xx = ax + t * (bx - ax)
                if x0 - inset <= xx <= x1 + inset:
                    hits.append((xx, ey))
        return hits

    def is_bundle_node(node_id: str) -> bool:
        return node_id.startswith(("__junction", "__merge", "__bypass"))

    boxes = [
        (
            sid,
            sec.bbox_x,
            sec.bbox_y,
            sec.bbox_x + sec.bbox_w,
            sec.bbox_y + sec.bbox_h,
        )
        for sid, sec in graph.sections.items()
        if sec.bbox_w > 0 and sec.bbox_h > 0
    ]
    for rp in routes:
        if is_bundle_node(rp.edge.source) or is_bundle_node(rp.edge.target):
            continue
        pts = rp.points
        for i in range(len(pts) - 1):
            p1, p2 = pts[i], pts[i + 1]
            # Only axis-aligned runs cut a box; diagonal corner curves clip
            # edges while approaching side ports legitimately.
            if abs(p1[0] - p2[0]) > axis_tol and abs(p1[1] - p2[1]) > axis_tol:
                continue
            for sid, x0, y0, x1, y1 in boxes:
                for bx, by in edge_crossings(p1, p2, x0, y0, x1, y1):
                    if not near_port(sid, bx, by):
                        return (rp, sid, bx, by)
    return None


def routes_through_unrelated_sections(
    graph: MetroGraph,
    *,
    inset: float = 2.0,
    routes: list[RoutedPath] | None = None,
    offsets: dict[tuple[str, str], float] | None = None,
) -> list[tuple[RoutedPath, str]]:
    """Return ``(route, section_id)`` for every routed segment that passes
    through the interior of a section box the route does not belong to.

    A metro line may only occupy a section's bbox coordinates where it
    connects to a station there: that is, the section must hold the route
    edge's source (the line starts there) or its target (the line enters
    via that section's port).  Any other section whose box a routed
    segment intersects is a pass-through error -- the line is plotted over
    a section it never interacts with (issue #484).

    Unlike :func:`_route_crosses_section_boundary`, this works on the final
    rendered geometry (route offsets applied) and inspects *every* route,
    including fan-in/-out bundle routes through ``__junction_*`` /
    ``__merge_*`` nodes, which the boundary guard intentionally excludes.
    Section membership is resolved via ``section_for_station`` (so a merge
    node assigned to its target section is correctly treated as belonging
    there).
    """
    return _section_interior_crossings(
        graph, own=False, inset=inset, routes=routes, offsets=offsets
    )


def routes_through_own_section_interior(
    graph: MetroGraph,
    *,
    inset: float = 2.0,
    routes: list[RoutedPath] | None = None,
    offsets: dict[tuple[str, str], float] | None = None,
) -> list[tuple[RoutedPath, str]]:
    """Return ``(route, section_id)`` for every inter-section route segment
    that passes back through the interior of its *own* source or target
    section box, beyond the port-to-boundary stub.

    A route legitimately starts at its source section's exit port and ends at
    its target section's entry port -- both on the box boundary -- so a clean
    route only grazes those two boxes at their edges and travels the
    inter-section gaps between them.  A segment whose interior lies inside its
    own source or target bbox has clawed back through the box instead of
    leaving it and routing around the outside: an away-facing-exit wrap that
    renders as a backtrack (issue #1078).

    This is the complement of :func:`routes_through_unrelated_sections`, which
    exempts the route's own sections; together they cover every section box.
    """
    return _section_interior_crossings(
        graph, own=True, inset=inset, routes=routes, offsets=offsets
    )


def _section_interior_crossings(
    graph: MetroGraph,
    *,
    own: bool,
    inset: float,
    routes: list[RoutedPath] | None,
    offsets: dict[tuple[str, str], float] | None,
) -> list[tuple[RoutedPath, str]]:
    """Shared scan behind :func:`routes_through_unrelated_sections` and
    :func:`routes_through_own_section_interior`.

    ``own`` selects which half of the sections each route is checked against:
    ``True`` its own source/target boxes, ``False`` every other box.  A
    crossing that runs along the section's own-line trunk is exempt either way
    (a forked bundle overlaying its trunk, not a foreign pass-through).
    """
    from nf_metro.layout.geometry import segment_intersects_bbox
    from nf_metro.layout.routing import compute_station_offsets, route_edges
    from nf_metro.layout.routing.common import apply_route_offsets

    if offsets is None:
        offsets = compute_station_offsets(graph)
    if routes is None:
        try:
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:  # noqa: BLE001 - routing failures surface elsewhere
            return []

    boxes = [
        (
            sid,
            sec.bbox_x + inset,
            sec.bbox_y + inset,
            sec.bbox_x + sec.bbox_w - inset,
            sec.bbox_y + sec.bbox_h - inset,
        )
        for sid, sec in graph.sections.items()
        if sec.bbox_w > 2 * inset and sec.bbox_h > 2 * inset
    ]

    out: list[tuple[RoutedPath, str]] = []
    for rp in routes:
        if own and not rp.is_inter_section:
            continue
        own_sections = {
            graph.section_for_station(rp.edge.source),
            graph.section_for_station(rp.edge.target),
        }
        pts = apply_route_offsets(rp, offsets)
        for sid, x0, y0, x1, y1 in boxes:
            is_own_section = sid in own_sections
            if is_own_section != own:
                continue
            if any(
                segment_intersects_bbox(
                    pts[i][0],
                    pts[i][1],
                    pts[i + 1][0],
                    pts[i + 1][1],
                    (x0, y0, x1, y1),
                )
                for i in range(len(pts) - 1)
            ) and not _runs_along_section_line_trunk(graph, rp, sid, pts):
                out.append((rp, sid))
    return out


def _runs_along_section_line_trunk(
    graph: MetroGraph, rp: RoutedPath, sid: str, pts: list[tuple[float, float]]
) -> bool:
    """Whether ``rp`` overlays section ``sid``'s own trunk for ``rp``'s line.

    A line that forks and rejoins -- a fan-out junction feeding a section's
    perpendicular entry while a sibling leg continues straight past it to a
    section stacked below -- overlays the intervening section's trunk along its
    own line.  That is one continuous stroke, not a foreign line plotted over a
    section it never touches, so it is exempt from the pass-through check.

    The pass is benign only when the segments crossing the box run parallel to
    the section's trunk axis at the coordinate the line's stations there occupy:
    a vertical-flow section's trunk is a constant X, a horizontal-flow one's a
    constant Y.  A section that does not carry the line has no such trunk, so any
    crossing is a real pass-through.
    """
    from nf_metro.layout.geometry import segment_intersects_bbox

    sec = graph.sections.get(sid)
    if sec is None:
        return False
    # Trunk runs along the flow axis; its constant coordinate is the lane axis.
    cross_axis, run_axis = (0, 1) if lanes_run_along_x(sec.direction) else (1, 0)
    trunk = [
        (st.x, st.y)[cross_axis]
        for stid, st in graph.stations.items()
        if st.section_id == sid and rp.line_id in graph.station_lines(stid)
    ]
    if not trunk:
        return False
    box = (sec.bbox_x, sec.bbox_y, sec.bbox_x + sec.bbox_w, sec.bbox_y + sec.bbox_h)
    for i in range(len(pts) - 1):
        if not segment_intersects_bbox(
            pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], box
        ):
            continue
        run_delta = abs(pts[i + 1][run_axis] - pts[i][run_axis])
        cross_delta = abs(pts[i + 1][cross_axis] - pts[i][cross_axis])
        if cross_delta > SAME_COORD_TOLERANCE and run_delta > SAME_COORD_TOLERANCE:
            return False
        coord = pts[i][cross_axis]
        if not any(abs(coord - t) <= SAME_COORD_TOLERANCE for t in trunk):
            return False
    return True


def is_loop_side_branch_station(graph: MetroGraph, sid: str) -> bool:
    """Mirror ``_recenter_loop_side_stations``'s precondition: a station
    with exactly one in-edge and one out-edge, whose predecessor and
    successor share Y, sitting off the trunk Y between them in X.

    The engine moves such stations to the midpoint of their loop's
    diagonal corners; that move legitimately decouples their X from the
    section's column grid, so column-X consistency checks must exempt
    them.  Both ``_guard_station_x_column_drift`` here and the
    ``test_station_x_within_column_tolerance`` invariant call this.
    """
    st = graph.stations.get(sid)
    if st is None:
        return False
    ins = graph.edges_to(sid)
    outs = graph.edges_from(sid)
    if len(ins) != 1 or len(outs) != 1:
        return False
    src = graph.stations.get(ins[0].source)
    tgt = graph.stations.get(outs[0].target)
    if src is None or tgt is None:
        return False
    if abs(src.y - tgt.y) > SAME_COORD_TOLERANCE:
        return False
    if abs(st.y - src.y) < SAME_COORD_TOLERANCE:
        return False
    if not ((src.x < st.x < tgt.x) or (tgt.x < st.x < src.x)):
        return False
    return True


def _grid_group_section_ids(graph: MetroGraph) -> set[str]:
    """Return the set of section IDs that participated in grid alignment."""
    require_phase_field(graph, "_row_y_grid_info")
    grid_info = graph._row_y_grid_info
    result: set[str] = set()
    for info in grid_info.values():
        result.update(info["section_ids"])
    return result


def _classify_multi_station_ys(
    sub: MetroGraph,
) -> tuple[dict[int, list[float]], set[float]]:
    """Classify Y values by layer and identify multi-station-layer Ys.

    Returns (layer_stations, multi_layer_ys) where layer_stations maps
    layer -> list of Y values, and multi_layer_ys is the set of Y values
    that appear in layers with >1 station.
    """
    layer_stations: dict[int, list[float]] = defaultdict(list)
    for s in sub.stations.values():
        layer_stations[s.layer].append(s.y)
    multi_layer_ys: set[float] = set()
    for ys_at_layer in layer_stations.values():
        if len(ys_at_layer) > 1:
            multi_layer_ys.update(ys_at_layer)
    return layer_stations, multi_layer_ys


def _max_stations_per_layer(sub: MetroGraph) -> int:
    """Return the maximum number of distinct Y positions at any single layer.

    Bypass V helpers (ids starting with ``__bypass_``) are excluded -
    they exist only for routing and must not inflate the row Y grid.
    """
    layer_ys: dict[int, set[float]] = defaultdict(set)
    for s in sub.stations.values():
        if is_bypass_v(s.id):
            continue
        layer_ys[s.layer].add(s.y)
    return max((len(ys) for ys in layer_ys.values()), default=1)


def _row_contiguous_column_groups(
    graph: MetroGraph,
) -> list[list[Section]]:
    """Group laid-out sections by grid row into contiguous column runs.

    Each returned group has at least 2 sections sitting in adjacent
    grid columns (gap <= 1) within the same row.  Sections with no
    bbox or unassigned row are skipped, matching the precondition
    used by the row-alignment callers in this module.
    """
    by_row: dict[int, list[Section]] = defaultdict(list)
    for section in graph.sections.values():
        if section.bbox_h > 0 and section.grid_row >= 0:
            by_row[section.grid_row].append(section)

    result: list[list[Section]] = []
    for row in by_row.values():
        if len(row) < 2:
            continue
        row_sorted = sorted(row, key=lambda s: s.grid_col)
        group = [row_sorted[0]]
        for s in row_sorted[1:]:
            if s.grid_col - group[-1].grid_col <= 1:
                group.append(s)
            else:
                if len(group) >= 2:
                    result.append(group)
                group = [s]
        if len(group) >= 2:
            result.append(group)
    return result


def _is_side_entered_vertical_section(graph: MetroGraph, section: Section) -> bool:
    """Whether *section* is a vertical-flow (TB/BT) section entered from a
    perpendicular side.

    Such a section routes its entry approach across the band above its first
    internal station, so that band is never empty even before the entry port's
    Y settles.
    """
    if lanes_run_along_y(section.direction):
        return False
    return any(
        (port := graph.ports.get(pid)) is not None
        and port.side in (PortSide.LEFT, PortSide.RIGHT)
        for pid in section.entry_ports
    )


def _side_entered_vertical_feeder_pairs(
    graph: MetroGraph,
) -> Iterator[tuple[Section, Section]]:
    """Yield each side-entered vertical section with its feeder row-mate.

    The feeder is the nearest contiguous row-mate to the section's left.  The
    Stage 6.15a top-align and its guard both iterate these pairs, so the
    enforcer and its check read the same section-to-feeder relation.
    """
    for group in _row_contiguous_column_groups(graph):
        for section in group:
            if not _is_side_entered_vertical_section(graph, section):
                continue
            left = [s for s in group if s.grid_col < section.grid_col]
            if left:
                yield section, max(left, key=lambda s: s.grid_col)


def _section_lr_port_anchor_y(graph: MetroGraph, section: Section) -> float | None:
    """The section's frozen trunk anchor: its LR/RL entry port Y, or the
    exit port Y when there is no LR/RL entry port.

    Unlike :func:`_section_trunk_y` (which reads an internal station's
    current Y), this returns a port station's Y -- a frozen inter-section
    anchor held fixed through content placement -- so content phases can
    centre on the trunk without depending on mutable station positions.
    Returns ``None`` when the section has no LR/RL port.
    """
    for ports in (section.entry_ports, section.exit_ports):
        for pid in ports:
            port = graph.ports.get(pid)
            st = graph.stations.get(pid)
            if port and st and port.side in (PortSide.LEFT, PortSide.RIGHT):
                return st.y
    return None


def _snapshot_placement_refs(graph: MetroGraph) -> None:
    """Freeze station Ys and section bbox tops as the placement reference.

    Captured once right before Stage 6.1 and read by Stages 6.1 / 6.2 via
    :func:`_ref_y` / :func:`_ref_bbox_top` for their slack and arrangement
    decisions, so re-applying or perturbing the live geometry between the
    snapshot and those phases can't change where they place content.  Sibling
    of :func:`...bbox._snapshot_struct_heights_below_top` (#485).
    """
    graph._placement_ref_y = {sid: st.y for sid, st in graph.stations.items()}
    graph._placement_ref_bbox_top = {
        sec.id: sec.bbox_y for sec in graph.sections.values()
    }


def _ref_y(graph: MetroGraph, sid: str) -> float:
    """Frozen reference Y for ``sid`` (see :func:`_snapshot_placement_refs`),
    falling back to the live Y when no snapshot covers it."""
    ref = graph._placement_ref_y.get(sid)
    return ref if ref is not None else graph.stations[sid].y


def _ref_bbox_top(graph: MetroGraph, section: Section) -> float:
    """Frozen reference bbox top for ``section`` (see
    :func:`_snapshot_placement_refs`), falling back to the live top."""
    ref = graph._placement_ref_bbox_top.get(section.id)
    return ref if ref is not None else section.bbox_y


def _section_trunk_y(graph: MetroGraph, section: Section) -> float | None:
    """Topmost Y of a full-bundle internal station connected to an LR port.

    This Y is what neighbouring sections must line up with for the row
    bundle to flow horizontally.  Returns ``None`` when no full-bundle
    internal station is directly connected to any LR port.  Bypass V
    helpers (ids starting with ``__bypass_``) are skipped - they exist
    only for routing and must not anchor the row's trunk.
    """
    if not lanes_run_along_y(section.direction):
        return None
    bundle = _section_bundle_lines(graph, section)
    if not bundle:
        return None
    port_ids = section.port_ids
    internal_ids = set(section.station_ids) - port_ids
    trunk_ys: set[float] = set()
    for pid in port_ids:
        p = graph.ports.get(pid)
        if p is None or p.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        candidates: list[str] = []
        for edge in graph.edges_from(pid):
            if edge.target in internal_ids:
                candidates.append(edge.target)
        for edge in graph.edges_to(pid):
            if edge.source in internal_ids:
                candidates.append(edge.source)
        for other_id in candidates:
            st = graph.stations.get(other_id)
            if (
                st
                and not st.is_port
                and not is_bypass_v(other_id)
                and set(graph.station_lines(other_id)) == bundle
            ):
                trunk_ys.add(round(st.y, 3))
    return min(trunk_ys) if trunk_ys else None


def _classify_section_station_ys(
    graph: MetroGraph, section: Section
) -> tuple[list[float], list[float], list[float]]:
    """Return (on_track_ys, off_track_ys, port_ys) for a section's stations."""
    on_track: list[float] = []
    off_track: list[float] = []
    ports: list[float] = []
    for sid in section.station_ids:
        st = graph.stations.get(sid)
        if st is None:
            continue
        if st.is_port:
            ports.append(st.y)
        elif st.off_track:
            off_track.append(st.y)
        else:
            on_track.append(st.y)
    return on_track, off_track, ports


def _section_bundle_lines(graph: MetroGraph, section: Section) -> set[str]:
    """Return the set of line IDs crossing a section's LEFT/RIGHT ports."""
    bundle: set[str] = set()
    for pid in list(section.entry_ports) + list(section.exit_ports):
        port = graph.ports.get(pid)
        if port is None or port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        bundle.update(graph.station_lines(pid))
    return bundle


def section_exit_lines(graph: MetroGraph, section: Section) -> set[str]:
    """Return the line IDs that leave a section.

    Combines the exit-port directives with the lines on the routed edges out
    of the section's exit ports. A station whose lines are disjoint from this
    set has no forward path out of the section (a terminal spur).
    """
    exit_lines: set[str] = set()
    for _side, line_ids in section.exit_hints:
        exit_lines.update(line_ids)
    for pid in section.exit_ports:
        for edge in graph.edges_from(pid):
            exit_lines.add(edge.line_id)
    return exit_lines


def _fan_offsets(n: int) -> list[int]:
    """Symmetric vertical slot offsets for ``n`` stations fanned about a
    trunk Y: even ``n`` leaves the trunk row empty (-n//2..-1, 1..n//2),
    odd ``n`` keeps a middle station on the trunk (-(n//2)..n//2).
    """
    if n % 2 == 0:
        return list(range(-(n // 2), 0)) + list(range(1, n // 2 + 1))
    return list(range(-(n // 2), n // 2 + 1))


def _expand_bbox_for_y(section: Section, y: float) -> None:
    """Expand *section*'s bbox so *y* sits inside with padding."""
    pad = SECTION_Y_PADDING
    top = section.bbox_y
    bot = section.bbox_y + section.bbox_h
    if y - pad < top:
        section.bbox_h += top - (y - pad)
        section.bbox_y = y - pad
    elif y + pad > bot:
        section.bbox_h = (y + pad) - section.bbox_y


def _build_section_subgraph(graph: MetroGraph, section: Section) -> MetroGraph:
    """Build a temporary MetroGraph containing only a section's real stations and edges.

    Excludes port stations and any edges that touch ports. Ports are positioned
    separately on section boundaries after the internal layout is computed.
    """
    sub = MetroGraph()
    sub.lines = graph.lines  # Share line definitions
    sub.diamond_style = graph.diamond_style
    sub.line_spread = graph.section_line_spread(section.id)

    # Collect port IDs for this section
    port_ids = section.port_ids

    # Add only real (non-port) stations belonging to this section
    real_station_ids: set[str] = set()
    for sid in section.station_ids:
        if sid in port_ids:
            continue
        if sid in graph.stations:
            station = graph.stations[sid]
            if station.is_port:
                continue
            sub.add_station(
                Station(
                    id=station.id,
                    label=station.label,
                    section_id=station.section_id,
                    is_port=False,
                    off_track=station.off_track,
                    terminus_labels=list(station.terminus_labels),
                    terminus_icon_types=list(station.terminus_icon_types),
                    terminus_names=list(station.terminus_names),
                )
            )
            real_station_ids.add(sid)

    # Add only edges between real stations (no port-touching edges)
    for edge in graph.edges:
        if edge.source in real_station_ids and edge.target in real_station_ids:
            sub.add_edge(
                Edge(
                    source=edge.source,
                    target=edge.target,
                    line_id=edge.line_id,
                )
            )

    return sub


def _pull_section_ports_to_edge(
    graph: MetroGraph, section: Section, side: PortSide, edge_y: float
) -> None:
    """Move every port on *side* of *section* to *edge_y*."""
    for pid in section.entry_ports + section.exit_ports:
        port = graph.ports.get(pid)
        port_st = graph.stations.get(pid)
        if not port or not port_st:
            continue
        if port.side == side:
            port_st.y = edge_y
            port.y = edge_y


def _set_section_bbox_top(
    graph: MetroGraph, section: Section, new_bbox_top: float
) -> None:
    """Move a section's bbox top to *new_bbox_top* and pull TOP ports along.

    Works in both directions: a smaller *new_bbox_top* grows the box
    upward, a larger one shrinks it.  BOTTOM ports stay put because only
    the top edge moves.
    """
    section.bbox_h += section.bbox_y - new_bbox_top
    section.bbox_y = new_bbox_top
    _pull_section_ports_to_edge(graph, section, PortSide.TOP, section.bbox_y)


def _grow_section_bbox_upward(
    graph: MetroGraph, section: Section, new_bbox_top: float
) -> None:
    """Expand a section's bbox upward to *new_bbox_top* and pull TOP ports.

    Grow-only convenience wrapper: callers guard on ``new_bbox_top <
    section.bbox_y`` so the top is never lowered.  See
    :func:`_set_section_bbox_top` for the bidirectional primitive.
    """
    _set_section_bbox_top(graph, section, new_bbox_top)


def _grow_section_bbox_downward(
    graph: MetroGraph, section: Section, new_bbox_bottom: float
) -> None:
    """Expand a section's bbox downward to *new_bbox_bottom* and pull BOTTOM
    ports along.  Grow-only: a *new_bbox_bottom* at or above the current
    bottom edge is a no-op, so the box is never raised.
    """
    if new_bbox_bottom <= section.bbox_y + section.bbox_h:
        return
    section.bbox_h = new_bbox_bottom - section.bbox_y
    _pull_section_ports_to_edge(
        graph, section, PortSide.BOTTOM, section.bbox_y + section.bbox_h
    )


def exit_run_corridor_clear(
    graph: MetroGraph,
    exit_port_id: str,
    section: Section,
    carrier_ids: list[str],
) -> bool:
    """Whether the X span between the carrier(s) and the exit port is free of
    other section stations.

    Anchoring a flow-aligned exit to its carrier row only helps when the
    straight run from the carrier to the port stays clear; a station seated
    in that span (e.g. an off-track output hung off the carrier) would be
    ploughed through, so the exit keeps its downstream-aligned placement.
    """
    port_st = graph.stations.get(exit_port_id)
    carrier_xs = [graph.stations[c].x for c in carrier_ids if c in graph.stations]
    if port_st is None or not carrier_xs:
        return False
    inner_x = max(carrier_xs) if section.direction == "LR" else min(carrier_xs)
    lo, hi = sorted((inner_x, port_st.x))
    carrier_set = set(carrier_ids)
    for sid in section.station_ids:
        if sid == exit_port_id or sid in carrier_set:
            continue
        st = graph.stations.get(sid)
        if st is None or st.is_port:
            continue
        if lo + SAME_COORD_TOLERANCE < st.x < hi - SAME_COORD_TOLERANCE:
            return False
    return True


def _is_fanout_junction(graph: MetroGraph, jid: str) -> bool:
    """Whether *jid* is a fan-out junction whose Y follows its exit port.

    A stricter, single-junction variant of
    :func:`nf_metro.layout.routing.invariants.fanout_junctions`: that one keys
    purely on a single distinct upstream source, this one additionally requires
    every successor to be an entry port so the anchoring caller only claims the
    plain exit -> junction -> entries shape.  Such a junction takes its Y from
    the single exit-port predecessor (see :func:`_position_junctions`), so
    anchoring that exit drives the junction's row and the fan-out risers fall in
    the inter-section gap rather than the climb falling inside the section.
    """
    succ = list(graph.edges_from(jid))
    if not succ:
        return False
    if any((tp := graph.ports.get(e.target)) is None or not tp.is_entry for e in succ):
        return False
    return len({e.source for e in graph.edges_to(jid)}) == 1


def _exit_anchorable_downstream(
    graph: MetroGraph, exit_port_id: str, junction_ids: set[str]
) -> bool:
    """Whether an exit's downstream lets its level change defer to the gap.

    True when every outgoing edge lands on an entry port directly, or on a
    fan-out junction (whose Y follows this exit).  A merge junction on the far
    side pins its own Y to the downstream entry, so the exit aligns there
    instead and keeps its downstream-aligned placement.
    """
    edges = graph.edges_from(exit_port_id)
    saw_target = False
    for e in edges:
        if e.target in junction_ids:
            if not _is_fanout_junction(graph, e.target):
                return False
        else:
            tp = graph.ports.get(e.target)
            if tp is None or not tp.is_entry:
                return False
        saw_target = True
    return saw_target


def exit_entry_ports_face(
    exit_port: Port,
    entry_port: Port,
    exit_section: Section,
    entry_section: Section,
) -> bool:
    """Whether a LEFT/RIGHT exit and its target entry port open toward each other.

    They face when the exit is on the RIGHT edge and the entry on the LEFT of a
    section further right (or the mirror): the inter-section link is then a
    straight horizontal hop across the column gap.  When both ports sit on the
    same horizontal side, the line must wrap vertically around one section to
    reach the other, so aligning the exit to the downstream row only drags it
    off its own carrier row without straightening the (wrapped) connection.
    """
    if exit_port.side is PortSide.RIGHT and entry_port.side is PortSide.LEFT:
        return exit_section.bbox_x < entry_section.bbox_x
    if exit_port.side is PortSide.LEFT and entry_port.side is PortSide.RIGHT:
        return exit_section.bbox_x > entry_section.bbox_x
    return False


def _in_section_exit_carriers(
    graph: MetroGraph, exit_port_id: str, section: Section
) -> dict[str, float]:
    """Y of each non-port station inside *section* that feeds *exit_port_id*."""
    carriers: dict[str, float] = {}
    for e in graph.edges_to(exit_port_id):
        s = graph.stations.get(e.source)
        if s is not None and not s.is_port and s.section_id == section.id:
            carriers[e.source] = s.y
    return carriers


def flow_exit_carrier_anchor(
    graph: MetroGraph,
    exit_port_id: str,
    section: Section,
    junction_ids: set[str],
) -> tuple[float, list[str]] | None:
    """Carrier row a flow-aligned exit should anchor to, with its carriers.

    Returns ``(carrier_y, carrier_ids)`` when a LEFT/RIGHT exit on a non-fold
    LR/RL section runs into a downstream entry port -- directly or through a
    fan-out junction -- over a clear corridor and its carriers anchor it to a
    shared row; ``None`` otherwise.  Anchoring it there turns the in-section
    run horizontal and moves the level change to a riser in the inter-section
    gap.

    The carriers anchor when they are either a single internal station or a
    *parallel bundle*: several stations sharing one row, one per distinct
    carried line, so each line rides its own offset track to the port.  A
    bypass bundle (several stations feeding one line) is excluded -- the
    farther feeder shares the nearer carrier's track and would run straight
    through it.  A fold section, a merge junction on the far side, or a
    corridor blocked by another station keep the downstream-aligned placement.
    """
    if _is_fold_section(section) or section.direction not in ("LR", "RL"):
        return None
    if not _exit_anchorable_downstream(graph, exit_port_id, junction_ids):
        return None
    carriers = _in_section_exit_carriers(graph, exit_port_id, section)
    if not carriers:
        return None
    ys = list(carriers.values())
    if len(carriers) > 1:
        exit_lines = {
            e.line_id for e in graph.edges_to(exit_port_id) if e.source in carriers
        }
        share_row = max(ys) - min(ys) <= SAME_COORD_TOLERANCE
        one_line_per_carrier = len(carriers) == len(exit_lines)
        if not (share_row and one_line_per_carrier):
            return None
    carrier_ids = list(carriers)
    if not exit_run_corridor_clear(graph, exit_port_id, section, carrier_ids):
        return None
    return min(ys), carrier_ids


def wrap_exit_carrier_anchor(
    graph: MetroGraph,
    exit_port_id: str,
    section: Section,
    junction_ids: set[str],
) -> tuple[float, list[str]] | None:
    """Carrier row a *wrapping* flow-aligned exit should anchor to.

    A LEFT/RIGHT exit on a non-fold LR/RL section whose sole downstream target
    is an entry port on the *same* horizontal side must wrap vertically around
    the target to reach it -- the ports do not face across the column gap (see
    :func:`exit_entry_ports_face`).  It leaves at its carrying station's row:
    the level change belongs to a riser in the inter-section corridor, not a
    diagonal off the carrier row into the box corner.  Returns
    ``(carrier_y, carrier_ids)`` when the carriers share one row (so the anchor
    is unambiguous); ``None`` otherwise -- including the facing case that
    :func:`flow_exit_carrier_anchor` already covers, and fan-out junctions whose
    row follows the exit.
    """
    port = graph.ports.get(exit_port_id)
    if port is None or port.side not in (PortSide.LEFT, PortSide.RIGHT):
        return None
    if _is_fold_section(section) or section.direction not in ("LR", "RL"):
        return None
    targets = [e.target for e in graph.edges_from(exit_port_id)]
    if len(targets) != 1 or targets[0] in junction_ids:
        return None
    entry_port = graph.ports.get(targets[0])
    entry_section = graph.sections.get(entry_port.section_id) if entry_port else None
    if entry_port is None or not entry_port.is_entry or entry_section is None:
        return None
    if exit_entry_ports_face(port, entry_port, section, entry_section):
        return None
    carriers = _in_section_exit_carriers(graph, exit_port_id, section)
    ys = list(carriers.values())
    if not ys or max(ys) - min(ys) > SAME_COORD_TOLERANCE:
        return None
    return min(ys), list(carriers)
