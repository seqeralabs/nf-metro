"""Inter-section edge routing: bypass, entry wraps, around-section,
inter-row corridors, stepped descent, and L-shape handlers.

``_route_inter_section`` selects the shape via a declarative table
(``_INTER_SECTION_RULES``): one :class:`_InterFacts` snapshot of the edge's
geometry and topology is matched against an ordered list of named rules, and
the first whose predicate holds owns the route.  The rule order is the
combinatorial space documented in ``docs/dev/inter_section_dispatch.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from nf_metro.layout.constants import (
    BYPASS_CLEARANCE,
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    EDGE_TO_BUNDLE_CLEARANCE,
    INTER_ROW_EDGE_CLEARANCE,
    INTER_ROW_HEADER_CLEARANCE,
    JUNCTION_MARGIN,
    MERGE_ROUTE_MARGIN,
    SECTION_ROUTE_CLEARANCE,
)
from nf_metro.layout.routing.bundle import build_tapered_bundle
from nf_metro.layout.routing.centrelines import (
    fan_offsets,
    gather_bundle,
    gather_member_edges,
    gather_tapered_bundle,
    route_along,
    route_hvh_tapered,
    route_straight,
    route_tapered,
    route_tapered_anchored,
)
from nf_metro.layout.routing.common import (
    Direction,
    RoutedPath,
    _center_inter_row_channel,
    bundle_width,
    bypass_bottom_y,
    clear_channel_of_section_edge,
    col_left_edge,
    col_right_edge,
    column_gap_edges,
    column_gap_midpoint,
    endpoint_port_xs,
    header_corridor_y,
    horizontal_direction,
    inter_column_channel_x,
    inter_row_channel_y,
    inter_row_wrap_band,
    resolve_section,
    row_bottom_edge,
    row_top_edge,
    symmetric_bundle_midpoint,
    vertical_direction,
)
from nf_metro.layout.routing.context import (
    _get_offset,
    _has_intervening_sections,
    _perp_riser_lateral,
    _resolve_section_col,
    _resolve_section_colrow,
    _resolve_section_row,
    _RoutingCtx,
    _tb_x_offset,
)
from nf_metro.layout.routing.corners import (
    bypass_stagger,
    l_shape_stagger,
)
from nf_metro.layout.routing.normalize import (
    _clear_channel_x_in_band,
    _gap_channel_base,
    _h_segment_crosses_other_section,
    _has_other_row_section_in_col_range,
)
from nf_metro.parser.model import (
    Edge,
    MetroGraph,
    Port,
    PortSide,
    Station,
)


@dataclass(frozen=True)
class _InterFacts:
    """Geometry and topology of one inter-section edge, computed once.

    The shared snapshot every dispatch rule reads.  Coordinates and grid
    columns/rows are resolved up front; the booleans the rules key on are
    derived properties so each rule predicate stays a one-line read.
    """

    edge: Edge
    src: Station
    tgt: Station
    ctx: _RoutingCtx
    sx: float
    sy: float
    tx: float
    ty: float
    i: int
    n: int
    src_port: Port | None
    tgt_port: Port | None
    src_col: int | None
    src_row: int | None
    tgt_col: int | None
    tgt_row: int | None
    needs_bypass: bool
    merge_ep: Station | None

    @property
    def graph(self) -> MetroGraph:
        return self.ctx.graph

    @property
    def dx(self) -> float:
        return self.tx - self.sx

    @property
    def dy(self) -> float:
        return self.ty - self.sy

    @property
    def horizontal(self) -> Direction:
        return horizontal_direction(self.dx)

    @property
    def same_y(self) -> bool:
        return abs(self.dy) < COORD_TOLERANCE_FINE

    @property
    def same_x(self) -> bool:
        return abs(self.dx) < COORD_TOLERANCE

    @property
    def cross_row(self) -> bool:
        return (
            self.src_row is not None
            and self.tgt_row is not None
            and self.src_row != self.tgt_row
        )

    @property
    def same_col(self) -> bool:
        return (
            self.src_col is not None
            and self.tgt_col is not None
            and self.src_col == self.tgt_col
        )

    @property
    def entry_side(self) -> PortSide | None:
        """The target entry port's side, or ``None`` when the target is not one."""
        if self.tgt_port is not None and self.tgt_port.is_entry:
            return self.tgt_port.side
        return None

    @property
    def is_perp_exit(self) -> bool:
        """Source is a TOP/BOTTOM exit on a horizontal-flow section."""
        return (
            self.src_port is not None
            and not self.src_port.is_entry
            and self.src_port.side in (PortSide.TOP, PortSide.BOTTOM)
            and self.src.section_id not in self.ctx.tb_sections
        )

    @property
    def is_tb_bottom_exit(self) -> bool:
        """Source is a BOTTOM exit on a TB/BT section (with station offsets)."""
        return (
            self.src_port is not None
            and not self.src_port.is_entry
            and self.src_port.side == PortSide.BOTTOM
            and self.src.section_id in self.ctx.tb_sections
            and bool(self.ctx.station_offsets)
        )

    @property
    def right_entry_from_left(self) -> bool:
        """Target is a RIGHT entry port whose source sits to its left.

        A straight or interior-cutting approach would plough through the box to
        reach the far-edge port, so such an edge wraps in from the port's own
        outward side instead.
        """
        return self.entry_side is PortSide.RIGHT and self.sx < self.tx - COORD_TOLERANCE

    @property
    def is_near_vertical_same_col_junction(self) -> bool:
        """Junction dropping almost straight into a same-column entry."""
        return (
            self.edge.source in self.ctx.junction_ids
            and abs(self.dx) <= JUNCTION_MARGIN + COORD_TOLERANCE
            and abs(self.dy) > abs(self.dx) * 3
            and self.same_col
        )

    @property
    def is_serpentine_left_exit_left_entry(self) -> bool:
        """LEFT exit dropping into a LEFT entry stacked in the same column."""
        return (
            self.src_port is not None
            and not self.src_port.is_entry
            and self.src_port.side == PortSide.LEFT
            and self.entry_side is PortSide.LEFT
            and self.same_col
            and self.cross_row
        )


