"""Geometry-aware bypass insertion for non-consumer marker crossings.

The parse-time bypass trigger in :mod:`nf_metro.parser.resolve`
(:func:`_insert_bypass_stations`) inserts hidden ``__bypass_`` helper
stations from topology alone, before any coordinates exist.  Topology
cannot tell whether a line that skips a station will be *drawn* through
that station's marker: an express edge ``s1 -> s3`` that skips ``s2`` on
a single trunk crosses ``s2``'s marker, but the same shape in a section
with vertical spread clears it on a parallel track.

This pass closes that gap by working from the laid-out geometry.  After
a first layout pass it routes the edges and finds every segment that
crosses the marker of a station it does not consume (the condition the
Stage 6.14 guard raises on).  For the in-section cases the bypass-V idiom
can express, it inserts a helper and re-lays out so a bow is drawn around
the marker - but only keeps that second layout when it is an improvement
(fewer crossings, no new fold-back, no new curve defect); otherwise the
helpers are removed so a render the idiom cannot cleanly fix is left
exactly as it was.  Because detection is geometric the pass fires only
where a crossing genuinely occurs, so a clean render gains no helpers and
is laid out exactly once.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from nf_metro.layout.geometry import BBoxXIndex, segment_intersects_bbox
from nf_metro.layout.phases._common import _station_marker_bbox
from nf_metro.parser.model import (
    BYPASS_V_PREFIX,
    Edge,
    MetroGraph,
    Station,
    is_bypass_v,
)


@dataclass(frozen=True)
class _Crossing:
    """One drawn segment passing through a non-consumer station's marker."""

    section_id: str
    pred_id: str
    crossed_id: str
    target_id: str
    line_id: str
    edge_index: int
    cross_x: float


@dataclass
class _LayoutEval:
    """A snapshot of the geometric defects a routed layout exhibits."""

    breeze_total: int = 0
    foldbacks: int = 0
    curve_ok: bool = True
    crossings: list[_Crossing] = field(default_factory=list)


def _section_of(graph: MetroGraph, station_id: str) -> str | None:
    station = graph.stations.get(station_id)
    return station.section_id if station is not None else None


def _is_bypassable_edge_endpoint(
    graph: MetroGraph, section_id: str, endpoint_id: str, *, is_target: bool
) -> bool:
    """True when an edge endpoint lets the bypass-V idiom claim the crossing.

    A helper ``V`` lives inside ``section_id`` and replaces the edge with
    ``pred -> V`` plus ``V -> target``.  The section subgraph that lays the
    helper out drops every port-touching edge, so the predecessor must be a
    real station for ``pred -> V`` to survive; the target may be a real
    station of the section or one of its exit ports (a ``V -> exit_port`` leg
    keeps a real-station source).
    """
    section = graph.sections.get(section_id)
    if section is None:
        return False
    if is_target and endpoint_id in section.exit_ports:
        return True
    station = graph.stations.get(endpoint_id)
    return (
        station is not None
        and not station.is_port
        and not station.is_hidden
        and station.section_id == section_id
    )


def _evaluate(graph: MetroGraph) -> _LayoutEval:
    """Route the laid-out graph and measure its non-consumer-crossing defects.

    Returns the total count of non-consumer marker crossings, the count of
    same-line fold-backs, whether the render-curve invariants hold, and the
    subset of crossings the bypass-V idiom can claim (real station skipped by
    an edge whose endpoints both sit inside that station's section).
    """
    from nf_metro.layout.phases.guards import iter_opposing_line_overlaps
    from nf_metro.layout.routing import compute_station_offsets, route_edges
    from nf_metro.layout.routing.common import apply_route_offsets
    from nf_metro.layout.routing.invariants import assert_render_curve_invariants

    offsets = compute_station_offsets(graph)
    try:
        routes = route_edges(graph, station_offsets=offsets)
    except Exception:  # noqa: BLE001 - routing failures surface in their own guards
        return _LayoutEval()

    boxes: list[tuple[str, tuple[float, float, float, float]]] = []
    station_lines: dict[str, set[str]] = {}
    for sid in graph.stations:
        bbox = _station_marker_bbox(graph, sid, offsets=offsets)
        if bbox is None:
            continue
        boxes.append((sid, bbox))
        station_lines[sid] = set(graph.station_lines(sid))

    crossings: list[_Crossing] = []
    breeze_total = 0
    if boxes:
        index = BBoxXIndex(boxes)
        edge_index = {id(e): i for i, e in enumerate(graph.edges)}
        for r in routes:
            src, tgt, line_id = r.edge.source, r.edge.target, r.line_id
            section_id = _section_of(graph, src)
            ei = edge_index.get(id(r.edge))
            edge_qualifies = (
                section_id is not None
                and ei is not None
                and _is_bypassable_edge_endpoint(
                    graph, section_id, src, is_target=False
                )
                and _is_bypassable_edge_endpoint(graph, section_id, tgt, is_target=True)
            )
            pts = apply_route_offsets(r, offsets)
            for k in range(len(pts) - 1):
                p1, p2 = pts[k], pts[k + 1]
                lo, hi = min(p1[0], p2[0]), max(p1[0], p2[0])
                for sid, bbox in index.query_x_range(lo, hi):
                    if line_id in station_lines[sid] or sid in (src, tgt):
                        continue
                    if not segment_intersects_bbox(p1[0], p1[1], p2[0], p2[1], bbox):
                        continue
                    breeze_total += 1
                    station = graph.stations.get(sid)
                    if (
                        edge_qualifies
                        and _section_of(graph, sid) == section_id
                        and station is not None
                        and not station.is_port
                        and not station.is_hidden
                    ):
                        crossings.append(
                            _Crossing(
                                section_id=section_id,  # type: ignore[arg-type]
                                pred_id=src,
                                crossed_id=sid,
                                target_id=tgt,
                                line_id=line_id,
                                edge_index=ei,  # type: ignore[arg-type]
                                cross_x=(bbox[0] + bbox[2]) / 2,
                            )
                        )

    foldbacks = sum(
        1 for _ in iter_opposing_line_overlaps(graph, offsets=offsets, routes=routes)
    )
    try:
        assert_render_curve_invariants(graph, routes, offsets)
        curve_ok = True
    except Exception:  # noqa: BLE001 - a defective curve is a "not better" signal here
        curve_ok = False

    return _LayoutEval(
        breeze_total=breeze_total,
        foldbacks=foldbacks,
        curve_ok=curve_ok,
        crossings=crossings,
    )


