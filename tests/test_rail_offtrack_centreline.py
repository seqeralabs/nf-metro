"""Rail-mode off-track elbows build their corner through the bundle builder.

In rail mode an off-track source/target drops straight down to a rail, turns
once, and runs flat into the consumer (``route_rail_edges`` in
``routing/rail.py``).  Sibling feeder lines feeding one consumer drop on
staggered Xs *centred on the feeder*, so a multi-line bundle straddles the bend:
one line lands on the inside of the turn.

A corner sized from the feeder centre (reference-anchored) pinches that
inside-of-turn line's arc below the floor radius.  Routed through
:func:`build_tapered_bundle` the corner anchors on the bundle's
innermost-of-turn line instead, so the innermost arc lands at the floor and the
rest fan outward -- no arc below the floor.  Unlike the TB perp-entry fan, the
rail stagger is centred, so a real multi-line off-track bundle straddles the
bend naturally; no synthetic fan is injected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import CURVE_RADIUS
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import assert_render_curve_invariants
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
TOPOLOGIES = EXAMPLES / "topologies"

# Rail-mode fixtures carrying a multi-line off-track bundle that feeds one
# consumer -- the centred stagger straddles the bend, so the inside line pinches
# on a reference-anchored corner.
_OFFTRACK_FIXTURES = ["rail_mode", "rail_offtrack_fan"]


def _find_fixture(stem: str) -> Path:
    for d in (EXAMPLES, TOPOLOGIES):
        p = d / f"{stem}.mmd"
        if p.exists():
            return p
    raise FileNotFoundError(stem)


def _offtrack_elbow_bundles(graph, routes):
    """Group off-track elbow routes by their (source, target) endpoint pair."""
    groups: dict[tuple[str, str], list] = {}
    for r in routes:
        e = r.edge
        src = graph.stations.get(e.source)
        tgt = graph.stations.get(e.target)
        if src is None or tgt is None:
            continue
        if (src.off_track and not tgt.off_track) or (
            tgt.off_track and not src.off_track
        ):
            groups.setdefault((e.source, e.target), []).append(r)
    return groups


@pytest.mark.parametrize("stem", _OFFTRACK_FIXTURES)
def test_rail_offtrack_elbow_clears_floor(stem: str) -> None:
    """Every off-track elbow arc lands at or above the floor radius.

    The inside-of-turn line of a straddling off-track bundle anchors at
    ``CURVE_RADIUS`` and the rest fan outward.  A corner sized from the feeder
    centre instead pinches the inside line below the floor.
    """
    path = _find_fixture(stem)
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    routes = route_edges(graph)

    groups = _offtrack_elbow_bundles(graph, routes)
    multi = {k: v for k, v in groups.items() if len({r.line_id for r in v}) > 1}
    assert multi, f"{stem}: expected a multi-line off-track bundle"

    offenders = [
        (k, r.line_id, [round(x, 2) for x in r.curve_radii])
        for k, v in multi.items()
        for r in v
        if r.curve_radii and any(x < CURVE_RADIUS - 0.01 for x in r.curve_radii)
    ]
    assert not offenders, f"{stem}: off-track elbows below floor: {offenders}"


@pytest.mark.parametrize("stem", _OFFTRACK_FIXTURES)
def test_rail_offtrack_render_is_clean(stem: str) -> None:
    """The naturally-routed rail render satisfies the render curve invariants."""
    path = _find_fixture(stem)
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    assert_render_curve_invariants(graph, routes, offsets)
    bundles = _offtrack_elbow_bundles(graph, routes)
    assert bundles, f"{stem}: expected routed off-track elbow edges"
    assert all(r.offsets_applied for v in bundles.values() for r in v)