def _build_inter_facts(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> _InterFacts:
    graph = ctx.graph
    src_col, src_row = _resolve_section_colrow(graph, src)
    tgt_col, tgt_row = _resolve_section_colrow(graph, tgt)
    # A multi-column hop needs a bypass when an intervening section blocks the
    # source row, or - for a cross-row L-shape, whose horizontal leg runs at the
    # target entry Y - the TARGET row, plowed through even when the source row
    # is clear.
    needs_bypass = (
        src_col is not None
        and tgt_col is not None
        and abs(tgt_col - src_col) > 1
        and (
            _has_intervening_sections(graph, src_col, tgt_col, src_row)
            or (
                src_row is not None
                and tgt_row is not None
                and tgt_row != src_row
                and _has_intervening_sections(graph, src_col, tgt_col, tgt_row)
            )
        )
    )
    ep_id = ctx.merge.entry_port_for.get(edge.target)
    i, n = ctx.bundle_info.get((edge.source, edge.target, edge.line_id), (0, 1))
    return _InterFacts(
        edge=edge,
        src=src,
        tgt=tgt,
        ctx=ctx,
        sx=src.x,
        sy=src.y,
        tx=tgt.x,
        ty=tgt.y,
        i=i,
        n=n,
        src_port=graph.ports.get(edge.source),
        tgt_port=graph.ports.get(edge.target),
        src_col=src_col,
        src_row=src_row,
        tgt_col=tgt_col,
        tgt_row=tgt_row,
        needs_bypass=needs_bypass,
        merge_ep=graph.stations.get(ep_id) if ep_id else None,
    )


def _route_straight_connector(f: _InterFacts) -> RoutedPath | None:
    """Straight horizontal (same Y) or vertical (same X) connector."""
    ctx = f.ctx
    return route_straight(
        f.edge, ctx, (f.sx, f.sy), (f.tx, f.ty), base_radius=ctx.curve_radius
    )


def _route_near_vertical_junction(f: _InterFacts) -> RoutedPath | None:
    """Drop a same-column junction into its entry via the inter-column gap.

    A standard L-shape would place the vertical channel toward the target (back
    inside the shared column); push it the other way so the line keeps the
    junction's natural direction before dropping.
    """
    ctx = f.ctx
    if f.horizontal is Direction.L:
        channel_x = f.sx + ctx.curve_radius + ctx.offset_step
    else:
        channel_x = f.sx - ctx.curve_radius - ctx.offset_step
    return route_hvh_tapered(
        ctx, f.edge, f.src, f.tgt, channel_x, base_radius=ctx.curve_radius
    )


def _route_bypass_family(f: _InterFacts) -> RoutedPath | None:
    """Multi-column hop past intervening sections (``needs_bypass``).

    A merge target splits into trunk (full bypass to the entry port) and branch
    (truncated descent to trunk level).  Otherwise: a LEFT entry one row
    directly below drops straight in when the entry-Y horizontal is clear (no
    canvas-bottom loop); a RIGHT entry fed from the left wraps around its
    outward side (via the inter-row gap above when clear, else the around-below
    loop); everything else takes the U-shaped bypass.
    """
    edge, src, tgt, ctx, graph = f.edge, f.src, f.tgt, f.ctx, f.graph
    assert f.src_col is not None and f.tgt_col is not None
    if edge.target in ctx.merge.trunk_source:
        if ctx.merge.trunk_source[edge.target] == edge.source:
            return _route_merge_trunk(
                edge, src, tgt, f.i, f.src_col, f.tgt_col, ctx, f.src_row
            )
        return _route_merge_branch(edge, src, ctx, f.src_col)
    if (
        f.entry_side is PortSide.LEFT
        and f.src_row is not None
        and f.tgt_row is not None
        and f.tgt_row == f.src_row + 1
    ):
        exclude = {sid for sid in (src.section_id, tgt.section_id) if sid is not None}
        if not _h_segment_crosses_other_section(graph, f.sx, f.tx, f.ty, exclude):
            return _route_l_shape(edge, src, tgt, f.i, f.n, ctx)
    if f.right_entry_from_left:
        if (
            f.src_row is not None
            and f.tgt_row is not None
            and f.src_row < f.tgt_row
            and _right_entry_gap_above_is_clear(graph, src, tgt, tgt, f.src_row)
        ):
            return _route_right_entry_via_gap_above(
                edge, src, tgt, tgt, f.i, f.n, ctx, f.src_row
            )
        return _route_right_entry_around_below(edge, src, tgt, tgt, f.i, f.n, ctx)
    return _route_bypass(edge, src, tgt, f.i, f.src_col, f.tgt_col, ctx, f.src_row)


def _route_left_entry_family(f: _InterFacts) -> RoutedPath | None:
    """Cross-row feed into a LEFT entry from a source on its right.

    A standard L-shape would cut through the target interior to reach the
    left-side port.  Wrap leftward through the inter-row gap; when that gap
    horizontal lands inside an intervening section, descend the clear corridor
    if one exists, else loop around below the target.
    """
    edge, src, tgt, ctx, graph = f.edge, f.src, f.tgt, f.ctx, f.graph
    wrap_hy = inter_row_channel_y(graph, src, tgt, f.sy, f.ty, f.dy, ctx.curve_radius)
    exclude = {src.section_id} if src.section_id else set[str]()
    if _h_segment_crosses_other_section(graph, f.sx, f.tx, wrap_hy, exclude):
        if _corridor_is_viable(ctx, src, tgt):
            return _route_inter_row_gap_corridor(edge, src, tgt, tgt, f.i, f.n, ctx)
        return _route_around_section_below(edge, src, tgt, tgt, f.i, f.n, ctx)
    return _route_left_entry_wrap(edge, src, tgt, f.i, f.n, ctx)


class _MergeEntryRoute(Enum):
    """Which shape :func:`_route_merge_entry_family` builds for a merge feeder."""

    STRAIGHT = "straight"
    CORRIDOR = "corridor"
    AROUND_BELOW = "around_below"
    L_SHAPE = "l_shape"


def _merge_entry_route_kind(f: _InterFacts) -> _MergeEntryRoute:
    """Classify a non-bypass merge-junction feed without building the route.

    A near-collinear feed connects ``STRAIGHT`` to avoid a cramped curve.  A
    LEFT entry whose L-shape horizontal would cross a foreign section descends
    the clear ``CORRIDOR`` if one exists, else loops ``AROUND_BELOW``;
    otherwise a standard ``L_SHAPE`` into the entry port.
    """
    src, ctx, graph = f.src, f.ctx, f.graph
    ep = f.merge_ep
    assert ep is not None
    if abs(ep.y - f.sy) < ctx.curve_radius:
        return _MergeEntryRoute.STRAIGHT
    ep_port = graph.ports.get(ep.id)
    if ep_port and ep_port.side == PortSide.LEFT:
        exclude = {src.section_id} if src.section_id else set[str]()
        if _h_segment_crosses_other_section(graph, f.sx, ep.x, ep.y, exclude):
            if _corridor_is_viable(ctx, src, ep):
                return _MergeEntryRoute.CORRIDOR
            return _MergeEntryRoute.AROUND_BELOW
    return _MergeEntryRoute.L_SHAPE


def _route_merge_entry_family(f: _InterFacts) -> RoutedPath | None:
    """Non-bypass feed into a merge junction, routed to its entry port."""
    edge, src, tgt, ctx = f.edge, f.src, f.tgt, f.ctx
    ep = f.merge_ep
    assert ep is not None
    builders = {
        _MergeEntryRoute.STRAIGHT: lambda: RoutedPath(
            edge=edge,
            line_id=edge.line_id,
            points=[(f.sx, f.sy), (ep.x, ep.y)],
            is_inter_section=True,
        ),
        _MergeEntryRoute.CORRIDOR: lambda: _route_inter_row_gap_corridor(
            edge, src, tgt, ep, f.i, f.n, ctx
        ),
        _MergeEntryRoute.AROUND_BELOW: lambda: _route_around_section_below(
            edge, src, tgt, ep, f.i, f.n, ctx
        ),
        _MergeEntryRoute.L_SHAPE: lambda: _route_l_shape(edge, src, ep, f.i, f.n, ctx),
    }
    return builders[_merge_entry_route_kind(f)]()


def _right_entry_plough_needs_bypass(f: _InterFacts) -> bool:
    """A same-row-section L-shape to a RIGHT entry from above would plough through."""
    if not (
        f.entry_side is PortSide.RIGHT
        and f.src_row is not None
        and f.tgt_row is not None
        and f.tgt_row > f.src_row
        and f.src_col is not None
        and f.tgt_col is not None
    ):
        return False
    exclude = {sid for sid in (f.src.section_id, f.tgt.section_id) if sid is not None}
    return _h_segment_crosses_other_section(f.graph, f.sx, f.tx, f.ty, exclude)


def _route_right_entry_plough_bypass(f: _InterFacts) -> RoutedPath | None:
    # Columns are guaranteed non-None by _right_entry_plough_needs_bypass.
    assert f.src_col is not None and f.tgt_col is not None
    return _route_bypass(
        f.edge, f.src, f.tgt, f.i, f.src_col, f.tgt_col, f.ctx, f.src_row
    )


@dataclass(frozen=True)
class _Rule:
    """One row of the dispatch table: a named predicate and its route builder."""

    name: str
    when: Callable[[_InterFacts], bool]
    route: Callable[[_InterFacts], RoutedPath | None]


# Inter-section dispatch table.  The first rule whose predicate holds owns the
# route; order is significant (earlier rules shadow later ones).  The
# combinatorial space (relative position x exit side x entry side) and why each
# rule sits where it does are documented in
# docs/dev/inter_section_dispatch.md.
_INTER_SECTION_RULES: list[_Rule] = [
    # A perpendicular (TOP/BOTTOM) exit leaves vertically: route it before the
    # same-Y shortcut, which would graze both boxes when exit and entry share an
    # edge Y.
    _Rule(
        "perp-exit",
        lambda f: f.is_perp_exit,
        lambda f: _route_perp_exit(f.edge, f.src, f.tgt, f.src_col, f.tgt_col, f.ctx),
    ),
    # Same Y, no obstacle, not a right-entry plough: a straight horizontal run.
    _Rule(
        "same-Y straight",
        lambda f: f.same_y and not f.needs_bypass and not f.right_entry_from_left,
        _route_straight_connector,
    ),
    _Rule(
        "TB bottom exit",
        lambda f: f.is_tb_bottom_exit,
        lambda f: _route_tb_bottom_exit(f.edge, f.src, f.tgt, f.ctx),
    ),
    # TOP entry needs an L-shape lead-in; checked before the same-X shortcut,
    # which would drop straight in with no horizontal approach.
    _Rule(
        "TOP entry L-shape",
        lambda f: f.entry_side is PortSide.TOP,
        lambda f: _route_top_entry_l_shape(f.edge, f.src, f.tgt, f.n, f.ctx),
    ),
    _Rule("same-X vertical drop", lambda f: f.same_x, _route_straight_connector),
    _Rule(
        "bottom-exit junction",
        lambda f: f.edge.source in f.ctx.bottom_exit_junctions,
        lambda f: _route_bottom_exit_junction(f.edge, f.src, f.tgt, f.i, f.n, f.ctx),
    ),
    _Rule("bypass family", lambda f: f.needs_bypass, _route_bypass_family),
    _Rule(
        "near-vertical same-col junction",
        lambda f: f.is_near_vertical_same_col_junction,
        _route_near_vertical_junction,
    ),
    # RIGHT entry fed from the left: wrap around the right side rather than cut
    # through the interior.
    _Rule(
        "RIGHT entry wrap",
        lambda f: f.entry_side is PortSide.RIGHT and f.horizontal is Direction.R,
        lambda f: _route_right_entry_wrap(f.edge, f.src, f.tgt, f.i, f.n, f.ctx),
    ),
    _Rule(
        "LEFT entry wrap family",
        lambda f: f.entry_side is PortSide.LEFT and f.dx < 0 and f.cross_row,
        _route_left_entry_family,
    ),
    _Rule(
        "serpentine LEFT exit -> LEFT entry",
        lambda f: f.is_serpentine_left_exit_left_entry,
        lambda f: _route_left_exit_left_entry_drop(f.edge, f.src, f.tgt, f.ctx),
    ),
    _Rule(
        "merge entry family",
        lambda f: f.merge_ep is not None,
        _route_merge_entry_family,
    ),
    # A higher-row L-shape to a RIGHT entry that would plough an intervening
    # same-row section deflects through the bypass instead.
    _Rule(
        "RIGHT entry plough -> bypass",
        _right_entry_plough_needs_bypass,
        _route_right_entry_plough_bypass,
    ),
]


def _route_inter_section(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Route an edge between ports/junctions via the dispatch table.

    Returns ``None`` when the edge is not inter-section (both endpoints must be
    a port or junction).  Otherwise the first rule in ``_INTER_SECTION_RULES``
    whose predicate holds builds the route; the standard L-shape is the
    fall-through when no rule matches.
    """
    is_inter = (src.is_port or edge.source in ctx.junction_ids) and (
        tgt.is_port or edge.target in ctx.junction_ids
    )
    if not is_inter:
        return None

    f = _build_inter_facts(edge, src, tgt, ctx)
    rule = _match_inter_section_rule(f)
    if rule is not None:
        return rule.route(f)
    # Standard L-shape: the default when no rule above claims the edge.
    return _route_l_shape(edge, src, tgt, f.i, f.n, ctx)


def _match_inter_section_rule(f: _InterFacts) -> _Rule | None:
    """The first dispatch rule whose predicate claims *f*, or ``None``.

    The selection seam: ``_route_inter_section`` routes through the matched
    rule, and the dispatch-table tests assert which rule claims each edge so a
    predicate edit that silently steals an edge class from a neighbouring rule
    is caught.
    """
    for rule in _INTER_SECTION_RULES:
        if rule.when(f):
            return rule
    return None


def _route_tb_bottom_exit(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Vertical drop from TB BOTTOM exit with X offsets.

    When the target sits directly below the exit the route is a clean
    vertical drop.  When the target X is offset (e.g. a TOP entry port a
    few px inward of the bottom exit), a straight 2-point connector would
    be a raw diagonal between two perpendicular ports.  Emit an orthogonal
    drop / jog / drop with curved corners instead: down out of the BOTTOM
    port, across the inter-row gap, then down into the target.
    """
    x_off = _tb_x_offset(ctx, edge.source, edge.line_id, src.section_id)
    sx = src.x + x_off
    sy = src.y
    tx = tgt.x + x_off
    ty = tgt.y

    member = [(edge, edge.line_id, 0.0)]
    if abs(tx - sx) <= COORD_TOLERANCE:
        return route_along(
            edge,
            member,
            [(sx, sy), (tx, ty)],
            base_radius=ctx.curve_radius,
            normalize_exempt=False,
        )

    # Misaligned: jog in the inter-row gap so the line leaves the BOTTOM
    # port travelling downward, transitions across with bounded curves,
    # then drops into the target.  A short horizontal jog is shared by both
    # corners, so the render-time segment budget shrinks each arc to half the
    # jog and the bend stays orthogonal rather than collapsing into a diagonal.
    dy = ty - sy
    hy = inter_row_channel_y(ctx.graph, src, tgt, sy, ty, dy, ctx.curve_radius)
    # Keep the jog Y strictly between the two ports so both vertical legs
    # have positive length for the corner curves to bite into.
    lo, hi = (sy, ty) if dy >= 0 else (ty, sy)
    hy = min(max(hy, lo + ctx.curve_radius), hi - ctx.curve_radius)
    return route_along(
        edge,
        member,
        [(sx, sy), (sx, hy), (tx, hy), (tx, ty)],
        base_radius=ctx.curve_radius,
    )


def _route_bottom_exit_junction(
    edge: Edge, src: Station, tgt: Station, i: int, n: int, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Vertical-first L-shape from bottom exit junction.

    The descent channel sits at the bundle's mean exit X (the fan above the
    junction), turns the corner, and runs to the entry at the mean entry Y.
    Because the channel is anchored on the exit fan rather than the per-line
    endpoint offsets, this corner is fanned rigidly -- one offset on every leg
    -- so the bundle is built with each line's source offset on both ends and
    ``route_tapered`` sends it down its rigid (``route_along``) path.
    """
    exit_pid = ctx.bottom_exit_junction_ports[edge.source]
    exit_src = ctx.graph.stations.get(exit_pid)
    exit_sec = exit_src.section_id if exit_src else ""

    def exit_x_offset(line_id: str) -> float:
        if ctx.station_offsets:
            return _tb_x_offset(ctx, exit_pid, line_id, exit_sec or "")
        bi, bn = ctx.bundle_info.get((edge.source, edge.target, line_id), (i, n))
        return l_shape_stagger(bi, bn, Direction.D, ctx.offset_step)

    members, _, tgt_center = gather_tapered_bundle(ctx, edge)
    exit_offs = [exit_x_offset(line_id) for _e, line_id, _s, _t in members]
    mean_exit_x = sum(exit_offs) / len(exit_offs)
    vx = src.x + mean_exit_x
    hy = tgt.y + tgt_center
    # Each line keeps its source offset on both legs: the channel is anchored
    # on the exit fan, so a per-end taper would detach the descent from the
    # entry offsets it never carried.
    rigid = [(e, line_id, src_off, src_off) for e, line_id, src_off, _tgt in members]
    return route_tapered(
        edge,
        rigid,
        [(vx, src.y), (vx, hy), (tgt.x, hy)],
        transition_leg=1,
        base_radius=ctx.curve_radius,
    )


def _route_merge_branch(
    edge: Edge,
    src: Station,
    ctx: _RoutingCtx,
    src_col: int,
) -> RoutedPath | None:
    """Truncated L-shape descent from a junction to the trunk level.

    Routes a 4-point path: horizontal lead-in, curve down, vertical
    drop, curve into trunk direction.  The lead-in is positioned at
    MERGE_ROUTE_MARGIN from the source section edge.
    """
    sx, sy = src.x, src.y
    dx = ctx.graph.stations[edge.target].x - sx
    horizontal = horizontal_direction(dx)
    src_off = _get_offset(ctx, edge.source, edge.line_id)

    # Trunk bypass Y level (branches drop to meet it)
    by = ctx.merge.trunk_by.get(edge.target, sy)

    # Position descent at MERGE_ROUTE_MARGIN from section edge
    if horizontal is Direction.R:
        lead_x = col_right_edge(ctx.graph, src_col) + MERGE_ROUTE_MARGIN
    else:
        lead_x = col_left_edge(ctx.graph, src_col) - MERGE_ROUTE_MARGIN
    # Clamp to at least curve_radius from the junction
    min_lead = sx + horizontal.sign * ctx.curve_radius
    if horizontal is Direction.R:
        lead_x = max(lead_x, min_lead)
    else:
        lead_x = min(lead_x, min_lead)
    tail_x = lead_x + horizontal.sign * ctx.curve_radius * 2

    # One branch line per call: a single descent with no bundle to fan, so the
    # centreline carries this line's own offset and both corners take the base
    # radius (the concentric radius at zero displacement).
    return route_along(
        edge,
        [(edge, edge.line_id, 0.0)],
        [
            (sx, sy + src_off),
            (lead_x, sy + src_off),
            (lead_x, by),
            (tail_x, by),
        ],
        base_radius=ctx.curve_radius,
        normalize_exempt=False,
    )


def _would_route_around_section_below(edge: Edge, ctx: _RoutingCtx) -> bool:
    """Whether *edge* dispatches to :func:`_route_around_section_below`.

    A merge-junction feeder reaches the around-below loop only through the
    merge-entry family, and only when :func:`_merge_entry_route_kind` selects
    it, so this consults the dispatch table rather than re-deriving the
    bypass / section-crossing predicates.
    """
    src = ctx.graph.stations.get(edge.source)
    tgt = ctx.graph.stations.get(edge.target)
    if src is None or tgt is None:
        return False
    f = _build_inter_facts(edge, src, tgt, ctx)
    rule = _match_inter_section_rule(f)
    return (
        rule is not None
        and rule.route is _route_merge_entry_family
        and _merge_entry_route_kind(f) is _MergeEntryRoute.AROUND_BELOW
    )


def _has_around_section_sibling(
    edge: Edge, ep: Station, ep_port: Port | None, ctx: _RoutingCtx
) -> bool:
    """Detect whether another edge to the same entry port will route via
    :func:`_route_around_section_below`.

    The around-section route hugs the target section's left edge with its
    V_up channel at ``section_left - base_gap - extra_clearance - delta``.
    When a merge trunk's bypass also lands in the same inter-column gap,
    the two bundles overlap visually.  Trunks that detect a competing
    around-section sibling can pull their V_up away from the target edge
    (see ``trunk_v_up_pull_away`` in :func:`_route_bypass`).

    A sibling competes only when it ACTUALLY dispatches to
    :func:`_route_around_section_below`, which
    :func:`_would_route_around_section_below` answers via the dispatch table.
    Siblings whose span pushes them into the bypass dispatch end up as
    merge-branches or trunk routes, not around-section, so they do NOT compete
    for the same channel and pulling the trunk away on their behalf produces
    the visible unbundling that #388 introduced on 03b_fan_in_merge.
    """
    if ep_port is None or ep_port.side != PortSide.LEFT:
        return False
    for other in ctx.graph.edges_to(edge.target):
        if other.source == edge.source:
            continue
        if _would_route_around_section_below(other, ctx):
            return True
    return False


def _route_merge_trunk(
    edge: Edge,
    src: Station,
    tgt: Station,
    i: int,
    src_col: int,
    tgt_col: int,
    ctx: _RoutingCtx,
    src_row: int | None = None,
) -> RoutedPath:
    """Full U-shape bypass for the trunk carrier, ending at the entry port.

    Delegates to _route_bypass with the entry port as the effective
    target so the route extends past the merge junction to the section
    entry.  Both X and Y of the entry port are overridden because the
    merge junction is virtual and lives inside the target section at a
    different Y from the actual entry port; without the Y override the
    bypass terminates at the merge junction's Y and leaves a visible
    "hanging" curve disconnected from the entry port.

    When the trunk and entry are in the same grid row but separated by
    intervening row-mates, the standard above-row bypass channel sits
    in the inter-row gap that also holds the target row's section
    titles.  Force ``cross_row`` so the channel runs BELOW all sections
    in the column range, mirroring :func:`_route_around_section_below`
    and avoiding overlap with the title text.

    When a sibling edge to the same merge junction will route via
    :func:`_route_around_section_below`, both routes would place
    their V_up channels in the inter-column gap just left of the target
    section, producing overlapping bundles in the same x range.  Detect
    that and pull the trunk's V_up channel further from the target edge
    (towards the previous column) so the two bundles occupy distinct
    columns within the gap.
    """
    ep_id = ctx.merge.entry_port_for.get(edge.target)
    ep = ctx.graph.stations.get(ep_id) if ep_id else None
    ep_port = ctx.graph.ports.get(ep_id) if ep_id else None
    effective_tx = ep.x if ep else tgt.x
    effective_ty = ep.y if ep else tgt.y
    tgt_row = _resolve_section_row(ctx.graph, tgt)
    force_cross_row = (
        src_row is not None
        and tgt_row == src_row
        and _has_other_row_section_in_col_range(ctx.graph, src_col, tgt_col, src_row)
    )
    trunk_v_up_pull_away = ep is not None and _has_around_section_sibling(
        edge, ep, ep_port, ctx
    )
    return _route_bypass(
        edge,
        src,
        tgt,
        i,
        src_col,
        tgt_col,
        ctx,
        src_row,
        effective_tx=effective_tx,
        effective_ty=effective_ty,
        force_cross_row=force_cross_row,
        trunk_v_up_pull_away=trunk_v_up_pull_away,
    )


def _route_bypass(
    edge: Edge,
    src: Station,
    tgt: Station,
    i: int,
    src_col: int,
    tgt_col: int,
    ctx: _RoutingCtx,
    src_row: int | None = None,
    effective_tx: float | None = None,
    effective_ty: float | None = None,
    force_cross_row: bool = False,
    trunk_v_up_pull_away: bool = False,
) -> RoutedPath:
    """U-shaped bypass route around intervening sections.

    When *effective_tx* / *effective_ty* are provided, they override
    the target coordinates for gap2 placement (used by merge trunks to
    reach the entry port instead of the merge junction, which sits at a
    different Y inside the section).  When *force_cross_row* is True,
    ``bypass_bottom_y`` is asked to route below ALL sections in the
    column range regardless of whether src and tgt share a row.

    When *trunk_v_up_pull_away* is True, gap2_x is placed in the half
    of the inter-column gap CLOSER to the previous column (i.e. AWAY
    from the target's edge) so it doesn't overlap with a sibling
    around-section route that hugs the target's edge.  Only honoured
    when the displacement keeps gap2_x at least SECTION_ROUTE_CLEARANCE
    from the neighbouring section; otherwise the standard placement is
    used (the bundles will overlap, but the alternative is to put
    gap2_x INSIDE the neighbouring section bbox, which is worse).
    """
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    if effective_tx is None:
        effective_tx = tx
    if effective_ty is not None:
        ty = effective_ty
    dx = tx - sx
    horizontal = horizontal_direction(dx)
    graph = ctx.graph

    ekey = (edge.source, edge.target, edge.line_id)
    g1_j, g1_n, g2_j, g2_n = ctx.bypass_gap_idx.get(ekey, (0, 1, 0, 1))

    fan = ctx.junction_fan_info.get(ekey)

    # Per-line trunk Y keeps lines visually separate on the horizontal.
    if fan is not None:
        nest_offset = g2_j * ctx.offset_step
    else:
        nest_offset = max(i, g2_j) * ctx.offset_step
    # Resolve target row to detect cross-row bypasses.
    tgt_row = _resolve_section_row(graph, tgt)
    cross_row = force_cross_row or (
        src_row is not None and tgt_row is not None and src_row != tgt_row
    )
    base_y = bypass_bottom_y(
        graph,
        src_col,
        tgt_col,
        BYPASS_CLEARANCE,
        src_row=src_row,
        cross_row=cross_row,
        tgt_row=tgt_row,
    )

    # Determine actual vertical direction at each gap from the geometry.
    # Gap1 goes from source Y to trunk Y; gap2 from trunk Y to target Y.
    # Normally gap1 goes down and gap2 goes up, but when the source is
    # below the trunk (bottom of a tall section bypassing a shorter
    # neighbour), gap1 also goes up.
    gap1_vertical = vertical_direction(base_y - sy)
    gap2_vertical = vertical_direction(ty - base_y)

    # Per-line lateral deltas at each gap's vertical channel; the centreline +
    # build_tapered_bundle below derive every corner radius from the geometry.
    delta1, delta2 = bypass_stagger(
        g1_j,
        g1_n,
        g2_j,
        g2_n,
        horizontal=horizontal,
        offset_step=ctx.offset_step,
        gap1_vertical=gap1_vertical,
        gap2_vertical=gap2_vertical,
    )
    by = base_y + nest_offset

    # Initial gap-channel centres and per-line positions.  These centre each
    # leg in its (row-aware) gap via _gap_channel_base; the post-routing
    # _normalize_gap_channels pass then re-stacks all inter-section channels
    # into their final centred / B-separated bundle positions.
    half_g1 = (g1_n - 1) * ctx.offset_step / 2
    half_g2 = (g2_n - 1) * ctx.offset_step / 2

    if horizontal is Direction.R:
        if fan is not None:
            # The fan shares its first corner across siblings; centre the
            # channel on the gap slot, but never left of the near-source
            # position or the curve would start behind the junction (nubbin).
            ui, un = fan
            fan_delta = l_shape_stagger(ui, un, gap1_vertical, ctx.offset_step)
            near = sx + ctx.curve_radius + (un - 1) * ctx.offset_step / 2
            slot = _gap_channel_base(graph, src_col, src_row, un, ctx.offset_step)
            fan_mid_x = max(near, slot)
            off1 = fan_delta
            gap1_x = fan_mid_x + fan_delta
        else:
            gap1_base = _gap_channel_base(
                graph, src_col, src_row, g1_n, ctx.offset_step
            )
            gap1_limit = sx + ctx.curve_radius
            if gap1_base - (g1_n - 1) * ctx.offset_step < gap1_limit:
                gap1_mid = gap1_limit + half_g1
            else:
                gap1_mid = gap1_base - half_g1
            off1 = delta1
            gap1_x = gap1_mid + delta1

        gap2_base = _gap_channel_base(
            graph, tgt_col - 1, tgt_row, g2_n, ctx.offset_step
        )
        gap2_limit = effective_tx - ctx.curve_radius
        if gap2_base + (g2_n - 1) * ctx.offset_step > gap2_limit:
            gap2_mid = gap2_limit - half_g2
        else:
            gap2_mid = gap2_base + half_g2
        if trunk_v_up_pull_away:
            # Two bundles share the gap between (tgt_col - 1) and tgt_col:
            # this bypass (gap2) bundle on the LEFT, paired with an
            # around-section bundle on the RIGHT (placed by
            # _route_around_section_below), positioned symmetrically via
            # symmetric_bundle_midpoint.  When the gap is too narrow to
            # fit both bundles with clearance, fall back to the standard
            # (single-bundle) placement; overlap is the lesser evil
            # compared to a route entering the neighbouring section's bbox.
            gap_left, gap_right = column_gap_edges(
                graph, tgt_col - 1, tgt_col, row=tgt_row
            )
            this_width = bundle_width(g2_n, ctx.offset_step)
            # The around-route bundle's line count equals the merge
            # trunk's effective line count, which today matches g2_n
            # (one around-route line per fan_in line).  Use g2_n as a
            # conservative width estimate.
            around_width = this_width
            pulled_mid_candidate = symmetric_bundle_midpoint(
                gap_left,
                gap_right,
                [this_width, around_width],
                bundle_index=0,
            )
            # Sanity: only honour the symmetric placement when both
            # bundles can fit with at least A clearance from each edge
            # and B inter-bundle separation.  Otherwise the gap was
            # never widened (e.g. layout disabled or pull-away
            # triggered without _enforce_min_column_gaps participating),
            # so fall back to the standard placement.
            this_xmin = pulled_mid_candidate - this_width / 2
            around_mid = symmetric_bundle_midpoint(
                gap_left,
                gap_right,
                [this_width, around_width],
                bundle_index=1,
            )
            around_xmax = around_mid + around_width / 2
            if (
                this_xmin - gap_left >= SECTION_ROUTE_CLEARANCE
                and gap_right - around_xmax >= SECTION_ROUTE_CLEARANCE
            ):
                gap2_mid = pulled_mid_candidate
        gap2_x = gap2_mid + delta2
    else:
        if fan is not None:
            # Mirror of the going-right fan: centre on the gap slot but never
            # right of the near-source position (curve must not start behind
            # the junction).  Wrap-style routes whose source-side curve is on
            # the RIGHT regardless of dx (left-entry wrap, around-section-
            # below) are dispatched through their own handlers, not here.
            ui, un = fan
            fan_delta = l_shape_stagger(ui, un, gap1_vertical, ctx.offset_step)
            near = sx - ctx.curve_radius - (un - 1) * ctx.offset_step / 2
            slot = _gap_channel_base(graph, src_col - 1, src_row, un, ctx.offset_step)
            fan_mid_x = min(near, slot)
            off1 = fan_delta
            gap1_x = fan_mid_x + fan_delta
        else:
            gap1_base = _gap_channel_base(
                graph, src_col - 1, src_row, g1_n, ctx.offset_step
            )
            gap1_limit = sx - ctx.curve_radius
            if gap1_base + (g1_n - 1) * ctx.offset_step > gap1_limit:
                gap1_mid = gap1_limit - half_g1
            else:
                gap1_mid = gap1_base + half_g1
            off1 = delta1
            gap1_x = gap1_mid + delta1

        gap2_base = _gap_channel_base(graph, tgt_col, tgt_row, g2_n, ctx.offset_step)
        gap2_limit = effective_tx + ctx.curve_radius
        if gap2_base - (g2_n - 1) * ctx.offset_step < gap2_limit:
            gap2_mid = gap2_limit + half_g2
        else:
            gap2_mid = gap2_base - half_g2
        gap2_x = gap2_mid + delta2

    # When the descent crosses other grid rows, the source/target-row gap
    # channel can still pierce an oversized section stacked in a crossed row
    # (its bbox extends into the gap).  Nudge each vertical leg clear of any
    # box its Y-span pierces, bounded to the inter-column gap so the channel
    # stays in clear space.
    if cross_row:
        exclude = {sid for sid in (src.section_id, tgt.section_id) if sid is not None}
        if horizontal is Direction.R:
            g1_lo, g1_hi = column_gap_edges(graph, src_col, src_col + 1)
            g2_lo, g2_hi = column_gap_edges(graph, tgt_col - 1, tgt_col)
        else:
            g1_lo, g1_hi = column_gap_edges(graph, src_col - 1, src_col)
            g2_lo, g2_hi = column_gap_edges(graph, tgt_col, tgt_col + 1)
        gap1_x = _clear_channel_x_in_band(
            graph, gap1_x, sy, by, SECTION_ROUTE_CLEARANCE, exclude, g1_lo, g1_hi
        )
        gap2_x = _clear_channel_x_in_band(
            graph, gap2_x, by, ty, SECTION_ROUTE_CLEARANCE, exclude, g2_lo, g2_hi
        )
        # When the source is a junction sitting at/beyond its source
        # section's right edge and the route runs leftward, the gap1
        # lead-in at the source Y would plough back across the source box
        # to reach a left-side descent channel.  Drop the descent on the
        # RIGHT of the source instead (straight down out of the junction),
        # so the long leftward traverse happens below the row at ``by``.
        if horizontal is Direction.L:
            src_sec = resolve_section(graph, src)
            if src_sec is not None and src_sec.bbox_w > 0:
                src_right = src_sec.bbox_x + src_sec.bbox_w
                if sx >= src_right - COORD_TOLERANCE and gap1_x < src_right:
                    gap1_x = max(sx, src_right + SECTION_ROUTE_CLEARANCE)
                    gap1_x = _clear_channel_x_in_band(
                        graph,
                        gap1_x,
                        sy,
                        by,
                        SECTION_ROUTE_CLEARANCE,
                        exclude,
                        bound_left=gap1_x,
                    )

    # Describe the U as a centreline through the two gap channels plus a
    # per-line offset on each, and let build_tapered_bundle derive every
    # corner concentrically.  The source-side legs (source lead-in, gap1
    # descent, the below-row traverse) fan by gap1's offset; the target-side
    # legs (gap2 rise, port approach) fan by gap2's, so the bundle tapers when
    # the two gaps carry different line counts and is rigid when they match.
    #
    # The two gaps' channel centres are recovered by subtracting each line's
    # lateral offset.  The vertical legs' perpendicular offsets (sigma1,
    # sigma2) are signed so the descent/rise lands at ``gap*_x``; the
    # horizontal legs would also pick up that offset as a Y shift, so the
    # centreline's horizontal Ys pre-subtract it, leaving each port at its
    # station offset and the traverse at ``by``.  Each horizontal leg's normal
    # follows its own travel direction: the source lead-in, the below-row
    # traverse, and the port approach can each run either way (a leftward
    # bypass out of a right-edge junction leads in rightward), so a single
    # direction would mis-sign the compensation.
    src_off = _get_offset(ctx, edge.source, edge.line_id)
    tgt_off = _get_offset(ctx, edge.target, edge.line_id)
    gap1_mid = gap1_x - off1
    gap2_mid = gap2_x - delta2
    n0y = 1.0 if gap1_mid >= sx else -1.0
    n2y = 1.0 if gap2_mid >= gap1_mid else -1.0
    n4y = 1.0 if effective_tx >= gap2_mid else -1.0
    n1x = -1.0 if gap1_vertical is Direction.D else 1.0
    n3x = 1.0 if gap2_vertical is Direction.U else -1.0
    sigma1 = off1 * n1x
    sigma2 = delta2 * n3x
    src_y = sy + src_off - sigma1 * n0y
    by_y = by - sigma1 * n2y
    tgt_y = ty + tgt_off - sigma2 * n4y
    centerline = [
        (sx, src_y),
        (gap1_mid, src_y),
        (gap1_mid, by_y),
        (gap2_mid, by_y),
        (gap2_mid, tgt_y),
        (effective_tx, tgt_y),
    ]

    # Declare each gap's CHANNEL bundle so the builder anchors its corners on
    # the innermost line that actually co-travels the descent/rise -- the
    # ``g*_n`` lines sharing the channel, not the wider junction fan that only
    # shares the lead-in pivot.  A line that peels off and descends alone
    # (``g1_n == 1``) then turns at the floor with a single-line radius rather
    # than the fan's wide sweep.  Each fan is built relative to this line at its
    # own ``g*_j`` rank, so the member is always included whatever the lead-in
    # position placed its offset at.
    def channel_fan(member_off: float, rank: int, n: int, sign: float) -> list[float]:
        return [member_off + (rank - i) * ctx.offset_step * sign for i in range(n)]

    src_anchor = channel_fan(sigma1, g1_j, g1_n, n1x)
    tgt_anchor = channel_fan(sigma2, g2_j, g2_n, n3x)
    return route_tapered_anchored(
        (edge, edge.line_id, sigma1, sigma2),
        centerline,
        transition_leg=3,
        base_radius=ctx.curve_radius,
        src_bundle_offsets=src_anchor,
        tgt_bundle_offsets=tgt_anchor,
        normalize_exempt=False,
    )


def _route_l_shape(
    edge: Edge, src: Station, tgt: Station, i: int, n: int, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Standard L-shape inter-section route with concentric arcs."""
    fan = ctx.junction_fan_info.get((edge.source, edge.target, edge.line_id))
    if fan is None:
        return _route_l_shape_plain(edge, src, tgt, n, ctx)
    return _route_l_shape_fan(edge, src, tgt, fan, ctx)


def _route_l_shape_plain(
    edge: Edge, src: Station, tgt: Station, n: int, ctx: _RoutingCtx
) -> RoutedPath | None:
    """L-shape for a self-contained bundle: centreline + tapering fan.

    One H -> V -> H centreline.  The source fan (an exit port / merge junction)
    and the target entry trunk can have different spreads, so the bundle tapers
    (each line lands on its own offset at both ends).  A vertical leg shorter
    than its two corners shrinks the base radius to fit.
    """
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    dx = tx - sx

    max_r = ctx.curve_radius + (n - 1) * ctx.offset_step
    mid_x = inter_column_channel_x(
        ctx.graph, src, tgt, sx, tx, dx, max_r, ctx.offset_step
    )
    half_width = (n - 1) * ctx.offset_step / 2
    mid_x = clear_channel_of_section_edge(
        ctx.graph,
        mid_x,
        half_width,
        min(sy, ty),
        max(sy, ty),
        endpoint_port_xs(ctx.graph, edge),
    )

    return route_hvh_tapered(
        ctx,
        edge,
        src,
        tgt,
        mid_x,
        base_radius=ctx.curve_radius,
        min_radius=COORD_TOLERANCE,
        fit_segment=True,
    )


def _route_l_shape_fan(
    edge: Edge,
    src: Station,
    tgt: Station,
    fan: tuple[int, int],
    ctx: _RoutingCtx,
) -> RoutedPath:
    """L-shape whose first corner is shared with bypass siblings.

    The source-side curve is shared with bypass siblings that pivot through the
    same channel but continue past instead of turning, so the channel is placed
    and fanned on the combined junction fan-out (``fan``), like the entry-wrap
    handlers.  A short horizontal lead-in lets the upstream exit -> junction
    segment curve into the descent::

        (lead_x, sy) -> (vx, sy) -> (vx, ty) -> (tx, ty)

    This is the bundle's centreline; the lone member sits ``delta`` off it and
    its fan-mates sit at their own ranks against the same centreline, so
    :func:`build_concentric_bundle` derives every corner radius from the turn
    geometry and the bundle cannot flip or pinch.
    """
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    horizontal = horizontal_direction(tx - sx)
    vertical = vertical_direction(ty - sy)

    ui, un = fan
    delta = l_shape_stagger(ui, un, vertical, ctx.offset_step)
    # Channel centre within the combined fan-out: every fan line diverges at sx,
    # so the source-side curve sits on the OUTSIDE of the source section.  The
    # sign follows travel; the fan half-width keeps the whole bundle clear.
    half_width = (un - 1) * ctx.offset_step / 2
    mid_x = sx + horizontal.sign * (ctx.curve_radius + half_width)
    # The fan pivots through ``sx +/- curve_radius``, hugging the source edge;
    # when that edge is a section bbox border the descent grazes it, so push the
    # channel outward until the nearest line clears.
    mid_x = clear_channel_of_section_edge(
        ctx.graph,
        mid_x,
        half_width,
        min(sy, ty),
        max(sy, ty),
        endpoint_port_xs(ctx.graph, edge),
    )

    # Lead-in long enough for the outermost fan line's first-corner arc; it
    # overlaps the upstream same-line tail (re-joined by the fan-out tail pass),
    # so the extra length is free.
    lead_len = ctx.curve_radius + 2 * half_width
    src_off = _get_offset(ctx, edge.source, edge.line_id)
    tgt_off = _get_offset(ctx, edge.target, edge.line_id)
    centerline = [
        (mid_x - horizontal.sign * lead_len, sy + src_off + delta),
        (mid_x, sy + src_off + delta),
        (mid_x, ty + tgt_off + delta),
        (tx, ty + tgt_off + delta),
    ]
    # Not normalize-exempt: L-shape fans from one junction to different targets
    # share the inter-column gap, and _normalize_gap_channels restacks them into
    # distinct channels so two lines never overlay the same descent.
    route = route_along(
        edge,
        [(edge, edge.line_id, -delta)],
        centerline,
        base_radius=ctx.curve_radius,
        bundle_offsets=fan_offsets(un, ctx.offset_step),
        normalize_exempt=False,
    )
    assert route is not None  # the lone member is always in its own bundle
    return route


def _source_exit_side(graph: MetroGraph, src: Station) -> Direction | None:
    """Horizontal side a route leaves its source section from, if any.

    Returns ``Direction.L`` / ``Direction.R`` when the source is a left/right
    exit port, or a junction fed (directly or transitively) by one; ``None``
    when the source has no horizontal exit side (e.g. a TOP/BOTTOM port).
    """
    seen: set[str] = set()
    cur: str | None = src.id
    while cur is not None and cur not in seen:
        seen.add(cur)
        port = graph.ports.get(cur)
        if port is not None and not port.is_entry:
            if port.side == PortSide.RIGHT:
                return Direction.R
            if port.side == PortSide.LEFT:
                return Direction.L
            return None
        if cur in graph.junctions:
            cur = next(
                (e.source for e in graph.edges if e.target == cur),
                None,
            )
            continue
        return None
    return None


def _route_perp_exit_drop(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Straight vertical drop from a perpendicular exit into an aligned entry.

    A TOP/BOTTOM exit on a horizontal-flow section and the TOP/BOTTOM entry it
    feeds share an X (the target trunk is aligned to the exit), so the
    inter-section leg is a single straight segment.  Each line drops at the
    target trunk's per-line X offset so a co-travelling bundle stays parallel
    down to the port and on into the trunk, merging only at the first real
    station inside the target.
    """
    x = tgt.x + _tb_x_offset(ctx, edge.target, edge.line_id, tgt.section_id)
    return route_along(
        edge,
        [(edge, edge.line_id, 0.0)],
        [(x, src.y), (x, tgt.y)],
        base_radius=ctx.curve_radius,
    )


def _route_perp_exit(
    edge: Edge,
    src: Station,
    tgt: Station,
    src_col: int | None,
    tgt_col: int | None,
    ctx: _RoutingCtx,
) -> RoutedPath | None:
    """Route a perpendicular (TOP/BOTTOM) exit on a horizontal-flow section.

    A column-aligned drop into a TB/BT trunk is a straight vertical (the trunk
    is aligned under the exit X by ``_align_drop_target_trunk``); a side entry
    or a cross-column perpendicular entry goes up and over the source section.
    Returns ``None`` when *src* is not such an exit.
    """
    graph = ctx.graph
    src_port = graph.ports.get(edge.source)
    if (
        src_port is None
        or src_port.is_entry
        or src_port.side not in (PortSide.TOP, PortSide.BOTTOM)
        or src.section_id in ctx.tb_sections
    ):
        return None
    tgt_port = graph.ports.get(edge.target)
    is_aligned_drop = (
        tgt_port is not None
        and tgt_port.is_entry
        and tgt_port.side in (PortSide.TOP, PortSide.BOTTOM)
        and tgt.section_id in ctx.tb_sections
        and src_col == tgt_col
    )
    if is_aligned_drop:
        return _route_perp_exit_drop(edge, src, tgt, ctx)
    return _route_perp_exit_over(edge, src, tgt, ctx)


def _route_perp_exit_over(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath:
    """Up-and-over route from a perpendicular exit that does not drop straight.

    A TOP/BOTTOM exit on a horizontal-flow section whose target is not a
    column-aligned vertical drop (a side entry, or a perpendicular entry in
    another column) leaves the section vertically, rises (TOP) or descends
    (BOTTOM) into the inter-row header band that clears the source section,
    runs across, then descends to the target's own row and turns straight in::

        (lift)     (corridor)      (descent)      (into target)
        port -> up -> over -> down to station Y -> straight into entry

    The polyline above is the bundle's centreline; every co-travelling line is
    fanned as a perpendicular offset of it by the bundle builder, which anchors
    each corner on the bundle's innermost-of-turn line so no arc pinches below
    the floor radius.  The vertical legs carry the source-side riser lateral and
    the final turn-in carries the target's per-line Y, so a side entry tapers
    between the two while a perp-entry trunk drop stays rigid.
    """
    graph = ctx.graph
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    src_port = graph.ports[edge.source]
    tgt_port = graph.ports.get(edge.target)
    src_sec = resolve_section(graph, src)
    tgt_sec = resolve_section(graph, tgt)
    base = ctx.curve_radius
    is_top = src_port.side == PortSide.TOP
    row = src_sec.grid_row if src_sec is not None else None

    # The co-travelling bundle: every line leaving this perpendicular exit for
    # the same target rises into the corridor together.  Each contributes its
    # source-side lateral so the builder anchors every corner on the bundle's
    # innermost-of-turn line.
    _member_edges, line_ids, _edge_by_line = gather_member_edges(graph, edge)

    def source_lateral(line_id: str) -> float:
        """The centreline's source-side perpendicular offset for *line_id*.

        ``_perp_riser_lateral`` keeps the raw per-line X on a TOP riser and
        reverses it on a BOTTOM one; the right-hand normal on the centreline's
        vertical legs reverses the BOTTOM sign back, so it is negated here.
        """
        d = _perp_riser_lateral(
            ctx, edge.source, line_id, src_port.side, src.section_id
        )
        return d if is_top else -d

    src_offs = {lid: source_lateral(lid) for lid in line_ids}

    # Corridor Y: the header band clearing the source section's near edge.
    cy_base = (
        header_corridor_y(graph, row, below=not is_top, base_radius=base, default=sy)
        if row is not None
        else sy - base
        if is_top
        else sy + base
    )

    perp_entry = (
        tgt_port is not None
        and tgt_port.is_entry
        and tgt_port.side in (PortSide.TOP, PortSide.BOTTOM)
    )
    if perp_entry:
        # Perpendicular entry: descend straight on the target trunk's per-line
        # X and stop there.  The matching entry drop continues from that same
        # X, so ending the corridor short of the port centre keeps the two
        # legs one continuous line instead of jogging onto the port marker.
        centerline = [(sx, sy), (sx, cy_base), (tx, cy_base), (tx, ty)]
        route = route_along(
            edge,
            [(edge, edge.line_id, src_offs[edge.line_id])],
            centerline,
            base_radius=ctx.curve_radius,
            bundle_offsets=[src_offs[lid] for lid in line_ids],
        )
    else:
        # Side entry: descend in the inter-column gap to the consumer's row and
        # turn straight in, holding each line on the target section's per-line Y
        # so the bundle stays stacked into the station marker rather than
        # collapsing onto the entry-port Y (which would hide all but one line).
        src_col = src_sec.grid_col if src_sec is not None else 0
        tgt_col = tgt_sec.grid_col if tgt_sec is not None else src_col
        gap_x = column_gap_midpoint(graph, src_col, tgt_col, row)
        centerline = [
            (sx, sy),
            (sx, cy_base),
            (gap_x, cy_base),
            (gap_x, ty),
            (tx, ty),
        ]
        tgt_offs = {lid: _get_offset(ctx, edge.target, lid) for lid in line_ids}
        routes = build_tapered_bundle(
            [(edge, edge.line_id, src_offs[edge.line_id], tgt_offs[edge.line_id])],
            centerline,
            transition_leg=3,
            base_radius=ctx.curve_radius,
            bundle_offsets=[(src_offs[lid], tgt_offs[lid]) for lid in line_ids],
        )
        route = next((r for r in routes if r.line_id == edge.line_id), None)

    assert route is not None
    return route


def _route_top_entry_l_shape(
    edge: Edge, src: Station, tgt: Station, n: int, ctx: _RoutingCtx
) -> RoutedPath:
    """Staircase route into a TOP entry port, fanned along one centreline.

    A short horizontal lead-in lets the transition from any preceding
    horizontal edge (e.g. exit -> junction) curve smoothly into a vertical
    drop, then a trunk run in the inter-row gap above the target section drops
    cleanly into the port::

        (sx,sy) -> (lx, sy) -> (lx, hy) -> (tx, hy) -> (tx, ty)

    This is the bundle's reference centreline; every co-travelling line is fanned
    as a per-leg offset of it (rigid for an LR/RL drop, tapering into a TB/BT
    trunk), mirroring how LEFT entry ports receive a vertical run in the
    inter-column gap.
    """
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    dx = tx - sx
    dy = ty - sy

    # Y for the horizontal trunk channel in the inter-row gap.
    mid_y = inter_row_channel_y(ctx.graph, src, tgt, sy, ty, dy, ctx.curve_radius)

    # For a same-row cross-column producer the generic fallback in
    # inter_row_channel_y places the channel at ty + clearance (inside the
    # section bbox).  The route must approach the TOP entry from ABOVE the
    # boundary, so override mid_y to sit above the row's top edge.
    src_sec = resolve_section(ctx.graph, src)
    tgt_sec = resolve_section(ctx.graph, tgt)
    if (
        src_sec is not None
        and tgt_sec is not None
        and src_sec.grid_row == tgt_sec.grid_row
        and mid_y > ty
    ):
        # The route must approach the TOP entry from above the row's top edge.
        mid_y = header_corridor_y(
            ctx.graph,
            tgt_sec.grid_row,
            below=False,
            base_radius=ctx.curve_radius,
            default=ty,
        )

    # A multi-line bundle fans the channel toward the source box (the line
    # nearest it sits a bundle-width above the centre); keep the centre low
    # enough that even that line clears the source section's bottom edge.
    if (
        n > 1
        and src_sec is not None
        and tgt_sec is not None
        and src_sec.grid_row != tgt_sec.grid_row
    ):
        src_bottom = src_sec.bbox_y + src_sec.bbox_h
        max_off = (n - 1) * ctx.offset_step
        mid_y = max(mid_y, src_bottom + INTER_ROW_EDGE_CLEARANCE + max_off)

    # Horizontal lead-in: a short run so the corner from horizontal to
    # vertical gets a proper curve.  The line leaves the source on the side
    # it physically exits from (a right/left exit port, or a junction fed by
    # one): a right exit whose target trunk sits to its LEFT must clear the
    # source section on the right and double back over the inter-row gap (a
    # right-down-left-down shape), so following dx would turn the line back
    # across the source box.  Falls back to dx for sources with no horizontal
    # exit side, and to the upstream-feeder direction for near-vertical
    # junction sources.
    exit_side = _source_exit_side(ctx.graph, src)
    if exit_side is not None:
        lead = exit_side
    elif abs(dx) > ctx.curve_radius:
        lead = horizontal_direction(dx)
    else:
        lead = Direction.R
        if src.id in ctx.graph.junctions:
            for je in ctx.graph.edges:
                if je.target == src.id:
                    js = ctx.graph.stations.get(je.source)
                    if js and js.is_port:
                        lead = Direction.R if js.x < src.x else Direction.L
                        break

    lx0 = sx + lead.sign * ctx.curve_radius

    # Anchor the centreline on the bundle's reference line (source offset 0) and
    # fan every co-travelling line as a per-leg offset of it, so each corner
    # radius is derived from the turn geometry rather than hand-signed.  The
    # source-side legs carry the source fan offset and the final drop the target
    # offset (transition_leg below), so the bundle tapers when they differ.
    _member_edges, line_ids, edge_by_line = gather_member_edges(ctx.graph, edge)

    def src_offset(line_id: str) -> float:
        return _get_offset(ctx, edge.source, line_id)

    # Reference line: the source-offset-0 line the centreline anchors on.
    ref_lid = min(line_ids, key=src_offset)

    # Into a TB/BT trunk each line lands on its trunk X offset so the bundle
    # flows straight on rather than converging on the shared port and re-fanning
    # (a boundary pinch); the target spread is the trunk's, not the source fan's,
    # so the bundle tapers.  An LR/RL drop lands on its own source-fan channel,
    # paired with the in-section drop -- target offset equals source offset, a
    # rigid bundle.
    if tgt_sec is not None and tgt_sec.direction in ("TB", "BT"):

        def tb_offset(line_id: str) -> float:
            return _tb_x_offset(ctx, edge.target, line_id, tgt_sec.id)

        ref_tb = tb_offset(ref_lid)
        final_x = tx + ref_tb
        members = [
            (edge_by_line[lid], lid, src_offset(lid), ref_tb - tb_offset(lid))
            for lid in line_ids
        ]
    else:
        final_x = tx
        members = [
            (edge_by_line[lid], lid, src_offset(lid), src_offset(lid))
            for lid in line_ids
        ]

    # When the lead-in already sits at the landing X the trunk leg collapses;
    # drop straight from the lead-in and jog into the port instead.
    if abs(lx0 - final_x) <= ctx.curve_radius:
        centerline = [(sx, sy), (lx0, sy), (lx0, ty), (final_x, ty)]
        transition_leg = 2
    else:
        centerline = [
            (sx, sy),
            (lx0, sy),
            (lx0, mid_y),
            (final_x, mid_y),
            (final_x, ty),
        ]
        transition_leg = 3

    routes = build_tapered_bundle(
        members,
        centerline,
        transition_leg,
        base_radius=ctx.curve_radius,
        normalize_exempt=True,
    )
    return next(r for r in routes if r.line_id == edge.line_id)


def _route_left_exit_left_entry_drop(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Drop a LEFT exit into a LEFT entry stacked directly below.

    Both ports sit on the left edge of one grid column.  Run a short lead
    out to the left of the column, drop vertically in that channel, then
    come back in to the target's left entry port::

        (sx,sy) -> (vx,sy) -> (vx,ty) -> (tx,ty)

    The channel ``vx`` is placed just left of the column's leftmost edge so
    the connector never re-enters either section's bbox.
    """
    src_col = _resolve_section_col(ctx.graph, src)
    left_edge = col_left_edge(ctx.graph, src_col, default=min(src.x, tgt.x))
    channel_x = min(left_edge, src.x, tgt.x) - ctx.curve_radius - ctx.offset_step

    return route_hvh_tapered(
        ctx, edge, src, tgt, channel_x, base_radius=ctx.curve_radius
    )


def _left_entry_descent_x(
    ctx: _RoutingCtx, anchor_x: float, n_outer: int, signed_delta: float = 0.0
) -> float:
    """Descent-channel X for a LEFT-entry bundle, left of *anchor_x*.

    Places the bundle ``base_gap`` (curve radius + one offset step) left of
    *anchor_x*, bumping further when that gap would bring the bundle's
    innermost line within ``SECTION_ROUTE_CLEARANCE`` of the edge.  Callers
    pass the per-line stagger as *signed_delta* (``+delta`` when the channel
    sits on the bundle's right, ``-delta`` when on its left) to keep the
    concentric-corner handedness local to each handler.
    """
    base_gap = ctx.curve_radius + ctx.offset_step
    max_delta = (n_outer - 1) * ctx.offset_step / 2
    extra_clearance = max(0.0, SECTION_ROUTE_CLEARANCE - (base_gap - max_delta))
    return anchor_x - base_gap - extra_clearance + signed_delta


def _right_entry_descent_x(
    ctx: _RoutingCtx, anchor_x: float, n_outer: int, signed_delta: float = 0.0
) -> float:
    """Descent-channel X for a RIGHT-entry bundle, right of *anchor_x*.

    The mirror of :func:`_left_entry_descent_x`: places the bundle ``base_gap``
    right of *anchor_x*, bumping further when that gap would bring the bundle's
    innermost line within ``SECTION_ROUTE_CLEARANCE`` of the edge.
    """
    base_gap = ctx.curve_radius + ctx.offset_step
    max_delta = (n_outer - 1) * ctx.offset_step / 2
    extra_clearance = max(0.0, SECTION_ROUTE_CLEARANCE - (base_gap - max_delta))
    return anchor_x + base_gap + extra_clearance + signed_delta


def _v1_corner_x(ctx: _RoutingCtx, src: Station, sx: float, corner_x: float) -> float:
    """Push *corner_x* right so the source-side V1 channel keeps
    ``SECTION_ROUTE_CLEARANCE`` from the source section's right edge.

    When the source station sits at its section's right edge (e.g. a
    right-side exit port), the default lead-in lands the closest line only
    ~curve_radius past the edge, which reads as flush.  A junction source
    already offset past the edge yields a zero bump.
    """
    src_section = ctx.graph.sections.get(src.section_id) if src.section_id else None
    if src_section and src_section.bbox_w > 0:
        section_right = src_section.bbox_x + src_section.bbox_w
    else:
        section_right = sx
    current_gap = sx + ctx.curve_radius - section_right
    return corner_x + max(0.0, SECTION_ROUTE_CLEARANCE - current_gap)


def _wrap_fan_geometry(
    ctx: _RoutingCtx, edge: Edge, src: Station, i: int, n: int, vertical: Direction
) -> tuple[tuple[int, int] | None, int, float, float]:
    """Resolve an entry-wrap's bundle stagger and source-side first corner.

    Unifies the junction fan and the edge's own ``(i, n)`` sub-bundle into one
    stagger: a fanned wrap takes its rank from the shared junction fan (so its
    V1 downturn stays bundled with the junction's other downturning siblings),
    an un-fanned one from its own sub-bundle.  Returns ``(fan, pos_n, delta,
    corner_x)`` -- the fan tuple (or ``None``), the bundle size, this line's
    lateral offset, and the first-corner X (lead-in right of the source, clear
    of its edge).
    """
    fan = ctx.junction_fan_info.get((edge.source, edge.target, edge.line_id))
    pos_i, pos_n = fan if fan is not None else (i, n)
    delta = l_shape_stagger(pos_i, pos_n, vertical, ctx.offset_step)
    mid_x = src.x + ctx.curve_radius + (pos_n - 1) * ctx.offset_step / 2
    corner_x = _v1_corner_x(ctx, src, src.x, mid_x)
    return fan, pos_n, delta, corner_x


def _route_entry_wrap(
    edge: Edge,
    src: Station,
    entry_port: Station,
    ctx: _RoutingCtx,
    *,
    pos_n: int,
    delta: float,
    corner_x: float,
    channel_y: float,
    descent_x: float,
    entry_side: PortSide,
) -> RoutedPath:
    """Fan a single-member entry-wrap loop along its centreline.

    Every entry-wrap shape -- LEFT or RIGHT entry, reached through the inter-row
    gap, the bypass band below the source row, or the around-below loop -- is the
    same 6-point R-D-?-D-? loop and differs only in three inputs the caller
    resolves from its own geometry: the horizontal channel Y (*channel_y*), the
    descent channel X (*descent_x*), and which edge the port sits on
    (*entry_side*)::

        (sx, sy)        -> H lead-in right of the source
        (corner_x, sy)  ; turn down
        (corner_x, cy)  -> V into the channel
        (vx, cy)        -> H along the channel to the descent X
        (vx, ey)        -> V to the entry Y
        (ex, ey)        -> H into the port from its own outward side

    This is the bundle's centreline; the lone member sits ``delta`` off it and
    its fan-mates sit at their own ranks against the same centreline, so
    :func:`build_concentric_bundle` derives every corner radius from the turn
    geometry and the loop can neither flip nor pinch.  Each port endpoint bakes
    the member's normal-projected stagger so the line lands on its station
    offset there: ``+delta`` at the source lead-in (runs rightward) and at a LEFT
    entry (runs rightward in), ``-delta`` at a RIGHT entry (runs leftward in).
    """
    sx, sy = src.x, src.y
    ex, ey = entry_port.x, entry_port.y
    entry_delta = delta if entry_side is PortSide.LEFT else -delta
    src_off = _get_offset(ctx, edge.source, edge.line_id)
    tgt_off = _get_offset(ctx, edge.target, edge.line_id)
    centerline = [
        (sx, sy + src_off + delta),
        (corner_x, sy + src_off + delta),
        (corner_x, channel_y),
        (descent_x, channel_y),
        (descent_x, ey + tgt_off + entry_delta),
        (ex, ey + tgt_off + entry_delta),
    ]
    route = route_along(
        edge,
        [(edge, edge.line_id, -delta)],
        centerline,
        base_radius=ctx.curve_radius,
        bundle_offsets=fan_offsets(pos_n, ctx.offset_step),
    )
    assert route is not None  # the lone member is always in its own bundle
    return route


def _route_left_entry_wrap(
    edge: Edge, src: Station, tgt: Station, i: int, n: int, ctx: _RoutingCtx
) -> RoutedPath:
    """Route to a LEFT entry port by wrapping around the left side.

    When the source is to the RIGHT of a LEFT entry port AND the sections
    are stacked vertically (so the standard L-shape would cut horizontally
    through the target section's interior to reach the left-side entry),
    drop straight down from the source, run leftward in the inter-row gap
    past the target section's left edge, then drop down and into the LEFT
    entry port::

        (sx,sy) -> (sx, hy) -> (vx, hy) -> (vx, ty) -> (tx, ty)

    This mirrors :func:`_route_right_entry_wrap` and avoids the
    "cut through intervening section" anti-pattern.

    Built via :func:`route_along` from the bundle's centreline: the loop is
    described once at the bundle centre, this line sits ``delta`` off it, and
    its siblings sit at their own ranks against the same centreline, so
    :func:`build_concentric_bundle` nests every corner concentrically and the
    R-D-L-D-R loop cannot flip.
    """
    sy, ty = src.y, tgt.y
    dy = ty - sy
    fan, pos_n, delta, corner_x = _wrap_fan_geometry(
        ctx, edge, src, i, n, vertical_direction(dy)
    )

    # Horizontal channel Y in the inter-row gap.  ``inter_row_channel_y`` clamps
    # the per-line stagger inside the clearance band (a narrow gap must not let
    # the run graze the source box); the builder re-adds ``delta`` on the
    # leftward traverse, so pre-subtract it here to land on the clamped Y.
    hy = inter_row_channel_y(ctx.graph, src, tgt, sy, ty, dy, ctx.curve_radius, delta)
    hy -= delta

    # V2 descent channel centre, left of the target section.
    vx = _left_entry_descent_x(ctx, tgt.x, pos_n)
    # When this wrap shares a junction fan with a corridor feeder descending
    # the same target column, anchor the descent channel to the column's LEFT
    # edge so the spine and the corridor overlay as one bundle instead of smearing.
    if fan is not None and _fan_has_corridor_sibling(edge.source, ctx):
        tgt_col = _resolve_section_col(ctx.graph, tgt)
        if tgt_col is not None:
            shared_vx = _fan_left_entry_descent_x(ctx, tgt_col, pos_n, 0.0)
            if shared_vx is not None:
                vx = shared_vx

    return _route_entry_wrap(
        edge,
        src,
        tgt,
        ctx,
        pos_n=pos_n,
        delta=delta,
        corner_x=corner_x,
        channel_y=hy,
        descent_x=vx,
        entry_side=PortSide.LEFT,
    )


def _has_bypass_sibling_to_same_entry(
    edge: Edge,
    entry_port: Station,
    ctx: _RoutingCtx,
) -> bool:
    """Detect whether a sibling merge trunk's bypass shares the V_up gap.

    Mirrors :func:`_has_around_section_sibling` (which lives on the
    trunk side and answers "is there an around-route sharing my gap?").
    Used by :func:`_route_around_section_below` to decide whether the
    V_up channel shares its gap with a bypass bundle (in which case
    the around-route is bundle index 1 in the symmetric layout) or
    has the gap to itself (bundle index 0 of 1).
    """
    if entry_port is None:
        return False
    ep_id = entry_port.id
    # Walk back from the entry port through the merge-junction graph
    # to find the merge junction this entry_port serves.
    for mj_id, mapped_ep in ctx.merge.entry_port_for.items():
        if mapped_ep != ep_id:
            continue
        # mj_id is a merge junction whose entry_port is ours.  Check
        # whether the trunk source feeding it routes via bypass.
        trunk_src = ctx.merge.trunk_source.get(mj_id)
        if trunk_src is None or trunk_src == edge.source:
            continue
        return True
    return False


def _corridor_descent_x(
    ctx: _RoutingCtx, ep_col: int, ep_row: int, delta: float
) -> float | None:
    """X of the inter-column channel just LEFT of the target column.

    The corridor descends the clear gap between ``ep_col - 1`` and
    ``ep_col`` measured at the *target* row, so a wide row-span section in a
    different row does not collapse the gap.  Returns ``None`` when there is no
    column to the left (degenerate; caller falls back to the around-below loop).
    """
    if ep_col <= 0:
        return None
    gap_left, gap_right = column_gap_edges(ctx.graph, ep_col - 1, ep_col, row=ep_row)
    if gap_right <= gap_left:
        return None
    # +delta (not -delta): the L->D corner into this channel is concentric
    # only when vx + r is constant across the bundle.  r_inner shrinks for
    # the +delta (rightmost) line, so that line must sit at the LARGER vx;
    # the opposite sign delaminates the descent corner.
    return (gap_left + gap_right) / 2 + delta


def _fan_left_entry_descent_x(
    ctx: _RoutingCtx, tgt_col: int, n_outer: int, delta: float
) -> float | None:
    """Shared descent-channel X for a junction fan's LEFT-entry targets.

    When one junction fans the same lines to two LEFT-entry sections
    stacked in the same column - one reached by :func:`_route_left_entry_wrap`
    (the spine), the other by :func:`_route_inter_row_gap_corridor` (the QC
    feed) - both bundles must descend the SAME vertical channel so they
    overlay as one clean bundle rather than smearing a few px apart.

    Anchor the channel to the column's LEFT edge (the leftmost section left
    edge across all rows of *tgt_col*) so both handlers, whose individual
    targets sit at slightly different x, agree on one channel.  The
    per-line ``delta`` stagger is preserved.  Returns ``None`` when the
    column has no measurable left edge.
    """
    col_left = col_left_edge(ctx.graph, tgt_col, default=0.0)
    if col_left <= 0.0:
        return None
    return _left_entry_descent_x(ctx, col_left, n_outer, delta)


def _fan_has_corridor_sibling(junction_id: str, ctx: _RoutingCtx) -> bool:
    """True if *junction_id* fans an edge routed via the inter-row-gap corridor.

    Used so a sibling :func:`_route_left_entry_wrap` spine aligns its descent
    channel with the corridor feeder's.  A corridor feeder is a
    downward cross-row edge into a LEFT-entry section (merge junction or
    direct port) for which :func:`_corridor_is_viable` holds.
    """
    graph = ctx.graph
    for edge in graph.edges_from(junction_id):
        tgt = graph.stations.get(edge.target)
        if tgt is None:
            continue
        ep_id = ctx.merge.entry_port_for.get(edge.target)
        ep = graph.stations.get(ep_id) if ep_id else tgt
        if ep is not None and _corridor_is_viable(ctx, graph.stations[junction_id], ep):
            return True
    return False


def _corridor_is_viable(ctx: _RoutingCtx, src: Station, entry_port: Station) -> bool:
    """Whether the inter-row-gap + inter-column-channel corridor exists.

    Used to route a downward cross-row merge feeder through the clear
    corridor instead of the canvas-bottom loop
    (:func:`_route_around_section_below`).  Requires:

    * a LEFT entry port (the corridor descends just left of the target);
    * the target section sits in a row strictly *below* the source's row
      (a downward cross-row feeder; same-row fan-ins U-route in the gap
      below the row and must keep the legacy handler);
    * an inter-row gap below the source row exists in the source's column;
    * a clear inter-column channel exists left of the target column.
    """
    if entry_port is None:
        return False
    ep_port = ctx.graph.ports.get(entry_port.id)
    if ep_port is None or ep_port.side != PortSide.LEFT:
        return False
    src_col, src_row = _resolve_section_colrow(ctx.graph, src)
    ep_col, ep_row = _resolve_section_colrow(ctx.graph, entry_port)
    if src_row is None or ep_row is None or src_col is None or ep_col is None:
        return False
    if ep_row <= src_row:
        return False
    if _corridor_descent_x(ctx, ep_col, ep_row, 0.0) is None:
        return False
    # The leftward traverse runs in a band INTER_ROW_EDGE_CLEARANCE below the
    # source-row bottom and INTER_ROW_HEADER_CLEARANCE above the lower row's
    # header badge, with the bundle's per-line stagger inside it.  A gap too
    # narrow for that band collapses the stagger onto one Y (a collinear
    # overlay) and forces the leftward run through the source box's bottom
    # edge; below it the feeder routes around the section instead.
    gap_top = row_bottom_edge(ctx.graph, src_row, col=src_col)
    gap_bottom = row_top_edge(ctx.graph, src_row + 1, col=src_col, default=gap_top)
    # The traverse carries only the bundle this source feeds into the entry
    # port (its co-travelling lines), so size the band by that bundle's
    # stagger - not by every line the port receives from other sources.
    bundle_lines = {
        e.line_id
        for e in ctx.graph.edges_from(src.id)
        if e.target == entry_port.id
        or ctx.merge.entry_port_for.get(e.target) == entry_port.id
    }
    # Section placement reserves exactly this band for the wrap bundle, so a
    # corridor sized for it sits right at the boundary; absorb float dust so
    # an exactly-reserved gap stays viable.
    required = inter_row_wrap_band(len(bundle_lines))
    return gap_bottom - gap_top >= required - COORD_TOLERANCE


def _route_inter_row_gap_corridor(
    edge: Edge,
    src: Station,
    tgt: Station,
    entry_port: Station,
    i: int,
    n: int,
    ctx: _RoutingCtx,
) -> RoutedPath | None:
    """Route a downward cross-row LEFT-entry merge feeder via the clear
    inter-row / inter-column corridor instead of the canvas-bottom loop.

    A multi-row collector fan-in feeds the left-entry ``reporting`` section
    (row 3) from QC sources exiting on the right in rows 0 and 1.  Rather
    than dropping to the canvas bottom (below the tall ``variant_calling``
    row-span) and climbing back up (:func:`_route_around_section_below`),
    descend through the corridor that genuinely exists::

        (lx, sy)        -> H lead-in right of source
        (corner_x, sy)  ; turn down
        (corner_x, gy)  -> V down to the inter-row gap below the source row
        (vx, gy)        -> H left in that gap to the inter-column channel
        (vx, ey)        -> V down the channel to the entry Y
        (ex, ey)        -> H right into the LEFT entry port

    All feeders converge in the same inter-column channel (``vx``) just
    left of the target column, so they travel down together as one bundle
    meeting the carriage-return spine, rather than two separate loops.

    The feeder is described as its centreline through the corridor with the
    line offset by its bundle position ``delta``; build_concentric_bundle then
    derives the concentric R->D->L->D->R corner radii, so each feeder nests
    against its siblings in the shared channel without a hand-picked radius.
    """
    # The source-side first corner and the per-line stagger come from the same
    # fan geometry as the sibling wrap (_route_left_entry_wrap), so a corridor
    # feeder and a wrap sharing a junction fan overlay rather than smear apart.
    fan, pos_n, delta, corner_x = _wrap_fan_geometry(
        ctx, edge, src, i, n, vertical_direction(entry_port.y - src.y)
    )

    src_col, src_row = _resolve_section_colrow(ctx.graph, src)
    ep_col, ep_row = _resolve_section_colrow(ctx.graph, entry_port)
    # Guaranteed by the _corridor_is_viable check at every call site.
    assert (
        src_col is not None
        and src_row is not None
        and ep_col is not None
        and ep_row is not None
    )

    # Inter-row gap Y just below the source row (column-restricted so a
    # tall row-span in another column doesn't push the channel down).  Use
    # the header-aware band so the leftward traverse clears the next row's
    # section-header badge, not just the bbox edge.
    gap_top = row_bottom_edge(ctx.graph, src_row, col=src_col)
    gap_bottom = row_top_edge(ctx.graph, src_row + 1, col=src_col, default=gap_top)
    if fan is not None:
        # Share the sibling wrap's inter-row band: it centres the leftward
        # traverse in the gap below the SOURCE row using the global (non
        # column-restricted) row edges, so the two bundles' H legs coincide
        # rather than smearing 3px apart.
        wrap_top = row_bottom_edge(ctx.graph, src_row, default=gap_top)
        wrap_bottom = row_top_edge(ctx.graph, src_row + 1, default=wrap_top)
        gy_base = _center_inter_row_channel(wrap_top, wrap_bottom)
    elif gap_bottom > gap_top:
        gy_base = _center_inter_row_channel(gap_top, gap_bottom)
    else:
        gy_base = gap_top + INTER_ROW_EDGE_CLEARANCE
    # Keep the channel inside the clearance band: at least
    # INTER_ROW_EDGE_CLEARANCE below the source-row bottom and clear of the
    # next row's header badge.  Skipped for fan feeders, which share the wrap
    # sibling's (unclamped) band so the two bundles' H legs coincide.
    if fan is None and gap_bottom > gap_top:
        gy_base = min(
            max(gy_base, gap_top + INTER_ROW_EDGE_CLEARANCE),
            gap_bottom - INTER_ROW_HEADER_CLEARANCE,
        )

    # Inter-column descent channel left of the target column.  For a fan
    # feeder, anchor it to the target COLUMN's left edge (shared with the
    # sibling wrap) so the two bundles descend the same channel; otherwise
    # use the inter-column gap midpoint.
    vx: float | None = None
    if fan is not None and ep_col is not None:
        vx = _fan_left_entry_descent_x(ctx, ep_col, pos_n, 0.0)
    if vx is None:
        vx = _corridor_descent_x(ctx, ep_col, ep_row, 0.0)
    assert vx is not None

    return _route_entry_wrap(
        edge,
        src,
        entry_port,
        ctx,
        pos_n=pos_n,
        delta=delta,
        corner_x=corner_x,
        channel_y=gy_base,
        descent_x=vx,
        entry_side=PortSide.LEFT,
    )


def _descent_rightward_clearable_pierce(
    ctx: _RoutingCtx, x: float, y_lo: float, y_hi: float, exclude: set[str]
) -> bool:
    """True if a vertical channel at *x* over ``[y_lo, y_hi]`` cuts through a
    section interior and can be cleared to its right.

    A zero-margin clear pinned to ``bound_left=x`` only moves the channel when
    it sits strictly inside a box (so a non-trivial rightward shift is exactly
    a pierce); a channel that merely runs near a box edge is not flagged.  This
    mirrors the band the actual divert below uses, so detection and clearing
    agree.
    """
    return (
        _clear_channel_x_in_band(ctx.graph, x, y_lo, y_hi, 0.0, exclude, bound_left=x)
        > x + COORD_TOLERANCE
    )


def _route_around_section_below(
    edge: Edge,
    src: Station,
    tgt: Station,
    entry_port: Station,
    i: int,
    n: int,
    ctx: _RoutingCtx,
) -> RoutedPath | None:
    """Route to a LEFT entry port by going AROUND BELOW the target section.

    Used when a standard L-shape or :func:`_route_left_entry_wrap` would
    have its horizontal segment cross an intervening section's bbox.
    Routes via 4 corners in a clockwise R-D-L-U-R loop that descends
    past the target row's bottom, runs leftward under everything, rises
    in the inter-section gap to the entry Y, and enters the LEFT port
    from below::

        (lx, sy) -> (cx, sy)          ; H lead-in right
        (cx, sy) -> (cx, by)          ; V down past target row's bottom
        (cx, by) -> (vx, by)          ; H left past target's left edge
        (vx, by) -> (vx, ey)          ; V up to entry Y
        (vx, ey) -> (ex, ey)          ; H right into LEFT entry port

    All four corners are clockwise (R->D, D->L, L->U, U->R), so the
    outer line of the bundle stays on the OUTSIDE of every turn and
    gets the larger radius throughout.

    *tgt* is the L-shape's nominal target (the edge target, often a
    merge junction).  *entry_port* is the actual endpoint of the route
    (the LEFT entry port station resolved from the merge junction or
    equal to *tgt* when the edge targets a port directly).
    """
    sy = src.y
    ex, ey = entry_port.x, entry_port.y

    # The route shares its first corner with any sibling routes from the same
    # junction (junction_fan_info pivots all outgoing edges through one shared
    # corner; merge-branch edges are excluded, so for the merge case the fan is
    # typically absent and the edge's own bundle position is used).
    _fan, pos_n, delta, corner_x = _wrap_fan_geometry(
        ctx, edge, src, i, n, vertical_direction(ey - sy)
    )

    # Bypass Y below all sections in the column range so the route
    # clears every intervening section (cross_row=True).
    src_col, src_row = _resolve_section_colrow(ctx.graph, src)
    ep_col = _resolve_section_col(ctx.graph, entry_port)
    # Fallbacks if a column can't be resolved (degenerate cases).
    bc_src_col = src_col if src_col is not None else 0
    bc_tgt_col = ep_col if ep_col is not None else bc_src_col
    by = bypass_bottom_y(
        ctx.graph,
        bc_src_col,
        bc_tgt_col,
        BYPASS_CLEARANCE,
        src_row=src_row,
        cross_row=True,
    )

    # Vertical V_up channel sits just left of the target section's bbox.
    ep_section = (
        ctx.graph.sections.get(entry_port.section_id) if entry_port.section_id else None
    )
    if ep_section and ep_section.bbox_w > 0:
        section_left = ep_section.bbox_x
    else:
        section_left = ex

    # V_up X: position the bundle centre within the inter-column gap just
    # left of the target section, using the principled symmetric placement.
    # When a sibling merge-trunk bypass shares this gap, we're bundle 1
    # (rightmost); else we're the sole bundle.
    paired_with_bypass = _has_bypass_sibling_to_same_entry(edge, entry_port, ctx)
    if ep_col is not None and ep_col > 0:
        gap_left, gap_right = column_gap_edges(ctx.graph, ep_col - 1, ep_col)
        bw = bundle_width(pos_n, ctx.offset_step)
        widths = [bw, bw] if paired_with_bypass else [bw]
        bundle_idx = 1 if paired_with_bypass else 0
        vx = symmetric_bundle_midpoint(gap_left, gap_right, widths, bundle_idx)
        # Sanity floor: keep the V_up clear of the target section's left
        # edge when the gap is too narrow for full symmetric placement.
        vx = min(vx, _left_entry_descent_x(ctx, section_left, pos_n))
    else:
        # Fallback for degenerate cases without column info: anchored to the
        # target section's left edge.
        vx = _left_entry_descent_x(ctx, section_left, pos_n)

    # The V1 channel descends from the source row to the bypass bottom.  When
    # it would cut THROUGH a section stacked below the source (one wider than
    # the source, so its box spans the channel), divert the bundle's channel
    # clear of it.  A channel that merely runs near a box edge is left
    # untouched.  The clearance steps past the box far enough to also miss any
    # LEFT-entry wrap hugging that section's right edge (box_right +
    # curve_radius), so a line the descent shares with such a wrap reads as a
    # distinct corridor, not two near-parallel tracks.
    exclude = {src.section_id} if src.section_id else set[str]()
    if _descent_rightward_clearable_pierce(ctx, corner_x, sy, by, exclude):
        clearance = (
            SECTION_ROUTE_CLEARANCE + ctx.curve_radius + EDGE_TO_BUNDLE_CLEARANCE
        )
        corner_x = _clear_channel_x_in_band(
            ctx.graph, corner_x, sy, by, clearance, exclude, bound_left=corner_x
        )

    # R-D-L-U-R loop: down past the target row's bottom (by), left of the target
    # column (vx), up to the entry Y, and into the LEFT port from below.
    return _route_entry_wrap(
        edge,
        src,
        entry_port,
        ctx,
        pos_n=pos_n,
        delta=delta,
        corner_x=corner_x,
        channel_y=by,
        descent_x=vx,
        entry_side=PortSide.LEFT,
    )


def _route_right_entry_over_top(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Loop a same-row left source over the top of a TB section's RIGHT port.

    The source sits at (nearly) the port's own Y, so a straight or under-the-box
    channel would cut the interior.  The bundle instead rises over the section's
    top edge, runs right past its right edge, and descends into the RIGHT port
    from the port's own outward side.  Approaching a right-side port from the
    left is a U-turn, which transposes the bundle end-to-end; the descent into
    the section therefore reverses the lines, matched by the section's reversed
    internal order (see ``_reverse_tb_right_entry_offsets``).

    Built via :func:`build_concentric_bundle` from the bundle's centreline, so
    the loop cannot flip and every corner is concentric by construction.
    """
    graph = ctx.graph
    # A U-turn keeps the bundle centred on its source mean end-to-end (the
    # descent's reversal is matched by the section's reversed internal order).
    members, src_center, _ = gather_bundle(ctx, edge)

    sx, sy = src.x, src.y
    ex, ey = tgt.x, tgt.y
    ep_section = graph.sections.get(tgt.section_id) if tgt.section_id else None
    section_right = (
        ep_section.bbox_x + ep_section.bbox_w
        if ep_section and ep_section.bbox_w > 0
        else ex
    )
    # The horizontal runs over the target section's own top, so the channel
    # clears the section's header badge (it protrudes SECTION_HEADER_PROTRUSION
    # above bbox_y), not merely the box edge.
    section_top = ep_section.bbox_y if ep_section else min(sy, ey)
    channel_y = section_top - INTER_ROW_HEADER_CLEARANCE - ctx.curve_radius
    lead_x = sx + ctx.curve_radius + ctx.offset_step
    descent_x = (
        section_right + ctx.curve_radius + ctx.offset_step + SECTION_ROUTE_CLEARANCE
    )
    mid_sy = sy + src_center
    mid_ey = ey + src_center
    centerline = [
        (sx, mid_sy),
        (lead_x, mid_sy),
        (lead_x, channel_y),
        (descent_x, channel_y),
        (descent_x, mid_ey),
        (ex, mid_ey),
    ]
    return route_along(edge, members, centerline, base_radius=ctx.curve_radius)


def _route_right_entry_wrap(
    edge: Edge, src: Station, tgt: Station, i: int, n: int, ctx: _RoutingCtx
) -> RoutedPath:
    """Route to a RIGHT entry port by wrapping around the right side.

    When the source is to the LEFT of a RIGHT entry port, the standard
    L-shape would cut horizontally through the target section.  Instead,
    drop into the inter-row gap, run horizontally past the target
    section's right edge, then drop into the RIGHT entry port::

        (sx,sy) -> (lx, sy) -> (lx, hy) -> (vx, hy) -> (vx, ty) -> (tx, ty)

    For cross-row cases, the horizontal channel runs just below the
    source row's sections (bypass style) so the line stays high and
    only drops down when it reaches the target column.

    This avoids crossing through intervening sections.

    Cross-row sources route via :func:`route_along` from the bundle's
    centreline: the R-D-R-D-L loop is described once at the bundle centre, this
    line sits ``delta`` off it, and :func:`build_concentric_bundle` nests every
    corner concentrically so the loop cannot flip.  Same-row sources delegate to
    :func:`_route_right_entry_over_top` (also a centreline build).
    """
    sy, tx, ty = src.y, tgt.x, tgt.y
    vertical = vertical_direction(ty - sy)

    # Detect cross-row case: use bypass-style Y just below the source
    # row's sections so the line runs horizontally under the adjacent
    # section before dropping to the target row.
    src_col, src_row = _resolve_section_colrow(ctx.graph, src)
    tgt_col, tgt_row = _resolve_section_colrow(ctx.graph, tgt)

    cross_row = (
        src_row is not None
        and tgt_row is not None
        and src_row != tgt_row
        and src_col is not None
        and tgt_col is not None
    )

    if not cross_row:
        # Same-row source: loop over the top into the right port (the channel
        # below would cut the interior).  Built as a concentric bundle.
        over_top = _route_right_entry_over_top(edge, src, tgt, ctx)
        assert over_top is not None  # edge is always among its own bundle members
        return over_top

    assert src_col is not None and tgt_col is not None

    _fan, pos_n, delta, corner_x = _wrap_fan_geometry(ctx, edge, src, i, n, vertical)

    # Horizontal channel Y centre, below the source row's sections.
    hy = bypass_bottom_y(ctx.graph, src_col, tgt_col, BYPASS_CLEARANCE, src_row=src_row)

    # V2 descent channel centre, just past the entry port in the gap to the
    # right of the target column.
    vx = _right_entry_descent_x(ctx, tx, pos_n)

    return _route_entry_wrap(
        edge,
        src,
        tgt,
        ctx,
        pos_n=pos_n,
        delta=delta,
        corner_x=corner_x,
        channel_y=hy,
        descent_x=vx,
        entry_side=PortSide.RIGHT,
    )


def _right_entry_gap_above_target_y(
    graph: MetroGraph, src_row: int
) -> tuple[float, float]:
    """Return ``(gap_top, gap_bottom)`` of the inter-row band below *src_row*.

    The band sits between the source row's bottom edge and the next row's
    top edge.  Computed over all columns (not column-restricted) so the
    long rightward traverse stays clear of every section in the span.
    """
    gap_top = row_bottom_edge(graph, src_row)
    gap_bottom = row_top_edge(graph, src_row + 1, default=gap_top)
    return gap_top, gap_bottom


def _right_entry_gap_above_is_clear(
    graph: MetroGraph,
    src: Station,
    tgt: Station,
    entry_port: Station,
    src_row: int,
) -> bool:
    """Whether a RIGHT-entry feed from above can use the inter-row gap.

    The route runs its long horizontal in the band just below the source
    row, then drops straight down the RIGHT side of the target column into
    the port.  Viable only when that band genuinely exists (the next row's
    top is below the source row's bottom) and the horizontal at the band's
    centre crosses no section interior between the source and the target's
    right edge.
    """
    gap_top, gap_bottom = _right_entry_gap_above_target_y(graph, src_row)
    if gap_bottom <= gap_top:
        return False
    gy = _center_inter_row_channel(gap_top, gap_bottom)

    ep_section = (
        graph.sections.get(entry_port.section_id) if entry_port.section_id else None
    )
    section_right = (
        ep_section.bbox_x + ep_section.bbox_w
        if ep_section and ep_section.bbox_w > 0
        else entry_port.x
    )
    # Horizontal run spans the source X out to just past the target's right
    # edge (where the descent channel sits).  Exclude the source and target
    # sections themselves; any OTHER section the band crosses kills the gap
    # route (fall back to the around-below loop).
    exclude = {sid for sid in (src.section_id, tgt.section_id) if sid is not None}
    return not _h_segment_crosses_other_section(
        graph, src.x, section_right, gy, exclude
    )


def _build_right_entry_wrap_route(
    edge: Edge,
    src: Station,
    entry_port: Station,
    i: int,
    n: int,
    ctx: _RoutingCtx,
    channel_y_base: float,
) -> RoutedPath:
    """Build a wrap route into a RIGHT entry port from its outward side.

    Shared body of :func:`_route_right_entry_via_gap_above` and
    :func:`_route_right_entry_around_below`, which differ only in the
    horizontal channel they pass.  Leads right out of the source, drops to
    ``channel_y_base``, runs right past the target's right edge, then turns to
    the entry Y and in to the RIGHT port from ``vx >= ex`` (its outward side),
    never crossing the section interior.

    Built via :func:`route_along` from the bundle's centreline: the loop is
    described once at the bundle centre, this line sits ``delta`` off it, and
    :func:`build_concentric_bundle` nests every corner concentrically so the
    R-D-R-D-L loop cannot flip.
    """
    ex = entry_port.x

    _fan, pos_n, delta, corner_x = _wrap_fan_geometry(
        ctx, edge, src, i, n, vertical_direction(entry_port.y - src.y)
    )

    # V_down/up channel centre, just RIGHT of the target section's bbox in the
    # gap to the right of the target column.
    ep_section = (
        ctx.graph.sections.get(entry_port.section_id) if entry_port.section_id else None
    )
    section_right = (
        ep_section.bbox_x + ep_section.bbox_w
        if ep_section and ep_section.bbox_w > 0
        else ex
    )
    vx = _right_entry_descent_x(ctx, section_right, pos_n)

    # R-D-R-D-L loop: down to the traverse channel, right past the target's
    # right edge, then in to the RIGHT port from its own outward side.
    return _route_entry_wrap(
        edge,
        src,
        entry_port,
        ctx,
        pos_n=pos_n,
        delta=delta,
        corner_x=corner_x,
        channel_y=channel_y_base,
        descent_x=vx,
        entry_side=PortSide.RIGHT,
    )


def _route_right_entry_via_gap_above(
    edge: Edge,
    src: Station,
    tgt: Station,
    entry_port: Station,
    i: int,
    n: int,
    ctx: _RoutingCtx,
    src_row: int,
) -> RoutedPath:
    """Route to a RIGHT entry port via the inter-row gap ABOVE the target row.

    Used when the source sits in a row ABOVE the target's row.  Going UNDER
    the whole target row (:func:`_route_right_entry_around_below`) would run
    the long rightward horizontal counter to the target row's flow.  Instead
    run that horizontal in the clear inter-row band just below the source
    row, then drop straight down the RIGHT side of the target column into the
    RIGHT entry port::

        (lx, sy) -> (cx, sy)        ; H lead-in right out of the source
        (cx, sy) -> (cx, gy)        ; V down into the inter-row gap
        (cx, gy) -> (vx, gy)        ; H right past the target's right edge
        (vx, gy) -> (vx, ey)        ; V down to the entry Y
        (vx, ey) -> (ex, ey)        ; H left into the RIGHT entry port

    The approach to the port arrives from ``vx >= ex`` (the port's own
    outward side), and the horizontal never crosses a section interior
    (guaranteed by :func:`_right_entry_gap_above_is_clear` at the call site).
    """
    gap_top, gap_bottom = _right_entry_gap_above_target_y(ctx.graph, src_row)
    channel_y_base = _center_inter_row_channel(gap_top, gap_bottom)
    return _build_right_entry_wrap_route(
        edge, src, entry_port, i, n, ctx, channel_y_base
    )


def _route_right_entry_around_below(
    edge: Edge,
    src: Station,
    tgt: Station,
    entry_port: Station,
    i: int,
    n: int,
    ctx: _RoutingCtx,
) -> RoutedPath:
    """Route to a RIGHT entry port by going AROUND BELOW the target section.

    The mirror of :func:`_route_around_section_below`.  Used when the
    source sits to the LEFT of a RIGHT entry port across intervening
    sections, so a standard bypass would rise in the inter-column gap
    LEFT of the target and then run its final horizontal RIGHTWARD across
    the section interior to reach the right-edge port (the route would
    enter the box's far side and double back).  Instead, descend past the
    target row's bottom, run leftward-to-rightward under everything, rise
    in the gap to the RIGHT of the target box, then enter the RIGHT port
    from the right::

        (lx, sy) -> (cx, sy)        ; H lead-in right out of the source
        (cx, sy) -> (cx, by)        ; V down past the target row's bottom
        (cx, by) -> (vx, by)        ; H right past the target's right edge
        (vx, by) -> (vx, ey)        ; V up to the entry Y
        (vx, ey) -> (ex, ey)        ; H left into the RIGHT entry port

    The approach to the port arrives from ``vx >= ex`` (the port's own
    outward side), never crossing the section interior.
    """
    # Bypass Y below all sections in the column range so the route clears
    # every intervening section, including the target row.
    src_col, src_row = _resolve_section_colrow(ctx.graph, src)
    ep_col = _resolve_section_col(ctx.graph, entry_port)
    bc_src_col = src_col if src_col is not None else 0
    bc_tgt_col = ep_col if ep_col is not None else bc_src_col
    channel_y_base = bypass_bottom_y(
        ctx.graph,
        bc_src_col,
        bc_tgt_col,
        BYPASS_CLEARANCE,
        src_row=src_row,
        cross_row=True,
    )
    return _build_right_entry_wrap_route(
        edge, src, entry_port, i, n, ctx, channel_y_base
    )
