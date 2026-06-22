"""TB LEFT-exit -> lower RIGHT-entry staircase: builder and #671 lock.

When a folded section flips to TB it gets a LEFT-edge exit port; a bundle
leaving it for a RIGHT-entry section below steps west -> down -> west (two
opposite-handed corners).  A concentric bundle inverts its nesting through
opposite turns and crosses the lines at the port, so the route is built as a
parallel staircase (per-leg offsets via :func:`build_offset_bundle`) that keeps
each line on the feed order at both ports.

Two layers:

* the always-on render-path guard :func:`assert_render_curve_invariants`
  rejects the inverted bundle the old wrap/L-shape produced;
* the ``tb_left_exit_step`` fixture lays out, routes, and renders with no curve
  defect, and the exit bundle keeps the same vertical order it is fed in.

See issue #671.
"""

from __future__ import annotations

from pathlib import Path

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import apply_route_offsets
from nf_metro.layout.routing.invariants import (
    assert_render_curve_invariants,
    check_bundle_order_preserved,
    check_concentric_bundle_corners,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

FIXTURE = (
    Path(__file__).parent.parent / "examples" / "topologies" / "tb_left_exit_step.mmd"
)

EXIT_PORT = "align__exit_left_1"
ENTRY_PORT = "post__entry_right_4"


def _laid_out():
    graph = parse_metro_mermaid(FIXTURE.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return graph, offsets, routes


def _baked(route, offsets):
    if route.offset_regime.name == "DEFERRED":
        return apply_route_offsets(route, offsets)
    return route.points


def test_fixture_renders_without_curve_defect():
    """The #671 lock: the folded-TB left-exit staircase has no flip or pinch.

    The old wrap/L-shape inverted the bundle through the step's opposite
    corners; ``assert_render_curve_invariants`` aborts the render on such a
    bundle, so a clean pass means the staircase keeps every line in order.
    """
    graph, offsets, routes = _laid_out()
    assert_render_curve_invariants(graph, routes, offsets)
    assert not check_bundle_order_preserved(routes)
    assert not check_concentric_bundle_corners(graph, routes, offsets)


def test_exit_bundle_keeps_feed_order_into_the_entry():
    """Each line leaves the exit port and enters the entry port on one side.

    The feed routes land at the exit port in a vertical order; the staircase
    must deliver them to the RIGHT entry in that same order, so no line crosses
    a bundle-mate.  The exit-port Y order and the entry-port Y order of the
    inter-section bundle must therefore agree.
    """
    _graph, offsets, routes = _laid_out()
    step = [
        r for r in routes if r.edge.source == EXIT_PORT and r.edge.target == ENTRY_PORT
    ]
    assert len(step) >= 2, "expected a multi-line exit bundle"

    exit_y = {r.line_id: _baked(r, offsets)[0][1] for r in step}
    entry_y = {r.line_id: _baked(r, offsets)[-1][1] for r in step}
    by_exit = sorted(exit_y, key=lambda lid: exit_y[lid])
    by_entry = sorted(entry_y, key=lambda lid: entry_y[lid])
    assert by_exit == by_entry, (
        f"exit order {by_exit} must match entry order {by_entry}; "
        "a mismatch means the staircase inverted the bundle"
    )


def test_exit_bundle_steps_through_a_descent_west_of_the_port():
    """The staircase descends on the port's outward side, never doubling back.

    The lines arrive at the left-edge exit port travelling west; the descent
    channel sits west of the port (no eastward U-turn), and the bundle reaches
    the entry from ``x >= entry.x`` (the RIGHT port's own outward side).
    """
    graph, offsets, routes = _laid_out()
    exit_port = graph.stations[EXIT_PORT]
    entry_port = graph.stations[ENTRY_PORT]
    step = [
        r for r in routes if r.edge.source == EXIT_PORT and r.edge.target == ENTRY_PORT
    ]
    for r in step:
        pts = _baked(r, offsets)
        descent_x = pts[1][0]
        assert descent_x <= exit_port.x + 1.0, (
            f"{r.line_id} descent x={descent_x:.1f} doubles back east of the "
            f"exit port x={exit_port.x:.1f}"
        )
        assert descent_x >= entry_port.x - 1.0, (
            f"{r.line_id} descent x={descent_x:.1f} runs left of the entry "
            f"port x={entry_port.x:.1f}, cutting the target box"
        )
