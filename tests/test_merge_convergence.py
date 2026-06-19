"""Same-line merge feeders converge as a single stroke.

A merge junction has N>1 feeders of one metro line converging on a single
entry port.  The farthest feeder carries a full bypass to the entry (the
"trunk"); every other feeder is a branch that descends onto the trunk's bypass
channel, so the converging line is a single stroke up to the point it genuinely
diverges.  Two invariants pin that: no two same-line feeders run offset-parallel
(which would draw as duplicate tracks, or abort the render when their descents
land an offset-step apart), and each non-trunk feeder terminates on the trunk's
channel rather than reaching the entry port on an independent path.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest

from nf_metro.layout.constants import (
    COORD_TOLERANCE,
    CURVE_RADIUS,
    DIAGONAL_RUN,
    EDGE_TO_BUNDLE_CLEARANCE,
)
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.context import _build_routing_context, _resolve_section_col
from nf_metro.layout.routing.invariants import check_no_same_line_parallel_descents
from nf_metro.layout.routing.normalize import (
    _final_port_approach,
    _initial_fanout_descent,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

_ROOT = Path(__file__).resolve().parents[1]
_TOPOLOGIES = _ROOT / "examples" / "topologies"

_FIXTURES = {
    name: (_TOPOLOGIES / f"{name}.mmd").read_text()
    for name in ("merge_bottom_row_bypass", "merge_pullaway", "merge_right_entry")
}


def _layout_and_route(mmd: str):
    graph = parse_metro_mermaid(mmd)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    ctx = _build_routing_context(graph, DIAGONAL_RUN, CURVE_RADIUS, offsets)
    return graph, routes, offsets, ctx


@pytest.mark.parametrize("name", sorted(_FIXTURES))
def test_no_same_line_parallel_merge_descents(name: str) -> None:
    """No two same-line feeders of a merge run offset-parallel on the V axis."""
    graph, routes, offsets, _ctx = _layout_and_route(_FIXTURES[name])
    violations = check_no_same_line_parallel_descents(graph, routes, offsets)
    assert not violations, "\n".join(v.message() for v in violations)


@pytest.mark.parametrize("name", sorted(_FIXTURES))
def test_merge_branches_join_trunk_channel(name: str) -> None:
    """Each non-trunk feeder terminates on the trunk's bypass channel.

    A branch that ends at the channel ``Y`` (``trunk_by``) has dropped onto the
    trunk to travel as one stroke; one that ends elsewhere (at the entry port,
    or looping around below) is a second independent stroke into the merge.
    """
    graph, routes, _offsets, ctx = _layout_and_route(_FIXTURES[name])
    by_key = {(r.edge.source, r.edge.target, r.line_id): r for r in routes}
    checked = 0
    for mjid, trunk_src in ctx.merge.trunk_source.items():
        trunk_by = ctx.merge.trunk_by[mjid]
        for e in graph.edges_to(mjid):
            if e.source == trunk_src:
                continue
            rp = by_key.get((e.source, e.target, e.line_id))
            if rp is None:
                continue
            checked += 1
            end_y = rp.points[-1][1]
            assert abs(end_y - trunk_by) <= COORD_TOLERANCE, (
                f"{name}: non-trunk feeder {e.source}->{mjid} ends at "
                f"y={end_y:.1f}, not on the trunk channel y={trunk_by:.1f} -- "
                "routed as a separate stroke rather than joining the trunk"
            )
    assert checked, f"{name}: expected at least one non-trunk merge feeder"


@pytest.mark.parametrize("name", sorted(_FIXTURES))
def test_feeder_descent_snaps_only_in_trunk_column(name: str) -> None:
    """A feeder's opening descent fuses with the trunk's only in its column.

    ``_coincide_same_line_tracks`` snaps a feeder onto the trunk's exact
    descent X only when the feeder shares the trunk's source column; a feeder in
    another column descends in its own inter-column gap and converges along the
    shared horizontal channel instead.  Pins that scope so the pass cannot
    broaden to collapse genuinely distinct corridors onto one channel.
    """
    graph, routes, _offsets, ctx = _layout_and_route(_FIXTURES[name])
    by_key = {(r.edge.source, r.edge.target, r.line_id): r for r in routes}
    seen = 0
    for mjid, trunk_src in ctx.merge.trunk_source.items():
        trunk_rp = next(
            (
                by_key[(e.source, e.target, e.line_id)]
                for e in graph.edges_to(mjid)
                if e.source == trunk_src and (e.source, e.target, e.line_id) in by_key
            ),
            None,
        )
        trunk_ch = _initial_fanout_descent(trunk_rp) if trunk_rp else None
        if trunk_ch is None:
            continue
        trunk_col = _resolve_section_col(graph, graph.stations[trunk_src])
        for e in graph.edges_to(mjid):
            if e.source == trunk_src:
                continue
            rp = by_key.get((e.source, e.target, e.line_id))
            ch = _initial_fanout_descent(rp) if rp else None
            if ch is None:
                continue
            seen += 1
            same_col = (
                _resolve_section_col(graph, graph.stations[e.source]) == trunk_col
            )
            coincident = abs(ch.x - trunk_ch.x) <= COORD_TOLERANCE
            if same_col:
                assert coincident, (
                    f"{name}: same-column feeder {e.source} descends at "
                    f"x={ch.x:.1f}, not fused with trunk descent x={trunk_ch.x:.1f}"
                )
            else:
                assert not coincident, (
                    f"{name}: cross-column feeder {e.source} was snapped onto the "
                    f"trunk descent x={trunk_ch.x:.1f}; distinct corridors collapsed"
                )
    assert seen, f"{name}: expected at least one non-trunk merge feeder"


@pytest.mark.parametrize("name", sorted(_FIXTURES))
def test_same_line_port_approaches_coincide(name: str) -> None:
    """Same-line vertical approaches converging on one entry port share an X.

    The merge trunk ends at the entry port carrying the merge junction as its
    edge target; a same-line feed arriving directly at that port (an exit-port
    source not folded into the merge) must share the trunk's final riser rather
    than running an offset apart beside it.
    """
    _graph, routes, _offsets, ctx = _layout_and_route(_FIXTURES[name])
    by_port: dict[tuple[str, str, bool], list[float]] = defaultdict(list)
    for rp in routes:
        if not rp.is_inter_section:
            continue
        ch = _final_port_approach(rp)
        if ch is None:
            continue
        target = ctx.merge.entry_port_for.get(rp.edge.target, rp.edge.target)
        by_port[(target, rp.line_id, ch.down)].append(ch.x)
    for (target, line, _down), xs in by_port.items():
        # Consecutive same-line approaches to one port must be either coincident
        # (one fused track) or genuinely distant (separate corridors beyond the
        # fuse band); a small offset between them is the duplicate-riser defect.
        for a, b in zip(sorted(xs), sorted(xs)[1:]):
            gap = b - a
            assert gap <= COORD_TOLERANCE or gap > EDGE_TO_BUNDLE_CLEARANCE, (
                f"{name}: line {line!r} approaches port {target!r} on two "
                f"near-parallel risers (x={a:.1f}, {b:.1f}; {gap:.1f}px apart) "
                "instead of one fused track"
            )