def _is_improvement(before: _LayoutEval, after: _LayoutEval) -> bool:
    """True when the second layout is strictly better at the crossing it fixed.

    The helpers are worth keeping only when they remove a crossing without
    trading it for a fold-back or a broken curve the first layout did not have.
    """
    return (
        after.breeze_total < before.breeze_total
        and after.foldbacks <= before.foldbacks
        and not (before.curve_ok and not after.curve_ok)
    )


def _insert_helpers(graph: MetroGraph, crossings: list[_Crossing]) -> list[str]:
    """Rewrite each crossed edge ``pred -> V1 -> ... -> Vn -> target``.

    One hidden ``__bypass_`` helper is inserted per skipped station, ordered
    along the edge.  Returns the ids of the inserted helpers.
    """
    by_edge: dict[int, list[_Crossing]] = {}
    for c in crossings:
        by_edge.setdefault(c.edge_index, []).append(c)

    count = sum(1 for sid in graph.stations if is_bypass_v(sid))
    new_stations: list[Station] = []
    new_edges: list[Edge] = []
    edges_to_remove: set[int] = set()

    for ei, edge_crossings in by_edge.items():
        edge = graph.edges[ei]
        seen: dict[str, _Crossing] = {}
        for c in edge_crossings:
            seen.setdefault(c.crossed_id, c)
        src_station = graph.stations.get(edge.source)
        tgt_station = graph.stations.get(edge.target)
        left_to_right = (
            src_station is None or tgt_station is None or src_station.x <= tgt_station.x
        )
        ordered = sorted(
            seen.values(), key=lambda c: c.cross_x, reverse=not left_to_right
        )

        edges_to_remove.add(ei)
        prev = edge.source
        for c in ordered:
            count += 1
            v_id = f"{BYPASS_V_PREFIX}{c.crossed_id}_{c.pred_id}_{count}"
            new_stations.append(
                Station(
                    id=v_id,
                    label="",
                    section_id=c.section_id,
                    is_hidden=True,
                    bypasses_station_id=c.crossed_id,
                )
            )
            new_edges.append(Edge(source=prev, target=v_id, line_id=edge.line_id))
            prev = v_id
        new_edges.append(Edge(source=prev, target=edge.target, line_id=edge.line_id))

    for st in new_stations:
        graph.register_station(st)
    graph.replace_edges(
        [e for i, e in enumerate(graph.edges) if i not in edges_to_remove]
    )
    for edge in new_edges:
        graph.add_edge(edge)
    return [st.id for st in new_stations]


def _remove_helpers(
    graph: MetroGraph, helper_ids: list[str], saved_edges: list[Edge]
) -> None:
    """Undo :func:`_insert_helpers`: drop the helpers and restore the edges."""
    for v_id in helper_ids:
        station = graph.stations.pop(v_id, None)
        if station is None or station.section_id is None:
            continue
        section = graph.sections.get(station.section_id)
        if section is not None and v_id in section.station_ids:
            section.station_ids.remove(v_id)
    graph.replace_edges(list(saved_edges))


def apply_geometric_bypass(
    graph: MetroGraph, layout_pass: Callable[[bool], None]
) -> tuple[bool, int]:
    """Bow any drawn non-consumer marker crossing the idiom can cleanly fix.

    ``layout_pass(validate)`` re-runs the layout body on ``graph``.  Returns
    ``(changed, residual_breeze)``: ``changed`` is True when helpers were
    inserted and kept (the graph is left in the re-laid-out state), and
    ``residual_breeze`` is the count of non-consumer crossings remaining in
    the final state - non-zero when a crossing exists that the idiom could not
    express or whose bypass was reverted as no improvement.
    """
    before = _evaluate(graph)
    if not before.crossings:
        return False, before.breeze_total

    saved_edges = list(graph.edges)
    helper_ids = _insert_helpers(graph, before.crossings)
    layout_pass(False)
    after = _evaluate(graph)
    if _is_improvement(before, after):
        return True, after.breeze_total

    _remove_helpers(graph, helper_ids, saved_edges)
    layout_pass(False)
    return False, before.breeze_total
