"""The multi-line intra-section perpendicular exit is built via the bundle builder.

An internal station feeding a TOP/BOTTOM exit port on a horizontal-flow section
runs along the trunk, turns once past the trailing station, and leaves
vertically.  When several lines co-travel that edge the turn must fan them as a
concentric bundle: the line deepest inside the bend sits at the floor radius and
the rest fan outward, so no arc pinches below the floor.

``_route_intra_section`` routes that bundle through ``build_tapered_bundle``,
which anchors every corner on the bundle's innermost-of-turn line from the
declared fan.  These tests pin that on every fixture whose internal flow exits
through a perpendicular port, by routing the arm with a wide co-travelling fan
that straddles the turn (so one line lands inside the bend) and asserting the
inside-of-turn arc clears the floor radius.  A corner sized from the raw port
centre rather than the bundle's innermost line pinches the inside line to a
zero-radius kink, which these assertions reject.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import CURVE_RADIUS
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.context import _build_routing_context
from nf_metro.layout.routing.intra_handlers import _route_intra_section
from nf_metro.layout.routing.invariants import (
    assert_render_curve_invariants,
    check_bundle_order_preserved,
    check_concentric_bundle_corners,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import PortSide

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"

# Fixtures whose intra-section flow exits through a perpendicular (TOP/BOTTOM)
# port carrying more than one co-travelling line -- the multi-sibling arm.
PERP_EXIT_FIXTURES = [
    EXAMPLES / "topologies" / "lr_perp_top_exit_perp_entry.mmd",
    EXAMPLES / "topologies" / "lr_perp_top_exit_side_entry.mmd",
    EXAMPLES / "topologies" / "lr_perp_top_exit_perp_entry_diverging.mmd",
    EXAMPLES / "topologies" / "lr_perp_bottom_exit_perp_entry.mmd",
    EXAMPLES / "topologies" / "lr_perp_bottom_exit_side_entry.mmd",
    EXAMPLES / "topologies" / "lr_to_tb_top_drop_two_lines.mmd",
]

# A co-travelling fan wider than the floor radius and straddling zero, so one
# line lands inside the bend.  Real layouts anchor their fans at the innermost
# line (offsets >= 0), so the inside-of-turn case never arises naturally; this
# manufactures it to exercise the arm's fan.
_FAN_STEP = 20.0


def _arm_groups(graph):
    """The multi-sibling perp-exit edges, grouped by ``(source, target)``.

    An internal (non-port) station feeding a TOP/BOTTOM exit port on an LR/RL
    section with more than one co-travelling line: the shape ``_route_intra_section``
    routes through the bundle builder.
    """
    groups: dict[tuple[str, str], list] = {}
    for edge in graph.edges:
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if not src or not tgt:
            continue
        port = graph.ports.get(tgt.id)
        section = graph.sections.get(src.section_id) if src.section_id else None
        siblings = sum(
            1 for e in graph.edges_from(edge.source) if e.target == edge.target
        )
        if (
            not src.is_port
            and port is not None
            and not port.is_entry
            and port.side in (PortSide.TOP, PortSide.BOTTOM)
            and section is not None
            and section.direction in ("LR", "RL")
            and siblings > 1
        ):
            groups.setdefault((edge.source, edge.target), []).append(edge)
    return groups


def _route_arm_with_fan(graph, source, target, edges):
    """Route the arm's bundle with a manufactured straddling fan.

    Assigns each co-travelling line a signed offset centred on zero at both the
    source station and the exit port, then routes every member through
    ``_route_intra_section`` with that fan in the context.
    """
    line_ids = list(dict.fromkeys(e.line_id for e in edges))
    n = len(line_ids)
    offsets: dict[tuple[str, str], float] = {}
    for j, line_id in enumerate(line_ids):
        d = (j - (n - 1) / 2) * _FAN_STEP
        offsets[(source, line_id)] = d
        offsets[(target, line_id)] = d
    ctx = _build_routing_context(graph, 30.0, CURVE_RADIUS, offsets)
    routes = [
        _route_intra_section(e, graph.stations[e.source], graph.stations[e.target], ctx)
        for e in edges
    ]
    return offsets, routes


@pytest.mark.parametrize("path", PERP_EXIT_FIXTURES, ids=lambda p: p.stem)
def test_intra_perp_exit_corner_clears_floor_when_fanned(path: Path) -> None:
    """A fanned multi-line perp exit keeps every corner at or above the floor.

    The inside-of-turn line anchors at ``CURVE_RADIUS`` and the rest fan
    outward.  A corner anchored on the raw port centre instead pinches the
    inside line below the floor (to a zero-radius kink).
    """
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    groups = _arm_groups(graph)
    assert groups, f"{path.stem}: expected a multi-sibling perp-exit arm"
    for (source, target), edges in groups.items():
        offsets, routes = _route_arm_with_fan(graph, source, target, edges)
        offenders = [
            (r.line_id, r.curve_radii)
            for r in routes
            if r.curve_radii
            and any(radius < CURVE_RADIUS - 0.01 for radius in r.curve_radii)
        ]
        assert not offenders, (
            f"{path.stem} {source}->{target}: perp-exit corners below the floor: "
            f"{offenders}"
        )


@pytest.mark.parametrize("path", PERP_EXIT_FIXTURES, ids=lambda p: p.stem)
def test_intra_perp_exit_corner_is_concentric_when_fanned(path: Path) -> None:
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    groups = _arm_groups(graph)
    assert groups, f"{path.stem}: expected a multi-sibling perp-exit arm"
    for (source, target), edges in groups.items():
        offsets, routes = _route_arm_with_fan(graph, source, target, edges)
        assert check_concentric_bundle_corners(graph, routes, offsets) == []
        assert check_bundle_order_preserved(routes) == []


@pytest.mark.parametrize("path", PERP_EXIT_FIXTURES, ids=lambda p: p.stem)
def test_intra_perp_exit_natural_render_is_clean(path: Path) -> None:
    """The naturally-routed render has concentric, offset-baked perp-exit corners."""
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    assert check_concentric_bundle_corners(graph, routes, offsets) == []
    assert_render_curve_invariants(graph, routes, offsets)
    arm_targets = {t for (_s, t) in _arm_groups(graph)}
    arm_routes = [r for r in routes if r.edge.target in arm_targets]
    assert arm_routes, f"{path.stem}: expected routed perp-exit arm edges"
    assert all(r.offsets_applied for r in arm_routes)
