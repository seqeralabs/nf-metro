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

``rail_offtrack_fan`` carries a bundle in both directions: an off-track input
(consumer to the right, a down-then-right corner) and an off-track output
(consumer to the left, a left-then-up corner).  The drop order reverses for the
mirrored output corner, so the render-curve invariants catch a twist there too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import CURVE_RADIUS
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import (
    OffsetRegime,
    compute_station_offsets,
    route_edges,
)
from nf_metro.layout.routing.invariants import assert_render_curve_invariants
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
TOPOLOGIES = EXAMPLES / "topologies"

# Rail-mode fixture -> number of multi-line off-track bundles it must produce.
# The centred stagger straddles the bend, so the inside line pinches on a
# reference-anchored corner.  rail_offtrack_fan carries one in each direction
# (off-track input + off-track output) to lock both corner orientations.
_OFFTRACK_FIXTURES = {"rail_mode": 1, "rail_offtrack_fan": 2}


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


@pytest.mark.parametrize("stem,n_bundles", _OFFTRACK_FIXTURES.items())
def test_rail_offtrack_elbow_clears_floor(stem: str, n_bundles: int) -> None:
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
    assert len(multi) == n_bundles, (
        f"{stem}: expected {n_bundles} multi-line off-track bundle(s), got {len(multi)}"
    )

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
    assert all(
        r.offset_regime is OffsetRegime.BAKED for v in bundles.values() for r in v
    )
