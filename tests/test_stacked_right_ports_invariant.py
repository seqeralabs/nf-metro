"""Routing invariant: a RIGHT exit feeding a RIGHT entry stacked in the same
grid column must bow out into the descent channel, not drop as a bare vertical.

When both sections sit in one grid column their right-facing ports pin to the
column's shared right edge, so the exit and entry land at the same X.  A
straight vertical connector between them leaves the RIGHT exit travelling
downward (not out its outward side) and enters the RIGHT port from directly
above (not from the right), skipping both outward corner curves; a second feed
terminating at the same entry port is then forced onto its own parallel channel
instead of sharing one descent.

The feed must instead leave the exit rightward, descend the channel past the
port's outward edge, and turn in from the right, so co-terminating feeds bundle
into that one channel.

Regression lock for #1398.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import BUNDLE_TO_BUNDLE_CLEARANCE
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import apply_route_offsets
from nf_metro.layout.routing.invariants import check_stacked_right_ports_bow_out
from nf_metro.parser.mermaid import parse_metro_mermaid

TOPOLOGIES_DIR = Path(__file__).parent.parent / "examples" / "topologies"

# Every topology fixture: the check must never flag one, so any stacked
# RIGHT-exit -> RIGHT-entry feed that arises across the corpus is exercised.
_ALL_FIXTURES = sorted(p.stem for p in TOPOLOGIES_DIR.glob("*.mmd"))

_COINCIDENT = "stacked_right_ports_coincident"


def _routed(mmd: str):
    graph = parse_metro_mermaid(mmd)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    return graph, route_edges(graph, station_offsets=offsets), offsets


@pytest.mark.parametrize("stem", _ALL_FIXTURES)
def test_no_bare_vertical_between_stacked_right_ports(stem):
    graph, routes, offsets = _routed((TOPOLOGIES_DIR / f"{stem}.mmd").read_text())
    violations = check_stacked_right_ports_bow_out(graph, routes, offsets)
    assert not violations, "; ".join(v.message() for v in violations)


def test_coincident_feed_bows_out_with_outward_corners():
    """Both feeds into the stacked RIGHT entry leave/enter through the channel.

    The stacked feed leaves its exit rightward and turns into the port from the
    right (outward corner curves at both ends), and the co-terminating feed
    descends within a bundle width of it, so the two share one channel rather
    than running as separate parallel verticals.
    """
    mmd = (TOPOLOGIES_DIR / f"{_COINCIDENT}.mmd").read_text()
    graph, routes, offsets = _routed(mmd)

    inter = {
        (r.edge.source, r.edge.target): apply_route_offsets(r, offsets)
        for r in routes
        if r.is_inter_section
    }
    stacked = inter[("above__exit_right_1", "below__entry_right_2")]
    sibling = inter[("feeder__exit_right_0", "below__entry_right_2")]

    # Leaves the RIGHT exit rightward and turns in from the RIGHT of the port.
    assert stacked[1][0] > stacked[0][0] + 1.0, stacked
    assert stacked[-2][0] > stacked[-1][0] + 1.0, stacked

    # The two feeds' descent channels sit within a bundle width of each other:
    # one shared channel, not two far-apart parallel verticals.
    assert abs(stacked[-2][0] - sibling[-2][0]) <= BUNDLE_TO_BUNDLE_CLEARANCE, (
        stacked,
        sibling,
    )


def test_convergent_feeds_port_corners_nest_concentrically():
    """The two feeds' port-approach corners nest instead of pinching.

    Built as one shared bundle, the outer feed (the descent further from the
    port) takes the larger corner radius, so the two arcs are concentric and
    hold a constant gap through the turn.  Equal radii on centres a step apart
    would pinch the gap through the corner.
    """
    mmd = (TOPOLOGIES_DIR / f"{_COINCIDENT}.mmd").read_text()
    graph, routes, offsets = _routed(mmd)

    feeds = [
        r
        for r in routes
        if r.is_inter_section and r.edge.target == "below__entry_right_2"
    ]
    assert len(feeds) == 2, feeds
    # Rank by the final descent X: for a RIGHT entry the outer feed descends
    # further right (larger X), so it must carry the larger port-turn radius.
    outer, inner = sorted(feeds, key=lambda r: apply_route_offsets(r, offsets)[-2][0])[
        ::-1
    ]
    assert outer.curve_radii[-1] > inner.curve_radii[-1], (
        outer.curve_radii,
        inner.curve_radii,
    )
