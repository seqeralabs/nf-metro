"""A reverse-flow bypass into a far-side LEFT entry wraps around below cleanly.

When the source sits to the RIGHT of its target and exits its LEFT edge, while
the target's entry port is on its own far (LEFT) edge, the bundle leaves the
exit travelling west, drops below every intervening section, and rises into the
port from its outward side -- a net half-turn that transposes the bundle
end-to-end.  ``_route_left_exit_around_below_left_entry`` routes that wrap and
``_reverse_around_below_left_entry_offsets`` reverses the destination section's
line order to match, so the cable order dictates the destination order and no
line crosses a bundle-mate.

On ``main`` this topology aborts the render-time curve invariants (the U-shaped
bypass rakes its delivery through the target interior and inverts the bundle at
the lead-in corner); these tests pin the clean wrap.
"""

from __future__ import annotations

from pathlib import Path

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import (
    assert_render_curve_invariants,
    check_bundle_order_preserved,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "examples" / "topologies" / "bypass_leftward_far_side_entry.mmd"


def _route(path: Path):
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return graph, offsets, routes


def test_far_side_wrap_passes_curve_invariants() -> None:
    """The wrap is concentric and unflipped (aborts on main)."""
    graph, offsets, routes = _route(FIXTURE)
    assert check_bundle_order_preserved(routes) == []
    assert_render_curve_invariants(graph, routes, offsets)


def test_far_side_wrap_stays_within_canvas() -> None:
    """The wrap rises left of the leftmost section; the layout reserves that
    clearance so it does not run off the left canvas edge (x < 0)."""
    _graph, _offsets, routes = _route(FIXTURE)
    leftmost = min(px for r in routes for px, _py in r.points)
    assert leftmost >= 0.0, f"wrap runs off the canvas at x={leftmost}"


def test_far_side_wrap_delivers_each_line_to_its_entry_offset() -> None:
    """Each wrapped line lands on its entry-port offset, so it flows into the
    (reversed) trunk without a kink at the port."""
    graph, offsets, routes = _route(FIXTURE)
    port = graph.stations["tgt_sec__entry_left_1"]
    wraps = [
        r
        for r in routes
        if r.edge.source == "src_sec__exit_left_0"
        and r.edge.target == "tgt_sec__entry_left_1"
    ]
    assert len(wraps) == 7
    for r in wraps:
        expected = port.y + offsets[(port.id, r.line_id)]
        assert abs(r.points[-1][1] - expected) < 0.5, (
            f"{r.line_id} lands at {r.points[-1][1]}, expected {expected}"
        )


def test_far_side_wrap_arrival_order_is_monotonic() -> None:
    """Lines arrive at the port stacked monotonically by entry offset: the wrap
    transposes the bundle as a whole rather than letting any pair cross."""
    graph, offsets, routes = _route(FIXTURE)
    port = graph.stations["tgt_sec__entry_left_1"]
    wraps = sorted(
        (
            r
            for r in routes
            if r.edge.source == "src_sec__exit_left_0"
            and r.edge.target == "tgt_sec__entry_left_1"
        ),
        key=lambda r: offsets[(port.id, r.line_id)],
    )
    arrival_y = [r.points[-1][1] for r in wraps]
    assert arrival_y == sorted(arrival_y), arrival_y


def test_far_side_wrap_stays_clear_of_target_interior() -> None:
    """No wrap segment crosses the target box left of the entry port: the bundle
    enters at the port, never raking through the interior."""
    graph, _offsets, routes = _route(FIXTURE)
    sec = graph.sections["tgt_sec"]
    left, right = sec.bbox_x, sec.bbox_x + sec.bbox_w
    top, bottom = sec.bbox_y, sec.bbox_y + sec.bbox_h
    for r in routes:
        if r.edge.target != "tgt_sec__entry_left_1":
            continue
        for (x0, y0), (x1, y1) in zip(r.points, r.points[1:]):
            if abs(y0 - y1) >= 0.01:  # only horizontal segments
                continue
            if not (top - 0.01 <= y0 <= bottom + 0.01):
                continue
            # A horizontal run in the box's y-band must terminate at the
            # left-edge port (x == left), never penetrating the interior -- the
            # original bug raked from the right boundary across the box to reach
            # the far-side port.  Segments wholly outside the x-extent (the
            # source-side lead-out, which shares this row's y-band) are ignored.
            if min(x0, x1) < right - 0.5:
                assert max(x0, x1) <= left + 0.5, (
                    f"{r.line_id} horizontal at y={y0} rakes into "
                    f"[{left},{right}] reaching x={max(x0, x1)}"
                )
