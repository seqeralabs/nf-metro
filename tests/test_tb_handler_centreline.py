"""TB section handlers build their corners via the bundle builder.

``tb_handlers`` routes four shapes that turn one or two corners: an internal
station to a LEFT/RIGHT exit port (``_route_tb_lr_exit``), a LEFT/RIGHT entry
port to an internal station (``_route_tb_lr_entry``), a TOP/BOTTOM entry port
down into the trunk (``_route_perp_entry``), and the corridor-fed variant of the
last (``_route_perp_entry_from_corridor``).  Each fans a co-travelling bundle
around its corner(s) through :func:`build_tapered_bundle`, which anchors every
corner on the bundle's innermost-of-turn line, so no arc pinches below the floor
and the lines keep a constant side-of-travel order.

The perpendicular-entry L-shape turns a single wholesale corner (drop then turn
into the station), so a fan straddling that bend manufactures an inside-of-turn
line whose arc a hand-built radius would pinch below the floor; that arm is
tested directly.  The exit/entry/corridor shapes turn a *transition* corner
(the drop channel fans in X, the port arrival in Y), which is not concentric and
reorders by construction under such a fan; those are pinned by the natural-render
guard, which routes real geometry and asserts the curve invariants the renderer
relies on.
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
from nf_metro.layout.routing.context import _build_routing_context
from nf_metro.layout.routing.invariants import (
    assert_render_curve_invariants,
    check_concentric_bundle_corners,
)
from nf_metro.layout.routing.tb_handlers import (
    _route_perp_entry,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import PortSide

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
TOPOLOGIES = EXAMPLES / "topologies"

# A co-travelling fan wider than the floor radius and straddling zero, so one
# line lands inside the bend.  Real layouts anchor their fans at the innermost
# line (offsets >= 0), so the inside-of-turn case never arises naturally; this
# manufactures it to exercise the arm's fan.
_FAN_STEP = 20.0


def _tb_section_ids(graph) -> set[str]:
    return {sid for sid, s in graph.sections.items() if s.direction in ("TB", "BT")}


def _exit_arms(graph) -> dict[tuple[str, str], list]:
    """Internal station -> LEFT/RIGHT exit port arms in a TB section."""
    tb = _tb_section_ids(graph)
    groups: dict[tuple[str, str], list] = {}
    for edge in graph.edges:
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if not src or not tgt:
            continue
        port = graph.ports.get(tgt.id)
        if (
            port is not None
            and not port.is_entry
            and port.side in (PortSide.LEFT, PortSide.RIGHT)
            and not src.is_port
            and src.section_id in tb
            and src.section_id == tgt.section_id
        ):
            groups.setdefault((edge.source, edge.target), []).append(edge)
    return groups


def _entry_arms(graph) -> dict[tuple[str, str], list]:
    """LEFT/RIGHT entry port -> internal station arms in a TB section."""
    tb = _tb_section_ids(graph)
    groups: dict[tuple[str, str], list] = {}
    for edge in graph.edges:
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if not src or not tgt:
            continue
        port = graph.ports.get(src.id)
        if (
            port is not None
            and port.is_entry
            and port.side in (PortSide.LEFT, PortSide.RIGHT)
            and not tgt.is_port
            and src.section_id in tb
        ):
            groups.setdefault((edge.source, edge.target), []).append(edge)
    return groups


def _perp_arms(graph) -> dict[tuple[str, str], list]:
    """TOP/BOTTOM entry port -> internal station arms."""
    groups: dict[tuple[str, str], list] = {}
    for edge in graph.edges:
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if not src or not tgt:
            continue
        port = graph.ports.get(src.id)
        if (
            port is not None
            and port.is_entry
            and port.side in (PortSide.TOP, PortSide.BOTTOM)
            and not tgt.is_port
        ):
            groups.setdefault((edge.source, edge.target), []).append(edge)
    return groups


def _find_fixture(stem: str) -> Path:
    for d in (EXAMPLES, TOPOLOGIES, EXAMPLES / "guide"):
        p = d / f"{stem}.mmd"
        if p.exists():
            return p
    raise FileNotFoundError(stem)


def _multi_line_arms(graph, finder):
    return {k: v for k, v in finder(graph).items() if len({e.line_id for e in v}) > 1}


# Fixtures whose perpendicular entry turns a multi-line L-shape corner -- the
# wholesale bend a straddling fan can pinch.
_PERP_L_FIXTURES = ["rnaseq_auto", "cross_row_gap_wrap", "fold_fan_across"]

# (family, arm-finder, fixtures) for the natural-render guard, one per TB shape.
_NATURAL_FAMILIES = [
    ("exit", _exit_arms, ["04_directions", "fold_double", "u_turn_fold"]),
    ("entry", _entry_arms, ["04_directions", "fold_double", "rnaseq_auto"]),
    (
        "perp",
        _perp_arms,
        ["lr_to_tb_top_two_lines", "tb_right_entry_stack", "rnaseq_auto"],
    ),
    (
        "corridor",
        _perp_arms,
        ["lr_perp_top_exit_perp_entry", "lr_perp_bottom_exit_perp_entry"],
    ),
]
_NATURAL_CASES = [
    (fam, finder, stem) for fam, finder, stems in _NATURAL_FAMILIES for stem in stems
]


@pytest.mark.parametrize("stem", _PERP_L_FIXTURES)
def test_perp_entry_l_corner_clears_floor_when_fanned(stem: str) -> None:
    """A fanned multi-line perp-entry L-corner keeps every arc at or above the floor.

    The inside-of-turn line anchors at ``CURVE_RADIUS`` and the rest fan
    outward.  A corner sized from the raw port centre instead pinches the inside
    line below the floor (to a sub-floor kink).
    """
    path = _find_fixture(stem)
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    groups = _multi_line_arms(graph, _perp_arms)
    assert groups, f"{stem}: expected a multi-line perp-entry arm"
    for (source, target), edges in groups.items():
        line_ids = list(dict.fromkeys(e.line_id for e in edges))
        n = len(line_ids)
        offsets: dict[tuple[str, str], float] = {}
        for j, line_id in enumerate(line_ids):
            d = (j - (n - 1) / 2) * _FAN_STEP
            offsets[(source, line_id)] = d
            offsets[(target, line_id)] = d
        ctx = _build_routing_context(graph, 30.0, CURVE_RADIUS, offsets)
        routes = [
            _route_perp_entry(
                e, graph.stations[e.source], graph.stations[e.target], ctx
            )
            for e in edges
        ]
        offenders = [
            (r.line_id, [round(x, 2) for x in r.curve_radii])
            for r in routes
            if r is not None
            and r.curve_radii
            and any(x < CURVE_RADIUS - 0.01 for x in r.curve_radii)
        ]
        assert not offenders, (
            f"{stem} {source}->{target}: perp-entry corners below floor: {offenders}"
        )


# A TB BOTTOM exit feeding a subset of a shared TOP entry port collapses every
# port line onto one slot while the consumer station fans them apart -- a
# multi-line perpendicular entry whose port offsets are uniform but whose target
# offsets are distinct.  Such a collapse forces a per-line boundary offset, so it
# cannot pass ``check_perp_entry_boundary_consistent`` and lives inline here
# rather than as a corpus fixture.
_COLLAPSED_PERP_FAN_MMD = """\
%%metro title: Collapsed Perp Fan
%%metro line: main | Main | #0570b0
%%metro line: feed | Feed | #e63946
%%metro grid: up | 0,0
%%metro grid: side | 1,0
%%metro grid: down | 0,1

