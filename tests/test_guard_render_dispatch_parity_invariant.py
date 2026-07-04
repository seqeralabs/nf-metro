"""Guard-path routing must dispatch identically to the render (#1319).

The validate-strict layout guards inspect routes produced by
:func:`nf_metro.layout.phases.guards._ensure_routes`.  The SVG render draws
routes produced by ``route_edges_centred(graph, station_offsets=...)``.  If the
guard path routes with a *different* ``station_offsets`` argument than the
render, offset-gated dispatch predicates (e.g. ``_InterFacts.is_tb_bottom_exit``
requires ``bool(ctx.station_offsets)``) fire differently, so a *different*
handler routes the same edge in each path.  Then "passes ``validate=True``"
stops meaning "renders clean": the guard can flag a crossing the user never
sees, or pass a route the render draws badly.

The invariant here is offset-magnitude-independent: it compares the *topology*
(the H/V/D segment-direction sequence, which is exactly what dispatch selects)
of every edge's guard-path route against its render-path route.  The
TB-bottom-exit class is the sharp case: without offsets present the guard would
draw an ``H,V,H`` dog-leg while the render drops straight ``V``, so the two
paths must route with matching offset presence to dispatch identically.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import parse_and_layout

from nf_metro.layout.constants import SAME_COORD_TOLERANCE, resolve_offset_step
from nf_metro.layout.phases.guards import _ensure_routes
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.themes import THEMES

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

# Fixtures spanning the offset-gated dispatch classes: TB bottom-exit straight
# drops, LR->TB top entries, TB side-exits, plus general multi-section gallery
# graphs as a control that the parity holds broadly, not only in the TB classes.
_FIXTURES = [
    "topologies/tb_bottom_exit_bundle_jog.mmd",
    "topologies/tb_bottom_exit_fork_diamond.mmd",
    "topologies/tb_column_continuation_two_lines.mmd",
    "topologies/tb_right_entry_stack.mmd",
    "topologies/tb_lr_exit_left.mmd",
    "topologies/tb_lr_exit_right.mmd",
    "topologies/fold_fan_across.mmd",
    "topologies/fold_split_targets.mmd",
    "topologies/lr_top_entry_cross_column.mmd",
    "rnaseq_sections.mmd",
    "rnaseq_auto.mmd",
]


def _segment_dirs(points: list[tuple[float, float]]) -> tuple[str, ...]:
    """The H/V/D direction sequence of a polyline (its dispatch fingerprint)."""
    dirs: list[str] = []
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        dx, dy = x2 - x1, y2 - y1
        if abs(dx) < SAME_COORD_TOLERANCE and abs(dy) < SAME_COORD_TOLERANCE:
            continue
        if abs(dy) < SAME_COORD_TOLERANCE:
            dirs.append("H")
        elif abs(dx) < SAME_COORD_TOLERANCE:
            dirs.append("V")
        else:
            dirs.append("D")
    return tuple(dirs)


def _by_edge(routes: list[RoutedPath]) -> dict[tuple[str, str, str], RoutedPath]:
    return {(r.edge.source, r.edge.target, r.line_id): r for r in routes}


@pytest.mark.parametrize("fixture", _FIXTURES)
def test_guard_and_render_routes_dispatch_identically(fixture: str) -> None:
    graph = parse_and_layout((EXAMPLES / fixture).read_text(), validate=False)

    guard_routes = _by_edge(_ensure_routes(graph, None))

    theme = THEMES["nfcore"]
    render_offsets = compute_station_offsets(
        graph, offset_step=resolve_offset_step(graph.track_gap, theme.line_width)
    )
    render_routes = _by_edge(route_edges(graph, station_offsets=render_offsets))

    mismatches: list[str] = []
    for key, guard_rp in guard_routes.items():
        render_rp = render_routes.get(key)
        if render_rp is None:
            mismatches.append(f"{key[2]}: {key[0]}->{key[1]} missing from render path")
            continue
        gd, rd = _segment_dirs(guard_rp.points), _segment_dirs(render_rp.points)
        if gd != rd:
            mismatches.append(f"{key[2]}: {key[0]}->{key[1]} guard {gd} != render {rd}")

    assert not mismatches, (
        f"{fixture}: guard-path routing diverges from render-path dispatch "
        f"(#1319):\n  " + "\n  ".join(mismatches)
    )
