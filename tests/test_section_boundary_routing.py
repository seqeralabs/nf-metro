"""Inter-section routes must not skirt or cross section boundaries (#724).

The discipline these tests pin for cross-row / merge / stacked geometries:

* a line never passes through the interior of a section it does not stop in
  (including the route's *own* target box reached on the far side), and
* every route that lands on a LEFT/RIGHT entry port approaches it from the
  port's own outward side, never by slicing across the box.

The checks read the final routed polylines directly, so they hold the
geometry to account regardless of which runtime guard (if any) covers a case.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout import compute_layout
from nf_metro.layout.geometry import segment_intersects_bbox
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.parser import parse_metro_mermaid
from nf_metro.parser.model import PortSide
from nf_metro.render.svg import apply_route_offsets

TOPOLOGIES_DIR = Path(__file__).parent.parent / "examples" / "topologies"

# Each exercises one cross-row / merge / stacked geometry from #724.
BOUNDARY_FIXTURES = [
    "stacked_left_exit_drop",
    "merge_leftmost_sink_branch",
    "merge_around_below_leftmost",
    "right_entry_from_above",
]
# Established geometries the same discipline must keep holding on.
CLEAN_FIXTURES = [
    "around_below_ep_col_gt0",
    "right_entry_gap_above_empty_row",
    "right_entry_wrap_no_fan",
]
# RIGHT entry fed from a higher row whose source sits past the target's right
# edge: a straight drop down the outward side reaches the entry Y unobstructed,
# so the route must turn in directly rather than loop below the box (#889).
DROP_IN_FIXTURES = [
    "right_entry_from_above",
    "right_entry_from_above_far",
]

TOL = 2.0


def _routed(stem):
    graph = parse_metro_mermaid((TOPOLOGIES_DIR / f"{stem}.mmd").read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return graph, offsets, routes


def _entry_port_at(graph, x, y):
    """The entry port a route ending at ``(x, y)`` lands on, or ``None``.

    Resolves a port even when the edge targets a virtual merge/junction node:
    such a route is extended to terminate on the section's entry-port station.
    """
    for port in graph.ports.values():
        if port.is_entry and abs(port.x - x) <= TOL and abs(port.y - y) <= TOL:
            return port
    return None


@pytest.mark.parametrize("stem", BOUNDARY_FIXTURES + CLEAN_FIXTURES)
def test_entry_ports_approached_from_outward_side(stem):
    """No route reaches a LEFT/RIGHT entry port by crossing the target box."""
    graph, offsets, routes = _routed(stem)
    for rp in routes:
        pts = apply_route_offsets(rp, offsets)
        if len(pts) < 2:
            continue
        port = _entry_port_at(graph, pts[-1][0], pts[-1][1])
        if port is None or port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        section = graph.sections.get(port.section_id) if port.section_id else None
        if section is None or section.bbox_w <= 0:
            continue
        prev = pts[-2]
        if abs(prev[1] - pts[-1][1]) > TOL:
            continue  # not a horizontal approach leg
        bx0 = section.bbox_x
        bx1 = bx0 + section.bbox_w
        if port.side is PortSide.RIGHT:
            assert prev[0] >= bx1 - TOL, (
                f"{stem}: {rp.edge.source}->{rp.edge.target} approaches RIGHT "
                f"entry {port.id} from inside the box (prev x={prev[0]:.1f} < "
                f"right edge {bx1:.1f})"
            )
        else:
            assert prev[0] <= bx0 + TOL, (
                f"{stem}: {rp.edge.source}->{rp.edge.target} approaches LEFT "
                f"entry {port.id} from inside the box (prev x={prev[0]:.1f} > "
                f"left edge {bx0:.1f})"
            )


@pytest.mark.parametrize("stem", BOUNDARY_FIXTURES + CLEAN_FIXTURES)
def test_routes_do_not_cross_unrelated_section_interiors(stem):
    """No routed segment passes through a section box it does not connect to."""
    graph, offsets, routes = _routed(stem)
    inset = 3.0
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
    for rp in routes:
        own = {
            graph.section_for_station(rp.edge.source),
            graph.section_for_station(rp.edge.target),
        }
        pts = apply_route_offsets(rp, offsets)
        for sid, x0, y0, x1, y1 in boxes:
            if sid in own:
                continue
            for i in range(len(pts) - 1):
                assert not segment_intersects_bbox(
                    pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], (x0, y0, x1, y1)
                ), (
                    f"{stem}: {rp.edge.source}->{rp.edge.target} crosses "
                    f"unrelated section {sid!r} interior"
                )


@pytest.mark.parametrize("stem", DROP_IN_FIXTURES)
def test_right_entry_from_above_drops_in_without_diving_below(stem):
    """A clear outward-side drop-in must not loop below the target box (#889)."""
    graph, offsets, routes = _routed(stem)
    checked = 0
    for rp in routes:
        pts = apply_route_offsets(rp, offsets)
        if len(pts) < 2:
            continue
        port = _entry_port_at(graph, pts[-1][0], pts[-1][1])
        if port is None or port.side is not PortSide.RIGHT:
            continue
        section = graph.sections.get(port.section_id) if port.section_id else None
        if section is None or section.bbox_h <= 0:
            continue
        box_bottom = section.bbox_y + section.bbox_h
        max_y = max(y for _, y in pts)
        assert max_y <= box_bottom + TOL, (
            f"{stem}: {rp.edge.source}->{rp.edge.target} dives to y={max_y:.1f}, "
            f"below the target box bottom {box_bottom:.1f}; a direct drop-in down "
            f"the outward side reaches the entry Y without looping under the box"
        )
        checked += 1
    assert checked, f"{stem}: no RIGHT entry route found to check"


@pytest.mark.parametrize("stem", BOUNDARY_FIXTURES)
def test_boundary_fixtures_pass_runtime_guards(stem):
    """The validate-block guards accept each repaired geometry."""
    graph = parse_metro_mermaid((TOPOLOGIES_DIR / f"{stem}.mmd").read_text())
    compute_layout(graph, validate=True)