graph LR
    subgraph up [Upstream]
        %%metro direction: TB
        u1[Collect]
        u2[Filter]
        u1 -->|main| u2
    end
    subgraph side [Side]
        sv1[Load]
        sv2[Tag]
        sv1 -->|feed| sv2
    end
    subgraph down [Combine]
        %%metro direction: TB
        c1[Merge]
        c2[Polish]
        c1 -->|main,feed| c2
    end
    u2 -->|main| c1
    sv2 -->|feed| c1
"""


def test_collapsed_perp_fan_routes_as_l_shape_not_staircase() -> None:
    """A collapsed-fan TOP entry into a TB target routes V-H, never H-V-H.

    When a shared TOP/BOTTOM entry port collapses every line onto one slot but
    the consumer station fans them apart, each line could drop straight off the
    port and turn in (V-H, an L-shape) or jog horizontally onto a per-line
    channel before dropping (H-V-H), which lands an S-cusp on the section
    boundary that ``check_perp_entry_boundary_consistent`` forbids.  Assert
    every perp-entry route leaves the port on a vertical segment, the L-shape
    signature.
    """
    graph = parse_metro_mermaid(_COLLAPSED_PERP_FAN_MMD)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    perp_routes = []
    for r in routes:
        port = graph.ports.get(r.edge.source)
        tgt = graph.stations.get(r.edge.target)
        if (
            port is not None
            and port.is_entry
            and port.side in (PortSide.TOP, PortSide.BOTTOM)
            and tgt is not None
            and not tgt.is_port
        ):
            perp_routes.append(r)
    assert perp_routes, "expected a TOP/BOTTOM perp-entry route"

    # Require the collapsed-fan state (all port lines on one slot); a spread fan
    # routes an ordinary L-shape and would pass the vertical-departure check
    # without exercising the H-V-H temptation.
    port_id = perp_routes[0].edge.source
    port_offsets = {
        offsets.get((port_id, lid), 0.0) for lid in graph.station_lines(port_id)
    }
    assert len(port_offsets) == 1, f"port fan not collapsed: {port_offsets}"

    for r in perp_routes:
        p0, p1 = r.points[0], r.points[1]
        assert abs(p1[1] - p0[1]) > abs(p1[0] - p0[0]), (
            f"{r.line_id}: perp-entry leaves the port on a horizontal jog "
            f"{p0}->{p1} (H-V-H shape); expected a vertical drop (L-shape)"
        )


@pytest.mark.parametrize(
    "family,finder,stem",
    _NATURAL_CASES,
    ids=[f"{fam}-{stem}" for fam, _f, stem in _NATURAL_CASES],
)
def test_tb_corner_natural_render_is_clean(family, finder, stem) -> None:
    """The naturally-routed render has concentric, offset-baked TB corners.

    ``check_concentric_bundle_corners`` skips the transition corners (exit /
    entry / corridor) and pins the wholesale ones; ``assert_render_curve_invariants``
    rejects any pinched or flipped arc the builder should have prevented.
    """
    path = _find_fixture(stem)
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    assert check_concentric_bundle_corners(graph, routes, offsets) == []
    assert_render_curve_invariants(graph, routes, offsets)
    arm_targets = {t for (_s, t) in finder(graph)}
    arm_routes = [r for r in routes if r.edge.target in arm_targets]
    assert arm_routes, f"{stem}: expected routed {family} arm edges"
    assert all(r.offset_regime is OffsetRegime.BAKED for r in arm_routes)
