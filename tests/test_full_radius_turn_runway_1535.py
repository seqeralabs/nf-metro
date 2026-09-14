"""Every inter-section turn in the example corpus rounds at its full radius.

`resolve_curve_radii` clamps a corner to the run its adjacent segments leave,
and the renderer draws the arc from that clamped value, so a corner seated
closer than its radius to what it turns beside draws tighter than the bundle's
concentric spacing intends.

The corpus sweep here is a **ratchet, not a live invariant**: the render-time
guard (`check_orthogonal_turns_form_curves`) holds turns to a floor rather than
to their full radius, because some topologies -- an L-shape whose two section
rows sit closer than two curve radii apart -- cannot reach full radius without
layout-level spacing, and aborting a render over a merely-tight arc would be
worse than drawing it.  Holding the corpus to the stricter bar keeps the engine
from regressing where it already achieves it.

The seatings that starved the runway are pinned individually below, so a
regression names its own cause rather than surfacing as a clamped corner
somewhere downstream.
"""

from __future__ import annotations

import glob
from pathlib import Path

import pytest

from nf_metro.layout.constants import CURVE_RADIUS, OFFSET_STEP
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.layout.routing.common import (
    RoutedPath,
    column_gap_edges,
    is_orthogonal_turn,
)
from nf_metro.layout.routing.corners import outer_lane_radius, resolve_curve_radii
from nf_metro.parser.mermaid import parse_metro_mermaid

ROOT = Path(__file__).resolve().parent.parent
TOPOLOGIES = ROOT / "examples" / "topologies"

CORPUS = sorted(glob.glob(str(ROOT / "examples" / "**" / "*.mmd"), recursive=True))
CORPUS_IDS = [Path(p).stem for p in CORPUS]

_RADIUS_TOL = 0.5


def _route(path: Path) -> tuple:
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    return graph, route_edges_centred(graph, station_offsets=offsets)


def _clamped_turns(routes: list[RoutedPath]) -> list[str]:
    """Every inter-section H/V turn whose arc falls short of its own request.

    Stricter than the render-time guard, which compares against a floor: this
    reports any shortfall from the radius the bundle's concentric geometry asked
    for at that corner.
    """
    short: list[str] = []
    for rp in routes:
        if not rp.is_inter_section or len(rp.points) < 3:
            continue
        for k, eff in enumerate(resolve_curve_radii(rp.points, rp.curve_radii)):
            i = k + 1
            if not is_orthogonal_turn(rp.points[i - 1], rp.points[i], rp.points[i + 1]):
                continue
            radii = rp.curve_radii
            requested = (
                radii[k]
                if radii and k < len(radii) and radii[k] is not None
                else CURVE_RADIUS
            )
            if eff < requested - _RADIUS_TOL:
                x, y = rp.points[i]
                short.append(
                    f"{rp.edge.source}->{rp.edge.target} [{rp.line_id}] at "
                    f"({x:.0f},{y:.0f}): radius {eff:.1f} of requested {requested:.1f}"
                )
    return short


@pytest.mark.parametrize("path", CORPUS, ids=CORPUS_IDS)
def test_corpus_turns_round_at_full_radius(path: str) -> None:
    _graph, routes = _route(Path(path))
    short = _clamped_turns(routes)
    assert not short, "turns clamped below their requested radius:\n" + "\n".join(short)


def test_column_gap_edges_reports_no_gap_where_lower_column_is_absent() -> None:
    """A row the lower column does not occupy bounds no gap in that row.

    ``merge_offrow_continuation`` has no section in column 1 of row 0, so the
    gap between columns 1 and 2 does not exist there.  Reporting one bounded at
    the canvas origin would span two real columns of the map, and any channel
    inside that span would match it.
    """
    graph = parse_metro_mermaid(
        (TOPOLOGIES / "merge_offrow_continuation.mmd").read_text()
    )
    compute_layout(graph)
    left, right = column_gap_edges(graph, 1, 2, row=0)
    assert right <= left, f"fabricated gap [{left:.0f}..{right:.0f}] in an empty cell"
    # The same gap over every row it does exist in is bounded by real edges.
    left_all, right_all = column_gap_edges(graph, 1, 2, row=None)
    assert right_all > left_all
    sink = graph.sections["sink"]
    assert left_all == pytest.approx(sink.bbox_x + sink.bbox_w)


def test_bypass_descent_keeps_its_runway_into_the_entry_port() -> None:
    """The off-row bypass turns into ``sink``'s port a full radius clear of it.

    Its descent column sits in the gap one column to the left; seating it in the
    span the absent row-0 cell reported would leave a stub of runway for the turn
    at the bottom.
    """
    graph, routes = _route(TOPOLOGIES / "merge_offrow_continuation.mmd")
    port_x = graph.stations["sink__entry_left_3"].x
    bypass = next(
        rp
        for rp in routes
        if rp.edge.source == "above_src__exit_right_0" and rp.line_id == "bypass"
    )
    descent_x = bypass.points[-2][0]
    assert port_x - descent_x >= CURVE_RADIUS - _RADIUS_TOL, (
        f"descent column at x={descent_x:.0f} is {port_x - descent_x:.0f}px "
        f"from the port at x={port_x:.0f}"
    )


def test_bypass_trunk_step_keeps_its_runway_off_the_source_leg() -> None:
    """The step down onto ``reference``'s bypass trunk spans two full radii.

    The junction at ``input``'s RIGHT exit sits only a little above the
    clearance lane under ``assemble``, so the step onto that trunk is short.
    Two formed corners need ``2 * CURVE_RADIUS`` of vertical run between them,
    and a shorter step halves both radii to fit.
    """
    graph, routes = _route(TOPOLOGIES / "packed_cell_right_exit_left_entry_wrap.mmd")
    bypass = next(
        rp
        for rp in routes
        if rp.edge.source == "__junction_10" and rp.edge.target == "annot__entry_left_9"
    )
    source_leg_y, trunk_y = bypass.points[1][1], bypass.points[2][1]
    assert trunk_y - source_leg_y >= 2 * CURVE_RADIUS - _RADIUS_TOL, (
        f"trunk at y={trunk_y:.0f} steps only {trunk_y - source_leg_y:.0f}px "
        f"off a source leg at y={source_leg_y:.0f}"
    )


def test_bundle_lead_in_covers_the_widest_lane_arc() -> None:
    """A two-line drop leads out far enough for its outer lane's wider arc.

    Both lines leave ``src_sec``'s RIGHT exit and turn down toward ``tgt_sec``'s
    TOP entry.  The outer lane of that turn sweeps one ``OFFSET_STEP`` wider than
    the inner one, so the lead-in must run the *outer* lane's radius clear of the
    exit -- a lead sized to the base radius clamps both lanes to it.
    """
    graph, routes = _route(TOPOLOGIES / "lr_to_tb_top_two_lines.mmd")
    exit_x = graph.stations["src_sec__exit_right_0"].x
    drops = [rp for rp in routes if rp.edge.source == "src_sec__exit_right_0"]
    assert len(drops) == 2, "fixture must route two lines out of the RIGHT exit"
    widest = max(drops, key=lambda rp: rp.curve_radii[0])
    assert widest.curve_radii[0] == pytest.approx(
        outer_lane_radius(2, CURVE_RADIUS, OFFSET_STEP)
    )
    for rp in drops:
        lead = abs(rp.points[1][0] - exit_x)
        assert lead >= rp.curve_radii[0] - _RADIUS_TOL, (
            f"line {rp.line_id!r} leads out {lead:.0f}px before a corner "
            f"asking for {rp.curve_radii[0]:.0f}px"
        )
